"""Seçili klasör self-check: büyük dosya/kopya/grafik yalnız o klasörden, Refresh silineni düşürür.  python3 test_scope.py"""
import os, shutil, sys, tempfile, time
d = tempfile.mkdtemp()
os.environ["HOME"] = d
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diskman as D

def w(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)

r = D.norm(os.path.join(d, "data"))
blob = os.urandom(3 << 20)
w(f"{r}/a/x/big.bin", blob); w(f"{r}/a/dup.bin", blob); w(f"{r}/b/other.bin", os.urandom(2 << 20))
S = D.ROOTS[r] = D.new_state(r)
D.VIEW[0] = r
D.scan(S)
assert {f["path"].rsplit("/", 1)[1] for f in D.big_files(1, 0, f"{r}/a")} == {"big.bin", "dup.bin"}
assert len(D.big_files(1)) == 3
assert sum(e["n"] for e in D.charts(f"{r}/a")["ext"]) == 2
D.dup_scan(1 << 20, f"{r}/a"); assert len(D.DUP["groups"]) == 1 and D.DUP["root"] == f"{r}/a"
shutil.rmtree(f"{r}/b")  # DiskMan dışında silindi
D.refresh(f"{r}/b")
while S["running"]:
    time.sleep(0.05)
assert [k.name for k in S["tree"].kids] == ["a"], [k.name for k in S["tree"].kids]
w(f"{r}/a/x/new.bin", b"1")  # yeni dosya → x ve üstleri "güncel değil"
D.check_changes(S)
t = D.tree_json(r, 2, 40)
assert t.get("stale") and [k for k in t["children"] if k["name"] == "a"][0]["stale"], t
D.rescan(S)
assert "stale" not in D.tree_json(r, 2, 40)
shutil.rmtree(d); print("ok")
