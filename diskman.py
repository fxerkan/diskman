#!/usr/bin/env python3
"""diskman — disk analysis + cleanup web UI (macOS, Linux, Windows; Python 3.12+, stdlib only).
  diskman [port]                  opens the UI in the browser, runs in the background (port 18791; exits after 30 min unused)
  diskman --serve [--host H] [port]   headless server (no browser, never exits); --host 0.0.0.0 for LAN/mobile
  diskman --set-password          user + password for remote (non-localhost) access; --clear-password removes it
  diskman --install [--lan]       run as a background service (launchd / systemd / Task Scheduler); --uninstall
  diskman --auto-clean [--dry]    auto-clean with the rules from Settings (launchd calls this)"""
import bisect, hashlib, hmac, ipaddress, json, os, pickle, plistlib, queue, secrets, shutil, socket, stat, subprocess, sys, \
    threading, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from glob import glob
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import cleaner as C  # noqa: E402
import assistant as AI  # noqa: E402

WIN, MAC = os.name == "nt", sys.platform == "darwin"
norm = lambda p: os.path.normpath(p).replace(os.sep, "/")  # içeride her yerde "/" (Windows da kabul eder)
HOME = norm(os.path.expanduser("~"))
H = lambda *p: norm(os.path.join(HOME, *p))
TRASH = H(".Trash")
DATA_VOL = "/System/Volumes/Data"
CACHE = H(".cache/diskman")
THUMBS = os.path.join(CACHE, "thumbs")
ALL = "*"  # birleşik görünüm: tüm diskler tek ağaç
VERSION = "2.0.0"
REPO = "https://github.com/fxerkan/diskman"
PORT = 18791  # sabit: tarayıcı tercihleri (görünüm, filtre) kalıcı olsun; 8791 başka araçlarla çakışıyordu
HOSTFS = norm(os.environ.get("DISKMAN_HOSTFS", "")).rstrip("/")  # konteyner: ana makinenin / dizini burada (ör. /host)
SERVE = [False]  # servis/konteyner modu: kendi zamanlayıcısı çalışır, tarayıcı açılmaz
THREADS = int(os.environ.get("DISKMAN_THREADS") or 16)  # disk başına; her disk kendi havuzunda paralel taranır

# ---------- dosya türleri ve yaş dilimleri ----------
TYPES = ["Video", "Image", "Audio", "Archive", "Document", "Code", "App/Package", "Disk image/VM",
         "Database", "AI model", "Other"]
_EXTS = ["mp4 mov mkv avi m4v webm wmv flv mts 3gp",
         "jpg jpeg png gif heic heif webp tiff tif bmp cr2 nef arw dng psd svg ico",
         "mp3 m4a wav flac aac ogg aiff caf opus",
         "zip tar gz tgz bz2 xz 7z rar zst lz4",
         "pdf doc docx xls xlsx ppt pptx key pages numbers txt md csv rtf epub odt",
         "py js mjs cjs ts tsx jsx java c h cpp hpp go rs swift kt rb php json yaml yml xml html css scss sh "
         "class jar pyc o a so dylib map wasm lock",
         "app dmg pkg ipa apk aab xip mpkg",
         "raw img iso vmdk qcow2 vdi sparseimage vhd vhdx asif",
         "db sqlite sqlite3 duckdb parquet ldb realm",
         "gguf safetensors pt pth onnx ckpt h5 tflite mlmodel npz"]
EXT = {e: i for i, s in enumerate(_EXTS) for e in s.split()}
OTHER = len(TYPES) - 1
AGES = ["< 1 week", "< 1 month", "< 6 months", "< 1 year", "< 2 years", "< 5 years", "5+ years"]
_AGE_DAYS = [7, 30, 182, 365, 730, 1826]
BIG = 1 << 20  # bu boyuttan büyük dosyalar ağaçta tek tek tutulur, küçükler klasör başına toplanır


def ext_of(name):
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot > 0 and len(name) - dot <= 12 else ""


def ftype(name):
    return EXT.get(ext_of(name), OTHER)


def usage(st):
    return st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size  # Windows: st_blocks yok


def age_idx(mtime, now):
    d = (now - mtime) / 86400
    for i, lim in enumerate(_AGE_DAYS):
        if d < lim:
            return i
    return len(_AGE_DAYS)


# ---------- tarama: bellekte klasör ağacı ----------
class Node:
    __slots__ = ("name", "parent", "size", "n", "mtime", "kids", "files", "small", "smalln", "hist", "names")

    def __init__(self, name, parent, mtime):
        self.name, self.parent, self.mtime = name, parent, mtime
        self.size = self.n = self.small = self.smalln = 0
        self.kids, self.files, self.hist = [], [], {}  # files: (ad, boyut, mtime); hist: tür*8+yaş -> bayt
        self.names = ""  # küçük dosyaların adları "\n" ile; klasör başına tek dize → hızlı ad araması, az bellek


SKIP = {H("Library/CloudStorage"), H("Dropbox"), DATA_VOL + H("Library/CloudStorage"), DATA_VOL + H("Dropbox"),
        "/System/Volumes/Data/System/Volumes"}  # bulut klasörleri: dataless, taramak yavaş ve anlamsız
ROOTS = {}  # kök → tarama durumu; her kök ayrı thread'de, aynı anda taranabilir
VIEW = [None]  # arayüzün baktığı kök ya da ALL
_idx_lock = threading.Lock()


def new_state(root):  # kind: full | inc | cache; _changed: son kontrolde mtime'ı değişen (node, path) klasörleri
    return {"root": root, "tree": None, "running": False, "done": False, "files": 0, "dirs": 0, "bytes": 0,
            "start": 0, "end": 0, "cur": "", "errors": 0, "charts": None, "kind": "", "full_at": 0,
            "checking": False, "changed": None, "from_cache": False, "_changed": [], "_stale": set()}


def kid_path(p, k):
    return k.name if p == ALL else p.rstrip("/") + "/" + k.name  # ALL'ın çocukları disk kökleri (adları tam yol)


def _scan_one(S, nd, path, dev, now, seen, push, acc):
    try:
        it = os.scandir(path)
    except OSError:
        S["errors"] += 1
        return
    S["cur"] = path
    sub, small = [], []
    exts, sizes, months = acc
    with it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                p = path.rstrip("/") + "/" + e.name
                # Windows: DirEntry st_dev=0; bağlama noktaları/junction'lar reparse point (0x400) → atla
                if (st.st_dev == dev or not st.st_dev) and not getattr(st, "st_file_attributes", 0) & 0x400 \
                        and p not in SKIP:
                    c = Node(e.name, nd, st.st_mtime)
                    nd.kids.append(c)
                    sub.append((c, p))
                continue
            if st.st_nlink > 1:  # hard link'leri bir kez say (pnpm, Xcode)
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            sz = usage(st)
            nd.size += sz
            nd.n += 1
            x = ext_of(e.name)
            k = EXT.get(x, OTHER) * 8 + age_idx(st.st_mtime, now)
            nd.hist[k] = nd.hist.get(k, 0) + sz
            if sz >= BIG:
                nd.files.append((e.name, sz, st.st_mtime))
            else:
                nd.small += sz
                nd.smalln += 1
                small.append(e.name)
            ex = exts.get(x)
            if ex:
                ex[0] += sz
                ex[1] += 1
            else:
                exts[x] = [sz, 1]
            b = min(sz.bit_length(), 44)
            sizes[b][0] += 1
            sizes[b][1] += sz
            m = time.localtime(st.st_mtime)
            mk = m.tm_year * 100 + m.tm_mon
            months[mk] = months.get(mk, 0) + sz
            S["files"] += 1
            S["bytes"] += sz
    nd.names = "\n".join(small)
    S["dirs"] += len(sub)
    push(sub)


def _crawl(S, items, dev, now, seen):
    """items: [(node, path)] alt ağaçlarını paralel tara; thread başı istatistikleri döndür.
    ponytail: thread + GIL; I/O-bound disklerde yeter, CPU-bound olursa disk başına süreç (multiprocessing)."""
    if not items:
        return []
    q, pending, lock, nworkers, accs = queue.Queue(), [len(items)], threading.Lock(), THREADS, []

    def push(sub):
        with lock:
            pending[0] += len(sub)
        for s in sub:
            q.put(s)

    def worker():  # ponytail: düz iş kuyruğu, scandir/stat GIL'i bıraktığı için thread'ler yeterli
        acc = ({}, [[0, 0] for _ in range(45)], {})  # thread başına istatistik, sonda birleşir
        accs.append(acc)
        while (item := q.get()) is not None:
            try:
                _scan_one(S, *item, dev, now, seen, push, acc)
            finally:
                with lock:
                    pending[0] -= 1
                    if pending[0] == 0:
                        for _ in range(nworkers):
                            q.put(None)

    for it in items:
        q.put(it)
    ts = [threading.Thread(target=worker, daemon=True) for _ in range(nworkers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return accs


def _bfs(tree):
    order = [tree]
    for nd in order:
        order.extend(nd.kids)
    return order


def _finish(tree):
    """Klasörlerin kendi boyutlarını alttan yukarı topla, sırala."""
    order = _bfs(tree)
    for nd in reversed(order):
        if nd.parent:
            nd.parent.size += nd.size
            nd.parent.n += nd.n
    for nd in order:
        nd.kids.sort(key=lambda c: -c.size)
        nd.files.sort(key=lambda f: -f[1])
    return order


def _charts(accs):
    exts, sizes, months = {}, [[0, 0] for _ in range(45)], {}
    for e, s, m in accs:
        for k, v in e.items():
            t = exts.setdefault(k, [0, 0])
            t[0] += v[0]
            t[1] += v[1]
        for i, v in enumerate(s):
            sizes[i][0] += v[0]
            sizes[i][1] += v[1]
        for k, v in m.items():
            months[k] = months.get(k, 0) + v
    top = sorted(exts.items(), key=lambda kv: -kv[1][0])[:40]
    return {"ext": [{"ext": k or "(no extension)", "size": v[0], "n": v[1], "type": EXT.get(k, OTHER)} for k, v in top],
            "sizes": [{"lo": (1 << (i - 1)) if i else 0, "hi": 1 << i, "n": v[0], "size": v[1]}
                      for i, v in enumerate(sizes) if v[0]],
            "months": [{"m": f"{k // 100}-{k % 100:02d}", "size": v} for k, v in sorted(months.items())
                       if 1990 <= k // 100 <= time.localtime().tm_year]}


def scan(S):
    root = S["root"]
    st = os.stat(root)
    tree = Node(root, None, st.st_mtime)
    S.update(tree=None, running=True, done=False, files=0, dirs=0, bytes=0, errors=0,
             start=time.time(), cur=root, charts=None, kind="full", changed=None, from_cache=False)
    S["_changed"].clear()
    S["_stale"] = set()
    accs = _crawl(S, [(tree, root)], st.st_dev, time.time(), set())
    _finish(tree)
    now = time.time()
    S.update(tree=tree, running=False, done=True, end=now, full_at=now, cur="", charts=_charts(accs), changed=0)
    _sug_cache.clear()
    save_cache(S)


# ---------- önbellek + artımlı tarama ----------
def _cache_path(root):
    return os.path.join(CACHE, "scan-" + hashlib.sha1(root.encode()).hexdigest()[:12] + ".pkl")


_META = ("root", "files", "dirs", "bytes", "start", "end", "errors", "charts", "kind", "full_at")
_INDEX = os.path.join(CACHE, "scans.json")  # kök → özet; seçim listesi için (pickle'ları açmadan)


def cached_roots():
    try:
        with open(_INDEX) as f:
            idx = json.load(f)
    except (OSError, ValueError):
        return []
    return sorted(({"path": k, **v} for k, v in idx.items() if os.path.exists(_cache_path(k))), key=lambda r: -r["end"])


def save_cache(S):
    """Ağacı düz satırlara çevirip (derin özyineleme yok) diske yaz."""
    tree, rows, idx = S["tree"], [], {}
    for nd in _bfs(tree):
        idx[id(nd)] = len(rows)
        rows.append((idx[id(nd.parent)] if nd.parent else -1, nd.name, nd.mtime, nd.size, nd.n, nd.files,
                     nd.small, nd.smalln, nd.hist, nd.names))
    os.makedirs(CACHE, exist_ok=True)
    path = _cache_path(S["root"])
    with open(path + ".tmp", "wb") as f:
        pickle.dump({"v": 2, "meta": {k: S[k] for k in _META}, "rows": rows}, f, pickle.HIGHEST_PROTOCOL)
    os.replace(path + ".tmp", path)
    _snapshot(S)
    with _idx_lock:  # paralel taramalar aynı index'i günceller
        idx = {r.pop("path"): r for r in cached_roots()}
        idx[S["root"]] = {"end": S["end"], "bytes": S["bytes"], "files": S["files"]}
        with open(_INDEX + ".tmp", "w") as f:
            json.dump(idx, f)
        os.replace(_INDEX + ".tmp", _INDEX)


def load_cache(S):
    root = S["root"]
    S.update(tree=None, running=True, done=False, start=time.time(), cur="loading cache…", kind="cache", changed=None)
    S["_changed"].clear()
    S["_stale"] = set()
    with open(_cache_path(root), "rb") as f:
        d = pickle.load(f)  # ponytail: kendi ürettiğimiz yerel dosya; ~/.cache'e yazabilen zaten her şeyi yapabilir
    if d.get("v") != 2:  # ad dizini yok → tam tarama
        raise ValueError("old cache")
    nodes = []
    for pi, name, mt, size, n, files, small, smalln, hist, names in d["rows"]:
        nd = Node(name, nodes[pi] if pi >= 0 else None, mt)
        nd.size, nd.n, nd.files, nd.small, nd.smalln, nd.hist, nd.names = size, n, files, small, smalln, hist, names
        if pi >= 0:
            nodes[pi].kids.append(nd)
        nodes.append(nd)
    S.update(**d["meta"], tree=nodes[0], running=False, done=True, cur="", checking=True, from_cache=True)
    _sug_cache.clear()


def _snap_path(root):
    return _cache_path(root)[:-4] + ".snaps.json"


def _load_snaps(root):
    try:
        with open(_snap_path(root)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _snapshot(S):
    """Büyüme görünümü için: klasör boyutları (4 seviye, ≥10 MB), günde bir anlık görüntü, son 60 gün."""
    sizes, stack = {}, [(S["tree"], S["root"], 0)]
    while stack:
        nd, p, d = stack.pop()
        sizes[p] = nd.size
        if d < 4:
            stack.extend((k, kid_path(p, k), d + 1) for k in nd.kids if k.size >= 10 << 20)
    snaps = _load_snaps(S["root"])
    if snaps and time.strftime("%F", time.localtime(snaps[-1]["t"])) == time.strftime("%F", time.localtime(S["end"])):
        snaps.pop()
    snaps.append({"t": S["end"], "sizes": sizes})
    del snaps[:-60]
    with open(_snap_path(S["root"]) + ".tmp", "w") as f:
        json.dump(snaps, f)
    os.replace(_snap_path(S["root"]) + ".tmp", _snap_path(S["root"]))


def _mt(p):
    try:
        return os.lstat(p).st_mtime
    except OSError:
        return None


def check_changes(S):
    """Klasör mtime'ı (ekleme/silme/yeniden adlandırma) ya da ≥1 MB dosyanın mtime'ı değiştiyse klasör değişmiştir.
    ponytail: küçük dosyaların yerinde büyümesi yakalanmaz (klasör mtime'ı değişmez); gerekirse FSEvents."""
    S.update(checking=True, changed=None)
    out = []

    def changed(item):
        nd, p = item
        mt = _mt(p)  # None: silinmiş, üst klasörün mtime'ı zaten değişti
        return mt is not None and (mt != nd.mtime or any(_mt(p.rstrip("/") + "/" + f[0]) != f[2] for f in nd.files))
    try:
        items = list(walk_tree(S["tree"], S["root"]))
        with ThreadPoolExecutor(THREADS) as ex:  # lstat GIL'i bırakır
            out = [it for it, c in zip(items, ex.map(changed, items, chunksize=4096)) if c]
    finally:
        S["_changed"][:] = out
        st = set()
        for nd, _ in out:  # değişen klasör + üstleri: arayüz bunları "güncel değil" diye işaretler
            while nd is not None and id(nd) not in st:
                st.add(id(nd))
                nd = nd.parent
        S["_stale"] = st
        S.update(checking=False, changed=len(out))


def rescan(S):
    """Yalnızca değişen klasörlerin doğrudan içeriğini yeniden oku; yeni alt klasörleri tümüyle tara.
    ponytail: grafik sekmesi (uzantı/boyut/ay) son tam taramadan kalır; hard link tekilleştirmesi yalnız bu turda."""
    tree, root, changed = S["tree"], S["root"], list(S["_changed"])
    S.update(running=True, kind="inc", start=time.time(), files=0, bytes=0, errors=0, cur=root, changed=None, from_cache=False)
    dev, now, seen, acc, fresh = os.stat(root).st_dev, time.time(), set(), ({}, [[0, 0] for _ in range(45)], {}), []
    try:
        for nd, _ in walk_tree(tree, root):  # toplamları "kendi boyutu"na çevir (üst, altlarından önce gelir)
            nd.size -= sum(k.size for k in nd.kids)
            nd.n -= sum(k.n for k in nd.kids)
        for nd, p in changed:
            mt = _mt(p)
            if mt is None:
                continue
            tmp, sub = Node(nd.name, nd.parent, mt), []
            _scan_one(S, tmp, p, dev, now, seen, sub.extend, acc)
            old, nd.kids = {k.name: k for k in nd.kids}, []
            for c, cp in sub:
                o = old.get(c.name)
                if o is None:
                    c.parent = nd
                    fresh.append((c, cp))
                nd.kids.append(o or c)
            nd.mtime, nd.size, nd.n, nd.files, nd.small, nd.smalln, nd.hist, nd.names = (
                mt, tmp.size, tmp.n, tmp.files, tmp.small, tmp.smalln, tmp.hist, tmp.names)
        _crawl(S, fresh, dev, now, seen)
    finally:
        order = _finish(tree)
        S["_changed"].clear()
        S["_stale"] = set()
        S.update(running=False, end=time.time(), cur="", files=tree.n, bytes=tree.size, dirs=len(order) - 1, changed=0)
        _sug_cache.clear()
    save_cache(S)


def _open(S, mode):
    try:
        if mode == "inc":
            rescan(S)
        elif mode == "auto" and S["tree"]:
            check_changes(S)
        elif mode == "auto" and os.path.exists(_cache_path(S["root"])):
            try:
                load_cache(S)
            except Exception:  # bozuk/eski önbellek → tam tara
                return scan(S)
            check_changes(S)
        else:
            scan(S)
    except Exception:
        S.update(running=False, checking=False)
        raise


def _open_warm(S, mode):
    _open(S, mode)
    if S["tree"]:
        _sindex(S)  # ilk arama beklemesin


def start_scan(root, mode="auto"):
    """mode: auto = önbellek varsa yükle + değişiklikleri kontrol et, yoksa tam tara; full; inc = değişenleri tara.
    root = ALL → tüm diskler aynı anda (her biri kendi thread havuzunda)."""
    roots = disk_roots() if root == ALL else [norm(os.path.abspath(root))]
    for r in roots:
        if not os.path.isdir(r):
            raise ValueError("folder not found")
    VIEW[0] = ALL if root == ALL else roots[0]
    for r in roots:
        S = ROOTS.setdefault(r, new_state(r))
        if S["running"] or S["checking"] or (mode == "inc" and not S["_changed"]):
            continue  # zaten taranıyor / değişiklik yok → sadece görünüme geç
        S["running" if mode != "auto" or not S["tree"] else "checking"] = True  # yarışı önle: hemen meşgul
        threading.Thread(target=_open_warm, args=(S, mode), daemon=True).start()


def refresh(path):
    """Seçili klasörü (alt ağacı + üst klasörünün doğrudan içeriği) yeniden tara: Finder'da silinen/eklenen yansısın.
    Bekleyen değişiklik kontrolü bitince onun bulduklarıyla birlikte tek artımlı taramada işlenir."""
    path = norm(path)
    S = owner(path)
    if not S:
        raise ValueError("folder is not in the scanned tree")
    if S["running"]:
        raise ValueError("a scan is already running")
    nd = find_dir(path)
    S["running"] = True  # hemen meşgul: ikinci tık/yarış olmasın

    def run():
        try:
            while S["checking"]:
                time.sleep(0.2)
            items = list(S["_changed"]) + list(walk_tree(nd, path))
            if nd.parent:
                items.append((nd.parent, path.rsplit("/", 1)[0] or "/"))
            seen = set()
            S["_changed"][:] = [it for it in items if id(it[0]) not in seen and not seen.add(id(it[0]))]
            rescan(S)
        finally:
            S["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return {"ok": 1}


def home_dirs():
    try:
        return sorted(norm(e.path) for e in os.scandir(HOME) if e.is_dir(follow_symlinks=False) and not e.name.startswith("."))
    except OSError:
        return []


def inside(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")


def owner(path):
    """Yolu içeren en derin yüklü kökün durumu (iç içe kökler: / ve /media/x)."""
    best = None
    for S in list(ROOTS.values()):
        if S["tree"] and inside(path, S["root"]) and (not best or len(S["root"]) > len(best["root"])):
            best = S
    return best


def virtual():
    """ALL görünümü: yüklü disk kökleri tek ağacın çocukları (ebeveynleri değişmez, toplam anlık)."""
    v = Node("All disks", None, 0)
    v.kids = sorted((S["tree"] for r in disk_roots() if (S := ROOTS.get(r)) and S["tree"]), key=lambda t: -t.size)
    v.size, v.n = sum(t.size for t in v.kids), sum(t.n for t in v.kids)
    return v


def find(path):
    """Ağaçta yolu bul → (node, None) klasör, (parent, dosya) büyük dosya, None yok."""
    path = norm(path) if path != ALL else ALL
    if path == ALL:
        v = virtual()
        if not v.kids:
            raise ValueError("no scan yet")
        return v, None
    S = owner(path)
    if not S:
        if not any(s["tree"] for s in ROOTS.values()):
            raise ValueError("no scan yet")
        return None
    tree, root = S["tree"], S["root"]
    if path == root:
        return tree, None
    if not path.startswith(root.rstrip("/") + "/"):
        return None
    nd = tree
    parts = path[len(root.rstrip("/")) + 1:].split("/")
    for i, part in enumerate(parts):
        nxt = next((k for k in nd.kids if k.name == part), None)
        if nxt is None:
            if i == len(parts) - 1:
                f = next((f for f in nd.files if f[0] == part), None)
                return (nd, f) if f else None
            return None
        nd = nxt
    return nd, None


def find_dir(path):
    r = find(path)
    if not r or r[1]:
        raise ValueError("folder is not in the scanned tree")
    return r[0]


def stale_ids():
    return set().union(*(S.get("_stale", ()) for S in list(ROOTS.values())))


def jnode(nd, path, depth, top, st=frozenset()):
    d = {"name": nd.name if nd.parent or path == ALL else path, "path": path, "size": nd.size, "n": nd.n, "kind": "dir",
         "mtime": nd.mtime, "nk": len(nd.kids), **({"stale": 1} if id(nd) in st else {})}
    if depth <= 0:
        return d
    items = [(k.size, 0, k) for k in nd.kids] + [(f[1], 1, f) for f in nd.files]
    items.sort(key=lambda x: -x[0])
    kids = []
    for size, isfile, x in items[:top]:
        p = path.rstrip("/") + "/" + x[0] if isfile else kid_path(path, x)
        kids.append({"name": x[0], "path": p, "size": size, "kind": "file", "mtime": x[2], "type": ftype(x[0])}
                    if isfile else jnode(x, p, depth - 1, top, st))
    rest = items[top:]
    if rest:
        kids.append({"name": f"{len(rest)} other items", "size": sum(r[0] for r in rest), "kind": "other",
                     "path": path})
    if nd.small:
        kids.append({"name": f"{nd.smalln} small files", "size": nd.small, "kind": "small", "path": path})
    d["children"] = kids
    return d


def tree_json(path, depth, top):
    return jnode(find_dir(path), path if path == ALL else norm(path), min(depth, 4), min(top, 5000), stale_ids())


def walk_tree(nd, path):
    """(node, path) üreteci — alt ağaçtaki tüm klasörler."""
    stack = [(nd, path)]
    while stack:
        n, p = stack.pop()
        yield n, p
        stack.extend((k, kid_path(p, k)) for k in n.kids)


def stats(path):
    heat = [[0] * len(AGES) for _ in TYPES]
    for nd, _ in walk_tree(find_dir(path), path):
        for k, v in nd.hist.items():
            heat[k >> 3][k & 7] += v
    return {"types": TYPES, "ages": AGES, "heat": heat}


def cell(path, t, a):
    """Isı haritası hücresi: bu tür + yaştaki ≥1 MB dosyalar, küçükler toplam olarak."""
    path, now, files, total = (path if path == ALL else norm(path)), time.time(), [], 0
    for nd, p in walk_tree(find_dir(path), path):
        total += nd.hist.get(t * 8 + a, 0)
        for name, size, m in nd.files:
            if ftype(name) == t and age_idx(m, now) == a:
                files.append({"path": p.rstrip("/") + "/" + name, "size": size, "mtime": m, "type": t})
    files.sort(key=lambda f: -f["size"])
    listed = sum(f["size"] for f in files)
    return {"type": TYPES[t], "age": AGES[a], "total": total, "files": files[:500], "count": len(files),
            "small": max(0, total - listed)}


def all_big(min_bytes=BIG, path=None):
    path = path or VIEW[0]
    path = path if path in (None, ALL) else norm(path)
    try:
        r = find(path) if path else None
    except ValueError:
        return []
    if not r or r[1]:
        return []
    return [(p.rstrip("/") + "/" + f[0], f[1], f[2]) for nd, p in walk_tree(r[0], path)
            for f in nd.files if f[1] >= min_bytes]


def big_files(min_mb, older_days=0, path=None):
    if not any(S["tree"] for S in ROOTS.values()):
        raise ValueError("no scan yet")
    cut = time.time() - float(older_days) * 86400
    res = [{"path": p, "size": s, "mtime": m, "type": ftype(p)} for p, s, m in all_big(int(float(min_mb) * (1 << 20)), path)
           if not older_days or m < cut]
    return sorted(res, key=lambda i: -i["size"])[:2000]


_SIDX, _TREEV = {}, [0]  # kök → arama dizini; _TREEV: silmede ağaç değişince artar


def _low(s):  # "İ".lower() = "i" + birleşen nokta → noktayı at: "istanbul" "İstanbul"u bulsun
    return s.lower().replace("\u0307", "")


def path_of(nd):
    parts = []
    while nd.parent:
        parts.append(nd.name)
        nd = nd.parent
    return nd.name.rstrip("/") + "/" + "/".join(reversed(parts)) if parts else nd.name


def _sindex(S):
    """Kökün tüm adları tek küçük harfli metinde (düğüm başına bir parça): str.find C hızında tarar.
    Parçalar ayrı ayrı küçültülür; 'İ' gibi harflerde uzunluk değişse de konumlar doğru kalır."""
    key = (id(S["tree"]), S["end"], _TREEV[0])
    ix = _SIDX.get(S["root"])
    if ix and ix[0] == key:
        return ix
    parts, starts, nodes, pos = [], [], [], 0
    for nd in _bfs(S["tree"]):
        b = _low("\n".join([nd.name, *(f[0] for f in nd.files), *([nd.names] if nd.names else [])]))
        parts.append(b)
        starts.append(pos)
        nodes.append(nd)
        pos += len(b) + 1
    ix = _SIDX[S["root"]] = (key, "\0".join(parts), starts, nodes)
    return ix


def search(q, path, limit=300):
    """Hızlı ad araması: taranan ağaçtaki tüm klasörler ve dosyalar (küçükler dahil).
    Boşlukla ayrılan terimlerin hepsi adda geçmeli (büyük/küçük harf duyarsız)."""
    terms = _low(q).split()
    if not terms:
        return []
    path = path if path == ALL else norm(path)
    states = [S for S in _view_states() if S["tree"]] if path == ALL else [owner(path)]
    hit, res, t0 = (lambda s: all(t in _low(s) for t in terms)), [], max(terms, key=len)
    for S in filter(None, states):
        _, blob, starts, nodes = _sindex(S)
        i = blob.find(t0)
        while i != -1 and len(res) < limit * 5:  # ponytail: ilk 1500 eşleşmeden en büyükleri; terim eklemek daraltır
            k = bisect.bisect_right(starts, i) - 1
            nd = nodes[k]
            p = path_of(nd)
            if path == ALL or inside(p, path):
                if nd.parent and hit(nd.name) and p != path:
                    res.append({"path": p, "name": nd.name, "kind": "dir", "size": nd.size, "n": nd.n, "mtime": nd.mtime})
                for name, size, m in nd.files:
                    if hit(name):
                        res.append({"path": p.rstrip("/") + "/" + name, "name": name, "kind": "file", "size": size,
                                    "mtime": m, "type": ftype(name)})
                for name in nd.names.split("\n") if nd.names else ():
                    if hit(name):
                        fp = p.rstrip("/") + "/" + name
                        try:
                            st = os.lstat(fp)
                        except OSError:
                            continue
                        res.append({"path": fp, "name": name, "kind": "file", "size": usage(st), "mtime": st.st_mtime,
                                    "type": ftype(name)})
            i = blob.find(t0, starts[k + 1]) if k + 1 < len(starts) else -1
    return sorted(res, key=lambda x: -x["size"])[:limit]


# ---------- araçlar: boş klasörler, kopyalar, büyüme, ilişki grafiği ----------
def empty_dirs(path, limit=3000):
    """İçinde hiç dosya olmayan klasörler (yalnız boş alt klasörler olabilir); en üstteki raporlanır."""
    path, out = (path if path == ALL else norm(path)), []
    stack = [(find_dir(path), path)]
    while stack and len(out) < limit:
        nd, p = stack.pop()
        if nd.n == 0 and nd.parent is not None and p != path:  # disk kökü ve bakılan klasörün kendisi hariç
            out.append({"path": p, "name": nd.name, "kind": "dir", "size": 0, "n": 0, "mtime": nd.mtime,
                        "sub": sum(1 for _ in walk_tree(nd, p)) - 1})
            continue
        stack.extend((k, kid_path(p, k)) for k in nd.kids)
    return sorted(out, key=lambda r: r["path"])


DUP = {"running": False, "phase": "", "done": 0, "total": 0, "bytes": 0, "tbytes": 0, "groups": None, "at": 0, "min": 0,
       "root": None}


def _hash(p, full):
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(p, "rb") as f:
            if full:
                while b := f.read(1 << 20):
                    h.update(b)
                    DUP["bytes"] += len(b)
            else:  # baş/orta/son 64 KB: farklı dosyaların çoğunu tam okumadan eler
                n = os.fstat(f.fileno()).st_size
                for off in (0, n // 2, max(0, n - 65536)):
                    f.seek(off)
                    h.update(f.read(65536))
    except OSError:
        return None
    finally:
        DUP["done"] += 1
    return h.hexdigest()


def dup_scan(min_bytes, path=None):
    """Aynı boyut → örnek özet → tam özet (BLAKE2): yalnız içeriği birebir aynı dosyalar kopya sayılır.
    ponytail: ağaçtaki ≥1 MB dosyalar; küçükler ağaçta tek tek tutulmuyor."""
    DUP.update(running=True, groups=None, min=min_bytes, root=path or VIEW[0], at=0)
    try:
        by = {}
        for p, s, m in all_big(max(int(min_bytes), BIG), path):
            by.setdefault(s, []).append((s, p, m))
        cands, g = [c for v in by.values() if len(v) > 1 for c in v], {}
        for full in (False, True):
            DUP.update(phase="full" if full else "sample", done=0, total=len(cands), bytes=0,
                       tbytes=sum(c[0] for c in cands) if full else 0)
            with ThreadPoolExecutor(4) as ex:
                hs = list(ex.map(lambda c: _hash(c[1], full), cands))
            g = {}
            for c, h in zip(cands, hs):
                if h:
                    g.setdefault((c[0], h), []).append(c)
            cands = [c for v in g.values() if len(v) > 1 for c in v]
        groups = [{"size": k[0], "files": [{"path": p, "mtime": m, "type": ftype(p)} for _, p, m in sorted(v, key=lambda c: -c[2])]}
                  for k, v in g.items() if len(v) > 1]
        DUP["groups"] = sorted(groups, key=lambda x: -x["size"] * (len(x["files"]) - 1))
    finally:
        DUP.update(running=False, at=time.time())


def dupes_start(min_mb, path=None):
    if DUP["running"]:
        return DUP
    if not any(S["tree"] for S in ROOTS.values()):
        raise ValueError("no scan yet")
    DUP["running"] = True
    threading.Thread(target=dup_scan, args=(float(min_mb) * (1 << 20), path), daemon=True).start()
    return {k: v for k, v in DUP.items() if k != "groups"}


def growth(path, since=None):
    """Anlık görüntüye göre klasör boyutu değişimi (TreeSize'ın 'snapshot karşılaştırma'sı gibi)."""
    path = path if path == ALL else norm(path)
    states = [S for S in _view_states() if S["tree"]] if path == ALL else [owner(path)]
    states = [S for S in states if S]
    snaps = {S["root"]: _load_snaps(S["root"]) for S in states}
    times = sorted({round(x["t"]) for v in snaps.values() for x in v})
    since = float(since) if since else (times[-2] if len(times) > 1 else times[0] if times else 0)
    rows, total = [], [0, 0]
    for S in states:
        old = next((x["sizes"] for x in reversed(snaps[S["root"]]) if x["t"] <= since + 43200), None)
        if old is None:
            continue
        now, stack = {}, [(S["tree"], S["root"], 0)]
        while stack:
            nd, p, d = stack.pop()
            now[p] = nd.size
            if d < 4:
                stack.extend((k, kid_path(p, k), d + 1) for k in nd.kids if k.size >= 10 << 20 or kid_path(p, k) in old)
        for p in set(old) | set(now):
            if not (path == ALL or inside(p, path)):
                continue
            a, b = old.get(p, 0), now.get(p, 0)
            if p == (S["root"] if path == ALL else path):
                total[0] += a
                total[1] += b
            elif abs(b - a) >= 1 << 20:
                rows.append({"path": p, "name": p.rsplit("/", 1)[-1], "old": a, "new": b, "delta": b - a})
    rows.sort(key=lambda r: -abs(r["delta"]))
    return {"snaps": times, "since": since, "old": total[0], "new": total[1], "rows": rows[:300]}


_dk = {"t": 0, "v": []}


def docker_mounts():
    """Konteyner → bağlı ana makine klasörleri (docker inspect), 60 sn önbellek."""
    if time.time() - _dk["t"] < 60:
        return _dk["v"]
    v, d = [], docker_bin()
    if d:
        try:
            ids = subprocess.run([d, "ps", "-aq"], capture_output=True, text=True, timeout=15).stdout.split()
            js = json.loads(subprocess.run([d, "inspect", *ids], capture_output=True, text=True, timeout=30).stdout
                            or "[]") if ids else []
            v = [{"name": c["Name"].lstrip("/"), "image": c["Config"]["Image"], "running": c["State"]["Running"],
                  "mounts": [HOSTFS + norm(m["Source"]) for m in c.get("Mounts", []) if m.get("Source")]} for c in js]
        except Exception:
            pass
    _dk.update(t=time.time(), v=v)
    return v


def node_at(p):
    """(klasör düğümü, ağaçtaki yol) ya da None. macOS: /Users/... Data biriminin altında da aranır."""
    for c in (p, DATA_VOL + p) if MAC else (p,):
        try:
            r = find(c)
        except ValueError:
            return None
        if r and not r[1]:
            return r[0], norm(c)
    return None


def _app_key(name):
    n = name[:-4] if name.endswith(".app") else name
    if n.count(".") >= 2:  # paket kimliği: com.google.Chrome → chrome
        n = n.rsplit(".", 1)[-1]
    return "".join(ch for ch in n.lower() if ch.isalnum())


def app_dirs():
    """Uygulama → veri/önbellek klasörleri; ad eşleştirmesi sezgisel (ponytail: paket veritabanı okumuyor)."""
    if MAC:
        bases = [H("Library/Application Support"), H("Library/Caches"), H("Library/Containers"),
                 H("Library/Group Containers"), "/Applications"]
    elif WIN:
        bases = [H("AppData/Local"), H("AppData/Roaming"), "C:/Program Files", "C:/Program Files (x86)"]
    else:
        bases = [H(".cache"), H(".config"), H(".local/share"), HOSTFS + "/var/lib", HOSTFS + "/opt"]  # CasaOS AppData: konteyner bağlamaları zaten gösteriyor
    apps = {}
    for b in bases:
        r = node_at(b)
        if not r:
            continue
        for k in r[0].kids:
            if k.size < 20 << 20:
                continue
            key, label = _app_key(k.name), k.name[:-4] if k.name.endswith(".app") else k.name
            if MAC and k.name.endswith(".app"):
                try:
                    with open(f"/Applications/{k.name}/Contents/Info.plist", "rb") as f:
                        key = _app_key(plistlib.load(f).get("CFBundleIdentifier", k.name))
                except Exception:
                    pass
            if not key:
                continue
            a = apps.setdefault(key, {"label": label, "size": 0, "dirs": []})
            if label.count(".") < 2 and (a["label"].count(".") >= 2 or k.name.endswith(".app")):
                a["label"] = label  # okunur ad (paket kimliği değil)
            a["size"] += k.size
            a["dirs"].append((kid_path(r[1], k), k.size))
    return sorted(apps.values(), key=lambda a: -a["size"])[:18]


def graph(path):
    """İlişki grafiği: klasörler, baskın dosya türleri, Docker konteynerlerinin bağladığı klasörler,
    uygulama veri klasörleri ve kopya dosyalar."""
    path = path if path == ALL else norm(path)
    top, N, L = find_dir(path), {}, []

    def node(i, **kw):
        if i not in N:
            N[i] = {"id": i, **kw}
        return i

    def link(a, b, kind, v=0):
        L.append({"source": a, "target": b, "kind": kind, "value": v})

    def anc(p):  # grafikteki en yakın üst klasör
        q = p
        while True:
            q2 = q.rsplit("/", 1)[0] or "/"
            if q2 == q:
                return path
            q = q2
            if q in N:
                return q
            if path != ALL and not inside(q, path):
                return path

    def attach(p, size):  # klasörü grafiğe ekle, en yakın üst klasöre bağla
        if p not in N:
            node(p, label="/".join(p.rsplit("/", 2)[-2:]), kind="dir", size=size, path=p)  # "plex/config": ayırt edici
            link(anc(p), p, "contains", size)
        return p

    node(path, label="All disks" if path == ALL else path.rsplit("/", 1)[-1] or path, kind="root", size=top.size, path=path)
    for k in top.kids[:14]:
        kp = node(kid_path(path, k), label=k.name, kind="dir", size=k.size, path=kid_path(path, k))
        link(path, kp, "contains", k.size)
        by = {}
        for nd, _ in walk_tree(k, kp):
            for h, v in nd.hist.items():
                by[h >> 3] = by.get(h >> 3, 0) + v
        for t, v in by.items():
            if v >= 0.15 * max(k.size, 1) and v >= 50 << 20 and t != OTHER:
                tid = node(f"type:{t}", label=TYPES[t], kind="type", type=t, size=0)
                N[tid]["size"] += v
                link(kp, tid, "type", v)
        for g in k.kids[:4]:
            if g.size >= top.size * 0.01:
                gp = node(kid_path(kp, g), label=g.name, kind="dir", size=g.size, path=kid_path(kp, g))
                link(kp, gp, "contains", g.size)
    for c in docker_mounts()[:60]:
        ms = {}  # aynı klasör iki kez bağlanabilir
        for m in c["mounts"]:
            r = node_at(m) if path == ALL or inside(m, path) else None
            if r:
                ms[r[1]] = r[0].size
        if not ms:
            continue
        ms = sorted(ms.items(), key=lambda x: -x[1])
        cid = node("ctr:" + c["name"], label=c["name"], kind="container", size=sum(x[1] for x in ms),
                   image=c["image"], running=c["running"])
        for mp, size in ms[:6]:
            link(cid, attach(mp, size), "uses", size)
    for a in app_dirs():
        ds = [(p, s) for p, s in a["dirs"] if path == ALL or inside(p, path)]
        if not ds:
            continue
        aid = node("app:" + a["label"], label=a["label"], kind="app", size=sum(s for _, s in ds))
        for p, s in ds[:6]:
            link(aid, attach(p, s), "data", s)
    for gi, g in enumerate((DUP["groups"] or [])[:12]):
        fs = [f for f in g["files"] if path == ALL or inside(f["path"], path)]
        if len(fs) < 2:
            continue
        ids = [node("file:" + f["path"], label=f["path"].rsplit("/", 1)[-1], kind="file", size=g["size"], path=f["path"],
                    type=ftype(f["path"])) for f in fs[:5]]
        for i in ids:
            link(anc(N[i]["path"]), i, "contains", g["size"])
        for i in ids[1:]:
            link(ids[0], i, "dup", g["size"])
    return {"nodes": list(N.values()), "links": L}


def ls(path):
    """Gezgin: klasör boyutları ağaçtan, dosyalar canlı (küçükler dahil)."""
    if path == ALL:  # diskler
        v = virtual()
        return {"scanned": True, "rows": [{"path": k.name, "name": k.name, "kind": "dir", "mtime": k.mtime,
                                           "size": k.size, "n": k.n, "type": None} for k in v.kids]}
    path = norm(path)
    try:
        r = find(path)
    except ValueError:
        r = None
    kids = {k.name: k for k in r[0].kids} if r and not r[1] else {}
    rows, st = [], stale_ids()
    for e in os.scandir(path):
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        d = e.is_dir(follow_symlinks=False)
        k = kids.get(e.name)
        rows.append({"path": norm(e.path), "name": e.name, "kind": "dir" if d else "file", "mtime": st.st_mtime,
                     "size": (k.size if k else -1) if d else usage(st), "n": k.n if k else None,
                     "type": None if d else ftype(e.name), **({"stale": 1} if k and id(k) in st else {})})
    return {"rows": sorted(rows, key=lambda x: -x["size"]), "scanned": bool(r)}


def _mounts():
    """(bağlama noktası, ad, taranacak kök) — yalnız gerçek diskler."""
    if MAC:  # "/" salt-okunur sistem birimi; veriler Data biriminde
        return [("/", "Macintosh HD", DATA_VOL)] + [(p, os.path.basename(p), p) for p in sorted(glob("/Volumes/*"))
                                                    if not os.path.islink(p)]
    if WIN:
        import ctypes, string
        drives = [f"{c}:\\" for c in string.ascii_uppercase]
        return [(norm(d), d[:2], norm(d)) for d in drives
                if ctypes.windll.kernel32.GetDriveTypeW(d) in (2, 3)]  # 2 çıkarılabilir, 3 sabit disk (ağ/CD değil)
    out, devs = [], set()
    try:
        with open(HOSTFS + "/proc/1/mounts" if HOSTFS else "/proc/mounts") as f:  # konteyner: ana makinenin bağlamaları
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        dev, mnt, fs = line.split()[:3]
        mnt = mnt.replace("\\040", " ")
        if not dev.startswith("/dev/") or dev.startswith("/dev/loop") or fs == "squashfs" \
                or mnt.startswith(("/boot", "/snap")) or dev in devs:
            continue
        devs.add(dev)  # bind mount / btrfs alt birimleri bir kez
        hp = HOSTFS + mnt if mnt != "/" else HOSTFS or "/"
        out.append((hp, "System disk" if mnt == "/" else os.path.basename(mnt), hp))
    return out or [(HOSTFS or "/", "System disk", HOSTFS or "/")]


def volumes():
    out = []
    for p, name, root in _mounts():
        try:
            t, u, f = shutil.disk_usage(p)
        except OSError:
            continue
        out.append({"path": p, "name": name, "scan": root, "total": t, "used": u, "free": f, "system": p == SYS_ROOT})
    return out


SYS_ROOT = norm(os.environ.get("SystemDrive", "C:") + "/") if WIN else HOSTFS or "/"


def disk_roots():
    return [v["scan"] for v in volumes()]


# ---------- küçük resimler (Quick Look) ----------
_thumb_sem = threading.Semaphore(4)


RAW_IMG = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}  # tarayıcı doğrudan gösterir (svg değil: betik çalıştırabilir)


def thumb(path, size):
    size = max(64, min(int(size), 1600))
    st = os.stat(path)
    if not MAC:  # ponytail: Quick Look yok → resimleri olduğu gibi gönder; video önizlemesi yok (ffmpeg ile eklenebilir)
        if ext_of(path) not in RAW_IMG or st.st_size > 40 << 20:
            raise ValueError("no preview")
        with open(path, "rb") as f:
            return f.read()
    key = hashlib.sha1(f"{path}|{st.st_mtime}|{size}".encode()).hexdigest()
    out = os.path.join(THUMBS, key + ".png")
    if not os.path.exists(out):
        os.makedirs(THUMBS, exist_ok=True)
        tmp = os.path.join(THUMBS, key)
        with _thumb_sem:
            os.makedirs(tmp, exist_ok=True)
            subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", tmp, path], capture_output=True, timeout=30)
            made = glob(os.path.join(tmp, "*.png"))
            if not made:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError("no preview")
            os.replace(made[0], out)
            shutil.rmtree(tmp, ignore_errors=True)
    with open(out, "rb") as f:
        return f.read()


# ---------- silme ----------
EMPTY_ONLY = {TRASH, H("Library/Logs"), H("Library/Caches")}  # klasörü değil içini sil
PROTECTED = {HOME, H("Library"), H("Library/Application Support"), H("Documents"), H("Desktop"),
             H("Downloads"), H("Library/CloudStorage"), H("Pictures"), H("Movies"), H("Music")}


def check(p):
    """Silinebilir mi? Ev klasörü ya da sistem dışı bir disk içinde (diskin kökü değil), korunan klasör değil.
    Symlink'in kendisi değerlendirilir. Sistem diskinde ev dışı (/usr, /var, C:/Windows) silinmez."""
    p = norm(p)
    if p.startswith(DATA_VOL + "/"):
        p = p[len(DATA_VOL):]
    if not os.path.isabs(p) or not os.path.lexists(p):
        raise ValueError("not found")
    real = norm(os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p)))
    ok = real.startswith(HOME + "/") or any(not v["system"] and real.startswith(v["path"].rstrip("/") + "/")
                                            for v in volumes())
    if not ok or (real in PROTECTED and real != TRASH):
        raise ValueError("protected location")
    return real


def tree_forget(path, size, contents=False):
    """Silinen yolu ağaçtan düşür. ponytail: ısı haritası/grafik verisi yeniden taramaya kadar eski kalır."""
    try:
        r = find(path)
    except ValueError:
        return
    if not r:  # küçük dosya: sadece toplamları düş
        r = find(os.path.dirname(path))
        if not r or r[1]:
            return
        nd, n = r[0], 1
        nd.small = max(0, nd.small - size)
        nd.smalln = max(0, nd.smalln - 1)
        name = os.path.basename(path)
        nd.names = "\n".join(x for x in nd.names.split("\n") if x != name)
    elif r[1]:
        nd, n = r[0], 1
        nd.files.remove(r[1])
    elif contents:  # klasör kalır, içi boşalır
        nd, size, n = r[0], r[0].size, r[0].n
        nd.kids, nd.files, nd.small, nd.smalln, nd.hist, nd.names = [], [], 0, 0, {}, ""
    else:
        node = r[0]
        if not node.parent:
            return
        nd, size, n = node.parent, node.size, node.n
        nd.kids.remove(node)
    while nd:
        nd.size -= size
        nd.n -= n
        nd = nd.parent
    _TREEV[0] += 1


def delete_items(paths, contents=()):
    """Manual delete: paths + mode. For paths in contents only the folder's contents are deleted."""
    items = []
    for p in paths:
        real = p
        try:
            real = check(p)
        except ValueError:
            pass
        items.append({"path": p, "action": "contents" if real in EMPTY_ONLY or p in contents else "rm",
                      "size": _size_of(p)})
    return items


def _size_of(p):
    try:
        r = find(p)
        if r and not r[1]:
            return r[0].size
        return usage(os.lstat(p))
    except (OSError, ValueError):
        return 0


# ---------- temizlik / öneri uçları ----------
_items_cache, _sug_cache = {}, {}


def clean_items(groups):
    key = ",".join(groups)
    if key not in _items_cache:
        _items_cache[key] = C.collect(C.load_conf(), groups)
    return _items_cache[key]


def run_clean(items, mode, source="manual"):
    """Job runner: delete items, then drop them from the tree and invalidate caches."""
    def run(items, prog):
        r = C.clean(items, mode, check, source, prog)
        done = set(r["done"])  # drop from cached lists instead of recomputing (suggestions take ~20 s)
        for k, v in _items_cache.items():
            _items_cache[k] = [i for i in v if i["path"] not in done]
        if "v" in _sug_cache:
            _sug_cache["v"] = [x for x in _sug_cache["v"] if x["path"] not in done]
        touched = {}
        for it in items:
            if it["path"] in r["done"] and it["action"] != "cmd":
                tree_forget(it["path"], it.get("size", 0), contents=it["action"] == "contents")
                if (o := owner(it["path"])) and not o["running"]:
                    touched[o["root"]] = o
        for o in touched.values():  # kalıcı önbellek de silinenleri bilsin (yeniden açınca eski hâli görünmesin)
            save_cache(o)
        return r
    return run


def clean_job(paths, groups, mode):
    items = [i for i in clean_items(groups) if i["path"] in set(paths)]
    return submit(f"Cleaning {len(items)} items", items, run_clean(items, mode))


def quick_job():
    items = clean_items(SAFE)
    return submit("Quick clean", items, run_clean(items, "rm"))


def auto_job():
    c = C.load_conf()
    items = C.collect(c, c["auto"]["groups"])
    return submit("Auto clean", items, run_clean(items, c["auto"]["mode"], "auto"))


SAFE = [g for g, _, _, risk in C.GROUPS if risk == "safe"]


def quick():
    """One-click cleanup estimate: everything in the 'safe' rule groups (like mac-cleanup.sh)."""
    items = clean_items(SAFE)
    names = {g: n for g, n, _, _ in C.GROUPS}
    by = {}
    for i in items:
        b = by.setdefault(i["group"], {"id": i["group"], "name": names[i["group"]], "size": 0, "n": 0})
        b["size"] += i.get("size", 0)
        b["n"] += 1
    return {"total": sum(b["size"] for b in by.values()), "groups": sorted(by.values(), key=lambda b: -b["size"]),
            "browsers_running": [C.BROWSERS[b][0] for b in C.running_browsers()]}


# ---------- background jobs ----------
JOBS, _jobq, _jid = [], queue.Queue(), [0]


def submit(title, items, run):
    """Queue work for the single background worker; run(items, progress) does it.
    ponytail: one worker, jobs run in order — parallel deletes on one disk gain nothing."""
    _jid[0] += 1
    j = {"id": _jid[0], "title": title, "status": "queued", "n": len(items), "done": 0, "errors": [], "cur": "",
         "total": sum(max(0, i.get("size") or 0) for i in items), "bytes": 0, "freed": 0, "note": "", "ts": time.time()}
    JOBS.append(j)
    del JOBS[:-30]
    _jobq.put((j, items, run))
    return {"id": j["id"]}


def _job_worker():
    while True:
        j, items, run = _jobq.get()
        j["status"] = "running"

        def prog(it, err):
            j["done"] += 1
            j["bytes"] += max(0, it.get("size") or 0)
            j["cur"] = it.get("label") or it["path"]
            if err:
                j["errors"].append({"path": it["path"], "error": err})
        try:
            r = run(items, prog) or {}
            j["freed"], j["note"] = r.get("freed", 0), r.get("note", "")
        except Exception as e:
            j["errors"].append({"path": "", "error": str(e)[:200]})
        j.update(status="done", cur="", end=time.time())


threading.Thread(target=_job_worker, daemon=True).start()


def delete_job(paths, mode, contents):
    items = delete_items(paths, contents)
    verb = "Deleting" if mode == "rm" else "Moving to Trash:"
    return submit(f"{verb} {len(items)} item{'s' * (len(items) != 1)}", items, run_clean(items, mode))


# ---------- Docker ----------
DOCKER_ACTIONS = [  # id, label, description, docker args
    ("containers", "Stopped containers", "Removes all stopped containers.", ["container", "prune", "-f"]),
    ("images", "Dangling images", "Untagged image layers left over from rebuilds.", ["image", "prune", "-f"]),
    ("images_all", "All unused images", "Every image not used by a container. Pulled again when needed.",
     ["image", "prune", "-a", "-f"]),
    ("build", "Build cache", "BuildKit cache. Next builds will be slower once.", ["builder", "prune", "-a", "-f"]),
    ("volumes", "Unused volumes", "All volumes not attached to a container. Their data is lost.",
     ["volume", "prune", "-a", "-f"]),
    ("networks", "Unused networks", "Custom networks not used by any container.", ["network", "prune", "-f"]),
]
_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def _dbytes(s):
    """'1.2GB' / '512kB' → bytes (docker prints decimal units)."""
    s = s.strip().split(" ")[0].upper()
    for u in ("TB", "GB", "MB", "KB", "B"):
        if s.endswith(u):
            try:
                return int(float(s[:-len(u)]) * _UNITS[u])
            except ValueError:
                return 0
    return 0


def docker_bin():
    return shutil.which("docker") or next((p for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker")
                                           if os.path.exists(p)), None)


def docker_info():
    d = docker_bin()
    raw = H("Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw")
    out = {"installed": bool(d), "running": False, "df": [], "raw": os.lstat(raw).st_blocks * 512 if os.path.exists(raw) else 0,
           "actions": [{"id": a, "label": l, "desc": ds, "cmd": "docker " + " ".join(c)} for a, l, ds, c in DOCKER_ACTIONS]}
    if not d:
        return out
    try:
        r = subprocess.run([d, "system", "df", "--format", "json"], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {**out, "error": "docker did not respond"}
    if r.returncode:
        return {**out, "error": (r.stderr or r.stdout).strip().splitlines()[-1:][0] if (r.stderr or r.stdout).strip() else "docker error"}
    txt = r.stdout.strip()
    rows = json.loads(txt) if txt.startswith("[") else [json.loads(l) for l in txt.splitlines() if l.strip()]
    out["df"] = [{"type": x.get("Type"), "count": x.get("TotalCount"), "active": x.get("Active"),
                  "size": _dbytes(x.get("Size", "0B")), "reclaimable": _dbytes(x.get("Reclaimable", "0B"))} for x in rows]
    out["running"] = True
    return out


def docker_job(ids):
    d = docker_bin()
    acts = [a for a in DOCKER_ACTIONS if a[0] in ids]
    if not d or not acts:
        raise ValueError("nothing to run")
    items = [{"path": a, "label": l, "size": 0} for a, l, _, _ in acts]
    cmds = {a: c for a, _, _, c in acts}

    def run(items, prog):
        freed = 0
        for it in items:
            try:
                r = subprocess.run([d, *cmds[it["path"]]], capture_output=True, text=True, timeout=1800)
                if r.returncode:
                    raise RuntimeError((r.stderr or r.stdout).strip()[-200:])
                line = next((l for l in r.stdout.splitlines() if "reclaimed space" in l.lower()), "")
                freed += _dbytes(line.split(":")[-1]) if line else 0
                prog(it, None)
            except Exception as e:
                prog(it, str(e)[:200])
        return {"freed": freed, "note": "Docker's disk image shrinks over time; Docker Desktop reclaims space on restart."}
    return submit(f"Docker: {', '.join(i['label'].lower() for i in items)}", items, run)


# ---------- recursive category view ----------
def typed(path, types, limit=3000):
    """Files of the given types under path, all subfolders included, largest first.
    Per-subfolder totals come from the type×age histogram, so files under 1 MB count too."""
    path, ts = (path if path == ALL else norm(path)), {int(t) for t in types}
    top, files, by = find_dir(path), [], []
    hsum = lambda nd: sum(v for k, v in nd.hist.items() if k >> 3 in ts)

    def take(nd, p):
        files.extend({"path": p.rstrip("/") + "/" + f[0], "name": f[0], "kind": "file", "size": f[1], "mtime": f[2],
                      "type": ftype(f[0])} for f in nd.files if ftype(f[0]) in ts)
        return hsum(nd)
    for k in top.kids:
        kp = kid_path(path, k)
        s = sum(take(n, p) for n, p in walk_tree(k, kp))
        if s:
            by.append({"path": kp, "name": k.name, "size": s})
    total = take(top, path) + sum(b["size"] for b in by)
    files.sort(key=lambda f: -f["size"])
    return {"files": files[:limit], "count": len(files), "total": total,
            "small": max(0, total - sum(f["size"] for f in files)), "by": sorted(by, key=lambda b: -b["size"])}


def suggest():
    if "v" not in _sug_cache:
        _sug_cache["v"] = C.suggestions(all_big(), C.load_conf())
    return _sug_cache["v"]


def clean_meta():
    conf = C.load_conf()
    conf["auth_user"] = (conf.pop("auth", None) or {}).get("user")  # parola özeti arayüze gitmez
    launchd = MAC and not SERVE[0]
    return {"groups": [{"id": g, "name": n, "desc": d, "risk": r} for g, n, d, r in C.GROUPS], "conf": conf,
            "profiles": C.browser_profiles(), "browsers": C.installed_browsers(),
            "browsers_running": [C.BROWSERS[b][0] for b in C.running_browsers()], "service": SERVE[0],
            "scheduler": "launchd" if launchd else "built-in" if SERVE[0] else None,
            "schedule_active": C.schedule_active() if launchd else SERVE[0], "next_run": C.next_run(conf["auto"]),
            "periods": C.PERIODS, "history": C.history()}


def save_conf(body):
    conf = C.load_conf()
    if "browser_profile" in body:
        conf["browser_profile"] = str(body["browser_profile"])
    a = body.get("auto", {})
    for k in ("groups", "mode", "dev_min_age_days", "dev_names", "custom_globs", "chrome_profiles", "schedule", "hour"):
        if k in a:
            conf["auto"][k] = a[k]
    if conf["auto"]["mode"] not in ("rm", "trash"):
        conf["auto"]["mode"] = "rm"
    if conf["auto"]["schedule"] not in C.PERIODS:
        conf["auto"]["schedule"] = "off"
    conf["auto"]["groups"] = [g for g in conf["auto"]["groups"] if g in C.GROUP_IDS]
    C.save_conf(conf)
    if ("schedule" in a or "hour" in a) and MAC:  # servis modunda launchd işi kaldırılır: kendi zamanlayıcımız çalışır
        C.set_schedule("off" if SERVE[0] else conf["auto"]["schedule"], conf["auto"]["hour"], os.path.realpath(__file__))
    _items_cache.clear()
    return clean_meta()


# ---------- görünüm durumu ----------
_pub = lambda S: {k: v for k, v in S.items() if k not in ("tree", "charts") and k[0] != "_"}


def _view_states():
    v = VIEW[0]
    return [ROOTS[r] for r in (disk_roots() if v == ALL else [v]) if r in ROOTS]


def status():
    ss = _view_states()
    if VIEW[0] == ALL:  # birleşik: sayaçlar toplanır
        run = [s for s in ss if s["running"]]
        agg = {"running": bool(run), "checking": any(s["checking"] for s in ss), "done": bool(ss) and all(s["done"] for s in ss),
               "files": sum(s["files"] for s in ss), "dirs": sum(s["dirs"] for s in ss), "bytes": sum(s["bytes"] for s in ss),
               "errors": sum(s["errors"] for s in ss), "start": min((s["start"] for s in run or ss), default=0),
               "end": max((s["end"] for s in ss), default=0), "full_at": min((s["full_at"] for s in ss), default=0),
               "cur": " · ".join(s["cur"] for s in run), "kind": run[0]["kind"] if run else "full",
               "changed": None if any(s["changed"] is None for s in ss) else sum(s["changed"] for s in ss),
               "from_cache": bool(ss) and all(s["from_cache"] for s in ss)}
    else:
        agg = _pub(ss[0] if ss else new_state(VIEW[0]))
    return {**agg, "root": VIEW[0], "roots": [_pub(s) for s in ROOTS.values()], "home": HOME, "cached": cached_roots(),
            "homedirs": home_dirs(), "now": time.time(), "volumes": volumes(), "types": TYPES, "all": ALL,
            "version": VERSION, "repo": REPO, "hostfs": HOSTFS}


_sub_charts = {}


def charts(path=None):
    path = path if path in (None, ALL) else norm(path)
    if path and path != VIEW[0] and not (VIEW[0] == ALL and path in disk_roots()):
        S = owner(path)
        if not S or not os.path.isdir(path):
            raise ValueError("folder is not in the scanned tree")
        key = (path, S["end"])
        if key not in _sub_charts:  # ponytail: alt klasör için ayrı, geçici tarama (uzantı/boyut/ay ağaçta tutulmuyor)
            st = os.stat(path)
            accs = _crawl(new_state(path), [(Node(path, None, st.st_mtime), path)], st.st_dev, time.time(), set())
            _sub_charts.clear()
            _sub_charts[key] = _charts(accs)
        return _sub_charts[key]
    if path and path != VIEW[0]:
        return (ROOTS.get(path) or {}).get("charts") or {}
    cs = [s["charts"] for s in _view_states() if s["charts"]]
    if len(cs) < 2:
        return cs[0] if cs else {}
    ext, sizes, months = {}, {}, {}
    for c in cs:
        for e in c["ext"]:
            t = ext.setdefault(e["ext"], {**e, "size": 0, "n": 0})
            t["size"] += e["size"]
            t["n"] += e["n"]
        for s in c["sizes"]:
            t = sizes.setdefault(s["lo"], {**s, "size": 0, "n": 0})
            t["size"] += s["size"]
            t["n"] += s["n"]
        for m in c["months"]:
            months[m["m"]] = months.get(m["m"], 0) + m["size"]
    return {"ext": sorted(ext.values(), key=lambda e: -e["size"])[:40], "sizes": sorted(sizes.values(), key=lambda s: s["lo"]),
            "months": [{"m": k, "size": v} for k, v in sorted(months.items())]}


def reveal(p, select, local):
    if not local:
        raise ValueError("opening files works only on the computer running DiskMan")
    p = norm(p)
    if MAC:
        cmd = ["open", "-R", p] if select else ["open", p]
    elif WIN:
        w = p.replace("/", "\\")
        cmd = ["explorer", "/select," + w] if select else ["explorer", w]
    else:
        cmd = ["xdg-open", os.path.dirname(p) if select else p]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": 1}


# ---------- erişim ----------
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SESSION_DAYS = 30


def pw_hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 300_000).hex()


def save_auth(b):
    """Arayüzden (ilk açılış / Settings) ya da --set-password ile; yalnız PBKDF2 özeti saklanır."""
    user, pw = str(b.get("user", "")).strip(), str(b.get("password", ""))
    if not user or ":" in user or len(pw) < 8:
        raise ValueError("enter a username (no ':') and a password of at least 8 characters")
    salt = secrets.token_hex(16)
    conf = C.load_conf()
    # key: oturum çerezlerini imzalar; parola değişince yenisi gelir → eski oturumlar düşer
    conf["auth"] = {"user": user, "salt": salt, "hash": pw_hash(pw, salt), "key": secrets.token_hex(32)}
    C.save_conf(conf)
    return {"ok": 1, "user": user}


def check_login(auth, user, pw):
    return hmac.compare_digest(str(user).encode(), auth["user"].encode()) \
        and hmac.compare_digest(pw_hash(str(pw), auth["salt"]), auth["hash"])


def _sig(auth, exp):
    return hmac.new(bytes.fromhex(auth.get("key") or auth["hash"]), f"{auth['user']}|{exp}".encode(), "sha256").hexdigest()


def session_cookie(auth):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return (f"dm={exp}.{_sig(auth, exp)}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict")


def valid_session(auth, cookie_header):
    """Durumsuz oturum: dm=<bitiş>.<HMAC>; sunucu yeniden başlasa da geçerli."""
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "dm":
            exp, _, sig = v.partition(".")
            if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sig(auth, exp)):
                return True
    return False


def set_password():
    import getpass
    user = input("Username [admin]: ").strip() or "admin"
    pw = getpass.getpass("Password (min 8 chars): ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords differ.")
    save_auth({"user": user, "password": pw})
    print(f"Saved (hash only) to {C.CONF_FILE}. Remote browsers now log in as '{user}'.")


_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale


def claimable(ip, host):
    """Parola yokken ilk kurulum ekranı yalnız ev ağı / Tailscale'den açılır (internetten değil).
    Host bir IP ya da yerel ad olmalı: yabancı alan adı = DNS rebinding, parolayı başkası belirleyemesin."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    try:
        ipaddress.ip_address(host)
        local_name = True
    except ValueError:
        local_name = "." not in host or host.endswith((".local", ".lan", ".home.arpa", ".ts.net"))
    return (a.is_private or a in _CGNAT) and not a.is_loopback and local_name


ICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#15181e"/>'
        b'<g transform="translate(3 3) scale(.8125)"><path d="M16 16V3A13 13 0 1 0 29 16Z" fill="#ff7a45"/>'
        b'<path d="M19 13V2a11 11 0 0 1 11 11Z" fill="#2dd4bf"/><circle cx="16" cy="16" r="2.6" fill="#15181e"/></g></svg>')
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DiskMan</title><style>:root{color-scheme:dark light}body{margin:0;min-height:100vh;display:grid;place-items:center;
font:15px/1.5 system-ui,sans-serif;background:#16181d;color:#e8e6e1;padding:16px}form{width:100%%;max-width:360px;background:#1d2129;
border:1px solid #2c313b;border-radius:14px;padding:24px;display:grid;gap:12px}h1{margin:0;font-size:20px}p{margin:0;color:#9aa0aa;font-size:13px}
label{display:grid;gap:4px;font-size:13px}input{font:inherit;padding:10px;border-radius:8px;border:1px solid #2c313b;background:#16181d;color:inherit}
button{font:inherit;font-weight:600;padding:11px;border:0;border-radius:8px;background:#ff7a45;color:#16181d;cursor:pointer}#e{color:#ff4d5e}
@media (prefers-color-scheme:light){body{background:#f6f5f2;color:#1a1c20}form{background:#fff;border-color:#e4e1da}input{background:#fff;border-color:#e4e1da}}</style></head>
<body><form id="f"><h1>%(title)s</h1><p>%(intro)s</p>
<label>Username<input name="user" autocomplete="username" required value="admin" autocapitalize="none"></label>
<label>Password<input name="password" type="password" autocomplete="%(ac)s" minlength="8" required autofocus></label>%(again)s
<p id="e" role="alert"></p><button>%(btn)s</button></form><script>
f.onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(f)),E=document.getElementById('e');
if(d.again!==undefined&&d.password!==d.again)return E.textContent='Passwords do not match.';
const r=await fetch('%(url)s',{method:'POST',headers:{'X-DM':'1','Content-Type':'application/json'},body:JSON.stringify(d)});
const j=await r.json().catch(()=>({}));if(!r.ok)return E.textContent=j.error||'Could not log in.';location.replace('/')}
</script></body></html>"""
SETUP_HTML = (_PAGE % {"title": "Set up DiskMan", "btn": "Save and log in", "url": "/api/setup", "ac": "new-password",
                       "intro": "Choose the username and password for opening DiskMan from other devices.",
                       "again": '\n<label>Repeat password<input name="again" type="password" autocomplete="new-password" '
                                'minlength="8" required></label>'}).encode()
LOGIN_HTML = (_PAGE % {"title": "DiskMan", "btn": "Log in", "url": "/api/login", "ac": "current-password",
                       "intro": "Log in to manage the disks of this computer.", "again": ""}).encode()


# ---------- HTTP ----------
IDLE = 30 * 60  # bu kadar süre kullanılmazsa (sekme açık olsa da) kapan
_last_ping = [time.time()]
LIVE_PORT = [PORT]  # asistanın sistem isteminde DiskMan API adresi


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # yalnız hatalar (her poll'ü yazmak journal'ı doldurur)
        msg = repr(fmt % a)[1:-1]  # HTTPS denemeleri ikili çöp getirir → kaçışla, kısalt
        sys.stderr.write(f"{self.client_address[0]} {msg[:200]}\n")

    def log_request(self, code="-", size="-"):
        if str(code)[0] in "45":
            super().log_request(code, size)

    def _send(self, code, body, ctype="application/json", cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600" if ctype == "image/png" else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        """Yerel tarayıcı (loopback + localhost Host'u) serbest; Host kontrolü DNS rebinding/tünelleri keser.
        Diğer herkes: giriş sayfası + imzalı oturum çerezi; parola yoksa kapalı (DISKMAN_AUTH=off ile açılabilir)."""
        self.host = host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        try:
            loop = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            loop = False
        self.local = loop and host in LOCAL_HOSTS
        if self.local:
            return True
        auth = C.load_conf().get("auth")
        if not auth:
            return os.environ.get("DISKMAN_AUTH") == "off"
        return valid_session(auth, self.headers.get("Cookie"))

    def _route(self, body=None):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/icon.svg":  # CasaOS panosu simgeyi girişsiz ister
            return self._send(200, ICON, "image/svg+xml")
        if not self._authorized():
            auth, post = C.load_conf().get("auth"), body is not None and self.headers.get("X-DM") == "1"
            try:
                b = json.loads(body or "{}") if post else {}
            except ValueError:
                b = {}
            if auth:  # giriş sayfası (tarayıcının Basic penceresi yerine)
                if u.path == "/api/login" and post:
                    if check_login(auth, b.get("user", ""), b.get("password", "")):
                        return self._send(200, {"ok": 1}, cookie=session_cookie(auth))
                    time.sleep(1)  # ponytail: kaba deneme yavaşlatıcı; internete açılacaksa önüne TLS'li ters vekil koy
                    return self._send(401, {"error": "Wrong username or password."})
                if u.path == "/":
                    return self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
                return self._send(401, {"error": "login required"})
            if claimable(self.client_address[0], self.host):  # ilk açılış: parolayı belirle, hemen oturum aç
                if u.path == "/api/setup" and post:
                    try:
                        r = save_auth(b)
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, r, cookie=session_cookie(C.load_conf()["auth"]))
                return self._send(200, SETUP_HTML, "text/html; charset=utf-8")
            return self._send(403, b"Remote access is off: set a password from your home network or in Settings.\n",
                              "text/plain; charset=utf-8")
        if u.path == "/" or body is not None or q.get("a"):  # sayfa açma, işlem, ya da kullanıcı etkileşimli ping
            _last_ping[0] = time.time()
        if u.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/thumb":  # <img> özel başlık gönderemez
            try:
                return self._send(200, thumb(q["path"], q.get("s", 256)), "image/png")
            except Exception as e:
                return self._send(404, {"error": str(e)})
        if self.headers.get("X-DM") != "1":  # özel başlık: çapraz site istekleri CORS ön kontrolüne takılır (CSRF)
            return self._send(403, {"error": "missing X-DM header"})
        try:
            groups = [g for g in q.get("groups", "").split(",") if g] or C.GROUP_IDS
            r = {
                "/api/ping": lambda: {"ok": 1},
                "/api/status": status,
                "/api/tree": lambda: tree_json(q.get("path", VIEW[0]), int(q.get("depth", 2)), int(q.get("top", 40))),
                "/api/stats": lambda: stats(q.get("path", VIEW[0])),
                "/api/cell": lambda: cell(q.get("path", VIEW[0]), int(q["type"]), int(q["age"])),
                "/api/charts": lambda: charts(q.get("path")),
                "/api/ls": lambda: ls(q.get("path", HOME)),
                "/api/search": lambda: search(q["q"], q.get("path", VIEW[0] or HOME)),
                "/api/big": lambda: big_files(q.get("min", 500), q.get("older", 0), q.get("path")),
                "/api/empty": lambda: empty_dirs(q.get("path", VIEW[0])),
                "/api/dupes": lambda: DUP,
                "/api/growth": lambda: growth(q.get("path", VIEW[0]), q.get("since")),
                "/api/graph": lambda: graph(q.get("path", VIEW[0])),
                "/api/clean/meta": clean_meta,
                "/api/clean/items": lambda: clean_items(groups),
                "/api/suggest": suggest,
                "/api/quick": quick,
                "/api/jobs": lambda: JOBS,
                "/api/docker": docker_info,
                "/api/typed": lambda: typed(q.get("path", VIEW[0]), q["types"].split(",")),
                "/api/auto/preview": lambda: C.auto_clean(check, dry=True),
                "/api/reveal": lambda: reveal(q["path"], True, self.local),
                "/api/open": lambda: reveal(q["path"], False, self.local),
                "/api/ai": lambda: AI.state(int(q.get("since", 0))),
            }.get(u.path)
            if body is not None:
                b = json.loads(body or "{}")
                r = {"/api/scan": lambda: start_scan(b.get("root") or VIEW[0] or HOME, b.get("mode", "auto")) or {"ok": 1},
                     "/api/delete": lambda: delete_job(b["paths"], b.get("mode", "trash"), set(b.get("contents", []))),
                     "/api/clean/run": lambda: clean_job(b["paths"], b.get("groups") or C.GROUP_IDS, b.get("mode", "rm")),
                     "/api/quick/run": quick_job,
                     "/api/auto/run": auto_job,
                     "/api/docker/run": lambda: docker_job(set(b.get("ids", []))),
                     "/api/dupes/run": lambda: dupes_start(b.get("min", 10), b.get("path")),
                     "/api/refresh": lambda: refresh(b["path"]),
                     "/api/config": lambda: save_conf(b),
                     "/api/setup": lambda: save_auth(b),  # parola değiştir (oturum açıkken)
                     "/api/ai/send": lambda: AI.send(b.get("text", ""), b.get("path"), LIVE_PORT[0]),
                     "/api/ai/permit": lambda: AI.permit(b["id"], b.get("decision", "deny")),
                     "/api/ai/stop": AI.stop,
                     "/api/ai/reset": AI.reset,
                     "/api/logout": lambda: {"ok": 1}}.get(u.path)
            if not r:
                return self._send(404, {"error": "not found"})
            res = r()
            if u.path == "/api/setup":  # yeni parola: bu tarayıcı oturumda kalsın (eski çerezler artık geçersiz)
                return self._send(200, res, cookie=session_cookie(C.load_conf()["auth"]))
            self._send(200, res, cookie="dm=; Path=/; Max-Age=0" if u.path == "/api/logout" else None)
        except Exception as e:
            self._send(400, {"error": str(e)})

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route(self.rfile.read(int(self.headers.get("Content-Length", 0))))


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # paket gitmez; yalnız varsayılan arayüzün IP'si
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def install(remove, lan, port=PORT):
    """Arka plan servisi: macOS LaunchAgent, Linux systemd (root), Windows Task Scheduler (yönetici).
    Gereken izni ister: sudo / UAC; macOS'ta Full Disk Access ayar sayfasını açar."""
    exe, script = sys.executable, os.path.realpath(__file__)
    args = [script, "--serve", *(["--host", "0.0.0.0"] if lan else []), str(port)]
    if MAC:
        import plistlib
        plist, uid = H("Library/LaunchAgents/com.fxerkan.diskman.server.plist"), os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist], capture_output=True)
        if remove:
            os.path.exists(plist) and os.remove(plist)
            return print("DiskMan service removed.")
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        os.makedirs(CACHE, exist_ok=True)
        with open(plist, "wb") as f:
            plistlib.dump({"Label": "com.fxerkan.diskman.server", "ProgramArguments": [exe, *args], "RunAtLoad": True,
                           "KeepAlive": True, "StandardErrorPath": os.path.join(CACHE, "server.log")}, f)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], check=True)
        print(f"Grant Full Disk Access to {exe} (opening System Settings) so every folder can be scanned.")
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
    elif WIN:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():  # UAC ile yükseltilmiş olarak kendini yeniden çalıştır
            ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline([script, *sys.argv[1:]]), None, 1)
            return print("Continuing in the elevated window…")
        subprocess.run(["schtasks", "/End", "/TN", "DiskMan"], capture_output=True)
        if remove:
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", "DiskMan"], check=True)
            return print("DiskMan service removed.")
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")  # konsol penceresi açmasın
        cmd = subprocess.list2cmdline([pyw if os.path.exists(pyw) else exe, *args])
        subprocess.run(["schtasks", "/Create", "/F", "/TN", "DiskMan", "/SC", "ONLOGON", "/RL", "HIGHEST", "/TR", cmd], check=True)
        subprocess.run(["schtasks", "/Run", "/TN", "DiskMan"], check=True)
    else:
        if os.geteuid() != 0:  # sistemin tamamını okuyabilmek için root servis; HOME korunur → ayar/önbellek aynı yerde
            os.execvp("sudo", ["sudo", "env", f"HOME={HOME}", exe, script, *sys.argv[1:]])
        import shlex
        unit = "/etc/systemd/system/diskman.service"
        if remove:
            subprocess.run(["systemctl", "disable", "--now", "diskman"])
            os.path.exists(unit) and os.remove(unit)
            subprocess.run(["systemctl", "daemon-reload"])
            return print("DiskMan service removed.")
        with open(unit, "w") as f:
            f.write(f"""[Unit]
Description=DiskMan web UI
After=network-online.target local-fs.target

[Service]
Environment=HOME={HOME}
ExecStart={shlex.join([exe, *args])}
Restart=on-failure
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
""")
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "diskman"], check=True)
        subprocess.run(["systemctl", "restart", "diskman"], check=True)
    print(f"DiskMan runs in the background: http://{lan_ip() if lan else '127.0.0.1'}:{port}/")
    if lan and not C.load_conf().get("auth"):
        print("Open that address once to choose the username and password.")


def running_at(url):
    """Bu adreste DiskMan zaten çalışıyor mu (başka bir program değil)?"""
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "api/ping", headers={"X-DM": "1"}), timeout=1) as r:
            return json.load(r).get("ok") == 1
    except Exception:
        return False


def open_ui(url):
    """Ayardaki tarayıcı ("firefox", "edge:Profile 1"; eski ayar "Profile 2" = Chrome) yoksa varsayılan tarayıcı."""
    pref = C.load_conf().get("browser_profile", "")
    b, _, prof = pref.partition(":") if ":" in pref or pref in C.BROWSERS else ("chrome", ":", pref)
    cmd = C.browser_cmd(b) if pref else None
    if not cmd:
        return webbrowser.open(url)
    chromium = b not in ("firefox", "safari")
    extra = [f"--profile-directory={prof}"] if prof and chromium else []
    subprocess.Popen(cmd + (["--args", *extra, url] if MAC and extra else [url] if MAC else [*extra, url]),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def refresh_all():
    """Gece: değişen klasörleri yeniden tara → arama dizini güncel kalır, Büyüme için günlük anlık görüntü."""
    for r in disk_roots():
        S = ROOTS.get(r)
        if S and S["tree"] and not (S["running"] or S["checking"]):
            check_changes(S)
            S["_changed"] and rescan(S)


def scheduler():
    """Servis/konteyner modunda zamanlanmış otomatik temizlik (launchd/cron/systemd timer gerekmez)."""
    last, day = os.path.join(C.CONF_DIR, "auto.last"), None
    while True:
        t = time.localtime()
        key = time.strftime("%Y-%m-%d %H", t)
        if t.tm_hour == 4 and day != key[:10]:
            day = key[:10]
            threading.Thread(target=refresh_all, daemon=True).start()
        try:
            with open(last) as f:
                done = f.read()
        except OSError:
            done = ""
        try:
            if key != done and C.due(C.load_conf()["auto"], t):
                os.makedirs(C.CONF_DIR, exist_ok=True)
                with open(last, "w") as f:  # yeniden başlatmada aynı saatte ikinci kez çalışmasın
                    f.write(key)
                auto_job()
        except Exception as e:
            sys.stderr.write(f"scheduler: {e}\n")
        time.sleep(60)


def main():
    a = sys.argv[1:]
    if "--set-password" in a:
        return set_password()
    if "--clear-password" in a:
        conf = C.load_conf()
        conf.pop("auth", None)
        C.save_conf(conf)
        return print("Password removed: remote access is off.")
    port = int(next((x for x in a if x.isdigit()), PORT))
    if "--install" in a or "--uninstall" in a:
        return install("--uninstall" in a, "--lan" in a, port)
    if "--auto-clean" in sys.argv:
        r = C.auto_clean(check, dry="--dry" in sys.argv)
        print(time.strftime("%F %T"), json.dumps({k: v for k, v in r.items() if k != "items"}),
              *(f"\n  {i['path']}  {i.get('size', 0) >> 20} MB" for i in r.get("items", [])), flush=True)
        return
    serve = SERVE[0] = "--serve" in a or bool(os.environ.get("DISKMAN_NO_BROWSER"))
    host = a[a.index("--host") + 1] if "--host" in a else os.environ.get("DISKMAN_HOST", "127.0.0.1")  # 0.0.0.0 → LAN
    url = f"http://127.0.0.1:{port}/"
    if not serve and running_at(url):  # zaten açık → yalnız sekmeyi aç
        return open_ui(url)
    if not serve and not os.environ.get("DISKMAN_CHILD"):  # terminali tutma: arka planda başlat, tarayıcıyı o açar
        os.makedirs(CACHE, exist_ok=True)
        with open(os.path.join(CACHE, "app.log"), "ab") as log:
            subprocess.Popen([sys.executable, os.path.realpath(__file__), *a], env={**os.environ, "DISKMAN_CHILD": "1"},
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError:  # port başka bir programda → rastgele port
        srv = ThreadingHTTPServer((host, 0), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/"
    LIVE_PORT[0] = srv.server_port
    lan = host not in ("127.0.0.1", "localhost")
    print(f"DiskMan: {url}" + (f"  ·  LAN: http://{lan_ip()}:{srv.server_port}/" if lan else "")
          + ("" if serve else f"\n(Exits after {IDLE // 60} min without use)"), flush=True)
    if lan and not C.load_conf().get("auth") and os.environ.get("DISKMAN_AUTH") != "off":
        print("No password yet: open the LAN address to set one", flush=True)
    last = cached_roots()
    # servis: tüm diskler paralel (önbellekten anında, sonra değişiklik kontrolü); uygulama: son taranan kök
    start_scan(ALL if serve else last[0]["path"] if last else HOME)

    def watchdog():  # IDLE süresince kullanılmazsa kapan; tarama/silme sürerken bekle
        while time.time() - _last_ping[0] < IDLE or any(S["running"] for S in list(ROOTS.values())) \
                or any(j["status"] != "done" for j in JOBS) or AI.busy():
            time.sleep(10)
        os._exit(0)
    if serve:
        threading.Thread(target=scheduler, daemon=True).start()
    if not serve:  # başsız (servis) modda sekme yok → watchdog da yok
        threading.Thread(target=watchdog, daemon=True).start()
        open_ui(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
