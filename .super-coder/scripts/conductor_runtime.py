#!/usr/bin/env python3
"""Deterministic Conductor mechanics.

The OpenCode Conductor shell reads the transition table rendered from this
module plus the matching ``sprint_cond`` skill and calls
``sc directives act <id>``. All authority stays here: payload validation,
board transitions, role-slot launches, refusal, and the durable action trail
are code, not model judgement.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from conductor_policy import CONDUCTOR_HARNESS, DEFAULT_CONDUCTOR_MODEL
import shell_liveness
import sprint_lifecycle
import sprint_state
from sprint_units import SPRINT_UNIT_EDGES, SprintTransitionError, check_transition

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
INSTANCE_CONFIG = ENGINE / "instance.json"
_LAUNCH_GUARD_SECONDS = 20
LAUNCH_LOG = ENGINE / "logs" / "conductor-launches.log"


@dataclass(frozen=True)
class ConductorConfig:
    enabled: bool = False
    shell: str = "CON1"
    model: str = DEFAULT_CONDUCTOR_MODEL


class ConductorConfigError(ValueError):
    """An enabled Conductor configuration that cannot safely wake."""


class DirectiveRefused(ValueError):
    """A pending directive that cannot be mechanically executed."""


class ConductorLaunchError(RuntimeError):
    """A role or Conductor process that refused before it could start."""


# issuer, kind, mechanical action, success condition.  The renderer and runtime
# tests consume the same rows so the boot table cannot drift from the executor.
TRANSITIONS = (
    (
        "dev",
        "ready-for-review",
        "record PR + move in_review; boot reviewer",
        "reviewer slot starts at the exact head",
    ),
    (
        "dev",
        "ask-planner",
        "move blocked; boot recorded originating Planner with evidence",
        "originating Planner receives the question",
    ),
    (
        "dev",
        "merged",
        "move merged; release every newly dependency-ready unit",
        "normal merge never boots Planner",
    ),
    (
        "dev",
        "unit-report",
        "record report; boot originating Planner only when all units are terminal",
        "ordinary reports never boot Planner",
    ),
    (
        "reviewer",
        "review-clean",
        "record review head; boot dev",
        "developer receives approval for the exact head",
    ),
    (
        "reviewer",
        "findings",
        "relay findings to dev or planner",
        "the responsible slot receives every finding",
    ),
    (
        "reviewer",
        "ask-planner",
        "move blocked; boot recorded originating Planner with evidence",
        "originating Planner receives the question",
    ),
    (
        "planner",
        "handoff",
        "validate board; activate sprint; boot every dependency-ready developer",
        "authoritative state is active and each released unit is working",
    ),
    (
        "planner",
        "kickoff",
        "move released unit; boot target slot",
        "the declared target starts with bounded context",
    ),
    ("planner", "hold", "move active unit blocked", "board records the hold"),
    (
        "planner",
        "re-scope",
        "move working; reboot target dev",
        "developer receives the new boundary",
    ),
    (
        "planner",
        "re-task",
        "move working; reboot target dev",
        "developer receives the replacement path",
    ),
    (
        "planner",
        "close",
        "freeze sprint after terminal-unit check",
        "sprint is structurally closed",
    ),
    (
        "planner",
        "answer",
        "restore target role state; reboot asker",
        "asker receives the ruling",
    ),
    (
        "system",
        "pr-green",
        "boot assigned reviewer",
        "reviewer receives the green-head evidence",
    ),
    (
        "system",
        "pr-red",
        "move blocked; boot assigned dev",
        "developer receives failing-check evidence",
    ),
    (
        "system",
        "pr-merged",
        "move merged; release every newly dependency-ready unit",
        "normal merge never boots Planner",
    ),
    (
        "system",
        "stall",
        "move blocked when legal; boot originating Planner",
        "originating Planner receives the full dwell snapshot",
    ),
    (
        "system",
        "dead-shell",
        "move blocked when legal; boot originating Planner",
        "originating Planner receives the liveness snapshot",
    ),
)
_TRANSITION_SET = {(issuer, kind) for issuer, kind, _action, _pass in TRANSITIONS}

_wake_lock = threading.Lock()
_act_lock = threading.Lock()
_launching_until = 0.0


def load_config(path: Path = INSTANCE_CONFIG) -> ConductorConfig:
    """Read the top-level ``conductor`` block; absent means disabled."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return ConductorConfig()
    except OSError as exc:
        raise ConductorConfigError(f"cannot read conductor config: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConductorConfigError(f"instance.json is not valid JSON: {exc}") from exc
    block = raw.get("conductor")
    if not isinstance(block, dict):
        return ConductorConfig()
    enabled = block.get("enabled", False)
    shell = block.get("shell", "CON1")
    model = block.get("model", DEFAULT_CONDUCTOR_MODEL)
    if not isinstance(enabled, bool):
        raise ConductorConfigError("conductor.enabled must be boolean")
    if not isinstance(shell, str) or not shell.strip():
        raise ConductorConfigError("conductor.shell must be a nonblank shortname")
    if not isinstance(model, str) or not model.strip():
        raise ConductorConfigError("conductor.model must be a nonblank route")
    return ConductorConfig(enabled, shell.strip(), model.strip())


def doctor(
    con,
    config: ConductorConfig,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable = subprocess.run,
) -> dict:
    """Fail an enabled configuration before the service starts."""
    if not config.enabled:
        return {"enabled": False, "ok": True}
    if which(CONDUCTOR_HARNESS) is None:
        raise ConductorConfigError("OpenCode CLI is not installed on PATH")
    shell = con.execute(
        "SELECT shell_id,shortname,flavor,api_key FROM shells "
        "WHERE shortname=? COLLATE NOCASE AND COALESCE(is_deleted,0)=0",
        (config.shell,),
    ).fetchone()
    if shell is None:
        raise ConductorConfigError(f"conductor shell {config.shell!r} does not exist")
    if shell["flavor"] != "conductor":
        raise ConductorConfigError(
            f"shell {config.shell!r} is {shell['flavor'] or 'bespoke'}, not conductor"
        )
    if not shell["api_key"]:
        raise ConductorConfigError(
            f"conductor shell {config.shell!r} has no API credential"
        )
    grants = [
        row[0]
        for row in con.execute(
            "SELECT sk.name FROM flavor_skills fs "
            "JOIN skills sk ON sk.skill_id=fs.skill_id "
            "WHERE fs.flavor='conductor' AND COALESCE(sk.is_deleted,0)=0 "
            "ORDER BY sk.name"
        )
    ]
    direct = con.execute(
        "SELECT COUNT(*) FROM shell_skills WHERE shell_id=?",
        (shell["shell_id"],),
    ).fetchone()[0]
    if grants != ["sprint_cond"] or direct:
        raise ConductorConfigError(
            "conductor must have exactly the sprint_cond flavor skill and no "
            "direct grants"
        )
    try:
        models = run(
            [CONDUCTOR_HARNESS, "models"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConductorConfigError(f"OpenCode credential probe failed: {exc}") from exc
    if models.returncode != 0:
        detail = (models.stderr or models.stdout or "unknown error").strip()
        raise ConductorConfigError(f"OpenCode credential probe failed: {detail}")
    available = {line.strip() for line in models.stdout.splitlines() if line.strip()}
    if config.model not in available:
        raise ConductorConfigError(
            f"OpenCode route {config.model!r} is not locally runnable; "
            "authenticate OpenCode or select a listed model"
        )
    return {
        "enabled": True,
        "ok": True,
        "shell_id": shell["shell_id"],
        "shell": shell["shortname"],
        "harness": CONDUCTOR_HARNESS,
        "model": config.model,
    }


def _pending_ids(con) -> list[int]:
    return [
        row[0]
        for row in con.execute(
            "SELECT directive_id FROM directives "
            "WHERE status='pending' AND target='conductor' "
            "ORDER BY directive_id"
        )
    ]


def _default_launcher(command: list[str]) -> int:
    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_LOG.open("a") as log:
        proc = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={**os.environ, "SC_NO_AUTOPRUNE": "1"},
        )
        time.sleep(0.2)
        returncode = proc.poll()
    if returncode not in (None, 0):
        raise ConductorLaunchError(
            f"launch pid {proc.pid} exited {returncode}; see {LAUNCH_LOG}"
        )
    return proc.pid


def _launch_is_live(con, shell_id: int) -> bool:
    row = con.execute(
        "SELECT shell_id,pid,start_ticks,worktree,harness,launched_at "
        "FROM shell_launch_records WHERE shell_id=?",
        (shell_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        return shell_liveness.claim_live(dict(row)) is True
    except (OSError, ValueError):
        return False


def maybe_wake(
    con,
    *,
    config: ConductorConfig | None = None,
    launcher: Callable[[list[str]], int] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Boot one ephemeral Conductor when a pending row arrives."""
    global _launching_until
    config = config or load_config()
    launcher = launcher or _default_launcher
    if not config.enabled:
        return {"enabled": False, "launched": False}
    checked = doctor(con, config)
    pending = _pending_ids(con)
    if not pending:
        return {**checked, "launched": False, "reason": "no-pending"}
    with _wake_lock:
        current = now()
        if current < _launching_until:
            return {**checked, "launched": False, "reason": "launching"}
        if _launch_is_live(con, checked["shell_id"]):
            return {**checked, "launched": False, "reason": "already-live"}
        prompt = (
            "Process pending Conductor directives in ascending id order. "
            "For each id run `sc directives act <id>`, inspect the result, "
            "and continue until `sc directives list --status pending` is empty."
        )
        command = [
            str(REPO_ROOT / "sc"),
            "run",
            checked["shell"],
            "--harness",
            CONDUCTOR_HARNESS,
            "--model",
            config.model,
            "--prompt",
            prompt,
        ]
        pid = launcher(command)
        _launching_until = current + _LAUNCH_GUARD_SECONDS
    return {
        **checked,
        "launched": True,
        "pid": pid,
        "pending": pending,
    }


def _payload(row) -> dict:
    try:
        body = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise DirectiveRefused("payload is not valid JSON") from exc
    if not isinstance(body, dict):
        raise DirectiveRefused("payload must be a JSON object")
    return body


def _text(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DirectiveRefused(f"payload.{field} must be a nonblank string")
    return value.strip()


def _integer(payload: dict, field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DirectiveRefused(f"payload.{field} must be an integer")
    return value


def _unit(con, row):
    unit_id = row["unit_id"]
    sprint_id = row["sprint_doc_id"]
    if unit_id is None or sprint_id is None:
        raise DirectiveRefused("directive requires sprint_doc_id and unit_id")
    unit = con.execute(
        "SELECT * FROM sprint_units WHERE unit_id=? AND sprint_doc_id=?",
        (unit_id, sprint_id),
    ).fetchone()
    if unit is None:
        raise DirectiveRefused("linked sprint unit does not exist")
    return unit


def _shell(con, shortname: str, flavor: str | None = None):
    row = con.execute(
        "SELECT shell_id,shortname,flavor FROM shells "
        "WHERE shortname=? COLLATE NOCASE AND COALESCE(is_deleted,0)=0",
        (shortname,),
    ).fetchone()
    if row is None:
        raise DirectiveRefused(f"target shell {shortname!r} does not exist")
    if flavor is not None and row["flavor"] != flavor:
        raise DirectiveRefused(f"target shell {shortname!r} is not {flavor}")
    return row


def _planner_for_sprint(con, sprint_id: int):
    try:
        return sprint_lifecycle.planner_for_sprint(con, sprint_id)
    except sprint_lifecycle.SprintLifecycleError as exc:
        raise DirectiveRefused(str(exc)) from exc


def _route_for_slot(con, sprint_id: int, slot: str) -> tuple[str, str]:
    role = {"plan": "planner", "dev": "dev", "rev": "reviewer"}[slot]
    try:
        return sprint_lifecycle.route_for_role(con, sprint_id, role)
    except sprint_lifecycle.SprintLifecycleError as exc:
        raise DirectiveRefused(str(exc)) from exc


def _assert_issuer_assignment(row, unit) -> None:
    if row["issuer_flavor"] == "dev" and row["issuer_shell_id"] != unit["dev_shell_id"]:
        raise DirectiveRefused("dev issuer is not the unit's assigned developer")
    if (
        row["issuer_flavor"] == "reviewer"
        and row["issuer_shell_id"] != unit["reviewer_shell_id"]
    ):
        raise DirectiveRefused("reviewer issuer is not the unit's assigned reviewer")


def _move(con, unit, state: str, actor_shell_id: int) -> None:
    try:
        check_transition(SPRINT_UNIT_EDGES, unit["state"], state)
    except SprintTransitionError as exc:
        raise DirectiveRefused(str(exc)) from exc
    con.execute(
        "UPDATE sprint_units SET state=?,"
        " state_changed_at=CASE WHEN state<>? THEN datetime('now') "
        " ELSE state_changed_at END,"
        " updated_at=datetime('now'),updated_by_shell_id=? WHERE unit_id=?",
        (state, state, actor_shell_id, unit["unit_id"]),
    )


def _block_when_legal(con, unit, actor_shell_id: int) -> None:
    if unit["state"] in ("working", "in_review"):
        _move(con, unit, "blocked", actor_shell_id)


def _slot_command(
    shell,
    slot: str,
    sprint_id: int,
    *,
    unit_seq: str | None,
    prompt: str,
    harness: str,
    model: str,
) -> list[str]:
    command = [
        str(REPO_ROOT / "sc"),
        "run",
        shell["shortname"],
        "--slot",
        slot,
        "--sprint",
        str(sprint_id),
        "--await-sprint-active",
        "--harness",
        harness,
        "--model",
        model,
    ]
    if unit_seq is not None:
        command += ["--unit", unit_seq]
    command += ["--prompt", prompt]
    return command


def _spawn(
    con,
    launches: list[list[str]],
    shell,
    slot: str,
    row,
    payload: dict,
    *,
    unit=None,
) -> None:
    prompt = json.dumps(
        {
            "directive_id": row["directive_id"],
            "issuer": row["issuer_flavor"],
            "kind": row["kind"],
            "payload": payload,
        },
        sort_keys=True,
    )
    harness, model = _route_for_slot(con, row["sprint_doc_id"], slot)
    launches.append(
        _slot_command(
            shell,
            slot,
            row["sprint_doc_id"],
            unit_seq=unit["seq"] if unit is not None else None,
            prompt=prompt,
            harness=harness,
            model=model,
        )
    )


def _dependency_tokens(raw: str | None) -> list[str]:
    return [token.strip() for token in (raw or "").split(",") if token.strip()]


def _all_units_terminal(con, sprint_id: int) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM sprint_units WHERE sprint_doc_id=? "
        "AND state NOT IN ('merged','cancelled')",
        (sprint_id,),
    ).fetchone()[0] == 0


def _ready_pending_units(con, sprint_id: int):
    units = con.execute(
        "SELECT * FROM sprint_units WHERE sprint_doc_id=? ORDER BY unit_id",
        (sprint_id,),
    ).fetchall()
    by_seq = {unit["seq"]: unit for unit in units}
    ready = []
    for unit in units:
        if unit["state"] != "pending":
            continue
        deps = _dependency_tokens(unit["depends_on"])
        if all(
            dep in by_seq and by_seq[dep]["state"] in ("merged", "cancelled")
            for dep in deps
        ):
            ready.append(unit)
    return ready


def _release_ready_units(con, launches, row, payload, actor_shell_id: int):
    released = []
    for unit in _ready_pending_units(con, row["sprint_doc_id"]):
        dev = con.execute(
            "SELECT shell_id,shortname,flavor FROM shells "
            "WHERE shell_id=? AND flavor='dev' AND COALESCE(is_deleted,0)=0",
            (unit["dev_shell_id"],),
        ).fetchone()
        if dev is None:
            raise DirectiveRefused(
                f"unit {unit['seq']} has no active assigned developer"
            )
        _move(con, unit, "working", actor_shell_id)
        _spawn(con, launches, dev, "dev", row, payload, unit=unit)
        released.append(unit["seq"])
    return released


def _validate_handoff_board(con, sprint_id: int) -> None:
    units = con.execute(
        "SELECT seq,state,dev_shell_id,reviewer_shell_id,depends_on "
        "FROM sprint_units WHERE sprint_doc_id=? ORDER BY unit_id",
        (sprint_id,),
    ).fetchall()
    if not units:
        raise DirectiveRefused("handoff requires a non-empty sprint board")
    seqs = {unit["seq"] for unit in units}
    gaps = []
    dependencies = {}
    for unit in units:
        if unit["state"] != "pending":
            gaps.append(f"{unit['seq']} is {unit['state']}, not pending")
        if unit["dev_shell_id"] is None:
            gaps.append(f"{unit['seq']} missing developer")
        elif con.execute(
            "SELECT 1 FROM shells WHERE shell_id=? AND flavor='dev' "
            "AND COALESCE(is_deleted,0)=0",
            (unit["dev_shell_id"],),
        ).fetchone() is None:
            gaps.append(f"{unit['seq']} developer is not an active dev shell")
        if unit["reviewer_shell_id"] is None:
            gaps.append(f"{unit['seq']} missing reviewer")
        elif con.execute(
            "SELECT 1 FROM shells WHERE shell_id=? AND flavor='reviewer' "
            "AND COALESCE(is_deleted,0)=0",
            (unit["reviewer_shell_id"],),
        ).fetchone() is None:
            gaps.append(
                f"{unit['seq']} reviewer is not an active reviewer shell"
            )
        dependencies[unit["seq"]] = set(_dependency_tokens(unit["depends_on"]))
        for dep in dependencies[unit["seq"]]:
            if dep == unit["seq"]:
                gaps.append(f"{unit['seq']} depends on itself")
            elif dep not in seqs:
                gaps.append(f"{unit['seq']} names unknown dependency {dep}")
    if gaps:
        raise DirectiveRefused("handoff board invalid: " + "; ".join(gaps))
    remaining = {seq: set(deps) for seq, deps in dependencies.items()}
    while remaining:
        roots = {seq for seq, deps in remaining.items() if not deps}
        if not roots:
            cycle = ", ".join(sorted(remaining))
            raise DirectiveRefused(
                f"handoff board has a dependency cycle among: {cycle}"
            )
        remaining = {
            seq: deps - roots
            for seq, deps in remaining.items()
            if seq not in roots
        }
    if not _ready_pending_units(con, sprint_id):
        raise DirectiveRefused(
            "handoff board has no dependency-ready unit (dependency cycle)"
        )


def _execute(con, row, payload: dict, actor_shell_id: int) -> list[list[str]]:
    issuer, kind = row["issuer_flavor"], row["kind"]
    if (issuer, kind) not in _TRANSITION_SET:
        raise DirectiveRefused(f"no transition for {issuer}:{kind}")
    if row["target"] != "conductor":
        raise DirectiveRefused("directive target must be conductor")
    sprint_id = row["sprint_doc_id"]
    sprint = (
        sprint_lifecycle.sprint_row(con, sprint_id)
        if sprint_id is not None else None
    )
    required_state = "declared" if (issuer, kind) == ("planner", "handoff") \
        else "active"
    if sprint is None or sprint["state"] != required_state:
        raise DirectiveRefused(
            f"directive requires a {required_state} authoritative sprint"
        )
    if issuer == "planner":
        planner = _planner_for_sprint(con, sprint_id)
        if row["issuer_shell_id"] != planner["shell_id"]:
            raise DirectiveRefused(
                f"planner issuer is not sprint {sprint_id}'s originating Planner"
            )
    launches: list[list[str]] = []

    if issuer == "dev":
        unit = _unit(con, row)
        _assert_issuer_assignment(row, unit)
        if kind == "ready-for-review":
            pr = _integer(payload, "pr_number")
            head = _text(payload, "head")
            branch = _text(payload, "branch")
            if payload.get("checks") != "green":
                raise DirectiveRefused("payload.checks must be 'green'")
            con.execute(
                "UPDATE sprint_units SET pr_number=?,branch=?,"
                "updated_at=datetime('now'),updated_by_shell_id=? "
                "WHERE unit_id=?",
                (pr, branch, actor_shell_id, unit["unit_id"]),
            )
            _move(con, unit, "in_review", actor_shell_id)
            reviewer = con.execute(
                "SELECT shell_id,shortname,flavor FROM shells "
                "WHERE shell_id=? AND flavor='reviewer' "
                "AND COALESCE(is_deleted,0)=0",
                (unit["reviewer_shell_id"],),
            ).fetchone()
            if reviewer is None:
                raise DirectiveRefused("unit has no assigned reviewer")
            _spawn(con, launches, reviewer, "rev", row, payload, unit=unit)
        elif kind == "ask-planner":
            _text(payload, "question")
            _block_when_legal(con, unit, actor_shell_id)
            _spawn(
                con, launches, _planner_for_sprint(con, sprint_id),
                "plan", row, payload,
            )
        elif kind == "merged":
            pr_number = _integer(payload, "pr_number")
            head = _text(payload, "head")
            _text(payload, "merge_sha")
            if unit["pr_number"] != pr_number:
                raise DirectiveRefused("merged PR does not match the recorded unit PR")
            if unit["review_head"] != head:
                raise DirectiveRefused(
                    "merged head does not match the recorded review head"
                )
            _move(con, unit, "merged", actor_shell_id)
            _release_ready_units(con, launches, row, payload, actor_shell_id)
        elif kind == "unit-report":
            _text(payload, "shipped")
            if _all_units_terminal(con, sprint_id):
                _spawn(
                    con, launches, _planner_for_sprint(con, sprint_id),
                    "plan", row, payload,
                )
        return launches

    if issuer == "reviewer":
        unit = _unit(con, row) if row["unit_id"] is not None else None
        if unit is not None:
            _assert_issuer_assignment(row, unit)
        if kind == "ask-planner":
            _text(payload, "question")
            if unit is not None:
                _block_when_legal(con, unit, actor_shell_id)
            _spawn(
                con, launches, _planner_for_sprint(con, sprint_id),
                "plan", row, payload,
            )
        elif kind == "review-clean":
            if unit is None:
                if payload.get("mode") != "conformance":
                    raise DirectiveRefused(
                        "unitless review-clean requires mode=conformance"
                    )
                _text(payload, "main_sha")
                _spawn(
                    con, launches, _planner_for_sprint(con, sprint_id),
                    "plan", row, payload,
                )
            else:
                if unit["state"] != "in_review":
                    raise DirectiveRefused("review-clean requires an in_review unit")
                head = _text(payload, "head")
                con.execute(
                    "UPDATE sprint_units SET review_head=?,"
                    "updated_at=datetime('now'),updated_by_shell_id=? "
                    "WHERE unit_id=?",
                    (head, actor_shell_id, unit["unit_id"]),
                )
                dev = con.execute(
                    "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
                    (unit["dev_shell_id"],),
                ).fetchone()
                _spawn(con, launches, dev, "dev", row, payload, unit=unit)
        elif kind == "findings":
            findings = payload.get("findings")
            if not isinstance(findings, list) or not findings:
                raise DirectiveRefused("payload.findings must be a nonempty array")
            if unit is None:
                _spawn(
                    con, launches, _planner_for_sprint(con, sprint_id),
                    "plan", row, payload,
                )
            else:
                dev = con.execute(
                    "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
                    (unit["dev_shell_id"],),
                ).fetchone()
                _spawn(con, launches, dev, "dev", row, payload, unit=unit)
        return launches

    if issuer == "planner":
        sprint_id = row["sprint_doc_id"]
        if sprint_id is None:
            raise DirectiveRefused("planner directive requires sprint_doc_id")
        unit = _unit(con, row) if row["unit_id"] is not None else None
        if kind == "handoff":
            if unit is not None:
                raise DirectiveRefused("handoff is sprint-scoped, not unit-scoped")
            if payload not in ({}, {"verified": True}):
                raise DirectiveRefused(
                    "handoff payload must be empty or {'verified': true}"
                )
            _validate_handoff_board(con, sprint_id)
            sprint_lifecycle.transition(con, sprint_id, "active")
            _release_ready_units(con, launches, row, payload, actor_shell_id)
        elif kind == "kickoff":
            target = _shell(con, _text(payload, "to"))
            if target["flavor"] == "dev":
                if unit is None:
                    raise DirectiveRefused("dev kickoff requires unit_id")
                _text(payload, "instruction")
                if unit["dev_shell_id"] != target["shell_id"]:
                    raise DirectiveRefused(
                        "kickoff target is not the assigned developer"
                    )
                if unit["state"] in ("pending", "blocked"):
                    _move(con, unit, "working", actor_shell_id)
                _spawn(con, launches, target, "dev", row, payload, unit=unit)
            elif target["flavor"] == "reviewer":
                if unit is None and payload.get("mode") != "conformance":
                    raise DirectiveRefused(
                        "unitless reviewer kickoff requires mode=conformance"
                    )
                _spawn(con, launches, target, "rev", row, payload, unit=unit)
            else:
                raise DirectiveRefused("kickoff target must be dev or reviewer")
        elif kind == "hold":
            _text(payload, "reason")
            if unit is not None:
                _block_when_legal(con, unit, actor_shell_id)
        elif kind in ("re-task", "re-scope"):
            if unit is None:
                raise DirectiveRefused(f"{kind} requires unit_id")
            target = _shell(con, _text(payload, "to"), "dev")
            _text(payload, "instruction" if kind == "re-task" else "scope")
            _text(payload, "reason")
            if unit["dev_shell_id"] != target["shell_id"]:
                raise DirectiveRefused(f"{kind} target is not the assigned developer")
            if unit["state"] == "blocked":
                _move(con, unit, "working", actor_shell_id)
            _spawn(con, launches, target, "dev", row, payload, unit=unit)
        elif kind == "answer":
            if unit is None:
                raise DirectiveRefused("answer requires unit_id")
            target = _shell(con, _text(payload, "to"))
            _integer(payload, "question_directive_id")
            _text(payload, "answer")
            if target["flavor"] == "dev":
                if unit["state"] == "blocked":
                    _move(con, unit, "working", actor_shell_id)
                _spawn(con, launches, target, "dev", row, payload, unit=unit)
            elif target["flavor"] == "reviewer":
                if unit["state"] == "blocked":
                    _move(con, unit, "working", actor_shell_id)
                    unit = con.execute(
                        "SELECT * FROM sprint_units WHERE unit_id=?",
                        (unit["unit_id"],),
                    ).fetchone()
                    _move(con, unit, "in_review", actor_shell_id)
                _spawn(con, launches, target, "rev", row, payload, unit=unit)
            else:
                raise DirectiveRefused("answer target must be dev or reviewer")
        elif kind == "close":
            main_sha = _text(payload, "main_sha")
            conformance_id = _integer(payload, "conformance_directive_id")
            _text(payload, "summary")
            nonterminal = con.execute(
                "SELECT COUNT(*) FROM sprint_units WHERE sprint_doc_id=? "
                "AND state NOT IN ('merged','cancelled')",
                (sprint_id,),
            ).fetchone()[0]
            if nonterminal:
                raise DirectiveRefused(
                    f"cannot close with {nonterminal} nonterminal unit(s)"
                )
            conformance = con.execute(
                "SELECT issuer_flavor,kind,status,sprint_doc_id,unit_id,payload "
                "FROM directives WHERE directive_id=?",
                (conformance_id,),
            ).fetchone()
            if (
                conformance is None
                or conformance["issuer_flavor"] != "reviewer"
                or conformance["kind"] != "review-clean"
                or conformance["status"] != "executed"
                or conformance["sprint_doc_id"] != sprint_id
                or conformance["unit_id"] is not None
            ):
                raise DirectiveRefused(
                    "close requires an executed unitless reviewer "
                    "review-clean directive from this sprint"
                )
            verdict = _payload(conformance)
            if (
                verdict.get("mode") != "conformance"
                or verdict.get("main_sha") != main_sha
                or verdict.get("findings") not in (None, [])
            ):
                raise DirectiveRefused(
                    "close conformance must be clean and match main_sha"
                )
            con.execute(
                "UPDATE documents SET frozen=1,frozen_date=date('now'),"
                "updated_at=datetime('now') WHERE document_id=?",
                (sprint_id,),
            )
            sprint_lifecycle.transition(con, sprint_id, "closing")
            sprint_lifecycle.transition(con, sprint_id, "closed")
        return launches

    unit = _unit(con, row)
    if kind == "pr-green":
        if unit["state"] != "in_review":
            raise DirectiveRefused("pr-green requires an in_review unit")
        reviewer = con.execute(
            "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
            (unit["reviewer_shell_id"],),
        ).fetchone()
        if reviewer is None:
            raise DirectiveRefused("unit has no assigned reviewer")
        _spawn(con, launches, reviewer, "rev", row, payload, unit=unit)
    elif kind == "pr-red":
        if unit["state"] != "working":
            _move(con, unit, "working", actor_shell_id)
        dev = con.execute(
            "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
            (unit["dev_shell_id"],),
        ).fetchone()
        _spawn(con, launches, dev, "dev", row, payload, unit=unit)
    elif kind == "pr-merged":
        _move(con, unit, "merged", actor_shell_id)
        _release_ready_units(con, launches, row, payload, actor_shell_id)
        if _all_units_terminal(con, sprint_id):
            _spawn(
                con, launches, _planner_for_sprint(con, sprint_id),
                "plan", row, payload,
            )
    elif kind in ("stall", "dead-shell"):
        _block_when_legal(con, unit, actor_shell_id)
        _spawn(
            con, launches, _planner_for_sprint(con, sprint_id),
            "plan", row, payload,
        )
    return launches


def _trail(con, row, actor_shell_id: int, event_kind: str, evidence: dict) -> None:
    con.execute(
        "INSERT INTO sentinel_events "
        "(event_kind,shell_id,sprint_doc_id,unit_id,directive_id,evidence) "
        "VALUES (?,?,?,?,?,?)",
        (
            event_kind,
            actor_shell_id,
            row["sprint_doc_id"],
            row["unit_id"],
            row["directive_id"],
            json.dumps(evidence, sort_keys=True),
        ),
    )


def _act_locked(
    con,
    directive_id: int,
    actor_shell_id: int,
    *,
    launcher: Callable[[list[str]], int] | None = None,
) -> dict:
    launcher = launcher or _default_launcher
    actor = con.execute(
        "SELECT shell_id,flavor FROM shells "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
        (actor_shell_id,),
    ).fetchone()
    if actor is None or actor["flavor"] != "conductor":
        raise PermissionError("directive act requires a conductor shell")
    row = con.execute(
        "SELECT * FROM directives WHERE directive_id=?",
        (directive_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no directive {directive_id}")
    if row["status"] != "pending":
        return {
            "directive_id": directive_id,
            "status": row["status"],
            "replayed": True,
        }

    con.execute("SAVEPOINT conductor_act")
    try:
        payload = _payload(row)
        launches = _execute(con, row, payload, actor_shell_id)
        pids = [launcher(command) for command in launches]
        con.execute("RELEASE conductor_act")
        con.execute(
            "UPDATE directives SET status='executed',"
            "executed_at=datetime('now') WHERE directive_id=?",
            (directive_id,),
        )
        evidence = {
            "issuer": row["issuer_flavor"],
            "kind": row["kind"],
            "launches": launches,
            "pids": pids,
        }
        _trail(con, row, actor_shell_id, "conductor-executed", evidence)
        con.commit()
        return {
            "directive_id": directive_id,
            "status": "executed",
            "launches": launches,
            "pids": pids,
        }
    except DirectiveRefused as exc:
        con.execute("ROLLBACK TO conductor_act")
        con.execute("RELEASE conductor_act")
        reason = str(exc)
        con.execute(
            "UPDATE directives SET status='refused',refusal_reason=?,"
            "executed_at=datetime('now') WHERE directive_id=?",
            (reason, directive_id),
        )
        escalation = None
        try:
            if row["sprint_doc_id"] is None:
                raise DirectiveRefused("cannot escalate without sprint_doc_id")
            planner = _planner_for_sprint(con, row["sprint_doc_id"])
            harness, model = _route_for_slot(
                con, row["sprint_doc_id"], "plan"
            )
            prompt = json.dumps(
                {
                    "directive_id": directive_id,
                    "refusal": reason,
                    "issuer": row["issuer_flavor"],
                    "kind": row["kind"],
                    "payload": row["payload"],
                },
                sort_keys=True,
            )
            command = _slot_command(
                planner,
                "plan",
                row["sprint_doc_id"],
                unit_seq=None,
                prompt=prompt,
                harness=harness,
                model=model,
            )
            escalation = {"command": command, "pid": launcher(command)}
        except DirectiveRefused as escalation_error:
            escalation = {"error": str(escalation_error)}
        _trail(
            con,
            row,
            actor_shell_id,
            "conductor-refused",
            {"reason": reason, "escalation": escalation},
        )
        con.commit()
        return {
            "directive_id": directive_id,
            "status": "refused",
            "reason": reason,
            "escalation": escalation,
        }
    except Exception:
        con.execute("ROLLBACK TO conductor_act")
        con.execute("RELEASE conductor_act")
        raise


def act(
    con,
    directive_id: int,
    actor_shell_id: int,
    *,
    launcher: Callable[[list[str]], int] | None = None,
) -> dict:
    """Execute or refuse one pending directive as a Conductor shell."""
    with _act_lock:
        return _act_locked(
            con,
            directive_id,
            actor_shell_id,
            launcher=launcher,
        )


def render_boot(con, shell) -> str:
    """Render the Conductor's complete transition-table boot doc."""
    pending = con.execute(
        "SELECT directive_id,issuer_flavor,kind,sprint_doc_id,unit_id "
        "FROM directives WHERE status='pending' AND target='conductor' "
        "ORDER BY directive_id"
    ).fetchall()
    lines = [
        "# CONDUCTOR — MECHANICAL RELAY",
        "",
        "| Invariant | Required result |",
        "|---|---|",
        "| Never decide | Execute only a row below through `sc directives act <id>` |",
        "| Never invent data | A missing/malformed payload is refused and escalated by the command |",
        "| Never poll | Drain the current pending list once, then exit |",
        "| Never write directly | Board/session changes occur only inside the authenticated act command |",
        "| Never select an owner or route | Use the recorded originating Planner and each stored role route |",
        "",
        "| Issuer | Kind | Mechanical action | Pass |",
        "|---|---|---|---|",
    ]
    for issuer, kind, action, success in TRANSITIONS:
        lines.append(f"| `{issuer}` | `{kind}` | {action} | {success} |")
    lines += [
        "",
        "| Pending id | Issuer | Kind | Sprint | Unit |",
        "|---:|---|---|---:|---:|",
    ]
    if pending:
        for row in pending:
            lines.append(
                f"| {row['directive_id']} | `{row['issuer_flavor']}` | "
                f"`{row['kind']}` | {row['sprint_doc_id'] or '—'} | "
                f"{row['unit_id'] or '—'} |"
            )
    else:
        lines.append("| — | — | — | — | — |")
    lines += [
        "",
        "1. Run `sc directives list --status pending`.",
        "2. For each id in ascending order run `sc directives act <id>`.",
        "3. Inspect every result; continue after executed or refused.",
        "4. Exit when the pending list is empty.",
        "",
        f"Shell: `{shell['shortname']}` · flavor: `conductor` · skill: `sprint_cond`",
    ]
    return "\n".join(lines) + "\n"
