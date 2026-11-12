"""Paralel çoklu disk + birleşik görünüm + erişim self-check.  python3 test_multi.py"""
import json, os, shutil, sys, tempfile, threading, time, urllib.request
d = tempfile.mkdtemp()
os.environ["HOME"] = d
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diskman as D


def w(p, n):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(os.urandom(n))


a, b = D.norm(d + "/diskA"), D.norm(d + "/diskB")
w(f"{a}/x/big.mp4", 3 << 20); w(f"{a}/y/s.txt", 10); w(f"{b}/z/big.zip", 2 << 20)
D.disk_roots = lambda: [a, b]  # iki "disk"
D.start_scan(D.ALL, "full")
while any(s["running"] or s["checking"] for s in D.ROOTS.values()) or len(D.ROOTS) < 2:
    time.sleep(0.05)
st = D.status()
assert st["root"] == D.ALL and st["done"] and st["files"] == 3, st
t = D.tree_json(D.ALL, 2, 10)
assert {k["path"] for k in t["children"]} == {a, b} and t["size"] == st["bytes"], t
assert D.find(f"{b}/z")[0].name == "z" and D.find(f"{a}/x/big.mp4")[1][0] == "big.mp4"
assert {p for p, *_ in D.all_big()} == {f"{a}/x/big.mp4", f"{b}/z/big.zip"}
assert {r["path"] for r in D.ls(D.ALL)["rows"]} == {a, b}
assert {e["ext"] for e in D.charts()["ext"]} >= {"mp4", "zip"}
assert [r["path"] for r in D.search("big", D.ALL)][0] == f"{a}/x/big.mp4"
D.start_scan(b)  # tek diske geçiş: önbellekteki ağaç, değişiklik kontrolü
while D.ROOTS[b]["checking"]:
    time.sleep(0.05)
assert D.status()["root"] == b and D.status()["files"] == 1

# HTTP: yerel serbest (X-DM şart), uzak Host → parola
srv = D.ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/api/status"


def get(h):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=h)).status
    except urllib.error.HTTPError as e:
        return e.code


assert get({"X-DM": "1"}) == 200
assert get({}) == 403  # CSRF: özel başlık yok
assert get({"X-DM": "1", "Host": "evil.example"}) == 403  # parola yok → uzak erişim kapalı
assert D.claimable("192.168.1.5", "192.168.1.62") and D.claimable("100.70.1.2", "rpifx.local")
assert not D.claimable("192.168.1.5", "evil.example") and not D.claimable("8.8.8.8", "1.2.3.4")
assert not D.claimable("127.0.0.1", "evil.example")  # loopback + yabancı Host (tünel/rebinding)
assert D.save_auth({"user": "u", "password": "secret12"})["ok"]
base = url.rsplit("/api", 1)[0]


def req(path, h, body=None):
    r = urllib.request.Request(base + path, headers=h, data=body and json.dumps(body).encode())
    try:
        with urllib.request.urlopen(r) as x:
            return x.status, x.headers.get("Set-Cookie"), x.read()
    except urllib.error.HTTPError as e:
        return e.code, None, e.read()


R = {"X-DM": "1", "Host": "pi.lan"}
assert get(R) == 401  # oturum yok
code, _, page = req("/", {"Host": "pi.lan"})
assert code == 200 and b"/api/login" in page and b"WWW-Authenticate" not in page  # HTML giriş sayfası, tarayıcı penceresi değil
assert req("/api/login", R, {"user": "u", "password": "wrong"})[0] == 401
code, ck, _ = req("/api/login", R, {"user": "u", "password": "secret12"})
assert code == 200 and "HttpOnly" in ck and "SameSite=Strict" in ck
cookie = ck.split(";")[0]
assert get({**R, "Cookie": cookie}) == 200
assert get({**R, "Cookie": "dm=9999999999.bad"}) == 401
D.save_auth({"user": "u", "password": "another12"})  # parola değişti → eski oturum düşer
assert get({**R, "Cookie": cookie}) == 401
assert "auth" not in D.clean_meta()["conf"] and D.clean_meta()["conf"]["auth_user"] == "u"
srv.shutdown(); shutil.rmtree(d); print("ok")
