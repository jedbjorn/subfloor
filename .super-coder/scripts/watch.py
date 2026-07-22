#!/usr/bin/env python3
"""sc watch — the sprint-eventing surface: PR watches, the GitHub watcher
daemon, and the zero-token inbox watcher.

Spec: specs_sc/sprint-eventing.md. Four verbs, two vantages:

Shell-side (over the engine API, token identity — the `sc mem` doctrine):
    ./sc watch pr <owner/repo> <n> [--shell <shortname>]   register a watch
                                                           (defaults to the
                                                           calling shell)
    ./sc watch list [--all]                                live watches
    ./sc watch inbox [--interval 30] [--timeout 21600]     block until this
                                                           shell has unread
                                                           messages, then exit

Host-side (direct DB — engine code, like run.py; never run by a shell):
    ./sc watch daemon [--interval 75] [--once]             the fork's ONE
                                                           GitHub poller

The daemon is the fork's single GitHub subscriber: every live `watched_prs`
row is folded into ONE batched GraphQL query per poll (~1% of an authenticated
rate limit at 60–90s), each PR is diffed against its stored `last_seen`
fingerprint, and every transition becomes a `pr_event` row in shell_messages
addressed to the watch's shell. Events: checks concluded (green or red),
review submitted, merged, closed. On close — and on merge with no checks
still running — the final event is emitted and `closed_at` set: the watch
retires itself. A merge with checks still PENDING retains the watch (#375):
the merge event is emitted immediately, the watch stays live until the
rollup concludes, then the green/red event retires it — an early merge must
not swallow the terminal CI verdict the planner is waiting on. The daemon
only ever writes message rows + its own registry state: it never boots
shells, never marks anything read, never touches git. Each cycle it also beats a heartbeat row
(daemon_heartbeats) so `list`/`pr` can say whether anybody is actually
polling (#359) — a dead daemon otherwise leaves watches reporting "live"
with no eventing behind them.

A `pr_event` body is one line — repo, PR, what changed, head SHA. Detail
lives in `gh`; the message is the wake-up, not the payload.

`inbox` is the planner-side replacement for scheduled polling: it loops a
cheap local API read (zero harness turns, zero tokens) and exits the moment
the shell has unread mail — armed as a background task, its exit is the
wake-up. Re-arm after draining the inbox.

Supervision (daemon): `./sc launch` brings it up, `./sc down` stops it —
the broker model (nohup + pidfile via the sc dispatcher).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402

# API proxy — run.py injects these at boot (token = the shell's api_key).
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
SC_API_BASE = os.environ.get("SC_API_BASE", "")

CONCLUDED = {"SUCCESS", "FAILURE", "ERROR"}   # statusCheckRollup terminal states


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
    daemon is dead was the lying half of the dos-arch incident."""
    dead = "watches are NOT being polled (host: ./sc watch-daemon-up)"
    if not d or not d.get("beat_at"):
        return f"  daemon: never run — {dead}"
    age = _age_str(int(d.get("age_s") or 0))
    if d.get("stale"):
        return f"  daemon: STALE — last poll {age} ago ({d['beat_at']}Z); {dead}"
    return f"  daemon: live — last poll {age} ago (interval {d.get('interval_s')}s)"


def cmd_pr(args) -> int:
    _require_api()
    repo = args.repo.strip().strip("/")
    if repo.count("/") != 1:
        die(f"repo must be owner/name, got '{args.repo}'")
    payload: dict = {"repo": repo, "pr_number": args.number}
    if args.shell:
        payload["shell"] = args.shell
    r = _api("POST", "/_sc/watches", payload)
    who = args.shell or "you"
    if r.get("existing"):
        print(f"watch: {repo}#{args.number} already watched for {who} (watch #{r['watch_id']})")
    else:
        state = "re-armed" if r.get("reopened") else "registered"
        print(f"watch: {repo}#{args.number} {state} for {who} (watch #{r['watch_id']})")
        print("  (pr_event rows land in the shell's inbox as the daemon sees transitions)")
    d = r.get("daemon")
    if not d or not d.get("beat_at") or d.get("stale"):
        print(daemon_line(d))
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
    channel = None
    if os.environ.get("SC_SESSION_ACTIVE_CHANNEL") == "claude-inbox":
        try:
            binding_id = int(os.environ["SC_SESSION_BINDING_ID"])
        except (KeyError, ValueError):
            die("Claude inbox channel requires SC_SESSION_BINDING_ID")
        channel = _api(
            "POST",
            "/_sc/session-control/channel",
            {"binding_id": binding_id, "action": "register", "pid": os.getpid()},
        )

    try:
        while True:
            try:
                if channel:
                    _api_soft(
                        "POST",
                        "/_sc/session-control/channel",
                        {
                            "binding_id": channel["binding_id"],
                            "action": "heartbeat",
                            "pid": channel["pid"],
                            "start_ticks": channel["start_ticks"],
                        },
                    )
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
                    print(
                        f"watch: inbox watcher giving up — API unreachable ({e})",
                        file=sys.stderr,
                    )
                    return 3
            if deadline and time.monotonic() >= deadline:
                print(
                    "watch: inbox watcher timed out with no unread messages — "
                    "re-arm to keep watching."
                )
                return 2
            time.sleep(args.interval)
    finally:
        if channel:
            try:
                _api_soft(
                    "POST",
                    "/_sc/session-control/channel",
                    {
                        "binding_id": channel["binding_id"],
                        "action": "clear",
                        "pid": channel["pid"],
                        "start_ticks": channel["start_ticks"],
                    },
                )
            except _ApiDown:
                pass


class _ApiDown(Exception):
    pass


def _api_soft(
    method: str, path: str, payload: "dict | None" = None
) -> dict:
    """Like _api but raises _ApiDown instead of exiting — the inbox watcher
    rides out server restarts (e.g. an `./sc launch` bounce) instead of dying."""
    url = SC_API_BASE.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {SC_API_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        raise _ApiDown(str(exc)) from exc


# ── Daemon: fingerprint + diff (pure — the tested core) ──────────────────────

def build_query(prs: "list[tuple[str, int]]") -> str:
    """One batched GraphQL query over every (repo, pr) pair. Aliases are
    positional (r0, r1, …) so the response maps back by index regardless of
    characters in repo names."""
    parts = []
    for i, (repo, number) in enumerate(prs):
        owner, name = repo.split("/", 1)
        parts.append(
            f'r{i}: repository(owner: "{owner}", name: "{name}") {{'
            f' pullRequest(number: {number}) {{'
            f' state headRefOid'
            f' reviews(last: 1) {{ totalCount nodes {{ state }} }}'
            f' commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}'
            f' }} }}')
    return "query { " + " ".join(parts) + " }"


def fingerprint(node: "dict | None") -> "dict | None":
    """Collapse a GraphQL pullRequest node to the compared surface. None when
    the PR was unreadable this poll (deleted repo, bad number, partial error)."""
    if not node:
        return None
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") if commits else None
    reviews = node.get("reviews") or {}
    review_nodes = reviews.get("nodes") or []
    return {
        "state": node.get("state"),                       # OPEN | MERGED | CLOSED
        "sha": node.get("headRefOid"),
        "checks": (rollup or {}).get("state"),            # SUCCESS/FAILURE/ERROR/PENDING/None
        "reviews": reviews.get("totalCount") or 0,
        "review_state": review_nodes[0].get("state") if review_nodes else None,
    }


def diff_events(prev: "dict | None", cur: dict, repo: str, number: int) -> "tuple[list[str], bool]":
    """The daemon's core: (event bodies, terminal?) for one PR transition.

    prev None = first poll of a fresh watch: baseline silently, EXCEPT states
    that are already conclusive (checks concluded, merged, closed) — a watch
    registered moments after the transition must still wake its shell, or the
    event-driven loop drops its first link. Review history is never replayed
    from a baseline (stale reviews aren't a wake-up).

    A head-SHA change resets the checks comparison implicitly: the fingerprint
    compares (sha, checks) together, so a new push going green is a fresh
    transition even if the old head was green too.

    Merge is terminal ONLY once no checks are still running (#375): a PR
    merged while its rollup is PENDING keeps its watch — the merge event
    fires now, the checks conclusion fires (and retires the watch) when the
    already-running workflows finish. Retiring at merge dropped that verdict
    on the floor and silently stalled the planner's sprint gate. Close
    without merge retires immediately regardless — its pending checks get
    cancelled, no conclusion is coming."""
    events: list[str] = []
    sha7 = (cur.get("sha") or "")[:7]
    tag = f"{repo}#{number}"

    merged = cur.get("state") == "MERGED"
    checks = cur.get("checks")
    checks_pending = checks is not None and checks not in CONCLUDED  # PENDING/EXPECTED
    terminal = cur.get("state") == "CLOSED" or (merged and not checks_pending)

    checks_changed = prev is None or (prev.get("checks"), prev.get("sha")) != (checks, cur.get("sha"))
    if checks in CONCLUDED and checks_changed:
        word = "green" if checks == "SUCCESS" else "red"
        # On a retained post-merge watch this conclusion is the retiring event.
        tail = " — watch retired" if merged and prev is not None and prev.get("state") == "MERGED" else ""
        events.append(f"pr_event {tag}: checks {word} ({checks}) @ {sha7}{tail}")

    if prev is not None and (cur.get("reviews") or 0) > (prev.get("reviews") or 0):
        state = cur.get("review_state") or "REVIEW"
        events.append(f"pr_event {tag}: review submitted ({state}) @ {sha7}")

    if merged and (prev is None or prev.get("state") != "MERGED"):
        if checks_pending:
            events.append(f"pr_event {tag}: merged @ {sha7} — checks still pending, watch retained")
        else:
            events.append(f"pr_event {tag}: merged @ {sha7} — watch retired")
    elif cur.get("state") == "CLOSED" and (prev is None or prev.get("state") != "CLOSED"):
        events.append(f"pr_event {tag}: closed without merge — watch retired")
    return events, terminal


# ── Daemon: poll loop (host-side, direct DB + gh) ─────────────────────────────

def _gh_graphql(query: str) -> "dict | None":
    """Run the batched query through the host's authenticated `gh`. Returns the
    `data` object, or None on any failure (logged; the loop just tries again —
    a missed poll is a delayed event, never a lost one)."""
    try:
        out = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"watch daemon: gh unavailable ({e})", flush=True)
        return None
    if out.returncode != 0:
        # gh exits non-zero on partial GraphQL errors but still prints data —
        # use it if parseable (one bad watch must not blind the rest).
        try:
            return json.loads(out.stdout).get("data")
        except Exception:
            print(f"watch daemon: gh api graphql failed: {out.stderr.strip()[:300]}", flush=True)
            return None
    try:
        return json.loads(out.stdout).get("data")
    except Exception as e:
        print(f"watch daemon: unparseable gh output ({e})", flush=True)
        return None


def poll_once(con, fetch=_gh_graphql) -> int:
    """One daemon cycle: read live watches, one batched fetch, diff each PR,
    emit pr_event rows, persist fingerprints, retire terminal watches.
    Returns the number of events emitted. `fetch` is injectable for tests."""
    watches = con.execute(
        "SELECT watch_id, repo, pr_number, shell_id, last_seen "
        "FROM watched_prs WHERE closed_at IS NULL "
        "ORDER BY repo, pr_number, watch_id").fetchall()
    if not watches:
        return 0
    # One query node per distinct (repo, pr); a PR watched by several shells
    # fans its events out to each subscriber from the same fetch.
    prs = sorted({(w["repo"], w["pr_number"]) for w in watches})
    data = fetch(build_query(prs))
    if data is None:
        return 0
    snaps = {pr: fingerprint((data.get(f"r{i}") or {}).get("pullRequest"))
             for i, pr in enumerate(prs)}
    emitted = 0
    for w in watches:
        cur = snaps.get((w["repo"], w["pr_number"]))
        if cur is None:
            continue  # unreadable this poll — keep the watch, try next cycle
        prev = json.loads(w["last_seen"]) if w["last_seen"] else None
        events, terminal = diff_events(prev, cur, w["repo"], w["pr_number"])
        for body in events:
            con.execute(
                "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, kind) "
                "VALUES (?, ?, ?, 'pr_event')",
                (w["shell_id"], w["shell_id"], body))
            emitted += 1
        con.execute(
            "UPDATE watched_prs SET last_seen=?" +
            (", closed_at=datetime('now')" if terminal else "") +
            " WHERE watch_id=?",
            (json.dumps(cur), w["watch_id"]))
    con.commit()
    return emitted


def beat(con, interval: int) -> None:
    """Heartbeat (#359): one UPSERT per poll cycle — idle cycles included —
    so the watch surface can tell "no transition yet" from "nobody watching"."""
    con.execute(
        "INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
        "VALUES ('watch', datetime('now'), ?) "
        "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at, "
        "interval_s=excluded.interval_s", (interval,))
    con.commit()


def cmd_daemon(args) -> int:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        die(f"no usable DB at {DB_PATH} — rebuild with ./sc rebuild")
    interval = args.interval or int(os.environ.get("SC_WATCH_INTERVAL", "75"))
    print(f"watch daemon: polling every {interval}s · db {DB_PATH}", flush=True)
    while True:
        con = db_driver.connect(DB_PATH)
        try:
            # The beat must never block the poll: on a pre-0068 DB (code newer
            # than schema — a dev tree between migrate runs) the table is
            # missing, and a beat raising into the poll's except would turn a
            # working daemon into a dead-with-noise one. Liveness degrades to
            # "never run"; eventing keeps flowing.
            try:
                beat(con, interval)
            except Exception as e:
                print(f"watch daemon: heartbeat error ({e})", flush=True)
            n = poll_once(con)
            if n:
                print(f"watch daemon: {n} pr_event(s) emitted", flush=True)
        except Exception as e:
            # Never die on a poll: a daemon crash silently reverts the fork to
            # the polling world. Log and keep the loop.
            print(f"watch daemon: poll error ({e})", flush=True)
        finally:
            con.close()
        if args.once:
            return 0
        time.sleep(interval)


# ── arg parsing ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sc watch",
                                description="PR watches, inbox watcher, GitHub watcher daemon")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pr", help="register a PR watch (defaults to the calling shell)")
    sp.add_argument("repo", help="owner/name")
    sp.add_argument("number", type=int, help="PR number")
    sp.add_argument("--shell", help="subscribe another shell (e.g. the planner) instead of you")
    sp.set_defaults(fn=cmd_pr)

    sp = sub.add_parser("list", help="live watches (--all includes retired)")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("inbox", help="block until this shell has unread messages, then exit (arm as a background task)")
    sp.add_argument("--interval", type=int, default=30, help="poll seconds (default 30)")
    sp.add_argument("--timeout", type=int, default=21600,
                    help="give up after N seconds (default 21600 = 6h; 0 = never)")
    sp.set_defaults(fn=cmd_inbox)

    sp = sub.add_parser("daemon", help="HOST-side: the fork's one GitHub poller (launch/down supervise it)")
    sp.add_argument("--interval", type=int, default=0,
                    help="poll seconds (default $SC_WATCH_INTERVAL or 75)")
    sp.add_argument("--once", action="store_true", help="single poll cycle, then exit")
    sp.set_defaults(fn=cmd_daemon)
    return p


def main(argv: "list[str]") -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
