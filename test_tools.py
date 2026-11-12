"""Araçlar self-check: ad araması (küçük dosyalar), boş klasörler, kopyalar, büyüme, graf, zamanlama.  python3 test_tools.py"""
import os, sys, tempfile, time
d = tempfile.mkdtemp()
os.environ["HOME"] = d
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diskman as D
import cleaner as C


def w(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)


r = D.norm(d + "/disk")
blob = os.urandom(2 << 20)
w(f"{r}/a/movie.mkv", blob); w(f"{r}/b/copy.mkv", blob)                    # kopya
w(f"{r}/c/same-size.mkv", blob[:-1] + bytes([blob[-1] ^ 1]))              # aynı boyut, son bayt farklı
w(f"{r}/a/Notes Tiny.txt", b"hi")                                         # küçük dosya
w(f"{r}/a/İstanbul.txt", b"hi")
os.makedirs(f"{r}/empty/inner"); os.makedirs(f"{r}/b/also-empty")
D.disk_roots = lambda: [r]
D.start_scan(r, "full")
while D.ROOTS[r]["running"]:
    time.sleep(0.05)

assert [x["path"] for x in D.search("tiny notes", r)] == [f"{r}/a/Notes Tiny.txt"], D.search("tiny notes", r)
assert [x["name"] for x in D.search("istanbul", r)] == ["İstanbul.txt"]
assert {x["path"] for x in D.search("mkv", r)} == {f"{r}/a/movie.mkv", f"{r}/b/copy.mkv", f"{r}/c/same-size.mkv"}
assert [x["path"] for x in D.empty_dirs(r)] == [f"{r}/b/also-empty", f"{r}/empty"], D.empty_dirs(r)

D.dupes_start(1)
while D.DUP["running"]:
    time.sleep(0.05)
g = D.DUP["groups"]
assert len(g) == 1 and {f["path"] for f in g[0]["files"]} == {f"{r}/a/movie.mkv", f"{r}/b/copy.mkv"}, g

old = D._load_snaps(r)[-1]
old["t"] -= 86400 * 3
old["sizes"][f"{r}/a"] = D.find(f"{r}/a")[0].size - (5 << 20)
open(D._snap_path(r), "w").write(D.json.dumps([old, *D._load_snaps(r)]))
gr = D.growth(r, old["t"])
assert gr["rows"][0]["path"] == f"{r}/a" and gr["rows"][0]["delta"] == 5 << 20, gr

gp = D.graph(r)
kinds = {n["kind"] for n in gp["nodes"]}
assert {"root", "dir", "file"} <= kinds and any(l["kind"] == "dup" for l in gp["links"]), kinds
ids = {n["id"] for n in gp["nodes"]}
assert all(l["source"] in ids and l["target"] in ids for l in gp["links"])

D.tree_forget(f"{r}/a/Notes Tiny.txt", 4096)
assert not D.search("tiny", r)

a = {"schedule": "monthly", "hour": 3}
t = time.localtime(C.next_run(a))
assert t.tm_mday == 1 and t.tm_hour == 3
assert time.localtime(C.next_run({"schedule": "yearly", "hour": 1})).tm_yday == 1
assert time.localtime(C.next_run({"schedule": "weekly", "hour": 1})).tm_wday == 6
assert C.next_run({"schedule": "off"}) is None
print("ok")
