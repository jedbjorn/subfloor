"""Engine-owned run identities, terminal outcomes and pulse reconciliation.

No credentials are persisted. Recovery reads only engine-allocated evidence
paths; an absent supervisor never supplies an inferred command exit code.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import db_driver

TERMINAL = frozenset({"done", "failed", "timeout", "killed", "lost"})
ENGINE = Path(__file__).resolve().parents[1]
PROBE_EXCERPT = 160
# Subagent limits (spec #273 "Limits"). Each is held at registration, inside
# the write transaction, so parallel launches cannot share one slot.
AGENT_LIVE_PER_SHELL = 3
AGENT_LIVE_PER_INSTALL = 6
AGENT_DEFAULT_TIMEOUT = 7200
AGENT_MAX_TIMEOUT = 28800
AGENT_BRIEF_MAX = 65536
AGENT_RESULT_MAX = 262144
AGENT_WORKTREE_PREFIX = "agent-"
AGENT_FACTS_FILE = "facts.json"
AGENT_RESULT_FILE = "result.md"
# Watchers (spec #273 "Watchers"): every probe is one, whatever its form.
WATCH_LIVE_PER_SHELL = 10
WATCH_ARGV = ("sc", "job", "watch")
WATCH_FORMS = ("pr", "timer")
# Decision #428: each refusal names the confusion it prevents, first.
NESTING_CONFUSION = "delegated work whose owner can no longer be traced"
REVIEWER_CONFUSION = "a review verdict that rests on a reader nobody assigned"
_AGENT_REQUEST = frozenset({
    "harness", "requested_model", "requested_effort", "effective_effort",
    "branch", "adapter_version", "brief_bytes", "launched_from_run",
    "continues_run_id",
})
_AGENT_FACTS = ("session_ref", "provider_model", "result_bytes", "worktree_removed_at")
_AGENT_OUTCOME = frozenset({"head_commit", "uncommitted"})


ADMIN_CONFUSION = "a subagent launched from the Admin seat, which the delegation rules do not cover"
RESERVED_NAME_CONFUSION = "a subagent worktree that is also a shell's worktree"
_HARNESS_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_RESERVED_SHELL_NAME = re.compile(r"agent-[0-9]+")


class RunLimitError(ValueError):
    """A registration refused because a concurrency limit is full."""


def harness_name(value: object) -> str:
    """A harness is an adapter directory name, never free text."""
    if not isinstance(value, str) or not _HARNESS_NAME.fullmatch(value):
        raise ValueError("harness must be a shipped harness name (lowercase letters, digits, - and _)")
    return value


def route_value(value: object, name: str, *, limit: int = 512, required: bool = True) -> str | None:
    """A model selector or effort level, safe to place in a command.

    The value travels as one argv element, and for some harnesses inside a
    quoted config value. A leading dash would be read as an option; a quote,
    backslash, newline or other control character could end the quoted value
    and add a setting of its own.
    """
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty value of at most {limit} characters")
    if value != value.strip() or value.startswith("-"):
        raise ValueError(f"{name} must not start with a dash or carry surrounding space")
    if any(ord(c) < 32 or ord(c) == 127 or c in "\"'`\\" for c in value):
        raise ValueError(f"{name} must not contain quotes, backslashes or control characters")
    return value


def _text(value: object, name: str, *, limit: int = 512, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(f"{name} must be display text (maximum {limit} characters)")
    return value


def validate_probe_attempt(attempt: object) -> dict:
    if not isinstance(attempt, dict) or set(attempt) != {'number', 'exit_code', 'excerpt'}:
        raise ValueError('invalid probe attempt')
    if type(attempt['number']) is not int or attempt['number'] < 1:
        raise ValueError('probe attempt number must be positive')
    if type(attempt['exit_code']) is not int:
        raise ValueError('probe exit_code must be an integer')
    if not isinstance(attempt['excerpt'], str) or len(attempt['excerpt']) > PROBE_EXCERPT:
        raise ValueError('probe excerpt exceeds bound')
    return attempt


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_alive(pid, ticks, boot) -> bool | None:
    """True = same incarnation, False = proven gone, None = indeterminate."""
    if not pid or ticks is None or not boot:
        return None
    try:
        if boot != boot_id():
            return False
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]) == int(ticks) and fields[0] != "Z"
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        return None


def terminal_payload(meta: dict) -> dict:
    state = meta.get("state")
    if state not in TERMINAL:
        state = (
            "timeout"
            if meta.get("timed_out")
            else "killed"
            if meta.get("killed")
            else "done"
            if meta.get("exit_code") == 0
            else "failed"
        )
    return {
        key: value
        for key, value in {
            "state": state,
            "exit_code": meta.get("exit_code"),
            "finished_at": meta.get("finished_at"),
            # JSON escapes a non-BMP character as twelve ASCII bytes. Leave
            # room for the supervisor's fixed state/code/timestamp fields.
            "spawn_error": str(meta["spawn_error"])[:128 if meta.get('probe_attempt') else 256]
            if meta.get("spawn_error") is not None
            else None,
            "probe_attempt": meta.get("probe_attempt"),
            "agent": meta.get("agent_outcome"),
        }.items()
        if value is not None
    }


def watch_form(row: dict) -> tuple[str, str] | None:
    """A probe's form and condition: `until`, `pr` or `timer`; None for other kinds.

    The two named forms register a fixed argv, `sc job watch <form> ...`, so
    the ledger states the condition without a column of its own.
    """
    if row.get("kind") != "probe":
        return None
    argv = row["argv"]
    if tuple(argv[:3]) == WATCH_ARGV and len(argv) > 3 and argv[3] in WATCH_FORMS:
        options = dict(zip(argv[4::2], argv[5::2]))
        if argv[3] == "timer":
            return "timer", f"timer, {options.get('--after', '?')} seconds"
        repository = options.get("--repository")
        return "pr", (
            f"PR #{options.get('--pr', '?')}"
            + (f" in {repository}" if repository else "")
            + ": checks settle (green or red), or it is merged or closed"
        )
    return "until", argv[-1]


def _span(seconds: object) -> str:
    if type(seconds) not in (int, float) or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def agent_wake_body(row: dict, payload: dict) -> str:
    """The wake for a finished subagent (spec #273 "The wake").

    It states facts and next commands. No part of the subagent's final
    message is placed in it.
    """
    rid = row["run_id"]
    agent = row.get("agent") or {}
    outcome = payload.get("agent") or {}
    start = _stamp(row.get("started_at") or row.get("created_at"))
    end = _stamp(payload.get("finished_at"))
    ran = _span((end - start).total_seconds()) if start and end else "unknown"
    base = agent.get("base_commit") or ""
    removed = bool(agent.get("worktree_removed_at"))
    where = "removed, unchanged" if removed else agent.get("worktree") or row["cwd"]
    head = outcome.get("head_commit") or (base if removed else None)
    dirty = outcome.get("uncommitted")
    if dirty is None and removed:
        dirty = False
    size = agent.get("result_bytes")
    return (
        f"Run {rid} (agent: {row['label']}) {payload['state']}; "
        f"exit={payload.get('exit_code', 'unknown')}; {agent.get('harness', '?')} · "
        f"{agent.get('requested_model', '?')} · {agent.get('effective_effort') or 'default'}; "
        f"ran {ran} of {_span(row.get('timeout_seconds'))}.\n"
        f"Worktree {where}; "
        f"{agent.get('branch') or 'detached at ' + (base[:12] or 'unknown')}; "
        f"HEAD {head[:12] if head else 'unknown'}; uncommitted changes: "
        f"{'unknown' if dirty is None else 'yes' if dirty else 'no'}.\n"
        f"Result: {f'{size} bytes' if size is not None else 'not recorded'}. "
        f"Read it with `sc job result {rid}`; log with `sc job tail {rid}`.\n"
        f"Continue with `sc job continue {rid} --brief-file <path>`; "
        f"remove a kept worktree with `sc job clean {rid}`.\n"
        "You are accountable for what this subagent did under your brief. Check its claims\n"
        "against the branch, the PR and its checks before you rely on them."
    )


def watch_wake_body(row: dict, payload: dict, form: str, condition: str) -> str:
    """The wake for a `watch pr` or `watch timer` (spec #273 "Watchers")."""
    rid = row["run_id"]
    attempt = payload.get("probe_attempt")
    state = payload["state"]
    if form == "timer":
        final = "the time passed" if state == "done" else state
    else:
        read = attempt["excerpt"].strip() if attempt and attempt["excerpt"].strip() else "no read"
        final = read if state == "done" else f"{state}; last read: {read}"
    return (
        f"Run {rid} (watch {form}: {row['label']}) {state}; "
        f"exit={payload.get('exit_code', 'unknown')}.\n"
        f"Condition: {condition}.\n"
        f"Final state: {final}.\n"
        f"Inspect `sc job status {rid}` and `sc job tail {rid}`. This wake grants no "
        "review, merge, or Sprint authority: green is not a directive."
    )


def wake_body(row: dict, payload: dict) -> str:
    rid = row["run_id"]
    if row["kind"] == "agent":
        return agent_wake_body(row, payload)
    watch = watch_form(row)
    if watch and watch[0] != "until":
        return watch_wake_body(row, payload, *watch)
    attempt = payload.get('probe_attempt')
    probe = (
        f"Final probe attempt {attempt['number']}: exit={attempt['exit_code']}; "
        f"excerpt={json.dumps(attempt['excerpt'])}.\n"
        if attempt else ''
    )
    return (
        f"Run {rid} ({row['kind']}: {row['label']}) {payload['state']}; "
        f"exit={payload.get('exit_code', 'unknown')}; cwd={row['cwd']}.\n"
        f"{probe}"
        f"Inspect `sc job status {rid}` and `sc job tail {rid}`, then continue "
        "dependent work under your current authority. This outcome grants no "
        "review, merge, or Sprint authority."
    )


class RunStore:
    def __init__(self, con: sqlite3.Connection, engine: Path = ENGINE):
        self.con = con
        con.row_factory = sqlite3.Row
        self.root = engine / "run" / "runs"
        self.worktrees = engine.parent / ".sc-worktrees"

    def get(self, run_id: int, owner: int | None = None) -> dict:
        row = self.con.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown run")
        if owner is not None and row["owner_shell_id"] != owner:
            raise PermissionError("run belongs to another shell")
        out = dict(row)
        out["argv"] = json.loads(out["argv"])
        if out["kind"] == "agent":
            out["agent"] = self._agent(run_id)
        if out["receipt_json"]:
            out["receipt_json"] = json.loads(out["receipt_json"])
        if out["message_id"] is not None:
            wake = self.con.execute(
                "SELECT wm.wake_id,f.attempts,f.last_attempt_at,f.last_error "
                "FROM sprint_wake_messages wm LEFT JOIN engine_wake_failures f USING(wake_id) "
                "WHERE wm.message_id=?",
                (out["message_id"],),
            ).fetchone()
            if wake:
                out["wake_id"] = wake["wake_id"]
                out["delivery_attempts"] = wake["attempts"] or 0
                out["delivery_attempted_at"] = wake["last_attempt_at"]
                out["delivery_error"] = wake["last_error"]
        return out

    def list(self, owner: int | None = None, *, kind: str | None = None,
             state: str | None = None, limit: int = 200) -> list[dict]:
        rows = self.con.execute(
            "SELECT run_id FROM runs WHERE (? IS NULL OR owner_shell_id=?) "
            "AND (? IS NULL OR kind=?) AND (? IS NULL OR state=?) "
            "ORDER BY run_id DESC LIMIT ?",
            (owner, owner, kind, kind, state, state, min(max(limit, 1), 200)),
        ).fetchall()
        return [self.get(row[0], owner) for row in rows]

    def count(self, owner: int | None = None, *, kind: str | None = None,
              state: str | None = None) -> int:
        return self.con.execute(
            "SELECT count(*) FROM runs WHERE (? IS NULL OR owner_shell_id=?) "
            "AND (? IS NULL OR kind=?) AND (? IS NULL OR state=?)",
            (owner, owner, kind, kind, state, state),
        ).fetchone()[0]

    def _agent(self, run_id: int) -> dict | None:
        row = self.con.execute(
            "SELECT * FROM run_agents WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out.pop("run_id")
        if out["usage_json"]:
            out["usage_json"] = json.loads(out["usage_json"])
        return out

    def _agent_request(self, data: dict) -> dict:
        """Validate what a subagent launch asks for; no state is read."""
        agent = data.get("agent")
        if not isinstance(agent, dict):
            raise ValueError("an agent run requires an agent object")
        unknown = set(agent) - _AGENT_REQUEST
        if unknown:
            raise ValueError(f"unknown agent field: {sorted(unknown)[0]}")
        launched_from = agent.get("launched_from_run")
        if launched_from is not None:
            raise PermissionError(
                f"refused — {NESTING_CONFUSION}: this call comes from inside "
                f"subagent run {str(launched_from)[:32]}, and a subagent cannot launch "
                "or continue a subagent (nesting depth 1). Command jobs and "
                "watchers remain available; they belong to the launching shell."
            )
        if data.get("cwd") is not None:
            raise ValueError("an agent run's cwd is its engine-assigned worktree")
        commit = data.get("commit")
        if not isinstance(commit, str) or not (
            len(commit) in (40, 64) and all(c in "0123456789abcdef" for c in commit)
        ):
            raise ValueError("an agent run requires its base commit as a full object id")
        brief = agent.get("brief_bytes")
        if type(brief) is not int or brief < 1:
            raise ValueError("brief_bytes must be a positive integer")
        if brief > AGENT_BRIEF_MAX:
            raise ValueError(
                f"brief is {brief} bytes; the limit is {AGENT_BRIEF_MAX} bytes (64 KiB)"
            )
        branch = _text(agent.get("branch"), "branch", limit=255, required=False)
        if branch is not None and (branch.startswith("-") or " " in branch):
            raise ValueError("branch must be a branch name")
        continues = agent.get("continues_run_id")
        if continues is not None and (type(continues) is not int or continues < 1):
            raise ValueError("continues_run_id must be a run id")
        return {
            "continues_run_id": continues,
            "harness": harness_name(agent.get("harness")),
            "requested_model": route_value(agent.get("requested_model"), "requested_model"),
            "requested_effort": route_value(
                agent.get("requested_effort"), "requested_effort", limit=64, required=False
            ),
            "effective_effort": route_value(
                agent.get("effective_effort"), "effective_effort", limit=64, required=False
            ),
            "branch": branch,
            "adapter_version": _text(
                agent.get("adapter_version"), "adapter_version", limit=128, required=False
            ),
            "base_commit": commit,
        }

    def _runs_without_a_process(self, kind: str = "agent") -> frozenset[int]:
        """Unfinished runs of one kind that hold no slot, because nothing runs.

        A run holds its slot while its supervisor or its command is proven
        alive. Otherwise it frees the slot when its supervisor is proven gone,
        or when its reconciliation was blocked and its supervisor is not proven
        alive: such a row waits for operator recovery with no process.

        Read before the write transaction, because it probes /proc. A run that
        registers after this read is absent from the set and so still counts.
        """
        rows = self.con.execute(
            "SELECT run_id,pid,start_ticks,supervisor_pid,supervisor_start_ticks,boot_id,"
            "wake_state FROM runs WHERE kind=? AND state IN ('registered','running')",
            (kind,),
        ).fetchall()
        free = set()
        for row in rows:
            supervisor = process_alive(
                row["supervisor_pid"], row["supervisor_start_ticks"], row["boot_id"]
            )
            command = process_alive(row["pid"], row["start_ticks"], row["boot_id"])
            if supervisor is True or command is True:
                continue
            if supervisor is False or row["wake_state"] == "blocked":
                free.add(row["run_id"])
        return frozenset(free)

    def _agents_without_a_process(self) -> frozenset[int]:
        return self._runs_without_a_process("agent")

    def _watch_admission(self, owner: int, gone: frozenset[int] = frozenset()) -> None:
        """At most ten live watchers for one shell; inside the write transaction."""
        mine = [
            row["run_id"] for row in self.con.execute(
                "SELECT run_id FROM runs WHERE kind='probe' AND owner_shell_id=? "
                "AND state IN ('registered','running') ORDER BY run_id", (owner,)
            )
            if row["run_id"] not in gone
        ]
        if len(mine) >= WATCH_LIVE_PER_SHELL:
            raise RunLimitError(
                f"live watcher limit for one shell reached: {len(mine)} of "
                f"{WATCH_LIVE_PER_SHELL} live (runs {', '.join(map(str, mine))}). "
                "Wait for one to end, or stop one with `sc job kill <id>`."
            )

    def _continued(self, owner: int, agent: dict, gone: frozenset[int]) -> dict:
        """The earlier run a continue resumes; inside the write transaction."""
        earlier_id = agent["continues_run_id"]
        try:
            earlier = self.get(earlier_id, owner)
        except KeyError:
            raise ValueError(f"cannot continue run {earlier_id}: no such run") from None
        facts = earlier.get("agent")
        if earlier["kind"] != "agent" or not facts:
            raise ValueError(f"cannot continue run {earlier_id}: it is not a subagent run")
        if facts["harness"] != agent["harness"]:
            raise ValueError(
                f"cannot continue run {earlier_id} on harness '{agent['harness']}': it ran on "
                f"'{facts['harness']}', and a continue resumes that harness's own session"
            )
        if facts["requested_model"] != agent["requested_model"]:
            raise ValueError(
                f"cannot continue run {earlier_id} with model '{agent['requested_model']}': "
                f"a continue keeps the earlier run's model, '{facts['requested_model']}'"
            )
        live = [
            row["run_id"] for row in self.con.execute(
                "SELECT r.run_id FROM runs r JOIN run_agents a USING(run_id) "
                "WHERE a.worktree=? AND r.state IN ('registered','running') ORDER BY r.run_id",
                (facts["worktree"],),
            )
            if row["run_id"] not in gone
        ]
        if live:
            raise ValueError(
                f"cannot continue run {earlier_id}: run {live[0]} is still live in its "
                "worktree. Wait for it to end, or stop it with `sc job kill`."
            )
        if earlier["state"] not in TERMINAL:
            raise ValueError(
                f"cannot continue run {earlier_id}: it has no terminal result yet "
                f"(state {earlier['state']})"
            )
        if not facts["session_ref"]:
            raise ValueError(
                f"cannot continue run {earlier_id}: no session reference was captured, "
                "so there is no harness session to resume. Launch a fresh subagent."
            )
        discarded = self.con.execute(
            "SELECT run_id FROM run_agents WHERE worktree=? AND worktree_discarded_at IS NOT NULL "
            "ORDER BY run_id LIMIT 1", (facts["worktree"],),
        ).fetchone()
        if discarded:
            raise ValueError(
                f"cannot continue run {earlier_id}: its worktree was removed with --discard "
                f"(run {discarded['run_id']}), so the work the session knows is gone. "
                "Launch a fresh subagent."
            )
        return facts

    def _agent_admission(self, owner: int, gone: frozenset[int] = frozenset()) -> None:
        """Who may launch and how many may run; called inside the write transaction."""
        flavor = self.con.execute(
            "SELECT flavor FROM shells WHERE shell_id=?", (owner,)
        ).fetchone()
        if flavor and flavor["flavor"] == "admin":
            raise PermissionError(
                f"refused — {ADMIN_CONFUSION}: every non-Admin flavor may launch a "
                "subagent; Admin may not."
            )
        taken = [
            row["shortname"] for row in self.con.execute(
                "SELECT shortname FROM shells WHERE COALESCE(is_deleted,0)=0 "
                "AND lower(shortname) LIKE 'agent-%'"
            )
            if _RESERVED_SHELL_NAME.fullmatch(row["shortname"].lower())
        ]
        if taken:
            raise PermissionError(
                f"refused — {RESERVED_NAME_CONFUSION}: shell {taken[0]!r} holds a name "
                "of the form agent-<number>, which is where a subagent run of that "
                "number works. Rename that shell before any subagent is launched."
            )
        reviewing = self.con.execute(
            "SELECT s.sprint_id,s.lifecycle FROM sprint_participants p "
            "JOIN sprints s USING(sprint_id) WHERE p.shell_id=? AND p.role='reviewer' "
            "AND s.lifecycle IN ('armed','paused') ORDER BY s.sprint_id LIMIT 1",
            (owner,),
        ).fetchone()
        if reviewing:
            raise PermissionError(
                f"refused — {REVIEWER_CONFUSION}: this shell is a Reviewer of "
                f"Sprint {reviewing['sprint_id']} ({reviewing['lifecycle']}), and a Reviewer "
                "does not delegate while its Sprint is armed or paused."
            )
        # A slot is held by a run that can still be running; `gone` names the
        # unfinished runs proven to have no process (_runs_without_a_process).
        live = [
            row for row in self.con.execute(
                "SELECT r.run_id,r.owner_shell_id,s.shortname FROM runs r "
                "LEFT JOIN shells s ON s.shell_id=r.owner_shell_id "
                "WHERE r.kind='agent' AND r.state IN ('registered','running') "
                "ORDER BY r.run_id"
            )
            if row["run_id"] not in gone
        ]
        mine = [row["run_id"] for row in live if row["owner_shell_id"] == owner]
        if len(mine) >= AGENT_LIVE_PER_SHELL:
            raise RunLimitError(
                f"live subagent limit for one shell reached: {len(mine)} of "
                f"{AGENT_LIVE_PER_SHELL} live (runs {', '.join(map(str, mine))}). "
                "Wait for one to finish. `sc job kill <id>` stops one that is "
                "running; `sc job status <id>` shows whether it is."
            )
        if len(live) >= AGENT_LIVE_PER_INSTALL:
            holders = ", ".join(
                f"{row['run_id']} ({row['shortname'] or row['owner_shell_id']})" for row in live
            )
            raise RunLimitError(
                f"live subagent limit for this install reached: {len(live)} of "
                f"{AGENT_LIVE_PER_INSTALL} live (runs {holders}). "
                "Wait for one to finish."
            )

    def register(self, owner: int, data: dict) -> dict:
        argv = data.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
        ):
            raise ValueError("argv must be a nonempty array of strings")
        kind = data.get("kind", "job")
        if kind not in {"job", "devkit", "probe", "agent"}:
            raise ValueError("invalid run kind")
        agent = self._agent_request(data) if kind == "agent" else None
        if agent is None and data.get("agent") is not None:
            raise ValueError("agent facts require an agent run")
        timeout = data.get("timeout_seconds")
        if timeout is None and agent is not None:
            timeout = AGENT_DEFAULT_TIMEOUT
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ValueError("timeout_seconds must be a positive integer")
        if agent is not None and timeout > AGENT_MAX_TIMEOUT:
            raise ValueError(
                f"a subagent run is limited to {AGENT_MAX_TIMEOUT} seconds (8 hours); "
                f"{timeout} was requested"
            )
        foreground = data.get("foreground", False)
        if type(foreground) is not bool or (foreground and kind != "devkit"):
            raise ValueError("foreground must be a boolean and requires a devkit run")
        key, cwd, label = (
            data.get("registration_key"),
            data.get("cwd"),
            data.get("label")
            or (f"{agent['harness']}:{agent['requested_model']}"[:256] if agent else argv[0]),
        )
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("registration_key is required (maximum 128 characters)")
        if agent is None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
            raise ValueError("cwd must be absolute")
        if (
            not isinstance(label, str)
            or len(label) > 256
            or any(ord(c) < 32 for c in label)
        ):
            raise ValueError("label must be display text (maximum 256 characters)")
        identity = (
            kind, label, json.dumps(argv), cwd, data.get("commit"), int(foreground), timeout,
        )
        columns = ("kind", "label", "argv", "cwd", "commit", "foreground", "timeout_seconds")
        legacy = self.root.parent / "jobs"
        legacy_floor = (
            max(
                (
                    int(p.name.split("-", 1)[0])
                    for p in legacy.iterdir()
                    if p.is_dir() and p.name.split("-", 1)[0].isdigit()
                ),
                default=0,
            )
            if legacy.is_dir()
            else 0
        )
        gone = (
            self._agents_without_a_process() if agent is not None
            else self._runs_without_a_process("probe") if kind == "probe"
            else frozenset()
        )
        with db_driver.write_transaction(self.con, "runs.register"):
            existing = self.con.execute(
                "SELECT * FROM runs WHERE owner_shell_id=? AND registration_key=?",
                (owner, key),
            ).fetchone()
            if existing:
                # An agent's cwd is assigned below, so it is not part of the request.
                compared = [k for k in columns if agent is None or k != "cwd"]
                same = all(existing[k] == identity[columns.index(k)] for k in compared)
                if same and agent is not None:
                    stored = self._agent(existing["run_id"]) or {}
                    # A continue takes its base and branch from the earlier run.
                    fixed = {"base_commit", "branch"} if agent["continues_run_id"] else set()
                    same = all(
                        stored.get(k) == v for k, v in agent.items() if k not in fixed
                    )
                if not same:
                    raise ValueError("registration key reused with different input")
                return self.get(existing["run_id"], owner)
            earlier = None
            if agent is not None:
                if agent["continues_run_id"] is not None:
                    earlier = self._continued(owner, agent, gone)
                self._agent_admission(owner, gone)
            elif kind == "probe":
                self._watch_admission(owner, gone)
            # Allocate above both the ledger high-water mark and legacy names,
            # so `status 1` never changes meaning after an upgrade.
            sequence = self.con.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='runs'"
            ).fetchone()
            rid = max(int(sequence[0]) if sequence else 0, legacy_floor) + 1
            if agent is not None:
                # The worktree name carries the run id, so it is assigned here.
                # A continue works where the earlier run worked, from its base.
                cwd = str(self.worktrees / f"{AGENT_WORKTREE_PREFIX}{rid}")
                if earlier is not None:
                    cwd = earlier["worktree"]
                    agent = {
                        **agent,
                        "base_commit": earlier["base_commit"],
                        "branch": earlier["branch"],
                    }
                identity = (*identity[:3], cwd, agent["base_commit"], *identity[5:])
            self.con.execute(
                'INSERT INTO runs(run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"commit",'
                "foreground,timeout_seconds,evidence_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rid, owner, key, *identity, str(self.root / str(rid) / "log")),
            )
            if agent is not None:
                self.con.execute(
                    "INSERT INTO run_agents(run_id,harness,requested_model,requested_effort,"
                    "effective_effort,worktree,base_commit,branch,adapter_version,"
                    "continues_run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid, agent["harness"], agent["requested_model"],
                        agent["requested_effort"], agent["effective_effort"], cwd,
                        agent["base_commit"], agent["branch"], agent["adapter_version"],
                        agent["continues_run_id"],
                    ),
                )
        return self.get(rid, owner)

    def facts(self, run_id: int, owner: int, data: dict) -> dict:
        """Record what a subagent run reported; each fact is written once."""
        row = self.get(run_id, owner)
        if row["kind"] != "agent":
            raise ValueError("facts require an agent run")
        if not isinstance(data, dict) or not data or set(data) - set(_AGENT_FACTS):
            raise ValueError(f"facts accept only: {', '.join(_AGENT_FACTS)}")
        if "session_ref" in data:
            # It becomes an argument of the resume command: a leading dash would
            # be read as an option, so it is refused when written, not when used.
            route_value(data["session_ref"], "session_ref")
        if "provider_model" in data:
            _text(data["provider_model"], "provider_model")
        if "result_bytes" in data and (
            type(data["result_bytes"]) is not int or data["result_bytes"] < 0
        ):
            raise ValueError("result_bytes must be a non-negative integer")
        if "worktree_removed_at" in data:
            try:
                datetime.fromisoformat(data["worktree_removed_at"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("worktree_removed_at must be a timestamp") from exc
        with db_driver.write_transaction(self.con, "runs.facts"):
            stored = self._agent(run_id)
            if stored is None:
                raise ValueError("agent run has no facts row")
            for key, value in data.items():
                if stored[key] is not None and stored[key] != value:
                    raise ValueError(f"conflicting agent fact: {key}")
            # One fixed statement: no column name is ever taken from the request.
            self.con.execute(
                "UPDATE run_agents SET session_ref=COALESCE(?,session_ref),"
                "provider_model=COALESCE(?,provider_model),"
                "result_bytes=COALESCE(?,result_bytes),"
                "worktree_removed_at=COALESCE(?,worktree_removed_at) WHERE run_id=?",
                (
                    data.get("session_ref"), data.get("provider_model"),
                    data.get("result_bytes"), data.get("worktree_removed_at"), run_id,
                ),
            )
        return self.get(run_id, owner)

    def result(self, run_id: int, owner: int | None) -> dict:
        """A subagent's final message, bounded, with its recorded facts."""
        row = self.get(run_id, owner)
        if row["kind"] != "agent":
            raise ValueError("only an agent run has a result")
        path = Path(row["evidence_path"]).with_name(AGENT_RESULT_FILE)
        out = {
            "run_id": run_id, "state": row["state"], "exit_code": row["exit_code"],
            "available": False, "text": "", "truncated": False,
            "result_bytes": (row["agent"] or {}).get("result_bytes"),
            "agent": row["agent"],
        }
        # Only the registered engine path is read, never caller input.
        if path.is_file():
            size = path.stat().st_size
            with path.open("rb") as handle:
                text = handle.read(AGENT_RESULT_MAX).decode(errors="replace")
            out.update(available=True, text=text, truncated=size > AGENT_RESULT_MAX)
            if out["result_bytes"] is None:
                out["result_bytes"] = size
        return out

    def running(self, run_id: int, owner: int, data: dict) -> dict:
        existing = self.get(run_id, owner)
        keys = (
            "pid",
            "start_ticks",
            "supervisor_pid",
            "supervisor_start_ticks",
            "boot_id",
            "started_at",
        )
        if not isinstance(data.get("boot_id"), str) or not data["boot_id"]:
            raise ValueError("boot_id is required")
        for key in keys[:4]:
            if data.get(key) is not None and (
                type(data[key]) is not int or data[key] <= 0
            ):
                raise ValueError(f"{key} must be a positive integer")
        for key in keys:
            if (
                existing[key] is not None
                and data.get(key) is not None
                and existing[key] != data[key]
            ):
                raise ValueError(f"conflicting run incarnation: {key}")
        with db_driver.write_transaction(self.con, "runs.running"):
            self.con.execute(
                "UPDATE runs SET state='running',"
                + ",".join(f"{k}=?" for k in keys)
                + " WHERE run_id=? AND state IN ('registered','running')",
                (*(data.get(k) for k in keys), run_id),
            )
        return self.get(run_id, owner)

    def terminal(self, run_id: int, owner: int, payload: dict) -> dict:
        # The job client imports pure helpers from this module. Wake delivery
        # loads host-private launch state, so import it only on the API side.
        from sprint_message_delivery import SprintMessageStore

        allowed = {"state", "exit_code", "finished_at", "spawn_error", "probe_attempt", "agent"}
        if set(payload) - allowed or payload.get("state") not in TERMINAL:
            raise ValueError("invalid terminal payload")
        code = payload.get("exit_code")
        if code is not None and type(code) is not int:
            raise ValueError("exit_code must be an integer or null")
        if payload["state"] == "lost" and code is not None:
            raise ValueError("lost has no authoritative exit code")
        if payload["state"] == "done" and code != 0:
            raise ValueError("done requires exit_code 0")
        if payload["state"] == "failed" and (code is None or code == 0):
            raise ValueError("failed requires a nonzero exit_code")
        attempt = payload.get('probe_attempt')
        if 'probe_attempt' in payload:
            attempt = validate_probe_attempt(attempt)
            if code != attempt['exit_code']:
                raise ValueError('probe attempt exit must match terminal exit_code')
        try:
            datetime.fromisoformat(payload["finished_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError("finished_at must be a timestamp") from exc
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(canonical) > 4096:
            raise ValueError("terminal payload exceeds 4096 bytes")
        outcome = payload.get("agent")
        if "agent" in payload:
            if (
                not isinstance(outcome, dict) or set(outcome) - _AGENT_OUTCOME
                or type(outcome.get("uncommitted", False)) is not bool
                or not all(
                    c in "0123456789abcdef" for c in str(outcome.get("head_commit", ""))
                ) or len(str(outcome.get("head_commit", ""))) not in (0, 40, 64)
            ):
                raise ValueError("invalid agent outcome")
        # A dev-kit runner persists the receipt before its command returns.
        # Recover it before publishing completion, including API-outage recovery.
        row = self.get(run_id, owner)
        if outcome is not None and row["kind"] != "agent":
            raise ValueError("agent outcome requires an agent run")
        # A subagent's supervisor persists its facts the same way, so the wake
        # written below states them even when the facts route was unreachable.
        facts_path = Path(row["evidence_path"]).with_name(AGENT_FACTS_FILE)
        if row["kind"] == "agent" and row["terminal_json"] is None and facts_path.is_file():
            if facts_path.stat().st_size > 65536:
                raise ValueError("agent facts exceed 64 KiB")
            recorded = json.loads(facts_path.read_text())
            if isinstance(recorded, dict) and recorded:
                self.facts(run_id, owner, recorded)
        receipt_path = Path(row["evidence_path"]).with_name("receipt.json")
        if row["kind"] == "devkit" and not row["receipt_json"] and receipt_path.is_file():
            if receipt_path.stat().st_size > 262144:
                raise ValueError("receipt exceeds 256 KiB")
            self.receipt(run_id, owner, json.loads(receipt_path.read_text()))
        with db_driver.write_transaction(self.con, "runs.terminal"):
            row = self.get(run_id, owner)
            if attempt is not None and row['kind'] != 'probe':
                raise ValueError('probe attempt requires a probe run')
            if row["receipt_json"] and payload["state"] in {"done", "failed"} and (
                row["receipt_json"]["exit_status"] != code
            ):
                raise ValueError("terminal outcome conflicts with receipt exit status")
            if row["terminal_json"] is not None:
                if row["terminal_json"] != canonical:
                    raise ValueError("conflicting terminal outcome")
                return row
            body = wake_body(row, payload)
            receiver = self.con.execute(
                "SELECT 1 FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
                (owner,),
            ).fetchone()
            receipt = None
            inbox_id = None
            if receiver and not row["foreground"]:
                inbox_id = self.con.execute(
                    "INSERT INTO shell_messages(from_shell_id,to_shell_id,kind,body,dedupe_key) "
                    "VALUES(?,?,'result',?,?)",
                    (owner, owner, body, f"run-{run_id}-terminal"),
                ).lastrowid
                receipt = SprintMessageStore(self.con).send_to_shell_in_transaction(
                    owner,
                    message_kind="result",
                    body=body,
                    idempotency_key=f"run-{run_id}-terminal",
                    declared_type="re-enter",
                )
            self.con.execute(
                "UPDATE runs SET state=?,exit_code=?,finished_at=?,terminal_json=?,wake_state=?,"
                "wake_id=?,message_id=?,inbox_message_id=?,last_error=? WHERE run_id=?",
                (
                    payload["state"],
                    code,
                    payload["finished_at"],
                    canonical,
                    "none" if row["foreground"] else "pending" if receipt else "blocked",
                    receipt.wake_id if receipt else None,
                    receipt.message_id if receipt else None,
                    inbox_id,
                    None
                    if receipt or row["foreground"]
                    else "owner unavailable; operator recovery required",
                    run_id,
                ),
            )
        return self.get(run_id, owner)

    def receipt(self, run_id: int, owner: int, data: dict) -> dict:
        row = self.get(run_id, owner)
        if row["kind"] != "devkit":
            raise ValueError("receipts require a devkit run")
        if not isinstance(data, dict) or len(json.dumps(data).encode()) > 262144:
            raise ValueError("receipt must be an object of at most 256 KiB")
        if data.get("hook") not in {"test", "lint", "typecheck", "deps"}:
            raise ValueError("invalid receipt hook")
        if type(data.get("run_id")) is not int or data["run_id"] != run_id or data.get("commit") != row["commit"]:
            raise ValueError("receipt run/commit conflict")
        if type(data.get("exit_status")) is not int:
            raise ValueError("receipt requires an exit_status")
        if type(data.get("duration_s")) not in (int, float) or not 0 <= data["duration_s"] < 1e12:
            raise ValueError("invalid receipt duration")
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise ValueError("invalid receipt argv")
        checkout, log = data.get("checkout"), data.get("log")
        if not isinstance(checkout, str) or not Path(checkout).is_absolute():
            raise ValueError("receipt checkout must be absolute")
        if not isinstance(log, str) or Path(log).is_absolute() or ".." in Path(log).parts:
            raise ValueError("receipt log must be checkout-relative")
        summary = data.get("summary")
        if summary is not None:
            if not isinstance(summary, dict) or summary.get("framework") != "pytest":
                raise ValueError("invalid receipt summary")
            if any(type(summary.get(k)) is not int or summary[k] < 0
                   for k in ("passed", "failed", "errors", "skipped")):
                raise ValueError("invalid pytest counts")
            if not isinstance(summary.get("failing"), list) or not all(
                isinstance(v, str) for v in summary["failing"]
            ):
                raise ValueError("invalid pytest failing ids")
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        with db_driver.write_transaction(self.con, "runs.receipt"):
            row = self.get(run_id, owner)
            if row["state"] in {"done", "failed"} and row["exit_code"] != data["exit_status"]:
                raise ValueError("receipt exit status conflicts with terminal outcome")
            existing = row["receipt_json"]
            if existing is not None and existing != data:
                raise ValueError("conflicting receipt")
            self.con.execute("UPDATE runs SET receipt_json=? WHERE run_id=?", (canonical, run_id))
        return self.get(run_id, owner)

    def prune_evidence(self, run_id: int, owner: int) -> dict:
        row = self.get(run_id, owner)
        if row["state"] not in TERMINAL or row["receipt_json"] is None:
            raise ValueError("only completed receipt evidence may be pruned")
        with db_driver.write_transaction(self.con, "runs.prune_evidence"):
            self.con.execute("UPDATE runs SET evidence_pruned=1 WHERE run_id=?", (run_id,))
        return self.get(run_id, owner)

    def kill(self, run_id: int, owner: int | None) -> dict:
        row = self.get(run_id, owner)
        if row["state"] in TERMINAL:
            raise ValueError("run already terminal")
        if process_alive(row["pid"], row["start_ticks"], row["boot_id"]) is not True:
            raise ValueError("process incarnation is gone, recycled or unverifiable")
        # Marker is independent of meta.json so cancellation never races a
        # supervisor write. External signaling is outside the DB transaction.
        if (
            process_alive(
                row["supervisor_pid"], row["supervisor_start_ticks"], row["boot_id"]
            )
            is not True
        ):
            raise ValueError("supervisor incarnation is gone or unverifiable")
        Path(row["evidence_path"]).with_name("kill_requested").touch()
        # The still-owning supervisor sends TERM/KILL to its child group.
        # Signalling here could reap the leader before escalation is verified.
        return {"run_id": run_id, "kill_requested": True}

    def clean(self, run_id: int, owner: int, data: dict) -> dict:
        """Record that a subagent's worktree is being removed by `sc job clean`.

        The caller inspects and removes the worktree; the ledger holds the two
        things it cannot see from there: that no run is live in that worktree,
        and whether work was discarded, which a later continue must refuse.
        """
        row = self.get(run_id, owner)
        if row["kind"] != "agent" or not row["agent"]:
            raise ValueError("only a subagent run has a worktree to clean")
        discard = data.get("discard", False) if isinstance(data, dict) else None
        if type(discard) is not bool or set(data) - {"discard"}:
            raise ValueError("clean accepts only a boolean discard")
        worktree = row["agent"]["worktree"]
        gone = self._agents_without_a_process()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db_driver.write_transaction(self.con, "runs.clean"):
            live = [
                found["run_id"] for found in self.con.execute(
                    "SELECT r.run_id FROM runs r JOIN run_agents a USING(run_id) "
                    "WHERE a.worktree=? AND r.state IN ('registered','running') "
                    "ORDER BY r.run_id", (worktree,),
                )
                if found["run_id"] not in gone
            ]
            if live:
                raise ValueError(
                    f"cannot clean run {run_id}: run {live[0]} is live in its worktree. "
                    "Wait for it to end, or stop it with `sc job kill`."
                )
            self.con.execute(
                "UPDATE run_agents SET worktree_removed_at=COALESCE(worktree_removed_at,?),"
                "worktree_discarded_at=CASE WHEN ? THEN COALESCE(worktree_discarded_at,?) "
                "ELSE worktree_discarded_at END WHERE run_id=?",
                (now, int(discard), now, run_id),
            )
        return self.get(run_id, owner)

    @staticmethod
    def _stop_orphan(row: dict) -> None:
        """Stop a subagent's harness when its supervisor is gone.

        The harness runs as the leader of its own process group, so it outlives
        a supervisor that was killed. Nothing would then stop it at its time
        limit, and it would go on working under a run recorded as lost. The
        group is signalled only while its leader is the recorded incarnation
        (pid, start ticks and boot id all match); anything else is left alone.
        """
        if process_alive(row.get("pid"), row.get("start_ticks"), row.get("boot_id")) is True:
            try:
                os.killpg(int(row["pid"]), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def reconcile(self) -> None:
        rows = self.con.execute(
            "SELECT * FROM runs WHERE state IN ('registered','running') "
            "AND wake_state<>'blocked'"
        ).fetchall()
        for raw in rows:
            try:
                self._reconcile_row(dict(raw))
            except Exception as exc:  # noqa: BLE001 - row evidence must not halt engine wake delivery
                reason = f"{type(exc).__name__}: {exc}"[:512]
                with db_driver.write_transaction(self.con, "runs.reconcile_blocked"):
                    self.con.execute(
                        "UPDATE runs SET wake_state='blocked',last_error=? "
                        "WHERE run_id=? AND state IN ('registered','running')",
                        (
                            "reconciliation blocked; operator recovery required: "
                            + reason,
                            raw["run_id"],
                        ),
                    )

    def _reconcile_row(self, row: dict) -> None:
        path = Path(row["evidence_path"]).with_name("meta.json")
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("run_id") != row["run_id"]:
            meta = {}
        if meta.get("finished_at"):
            self.terminal(row["run_id"], row["owner_shell_id"], terminal_payload(meta))
            return
        if meta.get("supervisor_pid"):
            self.running(row["run_id"], row["owner_shell_id"], meta)
            row.update(meta)
        alive = process_alive(
            row.get("supervisor_pid"),
            row.get("supervisor_start_ticks"),
            row.get("boot_id"),
        )
        if alive is True or (alive is None and row.get("supervisor_pid")):
            return
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
        ).total_seconds()
        if alive is None and age < 30:
            return
        # Re-read after proving the supervisor gone: it may have persisted
        # its terminal result between our first read and the liveness check.
        try:
            final = json.loads(path.read_text())
        except (OSError, ValueError):
            final = {}
        if self.get(row["run_id"])["state"] in TERMINAL:
            return
        if final.get("run_id") == row["run_id"] and final.get("finished_at"):
            self.terminal(row["run_id"], row["owner_shell_id"], terminal_payload(final))
            return
        if row["kind"] == "agent":
            self._stop_orphan(row)
        self.terminal(
            row["run_id"],
            row["owner_shell_id"],
            {
                "state": "lost",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
