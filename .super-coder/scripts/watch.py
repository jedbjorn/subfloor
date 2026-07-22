#!/usr/bin/env python3
"""sc watch — the sprint-eventing surface: PR watches and the zero-token
inbox watcher.

Spec: specs_sc/sprint-eventing.md + spec #20 (polling cutover). Three verbs,
one vantage — everything shell-side rides the engine API (token identity,
the `sc mem` doctrine):

    ./sc watch pr <owner/repo> <n> [--shell <shortname>] [--sprint <doc-id>]
                                                           register a watch
                                                           (defaults to the
                                                           calling shell;
                                                           --sprint arms it
                                                           to an ACTIVE sprint)
    ./sc watch list [--all]                                live watches
    ./sc watch inbox [--interval 30] [--timeout 21600]     block until this
                                                           shell has unread
                                                           messages, then exit
    ./sc watch reconcile                                   explicit one-shot
                                                           poll (operator)

Polling: the supervised engine service is the fork's SOLE GitHub poller
(spec #20 task #85, decision #19) — the legacy host `sc watch daemon` (a
direct-DB writer) is RETIRED: the `daemon` verb here now prints the retirement
notice and exits clean so legacy nohup/systemd supervision stops instead of
racing the service. Registration performs an immediate GitHub read and stores
the normalized baseline before arming; the service then polls armed watches
(live, scoped to an ACTIVE sprint) on a bounded interval, and every semantic
transition becomes an idempotent `pr_event` row (+ wake item when a binding is
armed) addressed to the watch's shell. Events: checks concluded (green or
red), review submitted, merged, closed. On close — and on merge with no
checks still running — the final event is emitted and `closed_at` set: the
watch retires itself. A merge with checks still PENDING retains the watch
(#375). Unscoped legacy watches stay readable but dormant until rebound to an
ACTIVE sprint. The poller only ever writes message rows + its own registry
state: it never boots shells, never marks anything read, never touches git,
never injects terminal input.

A `pr_event` body is one line — repo, PR, what changed, head SHA. Detail
lives in `gh`; the message is the wake-up, not the payload.

`inbox` is the planner-side replacement for scheduled polling: it loops a
cheap local API read (zero harness turns, zero tokens) and exits the moment
the shell has unread mail — armed as a background task, its exit is the
wake-up. Re-arm after draining the inbox.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ENGINE / "scripts"))
import pr_poller  # noqa: E402

# API proxy — run.py injects these at boot (token = the shell's api_key).
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
SC_API_BASE = os.environ.get("SC_API_BASE", "")

# The normalized-GitHub core moved to pr_poller with the polling cutover
# (spec #20 task #85); re-exported here for the suites that import it from
# watch (the pure diff behavior is unchanged).
CONCLUDED = pr_poller.CONCLUDED
build_query = pr_poller.build_query
fingerprint = pr_poller.fingerprint
beat = pr_poller.beat


def diff_events(prev, cur, repo, number):
    """Compat adapter over pr_poller.transitions: (event bodies, terminal?)."""
    events, terminal = pr_poller.transitions(prev, cur, repo, number)
    return [e["body"] for e in events], terminal


def die(msg: str) -> "NoReturn":  # noqa: F821
    sys.exit(f"watch: {msg}")


# ── API client (pr / list / inbox — the shell-side verbs) ────────────────────

def _require_api() -> None:
    if SC_API_TOKEN and SC_API_BASE:
        return
    missing = [n for n, v in (("SC_API_BASE", SC_API_BASE),
                              ("SC_API_TOKEN", SC_API_TOKEN)) if not v]
    die(f"the engine API is required but {' + '.join(missing)} "
        f"{'is' if len(missing) == 1 else 'are'} unset — this shell isn't API-wired. "
        f"Boot via `./sc enter` with the server up (`./sc launch`).")


def _api(method: str, path: str, payload: "dict | None" = None) -> dict:
    url = SC_API_BASE.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers: dict = {"Authorization": f"Bearer {SC_API_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except Exception:
            msg = e.reason
        die(f"API {method} {path} → HTTP {e.code}: {msg}")
    except Exception as exc:
        die(f"API unreachable ({SC_API_BASE}): {exc}")


def _age_str(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def daemon_line(d: "dict | None") -> str:
    """Render the /_sc/watches `daemon` block as the liveness line (#359):
    a watch is only as live as its poller, and `list` saying "live" while the
    poller is dead was the lying half of the dos-arch incident. The poller is
    the engine service's scheduler thread since the cutover (decision #19)."""
    dead = "watches are NOT being polled (engine service poller down — restart: ./sc restart)"
    if not d or not d.get("beat_at"):
        return f"  poller: never run — {dead}"
    age = _age_str(int(d.get("age_s") or 0))
    if d.get("stale"):
        return f"  poller: STALE — last poll {age} ago ({d['beat_at']}Z); {dead}"
    return f"  poller: live — last poll {age} ago (interval {d.get('interval_s')}s)"


def cmd_pr(args) -> int:
    _require_api()
    repo = args.repo.strip().strip("/")
    if repo.count("/") != 1:
        die(f"repo must be owner/name, got '{args.repo}'")
    payload: dict = {"repo": repo, "pr_number": args.number}
    if args.shell:
        payload["shell"] = args.shell
    if args.sprint:
        payload["sprint_doc_id"] = args.sprint
    r = _api("POST", "/_sc/watches", payload)
    who = args.shell or "you"
    if r.get("existing"):
        print(f"watch: {repo}#{args.number} already watched for {who} (watch #{r['watch_id']})")
    elif r.get("rebound"):
        print(f"watch: {repo}#{args.number} rebound to sprint doc {args.sprint} for {who} "
              f"(watch #{r['watch_id']}, baseline armed)")
    else:
        print(f"watch: {repo}#{args.number} registered for {who} (watch #{r['watch_id']}, baseline armed)")
        print("  (pr_event rows land in the shell's inbox as the service poller sees transitions)")
    d = r.get("daemon")
    if not d or not d.get("beat_at") or d.get("stale"):
        print(daemon_line(d))
    return 0


def cmd_reconcile(args) -> int:
    """Explicit one-shot poll of every armed watch (operator recovery)."""
    _require_api()
    r = _api("POST", "/_sc/watches/reconcile")
    print(f"watch: reconcile — {r.get('watches', 0)} armed watch(es), "
          f"{r.get('repos', 0)} repo(s) polled, {r.get('events', 0)} event(s), "
          f"{r.get('errors', 0)} error(s), {r.get('skipped_backoff', 0)} in backoff")
    return 0


def cmd_list(args) -> int:
    _require_api()
    r = _api("GET", "/_sc/watches" + ("?all=1" if args.all else ""))
    print(daemon_line(r.get("daemon")))
    ws = r.get("watches", [])
    if not ws:
        print("watch: no watches" if args.all else "watch: no live watches")
        return 0
    for w in ws:
        state = f"closed {w['closed_at']}" if w.get("closed_at") else "live"
        if not w.get("closed_at"):
            if w.get("sprint_doc_id"):
                state += f", sprint #{w['sprint_doc_id']}"
                state += " (armed)" if w.get("armed") else " (dormant — sprint not ACTIVE)"
            else:
                state += ", dormant (unscoped — rebind with `sc watch pr … --sprint <doc>`)"
        print(f"  #{w['watch_id']} {w['repo']}#{w['pr_number']} → {w['shortname']} "
              f"({state}, since {w['created_at']})")
    return 0


def cmd_inbox(args) -> int:
    """Block until this shell (the token) has unread messages, then exit 0.

    The zero-token watcher: each poll is one local HTTP GET — no harness turn,
    no provider call. Armed as a background task, the exit IS the wake-up.
    Exit codes: 0 = unread mail waiting · 2 = timeout, inbox still empty ·
    3 = API unreachable for ~10 consecutive polls."""
    _require_api()
    deadline = time.monotonic() + args.timeout if args.timeout else None
    failures = 0
    while True:
        try:
            r = _api_soft("GET", "/_sc/mem/messages")
            failures = 0
            unread = [m for m in r.get("messages", []) if not m.get("read_at")]
            if unread:
                print(f"watch: {len(unread)} unread message(s) — inbox watcher fired:")
                for m in unread[:10]:
                    kind = m.get("kind") or "shell"
                    first = (m.get("body") or "").splitlines()[0][:120]
                    print(f"  [#{m['message_id']}] {kind} · {first}")
                print("  → `sc mem message check`, act, mark-read, then re-arm the watcher.")
                return 0
        except _ApiDown as e:
            failures += 1
            if failures >= 10:
                print(f"watch: inbox watcher giving up — API unreachable ({e})", file=sys.stderr)
                return 3
        if deadline and time.monotonic() >= deadline:
            print("watch: inbox watcher timed out with no unread messages — re-arm to keep watching.")
            return 2
        time.sleep(args.interval)


class _ApiDown(Exception):
    pass


def _api_soft(method: str, path: str) -> dict:
    """Like _api but raises _ApiDown instead of exiting — the inbox watcher
    rides out server restarts (e.g. an `./sc launch` bounce) instead of dying."""
    url = SC_API_BASE.rstrip("/") + path
    req = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bearer {SC_API_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        raise _ApiDown(str(exc)) from exc


# ── Retired daemon verb (spec #20 task #85, decision #19) ────────────────────

def cmd_daemon(args) -> int:
    """RETIRED — the host watch daemon (a direct-DB writer) is cut over: the
    supervised engine service is the fork's sole GitHub poller. Exit CLEAN (0)
    so legacy nohup/systemd supervision (Restart=on-failure) stops instead of
    crash-looping against the new single-poller world."""
    print("watch daemon: RETIRED (spec #20, decision #19) — the engine service "
          "is now the sole PR poller.\n"
          "  nothing to run; polling lives in the supervised API service "
          "(starts with ./sc launch).\n"
          "  remove legacy supervision: ./sc watch-daemon-down · "
          "./sc watch-daemon-uninstall", flush=True)
    return 0


# ── arg parsing ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sc watch",
                                description="PR watches, inbox watcher, explicit reconcile")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pr", help="register a PR watch (defaults to the calling shell)")
    sp.add_argument("repo", help="owner/name")
    sp.add_argument("number", type=int, help="PR number")
    sp.add_argument("--shell", help="subscribe another shell (e.g. the planner) instead of you")
    sp.add_argument("--sprint", type=int, default=0, metavar="DOC_ID",
                    help="arm the watch to an ACTIVE sprint document (polls only while it stays ACTIVE)")
    sp.set_defaults(fn=cmd_pr)

    sp = sub.add_parser("list", help="live watches (--all includes retired)")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("reconcile", help="explicit one-shot poll of every armed watch (operator)")
    sp.set_defaults(fn=cmd_reconcile)

    sp = sub.add_parser("inbox", help="block until this shell has unread messages, then exit (arm as a background task)")
    sp.add_argument("--interval", type=int, default=30, help="poll seconds (default 30)")
    sp.add_argument("--timeout", type=int, default=21600,
                    help="give up after N seconds (default 21600 = 6h; 0 = never)")
    sp.set_defaults(fn=cmd_inbox)

    sp = sub.add_parser("daemon", help="RETIRED (decision #19) — prints the cutover notice and exits clean")
    sp.add_argument("--interval", type=int, default=0, help=argparse.SUPPRESS)
    sp.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(fn=cmd_daemon)
    return p


def main(argv: "list[str]") -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
