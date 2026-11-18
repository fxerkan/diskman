"""Asistan self-check: sahte bir `claude` (stream-json) ile gönder → istisnai izin → Always allow (kalıcı) →
ikinci kez sorulmaz; yok sayma hafızası.  python3 test_assistant.py"""
import json, os, sys, tempfile, time
d = tempfile.mkdtemp()
os.environ["HOME"] = d  # ayar dosyası (allowlist, ignore) geçici klasöre
fake = os.path.join(d, "claude")
with open(fake, "w") as f:
    f.write(f"""#!{sys.executable}
import json, sys
out = lambda o: print(json.dumps(o), flush=True)
if sys.argv[1:2] == ["auth"]:
    out({{"authMethod": "claude.ai", "email": "me@example.com"}})
    sys.exit()
open({json.dumps(d + "/argv.json")}, "w").write(json.dumps(sys.argv))
n = 0
for line in sys.stdin:
    m = json.loads(line)
    if m["type"] == "user":
        n += 1
        out({{"type": "system", "subtype": "init", "session_id": "s1"}})
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": f"t{{n}}", "name": "Bash",
             "input": {{"command": "cd /x && sudo du -sh /private/var"}}}}]}}}})
        out({{"type": "control_request", "request_id": f"r{{n}}", "request": {{"subtype": "can_use_tool", "tool_name": "Bash",
             "input": {{"command": "cd /x && sudo du -sh /private/var"}}}}}})
    elif m["type"] == "control_response":
        res = m["response"]["response"]
        out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": f"t{{n}}", "content": json.dumps(res)}}]}}}})
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


# istisnai parçalar: sarmalayıcı (rtk/command), env ataması, zincir
assert A._risky("cd /x && sudo du -sh /y") == ["sudo du"]
assert A._risky("FOO=1 command rtk du -sh /y; ls") == []
assert A._risky("git status && git push origin main") == ["git push origin"]
assert A._risky("defaults write com.apple.x k v") == ["defaults write com.apple.x"]

A.send("measure /private/var", "/tmp", 1)
wait(lambda: A.ST["pending"])
argv = json.load(open(d + "/argv.json"))
assert "--dangerously-skip-permissions" in argv and "--permission-prompt-tool" in argv  # clauded gibi
assert "Bash(sudo:*)" in json.loads(argv[argv.index("--settings") + 1])["permissions"]["ask"]
ev = A.state()["events"]
assert [e["k"] for e in ev] == ["user", "tool", "ask"] and ev[2]["rules"] == ["Bash: sudo du"], ev
A.permit("r1", "always")
wait(lambda: not A.busy())
assert A.memory()["allow"] == ["Bash: sudo du"]  # kalıcı allowlist
assert "ANTHROPIC_API_KEY" not in A.subscription_env() and A.state()["account"] == "me@example.com"

A.send("again", None, 1)  # aynı istisnai komut: sorulmadan onaylanır
wait(lambda: not A.busy())
s = A.state()
assert s["events"][-4]["k"] == "ask" and s["events"][-4]["state"] == "auto" and s["pending"] == 0, s["events"]
assert json.loads(s["events"][-3]["text"])["behavior"] == "allow"

m = A.edit_memory({"add_ignore": [{"what": "/a/b", "note": "keep"}, {"what": "/a/b"}]})
assert [x["what"] for x in m["ignore"]] == ["/a/b"]  # tekrarlanmaz
assert "/a/b (keep)" in A.system_prompt(1)
A.send("third", None, 1)
wait(lambda: not A.busy())
A.edit_memory({"remove_ignore": "/a/b", "remove_allow": "Bash: sudo du"})
assert A.memory()["ignore"] == [] and A.memory()["allow"] == []
A.reset()
assert A.state()["events"] == [] and not A.ST["proc"]
print("assistant OK")
