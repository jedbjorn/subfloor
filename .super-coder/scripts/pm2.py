#!/usr/bin/env python3
"""Host pm2 — config read/write + live setup checks + the host-side loop verbs.

Link-only by design: the sandbox never gets a pm2 binary or a route to the
host's ports. The operator brings a host whose app stack is already pm2-managed
(`make deploy` or equivalent, host-side). These checks validate that the
operator-supplied `pm2` block matches reality BEFORE it is saved to
instance.json — and the loop verbs (status / app-health / logs / restart) then
drive that stack through the host's pm2.

The config lives under the `pm2` key of `.super-coder/instance.json` (so there
is no schema migration — the process stack is a host resource, not shell
state). It holds no secret material; the block only configures *use*:

    processes        the pm2 process names this fork may see + act on
                     (fail-closed scoping — empty/absent denies every verb)
    health_url       the app's local health endpoint, curled HOST-side
                     (the sandbox has no route to 127.0.0.1-bound host ports)
    pm2_bin          path/name of the pm2 CLI (default "pm2")
    allow_lifecycle  gate for stop/start (default false; restart is always
                     allowed for allowlisted processes — it is the deploy verb)

Like the tailnet (N hosts), a pm2 daemon supervises N processes — so the verbs
are parameterized by `{proc}` and `processes` fail-closes them to a declared
set: a compromised sandbox can only see + bounce what the fork has declared,
and cannot enumerate the host's full process list. pm2 + the app stay on the
HOST; the sandbox names verbs over the broker's unix socket and holds nothing.
The broker (api/pm2_broker.py) is the third sibling of the Windows VM + tailnet
brokers; see .super-coder/docs/pm2-broker.md.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import ports

CHECKS = ("daemon", "procs", "health")

# The broker listens here — a unix socket inside the bind-mounted engine dir, so
# the same absolute path resolves on the host (where the broker runs) and in the
# sandbox (where the pm2 skill curls it). No network surface; fs-perm gated.
# Distinct filename from vm-broker.sock / ts-broker.sock so all three coexist.
RUN_DIR = ports.ENGINE / "run"
SOCKET = RUN_DIR / "pm2-broker.sock"

LIFECYCLE_ALWAYS = ("restart",)          # allowlisted procs only
LIFECYCLE_GATED = ("stop", "start")      # + allow_lifecycle: true


# -- config (instance.json `pm2` block) ---------------------------------------

def read() -> dict | None:
    """The persisted pm2 block, or None if the fork has not linked its stack."""
    return ports.resolve(persist=False).get("pm2")


def write(pm2: dict | None) -> dict | None:
    """Persist (or clear) the pm2 block, preserving every other config key
    (ports, and the `vm` / `ts` blocks — all coexist)."""
    cfg = ports.resolve(persist=False)
    if pm2:
        cfg["pm2"] = pm2
    else:
        cfg.pop("pm2", None)
    ports.save(cfg)
    return cfg.get("pm2")


# -- primitives ---------------------------------------------------------------

def _run(argv: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except FileNotFoundError as e:
        return False, f"command not found: {e.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return False, f"timed out (>{timeout}s)"


def _bin(cfg: dict) -> str:
    return str(cfg.get("pm2_bin") or "pm2")


def _denied(cfg: dict, proc: str) -> str | None:
    """Fail-closed scoping. `processes` is the set a fork may see + act on;
    empty/absent denies all, so an admin shell must declare its stack and a
    compromised sandbox cannot enumerate or touch arbitrary host processes."""
    allowed = cfg.get("processes") or []
    if not allowed:
        return ("no processes in the `pm2` block — every verb is denied until "
                "you declare the pm2 process names this fork may manage.")
    if proc not in allowed:
        return f"process '{proc}' is not in processes {allowed}"
    return None


def _jlist(cfg: dict, timeout: int = 20) -> tuple[bool, list | str]:
    """Run `pm2 jlist` once and parse it. Returns (True, list) or
    (False, error-string)."""
    ok, out = _run([_bin(cfg), "jlist"], timeout=timeout)
    if not ok:
        return False, out
    # pm2 may prefix jlist with daemon-boot chatter ("[PM2] Spawning…"); the
    # payload is the JSON array that starts at some later line. Try each
    # line-start candidate — banner lines fail to parse, the array succeeds.
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            data = json.loads("\n".join(lines[i:]))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return True, data
    return False, f"could not parse `pm2 jlist`: {out[:200]}"


def _proc_row(p: dict) -> dict:
    """The status shape the pm2 skill depends on, from one jlist entry."""
    env = p.get("pm2_env") or {}
    monit = p.get("monit") or {}
    up_ms = env.get("pm_uptime")
    online = env.get("status") == "online"
    uptime_s = int(max(0, time.time() - up_ms / 1000)) if (online and up_ms) else None
    return {
        "name": p.get("name"),
        "status": env.get("status"),
        "pid": p.get("pid") or None,
        "uptime_s": uptime_s,
        "restarts": env.get("restart_time"),
        "cpu": monit.get("cpu"),
        "memory": monit.get("memory"),
    }


def _tail(path: str | None, lines: int) -> str:
    """Last `lines` lines of a pm2 log file — read host-side, never streamed
    (`pm2 logs` streams forever; a broker verb must return)."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return b"".join(f.readlines()[-lines:]).decode("utf-8", "replace")
    except OSError as e:
        return f"(unreadable: {e})"


# -- the checks (validate a CANDIDATE block before save) ----------------------

def _check_daemon(cfg: dict) -> tuple[bool, str]:
    ok, data = _jlist(cfg)
    if not ok:
        return False, f"`pm2 jlist` failed — is pm2 installed + its daemon up on the host?\n{data}"
    return True, f"pm2 daemon responds ({len(data)} process(es) supervised)."


def _check_procs(cfg: dict) -> tuple[bool, str]:
    allowed = cfg.get("processes") or []
    if not allowed:
        return False, "no processes configured — nothing to resolve."
    ok, data = _jlist(cfg)
    if not ok:
        return False, str(data)
    names = {p.get("name") for p in data}
    missing = [n for n in allowed if n not in names]
    if missing:
        return False, (f"processes not found under pm2: {missing}. "
                       f"Supervised: {sorted(n for n in names if n)}")
    return True, f"all declared processes are supervised by pm2: {allowed}"


def _check_health(cfg: dict) -> tuple[bool, str]:
    url = str(cfg.get("health_url") or "").strip()
    if not url:
        return False, "no health_url configured — nothing to probe."
    r = _fetch(url)
    if r["ok"]:
        return True, f"health_url responds {r['code']}: {r['body'][:120]}"
    return False, f"health_url unreachable: {r['error'] if 'error' in r else r['code']}"


_CHECKS = {
    "daemon": _check_daemon,
    "procs": _check_procs,
    "health": _check_health,
}


def validate(check: str, cfg: dict) -> dict | None:
    """Run one live check against the CANDIDATE config in `cfg` (the in-progress
    form, not necessarily what is saved). Returns {ok, output, check} or None for
    an unknown check name (→ 404 at the API layer)."""
    fn = _CHECKS.get(check)
    if fn is None:
        return None
    ok, out = fn(cfg or {})
    return {"ok": ok, "output": out or "(no output)", "check": check}


# -- the loop verbs (host-side; the broker exposes these over the socket) -----
#
# Verbs operate on the SAVED `pm2` block. (validate() above is the exception:
# it tests a CANDIDATE block passed in, before it is saved.)

def do_status() -> dict:
    """Summarize the DECLARED processes from `pm2 jlist` — never the host's full
    list. Configured-but-unsupervised names surface under `missing`."""
    cfg = read() or {}
    allowed = cfg.get("processes") or []
    if not allowed:
        return {"ok": False, "output": _denied(cfg, "")}
    ok, data = _jlist(cfg)
    if not ok:
        return {"ok": False, "output": str(data)}
    rows = [_proc_row(p) for p in data if p.get("name") in allowed]
    seen = {r["name"] for r in rows}
    return {"ok": True, "processes": rows,
            "missing": [n for n in allowed if n not in seen]}


def _fetch(url: str, timeout: int = 10) -> dict:
    """GET the app's health URL — from the HOST, where 127.0.0.1-bound ports
    resolve. This is the whole point: the sandbox has no route to them."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "code": resp.status, "body": body}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code, "body": e.read(2048).decode("utf-8", "replace")}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "code": None, "body": "", "error": str(e)}


def do_health() -> dict:
    """Curl the saved health_url host-side. Returns {ok, code, body}."""
    cfg = read() or {}
    url = str(cfg.get("health_url") or "").strip()
    if not url:
        return {"ok": False, "code": None, "body": "",
                "error": "no health_url in the `pm2` block"}
    return _fetch(url)


def do_logs(proc: str, lines: int = 100) -> dict:
    """Tail one declared process's out+err logs (paths from jlist). Capped —
    a broker verb returns, it never streams."""
    cfg = read() or {}
    if not str(proc).strip():
        return {"ok": False, "output": "logs: no process named"}
    if denied := _denied(cfg, proc):
        return {"ok": False, "output": denied}
    ok, data = _jlist(cfg)
    if not ok:
        return {"ok": False, "output": str(data)}
    entry = next((p for p in data if p.get("name") == proc), None)
    if entry is None:
        return {"ok": False, "output": f"process '{proc}' is not supervised by pm2"}
    env = entry.get("pm2_env") or {}
    lines = max(1, min(int(lines or 100), 1000))
    return {"ok": True, "proc": proc, "lines": lines,
            "out": _tail(env.get("pm_out_log_path"), lines),
            "err": _tail(env.get("pm_err_log_path"), lines)}


def do_lifecycle(action: str, proc: str, timeout: int = 60) -> dict:
    """restart/stop/start one declared process. `restart` is the deploy verb
    and rides the allowlist alone; stop/start additionally need
    `allow_lifecycle: true` (a stopped app is an outage — opt into that
    surface explicitly). Returns {ok, exit, stdout, stderr}."""
    cfg = read() or {}
    if action not in LIFECYCLE_ALWAYS + LIFECYCLE_GATED:
        return {"ok": False, "exit": -1, "stdout": "",
                "stderr": f"unknown action '{action}'"}
    if not str(proc).strip():
        return {"ok": False, "exit": -1, "stdout": "", "stderr": f"{action}: no process named"}
    if denied := _denied(cfg, proc):
        return {"ok": False, "exit": -1, "stdout": "", "stderr": denied}
    if action in LIFECYCLE_GATED and not cfg.get("allow_lifecycle"):
        return {"ok": False, "exit": -1, "stdout": "",
                "stderr": (f"{action} is gated — set \"allow_lifecycle\": true in the "
                           "`pm2` block to allow stop/start from the sandbox.")}
    try:
        p = subprocess.run([_bin(cfg), action, proc], capture_output=True,
                           text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "exit": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except FileNotFoundError as e:
        return {"ok": False, "exit": 127, "stdout": "",
                "stderr": f"command not found: {e.filename} — is pm2 installed on the host?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": 124, "stdout": "", "stderr": f"timed out (>{timeout}s)"}


# -- client: HTTP over the broker's unix socket -------------------------------

def broker_call(method: str, path: str, body: dict | None = None,
                timeout: int = 70) -> dict:
    """Speak HTTP/1.1 to the broker over its unix socket and return parsed JSON.
    Raises ConnectionError if the broker is not listening (so callers can render
    a 'start the broker' hint). Used by the in-sandbox server to proxy verbs."""
    payload = b"" if body is None else json.dumps(body).encode()
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: pm2-broker\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + payload
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        s.connect(str(SOCKET))
        s.sendall(req)
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise ConnectionError(f"pm2-broker not reachable at {SOCKET}: {e}") from e
    finally:
        s.close()
    _, _, raw_body = b"".join(chunks).partition(b"\r\n\r\n")
    try:
        return json.loads(raw_body.decode() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad broker response",
                "raw": raw_body[:200].decode("latin1")}


# -- host CLI (path lookup for `sc`; verbs for manual no-broker testing) -------

def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "sock"
    if mode == "sock":
        print(SOCKET)
    elif mode == "configured":
        # exit 0 if this fork has linked its stack (so the launch hook self-skips)
        return 0 if read() else 1
    elif mode == "status":
        print(json.dumps(do_status()))
    elif mode == "health":
        print(json.dumps(do_health()))
    elif mode == "logs":
        # pm2.py logs <proc> [lines]
        proc = argv[1] if len(argv) > 1 else ""
        print(json.dumps(do_logs(proc, int(argv[2]) if len(argv) > 2 else 100)))
    elif mode in LIFECYCLE_ALWAYS + LIFECYCLE_GATED:
        print(json.dumps(do_lifecycle(mode, argv[1] if len(argv) > 1 else "")))
    elif mode == "validate":
        print(json.dumps(validate(argv[1] if len(argv) > 1 else "", read() or {})))
    else:
        sys.exit("usage: pm2.py [sock|configured|status|health|logs <proc> [n]|"
                 "restart|stop|start <proc>|validate <check>]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
