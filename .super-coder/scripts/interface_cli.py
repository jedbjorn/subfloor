#!/usr/bin/env python3
"""sc interface — CLI parity for the Interface (spec #20, sprint 25 seq 6).

The terminal client half of the Interface: every verb talks to the engine's
Interface HTTP API (operator bearer, the mode-0600 capability the supervised
server provisions at boot) and NOTHING else — no direct DB reads of interface
state, no `tmux attach` (spec #20 CLI Parity: an API outage reports the
supervised-runtime remediation; it never falls back).

    ./sc interface status [<shell>] [--json]       rail state (+ exact session
                                                   state for a named shell)
    ./sc interface start <shell> [--harness H] [--model M] [--effort E] [--json]
                                                   scriptable New chat
    ./sc interface view <shell>                    read-only attach
    ./sc interface attach <shell>                  writer attach (refuses a held
                                                   lease; never takes over)
    ./sc interface take-control <shell>            explicit writer transfer
    ./sc interface stop <shell> [--force] [--json] graceful end; force only
                                                   after a graceful timeout
    ./sc interface reconcile <shell> [--close] [--json]
    ./sc interface enter [<shell>]                 the `sc enter` flow: New chat
                                                   for an available shell
                                                   (normal harness picker),
                                                   reattach an occupied one

The attach client speaks sc-term.v1 (api/interface_ws.py) — the same session
stream and writer lease as the browser, ported from the proven spike
(spikes/interface-stream/cli_client.py). A refresh/reconnect reattaches the
same generation; it never provider-resumes.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import ports as ports_mod  # noqa: E402
import style  # noqa: E402
import run as run_mod  # noqa: E402  — the enter flow reuses its harness picker

RUN_DIR = ENGINE / "run" / "interface"
OPERATOR_TOKEN_PATH = RUN_DIR / "operator.token"
API_BASE = f"http://127.0.0.1:{ports_mod.resolve().get('port', 8800)}"

EXIT_REFUSED = 1       # an API refusal (409/4xx) or a precondition failed
EXIT_USAGE = 2         # bad arguments / unknown shell
EXIT_API_DOWN = 3      # API unreachable / capability missing — remediation

SUBPROTOCOL = "sc-term.v1"
HEARTBEAT_S = 20
OCCUPIED_WAIT_S = 90   # reservation TTL is 60s; a healthy boot is seconds


class ApiError(Exception):
    """An HTTP answer from the Interface API (status + the error envelope)."""

    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def die(msg: str, code: int = EXIT_REFUSED) -> None:
    print(f"sc interface: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _die_unreachable(reason) -> None:
    die(f"Interface API unreachable at {API_BASE} ({reason})\n"
        "  The Interface runtime is host-supervised — restart it via the "
        "supervisor (`./sc restart`, or `make dos-r` on the host stack).\n"
        "  There is no direct-DB or tmux fallback (spec #20).",
        EXIT_API_DOWN)


def _operator_token() -> str:
    try:
        return OPERATOR_TOKEN_PATH.read_text().strip()
    except OSError:
        die(f"operator capability missing at {OPERATOR_TOKEN_PATH} — the "
            "Interface server provisions it at boot; restart the supervised "
            "runtime (`./sc restart` / `make dos-r`).", EXIT_API_DOWN)


def _http(req):
    """The one network seam — tests patch this, never the verbs."""
    return urllib.request.urlopen(req, timeout=15)


def api(method: str, path: str, body: dict | None = None) -> dict:
    """One Interface API call: operator bearer, Idempotency-Key (uuid4) on
    every mutation exactly as the API requires. An HTTP error answer raises
    ApiError; a transport failure is the supervised-runtime remediation."""
    headers = {"Authorization": f"Bearer {_operator_token()}"}
    data = None
    if method in ("POST", "DELETE", "PATCH", "PUT"):
        headers["Idempotency-Key"] = str(uuid.uuid4())
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with _http(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read() or b"{}").get("error") or {}
        except ValueError:
            err = {}
        raise ApiError(exc.code, err.get("code", f"http_{exc.code}"),
                       err.get("message", exc.reason),
                       err.get("details")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _die_unreachable(getattr(exc, "reason", exc))


# ------------------------------------------------------------------ helpers

def _shells() -> list[dict]:
    return api("GET", "/api/interface/shells").get("shells", [])


def _find_shell(name: str) -> dict:
    shells = _shells()
    chosen = next((s for s in shells
                   if (s.get("shortname") or "").lower() == name.lower()), None)
    if chosen is None:
        avail = ", ".join(s.get("shortname") or "?" for s in shells) or "none"
        die(f"no shell '{name}'. Available: {avail}", EXIT_USAGE)
    return chosen


def _session_of(shell: dict) -> int | None:
    sid = shell.get("session_id")
    if sid is None:
        die(f"{shell.get('shortname')} is {shell.get('availability')} — no "
            "live Interface session to act on")
    return sid


def _session_detail(session_id: int) -> dict:
    return api("GET", f"/api/interface/sessions/{session_id}")


def _client_id() -> str:
    return f"cli-{os.getpid()}"


def _winsize() -> tuple[int, int]:
    """Current terminal rows/cols for the pane reservation (24x80 off-TTY)."""
    try:
        import fcntl
        import struct
        rows, cols, _, _ = struct.unpack(
            "HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
        return rows or 24, cols or 80
    except OSError:
        return 24, 80


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _print_api_error(exc: ApiError) -> None:
    print(f"sc interface: {exc.code}: {exc.message}", file=sys.stderr)


# ------------------------------------------------------------------ status

def _status_line(s: dict) -> str:
    short = "{:<10}".format(s.get("shortname") or "?")
    name = "{:<18}".format(s.get("display_name") or "")
    avail = "{:<13}".format(s.get("availability") or "?")
    bits = []
    if s.get("lifecycle"):
        bits.append(s["lifecycle"])
    if s.get("harness"):
        bits.append(s["harness"])
    if s.get("composer"):
        bits.append(f"composer:{s['composer']}")
    if s.get("alerts"):
        bits.append(f"alerts:{s['alerts']}")
    return f"{short} {name} {avail} {' · '.join(bits)}".rstrip()


def cmd_status(args) -> int:
    if not args.shell:
        shells = _shells()
        if args.json:
            _print_json({"shells": shells})
        else:
            for s in shells:
                print(_status_line(s))
        return 0
    shell = _find_shell(args.shell)
    detail = (_session_detail(shell["session_id"])
              if shell.get("session_id") is not None else None)
    if args.json:
        _print_json({"shell": shell, "session": detail})
        return 0
    print(_status_line(shell))
    if detail is None:
        return 0
    writer = detail.get("writer") or {}
    held = (f"held by {writer.get('client_id')}" if writer.get("held")
            else "free")
    rows = [("session", detail.get("session_id")),
            ("generation", detail.get("generation")),
            ("occupancy", detail.get("occupancy")),
            ("lifecycle", detail.get("lifecycle")),
            ("harness", detail.get("harness")),
            ("model", detail.get("model_route")),
            ("composer", detail.get("composer")),
            ("writer", held),
            ("wake", detail.get("wake_state")),
            ("clients", detail.get("clients")),
            ("alerts", detail.get("alerts"))]
    if detail.get("error_detail"):
        rows.append(("error", detail["error_detail"]))
    for key, value in rows:
        print(f"  {key:<11} {value}")
    return 0


# ------------------------------------------------------------------ start

def _start_session(shell: dict, harness=None, model=None, effort=None) -> dict:
    body = {"shell_id": shell["shell_id"]}
    rows, cols = _winsize()
    body["rows"], body["cols"] = rows, cols
    if harness:
        body["harness"] = harness
    if model:
        body["model"] = model
    if effort:
        body["effort"] = effort
    try:
        return api("POST", "/api/interface/sessions", body)
    except ApiError as exc:
        if exc.status == 409 and exc.code == "shell_occupied":
            owner = (exc.details or {}).get("session_id")
            die(f"{shell.get('shortname')} is occupied — session {owner} "
                "owns it; attach with `sc interface attach "
                f"{shell.get('shortname')}` (or take-control)")
        _print_api_error(exc)
        raise SystemExit(EXIT_REFUSED) from exc


def cmd_start(args) -> int:
    shell = _find_shell(args.shell)
    resp = _start_session(shell, harness=args.harness, model=args.model,
                          effort=args.effort)
    if args.json:
        _print_json(resp)
        return 0
    short = shell.get("shortname")
    if resp.get("occupancy") in ("unreconciled", "ended"):
        die(f"session {resp.get('session_id')} could not start "
            f"({resp.get('error', 'spawn failed')}) — investigate, then "
            f"`sc interface reconcile {short}`")
    print(f"→ session {resp['session_id']} reserved for {short} "
          f"(generation {resp.get('generation')}, starting) — attach with "
          f"`sc interface attach {short}`")
    return 0


# ------------------------------------------------------------------ attach

def _acquire_lease(session_id: int, takeover: bool) -> dict:
    return api("POST", "/api/interface/writer-leases",
               {"session_id": session_id, "client_id": _client_id(),
                "takeover": takeover})


def _writer_holder(session_id: int) -> str:
    try:
        writer = (_session_detail(session_id).get("writer") or {})
    except ApiError:
        return ""
    return (f" by {writer.get('client_id')}" if writer.get("held") else "")


def _attach(session_id: int, role: str, lease: dict | None = None) -> int:
    """Mint a single-use ticket and run the terminal stream. Writer tickets
    ride the current lease token; input seq continues the SESSION's sequence
    (the lease reseeds from forwarded_seq+1 — starting at 1 would wedge)."""
    body = {"session_id": session_id, "role": role,
            "client_id": _client_id()}
    if lease is not None:
        body["lease_token"] = lease["lease_token"]
    ticket = api("POST", "/api/interface/stream-tickets", body)["ticket"]
    ws_url = (API_BASE.replace("http", "ws", 1)
              + f"/api/interface/session-streams/{session_id}?ticket={ticket}")
    start_seq = (lease or {}).get("next_input_seq", 1)
    return run_stream(ws_url, role, start_seq, lease)


def _attach_writer(shell: dict, session_id: int, takeover: bool) -> int:
    short = shell.get("shortname")
    try:
        lease = _acquire_lease(session_id, takeover)
    except ApiError as exc:
        if exc.status == 409 and not takeover:
            die(f"writer lease held{_writer_holder(session_id)} "
                f"({exc.message}) — not taking over silently; use "
                f"`sc interface take-control {short}` for an explicit "
                f"transfer, or `sc interface view {short}` to watch")
        _print_api_error(exc)
        raise SystemExit(EXIT_REFUSED) from exc
    return _attach(session_id, "writer", lease)


def cmd_view(args) -> int:
    shell = _find_shell(args.shell)
    return _attach(_session_of(shell), "viewer")


def cmd_attach(args) -> int:
    shell = _find_shell(args.shell)
    return _attach_writer(shell, _session_of(shell), takeover=False)


def cmd_take_control(args) -> int:
    shell = _find_shell(args.shell)
    return _attach_writer(shell, _session_of(shell), takeover=True)


def run_stream(ws_url: str, role: str, start_seq: int,
               lease: dict | None = None) -> int:
    """The sc-term.v1 terminal loop (spike port): stdin raw, stdin bytes →
    0x01|seq:u64be|payload as writer, 0x03 resize on start + SIGWINCH,
    0x00/0x04 payloads → stdout, JSON control frames dimmed to stderr, 20s
    writer heartbeat, clean exit on close/terminated. The writer lease is
    released on the way out (a closing client releases only ITS lease —
    tmux and the harness continue)."""
    from websockets.sync.client import connect

    ws = connect(ws_url, subprotocols=[SUBPROTOCOL])
    old_attrs = termios.tcgetattr(0) if os.isatty(0) else None
    if old_attrs:
        tty.setraw(0)
    state = {"seq": start_seq, "winch": True, "dead": False}
    signal.signal(signal.SIGWINCH, lambda *_: state.__setitem__("winch", True))

    def sender() -> None:
        try:
            while not state["dead"]:
                if state["winch"]:
                    state["winch"] = False
                    rows, cols = _winsize()
                    ws.send(b"\x03" + rows.to_bytes(2, "big")
                            + cols.to_bytes(2, "big"))
                r, _, _ = select.select([sys.stdin.buffer], [], [], 0.2)
                if not r:
                    continue
                data = os.read(0, 65536)
                if not data:
                    state["dead"] = True
                    return
                if role == "writer":
                    ws.send(b"\x01" + state["seq"].to_bytes(8, "big") + data)
                    state["seq"] += 1
        except Exception:
            state["dead"] = True

    def heartbeater() -> None:
        while not state["dead"]:
            time.sleep(HEARTBEAT_S)
            try:
                ws.send(json.dumps({"type": "heartbeat"}))
            except Exception:
                return

    threads = [threading.Thread(target=sender, daemon=True)]
    if role == "writer":
        threads.append(threading.Thread(target=heartbeater, daemon=True))
    for t in threads:
        t.start()

    out = sys.stdout.buffer
    try:
        for message in ws:
            if isinstance(message, bytes):
                if message[:1] in (b"\x00", b"\x04"):
                    out.write(message[1:])
                    out.flush()
            else:
                msg = json.loads(message)
                if msg.get("type") == "error" and msg.get("code") == "terminated":
                    break
                print(f"\x1b[2m[{message}]\x1b[0m", file=sys.stderr)
    except Exception as exc:
        print(f"connection closed: {exc!r}", file=sys.stderr)
    finally:
        state["dead"] = True
        if old_attrs:
            termios.tcsetattr(0, termios.TCSADRAIN, old_attrs)
        if lease:
            try:
                api("DELETE",
                    f"/api/interface/writer-leases/{lease['lease_id']}",
                    {"lease_token": lease["lease_token"]})
            except Exception:
                pass  # session end already revokes; release is best-effort
    return 0


# ------------------------------------------------------------------ stop / reconcile

def cmd_stop(args) -> int:
    shell = _find_shell(args.shell)
    session_id = _session_of(shell)
    try:
        resp = api("POST", "/api/interface/termination-requests",
                   {"session_id": session_id, "force": bool(args.force)})
    except ApiError as exc:
        _print_api_error(exc)
        if exc.code == "force_requires_graceful_timeout":
            print("  run without --force first; force unlocks only after "
                  "that graceful request times out", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from exc
    if args.json:
        _print_json(resp)
        return 0 if resp.get("terminated") else EXIT_REFUSED
    if resp.get("terminated"):
        print(f"→ session {session_id} ended — {shell.get('shortname')} "
              "offers New chat again")
        return 0
    reason = resp.get("reason", "graceful_timeout")
    print(f"→ session {session_id} not terminated ({reason}, "
          f"pid {resp.get('pid')}, generation {resp.get('generation')})")
    if reason == "graceful_timeout":
        print("  force is now unlocked: `sc interface stop "
              f"{shell.get('shortname')} --force`")
    return EXIT_REFUSED


def cmd_reconcile(args) -> int:
    shell = _find_shell(args.shell)
    session_id = _session_of(shell)
    action = "close" if args.close else "verify"
    try:
        resp = api("POST", "/api/interface/reconciliations",
                   {"session_id": session_id, "action": action})
    except ApiError as exc:
        _print_api_error(exc)
        raise SystemExit(EXIT_REFUSED) from exc
    if args.json:
        _print_json(resp)
        return 0
    for line in resp.get("actions", []):
        print(f"→ {line}")
    print(f"→ session {session_id}: occupancy {resp.get('occupancy')}")
    return 0


# ------------------------------------------------------------------ enter

def _flavor_harness(shell: dict) -> str | None:
    """The shell's flavor default harness, read from the engine DB exactly as
    the boot path does (launch routing data — never interface state, which
    comes from the API only). Best-effort: any failure degrades to the
    instance default."""
    try:
        con = run_mod.db_driver.connect(str(run_mod.DB_PATH))
        try:
            row = con.execute("SELECT flavor FROM shells WHERE shell_id=?",
                              (shell["shell_id"],)).fetchone()
            fdef = run_mod.flavor_defaults(con).get(row["flavor"] if row
                                                    else None)
        finally:
            con.close()
    except Exception:
        return None
    return fdef["default_harness"] if fdef else None


def _pick_harness(shell: dict, args) -> str | None:
    """The normal boot picker (run.py's own): an explicit --harness / HARNESS
    wins silently; else the harnesses on PATH, defaulting to this shell's
    flavor default → instance.json → 'claude'. Model/effort resolve through
    the reservation (prepare_launch's flavor_defaults), like the GUI's New
    chat; explicit --model/--effort flags override."""
    if args.harness:
        return args.harness
    if os.environ.get("HARNESS"):
        return os.environ["HARNESS"]
    default = (_flavor_harness(shell) or run_mod._configured_harness()
               or "claude")
    picked = run_mod.pick_harness(run_mod.detect_harnesses(), default,
                                  first=False)
    return picked or default


def _pick_shell(shells: list[dict], requested: str | None) -> dict:
    """The `sc enter` shell picker, API-backed: the rail's own data (the
    operator bearer is the auth — no username round-trip)."""
    if requested:
        return _find_shell(requested)
    if not shells:
        die("no shells")
    if not sys.stdin.isatty():
        return shells[0]
    for n, s in enumerate(shells, 1):
        print(f"{style.dim(f'{n:>3}')}  {_status_line(s)}")
    while True:
        choice = input("\nPick (#): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(shells):
            return shells[int(choice) - 1]
        print("  invalid choice")


def _wait_occupied(session_id: int, timeout: float = OCCUPIED_WAIT_S) -> dict:
    """Poll until the reservation promotes to occupied (the session_start
    hook) or fails. A writer lease requires occupied, so attach waits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = _session_detail(session_id)
        if detail.get("occupancy") == "occupied":
            return detail
        if detail.get("occupancy") in ("unreconciled", "ended"):
            die(f"session {session_id} failed to start "
                f"({detail.get('error_detail') or 'no detail'}) — "
                "`sc interface reconcile` after investigating")
        time.sleep(1)
    die(f"session {session_id} still starting after {int(timeout)}s — "
        "attach when occupied: `sc interface attach <shell>`")


def cmd_enter(args) -> int:
    """`sc enter`: resolve the Interface API, then New chat for an available
    shell or reattach the occupied generation. Never provider-resume, never
    `tmux attach`."""
    shell = _pick_shell(_shells(), args.shell)
    short = shell.get("shortname")
    avail = shell.get("availability")
    if avail == "available":
        harness = _pick_harness(shell, args)
        resp = _start_session(shell, harness=harness, model=args.model,
                              effort=args.effort)
        session_id = resp["session_id"]
        print(f"→ session {session_id} starting for {short} "
              f"(generation {resp.get('generation')}, harness "
              f"{resp.get('harness') or harness or 'default'})…")
        _wait_occupied(session_id)
        return _attach_writer(shell, session_id, takeover=False)
    if avail == "occupied":
        session_id = shell["session_id"]
        try:
            lease = _acquire_lease(session_id, takeover=False)
        except ApiError as exc:
            if exc.status != 409:
                _print_api_error(exc)
                raise SystemExit(EXIT_REFUSED) from exc
            print(f"→ writer lease held{_writer_holder(session_id)} — "
                  "attaching READ-ONLY (`sc interface take-control "
                  f"{short}` to take the writer role)", file=sys.stderr)
            return _attach(session_id, "viewer")
        return _attach(session_id, "writer", lease)
    if avail == "starting":
        die(f"{short} is starting (a reservation is booting) — retry in a "
            f"moment, or watch with `sc interface view {short}`")
    die(f"{short} is {avail} — New chat is blocked. "
        f"`sc interface status {short}` shows the session; "
        f"`sc interface reconcile {short}` revalidates it.")


# ------------------------------------------------------------------ dispatch

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sc interface",
        description="CLI parity for the Interface (spec #20) — API only, "
                    "no direct DB, no tmux attach")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="rail/session state")
    sp.add_argument("shell", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("start", help="scriptable API-backed New chat")
    sp.add_argument("shell")
    sp.add_argument("--harness")
    sp.add_argument("--model")
    sp.add_argument("--effort")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("view", help="read-only attach")
    sp.add_argument("shell")
    sp.set_defaults(fn=cmd_view)

    sp = sub.add_parser("attach", help="writer attach (never takes over)")
    sp.add_argument("shell")
    sp.set_defaults(fn=cmd_attach)

    sp = sub.add_parser("take-control", help="explicit writer transfer")
    sp.add_argument("shell")
    sp.set_defaults(fn=cmd_take_control)

    sp = sub.add_parser("stop", help="graceful end; --force after a timeout")
    sp.add_argument("shell")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("reconcile", help="revalidate (or --close) a session")
    sp.add_argument("shell")
    sp.add_argument("--close", action="store_true",
                    help="end an unreconciled session after proved absence")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_reconcile)

    sp = sub.add_parser("enter", help="the `sc enter` flow (in-container)")
    sp.add_argument("shell", nargs="?")
    sp.add_argument("--harness")
    sp.add_argument("--model")
    sp.add_argument("--effort")
    sp.set_defaults(fn=cmd_enter)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
