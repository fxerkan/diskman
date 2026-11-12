"""Önbellek + artımlı tarama self-check: artımlı sonuç tam taramayla aynı olmalı.  python3 test_cache.py"""
import os, shutil, sys, tempfile
d = tempfile.mkdtemp()
os.environ["HOME"] = d  # önbellek geçici ev klasörüne yazılsın
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diskman as D

def w(p, n):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(os.urandom(n))

def snap():
    return sorted((p, nd.size, nd.n, len(nd.files)) for nd, p in D.walk_tree(S["tree"], S["root"]))

r = D.norm(os.path.join(d, "data"))
w(f"{r}/a/x.bin", 2 << 20); w(f"{r}/a/b/y.txt", 5000); w(f"{r}/c/z.mp4", 3 << 20); w(f"{r}/c/d/e/q", 100)
S = D.ROOTS[r] = D.new_state(r)
D.scan(S)
D.load_cache(S); D.check_changes(S)
assert S["changed"] == 0, S["changed"]
os.utime(f"{r}/c", (1, 1)); os.utime(f"{r}/c", None)  # mtime'ı kesin değiştir
w(f"{r}/a/b/new/big.bin", 4 << 20); os.remove(f"{r}/c/z.mp4"); shutil.rmtree(f"{r}/c/d")
w(f"{r}/a/x.bin", 5 << 20)  # büyük dosya yerinde büyüdü
D.check_changes(S)
assert S["changed"] >= 3, S["changed"]
D.rescan(S); inc = snap(); tot = (S["bytes"], S["files"])
D.scan(S); assert inc == snap(), (inc, snap())
assert tot == (S["bytes"], S["files"])
D.load_cache(S); D.check_changes(S); assert S["changed"] == 0
assert D.cached_roots()[0]["path"] == r
shutil.rmtree(d); print("ok")
