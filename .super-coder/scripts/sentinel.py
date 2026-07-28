#!/usr/bin/env python3
"""Conductor sentinel — one bounded observation cycle for live sprint units.

The engine service is the only scheduler.  This module performs one injectable
cycle and owns no thread: ``pr_poller.Poller`` calls it when this fork enables
the ``sentinel`` block in ``.super-coder/instance.json``.

The sentinel observes and records.  It never changes the sprint board and it
never boots a shell.  Actionable observations become a system directive
addressed to the Conductor plus an append-only ``sentinel_events`` row linked
to that directive.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import activity_readers
import shell_liveness
import sprint_state
from sprint_units import TERMINAL_UNIT_STATES

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
INSTANCE_CONFIG = ENGINE / "instance.json"
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_ACTIVITY_BEAT_SECONDS = 300
CONDUCTOR_TARGET = "conductor"


def _row(row: Any, key: str, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _utc(value) -> datetime | None:
    if value is None or value == "":
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


def _stamp(value) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat() if parsed is not None else None


@dataclass(frozen=True)
class SentinelConfig:
    enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    activity_beat_seconds: int = DEFAULT_ACTIVITY_BEAT_SECONDS


def load_config(path: Path = INSTANCE_CONFIG) -> SentinelConfig:
    """Read this fork's feature flag and cadences.

    Missing config means disabled, preserving the pre-Step-5 service behavior.
    A malformed enabled block fails closed instead of starting a partially
    configured sentinel.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return SentinelConfig()
    block = raw.get("sentinel")
    if not isinstance(block, dict):
        return SentinelConfig()
    enabled = block.get("enabled", False)
    interval = block.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    beat = block.get(
        "activity_beat_seconds", DEFAULT_ACTIVITY_BEAT_SECONDS
    )
    if not isinstance(enabled, bool):
        return SentinelConfig()
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval <= 0
        or isinstance(beat, bool)
        or not isinstance(beat, int)
        or beat <= 0
    ):
        return SentinelConfig()
    return SentinelConfig(enabled, interval, beat)


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _event_with_key(con, dedupe_key: str):
    return con.execute(
        "SELECT event_id, directive_id FROM sentinel_events "
        "WHERE json_extract(evidence,'$.dedupe_key')=? "
        "ORDER BY event_id DESC LIMIT 1",
        (dedupe_key,),
    ).fetchone()


def append_observation(
    con,
    *,
    event_kind: str,
    evidence: dict,
    dedupe_key: str,
    shell_id=None,
    sprint_doc_id=None,
    unit_id=None,
) -> int | None:
    """Append one idempotent observation inside the caller's transaction."""
    if _event_with_key(con, dedupe_key) is not None:
        return None
    body = dict(evidence)
    body["dedupe_key"] = dedupe_key
    return con.execute(
        "INSERT INTO sentinel_events "
        "(event_kind,shell_id,sprint_doc_id,unit_id,evidence) "
        "VALUES (?,?,?,?,?)",
        (
            event_kind,
            shell_id,
            sprint_doc_id,
            unit_id,
            _json(body),
        ),
    ).lastrowid


def emit_system_signal(
    con,
    *,
    kind: str,
    evidence: dict,
    dedupe_key: str,
    shell_id=None,
    sprint_doc_id=None,
    unit_id=None,
) -> int | None:
    """Create one system directive and its linked observation atomically."""
    prior = _event_with_key(con, dedupe_key)
    if prior is not None:
        return None
    directive_id = con.execute(
        "INSERT INTO directives "
        "(issuer_shell_id,issuer_flavor,kind,payload,target,"
        " sprint_doc_id,unit_id) "
        "VALUES (NULL,'system',?,?,?,?,?)",
        (
            kind,
            _json(evidence),
            CONDUCTOR_TARGET,
            sprint_doc_id,
            unit_id,
        ),
    ).lastrowid
    body = dict(evidence)
    body["dedupe_key"] = dedupe_key
    con.execute(
        "INSERT INTO sentinel_events "
        "(event_kind,shell_id,sprint_doc_id,unit_id,directive_id,evidence) "
        "VALUES (?,?,?,?,?,?)",
        (
            kind,
            shell_id,
            sprint_doc_id,
            unit_id,
            directive_id,
            _json(body),
        ),
    )
    return directive_id


def emit_pr_transition(con, watch, event: dict, head_sha: str) -> int | None:
    """Retarget one semantic PR transition to the Conductor contracts."""
    key = event["key"]
    if key == "checks:SUCCESS":
        kind = "pr-green"
    elif key in {"checks:FAILURE", "checks:ERROR"}:
        kind = "pr-red"
    elif key == "merged:MERGED":
        kind = "pr-merged"
    elif key.startswith("review:"):
        kind = "pr-review"
    else:
        kind = "pr-closed"

    sprint_doc_id = _row(watch, "sprint_doc_id")
    unit_id = _row(watch, "unit_id")
    identity = unit_id if unit_id is not None else f"watch-{watch['watch_id']}"
    dedupe_key = (
        f"pr|{sprint_doc_id}|{identity}|{watch['repo']}|"
        f"{watch['pr_number']}|{key}|{head_sha}"
    )
    evidence = {
        "watch_id": watch["watch_id"],
        "repo": watch["repo"],
        "pr_number": watch["pr_number"],
        "unit_seq": _row(watch, "unit_seq"),
        "transition": key,
        "head_sha": head_sha or None,
        "summary": event["body"],
    }
    if kind in {"pr-green", "pr-red", "pr-merged"}:
        return emit_system_signal(
            con,
            kind=kind,
            evidence=evidence,
            dedupe_key=dedupe_key,
            sprint_doc_id=sprint_doc_id,
            unit_id=unit_id,
        )
    return append_observation(
        con,
        event_kind=kind,
        evidence=evidence,
        dedupe_key=dedupe_key,
        sprint_doc_id=sprint_doc_id,
        unit_id=unit_id,
    )


def _assigned_shell(unit) -> tuple[int | None, str]:
    if unit["state"] == "in_review":
        return unit["reviewer_shell_id"], "reviewer"
    if unit["state"] == "blocked":
        # Blocked has no active worker role in the board schema: it is reachable
        # from both working and in_review, and the transition does not retain
        # which side blocked. Its expectation is deliberately planner-shaped
        # (message/directive), so attaching either worker's pid would invent an
        # owner and can turn the inactive peer into a false dead-shell verdict.
        return None, "planner"
    return unit["dev_shell_id"], "dev"


def active_units(con) -> list[dict]:
    """Return live, nonterminal units with their state expectation."""
    sprint_ids = sprint_state.live_sprint_doc_ids(con)
    if not sprint_ids:
        return []
    marks = ",".join("?" for _ in sprint_ids)
    terminal_marks = ",".join("?" for _ in TERMINAL_UNIT_STATES)
    rows = con.execute(
        "SELECT u.*, e.expected_signals, e.max_dwell_seconds "
        "FROM sprint_units u "
        "JOIN unit_expectations e ON e.unit_state=u.state AND e.enabled=1 "
        f"WHERE u.sprint_doc_id IN ({marks}) "
        f"AND u.state NOT IN ({terminal_marks}) "
        "ORDER BY u.sprint_doc_id,u.unit_id",
        (*sorted(sprint_ids), *TERMINAL_UNIT_STATES),
    ).fetchall()
    return [dict(row) for row in rows]


def _shell_and_launch(con, shell_id) -> tuple[dict | None, dict | None]:
    if shell_id is None:
        return None, None
    shell = con.execute(
        "SELECT shell_id,shortname,flavor FROM shells "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
        (shell_id,),
    ).fetchone()
    launch = con.execute(
        "SELECT shell_id,pid,start_ticks,worktree,harness,launched_at "
        "FROM shell_launch_records WHERE shell_id=?",
        (shell_id,),
    ).fetchone()
    shell_dict = dict(shell) if shell is not None else None
    launch_dict = dict(launch) if launch is not None else None
    if shell_dict is not None and launch_dict is not None:
        shell_dict["worktree"] = launch_dict["worktree"]
    return shell_dict, launch_dict


def _message_snapshot(con, unit, shell_id) -> dict | None:
    sprint_doc_id = unit["sprint_doc_id"]
    if shell_id is None:
        rows = con.execute(
            "SELECT message_id,from_shell_id,to_shell_id,kind,body,created_at,"
            " read_at,dedupe_key "
            "FROM shell_messages WHERE sprint_doc_id=? "
            "ORDER BY created_at DESC,message_id DESC LIMIT 100",
            (sprint_doc_id,),
        ).fetchall()
        active_count = con.execute(
            "SELECT COUNT(*) FROM sprint_units WHERE sprint_doc_id=? "
            f"AND state NOT IN ({','.join('?' for _ in TERMINAL_UNIT_STATES)})",
            (sprint_doc_id, *TERMINAL_UNIT_STATES),
        ).fetchone()[0]
    else:
        rows = con.execute(
            "SELECT message_id,from_shell_id,to_shell_id,kind,body,created_at,"
            " read_at,dedupe_key "
            "FROM shell_messages WHERE sprint_doc_id=? "
            "AND (from_shell_id=? OR to_shell_id=?) "
            "ORDER BY created_at DESC,message_id DESC LIMIT 100",
            (sprint_doc_id, shell_id, shell_id),
        ).fetchall()
        active_count = con.execute(
            "SELECT COUNT(*) FROM sprint_units WHERE sprint_doc_id=? "
            "AND (dev_shell_id=? OR reviewer_shell_id=?) "
            f"AND state NOT IN ({','.join('?' for _ in TERMINAL_UNIT_STATES)})",
            (sprint_doc_id, shell_id, shell_id, *TERMINAL_UNIT_STATES),
        ).fetchone()[0]
    if not rows:
        return None
    row = rows[0] if active_count <= 1 else next(
        (
            candidate
            for candidate in rows
            if _message_unit_id(con, candidate) == unit["unit_id"]
            or re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(str(unit['seq']))}"
                rf"(?![A-Za-z0-9_])",
                candidate["body"] or "",
            )
        ),
        None,
    )
    if row is None:
        return None
    return {
        key: row[key]
        for key in (
            "message_id",
            "from_shell_id",
            "to_shell_id",
            "kind",
            "created_at",
            "read_at",
        )
    }


def _message_unit_id(con, row) -> int | None:
    key = row["dedupe_key"]
    if not key or not str(key).startswith("pr-event|"):
        return None
    parts = str(key).split("|")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    linked = con.execute(
        "SELECT unit_id FROM watched_prs WHERE watch_id=?",
        (int(parts[1]),),
    ).fetchone()
    return linked[0] if linked is not None else None


def _directive_snapshot(
    con, sprint_doc_id: int, unit_id: int, *, kind: str | None = None
) -> dict | None:
    params: list[Any] = [sprint_doc_id, unit_id]
    where = (
        "sprint_doc_id=? AND (unit_id=? OR unit_id IS NULL) "
        "AND issuer_flavor='planner'"
    )
    if kind is not None:
        where += " AND kind=?"
        params.append(kind)
    row = con.execute(
        "SELECT directive_id,kind,created_at,status FROM directives "
        f"WHERE {where} ORDER BY created_at DESC,directive_id DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row is not None else None


def _pr_snapshot(con, unit_id: int) -> dict | None:
    row = con.execute(
        "SELECT w.watch_id,w.repo,w.pr_number,w.last_seen,"
        " o.observed_at,o.transition,o.head_sha "
        "FROM watched_prs w "
        "LEFT JOIN pr_poll_observations o ON o.observation_id=("
        " SELECT o2.observation_id FROM pr_poll_observations o2 "
        " WHERE o2.watch_id=w.watch_id "
        " ORDER BY o2.observed_at DESC,o2.observation_id DESC LIMIT 1"
        ") WHERE w.unit_id=? "
        "ORDER BY COALESCE(o.observed_at,w.created_at) DESC,w.watch_id DESC "
        "LIMIT 1",
        (unit_id,),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["fingerprint"] = json.loads(item.pop("last_seen") or "null")
    except json.JSONDecodeError:
        item["fingerprint"] = None
    return item


def git_snapshot(worktree: Path) -> dict:
    """Read the current commit identity without mutating or fetching."""
    out = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%cI", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if out.returncode != 0:
        return {"head_sha": None, "committed_at": None}
    lines = out.stdout.splitlines()
    return {
        "head_sha": lines[0] if lines else None,
        "committed_at": lines[1] if len(lines) > 1 else None,
    }


def _latest(*values) -> datetime | None:
    parsed = [_utc(value) for value in values]
    return max((value for value in parsed if value is not None), default=None)


def _signal_times(
    *,
    activity: activity_readers.Evidence,
    message,
    commit,
    pr,
    planner,
    kickoff,
) -> dict[str, datetime | None]:
    latest_activity = _latest(
        activity.newest_mtime,
        activity.last_work_at,
        activity.marker_at,
        activity.last_durable_write_at,
    )
    pr_at = _utc(_row(pr, "observed_at"))
    review_at = (
        pr_at
        if str(_row(pr, "transition", "")).startswith("review:")
        else _utc(activity.last_result_row_at)
    )
    return {
        "activity": latest_activity,
        "message": _utc(_row(message, "created_at")),
        "commit": _utc(_row(commit, "committed_at")),
        "pr": pr_at,
        "review": review_at,
        "planner-directive": _utc(_row(planner, "created_at")),
        "kickoff": _utc(_row(kickoff, "created_at")),
    }


def _activity_beat(
    con,
    *,
    unit,
    shell_id,
    signal_at,
    now,
    interval_seconds: int,
    evidence: dict,
) -> bool:
    if signal_at is None:
        return False
    prior = con.execute(
        "SELECT evidence,observed_at FROM sentinel_events "
        "WHERE event_kind='activity-beat' AND unit_id=? AND shell_id IS ? "
        "ORDER BY event_id DESC LIMIT 1",
        (unit["unit_id"], shell_id),
    ).fetchone()
    if prior is not None:
        prior_body = json.loads(prior["evidence"])
        if prior_body.get("activity_at") == signal_at.isoformat():
            return False
        observed = _utc(prior["observed_at"])
        if observed is not None and (now - observed).total_seconds() < interval_seconds:
            return False
    dedupe_key = (
        f"activity|{unit['unit_id']}|{unit['state_changed_at']}|"
        f"{signal_at.isoformat()}"
    )
    body = dict(evidence)
    body["activity_at"] = signal_at.isoformat()
    return append_observation(
        con,
        event_kind="activity-beat",
        evidence=body,
        dedupe_key=dedupe_key,
        shell_id=shell_id,
        sprint_doc_id=unit["sprint_doc_id"],
        unit_id=unit["unit_id"],
    ) is not None


def cycle(
    con,
    *,
    config: SentinelConfig | None = None,
    now=None,
    activity_reader=None,
    liveness: Callable[[dict], bool] = shell_liveness.claim_live,
    git_probe: Callable[[Path], dict] = git_snapshot,
) -> dict:
    """Run one complete sentinel pass.

    Every external seam is injectable so fake clocks, worktrees, launch claims,
    and git state can exercise the production transaction.
    """
    config = config or load_config()
    summary = {
        "enabled": config.enabled,
        "active_units": 0,
        "activity_beats": 0,
        "dead_shells": 0,
        "stalls": 0,
        "indeterminate": 0,
    }
    if not config.enabled:
        return summary
    units = active_units(con)
    summary["active_units"] = len(units)
    if not units:
        return summary

    current = _utc(now) or datetime.now(timezone.utc)
    source = activity_reader or activity_readers.read
    read_one = source.read if hasattr(source, "read") else source

    for unit in units:
        shell_id, role = _assigned_shell(unit)
        shell, launch = _shell_and_launch(con, shell_id)
        state_clock = _utc(unit["state_changed_at"])
        if state_clock is None:
            summary["indeterminate"] += 1
            continue

        activity = activity_readers.Evidence(
            state_changed_at=state_clock,
            edits_code=role == "dev",
        )
        if shell is not None:
            activity = read_one(shell, unit, current, role=role)
        worktree = Path(
            _row(launch, "worktree")
            or (REPO_ROOT / ".sc-worktrees" / str(_row(shell, "shortname", "")))
        )
        try:
            commit = git_probe(worktree) if shell is not None else {}
        except (OSError, subprocess.SubprocessError):
            commit = {}
        message = _message_snapshot(con, unit, shell_id)
        planner = _directive_snapshot(
            con, unit["sprint_doc_id"], unit["unit_id"]
        )
        kickoff = _directive_snapshot(
            con, unit["sprint_doc_id"], unit["unit_id"], kind="kickoff"
        )
        pr = _pr_snapshot(con, unit["unit_id"])
        signals = _signal_times(
            activity=activity,
            message=message,
            commit=commit,
            pr=pr,
            planner=planner,
            kickoff=kickoff,
        )
        latest_activity = signals["activity"]
        evidence = {
            "unit_state": unit["state"],
            "unit_seq": unit["seq"],
            "role": role,
            "state_changed_at": state_clock.isoformat(),
            "worktree": str(worktree) if shell is not None else None,
            "last_mtime": _stamp(activity.newest_mtime),
            "last_activity": _stamp(latest_activity),
            "last_commit_sha": _row(commit, "head_sha"),
            "last_commit_at": _stamp(_row(commit, "committed_at")),
            "last_message_id": _row(message, "message_id"),
            "last_message_at": _stamp(_row(message, "created_at")),
            "last_message": message,
            "pr": pr,
            "process": {
                "pid": _row(launch, "pid"),
                "start_ticks": _row(launch, "start_ticks"),
                "launched_at": _stamp(_row(launch, "launched_at")),
            },
            "expected_signals": json.loads(unit["expected_signals"]),
        }

        launch_floor = _utc(unit.get("assigned_at")) or state_clock
        launch_at = _utc(_row(launch, "launched_at"))
        launch_applies = launch is not None and (
            launch_at is None or launch_at >= launch_floor
        )
        live = None
        if launch_applies:
            try:
                verdict = liveness(launch)
                live = verdict if isinstance(verdict, bool) else None
            except (OSError, ValueError):
                live = None
        evidence["process"]["live"] = live
        if launch_applies and live is False:
            dedupe_key = (
                f"dead-shell|{unit['unit_id']}|{unit['state_changed_at']}|"
                f"{launch['pid']}|{launch['start_ticks']}"
            )
            if emit_system_signal(
                con,
                kind="dead-shell",
                evidence=evidence,
                dedupe_key=dedupe_key,
                shell_id=shell_id,
                sprint_doc_id=unit["sprint_doc_id"],
                unit_id=unit["unit_id"],
            ) is not None:
                summary["dead_shells"] += 1
            continue

        if _activity_beat(
            con,
            unit=unit,
            shell_id=shell_id,
            signal_at=latest_activity,
            now=current,
            interval_seconds=config.activity_beat_seconds,
            evidence=evidence,
        ):
            summary["activity_beats"] += 1

        expected = evidence["expected_signals"]
        signal_clock = max(
            (
                signals[name]
                for name in expected
                if name in signals and signals[name] is not None
            ),
            default=None,
        )
        quiet_since = max(
            value for value in (state_clock, signal_clock) if value is not None
        )
        dwell = max(0, int((current - quiet_since).total_seconds()))
        evidence["last_expected_signal_at"] = _stamp(signal_clock)
        evidence["dwell_seconds"] = dwell
        evidence["max_dwell_seconds"] = unit["max_dwell_seconds"]
        if dwell < unit["max_dwell_seconds"]:
            continue
        dedupe_key = (
            f"stall|{unit['unit_id']}|{unit['state_changed_at']}|"
            f"{_stamp(signal_clock)}"
        )
        if emit_system_signal(
            con,
            kind="stall",
            evidence=evidence,
            dedupe_key=dedupe_key,
            shell_id=shell_id,
            sprint_doc_id=unit["sprint_doc_id"],
            unit_id=unit["unit_id"],
        ) is not None:
            summary["stalls"] += 1

    con.commit()
    return summary
