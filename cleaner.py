"""Cleanup rules (ported from mac-cleanup.sh + extended), suggestion engine, auto-clean, scheduling."""
import hashlib, json, os, plistlib, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from glob import glob

HOME = os.path.expanduser("~").replace(os.sep, "/")
H = lambda *p: os.path.join(HOME, *p).replace(os.sep, "/")  # diskman ile aynı: içeride "/"
MAC = sys.platform == "darwin"
WIN = sys.platform == "win32"
LAD = os.environ.get("LOCALAPPDATA", "").replace(os.sep, "/")
TRASH = H(".Trash")
CONF_DIR = H(".config/diskman")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
HIST_FILE = os.path.join(CONF_DIR, "history.jsonl")
PLIST = H("Library/LaunchAgents/com.fxerkan.diskman.plist")
# id: (ad, macOS veri klasörü, Linux, Windows (LOCALAPPDATA altı), süreç adları, macOS uygulaması, Linux komutları, Windows exe)
BROWSERS = {
    "chrome": ("Chrome", "Library/Application Support/Google/Chrome", ".config/google-chrome", "Google/Chrome/User Data",
               ["Google Chrome", "chrome"], "Google Chrome", ["google-chrome", "google-chrome-stable"], "chrome.exe"),
    "edge": ("Edge", "Library/Application Support/Microsoft Edge", ".config/microsoft-edge", "Microsoft/Edge/User Data",
             ["Microsoft Edge", "msedge"], "Microsoft Edge", ["microsoft-edge", "microsoft-edge-stable"], "msedge.exe"),
    "brave": ("Brave", "Library/Application Support/BraveSoftware/Brave-Browser", ".config/BraveSoftware/Brave-Browser",
              "BraveSoftware/Brave-Browser/User Data", ["Brave Browser", "brave"], "Brave Browser", ["brave-browser", "brave"], "brave.exe"),
    "chromium": ("Chromium", "Library/Application Support/Chromium", ".config/chromium", "Chromium/User Data",
                 ["Chromium", "chromium", "chromium-browser"], "Chromium", ["chromium", "chromium-browser"], "chromium.exe"),
    "vivaldi": ("Vivaldi", "Library/Application Support/Vivaldi", ".config/vivaldi", "Vivaldi/User Data",
                ["Vivaldi", "vivaldi-bin"], "Vivaldi", ["vivaldi", "vivaldi-stable"], "vivaldi.exe"),
    "firefox": ("Firefox", "Library/Caches/Firefox/Profiles", ".cache/mozilla/firefox", "Mozilla/Firefox/Profiles",
                ["firefox", "Firefox"], "Firefox", ["firefox", "firefox-esr"], "firefox.exe"),
    "safari": ("Safari", "", "", "", [], "Safari", [], ""),
}
PERIODS = ("off", "daily", "weekly", "monthly", "yearly")

DEFAULT_CONF = {
    "browser_profile": "",  # UI'yi açan tarayıcı: "" varsayılan | "firefox" | "chrome:Profile 2"
    "auto": {
        "groups": ["chrome", "app_caches", "pkg_caches", "brew", "logs", "xcode", "vscode"],
        "mode": "rm",                 # rm | trash
        "dev_min_age_days": 30,       # project artifacts: only if the project is untouched this many days
        "dev_names": ["node_modules", ".venv", "venv", ".next", ".nuxt", ".turbo", ".parcel-cache",
                      "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "target", ".gradle",
                      "DerivedData"],
        "custom_globs": [],           # globs relative to home, e.g. "Library/Caches/com.foo.*"
        "chrome_profiles": "all",     # "all" or ["Default", "Profile 2"]
        "schedule": "off",            # off | daily | weekly | monthly | yearly
        "hour": 2,
    },
}

GROUPS = [  # id, name, description, risk
    ("chrome", "Browser caches", "Chrome, Edge, Brave, Chromium, Vivaldi, Firefox: per-profile caches. A browser that is open is skipped.", "safe"),
    ("app_caches", "App caches", "Selected Library/Caches folders + Electron app caches.", "safe"),
    ("pkg_caches", "Package manager caches", "npm, pip, uv, yarn, pnpm, bun, go, cargo, gradle, poetry…", "safe"),
    ("brew", "Homebrew cleanup", "brew cleanup --prune=all (cache + old versions).", "safe"),
    ("logs", "Logs", "~/Library/Logs contents, gcloud logs.", "safe"),
    ("xcode", "Xcode build leftovers", "DerivedData, simulator cache.", "safe"),
    ("vscode", "VS Code orphaned extensions", "Extension folders not in extensions.json or marked in .obsolete.", "safe"),
    ("ml_caches", "AI/ML model caches", "huggingface, torch — re-downloaded on demand (large).", "regen"),
    ("all_caches", "All of Library/Caches", "Every app's cache; apps rebuild them.", "regen"),
    ("dev_artifacts", "Dev artifacts", "node_modules, .venv, .next, target, __pycache__… (in untouched projects).", "regen"),
    ("trash", "Trash", "Empties the Trash.", "regen"),
    ("custom", "Custom rules", "Globs you defined in Settings.", "regen"),
]
GROUP_IDS = [g[0] for g in GROUPS]

CHROME_SUBDIRS = ["Cache", "Code Cache", "GPUCache", "blob_storage", "DawnCache", "DawnGraphiteCache",
                  "DawnWebGPUCache", "AutofillAiModelCache", "Service Worker/CacheStorage",
                  "Service Worker/ScriptCache", "optimization_guide_model_store", "OptimizationHints",
                  "optimization_guide_hint_cache_store", "optimization_guide_model_metadata_store"]
CHROME_SHARED = ["component_crx_cache", "extensions_crx_cache", "screen_ai", "OptGuideOnDeviceModel",
                 "extension_activity", "GrShaderCache", "ShaderCache", "GraphiteDawnCache", "GPUPersistentCache",
                 "ActorSafetyLists", "ClientSidePhishing"]
APP_CACHES = ["pip", "pnpm", "com.apple.python", "com.microsoft.VSCode", "ms-playwright", "com.openai.codex",
              "Metadata", "Mail", "com.tinyapp.TablePlus", "colima", "Microsoft Edge", "com.spotify.client",
              "node-gyp", "com.anthropic.claudefordesktop.ShipIt", "com.teamviewer.TeamViewer", "Plex",
              "CocoaPods", "Yarn", "go-build", "JetBrains"]
APP_SUPPORT_CACHES = ["Cache", "Code Cache", "GPUCache", "CachedData", "Service Worker/CacheStorage",
                      "DawnCache", "DawnGraphiteCache"]
PKG = [".npm/_cacache", ".cache/pip", ".cache/yarn", ".cache/node", ".cache/go", ".cache/go-build",
       ".cache/google-cloud-sdk", ".cache/pypoetry", ".cache/uv", ".cache/pre-commit", ".bun/install/cache",
       ".gradle/caches", ".cargo/registry/cache", "go/pkg/mod/cache", "Library/pnpm/store",
       "Library/Caches/Homebrew/downloads"]
WALK_SKIP = {H("Library"), TRASH, H("Dropbox"), H(".cache"), H(".npm"), H("go"), H(".cargo"), H(".m2"),
             H(".gradle"), H(".Trash"), H(".ollama"), H(".docker")}
DEV_NEED = {"target": "Cargo.toml"}  # these names only count when this file sits next to them
# not user content: tool installs, build/dependency folders
NOT_USER = {"build", "DerivedData", "site-packages", "node_modules", "Pods", "venv", ".venv", "vendor", "dist",
            "target", "SourcePackages", "__pycache__"}


def user_content(p):
    """User's own file? (outside ~/Library, not inside hidden/build/dependency folders)"""
    if not p.startswith(HOME + "/") or p.startswith(H("Library") + "/"):
        return False
    parts = p[len(HOME) + 1:].split("/")[:-1]
    return not any(x.startswith(".") or x in NOT_USER or x.endswith(".app") for x in parts)


def load_conf():
    c = json.loads(json.dumps(DEFAULT_CONF))
    try:
        with open(CONF_FILE) as f:
            u = json.load(f)
        c.update({k: v for k, v in u.items() if k != "auto"})
        c["auto"].update(u.get("auto", {}))
    except (OSError, ValueError):
        pass
    return c


def save_conf(c):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(CONF_FILE, "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)


def du(path):
    try:
        out = subprocess.run(["du", "-skx", path], capture_output=True, text=True, timeout=600).stdout
        return int(out.split("\t", 1)[0]) * 1024
    except Exception:
        return 0


def bdir(b):
    m, l, w = BROWSERS[b][1:4]
    return (H(m) if MAC else f"{LAD}/{w}" if WIN else H(l)) if m else ""


def browser_profiles():
    """Önbelleği temizlenebilen profiller: Chromium tabanlılar (Local State) + Firefox profil klasörleri."""
    out = []
    for b, v in BROWSERS.items():
        d = bdir(b)
        if not d or not os.path.isdir(d):
            continue
        if b == "firefox":
            out += [{"id": f"firefox:{n}", "browser": b, "bname": v[0], "dir": n, "name": n.split(".", 1)[-1]}
                    for n in sorted(os.listdir(d)) if os.path.isdir(os.path.join(d, n))]
            continue
        try:
            with open(os.path.join(d, "Local State"), encoding="utf-8") as f:
                info = json.load(f)["profile"]["info_cache"]
        except (OSError, ValueError, KeyError):
            continue
        out += [{"id": f"{b}:{k}", "browser": b, "bname": v[0], "dir": k, "name": i.get("name", k)} for k, i in sorted(info.items())]
    return out


def _win_exe(exe):
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                return winreg.QueryValue(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
            except OSError:
                pass
    except ImportError:
        pass
    return None


def browser_cmd(b):
    """Tarayıcıyı başlatan komutun başı ya da None (kurulu değil)."""
    v = BROWSERS.get(b)
    if not v:
        return None
    if MAC:
        return ["open", "-na", v[5]] if os.path.exists(f"/Applications/{v[5]}.app") or b == "safari" else None
    if WIN:
        e = v[7] and _win_exe(v[7])
        return [e] if e else None
    w = next((shutil.which(c) for c in v[6] if shutil.which(c)), None)
    return [w] if w else None


def installed_browsers():
    return [{"id": b, "name": v[0]} for b, v in BROWSERS.items() if browser_cmd(b)]


def browser_running(b):
    names = BROWSERS[b][4]
    try:
        if WIN:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout.lower()
            return any(f'"{n.lower()}.exe"' in out for n in names)
        return any(subprocess.run(["pgrep", "-x", n], capture_output=True).returncode == 0 for n in names)
    except OSError:
        return False


def running_browsers():
    return [b for b in BROWSERS if bdir(b) and os.path.isdir(bdir(b)) and browser_running(b)]


def _pid(x):  # eski ayar: yalnız profil klasörü ("Profile 2") = Chrome
    return x if ":" in x else f"chrome:{x}"


def _it(group, path, note, action="rm", **kw):
    return {"group": group, "path": path, "note": note, "action": action, **kw}


def _exists(*items):
    return [i for i in items if os.path.lexists(i["path"]) and not os.path.islink(i["path"])]


def project_age_days(project, skip):
    """When the project last changed: newest mtime of top-level entries (artifact excluded)."""
    newest = 0
    try:
        for e in os.scandir(project):
            if e.name != skip:
                try:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
    except OSError:
        return 0
    return (time.time() - newest) / 86400 if newest else 0


def dev_artifacts(names, min_age):
    names, out = set(names), []
    for root, dirs, _ in os.walk(HOME):
        if root in WALK_SKIP or root.count(os.sep) - HOME.count(os.sep) > 8:
            dirs[:] = []
            continue
        for d in list(dirs):
            p = os.path.join(root, d)
            if root == HOME and d.startswith("."):  # ~/.vscode, ~/.nvm, ~/.local… tool installs, leave alone
                dirs.remove(d)
                continue
            if d in names and (d not in DEV_NEED or os.path.exists(os.path.join(root, DEV_NEED[d]))):
                age = project_age_days(root, d)
                if age >= min_age and "/lib/node_modules" not in p:
                    out.append(_it("dev_artifacts", p, f"Project untouched for {int(age)} days", age=int(age)))
                dirs.remove(d)
            elif p in WALK_SKIP or d in (".git", "Library"):
                dirs.remove(d)
    return out


def vscode_orphans():
    ext = H(".vscode/extensions")
    try:
        with open(os.path.join(ext, "extensions.json")) as f:
            known = {e.get("relativeLocation") for e in json.load(f)}
    except (OSError, ValueError):
        return []
    try:
        with open(os.path.join(ext, ".obsolete")) as f:
            obsolete = set(json.load(f))
    except (OSError, ValueError):
        obsolete = set()
    out = []
    for e in os.scandir(ext):
        if e.is_dir() and not e.name.startswith(".") and (e.name not in known or e.name in obsolete):
            out.append(_it("vscode", e.path, "Unregistered extension folder"))
    return out


def group_items(gid, conf):
    a = conf["auto"]
    if gid == "chrome":
        sel = a.get("chrome_profiles", "all")
        sel = sel if sel == "all" else {_pid(x) for x in sel}
        items, used = [], set()
        for p in browser_profiles():
            if sel != "all" and p["id"] not in sel:
                continue
            b, d = p["browser"], bdir(p["browser"])
            used.add(b)
            subs = ["cache2", "startupCache", "thumbnails"] if b == "firefox" else CHROME_SUBDIRS
            items += [_it(gid, os.path.join(d, p["dir"], s).replace(os.sep, "/"), f"{p['bname']} · {p['name']} · {s}", browser=b)
                      for s in subs]
        for b in used - {"firefox"}:
            items += [_it(gid, f"{bdir(b)}/{s}", f"{BROWSERS[b][0]} · shared · {s}", browser=b) for s in CHROME_SHARED]
        if "chrome" in used and MAC:
            items.append(_it(gid, H("Library/Caches/Google"), "Chrome Library cache", browser="chrome"))
        return _exists(*items)
    if gid == "app_caches":
        items = [_it(gid, H("Library/Caches", c), "App cache") for c in APP_CACHES]
        for sub in APP_SUPPORT_CACHES:
            items += [_it(gid, p, f"{p.split('/')[-2 - sub.count('/')]} · {sub}")
                      for p in glob(H("Library/Application Support/*", sub)) if "/Google/Chrome" not in p]
        return _exists(*items)
    if gid == "pkg_caches":
        return _exists(*[_it(gid, H(p), "Package cache, re-downloaded when needed") for p in PKG])
    if gid == "brew":
        brew = shutil.which("brew") or ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew") else None)
        if not brew:
            return []
        return [_it(gid, H("Library/Caches/Homebrew"), "brew cleanup --prune=all", "cmd", cmd=[brew, "cleanup", "--prune=all"])]
    if gid == "logs":
        return _exists(_it(gid, H("Library/Logs"), "Log files (contents)", "contents"),
                       _it(gid, H(".config/gcloud/logs"), "gcloud logs", "contents"),
                       _it(gid, H(".config/gcloud/.backup"), "gcloud backups", "contents"),
                       _it(gid, H(".config/gcloud/surface_data"), "gcloud surface data"))
    if gid == "xcode":
        return _exists(_it(gid, H("Library/Developer/Xcode/DerivedData"), "Build intermediates", "contents"),
                       _it(gid, H("Library/Developer/CoreSimulator/Caches"), "Simulator cache", "contents"))
    if gid == "vscode":
        return vscode_orphans() + _exists(_it(gid, H(".vscode/extensions/.cache"), "Extension cache"))
    if gid == "ml_caches":
        return _exists(_it(gid, H(".cache/huggingface"), "Hugging Face model cache"),
                       _it(gid, H(".cache/torch"), "PyTorch cache"))
    if gid == "all_caches":
        return _exists(*[_it(gid, p, "App cache") for p in glob(H("Library/Caches/*"))])
    if gid == "dev_artifacts":
        return dev_artifacts(a.get("dev_names", []), a.get("dev_min_age_days", 30))
    if gid == "trash":
        return _exists(_it(gid, TRASH, "Trash (contents)", "contents"))
    if gid == "custom":
        return _exists(*[_it(gid, p, f"Custom rule: {g}") for g in a.get("custom_globs", []) for p in glob(H(g))])
    return []


def collect(conf, groups=None, sizes=True):
    groups = groups or GROUP_IDS
    seen, items = set(), []
    for g in groups:
        for i in group_items(g, conf):
            if i["path"] not in seen:
                seen.add(i["path"])
                items.append(i)
    if sizes:
        with ThreadPoolExecutor(12) as ex:
            for it, s in zip(items, ex.map(lambda i: du(i["path"]), items)):
                it["size"] = s
        items = [i for i in items if i["action"] == "cmd" or i["size"] >= (1 << 20 if i["group"] == "dev_artifacts" else 1)]
    return items


# ---------- execution ----------
def _rm(p):
    if os.path.islink(p) or not os.path.isdir(p):
        os.unlink(p)
        return

    def onerr(fn, path, exc):  # read-only dirs like go mod
        os.chmod(os.path.dirname(path), 0o755)
        if os.path.isdir(path):
            os.chmod(path, 0o755)
        fn(path)
    shutil.rmtree(p, onexc=onerr)


def _trash_for(p):
    """Aynı diskteki çöp klasörü: ev diskindeyse ~/.Trash, değilse o diskin kökünde .Trash (kopyalama yok)."""
    dev = os.lstat(p).st_dev
    if os.stat(HOME).st_dev == dev:
        return TRASH
    m = os.path.dirname(os.path.abspath(p))
    while os.path.dirname(m) != m and os.stat(os.path.dirname(m)).st_dev == dev:
        m = os.path.dirname(m)
    return os.path.join(m, ".Trash")


def to_trash(p):
    if "/.Trash/" in p.replace(os.sep, "/"):
        return _rm(p)
    trash = _trash_for(p)
    os.makedirs(trash, exist_ok=True)
    dst = os.path.join(trash, os.path.basename(p))
    if os.path.lexists(dst):
        dst += time.strftime(" %H-%M-%S") + f"-{os.urandom(2).hex()}"
    shutil.move(p, dst)


def run_item(it, mode, check):
    """Apply one cleanup item. check = diskman.check (location safety check)."""
    if it["action"] == "cmd":
        r = subprocess.run(it["cmd"], capture_output=True, text=True, timeout=1800)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[-200:])
        return
    real = check(it["path"])
    targets = [os.path.join(real, c) for c in os.listdir(real)] if it["action"] == "contents" else [real]
    if real == TRASH:
        mode = "rm"
    for t in targets:
        try:
            (_rm if mode == "rm" else to_trash)(t)
        except FileNotFoundError:
            pass


def free_bytes(paths=()):
    """Etkilenen disklerin boş alanı toplamı (her disk bir kez)."""
    disks = {}
    for p in [HOME, *paths]:
        d = os.path.dirname(p) if not os.path.isdir(p) else p
        try:
            disks.setdefault(os.stat(d).st_dev, d)
        except OSError:
            pass
    return sum(shutil.disk_usage(d).free for d in disks.values())


def clean(items, mode, check, source="manual", progress=None):
    """progress(item, error_or_None) is called after each item (background jobs use it)."""
    paths = [i["path"] for i in items if i.get("action") != "cmd"]
    before, done, errors = free_bytes(paths), [], []
    running = {}
    for it in items:
        if (b := it.get("browser")):
            if b not in running:
                running[b] = browser_running(b)
            if running[b]:
                errors.append({"path": it["path"], "error": f"{BROWSERS[b][0]} is open, skipped"})
                progress and progress(it, errors[-1]["error"])
                continue
        try:
            run_item(it, mode, check)
            done.append(it["path"])
            progress and progress(it, None)
        except Exception as e:
            errors.append({"path": it["path"], "error": str(e)[:200]})
            progress and progress(it, errors[-1]["error"])
    freed = max(0, free_bytes(paths) - before)
    log = {"ts": time.time(), "source": source, "mode": mode, "items": len(done), "errors": len(errors),
           "freed": freed, "estimated": sum(i.get("size", 0) for i in items if i["path"] in done)}
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(HIST_FILE, "a") as f:
        f.write(json.dumps(log) + "\n")
    return {"done": done, "errors": errors, "freed": freed, "estimated": log["estimated"]}


def history(n=50):
    try:
        with open(HIST_FILE) as f:
            return [json.loads(l) for l in f.readlines()[-n:]][::-1]
    except OSError:
        return []


def auto_clean(check, dry=False):
    conf = load_conf()
    items = collect(conf, conf["auto"]["groups"])
    if dry:
        return {"items": items, "estimated": sum(i["size"] for i in items)}
    return clean(items, conf["auto"]["mode"], check, source="auto")


# ---------- scheduling (launchd) ----------
def due(a, t):
    """Zamanlama bu saatte mi çalışmalı? t: time.struct_time (yerel). Haftalık = Pazar, aylık = ayın 1'i, yıllık = 1 Ocak."""
    k = a.get("schedule", "off")
    return k in PERIODS[1:] and t.tm_hour == int(a.get("hour", 2)) and (
        k == "daily" or k == "weekly" and t.tm_wday == 6 or k == "monthly" and t.tm_mday == 1
        or k == "yearly" and t.tm_yday == 1)


def next_run(a, now=None):
    now = now or time.time()
    base = time.localtime(now)
    for d in range(367):
        ts = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + d, int(a.get("hour", 2)), 0, 0, 0, 0, -1))
        if ts > now and due(a, time.localtime(ts)):
            return ts
    return None


def set_schedule(kind, hour, script):
    """macOS uygulama modu: launchd. Servis/konteyner modunda DiskMan'in kendi zamanlayıcısı çalışır (diskman.py)."""
    if not MAC:
        return
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", PLIST], capture_output=True)
    if kind == "off":
        if os.path.exists(PLIST):
            os.remove(PLIST)
        return
    cal = {"Hour": int(hour), "Minute": 0}
    cal.update({"weekly": {"Weekday": 0}, "monthly": {"Day": 1}, "yearly": {"Month": 1, "Day": 1}}.get(kind, {}))
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump({"Label": "com.fxerkan.diskman", "ProgramArguments": [sys.executable, script, "--auto-clean"],
                       "StartCalendarInterval": cal, "StandardOutPath": os.path.join(CONF_DIR, "auto.log"),
                       "StandardErrorPath": os.path.join(CONF_DIR, "auto.log")}, f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())


def schedule_active():
    return MAC and subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.fxerkan.diskman"],
                          capture_output=True).returncode == 0


# ---------- suggestion engine ----------
def _quick_hash(p):
    h = hashlib.sha1()
    try:
        with open(p, "rb") as f:
            h.update(f.read(1 << 20))
            f.seek(-min(1 << 20, os.fstat(f.fileno()).st_size), 2)
            h.update(f.read(1 << 20))
    except OSError:
        return None
    return h.hexdigest()


def _sug(kind, title, reason, path, size, conf, action="delete", **kw):
    return {"kind": kind, "title": title, "reason": reason, "path": path, "size": size,
            "confidence": conf, "action": action, **kw}


def suggestions(big_files, conf):
    """big_files: [(path, size, mtime)] from the scanned tree (≥1 MB). Confidence: high/medium/low."""
    now, out = time.time(), []
    days = lambda t: (now - t) / 86400

    # 1) dev artifacts in untouched projects
    for it in dev_artifacts(conf["auto"]["dev_names"], 14):
        it["size"] = du(it["path"])
        if it["size"] >= 20 << 20:
            out.append(_sug("dev", "Dev artifact", it["note"], it["path"], it["size"],
                            "high" if it["age"] >= 90 else "medium", age=it["age"]))
    # 2) Downloads: installers and old large files
    for e in os.scandir(H("Downloads")) if os.path.isdir(H("Downloads")) else []:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        size = st.st_blocks * 512 if e.is_file() else 0
        ext = e.name.rsplit(".", 1)[-1].lower()
        if e.is_file() and ext in ("dmg", "pkg", "xip", "mpkg") and days(st.st_mtime) > 3:
            out.append(_sug("downloads", "Installer", f"In Downloads for {int(days(st.st_mtime))} days; not needed if the app is installed", e.path, size, "high"))
        elif e.is_file() and size >= 100 << 20 and days(st.st_atime) > 90:
            out.append(_sug("downloads", "Old large download", f"Not opened for {int(days(st.st_atime))} days", e.path, size, "medium"))
    # 3) duplicate large files (size + first/last 1 MB hash)
    bysize = {}
    for p, s, m in big_files:
        if s >= 20 << 20 and user_content(p):
            bysize.setdefault(s, []).append((p, m))
    groups = [g for g in bysize.values() if len(g) > 1]
    for g in groups[:300]:
        byhash = {}
        for p, m in g:
            if (h := _quick_hash(p)):
                byhash.setdefault(h, []).append((p, m))
        for dup in byhash.values():
            if len(dup) > 1:
                dup.sort(key=lambda x: -x[1])
                keep = dup[0][0]
                for p, m in dup[1:]:
                    out.append(_sug("duplicate", "Duplicate file", f"Same as {keep.replace(HOME, '~')} (newer copy is kept)",
                                    p, os.lstat(p).st_blocks * 512, "medium", keep=keep))
    # 4) old iOS DeviceSupport versions (newest per device is kept)
    by_dev = {}
    for p in glob(H("Library/Developer/Xcode/iOS DeviceSupport/*")):
        by_dev.setdefault(os.path.basename(p).split(" ")[0], []).append(p)
    for dev, ps in by_dev.items():
        ps.sort(key=lambda p: -os.stat(p).st_mtime)
        for p in ps[1:]:
            out.append(_sug("xcode", "Old iOS device symbols", f"A newer version exists for {dev}", p, du(p), "high"))
    # 4b) Xcode DerivedData: per-project build output, rebuilt on next build
    for p in glob(H("Library/Developer/Xcode/DerivedData/*")):
        if not os.path.isdir(p) or (s := du(p)) < 50 << 20:
            continue
        try:
            with open(os.path.join(p, "info.plist"), "rb") as f:
                ws = plistlib.load(f).get("WorkspacePath", "")
        except (OSError, plistlib.InvalidFileException):
            ws = ""
        age = int(days(os.stat(p).st_mtime))
        if ws and not os.path.exists(ws):
            out.append(_sug("xcode", "Xcode build data (project deleted)", f"Project {ws.replace(HOME, '~')} no longer exists", p, s, "high"))
        elif ws:
            out.append(_sug("xcode", "Xcode build data", f"Build output of {os.path.basename(ws)}, untouched {age} days; Xcode rebuilds it",
                            p, s, "high" if age >= 30 else "medium" if age >= 7 else "low", age=age))
        else:  # ModuleCache.noindex, CompilationCache.noindex…: shared caches
            out.append(_sug("xcode", "Xcode shared build cache", "Shared module/compilation cache; Xcode rebuilds it", p, s, "medium"))
    # 5) large app caches (not in the curated list)
    for p in glob(H("Library/Caches/*")):
        if os.path.basename(p) not in APP_CACHES and os.path.isdir(p):
            s = du(p)
            if s >= 1 << 30:
                out.append(_sug("cache", "Large app cache", "Over 1 GB; the app rebuilds it", p, s, "medium"))
    # 6) Trash
    if (s := du(TRASH)) >= 50 << 20:
        out.append(_sug("trash", "Trash is full", "Trashed items still take up space", TRASH, s, "high", contents=True))
    # 7) iPhone backups and Mail attachments (review)
    for p in glob(H("Library/Application Support/MobileSync/Backup/*")):
        out.append(_sug("backup", "iPhone backup", "May be redundant if iCloud Backup is on — review", p, du(p), "low"))
    mail = H("Library/Containers/com.apple.mail/Data/Library/Mail Downloads")
    if os.path.isdir(mail) and (s := du(mail)) >= 50 << 20:
        out.append(_sug("mail", "Mail attachments", "Copies of opened email attachments", mail, s, "medium", contents=True))
    # 8) very large files untouched for a long time
    for p, s, m in big_files:
        if s >= 500 << 20 and days(m) > 365 and user_content(p):
            out.append(_sug("old", "Old large file", f"Unchanged for {int(days(m) / 365 * 10) / 10} years — review", p, s, "low"))
    # 9) info only: cleaned via commands
    docker = H("Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw")
    if os.path.exists(docker) and (s := os.lstat(docker).st_blocks * 512) >= 10 << 30:
        out.append(_sug("info", "Docker disk image", "Use the Docker tab to prune unused images/volumes/cache (Docker must be running)", docker, s, "low", action="info"))
    if os.path.isdir(H(".ollama/models")) and (s := du(H(".ollama/models"))) >= 5 << 30:
        out.append(_sug("info", "Ollama models", "Remove unused models with 'ollama list' + 'ollama rm <model>'", H(".ollama/models"), s, "low", action="info"))
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda x: (rank[x["confidence"]], -x["size"]))
