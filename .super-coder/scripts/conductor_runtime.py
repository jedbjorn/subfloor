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
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import db_driver
import sprint_conversations
import sprint_lifecycle
from conductor_policy import CONDUCTOR_HARNESS, DEFAULT_CONDUCTOR_MODEL
from sprint_units import (
    SPRINT_UNIT_EDGES,
    TERMINAL_UNIT_STATES,
    SprintTransitionError,
    check_transition,
)

ENGINE = Path(__file__).resolve().parents[1]
INSTANCE_CONFIG = ENGINE / "instance.json"


@dataclass(frozen=True)
class ConductorConfig:
    enabled: bool = False
    shell: str = "CON1"
    model: str = DEFAULT_CONDUCTOR_MODEL


class ConductorConfigError(ValueError):
    """An enabled Conductor configuration that cannot safely wake."""


class DirectiveRefused(ValueError):
    """A pending directive that cannot be mechanically executed."""


class _RoutePreparationStale(RuntimeError):
    """The durable route/roster changed after external route preparation."""


def _operational_config() -> ConductorConfig:
    return ConductorConfig(True, "CON1", DEFAULT_CONDUCTOR_MODEL)


def reconcile_config(path: Path = INSTANCE_CONFIG) -> bool:
    """Persist the default Conductor block when an instance has none.

    Existing blocks are operator-owned, including an explicit opt-out.  The
    compatibility fallback in :func:`load_config` covers the first restart
    after an old updater materializes this code; fresh installs and subsequent
    updates call this function so the effective default becomes explicit.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConductorConfigError(
            f"cannot enable conductor without instance config at {path}"
        ) from exc
    except OSError as exc:
        raise ConductorConfigError(f"cannot read conductor config: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConductorConfigError(f"instance.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConductorConfigError("instance.json must contain a JSON object")
    if "conductor" in raw:
        return False
    config = _operational_config()
    raw["conductor"] = {
        "enabled": config.enabled,
        "shell": config.shell,
        "model": config.model,
    }
    path.write_text(json.dumps(raw, indent=2) + "\n")
    return True


# issuer, kind, mechanical action, success condition.  The renderer and runtime
# tests consume the same rows so the boot table cannot drift from the executor.
TRANSITIONS = (
    (
        "dev",
        "ready-for-review",
        "record PR/report-only head + move in_review; boot reviewer",
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
        "record review head; boot dev or terminalize report-only unit",
        "developer receives exact-head approval or report-only completion",
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
        "kickoff",
        "move released unit; boot target slot",
        "the declared target starts with bounded context",
    ),
    ("planner", "hold", "move active unit blocked", "board records the hold"),
    (
        "planner",
        "re-scope",
        "move non-terminal unit working; reboot target dev",
        "developer receives the new boundary",
    ),
    (
        "planner",
        "re-task",
        "move non-terminal unit working; reboot target dev",
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
        "sprint-armed",
        "release every dependency-ready unit",
        "every initially ready developer starts from committed board state",
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
    (
        "system",
        "worker-failed",
        "block the affected unit; boot originating Planner",
        "originating Planner receives typed run/result evidence",
    ),
)
_TRANSITION_SET = {(issuer, kind) for issuer, kind, _action, _pass in TRANSITIONS}

_act_lock = threading.Lock()


def load_config(path: Path = INSTANCE_CONFIG) -> ConductorConfig:
    """Read the top-level ``conductor`` block.

    Installed instances that predate the block use the operational default.
    Missing instance configuration still fails closed, while an explicit block
    remains authoritative and can disable automatic wakes.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return ConductorConfig()
    except OSError as exc:
        raise ConductorConfigError(f"cannot read conductor config: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConductorConfigError(f"instance.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConductorConfigError("instance.json must contain a JSON object")
    if "conductor" not in raw:
        return _operational_config()
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


def _prepare_assignment_routes(con, sprint_id: int) -> dict[tuple[int, str], dict]:
    """Prepare every eligible role route before the write transaction.

    Adapter loading and worktree inspection touch the filesystem, so they stay
    outside the action's short DB-only transaction.  ``_spawn`` revalidates the
    selected shell and stored route after ``BEGIN IMMEDIATE``.
    """
    import run as run_mod

    prepared: dict[tuple[int, str], dict] = {}
    shells = con.execute(
        "SELECT shell_id,shortname,flavor FROM shells "
        "WHERE flavor IN ('planner','dev','reviewer') "
        "AND COALESCE(is_deleted,0)=0 ORDER BY shell_id"
    ).fetchall()
    for shell in shells:
        harness = model = None
        slot = {
            "planner": "plan",
            "dev": "dev",
            "reviewer": "rev",
        }[shell["flavor"]]
        key = (int(shell["shell_id"]), slot)
        try:
            harness, model = _route_for_slot(con, sprint_id, slot)
            adapter = run_mod.load_adapter(harness)
            effort = run_mod.default_headless_effort(adapter)
            run_mod.validate_headless_request(adapter, model, effort)
            worktree = run_mod.shell_work_dir(
                shell["shortname"], shell["flavor"]
            ).resolve(strict=False)
            if worktree.exists() and not worktree.is_dir():
                raise ValueError(
                    f"shell {shell['shortname']!r} worktree is not a directory"
                )
            prepared[key] = {
                "shell_id": int(shell["shell_id"]),
                "shortname": shell["shortname"],
                "harness": harness,
                "provider": run_mod.session_provider(harness, model),
                "model": model,
                "effort": effort,
                "worktree": str(worktree),
                "error": None,
            }
        except (DirectiveRefused, OSError, ValueError) as exc:
            prepared[key] = {
                "shell_id": int(shell["shell_id"]),
                "shortname": shell["shortname"],
                "harness": harness,
                "model": model,
                "error": str(exc),
            }
    return prepared


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


def _assignment_role(slot: str, unit) -> str:
    if slot == "plan":
        return "planner"
    if slot == "dev":
        return "developer"
    return "reviewer" if unit is not None else "conformance"


def _required_result_kind(role: str) -> str:
    return {
        "planner": "planner-directive",
        "developer": "unit-report",
        "reviewer": "review-verdict",
        "conformance": "conformance-verdict",
    }[role]


def _spawn(
    con,
    assignments: list[dict],
    shell,
    slot: str,
    row,
    payload: dict,
    *,
    prepared_routes: dict[tuple[int, str], dict],
    unit=None,
) -> None:
    key = (int(shell["shell_id"]), slot)
    prepared = prepared_routes.get(key)
    if prepared is None or prepared["shortname"] != shell["shortname"]:
        raise _RoutePreparationStale(
            f"assignment route changed for shell {shell['shortname']}"
        )
    harness, model = _route_for_slot(con, row["sprint_doc_id"], slot)
    if (harness, model) != (prepared.get("harness"), prepared.get("model")):
        raise _RoutePreparationStale(
            f"Sprint {row['sprint_doc_id']} {slot} route changed"
        )
    if prepared["error"] is not None:
        raise DirectiveRefused(
            f"target shell {shell['shortname']!r} route is not runnable: "
            f"{prepared['error']}"
        )
    role = _assignment_role(slot, unit)
    prompt = json.dumps(
        {
            "directive_id": row["directive_id"],
            "issuer": row["issuer_flavor"],
            "kind": row["kind"],
            "payload": payload,
        },
        sort_keys=True,
    )
    creation_key = (
        f"sprint:{row['sprint_doc_id']}:assignment:{row['directive_id']}:"
        f"{role}:{shell['shortname']}"
    )
    conversation_id = sprint_conversations.create_sprint_conversation(
        con,
        sprint_doc_id=int(row["sprint_doc_id"]),
        shell=shell,
        role=role,
        lifecycle="one_shot",
        route=prepared,
        title=(
            f"Sprint #{row['sprint_doc_id']} {role} "
            f"{shell['shortname']}"
        ),
        creation_key=creation_key,
        prompt=prompt,
        unit_id=int(unit["unit_id"]) if unit is not None else None,
        source_directive_id=int(row["directive_id"]),
        required_result_kind=_required_result_kind(role),
    )
    assignments.append(
        {
            "conversation_id": conversation_id,
            "role": role,
            "slot": shell["shortname"],
            "unit_id": int(unit["unit_id"]) if unit is not None else None,
        }
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


def _release_ready_units(
    con,
    assignments,
    row,
    payload,
    actor_shell_id: int,
    prepared_routes,
):
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
        _spawn(
            con,
            assignments,
            dev,
            "dev",
            row,
            payload,
            prepared_routes=prepared_routes,
            unit=unit,
        )
        released.append(unit["seq"])
    return released


def validate_arm_board(con, sprint_id: int) -> None:
    units = con.execute(
        "SELECT seq,state,dev_shell_id,reviewer_shell_id,depends_on "
        "FROM sprint_units WHERE sprint_doc_id=? ORDER BY unit_id",
        (sprint_id,),
    ).fetchall()
    if not units:
        raise DirectiveRefused("arming requires a non-empty sprint board")
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
        raise DirectiveRefused("arming board invalid: " + "; ".join(gaps))
    remaining = {seq: set(deps) for seq, deps in dependencies.items()}
    while remaining:
        roots = {seq for seq, deps in remaining.items() if not deps}
        if not roots:
            cycle = ", ".join(sorted(remaining))
            raise DirectiveRefused(
                f"arming board has a dependency cycle among: {cycle}"
            )
        remaining = {
            seq: deps - roots
            for seq, deps in remaining.items()
            if seq not in roots
        }
    if not _ready_pending_units(con, sprint_id):
        raise DirectiveRefused(
            "arming board has no dependency-ready unit (dependency cycle)"
        )


def _execute(
    con,
    row,
    payload: dict,
    actor_shell_id: int,
    prepared_routes,
) -> list[dict]:
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
    required_state = "active"
    if sprint is None or sprint["state"] != required_state:
        raise DirectiveRefused(
            f"directive requires a {required_state} authoritative sprint"
        )
    if con.execute(
        "SELECT 1 FROM sprint_cancellations WHERE sprint_doc_id=?",
        (sprint_id,),
    ).fetchone() is not None:
        raise DirectiveRefused(
            f"sprint {sprint_id} has an operator cancellation request"
        )
    if issuer == "planner":
        planner = _planner_for_sprint(con, sprint_id)
        if row["issuer_shell_id"] != planner["shell_id"]:
            raise DirectiveRefused(
                f"planner issuer is not sprint {sprint_id}'s originating Planner"
            )
    assignments: list[dict] = []

    if issuer == "dev":
        unit = _unit(con, row)
        _assert_issuer_assignment(row, unit)
        if kind == "ready-for-review":
            report_only = payload.get("report_only") is True
            if report_only:
                if payload.get("pr_number") is not None:
                    raise DirectiveRefused(
                        "report-only ready-for-review requires null pr_number"
                    )
                if payload.get("branch") is not None:
                    raise DirectiveRefused(
                        "report-only ready-for-review requires null branch"
                    )
                if payload.get("checks") != "report-only":
                    raise DirectiveRefused(
                        "report-only ready-for-review requires "
                        "payload.checks='report-only'"
                    )
                verification = payload.get("verification")
                if not isinstance(verification, list) or not verification:
                    raise DirectiveRefused(
                        "report-only ready-for-review requires nonempty "
                        "payload.verification"
                    )
                head = _text(payload, "head")
                pr = None
                branch = None
            else:
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
            _spawn(
                con,
                assignments,
                reviewer,
                "rev",
                row,
                payload,
                prepared_routes=prepared_routes,
                unit=unit,
            )
        elif kind == "ask-planner":
            _text(payload, "question")
            _block_when_legal(con, unit, actor_shell_id)
            _spawn(
                con,
                assignments,
                _planner_for_sprint(con, sprint_id),
                "plan",
                row,
                payload,
                prepared_routes=prepared_routes,
                unit=unit,
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
            _release_ready_units(
                con,
                assignments,
                row,
                payload,
                actor_shell_id,
                prepared_routes,
            )
        elif kind == "unit-report":
            _text(payload, "shipped")
            if _all_units_terminal(con, sprint_id):
                _spawn(
                    con,
                    assignments,
                    _planner_for_sprint(con, sprint_id),
                    "plan",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                    unit=unit,
                )
        return assignments

    if issuer == "reviewer":
        unit = _unit(con, row) if row["unit_id"] is not None else None
        if unit is not None:
            _assert_issuer_assignment(row, unit)
        if kind == "ask-planner":
            _text(payload, "question")
            if unit is not None:
                _block_when_legal(con, unit, actor_shell_id)
            _spawn(
                con,
                assignments,
                _planner_for_sprint(con, sprint_id),
                "plan",
                row,
                payload,
                prepared_routes=prepared_routes,
                unit=unit,
            )
        elif kind == "review-clean":
            if unit is None:
                if payload.get("mode") != "conformance":
                    raise DirectiveRefused(
                        "unitless review-clean requires mode=conformance"
                    )
                _text(payload, "main_sha")
                _spawn(
                    con,
                    assignments,
                    _planner_for_sprint(con, sprint_id),
                    "plan",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
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
                if unit["pr_number"] is None and unit["branch"] is None:
                    _move(con, unit, "merged", actor_shell_id)
                    _release_ready_units(
                        con,
                        assignments,
                        row,
                        payload,
                        actor_shell_id,
                        prepared_routes,
                    )
                    _spawn(
                        con,
                        assignments,
                        dev,
                        "dev",
                        row,
                        {**payload, "report_only": True},
                        prepared_routes=prepared_routes,
                        unit=unit,
                    )
                else:
                    _spawn(
                        con,
                        assignments,
                        dev,
                        "dev",
                        row,
                        payload,
                        prepared_routes=prepared_routes,
                        unit=unit,
                    )
        elif kind == "findings":
            findings = payload.get("findings")
            if not isinstance(findings, list) or not findings:
                raise DirectiveRefused("payload.findings must be a nonempty array")
            if unit is None:
                _spawn(
                    con,
                    assignments,
                    _planner_for_sprint(con, sprint_id),
                    "plan",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                )
            else:
                dev = con.execute(
                    "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
                    (unit["dev_shell_id"],),
                ).fetchone()
                _spawn(
                    con,
                    assignments,
                    dev,
                    "dev",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                    unit=unit,
                )
        return assignments

    if issuer == "planner":
        sprint_id = row["sprint_doc_id"]
        if sprint_id is None:
            raise DirectiveRefused("planner directive requires sprint_doc_id")
        unit = _unit(con, row) if row["unit_id"] is not None else None
        if kind == "kickoff":
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
                _spawn(
                    con,
                    assignments,
                    target,
                    "dev",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                    unit=unit,
                )
            elif target["flavor"] == "reviewer":
                if unit is None and payload.get("mode") != "conformance":
                    raise DirectiveRefused(
                        "unitless reviewer kickoff requires mode=conformance"
                    )
                _spawn(
                    con,
                    assignments,
                    target,
                    "rev",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                    unit=unit,
                )
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
            if unit["state"] in TERMINAL_UNIT_STATES:
                raise DirectiveRefused(
                    f"{kind} requires a non-terminal unit; add a follow-up "
                    "unit for post-merge work"
                )
            if unit["state"] != "working":
                _move(con, unit, "working", actor_shell_id)
            con.execute(
                "UPDATE sprint_units SET review_head=NULL,"
                "updated_at=datetime('now'),updated_by_shell_id=? "
                "WHERE unit_id=?",
                (actor_shell_id, unit["unit_id"]),
            )
            _spawn(
                con,
                assignments,
                target,
                "dev",
                row,
                payload,
                prepared_routes=prepared_routes,
                unit=unit,
            )
        elif kind == "answer":
            if unit is None:
                raise DirectiveRefused("answer requires unit_id")
            target = _shell(con, _text(payload, "to"))
            _integer(payload, "question_directive_id")
            _text(payload, "answer")
            if target["flavor"] == "dev":
                if unit["state"] == "blocked":
                    _move(con, unit, "working", actor_shell_id)
                _spawn(
                    con,
                    assignments,
                    target,
                    "dev",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                    unit=unit,
                )
            elif target["flavor"] == "reviewer":
                if unit["state"] == "blocked":
                    _move(con, unit, "working", actor_shell_id)
                    unit = con.execute(
                        "SELECT * FROM sprint_units WHERE unit_id=?",
                        (unit["unit_id"],),
                    ).fetchone()
                    _move(con, unit, "in_review", actor_shell_id)
                _spawn(
                    con,
                    assignments,
                    target,
                    "rev",
                    row,
                    payload,
                    prepared_routes=prepared_routes,
                    unit=unit,
                )
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
            sprint_conversations.request_conductor_close(
                con,
                sprint_id,
                reason="originating Planner closed the Sprint",
            )
        return assignments

    unit = _unit(con, row) if row["unit_id"] is not None else None
    if kind == "sprint-armed":
        if unit is not None:
            raise DirectiveRefused("sprint-armed must not name a unit")
        _release_ready_units(
            con,
            assignments,
            row,
            payload,
            actor_shell_id,
            prepared_routes,
        )
    elif kind == "pr-green":
        if unit is None:
            raise DirectiveRefused("pr-green requires unit_id")
        if unit["state"] != "in_review":
            raise DirectiveRefused("pr-green requires an in_review unit")
        reviewer = con.execute(
            "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
            (unit["reviewer_shell_id"],),
        ).fetchone()
        if reviewer is None:
            raise DirectiveRefused("unit has no assigned reviewer")
        _spawn(
            con,
            assignments,
            reviewer,
            "rev",
            row,
            payload,
            prepared_routes=prepared_routes,
            unit=unit,
        )
    elif kind == "pr-red":
        if unit is None:
            raise DirectiveRefused("pr-red requires unit_id")
        if unit["state"] != "working":
            _move(con, unit, "working", actor_shell_id)
        dev = con.execute(
            "SELECT shell_id,shortname,flavor FROM shells WHERE shell_id=?",
            (unit["dev_shell_id"],),
        ).fetchone()
        _spawn(
            con,
            assignments,
            dev,
            "dev",
            row,
            payload,
            prepared_routes=prepared_routes,
            unit=unit,
        )
    elif kind == "pr-merged":
        if unit is None:
            raise DirectiveRefused("pr-merged requires unit_id")
        _move(con, unit, "merged", actor_shell_id)
        _release_ready_units(
            con,
            assignments,
            row,
            payload,
            actor_shell_id,
            prepared_routes,
        )
        if _all_units_terminal(con, sprint_id):
            _spawn(
                con,
                assignments,
                _planner_for_sprint(con, sprint_id),
                "plan",
                row,
                payload,
                prepared_routes=prepared_routes,
                unit=unit,
            )
    elif kind in ("stall", "dead-shell"):
        if unit is None:
            raise DirectiveRefused(f"{kind} requires unit_id")
        _block_when_legal(con, unit, actor_shell_id)
        _spawn(
            con,
            assignments,
            _planner_for_sprint(con, sprint_id),
            "plan",
            row,
            payload,
            prepared_routes=prepared_routes,
            unit=unit,
        )
    elif kind == "worker-failed":
        _integer(payload, "binding_id")
        _text(payload, "role")
        _text(payload, "run_outcome")
        _text(payload, "assignment_outcome")
        _text(payload, "error_code")
        if unit is not None:
            _block_when_legal(con, unit, actor_shell_id)
        _spawn(
            con,
            assignments,
            _planner_for_sprint(con, sprint_id),
            "plan",
            row,
            payload,
            prepared_routes=prepared_routes,
            unit=unit,
        )
    return assignments


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


def _source_unit(con, row):
    if row["unit_id"] is None:
        return None
    return con.execute(
        "SELECT * FROM sprint_units "
        "WHERE unit_id=? AND sprint_doc_id=?",
        (row["unit_id"], row["sprint_doc_id"]),
    ).fetchone()


def _queue_refusal_assignment(
    con,
    row,
    reason: str,
    prepared_routes,
) -> dict:
    """Atomically queue the originating Planner after a mechanical refusal."""
    if row["sprint_doc_id"] is None:
        return {"error": "cannot escalate without sprint_doc_id"}
    con.execute("SAVEPOINT conductor_refusal_assignment")
    try:
        assignments: list[dict] = []
        _spawn(
            con,
            assignments,
            _planner_for_sprint(con, row["sprint_doc_id"]),
            "plan",
            row,
            {
                "refusal": reason,
                "source_payload": row["payload"],
            },
            prepared_routes=prepared_routes,
            unit=_source_unit(con, row),
        )
        con.execute("RELEASE conductor_refusal_assignment")
        return assignments[0]
    except DirectiveRefused as exc:
        con.execute("ROLLBACK TO conductor_refusal_assignment")
        con.execute("RELEASE conductor_refusal_assignment")
        return {"error": str(exc)}


def _act_locked(
    con,
    directive_id: int,
    actor_shell_id: int,
) -> dict:
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

    for attempt in range(3):
        prepared_routes = _prepare_assignment_routes(
            con, int(row["sprint_doc_id"])
        ) if row["sprint_doc_id"] is not None else {}
        try:
            with db_driver.write_transaction(
                con,
                "conductor.directive_act",
            ):
                actor = con.execute(
                    "SELECT shell_id,flavor FROM shells "
                    "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
                    (actor_shell_id,),
                ).fetchone()
                if actor is None or actor["flavor"] != "conductor":
                    raise PermissionError(
                        "directive act requires a conductor shell"
                    )
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
                    assignments = _execute(
                        con,
                        row,
                        payload,
                        actor_shell_id,
                        prepared_routes,
                    )
                except DirectiveRefused as exc:
                    con.execute("ROLLBACK TO conductor_act")
                    con.execute("RELEASE conductor_act")
                    reason = str(exc)
                    escalation = _queue_refusal_assignment(
                        con,
                        row,
                        reason,
                        prepared_routes,
                    )
                    con.execute(
                        "UPDATE directives SET status='refused',"
                        "refusal_reason=?,executed_at=datetime('now') "
                        "WHERE directive_id=?",
                        (reason, directive_id),
                    )
                    evidence = {
                        "reason": reason,
                        "escalation": escalation,
                    }
                    _trail(
                        con,
                        row,
                        actor_shell_id,
                        "conductor-refused",
                        evidence,
                    )
                    conversation_ids = (
                        [escalation["conversation_id"]]
                        if "conversation_id" in escalation
                        else []
                    )
                    result = {
                        "directive_id": directive_id,
                        "status": "refused",
                        "reason": reason,
                        "escalation": escalation,
                        "conversation_ids": conversation_ids,
                    }
                else:
                    con.execute(
                        "UPDATE directives SET status='executed',"
                        "executed_at=datetime('now') WHERE directive_id=?",
                        (directive_id,),
                    )
                    evidence = {
                        "issuer": row["issuer_flavor"],
                        "kind": row["kind"],
                        "assignments": assignments,
                    }
                    _trail(
                        con,
                        row,
                        actor_shell_id,
                        "conductor-executed",
                        evidence,
                    )
                    con.execute("RELEASE conductor_act")
                    result = {
                        "directive_id": directive_id,
                        "status": "executed",
                        "assignments": assignments,
                        # Retained response fields for old API/CLI consumers.
                        # Browser-native actions never populate process evidence.
                        "launches": [],
                        "pids": [],
                        "conversation_ids": [
                            item["conversation_id"] for item in assignments
                        ],
                    }
            return result
        except _RoutePreparationStale:
            if attempt == 2:
                raise
            row = con.execute(
                "SELECT * FROM directives WHERE directive_id=?",
                (directive_id,),
            ).fetchone()
    raise RuntimeError("directive route preparation did not converge")


def act(
    con,
    directive_id: int,
    actor_shell_id: int,
) -> dict:
    """Commit one directive transition and every worker handoff atomically."""
    with _act_lock:
        return _act_locked(
            con,
            directive_id,
            actor_shell_id,
        )


def render_boot(con, shell, *, slot_context: dict | None = None) -> str:
    """Render the Conductor's complete transition-table boot doc."""
    sprint_id = (
        int(slot_context["sprint_doc_id"])
        if slot_context is not None
        else None
    )
    sql = (
        "SELECT directive_id,issuer_flavor,kind,sprint_doc_id,unit_id "
        "FROM directives WHERE status='pending' AND target='conductor' "
    )
    params: tuple = ()
    if sprint_id is not None:
        sql += "AND sprint_doc_id=? "
        params = (sprint_id,)
    pending = con.execute(sql + "ORDER BY directive_id", params).fetchall()
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
    ]
    if slot_context is not None:
        lines += [
            "## Browser Sprint binding",
            "",
            f"- **Sprint:** document `{sprint_id}` — "
            f"{slot_context.get('sprint_title') or '(untitled sprint)'}",
            f"- **Slot:** `{slot_context['slot']}`",
            f"- **Lifecycle:** `{slot_context['lifecycle']}`",
            f"- **Assignment:** `{slot_context['binding_id']}`",
            "",
            "Act only on pending directives for this Sprint. Other Sprints "
            "belong to their own persistent Conductor conversations.",
            "",
        ]
        if slot_context.get("spec_doc_id") is not None:
            lines.insert(
                -2,
                f"- **Governing spec:** document "
                f"`{slot_context['spec_doc_id']}` — "
                f"{slot_context.get('spec_title') or '(untitled spec)'}",
            )
    lines += [
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
    list_command = "sc directives list --status pending"
    if sprint_id is not None:
        list_command += f" --sprint {sprint_id}"
    lines += [
        "",
        f"1. Run `{list_command}`.",
        "2. For each id in ascending order run `sc directives act <id>`.",
        "3. Inspect every result; continue after executed or refused.",
        "4. Exit when this Sprint's pending list is empty.",
        "",
        f"Shell: `{shell['shortname']}` · flavor: `conductor` · skill: `sprint_cond`",
    ]
    if slot_context is not None:
        lines += [
            "",
            "## Loaded skill — sprint_cond",
            "",
            slot_context["skill_body"].strip(),
        ]
    return "\n".join(lines) + "\n"
