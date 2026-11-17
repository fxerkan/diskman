"""Asistan self-check: sahte bir `claude` (stream-json) ile gönder → izin iste → onayla → sonuç.  python3 test_assistant.py"""
import json, os, sys, tempfile, time
d = tempfile.mkdtemp()
fake = os.path.join(d, "claude")
with open(fake, "w") as f:
    f.write(f"""#!{sys.executable}
import json, sys
out = lambda o: print(json.dumps(o), flush=True)
if sys.argv[1:2] == ["auth"]:
    out({{"authMethod": "claude.ai", "email": "me@example.com"}})
    sys.exit()
for line in sys.stdin:
    m = json.loads(line)
    if m["type"] == "user":
        out({{"type": "system", "subtype": "init", "session_id": "s1"}})
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {{"command": "rm -rf ~/x", "description": "Delete x"}}}}]}}}})
        out({{"type": "control_request", "request_id": "r1", "request": {{"subtype": "can_use_tool", "tool_name": "Bash",
             "input": {{"command": "rm -rf ~/x"}}, "permission_suggestions": [{{"type": "addRules", "destination": "localSettings"}}]}}}})
    elif m["type"] == "control_response":
        res = m["response"]["response"]
        out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(res)}}]}}}})
        out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "done"}}]}}}})
        out({{"type": "result", "total_cost_usd": 0.01}})
""")
os.chmod(fake, 0o755)
os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
os.environ["ANTHROPIC_API_KEY"] = "test"  # abonelik: alt sürece geçmemeli
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assistant as A


def wait(cond):
    t = time.time()
    while not cond():
        assert time.time() - t < 5, A.ST["events"]
        time.sleep(0.02)


A.send("clean up", "/tmp", 1)
wait(lambda: A.ST["pending"])
ev = A.state()["events"]
assert [e["k"] for e in ev] == ["user", "tool", "ask"], ev
assert ev[2]["state"] == "pending" and A.state()["pending"] == 1
A.permit("r1", "always")
wait(lambda: not A.busy())
s = A.state()
assert [e["k"] for e in s["events"]] == ["user", "tool", "ask", "out", "text", "done"], s
res = json.loads(s["events"][3]["text"])
assert res["behavior"] == "allow" and res["updatedPermissions"][0]["destination"] == "session", res  # ayar dosyasına yazılmaz
assert "ANTHROPIC_API_KEY" not in A.subscription_env() and s["account"] == "me@example.com"
assert s["events"][2]["state"] == "always" and s["pending"] == 0 and A.ST["session"] == "s1"
assert A.state(4)["asks"][0]["state"] == "always"  # değişen izin kartı sonraki poll'lerde de gelir
A.reset()
assert A.state()["events"] == [] and not A.ST["proc"]
print("assistant OK")
