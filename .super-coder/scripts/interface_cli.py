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
stream and writer lease as the browser, with the full client-side protocol
semantics (ack-gated input, read-only flip on lease loss, quiet control
frames). A refresh/reconnect reattaches the same generation; it never
provider-resumes.
"""
from __future__ import annotations

import argparse
import json
from collections import deque
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
        if exc.status == 409 and exc.code == "writer_held" and not takeover:
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


def _stdin_ready(timeout: float) -> bool:
    """One stdin poll — the sender's input seam (tests patch this)."""
    r, _, _ = select.select([sys.stdin.buffer], [], [], timeout)
    return bool(r)


def _read_stdin() -> bytes:
    """One stdin read — ≤64 KiB, so it always fits MAX_INPUT_PAYLOAD."""
    return os.read(0, 65536)


def _ws_connect(ws_url: str):
    """The WS seam — tests patch this, never a socket. Lazy import: HTTP-only
    verbs (status/start/stop/reconcile) run on a stdlib python (spec #30 req
    12, #518); only attach/view/take-control reach here, and a missing
    package refuses with the exact dependency action."""
    try:
        from websockets.sync.client import connect
    except ImportError:
        die("this command streams the terminal over websockets, but no "
            "importable `websockets` package was found — run ./sc deps (or "
            "./sc build to refresh the sandbox image). The HTTP-only verbs "
            "(status/start/stop/reconcile) work without it.", EXIT_API_DOWN)
    return connect(ws_url, subprotocols=[SUBPROTOCOL])


def run_stream(ws_url: str, role: str, start_seq: int,
               lease: dict | None = None) -> int:
    """The sc-term.v1 terminal loop, production client semantics (spec #20
    Input Broker): stdin raw; keystrokes are sent ACK-GATED — at most one
    unacknowledged input frame in flight, later keystrokes buffered locally
    and drained one frame per input_ack (the same one-unacked rule as the
    browser, which also bounds what a stalled broker can queue server-side).
    A writer_revoked/stale_generation reject or a non-active writer control
    flips the client READ-ONLY with a loud notice — input stops, output
    continues (the browser's flip for a lost lease). Routine control frames
    (input_ack, heartbeat acks, unchanged state) are never echoed: raw-mode
    stderr carries errors and state transitions only, so the attached TUI
    isn't garbled. 0x00/0x04 payloads → stdout, 0x03 resize on start +
    SIGWINCH, 20s writer heartbeat (halted on read-only), clean exit on
    close/terminated. The writer lease is released on the way out (a
    closing client releases only ITS lease — tmux and the harness
    continue)."""
    ws = _ws_connect(ws_url)
    old_attrs = termios.tcgetattr(0) if os.isatty(0) else None
    if old_attrs:
        tty.setraw(0)
    lock = threading.Lock()
    state: dict = {"seq": start_seq, "winch": True, "dead": False,
                   "readonly": role != "writer", "inflight": None,
                   "outbuf": deque(), "writer_state": None, "lifecycle": None}
    signal.signal(signal.SIGWINCH, lambda *_: state.__setitem__("winch", True))

    def notice(text: str) -> None:
        # Raw-mode-safe: \r\n keeps the line from staircasing the TUI.
        print(f"\r\n\x1b[1m[sc interface] {text}\x1b[0m\r\n",
              file=sys.stderr, flush=True)

    def pump() -> None:
        """Send the next buffered input frame iff nothing is unacknowledged
        (the one-unacked-frame rule, client side)."""
        with lock:
            if (state["dead"] or state["readonly"]
                    or state["inflight"] is not None or not state["outbuf"]):
                return
            data = state["outbuf"].popleft()
            seq = state["seq"]
            state["seq"] += 1
            state["inflight"] = seq
        ws.send(b"\x01" + seq.to_bytes(8, "big") + data)

    def acked(seq) -> None:
        """An ack or a non-terminal reject: the inflight frame is settled —
        release it and drain the local buffer."""
        with lock:
            if state["inflight"] == seq:
                state["inflight"] = None
        pump()

    def go_readonly(text: str) -> None:
        """Terminal input loss: flip read-only exactly once (loud), drop
        pending input, keep streaming output like the browser."""
        with lock:
            if state["readonly"]:
                return
            state["readonly"] = True
            state["outbuf"].clear()
            state["inflight"] = None
        notice(f"{text} — READ-ONLY from here; `sc interface take-control` "
               "regains the writer role")

    def sender() -> None:
        try:
            while not state["dead"]:
                if state["winch"]:
                    state["winch"] = False
                    rows, cols = _winsize()
                    ws.send(b"\x03" + rows.to_bytes(2, "big")
                            + cols.to_bytes(2, "big"))
                if not _stdin_ready(0.2):
                    continue
                data = _read_stdin()
                if not data:
                    state["dead"] = True
                    return
                with lock:
                    if not state["readonly"]:
                        state["outbuf"].append(data)
                pump()
        except Exception:
            state["dead"] = True

    def heartbeater() -> None:
        while not state["dead"]:
            time.sleep(HEARTBEAT_S)
            with lock:
                if state["readonly"] or state["dead"]:
                    return  # no lease left to keep alive
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
                continue
            msg = json.loads(message)
            mtype = msg.get("type")
            if mtype == "input_ack":
                acked(msg.get("seq"))              # routine: silent
            elif mtype == "input_reject":
                reason = msg.get("reason", "?")
                if reason in ("writer_revoked", "stale_generation"):
                    go_readonly(f"writer lease lost ({reason})")
                else:
                    notice(f"input seq {msg.get('seq')} rejected: {reason}")
                    acked(msg.get("seq"))
            elif mtype == "writer":
                new = msg.get("state")
                with lock:
                    prev = state["writer_state"]
                    state["writer_state"] = new
                if role == "writer" and new != "active":
                    go_readonly(f"writer lease lost (state: {new})")
                elif new != prev:
                    notice(f"writer {new}")
            elif mtype == "lifecycle":
                cur = (msg.get("lifecycle"), msg.get("composer"))
                with lock:
                    changed = cur != state["lifecycle"]
                    state["lifecycle"] = cur
                if changed:
                    notice(f"lifecycle {cur[0]}"
                           + (f" · composer {cur[1]}" if cur[1] else ""))
            elif mtype == "resync":
                notice(f"resync ({msg.get('reason', '?')})")
            elif mtype == "error":
                if msg.get("code") == "terminated":
                    break
                notice(f"error: {msg.get('code', '?')}")
            # heartbeat acks: routine, silent
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


# ------------------------------------------------------------------ recover

def _confirm(prompt: str, yes: bool) -> None:
    """A scoped interactive confirmation; --yes pre-answers it. Never
    prompt on a non-terminal — refuse instead (the eject.py convention)."""
    if yes:
        return
    if not sys.stdin.isatty():
        die("confirmation required on a terminal — re-run with --yes to "
            "pre-confirm", EXIT_REFUSED)
    if input(f"{prompt} [y/N] ").strip().lower() not in ("y", "yes"):
        die("aborted by operator", EXIT_REFUSED)


def _print_recovery_preview(preview: dict) -> None:
    ev = preview["evidence"]
    print(f"→ classification: {preview['classification']} "
          f"(legal actions: {', '.join(preview['legal_actions']) or 'none'})")
    sess = ev.get("session")
    if sess:
        print(f"  session {sess['session_id']} gen {sess['generation']}: "
              f"{sess['occupancy']}/{sess['lifecycle']} "
              f"({sess.get('harness') or '?'})")
    proc = ev.get("process") or {}
    if proc.get("pane_pid"):
        print(f"  process: pid {proc['pane_pid']} "
              f"ticks {proc['pane_start_ticks']} "
              f"pgid {proc.get('pgid')} — {proc['pid_state']}"
              + ("" if proc.get("pane_present") is None else
                 f", pane {'present' if proc['pane_present'] else 'gone'}"))
    archive = ev.get("archive")
    if archive:
        print(f"  archive {archive['archive_id']}: "
              f"{'open' if archive['ended_at'] is None else 'closed'}"
              + (" (active)" if archive.get("active") else ""))
    if ev.get("sprint_binding"):
        print(f"  sprint binding {ev['sprint_binding']['binding_id']} armed")
    print(f"  unread messages: {ev['unread_messages']} (left unread)")
    git = ev.get("git")
    if git:
        print(f"  worktree {git['worktree']}: branch {git['branch']}, "
              f"{git['dirty_tracked']} dirty tracked, {git['untracked']} "
              f"untracked, {git['unpushed_commits']} unpushed commit(s)")


def cmd_recover(args) -> int:
    shell = _find_shell(args.shell)
    shell_id = shell["shell_id"]
    shortname = shell.get("shortname")
    try:
        preview = api("GET", f"/api/interface/shells/{shell_id}/recovery")
    except ApiError as exc:
        _print_api_error(exc)
        raise SystemExit(EXIT_REFUSED) from exc

    classification = preview["classification"]
    legal = preview["legal_actions"]
    if not args.json:
        _print_recovery_preview(preview)

    mode = "force" if args.force else "recover"
    if mode not in legal:
        if args.json:
            _print_json({"preview": preview, "result": None})
        if classification == "available":
            print(f"→ {shortname} is available — nothing to recover")
            return 0
        print(f"→ {classification.replace('_', ' ')} — no automatic action; "
              "investigate the evidence above", file=sys.stderr)
        return EXIT_REFUSED

    body = {"observation_id": preview["observation_id"], "mode": mode,
            "preserve_worktree": not args.discard_worktree}
    if mode == "force":
        proc = preview["evidence"]["process"]
        _confirm(
            f"Force recover {shortname}: SIGTERM the exact process group of "
            f"pid {proc.get('pane_pid')} (ticks "
            f"{proc.get('pane_start_ticks')}, pgid {proc.get('pgid')}), "
            "SIGKILL after the bounded grace if it persists?",
            args.yes)
        body["confirm_force"] = True
    if args.discard_worktree:
        if not args.yes:
            _confirm(
                f"Discard ALL tracked and untracked file changes in "
                f"{shortname}'s worktree (unpushed commits refuse)?",
                False)
        body["discard_worktree"] = True
        body["confirm_shortname"] = shortname
    try:
        result = api("POST", f"/api/interface/shells/{shell_id}/recovery",
                     body)
    except ApiError as exc:
        _print_api_error(exc)
        if exc.code == "recovery_observation_stale":
            print("  state changed since the preview — re-run to preview "
                  "again", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from exc
    if args.json:
        _print_json({"preview": preview, "result": result})
        return 0
    sig = result.get("signaled")
    if sig and sig.get("signaled"):
        print(f"→ signaled pid {sig['pid']} (pgid {sig.get('pgid')}"
              f"{', escalated to SIGKILL' if sig.get('escalated') else ''})")
    closed = result.get("closed") or {}
    if closed.get("session"):
        print(f"→ session {closed['session']['session_id']} ended "
              f"({closed['session']['end_reason']})")
    if closed.get("archive"):
        print(f"→ archive {closed['archive']['archive_id']} closed")
    if closed.get("binding"):
        print(f"→ sprint binding {closed['binding']['binding_id']} released")
    for parked in closed.get("parked", []):
        print(f"→ parked ambiguous binding {parked['binding_id']}: "
              f"{parked['next_action']}")
    wt = result.get("worktree") or {}
    if wt.get("discarded"):
        print(f"→ worktree {wt['worktree']} changes discarded")
    elif wt.get("failed"):
        done = ", ".join(wt.get("completed") or []) or "nothing"
        print(f"→ worktree discard INCOMPLETE in {wt['worktree']}: completed "
              f"[{done}], failed at {wt['failed']['step']} "
              f"({wt['failed'].get('error', 'unknown error')}) — the closure "
              "is committed; finish the discard by hand", file=sys.stderr)
    else:
        print("→ worktree preserved")
    print(f"→ {shortname} is {result.get('availability')}")
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
        if detail.get("attachable"):
            return detail
        if detail.get("occupancy") == "occupied":
            die(f"session {session_id} is occupied but not attachable "
                f"({detail.get('state_reason') or 'identity unverified'}) — "
                "`sc interface reconcile <shell>` revalidates it")
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
            if exc.status != 409 or exc.code != "writer_held":
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

    sp = sub.add_parser("recover", help="preview + execute shell recovery")
    sp.add_argument("shell")
    sp.add_argument("--force", action="store_true",
                    help="terminate a verified-live exact process identity")
    sp.add_argument("--discard-worktree", action="store_true",
                    help="also discard tracked/untracked worktree changes "
                         "(refuses unpushed commits; never implied)")
    sp.add_argument("--yes", action="store_true",
                    help="pre-answer the scoped confirmations")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_recover)

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
