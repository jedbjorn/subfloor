#!/usr/bin/env python3
"""Tailnet — config read/write + live connection checks + the host-side loop verbs.

Link-only by design: the engine never joins the tailnet. The operator brings a
host that is already `tailscale up` (authenticated once, host-side). These checks
validate that the operator-supplied `ts` block actually reaches a logged-in node
that can see the named hosts BEFORE it is saved to instance.json — and the loop
verbs (status / exec) then drive the tailnet through that host node.

The config lives under the `ts` key of `.super-coder/instance.json` (so there is
no schema migration — the tailnet is a host resource, not shell state). It holds
NO secret material: the host node's identity is the credential, and it stays on
the host. The block only configures *use*:

    ssh_user       remote user for `tailscale ssh` (user@host)
    allowed_hosts  the tailnet hosts this fork may exec against (scoping policy)
    tailscale_bin  path/name of the tailscale CLI (default "tailscale")

Unlike the Windows VM (one fixed target), a tailnet has N hosts — so the loop
verbs are parameterized by `{host, command}`, not a single saved target, and
`allowed_hosts` fail-closes exec to a declared set so a compromised sandbox can't
reach arbitrary nodes. tailscaled + the tailnet identity stay on the HOST; the
sandbox names verbs over the broker's unix socket and holds nothing. The broker
(api/ts_broker.py) is the sibling of the Windows VM broker; see
.super-coder/docs/tailscale-broker.md.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import ports

CHECKS = ("daemon", "auth", "peer", "ssh")

# The broker listens here — a unix socket inside the bind-mounted engine dir, so
# the same absolute path resolves on the host (where the broker runs) and in the
# sandbox (where the tailscale skill curls it). No network surface; fs-perm gated.
# Distinct filename from vm-broker.sock so the two brokers coexist per fork.
RUN_DIR = ports.ENGINE / "run"
SOCKET = RUN_DIR / "ts-broker.sock"


# -- config (instance.json `ts` block) ---------------------------------------

def read() -> dict | None:
    """The persisted ts block, or None if the fork has not linked a tailnet."""
    return ports.resolve(persist=False).get("ts")


def write(ts: dict | None) -> dict | None:
    """Persist (or clear) the ts block, preserving every other config key
    (ports, and the `vm` block written by vm.py — both coexist)."""
    cfg = ports.resolve(persist=False)
    if ts:
        cfg["ts"] = ts
    else:
        cfg.pop("ts", None)
    ports.save(cfg)
    return cfg.get("ts")


# -- primitives --------------------------------------------------------------

def _run(argv: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except FileNotFoundError as e:
        return False, f"command not found: {e.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return False, f"timed out (>{timeout}s)"


def _bin(cfg: dict) -> str:
    return str(cfg.get("tailscale_bin") or "tailscale")


def _ts(cfg: dict, *args: str) -> list[str]:
    """A tailscale CLI argv (e.g. `tailscale status --json`)."""
    return [_bin(cfg), *args]


def _ssh_argv(cfg: dict, host: str, remote: str) -> list[str]:
    """A `tailscale ssh [user@]host <command>` invocation. Auth is the host
    node's tailnet identity governed by the tailnet's SSH ACLs — no key, no
    password prompt, so it stays non-interactive without BatchMode."""
    user = str(cfg.get("ssh_user", "")).strip()
    target = f"{user}@{host}" if user else host
    return [_bin(cfg), "ssh", target, remote]


def _denied(cfg: dict, host: str) -> str | None:
    """Fail-closed scoping. `allowed_hosts` is the set a fork may exec against;
    empty/absent denies all, so a devops shell must declare its targets and a
    compromised sandbox cannot reach arbitrary tailnet nodes."""
    allowed = cfg.get("allowed_hosts") or []
    if not allowed:
        return ("no allowed_hosts in the `ts` block — exec is denied until you "
                "declare the tailnet hosts this fork may reach.")
    if host not in allowed:
        return f"host '{host}' is not in allowed_hosts {allowed}"
    return None


def _status_json(cfg: dict, timeout: int = 15) -> tuple[bool, dict | str]:
    """Run `tailscale status --json` once and parse it. Returns (True, data) or
    (False, error-string)."""
    ok, out = _run(_ts(cfg, "status", "--json"), timeout=timeout)
    if not ok:
        return False, out
    try:
        return True, json.loads(out)
    except json.JSONDecodeError:
        return False, f"could not parse `tailscale status --json`: {out[:200]}"


def _peer_names(data: dict) -> set[str]:
    """Hostnames + short MagicDNS labels of every visible peer."""
    names: set[str] = set()
    for p in (data.get("Peer") or {}).values():
        if p.get("HostName"):
            names.add(p["HostName"])
        dns = (p.get("DNSName") or "").rstrip(".")
        if dns:
            names.add(dns.split(".")[0])
    return names


# -- the checks (validate a CANDIDATE block before save) ---------------------

def _check_daemon(cfg: dict) -> tuple[bool, str]:
    ok, _ = _status_json(cfg)
    if not ok:
        return False, "`tailscale status` failed — is tailscaled running on the host?"
    return True, "tailscaled is up and `tailscale status` responds."


def _check_auth(cfg: dict) -> tuple[bool, str]:
    ok, data = _status_json(cfg)
    if not ok:
        return False, str(data)
    state = data.get("BackendState")
    if state == "Running":
        return True, "node is logged in to the tailnet (BackendState=Running)."
    return False, (f"node is not on the tailnet (BackendState={state}) — run "
                   "`tailscale up` on the host once.")


def _check_peer(cfg: dict) -> tuple[bool, str]:
    allowed = cfg.get("allowed_hosts") or []
    if not allowed:
        return False, "no allowed_hosts configured — nothing to resolve."
    ok, data = _status_json(cfg)
    if not ok:
        return False, str(data)
    names = _peer_names(data)
    missing = [h for h in allowed if h not in names]
    if missing:
        return False, (f"allowed_hosts not found in the tailnet: {missing}. "
                       f"Visible peers: {sorted(n for n in names if n)}")
    return True, f"all allowed_hosts resolve as tailnet peers: {allowed}"


def _check_ssh(cfg: dict) -> tuple[bool, str]:
    allowed = cfg.get("allowed_hosts") or []
    if not allowed:
        return False, "no allowed_hosts configured — nothing to probe."
    host = allowed[0]
    ok, out = _run(_ssh_argv(cfg, host, "echo ok"), timeout=20)
    if ok:
        return True, f"tailscale ssh to '{host}' works: {out or 'ok'}"
    return False, (f"tailscale ssh to '{host}' failed — check the tailnet SSH "
                   f"ACLs, the ssh_user, and that Tailscale SSH is enabled.\n{out}")


_CHECKS = {
    "daemon": _check_daemon,
    "auth": _check_auth,
    "peer": _check_peer,
    "ssh": _check_ssh,
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
# Verbs operate on the SAVED `ts` block + a caller-named host. (validate() above
# is the exception: it tests a CANDIDATE block passed in, before it is saved.)

def do_status() -> dict:
    """Summarize the tailnet from the host node's view: backend state, self, and
    every peer (host, MagicDNS name, IP, online)."""
    cfg = read() or {}
    ok, data = _status_json(cfg, timeout=20)
    if not ok:
        return {"ok": False, "output": str(data)}
    self_ = data.get("Self") or {}
    peers = [
        {"host": p.get("HostName"),
         "dns": (p.get("DNSName") or "").rstrip("."),
         "ip": (p.get("TailscaleIPs") or [None])[0],
         "online": p.get("Online")}
        for p in (data.get("Peer") or {}).values()
    ]
    return {
        "ok": True,
        "backend": data.get("BackendState"),
        "self": {"host": self_.get("HostName"),
                 "dns": (self_.get("DNSName") or "").rstrip("."),
                 "ip": (self_.get("TailscaleIPs") or [None])[0]},
        "peers": peers,
    }


def do_exec(host: str, command: str, timeout: int = 120) -> dict:
    """Run one command on a tailnet host over `tailscale ssh`. Scoped by
    allowed_hosts (fail-closed). Returns {ok, exit, stdout, stderr}."""
    cfg = read() or {}
    if not str(host).strip():
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "exec: no host named"}
    if not str(command).strip():
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "exec: empty command"}
    if denied := _denied(cfg, host):
        return {"ok": False, "exit": -1, "stdout": "", "stderr": denied}
    try:
        p = subprocess.run(_ssh_argv(cfg, host, command), capture_output=True,
                           text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "exit": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except FileNotFoundError as e:
        return {"ok": False, "exit": 127, "stdout": "",
                "stderr": f"command not found: {e.filename} — is tailscale installed on the host?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": 124, "stdout": "", "stderr": f"timed out (>{timeout}s)"}


# -- client: HTTP over the broker's unix socket ------------------------------

def broker_call(method: str, path: str, body: dict | None = None,
                timeout: int = 130) -> dict:
    """Speak HTTP/1.1 to the broker over its unix socket and return parsed JSON.
    Raises ConnectionError if the broker is not listening (so callers can render
    a 'start the broker' hint). Used by the in-sandbox server to proxy verbs."""
    payload = b"" if body is None else json.dumps(body).encode()
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: ts-broker\r\n"
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
        raise ConnectionError(f"ts-broker not reachable at {SOCKET}: {e}") from e
    finally:
        s.close()
    _, _, raw_body = b"".join(chunks).partition(b"\r\n\r\n")
    try:
        return json.loads(raw_body.decode() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad broker response",
                "raw": raw_body[:200].decode("latin1")}


# -- host CLI (path lookup for `sc`; verbs for manual no-broker testing) ------

def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "sock"
    if mode == "sock":
        print(SOCKET)
    elif mode == "configured":
        # exit 0 if this fork has linked a tailnet (so the launch hook self-skips)
        return 0 if read() else 1
    elif mode == "status":
        print(json.dumps(do_status()))
    elif mode == "exec":
        # ts.py exec <host> <command...>
        host = argv[1] if len(argv) > 1 else ""
        print(json.dumps(do_exec(host, " ".join(argv[2:]))))
    elif mode == "validate":
        print(json.dumps(validate(argv[1] if len(argv) > 1 else "", read() or {})))
    else:
        sys.exit("usage: ts.py [sock|status|exec <host> <cmd>|validate <check>]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
