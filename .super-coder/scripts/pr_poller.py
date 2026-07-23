#!/usr/bin/env python3
"""pr_poller — the engine service's watched-PR poller (spec #20 task #85,
decision #19: the supervised service is the SOLE poller and engine-DB writer;
the legacy host `sc watch daemon` is retired).

What lives here:

- The normalized GitHub surface: one batched GraphQL query per repo, collapsed
  to a fingerprint of head SHA, PR state, check rollup, and review decision.
  Never prose, logs, commit messages, raw payloads, or tokens.
- `transitions()` — the semantic diff: (watch, transition, head SHA, state)
  keyed events with the same one-line `pr_event` bodies the sprint skills
  already teach. Merge is terminal only once no checks are still PENDING
  (#375); close-without-merge retires immediately.
- `baseline_read()` — registration's immediate GitHub read: no normalized
  baseline stored, no armed watch (the caller fails retryable).
- `poll_cycle()` — one bounded pass over ARMED watches only (live rows whose
  sprint_doc_id names an ACTIVE, unfrozen SPRINT document; unscoped legacy
  watches stay dormant until rebound). Per cycle: a `pr_poll_runs` audit row
  per repo, durable `pr_poll_observations` for transitions and blind windows,
  idempotent `pr_event` messages (dedupe_key) plus same-transaction wake items
  when a live binding owns the (sprint, planner) pair, fingerprint persistence,
  and terminal retirement. Per-repo failures back off capped without blocking
  other repos; a repo recovering from failure marks its next observations as
  blind windows (GitHub may have moved unobserved — convergence, not history).
- `Poller` — the service's scheduler thread: 30s default interval with jitter,
  only while ACTIVE sprint watches exist, plus one startup pass; explicit
  reconcile rides `poll_cycle(source='reconcile')` through the API. It beats
  the 'watch' heartbeat so `sc watch list` liveness keeps telling the truth.

It never injects terminal input, never marks a message read, never acts on a
PR — polling may create an event, nothing more.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time

CONCLUDED = {"SUCCESS", "FAILURE", "ERROR"}   # statusCheckRollup terminal states

DEFAULT_INTERVAL = int(os.environ.get("SC_PR_POLL_INTERVAL", "30"))
JITTER_FRACTION = 0.25          # sleep interval + uniform(0, 25%) — herd spread
BACKOFF_CAP_S = 900             # per-repo failure backoff ceiling (15 min)

_STATUS_ACTIVE = re.compile(r"^status:\s*ACTIVE\s*$", re.MULTILINE)


# ── GitHub read (the only network seam — injectable for tests) ───────────────

class GhResult:
    """One GitHub read: `data` (GraphQL data object) or a sanitized failure."""
    __slots__ = ("data", "error", "rate_limited")

    def __init__(self, data=None, error=None, rate_limited=False):
        self.data = data
        self.error = error            # sanitized one-liner; None on success
        self.rate_limited = rate_limited

    @property
    def ok(self) -> bool:
        return self.data is not None


def _sanitize_err(text: str) -> str:
    """One line, bounded — gh stderr carries no tokens, but the poller's
    error column is durable, so it gets the normalized-only discipline anyway."""
    return (text or "").strip().splitlines()[0][:200] if text else "unknown"


def gh_fetch(query: str) -> GhResult:
    """Run the batched query through the sandbox's authenticated `gh`. gh exits
    non-zero on partial GraphQL errors but still prints data — use it if
    parseable (one bad watch must not blind the rest)."""
    try:
        out = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return GhResult(error=_sanitize_err(f"gh unavailable: {e}"))
    rate_limited = "rate limit" in (out.stderr or "").lower()
    try:
        data = json.loads(out.stdout).get("data")
    except Exception:
        data = None
    if out.returncode != 0 and data is None:
        return GhResult(error=_sanitize_err(out.stderr), rate_limited=rate_limited)
    return GhResult(data=data, rate_limited=rate_limited)


# ── Normalized fingerprint + semantic transitions (pure — the tested core) ───

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


def baseline_read(repo: str, pr_number: int, fetch=None) -> "tuple[dict | None, str | None]":
    """Registration's immediate GitHub read: (normalized fingerprint, None) or
    (None, sanitized retryable error). No baseline, no armed watch — a watch
    armed without one would either replay history or drop its first event."""
    fetch = fetch or gh_fetch
    r = fetch(build_query([(repo, pr_number)]))
    if not r.ok:
        return None, r.error or "baseline read failed"
    fp = fingerprint((r.data.get("r0") or {}).get("pullRequest"))
    if fp is None:
        return None, "PR unreadable (bad repo/number or no access)"
    return fp, None


def transitions(prev: "dict | None", cur: dict, repo: str, number: int) -> "tuple[list[dict], bool]":
    """The poller's core: (events, terminal?) for one PR transition.

    Each event is {"key", "body"}: key is the semantic transition key
    (kind:state — with watch id and head SHA it forms the dedupe identity),
    body the one-line pr_event text. Detail lives in `gh`; the message is the
    wake-up, not the payload.

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
    events: list[dict] = []
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
        events.append({"key": f"checks:{checks}",
                       "body": f"pr_event {tag}: checks {word} ({checks}) @ {sha7}{tail}"})

    if prev is not None and (cur.get("reviews") or 0) > (prev.get("reviews") or 0):
        state = cur.get("review_state") or "REVIEW"
        events.append({"key": f"review:{state}",
                       "body": f"pr_event {tag}: review submitted ({state}) @ {sha7}"})

    if merged and (prev is None or prev.get("state") != "MERGED"):
        if checks_pending:
            body = f"pr_event {tag}: merged @ {sha7} — checks still pending, watch retained"
        else:
            body = f"pr_event {tag}: merged @ {sha7} — watch retired"
        events.append({"key": "merged:MERGED", "body": body})
    elif cur.get("state") == "CLOSED" and (prev is None or prev.get("state") != "CLOSED"):
        events.append({"key": "closed:CLOSED",
                       "body": f"pr_event {tag}: closed without merge — watch retired"})
    return events, terminal


# ── Sprint scoping ────────────────────────────────────────────────────────────

def active_sprint_doc_ids(con) -> "set[int]":
    """The unfrozen SPRINT documents declaring `status: ACTIVE` — the only
    scopes whose watches the poller arms (spec: GitHub polling is limited to
    active sprint watches)."""
    ids: set[int] = set()
    rows = con.execute(
        "SELECT document_id, body FROM documents "
        "WHERE kind='doc' AND frozen=0 AND title LIKE 'SPRINT:%'").fetchall()
    for doc_id, body in rows:
        if body and _STATUS_ACTIVE.search(body):
            ids.add(doc_id)
    return ids


def is_active_sprint(con, doc_id: int) -> bool:
    r = con.execute(
        "SELECT body FROM documents WHERE document_id=? AND kind='doc' "
        "AND frozen=0 AND title LIKE 'SPRINT:%'", (doc_id,)).fetchone()
    return bool(r and r[0] and _STATUS_ACTIVE.search(r[0]))


def armed_watches(con) -> list:
    """Live watches scoped to an ACTIVE sprint — the poller's whole world.
    Unscoped legacy watches stay dormant until explicitly rebound."""
    active = active_sprint_doc_ids(con)
    if not active:
        return []
    marks = ",".join("?" for _ in active)
    return con.execute(
        "SELECT watch_id, repo, pr_number, shell_id, last_seen, sprint_doc_id "
        f"FROM watched_prs WHERE closed_at IS NULL AND sprint_doc_id IN ({marks}) "
        "ORDER BY repo, pr_number, watch_id", tuple(sorted(active))).fetchall()


# ── Per-repo backoff + blind windows ─────────────────────────────────────────

class PollerState:
    """Volatile per-repo poll health (a service restart resets it — the
    durable half is the run/observation audit). `failures` drives a capped
    exponential skip; any failure since the last success makes the next
    successful cycle's observations blind windows."""

    def __init__(self):
        self._repos: dict[str, dict] = {}

    def _r(self, repo: str) -> dict:
        return self._repos.setdefault(repo, {"failures": 0, "skip_until": 0.0})

    def due(self, repo: str, now: float) -> bool:
        return now >= self._r(repo)["skip_until"]

    def record_failure(self, repo: str, now: float, interval: int) -> int:
        r = self._r(repo)
        r["failures"] += 1
        r["skip_until"] = now + min(interval * (2 ** r["failures"]), BACKOFF_CAP_S)
        return r["failures"]

    def record_success(self, repo: str) -> bool:
        """True when this success follows ≥1 failure — a blind window: GitHub
        may have moved while polls were failing/skipped."""
        r = self._r(repo)
        blind = r["failures"] > 0
        r["failures"] = 0
        r["skip_until"] = 0.0
        return blind


# ── Heartbeat (#359 — same row the legacy daemon beat; liveness UI unchanged) ─

def beat(con, interval: int) -> None:
    con.execute(
        "INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
        "VALUES ('watch', datetime('now'), ?) "
        "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at, "
        "interval_s=excluded.interval_s", (interval,))
    con.commit()


# ── The poll cycle ────────────────────────────────────────────────────────────

def _alert(con, *, severity: str, reason: str, watch_id=None) -> None:
    """Raise an alert, deduplicated while open (partial unique index). Local
    helper — interface_broker._alert predates watch-scoped alerts."""
    dedupe = f"-|-|{watch_id or '-'}|-|{reason}"
    con.execute(
        "INSERT OR IGNORE INTO planner_alerts "
        "(watch_id, severity, reason, dedupe_key) VALUES (?,?,?,?)",
        (watch_id, severity, reason, dedupe))


def _emit_event(con, watch, event: dict, head_sha: str) -> bool:
    """One semantic transition → idempotent pr_event + same-transaction wake
    item. Dedupe keyed (watch, transition, head SHA, state) via the message's
    dedupe_key partial unique index: a repeated key is a no-op, so a replayed
    poll or a baseline race can never double-wake the planner. Returns True
    when the event was newly emitted."""
    dedupe_key = f"pr-event|{watch['watch_id']}|{event['key']}|{head_sha}"
    try:
        cur = con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id, to_shell_id, body, kind, sprint_doc_id, dedupe_key) "
            "VALUES (?, ?, ?, 'pr_event', ?, ?)",
            (watch["shell_id"], watch["shell_id"], event["body"],
             watch["sprint_doc_id"], dedupe_key))
    except sqlite3.IntegrityError:
        return False  # the dedupe index — already emitted
    message_id = cur.lastrowid
    binding = con.execute(
        "SELECT binding_id FROM sprint_planner_bindings "
        "WHERE sprint_doc_id=? AND planner_shell_id=? AND released_at IS NULL",
        (watch["sprint_doc_id"], watch["shell_id"])).fetchone()
    if binding is not None:
        con.execute(
            "INSERT OR IGNORE INTO planner_wake_items (binding_id, message_id) "
            "VALUES (?, ?)", (binding[0], message_id))
    return True


def poll_cycle(con, fetch=None, source: str = "scheduler",
               state: "PollerState | None" = None,
               interval: int = DEFAULT_INTERVAL, now: "float | None" = None) -> dict:
    """One bounded pass over armed watches. Per repo: one batched read, one
    pr_poll_runs audit row, transition/blind-window observations, idempotent
    events, fingerprint persistence, terminal retirement. A failed repo backs
    off (capped) without blocking the others. Returns a counts summary."""
    fetch = fetch or gh_fetch
    state = state if state is not None else PollerState()
    now = now if now is not None else time.monotonic()
    summary = {"watches": 0, "repos": 0, "skipped_backoff": 0,
               "events": 0, "errors": 0, "retired": 0}
    watches = armed_watches(con)
    summary["watches"] = len(watches)
    if not watches:
        return summary

    by_repo: dict[str, list] = {}
    for w in watches:
        by_repo.setdefault(w["repo"], []).append(w)

    for repo in sorted(by_repo):
        repo_watches = by_repo[repo]
        if not state.due(repo, now):
            summary["skipped_backoff"] += 1
            continue
        summary["repos"] += 1
        run_id = con.execute(
            "INSERT INTO pr_poll_runs (repo, source, watch_count) VALUES (?,?,?)",
            (repo, source, len(repo_watches))).lastrowid
        con.commit()

        prs = sorted({(w["repo"], w["pr_number"]) for w in repo_watches})
        r = fetch(build_query(prs))
        if not r.ok:
            failures = state.record_failure(repo, now, interval)
            con.execute(
                "UPDATE pr_poll_runs SET finished_at=datetime('now'), status=?, "
                "error=? WHERE run_id=?",
                ("rate_limited" if r.rate_limited else "error", r.error, run_id))
            _alert(con, severity="warning", reason="pr_poll_failure",
                   watch_id=repo_watches[0]["watch_id"])
            con.commit()
            summary["errors"] += 1
            if failures >= 3:
                _alert(con, severity="critical",
                       reason="pr_poll_backoff_escalated",
                       watch_id=repo_watches[0]["watch_id"])
                con.commit()
            continue

        blind = 1 if state.record_success(repo) else 0
        con.execute(
            "UPDATE pr_poll_runs SET finished_at=datetime('now'), status='ok' "
            "WHERE run_id=?", (run_id,))
        snaps = {pr: fingerprint((r.data.get(f"r{i}") or {}).get("pullRequest"))
                 for i, pr in enumerate(prs)}
        for w in repo_watches:
            cur = snaps.get((w["repo"], w["pr_number"]))
            if cur is None:
                continue  # unreadable this poll — keep the watch, try next cycle
            prev = json.loads(w["last_seen"]) if w["last_seen"] else None
            events, terminal = transitions(prev, cur, w["repo"], w["pr_number"])
            # Durable only with a transition or a blind-window marker (the
            # snapshot row filter); a quiet successful poll is noise.
            if events or blind:
                con.execute(
                    "INSERT INTO pr_poll_observations "
                    "(watch_id, run_id, head_sha, fingerprint, transition, "
                    " blind_window) VALUES (?,?,?,?,?,?)",
                    (w["watch_id"], run_id, cur.get("sha"), json.dumps(cur),
                     ",".join(e["key"] for e in events) or None, blind))
            for e in events:
                if _emit_event(con, w, e, cur.get("sha") or ""):
                    summary["events"] += 1
                    emitted.append((w["sprint_doc_id"], e, cur.get("sha") or ""))
            con.execute(
                "UPDATE watched_prs SET last_seen=?" +
                (", closed_at=datetime('now')" if terminal else "") +
                " WHERE watch_id=?",
                (json.dumps(cur), w["watch_id"]))
            if terminal:
                summary["retired"] += 1
        con.commit()
    return summary


# ── The service scheduler ─────────────────────────────────────────────────────

class Poller(threading.Thread):
    """The service's bounded PR-poll scheduler: 30s + jitter, only while
    ACTIVE sprint watches exist, plus one startup pass. It polls GitHub and
    writes the engine DB — it never touches a model, a terminal, or git.
    A fork without `gh` disables itself exactly like the legacy daemon did."""

    def __init__(self, db_path, interval: int = DEFAULT_INTERVAL, fetch=None,
                 connect=None):
        super().__init__(name="pr-poller", daemon=True)
        self._db_path = str(db_path)
        self._interval = interval
        self._fetch = fetch
        self._connect = connect
        self._stop_event = threading.Event()
        self.state = PollerState()

    def stop(self) -> None:
        self._stop_event.set()

    def _db(self):
        if self._connect is not None:
            return self._connect()
        import db_driver
        return db_driver.connect(self._db_path)

    def run(self) -> None:  # pragma: no cover — thread loop; scheduler tests drive it
        if self._fetch is None and shutil.which("gh") is None:
            print("pr-poller: gh CLI not found — PR polling disabled "
                  "(install gh + login to enable)", flush=True)
            return
        source = "startup"
        while not self._stop_event.is_set():
            try:
                con = self._db()
                try:
                    try:
                        beat(con, self._interval)
                    except Exception as e:
                        # The beat is ancillary liveness; polling is the
                        # mission (#359). A beat raising into the cycle's
                        # except would turn a working poller into a
                        # dead-with-noise one — log and keep polling.
                        print(f"pr-poller: heartbeat error ({e})", flush=True)
                    # The DB read is cheap and local; the bounded GitHub poll
                    # happens only while ACTIVE sprint watches exist.
                    if armed_watches(con):
                        n = poll_cycle(con, fetch=self._fetch, source=source,
                                       state=self.state, interval=self._interval)
                        if n["events"] or n["errors"]:
                            print(f"pr-poller: {n}", flush=True)
                finally:
                    con.close()
            except Exception as e:
                # Never die on a cycle: a dead poller silently reverts the
                # fork to the polling world. Log and keep the loop.
                print(f"pr-poller: cycle error ({e})", flush=True)
            source = "scheduler"
            self._stop_event.wait(self._interval +
                            random.uniform(0, self._interval * JITTER_FRACTION))
