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
  sprint_doc_id names a LIVE sprint per `sprint_state`; unscoped legacy
  watches stay dormant until rebound). Per cycle: a `pr_poll_runs` audit row
  per repo, durable `pr_poll_observations` for transitions and blind windows,
  idempotent `pr_event` messages (dedupe_key), fingerprint persistence, and
  terminal retirement. Per-repo failures back off capped without blocking
  other repos; a repo recovering from failure marks its next observations as
  blind windows (GitHub may have moved unobserved — convergence, not history).
- `Poller` — the service's scheduler thread. GitHub polling keeps its 30s
  watch-gated cadence; worker-expectation reconciliation runs every 10 minutes
  from structured live units even before a PR or watch exists. Explicit PR
  reconcile still rides `poll_cycle(source='reconcile')` through the API.
  PR polling beats the status-visible watch heartbeat. Worker reconciliation
  records tick completion in a separate row rendered alongside it by `sc watch list`.

It never injects terminal input, never marks a message read, never acts on a
PR, and never mutates the sprint board. PR polling may create an event; the
worker reconciler returns report-only readings for the alert unit to consume.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import activity_readers
import sprint_state
from quota_probes import dispatch as quota_dispatch
from sprint_units import TERMINAL_UNIT_STATES

CONCLUDED = {"SUCCESS", "FAILURE", "ERROR"}   # statusCheckRollup terminal states

DEFAULT_INTERVAL = int(os.environ.get("SC_PR_POLL_INTERVAL", "30"))
DEFAULT_RECONCILE_INTERVAL = int(
    os.environ.get("SC_RECONCILE_INTERVAL", "600")
)
JITTER_FRACTION = 0.25          # sleep interval + uniform(0, 25%) — herd spread
BACKOFF_CAP_S = 900             # per-repo failure backoff ceiling (15 min)
NO_PROGRESS_WINDOW = timedelta(minutes=20)
START_GRACE = timedelta(minutes=20)


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


def transitions(prev: "dict | None", cur: dict, repo: str, number: int,
                unit: "str | None" = None) -> "tuple[list[dict], bool]":
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
    # `unit=U3` in the header is the structured ref made visible (H-13): the
    # watch knows which unit its PR belongs to, so the planner reading the row
    # — and the reconciler's prose fallback — both get the answer instead of
    # inferring it. Omitted entirely when the watch carries no unit, because a
    # header that says `unit=None` is a claim, and an unlinked watch is making
    # none.
    tag = f"{repo}#{number}" + (f" unit={unit}" if unit else "")

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


# ── Worker-expectation classification (spec 58, U4) ─────────────────────────

def _utc(value) -> "datetime | None":
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _invalid_evidence_fields(evidence: activity_readers.Evidence) -> set[str]:
    """Resolve reader markers through U3's exported mapping, never by treating
    marker text as an Evidence field name."""
    invalid: set[str] = set()
    for marker in evidence.unreadable:
        fields = activity_readers.UNREADABLE_FIELDS.get(marker)
        if not fields:
            invalid.add("__unknown__")
            continue
        invalid.update(fields)
    return invalid


def classify(
    evidence: activity_readers.Evidence,
    now,
    *,
    window: timedelta = NO_PROGRESS_WINDOW,
    grace: timedelta = START_GRACE,
) -> str:
    """Apply spec 58's precedence ladder without side effects.

    The clocks are intentionally explicit:
    - result evidence compares with the unit state clock;
    - recent work compares with ``now``;
    - durable completion and branch grace use the boot epoch;
    - the no-progress window starts at the newest boot, state, work, or
      durable-write event — the last of these being the only signal a role
      with no code surface can produce.
    """
    current = _utc(now) or datetime.now(timezone.utc)
    epoch = _utc(evidence.epoch)
    state_clock = _utc(evidence.state_changed_at)
    invalid = _invalid_evidence_fields(evidence)

    # Rule 1.  These three inputs fence every reliable walk through the ladder:
    # without the boot/state clocks timing is undefined, and without the result
    # read a lower rule could outrank a report.  An unknown marker is an
    # undeclared U3/U4 interface and therefore indeterminate too.
    if "__unknown__" in invalid:
        return "indeterminate"
    if invalid.intersection(
        {"epoch", "state_changed_at", "last_result_row_at"}
    ):
        return "indeterminate"
    if epoch is None or state_clock is None:
        return "indeterminate"

    # A failed once-per-tick refresh invalidates every git-derived decider.
    # Do not turn stale refs into a confident "working" or "not_started".
    git_deciders = {
        "branch_present",
        "commits_since_epoch",
        "last_work_at",
    }
    if evidence.edits_code and git_deciders.issubset(invalid):
        return "indeterminate"

    # Rule 2 — STATE clock.  This deliberately precedes recent work.
    result_at = _utc(evidence.last_result_row_at)
    if result_at is not None and result_at >= state_clock:
        return "reported"

    # Rule 3 — WORK-EVENT clock.  ``dirty`` is never consulted: a delete-only
    # tree may legally be dirty with an untimed last_work_at.
    last_work = _utc(evidence.last_work_at)
    if (
        evidence.edits_code
        and last_work is not None
        and last_work >= current - window
    ):
        return "working"

    # Rule 4 — BOOT clock. Either the archive lifecycle or an absent headless
    # process closes the session.
    session_over = (
        _utc(evidence.session_ended_at) is not None
        or evidence.process_present is False
    )
    durable_at = _utc(evidence.last_durable_write_at)
    if session_over and durable_at is not None and durable_at >= epoch:
        return "work_complete_unreported"

    # Rule 5 — BOOT clock and the 20-minute grace floor.  `edits_code` is
    # explicit here: only a code expectation can be `not_started`, so a reviewer
    # is never graded on whether the dev has pushed yet (H-16).
    if (
        evidence.edits_code
        and evidence.branch_declared is not None
        and evidence.branch_present is False
        and current > epoch + max(grace, START_GRACE)
    ):
        return "not_started"

    # Rule 6 — newest BOOT, STATE, WORK-EVENT, or DURABLE-WRITE clock.
    # Explanation-tier marker/CPU observations never enter this maximum.
    #
    # The durable write is here — and ONLY here — because this floor measures
    # SILENCE, and a durable write is not silence (spec #76 H-16, flag #364
    # defect b).  `last_durable_write_at` was read on every tick and consulted
    # at exactly one place: Rule 4, behind `session_over`.  A live planner emits
    # `task` rows and never `result`, and has no branch, so it walked past
    # Rules 2, 3 and 5 into this floor measured from a boot clock it had long
    # since spoken past, and stuck at `checkup` for the rest of the sprint where
    # no healthy signal could resolve it.  A separate "recent durable write is
    # working" rule above Rule 5 was drafted and DELETED: it decided nothing
    # this maximum does not already decide, except to convert a dev's
    # declared-but-absent branch from `not_started` into `working`, which is a
    # semantic change no requirement here asks for.
    last_evidence = max(
        stamp
        for stamp in (epoch, state_clock, last_work, durable_at)
        if stamp is not None
    )
    if current > last_evidence + max(window, NO_PROGRESS_WINDOW):
        return "checkup"

    # Rule 7.
    return "working"


@dataclass(frozen=True)
class Expectation:
    sprint_doc_id: int
    unit_id: "int | None"
    seq: "str | None"
    role: str
    shell_id: int
    shell: object
    unit: object

    @property
    def key(self) -> tuple:
        """Confirmation identity, not planner-alert identity.

        The assignee is intentional here: a reassignment must earn two fresh
        observations. Alert identity is constructed separately from immutable
        unit_id and deliberately excludes shell_id.
        """
        return (
            self.sprint_doc_id,
            self.unit_id,
            self.role,
            self.shell_id,
        )


@dataclass
class ReconciliationReading:
    """One report-only comparison.  U5 owns turning confirmed signals into
    messages and alerts; U4 never writes either surface."""

    expectation: Expectation
    signal: str
    confirmed: bool
    evidence: activity_readers.Evidence
    measurement: dict[str, object]
    observed_at: datetime
    explanation: "str | None"


def _activity_explanation(evidence: activity_readers.Evidence) -> list[str]:
    """Render WHY-tier activity facts without classifying either one."""
    marker = _utc(evidence.marker_at)
    transcript = (
        f"transcript mtime as of {marker.isoformat()}"
        if marker is not None
        else "transcript mtime unavailable"
    )
    shape = evidence.launch_shape or "unavailable"
    delta = (
        f"{evidence.cpu_delta:g}"
        if evidence.cpu_delta is not None
        else "unavailable"
    )
    return [
        transcript,
        f"cpu launch_shape={shape} delta_ticks={delta}",
    ]


def _quota_exhausted(row) -> "bool | None":
    percent = _row(row, "used_percent")
    if percent is not None:
        try:
            return float(percent) >= 100
        except (TypeError, ValueError):
            return None

    used = _row(row, "used")
    limit = _row(row, "limit_value")
    if used is None or limit is None:
        return None
    try:
        limit_value = float(limit)
        if limit_value <= 0:
            return None
        return float(used) >= limit_value
    except (TypeError, ValueError):
        return None


def _provider_explanation(con) -> list[str]:
    """Render durable quota facts beside process-local probe status."""
    groups: dict[str, list[sqlite3.Row]] = {}
    durable_available = True
    try:
        rows = con.execute(
            "SELECT a.provider, w.window_kind, w.scope, w.used_percent, "
            "w.used, w.limit_value, w.resets_at, w.captured_at "
            "FROM harness_quota_window w "
            "JOIN harness_quota_account a ON a.account_pk=w.account_pk "
            "ORDER BY a.provider, w.captured_at, w.window_kind, w.scope"
        ).fetchall()
    except sqlite3.Error:
        rows = []
        durable_available = False
    for row in rows:
        groups.setdefault(_row(row, "provider"), []).append(row)

    latest_statuses = quota_dispatch.latest_statuses()
    parts: list[str] = []
    for provider in quota_dispatch.PROVIDERS:
        windows = groups.get(provider, [])
        if not durable_available or not windows:
            parts.append(f"{provider} quota unavailable")
        else:
            captured_at = max(_row(row, "captured_at") for row in windows)
            current = [
                row
                for row in windows
                if _row(row, "captured_at") == captured_at
            ]
            for row in current:
                scope = _row(row, "scope")
                window = _row(row, "window_kind")
                if scope:
                    window += f":{scope}"
                exhausted = _quota_exhausted(row)
                if exhausted is None:
                    parts.append(
                        f"{provider} quota unavailable window={window} "
                        f"as of {captured_at}"
                    )
                elif exhausted:
                    reset = _row(row, "resets_at") or "unavailable"
                    parts.append(
                        f"{provider} quota exhausted window={window} "
                        f"resets_at={reset} as of {captured_at}"
                    )
                else:
                    parts.append(
                        f"{provider} quota not exhausted window={window} "
                        f"as of {captured_at}"
                    )

        status = latest_statuses.get(provider)
        if status is None:
            parts.append(f"{provider} probe status unavailable")
        else:
            value, captured_at = status
            parts.append(
                f"{provider} probe status={value} as of {captured_at}"
            )
    return parts


def _measurement(evidence: activity_readers.Evidence) -> dict[str, object]:
    """Copy classification inputs across the U4/U5 boundary in renderable form."""

    def stamp(value):
        parsed = _utc(value)
        return parsed.isoformat() if parsed is not None else None

    return {
        "epoch": stamp(evidence.epoch),
        "state_changed_at": stamp(evidence.state_changed_at),
        "last_result_row_at": stamp(evidence.last_result_row_at),
        "last_work_at": stamp(evidence.last_work_at),
        "last_durable_write_at": stamp(evidence.last_durable_write_at),
        "session_ended_at": stamp(evidence.session_ended_at),
        "process_present": evidence.process_present,
        "edits_code": evidence.edits_code,
        "branch_declared": evidence.branch_declared,
        "branch_present": evidence.branch_present,
        "dirty": evidence.dirty,
        "commits_since_epoch": evidence.commits_since_epoch,
        "unreadable": tuple(evidence.unreadable),
        "window_seconds": int(NO_PROGRESS_WINDOW.total_seconds()),
        "grace_seconds": int(START_GRACE.total_seconds()),
    }


class ReconcilerState:
    """Volatile consecutive-tick confirmation.

    A restart intentionally resets confirmation: one fresh observation is
    cheaper than emitting from state whose immediately preceding tick the new
    process did not observe.
    """

    ACTIONABLE = {
        "checkup",
        "not_started",
        "work_complete_unreported",
    }

    def __init__(self):
        self._previous: dict[tuple, str] = {}

    def observe(self, key: tuple, signal: str) -> bool:
        previous = self._previous.get(key)
        self._previous[key] = signal
        return signal in self.ACTIONABLE and previous == signal

    def retain(self, keys: set[tuple]) -> None:
        self._previous = {
            key: signal
            for key, signal in self._previous.items()
            if key in keys
        }


def deliver_reconciliation_readings(
    con,
    readings: "list[ReconciliationReading]",
) -> list[int]:
    """Transitional sink while the old planner wake path is retired.

    Evidence collection stays available for Step 5's sentinel, but Step 3
    deliberately emits no binding-addressed alert or message. The directive
    contract (Step 4) gives the sentinel its durable event target.
    """
    return []


def live_expectations(con) -> list[Expectation]:
    """Enumerate live worker assignments from the structured board."""
    placeholders = ",".join("?" for _ in TERMINAL_UNIT_STATES)
    units = con.execute(
        "SELECT u.* FROM sprint_units u "
        "JOIN documents d ON d.document_id=u.sprint_doc_id "
        f"WHERE d.frozen=0 AND u.state NOT IN ({placeholders}) "
        "ORDER BY u.sprint_doc_id, u.unit_id",
        TERMINAL_UNIT_STATES,
    ).fetchall()
    shell_ids = {
        shell_id
        for unit in units
        for shell_id in (
            _row(unit, "dev_shell_id"),
            _row(unit, "reviewer_shell_id"),
        )
        if shell_id is not None
    }
    if not shell_ids:
        return []
    shell_marks = ",".join("?" for _ in shell_ids)
    shells = {
        _row(row, "shell_id"): row
        for row in con.execute(
            "SELECT shell_id, shortname, flavor FROM shells "
            f"WHERE shell_id IN ({shell_marks})",
            tuple(sorted(shell_ids)),
        ).fetchall()
    }

    expectations: list[Expectation] = []
    for unit in units:
        for role, column in (
            ("dev", "dev_shell_id"),
            ("reviewer", "reviewer_shell_id"),
        ):
            shell_id = _row(unit, column)
            shell = shells.get(shell_id)
            if shell is None:
                continue
            expectations.append(
                Expectation(
                    sprint_doc_id=_row(unit, "sprint_doc_id"),
                    unit_id=_row(unit, "unit_id"),
                    seq=_row(unit, "seq"),
                    role=role,
                    shell_id=shell_id,
                    shell=shell,
                    unit=unit,
                )
            )
    return expectations


def _row(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def reconcile_tick(
    con,
    *,
    now=None,
    reader=None,
    refresh=None,
    state: "ReconcilerState | None" = None,
    worktree=None,
) -> list[ReconciliationReading]:
    """Read and classify every live sprint expectation without DB mutation."""
    current = _utc(now) or datetime.now(timezone.utc)
    state = state if state is not None else ReconcilerState()
    expectations = live_expectations(con)
    if not expectations:
        state.retain(set())
        return []

    refresh = refresh or activity_readers.refresh_integration_refs
    try:
        refs_fresh = bool(refresh(Path(worktree or activity_readers.REPO_ROOT)))
    except Exception:  # noqa: BLE001 — failure is recorded on each code reading
        refs_fresh = False

    source = reader or activity_readers.read
    read_one = source.read if hasattr(source, "read") else source
    provider_facts = _provider_explanation(con)
    cache: dict[tuple, activity_readers.Evidence] = {}
    readings: list[ReconciliationReading] = []
    seen: set[tuple] = set()
    for expectation in expectations:
        # Role is part of the cache identity, not decoration: a dev and a
        # reviewer on the same unit are now read differently (H-15/H-16), so
        # keying on (shell, unit) alone would serve one role the other's answer.
        evidence_key = (
            expectation.shell_id,
            expectation.unit_id,
            expectation.role,
        )
        evidence = cache.get(evidence_key)
        if evidence is None:
            evidence = read_one(
                expectation.shell,
                expectation.unit,
                current,
                role=expectation.role,
            )
            if (
                not refs_fresh
                and evidence.edits_code
                and activity_readers.INTEGRATION_REF_REFRESH
                not in evidence.unreadable
            ):
                evidence.unreadable.append(
                    activity_readers.INTEGRATION_REF_REFRESH
                )
                evidence.unreadable.sort()
            cache[evidence_key] = evidence
        signal = classify(evidence, current)
        seen.add(expectation.key)
        readings.append(
            ReconciliationReading(
                expectation=expectation,
                signal=signal,
                confirmed=state.observe(expectation.key, signal),
                evidence=evidence,
                measurement=_measurement(evidence),
                observed_at=current,
                explanation=" | ".join(
                    _activity_explanation(evidence) + provider_facts
                ),
            )
        )
    state.retain(seen)
    return readings


# ── Sprint scoping ────────────────────────────────────────────────────────────

def armed_watches(con) -> list:
    """Live watches scoped to a LIVE sprint — the poller's whole world.
    Unscoped legacy watches stay dormant until explicitly rebound.

    Liveness is `sprint_state`'s structural predicate, never the body's prose
    (H-1): this module's own `status: ACTIVE` regex was one of the two divergent
    parsers that made a reformatted line silently disarm every watch."""
    active = sprint_state.live_sprint_doc_ids(con)
    if not active:
        return []
    marks = ",".join("?" for _ in active)
    return con.execute(
        "SELECT w.watch_id, w.repo, w.pr_number, w.shell_id, w.last_seen, "
        "w.sprint_doc_id, w.unit_id, u.seq AS unit_seq "
        "FROM watched_prs w "
        # LEFT, not INNER: an unlinked watch is a first-class row (H-13's link
        # is nullable), and an inner join would silently stop polling every
        # watch registered without --unit.
        "LEFT JOIN sprint_units u ON u.unit_id = w.unit_id "
        f"WHERE w.closed_at IS NULL AND w.sprint_doc_id IN ({marks}) "
        "ORDER BY w.repo, w.pr_number, w.watch_id",
        tuple(sorted(active))).fetchall()


def live_unscoped_watch_ids(con) -> list[int]:
    """Legacy watches that cannot be polled because they have no sprint scope.
    New registration rejects this state; old rows remain repairable by rebind."""
    return [r[0] for r in con.execute(
        "SELECT watch_id FROM watched_prs "
        "WHERE closed_at IS NULL AND sprint_doc_id IS NULL "
        "ORDER BY watch_id").fetchall()]


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


# ── Heartbeats (#359 — one row per independently observed poller) ────────────

def beat(con, interval: int, *, name: str = "watch") -> None:
    con.execute(
        "INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
        "VALUES (?, datetime('now'), ?) "
        "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at, "
        "interval_s=excluded.interval_s",
        (name, interval),
    )
    con.commit()


# ── The poll cycle ────────────────────────────────────────────────────────────

def _alert(con, *, severity: str, reason: str, watch_id=None) -> None:
    """Raise an alert, deduplicated while open (partial unique index). Local
    helper — interface_broker._alert predates watch-scoped alerts.

    The sprint and unit are read off the watch rather than left NULL (H-13):
    0102 gave planner_alerts structured identity columns, and a watch is the
    one alert source that KNOWS both. Leaving them NULL forced every reader
    back to parsing `dedupe_key` or grepping prose for "U3" — the guess this
    linkage exists to delete. `dedupe_key` is unchanged, so an already-open
    alert stays the same open alert and no re-alert storm follows.
    """
    dedupe = f"-|-|{watch_id or '-'}|-|{reason}"
    sprint_doc_id = unit_id = None
    if watch_id is not None:
        row = con.execute(
            "SELECT sprint_doc_id, unit_id FROM watched_prs WHERE watch_id=?",
            (watch_id,)).fetchone()
        if row is not None:
            sprint_doc_id, unit_id = row["sprint_doc_id"], row["unit_id"]
    con.execute(
        "INSERT OR IGNORE INTO planner_alerts "
        "(watch_id, sprint_doc_id, unit_id, severity, reason, dedupe_key) "
        "VALUES (?,?,?,?,?,?)",
        (watch_id, sprint_doc_id, unit_id, severity, reason, dedupe))


def surface_unscoped_watches(con) -> int:
    """Turn dormant legacy state into an operator-visible, deduped alert.
    Returns the number of affected watches, whether newly alerted or already
    carrying the same open alert."""
    watch_ids = live_unscoped_watch_ids(con)
    for watch_id in watch_ids:
        _alert(con, severity="critical", reason="pr_watch_unscoped",
               watch_id=watch_id)
    if watch_ids:
        con.commit()
    return len(watch_ids)


def _emit_event(con, watch, event: dict, head_sha: str) -> "int | None":
    """One semantic transition → an idempotent pr_event message.

    Dedupe keyed (watch, transition, head SHA, state) via the message's
    dedupe_key partial unique index: a repeated key is a no-op, so a replayed
    poll or a baseline race can never duplicate the event. Returns the new
    message_id when emitted, or None on dedupe."""
    dedupe_key = f"pr-event|{watch['watch_id']}|{event['key']}|{head_sha}"
    try:
        cur = con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id, to_shell_id, body, kind, sprint_doc_id, dedupe_key) "
            "VALUES (?, ?, ?, 'pr_event', ?, ?)",
            (watch["shell_id"], watch["shell_id"], event["body"],
             watch["sprint_doc_id"], dedupe_key))
    except sqlite3.IntegrityError:
        return None  # the dedupe index — already emitted
    message_id = cur.lastrowid
    return message_id


def sweep_stranded_runs(con) -> int:
    """Close pr_poll_runs rows left `running` by a crash (H-9).

    A run row is opened and committed before the fetch, and closed after it —
    so a process that dies mid-fetch leaves `running` forever, with no writer
    that could ever finish it and nothing that reports it. That makes the
    audit trail lie in the one direction that matters: a poller that keeps
    crashing looks like a poller that is still working.

    Swept at STARTUP only, and that bound is deliberate: `running` is the
    correct state for the cycle currently in flight, so a sweep on any other
    tick would close a live run out from under itself. A process that has
    just started has no run of its own in flight yet, so every `running` row
    it can see belongs to a process that is gone."""
    return con.execute(
        "UPDATE pr_poll_runs SET status='error', "
        "finished_at=datetime('now'), "
        "error='stranded: the poller process ended before this run finished' "
        "WHERE status='running'").rowcount


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
               "events": 0, "errors": 0, "retired": 0,
               "unscoped_alerts": surface_unscoped_watches(con),
               "stranded_runs": (sweep_stranded_runs(con)
                                 if source == "startup" else 0)}
    emitted_ids: list[int] = []
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
            events, terminal = transitions(prev, cur, w["repo"],
                                           w["pr_number"], w["unit_seq"])
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
                mid = _emit_event(con, w, e, cur.get("sha") or "")
                if mid is not None:
                    summary["events"] += 1
                    emitted_ids.append(mid)
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
    """The service's bounded scheduler.

    GitHub reads remain watch-gated.  Reconciliation has its own cadence and
    structured-unit trigger, so it still runs before a sprint has any PR.
    """

    def __init__(self, db_path, interval: int = DEFAULT_INTERVAL, fetch=None,
                 connect=None,
                 reconcile_interval: int = DEFAULT_RECONCILE_INTERVAL,
                 activity_reader=None, refresh=None):
        super().__init__(name="pr-poller", daemon=True)
        self._db_path = str(db_path)
        self._interval = interval
        self._fetch = fetch
        self._connect = connect
        self._reconcile_interval = reconcile_interval
        self._activity_reader = activity_reader or activity_readers.ActivityReader(
            db_path=Path(self._db_path)
        )
        self._refresh = refresh
        self._reconcile_due = 0.0
        self._github_enabled = fetch is not None or shutil.which("gh") is not None
        self._stop_event = threading.Event()
        self.state = PollerState()
        self.reconciler_state = ReconcilerState()
        self.last_reconciliation: list[ReconciliationReading] = []

    def stop(self) -> None:
        self._stop_event.set()

    def _db(self):
        if self._connect is not None:
            return self._connect()
        import db_driver
        return db_driver.connect(self._db_path)

    def run(self) -> None:  # pragma: no cover — thread loop; scheduler tests drive it
        if not self._github_enabled:
            print("pr-poller: gh CLI not found — PR polling disabled "
                  "(worker reconciliation remains enabled)", flush=True)
        source = "startup"
        while not self._stop_event.is_set():
            try:
                con = self._db()
                try:
                    if self._github_enabled:
                        try:
                            beat(con, self._interval)
                        except Exception as e:
                            # The beat is ancillary liveness; polling is the
                            # mission (#359). A beat raising into the cycle's
                            # except would turn a working poller into a
                            # dead-with-noise one — log and keep polling.
                            print(
                                f"pr-poller: heartbeat error ({e})",
                                flush=True,
                            )
                        # GitHub's bounded read remains watch-gated.
                        if armed_watches(con) or live_unscoped_watch_ids(con):
                            n = poll_cycle(
                                con,
                                fetch=self._fetch,
                                source=source,
                                state=self.state,
                                interval=self._interval,
                            )
                            if n["events"] or n["errors"]:
                                print(f"pr-poller: {n}", flush=True)

                    monotonic_now = time.monotonic()
                    if monotonic_now >= self._reconcile_due:
                        self.last_reconciliation = reconcile_tick(
                            con,
                            reader=self._activity_reader,
                            refresh=self._refresh,
                            state=self.reconciler_state,
                        )
                        deliver_reconciliation_readings(
                            con,
                            self.last_reconciliation,
                        )
                        try:
                            # The transitional sink writes nothing; the beat is
                            # still the proof that evidence collection ran.
                            beat(
                                con,
                                self._reconcile_interval,
                                name="reconcile",
                            )
                        except Exception as e:
                            # Older/malformed floors may lack the heartbeat
                            # surface. Findings remain the mission; preserve
                            # them while leaving the absent beat honest.
                            con.commit()
                            print(f"pr-poller: heartbeat error ({e})", flush=True)
                        self._reconcile_due = (
                            monotonic_now + self._reconcile_interval
                        )
                finally:
                    con.close()
            except Exception as e:
                # Never die on a cycle: a dead poller silently reverts the
                # fork to the polling world. Log and keep the loop.
                print(f"pr-poller: cycle error ({e})", flush=True)
            source = "scheduler"
            self._stop_event.wait(self._interval +
                            random.uniform(0, self._interval * JITTER_FRACTION))
