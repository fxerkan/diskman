"""AI asistan: Claude Code'u (claude -p, stream-json) alt süreç olarak çalıştırır.
Her şeyi Claude Code yapar; izin istekleri (can_use_tool) arayüze düşer, kullanıcı onaylar ya da reddeder."""
import json, os, shutil, subprocess, sys, threading, uuid

HOME = os.path.expanduser("~")
LOCK = threading.Lock()
ST = {"proc": None, "events": [], "pending": {}, "busy": False, "session": None, "path": None, "gen": 0, "account": None}
MAX_OUT = 6000  # araç çıktısı arayüzde bu kadar karakter


def find_claude():
    # servis (launchd/systemd) PATH'i kısa olabilir → bilinen kurulum yerleri
    return shutil.which("claude") or next((p for p in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                                                       os.path.join(HOME, ".local/bin/claude"),
                                                       os.path.join(HOME, ".claude/local/claude"))
                                           if os.path.exists(p)), None)


def system_prompt(port):
    return f"""You are the AI assistant inside DiskMan, a disk analysis and cleanup app on the user's computer ({sys.platform}).
The user chats with you from DiskMan's web UI; every tool call that needs permission is shown there for approval.
Help the user analyze disk usage and clean up safely:
- Measure before suggesting (du -sh, find -size, docker system df, ...). Say what each item is and whether it is safe to delete.
- Prefer tool-native cleanup (docker prune, xcrun simctl delete unavailable, sdkmanager --uninstall, npm cache clean,
  brew cleanup) and moving to the Trash over rm -rf. Never delete before listing what goes and how much space it frees.
- Never touch cloud-synced folders (Library/CloudStorage, iCloud) or personal documents without explicit confirmation.
- Answer in the user's language, briefly; use markdown tables for lists of items.
DiskMan's own API (header X-DM: 1): http://127.0.0.1:{port}/api/big?min=500 (large files from the last scan),
/api/suggest (cleanup suggestions), /api/docker (Docker usage). The user's current DiskMan folder is given in brackets."""


def _add(ev):
    ev["i"] = len(ST["events"])
    ST["events"].append(ev)
    return ev


def _text(c):
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")


def _write(p, obj):
    try:
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
    except (OSError, ValueError):
        pass


def _reader(p, gen):
    for line in p.stdout:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = o.get("type")
        with LOCK:
            if gen != ST["gen"]:  # yeni sohbet başladı; eski sürecin çıktısını at
                return
            if t == "system" and o.get("subtype") == "init":
                ST["session"] = o.get("session_id")
            elif t == "assistant":
                for b in o["message"].get("content", []):
                    if b.get("type") == "text" and b["text"].strip():
                        _add({"k": "text", "text": b["text"]})
                    elif b.get("type") == "tool_use":
                        _add({"k": "tool", "id": b["id"], "name": b["name"], "input": b.get("input", {})})
            elif t == "user" and not o.get("parent_tool_use_id"):
                for b in o["message"].get("content", []) if isinstance(o["message"].get("content"), list) else []:
                    if b.get("type") == "tool_result":
                        out = _text(b.get("content"))
                        _add({"k": "out", "id": b["tool_use_id"], "err": bool(b.get("is_error")),
                              "text": out if len(out) <= MAX_OUT else out[:MAX_OUT] + f"\n… ({len(out) - MAX_OUT} more chars)"})
            elif t == "control_request" and o["request"].get("subtype") == "can_use_tool":
                r = o["request"]
                ST["pending"][o["request_id"]] = r
                _add({"k": "ask", "id": o["request_id"], "name": r.get("display_name") or r["tool_name"],
                      "input": r.get("input", {}), "desc": r.get("description", ""), "state": "pending"})
            elif t == "result":
                ST["busy"] = False
                _add({"k": "done", "cost": o.get("total_cost_usd"), "err": o.get("is_error") and _text(o.get("result"))})
    with LOCK:
        if gen == ST["gen"]:
            code = p.wait()
            if ST["busy"] or code:
                _add({"k": "err", "text": f"Claude Code exited (code {code}). {p.stderr.read()[-800:]}".strip()})
            ST.update(proc=None, busy=False, pending={})


def subscription_env():
    """API anahtarı / Bedrock / Vertex değişkenleri atılır → Claude Code kullanıcının Claude.ai aboneliğiyle çalışır."""
    drop = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE")
    return {k: v for k, v in os.environ.items() if k not in drop}


def account(exe, env):
    try:
        return json.loads(subprocess.run([exe, "auth", "status"], env=env, capture_output=True, text=True,
                                         timeout=20).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _spawn(port):
    exe = find_claude()
    if not exe:
        raise RuntimeError("Claude Code (claude) not found: install it from https://claude.com/claude-code")
    # manual: her yazma/çalıştırma izni arayüze sorulur; okuma (du, ls, Read) Claude Code'un kendi kurallarıyla serbest
    cmd = [exe, "-p", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
           "--permission-mode", "manual", "--permission-prompt-tool", "stdio", "--add-dir", "/",
           "--allowedTools", "Bash(du:*)", "Bash(df:*)", "Bash(docker system df:*)", "Bash(xcrun simctl list:*)",
           "--append-system-prompt", system_prompt(port)]
    if ST["session"]:  # süreç kapandıysa aynı sohbetten devam
        cmd += ["--resume", ST["session"]]
    env = subscription_env()
    auth = account(exe, env)
    if auth.get("authMethod") != "claude.ai":
        raise RuntimeError("Claude Code is not logged in with a Claude subscription: run `claude auth login` "
                           "in a terminal and choose your Claude.ai account (no API key is used)")
    ST["account"] = auth.get("email")
    p = subprocess.Popen(cmd, cwd=HOME, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1, start_new_session=True)
    _write(p, {"type": "control_request", "request_id": uuid.uuid4().hex, "request": {"subtype": "initialize"}})
    ST["proc"] = p
    threading.Thread(target=_reader, args=(p, ST["gen"]), daemon=True).start()
    return p


def send(text, path, port):
    text = str(text).strip()
    if not text:
        raise ValueError("empty message")
    with LOCK:
        if ST["busy"]:
            raise ValueError("Claude is still working: wait or press Stop")
        p = ST["proc"] or _spawn(port)
        _add({"k": "user", "text": text})
        ST["busy"] = True
        ctx = f"[DiskMan folder: {path}]\n" if path and path != ST["path"] else ""
        ST["path"] = path or ST["path"]
        _write(p, {"type": "user", "message": {"role": "user", "content": ctx + text},
                   "parent_tool_use_id": None, "session_id": ""})
    return {"ok": 1}


def permit(rid, decision):
    """decision: allow | always (bu sohbet boyunca) | deny"""
    with LOCK:
        r, p = ST["pending"].pop(rid, None), ST["proc"]
        if not r or not p:
            raise ValueError("this request is no longer waiting")
        if decision == "deny":
            res = {"behavior": "deny", "message": "The user denied this in DiskMan."}
        else:
            res = {"behavior": "allow", "updatedInput": r.get("input", {})}
            if decision == "always":  # yalnız bu oturum; ayar dosyalarına yazılmaz
                res["updatedPermissions"] = [{**s, "destination": "session"} for s in r.get("permission_suggestions") or []]
        _write(p, {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": res}})
        for e in ST["events"]:
            if e.get("id") == rid and e["k"] == "ask":
                e["state"] = decision
    return {"ok": 1}


def stop():
    with LOCK:
        p = ST["proc"]
        if p:
            for rid in list(ST["pending"]):  # bekleyen izinler reddedilmiş sayılır
                _write(p, {"type": "control_response", "response": {"subtype": "success", "request_id": rid,
                                                                    "response": {"behavior": "deny", "message": "Stopped.",
                                                                                 "interrupt": True}}})
            ST["pending"].clear()
            for e in ST["events"]:
                if e["k"] == "ask" and e["state"] == "pending":
                    e["state"] = "deny"
            _write(p, {"type": "control_request", "request_id": uuid.uuid4().hex, "request": {"subtype": "interrupt"}})
    return {"ok": 1}


def reset():
    with LOCK:
        p = ST["proc"]
        ST.update(proc=None, events=[], pending={}, busy=False, session=None, path=None, gen=ST["gen"] + 1)
    if p:
        p.kill()
    return {"ok": 1}


def state(since=0):
    with LOCK:
        ev = ST["events"]
        # izin kartları sonradan değişir (pending → allow): bekleyenleri her seferinde yeniden gönder
        return {"events": ev[since:], "asks": [e for e in ev[:since] if e["k"] == "ask"], "n": len(ev),
                "busy": ST["busy"], "pending": len(ST["pending"]), "available": bool(find_claude()),
                "account": ST["account"]}


def busy():
    return ST["busy"]
