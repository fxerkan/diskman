"""AI asistan: Claude Code'u (claude -p, stream-json) alt süreç olarak çalıştırır.
`clauded` gibi izin atlama modunda (--dangerously-skip-permissions) çalışır; yalnız ASK listesindeki istisnai işlemler
arayüze sorulur. "Always allow" ile verilenler ve kullanıcının "bir daha önerme" dedikleri DiskMan ayarında kalıcıdır."""
import json, os, re, shlex, shutil, subprocess, sys, threading, time, uuid

import cleaner as C

HOME = os.path.expanduser("~")
LOCK = threading.Lock()
ST = {"proc": None, "events": [], "pending": {}, "busy": False, "session": None, "path": None, "gen": 0, "account": None,
      "mem": None}
MAX_OUT = 6000  # araç çıktısı arayüzde bu kadar karakter
# istisnai işlemler: bypass modunda bile sorulur (Claude Code'da ask kuralları izin atlamayı geçersiz kılar)
ASK_BASH = ["sudo", "su", "diskutil", "tmutil", "csrutil", "launchctl", "defaults write", "defaults delete", "security",
            "networksetup", "systemsetup", "pmset", "nvram", "dd", "mkfs", "fdisk", "chown", "chmod", "osascript",
            "crontab", "kill", "killall", "pkill", "shutdown", "reboot", "git push", "git reset", "git clean",
            "docker volume rm", "docker volume prune", "docker system prune --volumes", "brew uninstall", "ssh", "scp"]
ASK_OTHER = [f"{t}({p})" for t in ("Edit", "Write") for p in ("~/.ssh/**", "~/.claude/**", "~/.zshrc", "~/.bashrc",
                                                               "~/.config/**", "/etc/**", "/Library/**")]


def find_claude():
    # servis (launchd/systemd) PATH'i kısa olabilir → bilinen kurulum yerleri
    return shutil.which("claude") or next((p for p in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                                                       os.path.join(HOME, ".local/bin/claude"),
                                                       os.path.join(HOME, ".claude/local/claude"))
                                           if os.path.exists(p)), None)


# ---------- kalıcı hafıza: always-allow kuralları + yok sayılanlar (DiskMan ayar dosyasında) ----------
def memory():
    c = C.load_conf()
    return {"allow": c.get("ai_allow", []), "ignore": c.get("ai_ignore", []), "ask": ASK_BASH + ASK_OTHER}


def _save(key, val):
    c = C.load_conf()
    c[key] = val
    C.save_conf(c)


def edit_memory(b):
    """Arayüz ve Claude (curl ile) kullanır: add_ignore [{what, note}], remove_ignore what, remove_allow rule."""
    m = memory()
    if b.get("add_ignore"):
        seen = {x["what"] for x in m["ignore"]}
        for x in b["add_ignore"]:
            what = str(x.get("what", "")).strip()
            if what and what not in seen:
                seen.add(what)
                m["ignore"].append({"what": what, "note": str(x.get("note", ""))[:300], "at": time.strftime("%F")})
        _save("ai_ignore", m["ignore"])
    if b.get("remove_ignore"):
        _save("ai_ignore", [x for x in m["ignore"] if x["what"] != b["remove_ignore"]])
    if b.get("remove_allow"):
        _save("ai_allow", [r for r in m["allow"] if r != b["remove_allow"]])
    return memory()


def _ignore_text(ign):
    return "\n".join(f"- {x['what']}" + (f" ({x['note']})" if x.get("note") else "") for x in ign) or "- (empty)"


_SPLIT = re.compile(r"&&|\|\||;|\||\n")


def _risky(cmd):
    """Komuttaki istisnai parçalar → her biri için kural anahtarı: ask öneki + bir sonraki kelime ("sudo du")."""
    out = []
    for seg in (s.strip() for s in _SPLIT.split(cmd)):
        try:
            tok = shlex.split(seg)
        except ValueError:
            tok = seg.split()
        while tok and "=" in tok[0] and not tok[0].startswith("="):  # FOO=1 cmd
            tok = tok[1:]
        if tok and tok[0] in ("command", "rtk", "env", "nohup", "time"):  # sarmalayıcılar
            tok = tok[1:]
        for a in ASK_BASH:
            n = len(a.split())
            if tok[:n] == a.split():
                out.append(" ".join(tok[:n + 1]))
                break
    return out


def rule_for(r):
    """Always allow için saklanacak kural(lar)."""
    i = r.get("input", {})
    if r["tool_name"] == "Bash":
        return [f"Bash: {k}" for k in _risky(i.get("command", ""))] or [f"Bash: {i.get('command', '')}"]
    return [f"{r['tool_name']}: {i.get('file_path') or i.get('path') or i.get('url') or ''}"]


def allowed(r, allow):
    return all(k in allow for k in rule_for(r))


def system_prompt(port):
    return f"""You are the AI assistant inside DiskMan, a disk analysis and cleanup app on the user's computer ({sys.platform}).
The user chats with you from DiskMan's web UI. Tools run without asking, except a few exceptional operations
(sudo, system settings, kill, git push, ...) that DiskMan shows to the user for approval.
Help the user analyze disk usage and clean up safely:
- Measure before suggesting (du -sh, find -size, docker system df, ...). Say what each item is and whether it is safe to delete.
- Do NOT delete on your own initiative: list what you found; the user picks and deletes in DiskMan. Only delete or run
  cleanup commands when the user asks you to in a message.
- When asked to clean, prefer tool-native cleanup (docker prune, xcrun simctl delete unavailable, sdkmanager --uninstall,
  brew cleanup) and moving to the Trash over rm -rf.
- Never touch cloud-synced folders (Library/CloudStorage, iCloud) or personal documents without explicit confirmation.
- Answer in the user's language, briefly; use markdown tables for overviews.
- Whenever you present items the user could delete, ALSO append exactly one fenced code block with the language
  `diskman-items` holding a JSON array, one object per item:
  {{"path": "/absolute/path", "size": <bytes, integer>, "what": "short description", "safe": "yes" | "maybe" | "no",
   "cmd": "optional: a cleanup command to run instead of deleting the path, e.g. sdkmanager --uninstall \\"ndk;27.0.12077973\\""}}
  Only include paths that exist. DiskMan renders it as a checklist where the user selects items to delete or ignore.
- Ignore list: never suggest, measure or check anything on it. When the user says not to suggest or check something
  again, add it so it persists across chats:
  curl -s -X POST -H 'X-DM: 1' -H 'Content-Type: application/json' http://127.0.0.1:{port}/api/ai/memory \\
       -d '{{"add_ignore": [{{"what": "/absolute/path or a short rule", "note": "why"}}]}}'
Current ignore list:
{_ignore_text(memory()["ignore"])}
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


def _answer(p, rid, res):
    _write(p, {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": res}})


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
                r, rid = o["request"], o["request_id"]
                ev = {"k": "ask", "id": rid, "name": r.get("display_name") or r["tool_name"], "input": r.get("input", {}),
                      "desc": r.get("description", ""), "rules": rule_for(r), "state": "pending"}
                if allowed(r, memory()["allow"]):  # daha önce "Always allow" denmiş
                    _answer(p, rid, {"behavior": "allow", "updatedInput": r.get("input", {})})
                    ev["state"] = "auto"
                else:
                    ST["pending"][rid] = r
                _add(ev)
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
    # clauded ile aynı: --dangerously-skip-permissions; yalnız ASK kuralları --permission-prompt-tool stdio ile arayüze gelir
    settings = {"permissions": {"ask": [f"Bash({a}:*)" for a in ASK_BASH] + ASK_OTHER}}
    cmd = [exe, "-p", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions", "--permission-prompt-tool", "stdio", "--add-dir", "/",
           "--settings", json.dumps(settings), "--append-system-prompt", system_prompt(port)]
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
    ST["mem"] = json.dumps(memory()["ignore"])  # sistem istemine giren liste
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
        ign = memory()["ignore"]
        if json.dumps(ign) != ST["mem"]:  # arayüzden değişti → Claude da bilsin
            ST["mem"] = json.dumps(ign)
            ctx += f"[DiskMan ignore list is now:\n{_ignore_text(ign)}]\n"
        _write(p, {"type": "user", "message": {"role": "user", "content": ctx + text},
                   "parent_tool_use_id": None, "session_id": ""})
    return {"ok": 1}


def permit(rid, decision):
    """decision: allow | always (kalıcı allowlist'e yazılır) | deny"""
    with LOCK:
        r, p = ST["pending"].pop(rid, None), ST["proc"]
        if not r or not p:
            raise ValueError("this request is no longer waiting")
        if decision == "deny":
            _answer(p, rid, {"behavior": "deny", "message": "The user denied this in DiskMan."})
        else:
            if decision == "always":
                allow = memory()["allow"]
                _save("ai_allow", allow + [k for k in rule_for(r) if k not in allow])
            _answer(p, rid, {"behavior": "allow", "updatedInput": r.get("input", {})})
        for e in ST["events"]:
            if e.get("id") == rid and e["k"] == "ask":
                e["state"] = decision
    return {"ok": 1}


def stop():
    with LOCK:
        p = ST["proc"]
        if p:
            for rid in list(ST["pending"]):  # bekleyen izinler reddedilmiş sayılır
                _answer(p, rid, {"behavior": "deny", "message": "Stopped.", "interrupt": True})
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
