"""WoW Addon Updater - install and update World of Warcraft addons from GitHub repos.

Double-click to run (needs Python 3.9+ from python.org, no extra packages).
Settings and the addon list are stored in addons.json next to this file.
"""

import json
import os
import queue
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "addons.json")
API = "https://api.github.com"

# (label shown in the window, flavor id used by addon packagers, filename hints)
FLAVORS = [
    ("Retail", "mainline", ["mainline", "retail"]),
    ("Forever", "classic", ["classic", "vanilla"]),
]
ALL_HINTS = sorted({h for _, _, hints in FLAVORS for h in hints})
TOC_SUFFIX = re.compile(
    r"[-_](mainline|classic|vanilla|tbc|bcc|wrath|wotlkc|cata|mists|standard)$", re.I
)


class UpdaterError(Exception):
    pass


# ----------------------------------------------------------------------------
# GitHub
# ----------------------------------------------------------------------------

def _open(url, token=None, api=False):
    headers = {"User-Agent": "wow-addon-updater"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        if token:
            headers["Authorization"] = "Bearer " + token
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)


def api_get(path, token=None):
    """GET a GitHub API path. Returns parsed JSON, or None on 404."""
    try:
        with _open(API + path, token, api=True) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            raise UpdaterError("GitHub rate limit reached - add a token or wait an hour")
        if e.code == 401:
            raise UpdaterError("GitHub rejected the token")
        raise UpdaterError("GitHub error %s" % e.code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdaterError("Network error: %s" % getattr(e, "reason", e))


def parse_repo(text):
    """Accept 'owner/repo' or any github.com URL. Returns 'owner/repo' or None."""
    text = text.strip()
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", text)
    if not m:
        m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", text)
    if not m:
        return None
    name = m.group(2)
    if name.endswith(".git"):
        name = name[:-4]
    return "%s/%s" % (m.group(1), name)


def _hints_in(filename):
    parts = re.split(r"[-_. ]+", filename.lower())
    return {h for h in ALL_HINTS if h in parts}


def pick_asset(release, flavor):
    """Choose the right zip from a release for the given game flavor.

    Returns the asset dict, or None if the release has no zips at all.
    """
    assets = release.get("assets") or []
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    if not zips:
        return None
    by_name = {a["name"]: a for a in zips}

    # Releases built with the BigWigs packager include release.json, which
    # says exactly which zip is for which game flavor.
    meta = next((a for a in assets if a["name"] == "release.json"), None)
    if meta:
        try:
            with _open(meta["browser_download_url"]) as resp:
                info = json.load(resp)
            for rel in info.get("releases", []):
                if rel.get("nolib"):
                    continue
                flavors = {m.get("flavor") for m in rel.get("metadata", [])}
                if flavor in flavors and rel.get("filename") in by_name:
                    return by_name[rel["filename"]]
        except Exception:
            pass  # fall through to guessing from filenames

    full = [a for a in zips if "nolib" not in a["name"].lower()] or zips
    mine = next(h for _, f, h in FLAVORS if f == flavor)
    tagged = [a for a in full if _hints_in(a["name"]) & set(mine)]
    if tagged:
        return tagged[0]
    untagged = [a for a in full if not _hints_in(a["name"])]
    if untagged:
        return untagged[0]
    raise UpdaterError("Latest release has no download for this game version")


def get_latest(repo, flavor, token=None):
    """Return {'version', 'url', 'source'} for the newest thing to install."""
    release = api_get("/repos/%s/releases/latest" % repo, token)
    if release:
        tag = release["tag_name"]
        asset = pick_asset(release, flavor)
        if asset:
            return {"version": tag, "url": asset["browser_download_url"], "source": "release"}
        url = "https://github.com/%s/archive/refs/tags/%s.zip" % (repo, urllib.parse.quote(tag))
        return {"version": tag, "url": url, "source": "tag (source)"}

    info = api_get("/repos/%s" % repo, token)
    if not info:
        raise UpdaterError("Repository not found (or private)")
    branch = info["default_branch"]
    commit = api_get("/repos/%s/commits/%s" % (repo, urllib.parse.quote(branch, safe="")), token)
    if not commit:
        raise UpdaterError("Repository is empty")
    sha = commit["sha"]
    url = "https://github.com/%s/archive/%s.zip" % (repo, sha)
    return {"version": sha[:7], "url": url, "source": "%s (source)" % branch}


# ----------------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------------

def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path):
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_force_remove)


def _safe_name(name):
    return bool(name) and name not in (".", "..") and name == os.path.basename(name)


def _safe_extract(zip_path, dest):
    dest_real = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(dest, member))
            if target != dest_real and not target.startswith(dest_real + os.sep):
                raise UpdaterError("Zip contains an unsafe path")
        zf.extractall(dest)


def _addon_name(folder, is_root):
    """Folder name WoW expects: it must match the .toc file name."""
    tocs = [f[:-4] for f in os.listdir(folder) if f.lower().endswith(".toc")]
    bases = sorted({TOC_SUFFIX.sub("", t) for t in tocs}, key=len)
    current = os.path.basename(folder)
    if not is_root:
        for b in bases:
            if b.lower() == current.lower():
                return current
    return bases[0]


def find_addon_dirs(root):
    """Return [(path, name)] for every top-most folder containing a .toc."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if any(f.lower().endswith(".toc") for f in filenames):
            found.append((dirpath, _addon_name(dirpath, dirpath == root)))
            dirnames[:] = []
    return found


def install(url, addons_dir, old_folders):
    """Download a zip and install its addon folder(s). Returns folder names."""
    tmp = tempfile.mkdtemp(prefix="wowaddon_")
    try:
        zip_path = os.path.join(tmp, "download.zip")
        try:
            with _open(url) as resp, open(zip_path, "wb") as out:
                shutil.copyfileobj(resp, out)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UpdaterError("Download failed: %s" % getattr(e, "reason", e))

        extracted = os.path.join(tmp, "x")
        os.mkdir(extracted)
        try:
            _safe_extract(zip_path, extracted)
        except zipfile.BadZipFile:
            raise UpdaterError("Downloaded file is not a valid zip")

        addons = find_addon_dirs(extracted)
        if not addons:
            raise UpdaterError("No .toc file found - is this a WoW addon?")
        names = [n for _, n in addons]
        if len(set(n.lower() for n in names)) != len(names):
            raise UpdaterError("Zip contains duplicate addon folders")

        for name in set(old_folders) | set(names):
            if _safe_name(name):
                _rmtree(os.path.join(addons_dir, name))
        for src, name in addons:
            shutil.move(src, os.path.join(addons_dir, name))
        return names
    finally:
        _rmtree(tmp)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

def guess_addons_dir():
    for drive in ("C:", "D:", "E:"):
        for base in (r"\Program Files (x86)\World of Warcraft", r"\World of Warcraft",
                     r"\Games\World of Warcraft"):
            path = drive + base + r"\_retail_\Interface\AddOns"
            if os.path.isdir(path):
                return path
    return ""


def load_config():
    cfg = {"addons_dir": "", "flavor": "classic", "token": "", "dark_mode": False, "addons": []}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        cfg["addons_dir"] = guess_addons_dir()
    except (OSError, ValueError):
        pass
    if cfg.get("flavor") not in {f for _, f, _ in FLAVORS}:
        cfg["flavor"] = "classic"
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def addon_status(addon, latest, addons_dir):
    if not addon.get("installed_version"):
        return "Not installed"
    missing = [f for f in addon.get("folders", [])
               if not os.path.isdir(os.path.join(addons_dir, f))]
    if missing or not addon.get("folders"):
        return "Files missing"
    if addon["installed_version"] != latest["version"]:
        return "Update available"
    return "Up to date"


# ----------------------------------------------------------------------------
# Window
# ----------------------------------------------------------------------------

def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    cfg = load_config()
    events = queue.Queue()
    state = {"busy": False}

    root = tk.Tk()
    root.title("WoW Addon Updater")
    root.geometry("980x570")
    root.minsize(780, 440)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    dark_var = tk.BooleanVar(value=cfg.get("dark_mode", False))

    def apply_theme():
        dark = dark_var.get()
        bg, panel, fg = ("#171a21", "#222733", "#e7eaf0") if dark else ("#f2f4f8", "#ffffff", "#202633")
        muted = "#aab2c0" if dark else "#606b7a"
        root.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", padding=(12, 7), background=panel, foreground=fg)
        style.map("TButton", background=[("active", "#364255" if dark else "#e4eaf3")])
        style.configure("TEntry", fieldbackground=panel, foreground=fg, insertcolor=fg)
        style.configure("TCombobox", fieldbackground=panel, background=panel, foreground=fg, arrowcolor=fg)
        style.map("TCombobox", fieldbackground=[("readonly", panel)], foreground=[("readonly", fg)])
        style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg,
                        rowheight=29, borderwidth=0)
        style.configure("Treeview.Heading", background=("#303847" if dark else "#e7ebf2"),
                        foreground=fg, padding=(8, 7), font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#345b91" if dark else "#cfe0f7")],
                  foreground=[("selected", "#ffffff" if dark else fg)])
        style.configure("Status.TLabel", background=panel, foreground=muted, padding=(10, 7))

    # --- settings rows ---
    top = ttk.Frame(root, padding=(18, 16, 18, 10))
    top.pack(fill="x")
    top.columnconfigure(1, weight=1)

    ttk.Label(top, text="WoW Addon Updater", font=("Segoe UI Semibold", 17)).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    ttk.Label(top, text="AddOns folder").grid(row=1, column=0, sticky="w", padx=(0, 12))
    dir_var = tk.StringVar(value=cfg["addons_dir"])
    ttk.Entry(top, textvariable=dir_var).grid(row=1, column=1, sticky="ew", ipady=4)

    def browse():
        path = filedialog.askdirectory(title="Select your Interface\\AddOns folder",
                                       initialdir=dir_var.get() or None)
        if path:
            dir_var.set(os.path.normpath(path))
            persist()

    ttk.Button(top, text="Browse…", command=browse).grid(row=1, column=2, padx=(8, 0))

    ttk.Label(top, text="Game version").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
    row2 = ttk.Frame(top)
    row2.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(10, 0))
    labels = [l for l, _, _ in FLAVORS]
    current = next((l for l, f, _ in FLAVORS if f == cfg["flavor"]), "Forever")
    flavor_var = tk.StringVar(value=current)
    ttk.Combobox(row2, textvariable=flavor_var, values=labels, state="readonly",
                 width=16).pack(side="left")
    ttk.Label(row2, text="GitHub token (optional)").pack(side="left", padx=(18, 8))
    token_var = tk.StringVar(value=cfg["token"])
    ttk.Entry(row2, textvariable=token_var, show="*", width=30).pack(side="left", ipady=3)
    ttk.Checkbutton(top, text="Dark mode", variable=dark_var,
                    command=lambda: (apply_theme(), persist())).grid(
        row=3, column=1, sticky="w", pady=(10, 0))

    # --- addon list ---
    mid = ttk.Frame(root, padding=(18, 4, 18, 8))
    mid.pack(fill="both", expand=True)
    cols = ("repo", "installed", "latest", "source", "status")
    tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
    for col, text, width in (("repo", "Repository", 260), ("installed", "Installed", 110),
                             ("latest", "Latest", 110), ("source", "Source", 120),
                             ("status", "Status", 220)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w", stretch=col in ("repo", "status"))
    scroll = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    tree.tag_configure("ok", foreground="#2e7d32")
    tree.tag_configure("update", foreground="#b35c00")
    tree.tag_configure("error", foreground="#c62828")

    # --- buttons + status bar ---
    bottom = ttk.Frame(root, padding=(18, 6, 18, 10))
    bottom.pack(fill="x")
    status_var = tk.StringVar(value="Ready")
    ttk.Label(root, textvariable=status_var, style="Status.TLabel", anchor="w").pack(
        fill="x", side="bottom", padx=18, pady=(0, 14))

    def flavor_id():
        return next(f for l, f, _ in FLAVORS if l == flavor_var.get())

    def persist():
        cfg["addons_dir"] = dir_var.get().strip()
        cfg["flavor"] = flavor_id()
        cfg["token"] = token_var.get().strip()
        cfg["dark_mode"] = dark_var.get()
        try:
            save_config(cfg)
        except OSError as e:
            status_var.set("Could not save settings: %s" % e)

    def tag_for(status):
        if status == "Up to date":
            return ("ok",)
        if status.startswith("Error"):
            return ("error",)
        if status in ("Update available", "Not installed", "Files missing"):
            return ("update",)
        return ()

    def set_row(repo, latest=None, source=None, status=None):
        if not tree.exists(repo):
            return
        addon = next((a for a in cfg["addons"] if a["repo"] == repo), None)
        if addon:
            tree.set(repo, "installed", addon.get("installed_version") or "-")
        if latest is not None:
            tree.set(repo, "latest", latest)
        if source is not None:
            tree.set(repo, "source", source)
        if status is not None:
            tree.set(repo, "status", status)
            tree.item(repo, tags=tag_for(status))

    def add_row(addon):
        tree.insert("", "end", iid=addon["repo"], values=(
            addon["repo"], addon.get("installed_version") or "-", "-", "-", "Not checked"))

    for a in cfg["addons"]:
        add_row(a)

    # --- background work ---
    def worker(repos, do_install):
        addons_dir, flavor, token = cfg["addons_dir"], cfg["flavor"], cfg["token"] or None
        updated = failed = pending = 0
        for repo in repos:
            addon = next((a for a in cfg["addons"] if a["repo"] == repo), None)
            if not addon:
                continue
            events.put(("row", repo, {"status": "Checking..."}))
            try:
                latest = get_latest(repo, flavor, token)
                status = addon_status(addon, latest, addons_dir)
                events.put(("row", repo, {"latest": latest["version"],
                                          "source": latest["source"], "status": status}))
                if status == "Up to date":
                    continue
                if not do_install:
                    pending += 1
                    continue
                events.put(("row", repo, {"status": "Downloading..."}))
                folders = install(latest["url"], addons_dir, addon.get("folders", []))
                addon["installed_version"] = latest["version"]
                addon["folders"] = folders
                events.put(("save",))
                events.put(("row", repo, {"status": "Up to date"}))
                updated += 1
            except UpdaterError as e:
                failed += 1
                events.put(("row", repo, {"status": "Error: %s" % e}))
            except Exception as e:  # keep going with the other addons
                failed += 1
                events.put(("row", repo, {"status": "Error: %s" % e}))
        if do_install:
            msg = "Done - %d updated" % updated
        else:
            msg = "Done - %d update(s) available" % pending
        if failed:
            msg += ", %d failed" % failed
        events.put(("done", msg))

    def start(repos, do_install):
        if state["busy"] or not repos:
            return
        persist()
        if do_install:
            path = cfg["addons_dir"]
            if not os.path.isdir(path):
                messagebox.showerror("AddOns folder", "Pick your AddOns folder first.")
                return
            if os.path.basename(os.path.normpath(path)).lower() != "addons":
                if not messagebox.askyesno(
                        "AddOns folder",
                        "This folder isn't named 'AddOns':\n\n%s\n\nInstall here anyway?" % path):
                    return
        state["busy"] = True
        for b in buttons:
            b.state(["disabled"])
        status_var.set("Working...")
        threading.Thread(target=worker, args=(list(repos), do_install), daemon=True).start()

    def poll():
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "row":
                    set_row(ev[1], **ev[2])
                elif ev[0] == "save":
                    save_config(cfg)
                elif ev[0] == "done":
                    state["busy"] = False
                    for b in buttons:
                        b.state(["!disabled"])
                    status_var.set(ev[1])
        except queue.Empty:
            pass
        root.after(100, poll)

    # --- button actions ---
    def all_repos():
        return [a["repo"] for a in cfg["addons"]]

    def selected_repos():
        return list(tree.selection())

    def add_addon():
        text = simpledialog.askstring(
            "Add addon", "GitHub repository (URL or owner/repo):", parent=root)
        if not text:
            return
        repo = parse_repo(text)
        if not repo:
            messagebox.showerror("Add addon", "That doesn't look like a GitHub repository.")
            return
        if any(a["repo"].lower() == repo.lower() for a in cfg["addons"]):
            messagebox.showinfo("Add addon", "%s is already in the list." % repo)
            return
        addon = {"repo": repo, "installed_version": None, "folders": []}
        cfg["addons"].append(addon)
        add_row(addon)
        persist()
        start([repo], False)

    def remove_addon():
        selected = tree.selection()
        if not selected or state["busy"]:
            return
        answer = messagebox.askyesnocancel(
            "Remove", "Remove %d addon(s) from the list.\n\n"
            "Also delete their folders from your AddOns folder?" % len(selected))
        if answer is None:
            return
        for repo in selected:
            addon = next((a for a in cfg["addons"] if a["repo"] == repo), None)
            if addon and answer:
                for name in addon.get("folders", []):
                    if _safe_name(name) and os.path.isdir(cfg["addons_dir"]):
                        _rmtree(os.path.join(cfg["addons_dir"], name))
            cfg["addons"] = [a for a in cfg["addons"] if a["repo"] != repo]
            tree.delete(repo)
        persist()

    buttons = [
        ttk.Button(bottom, text="Add addon...", command=add_addon),
        ttk.Button(bottom, text="Remove", command=remove_addon),
        ttk.Button(bottom, text="Check for updates", command=lambda: start(all_repos(), False)),
        ttk.Button(bottom, text="Check selected", command=lambda: start(selected_repos(), False)),
        ttk.Button(bottom, text="Update selected",
                   command=lambda: start(tree.selection(), True)),
        ttk.Button(bottom, text="Update all", command=lambda: start(all_repos(), True)),
    ]
    buttons[0].pack(side="left")
    buttons[1].pack(side="left", padx=(6, 0))
    buttons[5].pack(side="right")
    buttons[4].pack(side="right", padx=(0, 6))
    buttons[3].pack(side="right", padx=(0, 6))
    buttons[2].pack(side="right", padx=(0, 6))

    apply_theme()

    def on_close():
        persist()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    poll()
    if cfg["addons"]:
        root.after(300, lambda: start(all_repos(), False))
    root.mainloop()


if __name__ == "__main__":
    main()
