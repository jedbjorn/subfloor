"""Mode-scoped Sprint admission through the installed native Chats owner.

Older schema fixtures read as legacy; native intent cannot bypass a missing
owner, different database, pending probe or current checked route evidence.
This module prepares selections only, and never launches native work.
"""

from __future__ import annotations

from pathlib import Path

import route_bindings


def mode(row) -> str:
    value = row["runtime_mode"] if "runtime_mode" in tuple(row.keys()) else "ephemeral"
    if value not in {"ephemeral", "native_experiment"}:
        raise ValueError("unsupported Sprint runtime mode")
    return value


def mode_projection(con, alias: str = "p") -> str:
    # Aliases are fixed source operands, never HTTP input.
    if alias not in {"p", "participant"}:
        raise ValueError("unknown source participant alias")
    present = any(
        row["name"] == "runtime_mode"
        for row in con.execute("PRAGMA table_info(sprint_participants)")
    )
    return f"{alias}.runtime_mode" if present else "'ephemeral' AS runtime_mode"


def checked_binding(
    con, participant_id: int, harness: str, model: str | None, effort: str | None
) -> tuple[dict, str]:
    import conversation_native_chats

    service = conversation_native_chats._SERVICE

    if harness not in {"codex", "claude"} or not model or not effort:
        raise ValueError(
            "native Sprint selection requires an exact supported model and effort"
        )
    row = con.execute(
        "SELECT p.shell_id,p.runtime_mode,p.harness,p.model,p.effort,p.role,p.active_route_binding_id,"
        "sh.user_id,sh.shortname,sh.flavor,sh.is_deleted,s.conversation_generation,s.lifecycle,s.originating_planner_shell_id,"
        "planner.user_id AS planner_owner FROM sprint_participants p "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN shells planner ON planner.shell_id=s.originating_planner_shell_id "
        "WHERE p.participant_id=?",
        (participant_id,),
    ).fetchone()
    database = next(
        (
            entry["file"]
            for entry in con.execute("PRAGMA database_list")
            if entry["name"] == "main"
        ),
        "",
    )
    if (
        row is None
        or row["user_id"] != 1
        or row["planner_owner"] != 1
        or row["runtime_mode"] != "native_experiment"
        or row["flavor"]
        != {"planner": "planner", "developer": "dev", "reviewer": "reviewer"}.get(
            row["role"]
        )
        or row["is_deleted"]
        or row["lifecycle"] in {"completed", "aborted"}
        or service is None
        or not database
        or Path(database).resolve() != Path(service.database).resolve()
    ):
        raise ValueError("native Sprint selection has no matching owned Chats service")
    from conversation_runtime_contract import RuntimeContractError

    try:
        binding, digest = service.resolve_route(harness, model, effort)
    except RuntimeContractError as exc:
        raise ValueError(f"{exc.code}: {exc}") from exc
    route_bindings.validate_v2_binding(binding)
    if (
        binding["harness"] != harness
        or binding["requested_model"] != model
        or binding["requested_effort"] != effort
        or binding["effective_effort"] != effort
        or binding["selector_binding"].get("proof_state") != "checked_native_selection"
        or not route_bindings._lower_hex(
            binding["selector_binding"].get("native_fingerprint"),
            route_bindings.LOWER_HEX_64,
        )
        or not route_bindings._exact_nonblank(
            binding["selector_binding"].get("native_executable_version")
        )
        or route_bindings.digest_json(binding) != digest
    ):
        raise ValueError("native Sprint route has no exact checked selection proof")
    # Resolver observation may cross a concurrent Close/reroute/ownership
    # boundary. Re-read before returning any prepared authorization.
    current = con.execute(
        "SELECT p.shell_id,p.runtime_mode,p.harness,p.model,p.effort,p.role,p.active_route_binding_id,"
        "sh.user_id,sh.shortname,sh.flavor,sh.is_deleted,s.conversation_generation,s.lifecycle,s.originating_planner_shell_id,"
        "planner.user_id AS planner_owner FROM sprint_participants p "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN shells planner ON planner.shell_id=s.originating_planner_shell_id "
        "WHERE p.participant_id=?",
        (participant_id,),
    ).fetchone()
    if (
        current is None
        or tuple(current) != tuple(row)
        or conversation_native_chats._SERVICE is not service
        or Path(database).resolve() != Path(service.database).resolve()
    ):
        raise ValueError("native Sprint owner or selection changed during observation")
    return binding, digest
