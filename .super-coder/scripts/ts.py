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

    ssh_user       default remote user for `tailscale ssh` (user@host)
    ssh_users      optional {host: user} overrides of ssh_user, per host
    allowed_hosts  the tailnet hosts this fork may exec against (scoping policy)
    readonly_hosts implicitly allowed hosts restricted to diagnostic commands
    tailscale_bin  path/name of the tailscale CLI (default "tailscale")

Unlike the VM seat (one fixed libvirt domain), a tailnet has N hosts — so the
loop verbs are parameterized by `{host, command}`, not a single saved target, and
exec is refused for any host the block does not declare. That is a rail against
reaching the wrong machine by accident, not containment: what a tailnet node may
actually do to another is decided by the tailnet policy, not by this file.
tailscaled + the tailnet identity stay on the HOST; a sandboxed shell names
verbs over the broker's unix socket. The broker
(api/ts_broker.py) is the sibling of the VM and remotes broker
(api/vm_broker.py); see .super-coder/docs/tailscale-broker.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

import ports

CHECKS = ("daemon", "auth", "peer", "ssh")

# Read-only commands are prefixes: trailing operands remain available for
# selecting a service, container, file, or other diagnostic target. Keeping the
# policy as data makes additions reviewable and lets tests exercise every entry.
READONLY_COMMANDS = (
    ("systemctl", "status"),
    ("systemctl", "is-active"),
    ("systemctl", "list-units"),
    ("systemctl", "list-timers"),
    ("journalctl",),
    ("pm2", "status"),
    ("pm2", "list"),
    ("pm2", "describe"),
    ("pm2", "logs", "--nostream"),
    ("docker", "ps"),
    ("docker", "logs"),
    ("docker", "inspect"),
    ("docker", "stats", "--no-stream"),
    ("docker", "images"),
    ("df",),
    ("free",),
    ("uptime",),
    ("ps",),
    ("ss",),
    ("ip",),
    ("ls",),
    ("stat",),
    ("cat",),
    ("head",),
    ("tail",),
    ("du",),
    ("nproc",),
    ("hostname",),
    ("whoami",),
    ("id",),
)
READONLY_FORBIDDEN_CHARACTERS = (
    ";", "&", "|", ">", "<", "$", "`", "(", ")", "\n", "\r",
)
READONLY_TOKEN_DISPLAY = {"\n": r"\n", "\r": r"\r"}

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


# Host and user names end up as ONE argv element of `tailscale ssh`
# (`user@host`). A name that starts with a dash would be read as an option
# there, so both are held to a conservative shape wherever a block is written:
# a tailnet machine name, MagicDNS name or IPv4 address for the host, a plain
# account name for the user. Neither may start with `-`.
HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?\Z")
USER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")


class BlockInvalid(ValueError):
    """The ts block carries a host or user name that must not be saved."""


def valid_host(value: object) -> bool:
    return isinstance(value, str) and bool(HOST_NAME.fullmatch(value))


def valid_user(value: object) -> bool:
    return isinstance(value, str) and bool(USER_NAME.fullmatch(value))


def block_error(block: dict) -> str | None:
    """Why `block` must not be saved, or None. Applied to EVERY write - the
    client verbs and a block arriving through `PUT /ts` alike."""
    for key in ("allowed_hosts", "readonly_hosts"):
        hosts = block.get(key)
        if hosts is None:
            continue
        if not isinstance(hosts, list):
            return f"{key} must be a list of host names"
        for host in hosts:
            if not valid_host(host):
                return f"{key} entry is not a valid host name: {host!r}"
    user = block.get("ssh_user")
    if user not in (None, "") and not valid_user(user):
        return f"ssh_user is not a valid user name: {user!r}"
    users = block.get("ssh_users")
    if users is not None:
        if not isinstance(users, dict):
            return "ssh_users must be an object of host: user"
        for host, name in users.items():
            if not valid_host(host):
                return f"ssh_users key is not a valid host name: {host!r}"
            if not valid_user(name):
                return f"ssh_users[{host!r}] is not a valid user name: {name!r}"
    return None


def write(ts: dict | None) -> dict | None:
    """Persist (or clear) the ts block, preserving every other config key
    (ports, and the `vm` block written by vm.py — both coexist).

    Two things hold for every block that is saved, whoever sends it: its
    names pass `block_error` (BlockInvalid otherwise, nothing written), and no
    host sits in both tiers - a host in `readonly_hosts` is dropped from
    `allowed_hosts`, which is what exec already treated it as.
    """
    if ts:
        if problem := block_error(ts):
            raise BlockInvalid(problem)
        readonly = ts.get("readonly_hosts") or []
        if readonly and any(h in readonly for h in ts.get("allowed_hosts") or []):
            ts = {**ts, "allowed_hosts": [
                h for h in ts["allowed_hosts"] if h not in readonly]}
        ports.update({"ts": ts})
    else:
        ports.update({}, remove=("ts",))
    return ts


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
    user = _user_for(cfg, host)
    target = f"{user}@{host}" if user else host
    return [_bin(cfg), "ssh", target, remote]


def _user_for(cfg: dict, host: str) -> str:
    """The remote user for one host: its `ssh_users` entry, else `ssh_user`.

    `ssh_users` is optional and additive, so a block written before it existed
    behaves exactly as it did (one user for every host).
    """
    overrides = cfg.get("ssh_users")
    if isinstance(overrides, dict):
        override = str(overrides.get(host) or "").strip()
        if override:
            return override
    return str(cfg.get("ssh_user") or "").strip()


def _declared_hosts(cfg: dict) -> list[str]:
    """Allowed plus read-only hosts, in declaration order without duplicates."""
    return list(dict.fromkeys(
        [*(cfg.get("allowed_hosts") or []), *(cfg.get("readonly_hosts") or [])]
    ))


def _denied(cfg: dict, host: str) -> str | None:
    """Fail closed; readonly_hosts are implicitly allowed and win on overlap."""
    allowed = cfg.get("allowed_hosts") or []
    readonly = cfg.get("readonly_hosts") or []
    if not allowed and not readonly:
        return ("no allowed_hosts or readonly_hosts in the `ts` block — exec is "
                "denied until you declare the tailnet hosts this fork may reach.")
    if host not in allowed and host not in readonly:
        return f"host '{host}' is not in allowed_hosts or readonly_hosts"
    return None


def readonly_refusal(command: str) -> str | None:
    """Return the token that makes a read-only command unsafe, else None."""
    for character in READONLY_FORBIDDEN_CHARACTERS:
        if character in command:
            return READONLY_TOKEN_DISPLAY.get(character, character)
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if "sudo" in tokens:
        return "sudo"
    if not tokens:
        return command
    for prefix in READONLY_COMMANDS:
        if tuple(tokens[:len(prefix)]) == prefix:
            if prefix == ("journalctl",):
                blocked = next(
                    (token for token in tokens[1:]
                     if token.startswith(("--vacuum", "--rotate"))),
                    None,
                )
                if blocked:
                    return blocked
            return None
    known_verbs = {prefix[0] for prefix in READONLY_COMMANDS}
    return tokens[1] if tokens[0] in known_verbs and len(tokens) > 1 else tokens[0]


def _readonly_refused(token: str) -> dict:
    return {
        "ok": False,
        "error": "readonly_refused",
        "token": token,
        "exit": -1,
        "stdout": "",
        "stderr": f"read-only host refused token: {token}",
    }


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


def _scope(cfg: dict, hosts: list[str] | None) -> list[str]:
    """The declared hosts a check covers: all of them, or the named subset."""
    declared = _declared_hosts(cfg)
    if not hosts:
        return declared
    return [host for host in declared if host in hosts]


def _check_peer(cfg: dict, hosts: list[str] | None = None) -> tuple[bool, str]:
    declared = _scope(cfg, hosts)
    if not declared:
        return False, "no allowed_hosts or readonly_hosts configured — nothing to resolve."
    ok, data = _status_json(cfg)
    if not ok:
        return False, str(data)
    names = _peer_names(data)
    missing = [h for h in declared if h not in names]
    if missing:
        return False, (f"declared hosts not found in the tailnet: {missing}. "
                       f"Visible peers: {sorted(n for n in names if n)}")
    return True, f"all declared hosts resolve as tailnet peers: {declared}"


def _check_ssh(cfg: dict, hosts: list[str] | None = None) -> tuple[bool, str]:
    """Probe `tailscale ssh` as each host's own user.

    With no `hosts` the first declared host is probed (the wizard's quick
    check); a link command names the hosts it just declared and every one of
    them is probed, since each may carry its own user.
    """
    declared = _scope(cfg, hosts)
    if not declared:
        return False, "no allowed_hosts or readonly_hosts configured — nothing to probe."
    targets = declared if hosts else declared[:1]
    passed, failed = [], []
    for host in targets:
        ok, out = _run(_ssh_argv(cfg, host, "echo ok"), timeout=20)
        if ok:
            passed.append(f"tailscale ssh to '{host}' works: {out or 'ok'}")
        else:
            user = _user_for(cfg, host) or "(default user)"
            failed.append(
                f"tailscale ssh to '{host}' as {user} failed — check the "
                "tailnet SSH ACLs, the ssh_user, and that Tailscale SSH is "
                f"enabled.\n{out}")
    if failed:
        return False, "\n".join(failed)
    return True, "\n".join(passed)


_CHECKS = {
    "daemon": _check_daemon,
    "auth": _check_auth,
    "peer": _check_peer,
    "ssh": _check_ssh,
}


def validate(check: str, cfg: dict, hosts: list[str] | None = None) -> dict | None:
    """Run one live check against the CANDIDATE config in `cfg` (the in-progress
    form, not necessarily what is saved). Returns {ok, output, check} or None for
    an unknown check name (→ 404 at the API layer). `hosts` narrows the
    per-host checks (peer, ssh) to the named declared hosts."""
    fn = _CHECKS.get(check)
    if fn is None:
        return None
    if check in ("peer", "ssh") and hosts:
        ok, out = fn(cfg or {}, [str(host) for host in hosts])
    else:
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
    # A hand-edited block is not validated on the way in; never build an argv
    # element that could be read as an option.
    user = _user_for(cfg, host)
    if not valid_host(host) or (user and not valid_user(user)):
        return {"ok": False, "exit": -1, "stdout": "",
                "stderr": f"exec: the saved host or user name for '{host}' is "
                          "not a valid name; fix the ts block"}
    if host in (cfg.get("readonly_hosts") or []) and (
        token := readonly_refusal(command)
    ):
        return _readonly_refused(token)
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


# -- socket-backed public client ---------------------------------------------

NOT_LINKED_MESSAGE = (
    "no tailnet is linked on this install, so there is nothing for the broker "
    "to serve - link one with ./sc ts init --ssh-user <user> --allowed-host "
    "<host> (or --readonly-host <host>)"
)
BROKER_START_WAIT = 20
BROKER_START_INTERVAL = 1
BROKER_START_TIMEOUT = 120
LINK_CHECK_TIMEOUT = 150
LINK_NEXT = {
    "daemon": (
        "start tailscaled on the host (`sudo systemctl enable --now "
        "tailscaled`), or name the CLI with ./sc ts init --tailscale-bin <path>"
    ),
    "auth": "run `tailscale up` on the host once, then re-run this command",
    "peer": (
        "compare the name with `tailscale status`; drop a wrong one with "
        "./sc ts remove-host <host> and declare the right one with "
        "./sc ts add-host <host>"
    ),
    "ssh": (
        "allow this node to SSH as that user in the tailnet policy, or set "
        "the host's own user with ./sc ts add-host <user>@<host>"
    ),
}


def _client_error(operation: str, message: str) -> dict:
    """A dead socket is one of two cases; say which.

    `./sc ts-broker-up` deliberately starts nothing while no `ts` block
    exists, so "start the broker" is the wrong fix for an unlinked install.
    """
    try:
        linked = bool(read())
    except Exception:  # an unreadable instance file is not "unlinked"
        linked = True
    if not linked:
        return {"ok": False, "error": "ts_not_linked", "operation": operation,
                "message": NOT_LINKED_MESSAGE}
    return {"ok": False, "error": "broker_unreachable", "operation": operation,
            "message": ("a tailnet is linked but the ts-broker is not running "
                        f"- start it on the host with ./sc ts-broker-up ({message})"),
            "start_command": "./sc ts-broker-up"}


def _broker_ready() -> bool:
    try:
        return broker_call("GET", "/health", timeout=5).get("ok") is True
    except ConnectionError:
        return False


def _broker_health() -> dict:
    return {"ready": _broker_ready(), "start_command": "./sc ts-broker-up"}


def _dispatch_broker_up() -> tuple[bool, str]:
    """Run `./sc ts-broker-up` as one fixed dispatcher verb.

    Kept as its own seam so tests replace it - it starts a real host process.
    """
    dispatch = ports.ENGINE / "scripts" / "dispatch.sh"
    root = ports.ENGINE.parent.resolve()
    try:
        process = subprocess.run(
            ["sh", str(dispatch), "ts-broker-up"], capture_output=True,
            text=True, cwd=str(root), timeout=BROKER_START_TIMEOUT, check=False,
            env={**os.environ, "SC_CALLER_ROOT": str(root)},
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        return False, f"command not found: {exc.filename}"
    except subprocess.TimeoutExpired:
        return False, f"ts-broker-up: timed out (>{BROKER_START_TIMEOUT}s)"
    return process.returncode == 0, (process.stdout + process.stderr).strip()


def ensure_broker() -> dict:
    """Make sure the broker serves the block that was just saved: already
    answering -> nothing to do; on the host -> start it and wait for the
    socket; in a sandbox -> return the exact host command instead."""
    state = {"ready": False, "started": False, "start_command": "./sc ts-broker-up"}
    if _broker_ready():
        return {**state, "ready": True}
    if os.environ.get("SC_SANDBOX"):
        return {**state,
                "detail": "the broker runs on the host; run ./sc ts-broker-up there"}
    ok, output = _dispatch_broker_up()
    if not ok:
        return {**state, "detail": output[:300] or "./sc ts-broker-up failed"}
    deadline = time.monotonic() + BROKER_START_WAIT
    while True:
        if _broker_ready():
            return {**state, "ready": True, "started": True}
        if time.monotonic() >= deadline:
            break
        time.sleep(BROKER_START_INTERVAL)
    return {**state,
            "detail": (f"the broker did not answer within {BROKER_START_WAIT}s "
                       "of ./sc ts-broker-up - read .super-coder/run/ts-broker.log")}


def _link_check(check: str, cfg: dict, hosts: list[str]) -> dict | None:
    """One validator result, or None when it could not be run at all. The
    tailscale CLI lives on the host; a sandboxed caller asks the broker."""
    if not os.environ.get("SC_SANDBOX"):
        return validate(check, cfg, hosts)
    try:
        response = broker_call("POST", f"/validate/{check}",
                               {"ts": cfg, "hosts": hosts},
                               timeout=LINK_CHECK_TIMEOUT)
    except ConnectionError:
        return None
    return response if isinstance(response.get("ok"), bool) else None


def verify_link(cfg: dict, hosts: list[str]) -> dict:
    """Test the hosts a command just NAMED: daemon, auth, then peer and ssh
    for those hosts only. A check is skipped once one it depends on has
    failed, so one cause is reported once.

    With no host named nothing on the tailnet is probed - the caller has
    already checked the broker, and that is all this reports. A command that
    only changes `ssh_user` must not fail because some other declared machine
    is asleep, and must not open a session to every machine in real use.
    """
    scope = _scope(cfg, hosts) if hosts else []
    if not scope:
        return {
            "passed": True, "checks": [], "hosts": [],
            "note": ("no host was named, so only the broker was checked and "
                     "no tailnet host was probed; ./sc ts add-host <host> "
                     "tests one host (it is safe to repeat for a host "
                     "already declared)"),
        }
    checks: list[dict] = []
    blocked = False
    for check in CHECKS:
        if blocked:
            checks.append({"check": check, "ok": False, "skipped": True,
                           "output": "not run: an earlier check failed",
                           "next": None})
            continue
        result = _link_check(check, cfg, scope)
        if result is None:
            entry = {"check": check, "ok": False, "skipped": True,
                     "output": "not run: the ts-broker is not reachable from here",
                     "next": "./sc ts-broker-up on the host, then re-run this command"}
        else:
            ok = bool(result["ok"])
            entry = {"check": check, "ok": ok, "skipped": False,
                     "output": str(result.get("output") or "")[:400],
                     "next": None if ok else LINK_NEXT[check]}
        blocked = not entry["ok"]
        checks.append(entry)
    return {"passed": all(item["ok"] for item in checks), "checks": checks,
            "hosts": scope}


class TierRefused(ValueError):
    """A command named a read-only host as allowed. Nothing may be written."""

    def __init__(self, host: str) -> None:
        super().__init__(f"'{host}' is declared read-only")
        self.host = host


def _parse_host(value: str) -> tuple[str, str]:
    """`host` or `user@host` -> (host, user). The user is that host's own
    remote user; without one the block's `ssh_user` applies. Both are held to
    HOST_NAME / USER_NAME, so neither can begin with a dash."""
    text = str(value)
    user, separator, host = text.rpartition("@")
    if not valid_host(host):
        raise ValueError(f"not a host name: {value!r}")
    if separator and not valid_user(user):
        raise ValueError(f"not a user name: {value!r}")
    return host, user


def _merge_hosts(current: dict, allowed: list[str] | None,
                 readonly: list[str] | None, replace: bool) -> tuple[dict, dict]:
    """Fold named hosts into the saved lists. Returns (fields, changes).

    Every argument is parsed and every refusal raised BEFORE anything is
    built, so a refused command changes nothing - not a list, not a host's
    user.

    - Additive by default: a host already declared is never dropped by naming
      another one. `replace` restores the wholesale behaviour for each list
      that was named.
    - A host that is read-only in the SAVED block can never be named as
      allowed: TierRefused, whatever else the command says. Loosening a host
      is `remove-host` followed by `add-host`, two deliberate commands.
    - Naming an allowed host read-only moves it (tightening is always fine).
    - The result never holds a host in both tiers. A saved block that does
      (hand-edited; exec already treated it as read-only) is read as
      read-only first, so replacing `readonly_hosts` drops such a host rather
      than leaving it behind in `allowed_hosts`.
    """
    named_allowed = [_parse_host(value) for value in allowed or []]
    named_readonly = [_parse_host(value) for value in readonly or []]
    before_readonly = list(current.get("readonly_hosts") or [])
    before_allowed = [host for host in current.get("allowed_hosts") or []
                      if host not in before_readonly]
    readonly_names = {host for host, _ in named_readonly}
    for host, _ in named_allowed:
        if host in before_readonly and host not in readonly_names:
            raise TierRefused(host)

    saved_users = current.get("ssh_users")
    users = dict(saved_users) if isinstance(saved_users, dict) else {}
    new_allowed = [] if replace and allowed is not None else list(before_allowed)
    new_readonly = [] if replace and readonly is not None else list(before_readonly)
    for pairs, target in ((named_allowed, new_allowed),
                          (named_readonly, new_readonly)):
        for host, user in pairs:
            if user:
                users[host] = user
            if host not in target:
                target.append(host)
    new_allowed = [host for host in new_allowed if host not in new_readonly]
    declared = set(new_allowed) | set(new_readonly)
    users = {host: user for host, user in users.items() if host in declared}
    before = list(dict.fromkeys([*before_allowed, *before_readonly]))
    after = list(dict.fromkeys([*new_allowed, *new_readonly]))
    changes = {
        "added": [host for host in after if host not in before],
        "kept": [host for host in after if host in before],
        "removed": [host for host in before if host not in after],
        "named": list(dict.fromkeys(
            host for host, _ in [*named_allowed, *named_readonly])),
    }
    return ({"allowed_hosts": new_allowed, "readonly_hosts": new_readonly,
             "ssh_users": users}, changes)


def _block(current: dict, fields: dict, ssh_user: str | None = None,
           tailscale_bin: str | None = None) -> dict:
    block = {
        "ssh_user": ssh_user if ssh_user is not None
        else current.get("ssh_user", ""),
        "allowed_hosts": fields["allowed_hosts"],
        "readonly_hosts": fields["readonly_hosts"],
        "tailscale_bin": tailscale_bin if tailscale_bin is not None
        else current.get("tailscale_bin", "tailscale"),
    }
    if fields["ssh_users"]:  # written only when used, so old blocks round-trip
        block["ssh_users"] = fields["ssh_users"]
    return block


def _save_and_report(operation: str, block: dict, changes: dict,
                     verify: bool) -> dict:
    """Save the block, then (unless told not to) start the broker and test the
    link. The block is durable before the checks run, so a failed check says
    it IS saved rather than discarding it."""
    try:
        saved = write(block)
        if isinstance(saved, dict):  # write() may have normalized the tiers
            block = saved
    except BlockInvalid as exc:
        return {"ok": False, "error": "ts_block_invalid", "operation": operation,
                "message": f"nothing was saved: {exc}"}
    except Exception as exc:  # a failed save must never look like a save
        return {"ok": False, "error": "ts_write_failed", "operation": operation,
                "message": (f"the ts block could not be saved "
                            f"({type(exc).__name__}: {exc}) - check that "
                            f"{ports.CONFIG} and its directory are writable "
                            "by this user")}
    result = {"ok": True, "operation": operation, "ts": block, "hosts": changes}
    if not verify:
        return {**result, "verified": False, "broker": _broker_health()}
    broker = ensure_broker()
    report = verify_link(block, changes.get("named") or [])
    result.update({"verified": report["passed"], "broker": broker,
                   "verify": report})
    if not report["passed"]:
        failed = [item["check"] for item in report["checks"] if not item["ok"]]
        result.update({
            "ok": False, "error": "ts_verify_failed", "saved": True,
            "message": ("the ts block IS saved, but the link did not verify ("
                        + ", ".join(failed) + "). Fix what the checks name "
                        "and re-run, or pass --no-verify to save without "
                        "testing."),
        })
    return result


def client_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="./sc ts",
        description="Observe and diagnose declared tailnet hosts through ts-broker.",
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("status", help="show tailnet node and peer status")
    execute = commands.add_parser(
        "exec", help="execute a permitted command on a declared tailnet host"
    )
    execute.add_argument("host")
    execute.add_argument("command", nargs=argparse.REMAINDER, metavar="COMMAND")
    init = commands.add_parser(
        "init",
        help="write the ts block (hosts are ADDED to the saved lists), start "
             "the broker and test the link",
    )
    init.add_argument("--ssh-user", help="default remote user for every host")
    init.add_argument("--allowed-host", action="append", dest="allowed_hosts",
                      metavar="[USER@]HOST",
                      help="a disposable test seat: unrestricted exec (repeatable)")
    init.add_argument("--readonly-host", action="append", dest="readonly_hosts",
                      metavar="[USER@]HOST",
                      help="a machine in real use: diagnostic commands only "
                           "(repeatable)")
    init.add_argument("--tailscale-bin")
    init.add_argument(
        "--replace-hosts", action="store_true",
        help="replace each host list that is named instead of adding to it",
    )
    add = commands.add_parser(
        "add-host", help="declare one more tailnet host without touching the rest"
    )
    add.add_argument("host", metavar="[USER@]HOST")
    add.add_argument(
        "--readonly", action="store_true",
        help="declare it in readonly_hosts (diagnostic commands only)",
    )
    remove = commands.add_parser("remove-host", help="stop declaring one tailnet host")
    remove.add_argument("host")
    for command in (init, add):
        command.add_argument(
            "--no-verify", action="store_true",
            help="save without testing the link or starting the broker "
                 "(offline setup)",
        )
    args = parser.parse_args(argv)

    current: dict = {}
    if args.operation in ("init", "add-host", "remove-host"):
        current = read() or {}
    try:
        if args.operation == "init":
            if args.ssh_user not in (None, "") and not valid_user(args.ssh_user):
                raise ValueError(f"not a user name: {args.ssh_user!r}")
            fields, changes = _merge_hosts(
                current, args.allowed_hosts, args.readonly_hosts, args.replace_hosts
            )
            result = _save_and_report(
                "init", _block(current, fields, args.ssh_user, args.tailscale_bin),
                changes, not args.no_verify,
            )
        elif args.operation == "add-host":
            fields, changes = _merge_hosts(
                current,
                None if args.readonly else [args.host],
                [args.host] if args.readonly else None,
                False,
            )
            result = _save_and_report(
                "add-host", _block(current, fields), changes, not args.no_verify
            )
        elif args.operation == "remove-host":
            # Not parsed: any declared entry can be removed, including a
            # hand-edited one that would not pass validation today.
            host = args.host
            if host not in _declared_hosts(current):
                result = {
                    "ok": False, "error": "host_not_declared",
                    "operation": "remove-host",
                    "message": f"'{host}' is not in allowed_hosts or readonly_hosts",
                    "declared": _declared_hosts(current),
                }
            else:
                users = current.get("ssh_users")
                kept_readonly = [
                    h for h in current.get("readonly_hosts") or [] if h != host]
                fields = {
                    "allowed_hosts": [
                        h for h in current.get("allowed_hosts") or []
                        if h != host and h not in kept_readonly],
                    "readonly_hosts": kept_readonly,
                    "ssh_users": {
                        h: u for h, u in (users if isinstance(users, dict) else {}).items()
                        if h != host},
                }
                changes = {"added": [], "removed": [host], "named": [],
                           "kept": list(dict.fromkeys(
                               [*fields["allowed_hosts"], *fields["readonly_hosts"]]))}
                result = _save_and_report(
                    "remove-host", _block(current, fields), changes, False
                )
        else:
            try:
                if args.operation == "status":
                    result = broker_call("GET", "/status")
                else:
                    command = (args.command[1:] if args.command[:1] == ["--"]
                               else args.command)
                    result = broker_call(
                        "POST", "/exec",
                        {"host": args.host, "command": " ".join(command)},
                    )
            except ConnectionError as exc:
                result = _client_error(args.operation, str(exc))
    except TierRefused as exc:
        result = {
            "ok": False, "error": "host_is_readonly", "operation": args.operation,
            "host": exc.host,
            "message": (f"nothing was saved: '{exc.host}' is declared read-only "
                        f"and stays read-only; ./sc ts remove-host {exc.host} "
                        "first to change its tier"),
        }
    except ValueError as exc:
        result = {"ok": False, "error": "host_invalid",
                  "operation": args.operation,
                  "message": f"nothing was saved: {exc}"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 1


# -- host CLI (path lookup for `sc`; verbs for manual no-broker testing) ------

def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "sock"
    if mode == "client":
        return client_main(argv[1:])
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
        sys.exit("usage: ts.py [client <status|exec|init|add-host|remove-host>"
                 "|sock|status|exec <host> <cmd>|validate <check>]")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
