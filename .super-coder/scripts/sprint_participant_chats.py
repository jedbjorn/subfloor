"""Sprint wake-chat creation backed by the active-chat registry.

Sprint participants keep durable conversation history, but current-chat state
belongs exclusively to ``active_shell_chats``.  Callers own transaction
boundaries; New delivery closes the previous registry chat in a committed
transaction before calling ``create_wake_conversation``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import active_chat_registry
import run as run_mod
from conversation_adapters import ADAPTER_TYPES

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "api"))
import route_bindings

_WAKE_ROLES = {
    "developer": ("Developer", "sprint_dev"),
    "reviewer": ("Reviewer", "sprint_rev"),
    "planner": ("Originating Planner", "sprint_pln"),
}


class SprintConversationError(ValueError):
    """The requested Sprint conversation transition violates its contract."""


class WakeConversationBusy(SprintConversationError):
    """A wake replacement lost the boundary race to another active chat."""


@dataclass(frozen=True)
class PreparedShellWake:
    shell_id: int
    owner_user_id: int
    shortname: str
    harness: str
    provider: str | None
    model: str | None
    effort: str | None
    worktree: str


@dataclass(frozen=True)
class PreparedSprintWake:
    participant_id: int
    sprint_id: int
    shell_id: int
    owner_user_id: int
    shortname: str
    harness: str
    provider: str | None
    model: str | None
    effort: str | None
    generation: str
    worktree: str
    route_contract_version: int
    route_revision: int | None
    control_state: str | None
    binding_digest: str | None
    binding: dict | None


@dataclass(frozen=True)
class PreparedParticipantRoute:
    participant_id: int
    shell_id: int
    role: str
    shortname: str
    harness: str
    provider: str | None
    model: str
    effort: str | None
    worktree: str


def wake_prompt(sprint_id: int, role: str) -> str:
    try:
        label, skill = _WAKE_ROLES[role]
    except KeyError as exc:
        raise SprintConversationError(
            f"unsupported Sprint participant role: {role}"
        ) from exc
    return (
        f"Sprint {sprint_id} handoff for your {label} role. Load `{skill}`. "
        f"Run `sc sprint inbox --sprint {sprint_id}` now and act on the Sprint "
        f"message(s) using `{skill}`. Confirm every Sprint write succeeds before "
        "stopping. If a Sprint command failed or did not confirm its durable write, "
        "retry that command. Do not re-check the inbox otherwise — new messages "
        "arrive as their own wakes."
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _request_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _nonblank(value: str | None, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SprintConversationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise SprintConversationError(f"{field} exceeds {maximum} characters")
    return value


def _browser_adapter(harness: str) -> dict:
    try:
        adapter = run_mod.load_adapter(harness)
    except ValueError as exc:
        raise SprintConversationError(str(exc)) from exc
    if not adapter.get("conversation"):
        raise SprintConversationError(
            f"harness '{harness}' has no browser conversation adapter"
        )
    return adapter


def _linked_to_participant(con, participant_id: int, conversation_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sprint_participant_conversations "
            "WHERE sprint_participant_id=? AND conversation_id=?",
            (participant_id, conversation_id),
        ).fetchone()
        is not None
    )


def _append_created_event(
    con,
    *,
    conversation_id: str,
    participant_id: int,
    sprint_id: int,
    wake_id: int,
) -> None:
    con.execute(
        "INSERT INTO conversation_events "
        "(conversation_id,sequence,event_type,payload) VALUES (?,1,?,?)",
        (
            conversation_id,
            "conversation.created",
            _canonical_json(
                {
                    "scope": "sprint",
                    "sprint_id": sprint_id,
                    "participant_id": participant_id,
                    "wake_id": wake_id,
                }
            ),
        ),
    )


def _append_event(
    con,
    conversation_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: int | None = None,
) -> None:
    sequence = int(
        con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO conversation_events "
        "(conversation_id,sequence,event_type,payload,run_id) VALUES (?,?,?,?,?)",
        (
            conversation_id,
            sequence,
            event_type,
            _canonical_json(payload),
            run_id,
        ),
    )


def prepare_shell_wake_conversation(con, shell_id: int) -> PreparedShellWake:
    """Resolve a non-Sprint shell route before any active chat is closed."""
    shell = con.execute(
        "SELECT shell_id,user_id,shortname,flavor FROM shells "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
        (shell_id,),
    ).fetchone()
    if shell is None:
        raise SprintConversationError("wake receiver shell does not exist")
    if shell["user_id"] is None:
        raise SprintConversationError("wake receiver shell has no browser owner")

    browser_harnesses = tuple(sorted(ADAPTER_TYPES))
    prior = con.execute(
        "SELECT harness,model,effort FROM conversations WHERE shell_id=? "
        f"AND harness IN ({','.join('?' for _ in browser_harnesses)}) "
        "ORDER BY created_at DESC,conversation_id DESC LIMIT 1",
        (shell_id, *browser_harnesses),
    ).fetchone()
    defaults = run_mod.flavor_defaults(con).get(shell["flavor"], {})
    harness = (
        (str(prior["harness"]) if prior is not None else None)
        or defaults.get("default_harness")
        or run_mod._configured_harness()
        or "claude"
    )
    selected_model = (
        str(prior["model"])
        if prior is not None and prior["model"] is not None
        else None
    )
    adapter = _browser_adapter(harness)
    selected_effort = (
        str(prior["effort"])
        if prior is not None and prior["effort"] is not None
        else None
    )
    try:
        resolved = run_mod.resolve_headless_route(
            harness=harness,
            adapter=adapter,
            flavor_model=defaults.get("models", {}).get(harness),
            model=selected_model,
            effort=selected_effort,
        )
    except ValueError as exc:
        raise SprintConversationError(str(exc)) from exc
    worktree = run_mod.shell_work_dir(shell["shortname"], shell["flavor"])
    return PreparedShellWake(
        shell_id=int(shell["shell_id"]),
        owner_user_id=int(shell["user_id"]),
        shortname=str(shell["shortname"]),
        harness=resolved.harness,
        provider=resolved.provider,
        model=resolved.model,
        effort=resolved.effort,
        worktree=str(worktree.resolve(strict=False)),
    )


def _prepare_participant_route(
    row,
    *,
    harness: str,
    model: str | None,
    effort: str | None,
) -> PreparedParticipantRoute:
    adapter = _browser_adapter(harness)
    try:
        resolved = run_mod.resolve_headless_route(
            harness=harness,
            adapter=adapter,
            flavor_model=row["flavor_model"],
            model=model,
            effort=effort,
        )
    except ValueError as exc:
        raise SprintConversationError(str(exc)) from exc
    worktree = run_mod.shell_work_dir(row["shortname"], row["flavor"])
    return PreparedParticipantRoute(
        participant_id=int(row["participant_id"]),
        shell_id=int(row["shell_id"]),
        role=str(row["role"]),
        shortname=str(row["shortname"]),
        harness=resolved.harness,
        provider=resolved.provider,
        model=resolved.model,
        effort=resolved.effort,
        worktree=str(worktree.resolve(strict=False)),
    )


def prepare_participant_route(
    con,
    *,
    sprint_id: int,
    participant_id: int,
    harness: str,
    model: str | None,
    effort: str | None,
) -> PreparedParticipantRoute:
    """Resolve one proposed participant route without mutating its Sprint."""
    row = con.execute(
        "SELECT p.participant_id,p.shell_id,p.role,p.harness,p.model,p.effort,"
        "sh.shortname,sh.flavor,fd.model AS flavor_model "
        "FROM sprint_participants p "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "LEFT JOIN flavor_defaults fd "
        "ON fd.flavor=sh.flavor AND fd.harness=? "
        "WHERE p.sprint_id=? AND p.participant_id=?",
        (harness, sprint_id, participant_id),
    ).fetchone()
    if row is None:
        raise SprintConversationError("Sprint participant does not exist")
    return _prepare_participant_route(
        row,
        harness=harness,
        model=model,
        effort=effort,
    )


def prepare_sprint_participant_routes(
    con,
    sprint_id: int,
) -> tuple[PreparedParticipantRoute, ...]:
    """Read and canonicalize one ordered snapshot of participant routes."""
    rows = con.execute(
        "SELECT p.participant_id,p.shell_id,p.role,p.harness,p.model,p.effort,"
        "sh.shortname,sh.flavor,fd.model AS flavor_model "
        "FROM sprint_participants p "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "LEFT JOIN flavor_defaults fd "
        "ON fd.flavor=sh.flavor AND fd.harness=p.harness "
        "WHERE p.sprint_id=? ORDER BY "
        "CASE p.role WHEN 'planner' THEN 0 WHEN 'developer' THEN 1 ELSE 2 END,"
        "p.participant_id",
        (sprint_id,),
    ).fetchall()
    return tuple(
        _prepare_participant_route(
            row,
            harness=str(row["harness"]),
            model=row["model"],
            effort=row["effort"],
        )
        for row in rows
    )


def create_shell_wake_conversation(
    con,
    *,
    wake_id: int,
    route: PreparedShellWake,
) -> str:
    """Create and register a prepared engine-wide wake chat."""
    if not con.in_transaction:
        raise RuntimeError("wake conversation creation requires a transaction")
    if active_chat_registry.get(con, route.shell_id) is not None:
        raise WakeConversationBusy("another chat became active before wake creation")

    key = f"shell:{route.shell_id}:wake:{wake_id}"
    request = {
        "effort": route.effort,
        "harness": route.harness,
        "model": route.model,
        "provider": route.provider,
        "shell_id": route.shell_id,
        "wake_id": wake_id,
        "worktree": route.worktree,
    }
    existing = con.execute(
        "SELECT conversation_id,shell_id,state,creation_request_hash "
        "FROM conversations WHERE owner_user_id=? "
        "AND creation_idempotency_key=?",
        (route.owner_user_id, key),
    ).fetchone()
    request_hash = _request_hash(request)
    if existing is not None:
        if (
            int(existing["shell_id"]) != route.shell_id
            or existing["creation_request_hash"] != request_hash
            or existing["state"] == "closed"
        ):
            raise SprintConversationError("idempotent wake chat is not reusable")
        conversation_id = str(existing["conversation_id"])
        active_chat_registry.register(con, route.shell_id, conversation_id)
        return conversation_id

    conversation_id = "cv_" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
        "worktree,title,creation_idempotency_key,creation_request_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            conversation_id,
            route.shell_id,
            route.owner_user_id,
            route.harness,
            route.provider,
            route.model,
            route.effort,
            route.worktree,
            f"Wake {wake_id} · {route.shortname}",
            key,
            request_hash,
        ),
    )
    _append_event(
        con,
        conversation_id,
        "conversation.created",
        {"scope": "normal", "shell_id": route.shell_id, "wake_id": wake_id},
    )
    active_chat_registry.register(con, route.shell_id, conversation_id)
    return conversation_id


def prepare_wake_conversation(
    con,
    *,
    sprint_id: int,
    participant_id: int,
) -> PreparedSprintWake:
    """Resolve a Sprint participant route before any active chat is closed."""
    binding_schema = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='sprint_participant_route_bindings'"
    ).fetchone() is not None
    binding_columns = {
        str(column["name"])
        for column in con.execute(
            "PRAGMA table_info(sprint_participant_route_bindings)"
        )
    } if binding_schema else set()
    provenance_projection = (
        "binding.source_fingerprint,binding.harness_version,"
        "binding.harness_evidence_format "
        if {"source_fingerprint", "harness_version", "harness_evidence_format"}
        <= binding_columns
        else "NULL AS source_fingerprint,NULL AS harness_version,"
        "NULL AS harness_evidence_format "
    )
    binding_projection = (
        "p.active_route_binding_id,binding.route_revision,binding.binding_json,"
        "binding.binding_digest," + provenance_projection
        if binding_schema
        else "NULL AS active_route_binding_id,NULL AS route_revision,"
        "NULL AS binding_json,NULL AS binding_digest,"
        "NULL AS source_fingerprint,NULL AS harness_version,"
        "NULL AS harness_evidence_format "
    )
    binding_join = (
        "LEFT JOIN sprint_participant_route_bindings binding "
        "ON binding.binding_id=p.active_route_binding_id "
        if binding_schema else ""
    )
    row = con.execute(
        "SELECT p.participant_id,p.sprint_id,p.shell_id,p.harness,p.model,p.effort,"
        "s.conversation_generation,sh.shortname,sh.flavor,owner.user_id,"
        "fd.model AS flavor_model," + binding_projection +
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN shells owner ON owner.shell_id=s.originating_planner_shell_id "
        "LEFT JOIN flavor_defaults fd "
        "ON fd.flavor=sh.flavor AND fd.harness=p.harness "
        + binding_join +
        "WHERE p.participant_id=? AND p.sprint_id=?",
        (participant_id, sprint_id),
    ).fetchone()
    if row is None:
        raise SprintConversationError("wake recipient is not a Sprint participant")
    if row["user_id"] is None:
        raise SprintConversationError("originating Planner has no browser owner")
    generation = _nonblank(
        row["conversation_generation"],
        "Sprint conversation generation",
        maximum=32,
    )
    binding = None
    route_revision = None
    binding_digest = None
    control_state = None
    if row["active_route_binding_id"] is None:
        harness = str(row["harness"])
        adapter = _browser_adapter(harness)
        try:
            resolved = run_mod.resolve_headless_route(
                harness=harness,
                adapter=adapter,
                flavor_model=row["flavor_model"],
                model=row["model"],
                effort=row["effort"],
            )
        except ValueError as exc:
            raise SprintConversationError(str(exc)) from exc
        provider = resolved.provider
        model = resolved.model
        effort = resolved.effort
        route_contract_version = 1
    else:
        try:
            binding = json.loads(row["binding_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SprintConversationError(
                "active Sprint route binding is invalid"
            ) from exc
        try:
            route_bindings.validate_binding(binding)
        except route_bindings.RouteResolutionError as exc:
            raise SprintConversationError(exc.message) from exc
        binding_digest = str(row["binding_digest"])
        if route_bindings.digest_json(binding) != binding_digest:
            raise SprintConversationError(
                "active Sprint route binding digest does not match its content"
            )
        harness = binding["harness"]
        _browser_adapter(harness)
        provider = run_mod.session_provider(harness, binding["requested_model"])
        model = binding["requested_model"]
        effort = binding["effective_effort"]
        route_revision = int(row["route_revision"])
        control_state = binding["control_state"]
        route_contract_version = int(binding["contract_version"])
        prior_native_turn = con.execute(
            "SELECT 1 FROM sprint_participant_conversations link "
            "JOIN conversations conversation "
            "ON conversation.conversation_id=link.conversation_id "
            "JOIN conversation_runs run "
            "ON run.conversation_id=conversation.conversation_id "
            "WHERE link.sprint_participant_id=? "
            "AND conversation.creation_idempotency_key LIKE ? "
            "AND run.harness_session_after IS NOT NULL "
            "AND run.runner_ref IS NOT NULL LIMIT 1",
            (
                row["participant_id"],
                f"generation:%:participant:{row['participant_id']}:"
                f"route:{route_revision}:wake:%",
            ),
        ).fetchone()
        if prior_native_turn is None:
            try:
                route_bindings.verify_stored_v2_before_first_turn(
                    con,
                    binding,
                    source_fingerprint=row["source_fingerprint"],
                    harness_version=row["harness_version"],
                    harness_evidence_format=(
                        row["harness_evidence_format"]
                        or route_bindings.LEGACY_HARNESS_EVIDENCE_FORMAT
                    ),
                )
            except route_bindings.RouteResolutionError as exc:
                raise SprintConversationError(
                    f"{exc.code}: {exc.message}"
                ) from exc
    worktree = run_mod.shell_work_dir(row["shortname"], row["flavor"])
    return PreparedSprintWake(
        participant_id=int(row["participant_id"]),
        sprint_id=int(row["sprint_id"]),
        shell_id=int(row["shell_id"]),
        owner_user_id=int(row["user_id"]),
        shortname=str(row["shortname"]),
        harness=harness,
        provider=provider,
        model=model,
        effort=effort,
        generation=generation,
        worktree=str(worktree.resolve(strict=False)),
        route_contract_version=route_contract_version,
        route_revision=route_revision,
        control_state=control_state,
        binding_digest=binding_digest,
        binding=binding,
    )


def create_prepared_wake_conversation(
    con,
    *,
    wake_id: int,
    route: PreparedSprintWake,
) -> str:
    """Create and register one preflighted Sprint wake chat."""
    if not con.in_transaction:
        raise RuntimeError("wake conversation creation requires a transaction")
    if active_chat_registry.get(con, route.shell_id) is not None:
        raise WakeConversationBusy("another chat became active before wake creation")

    key = (
        f"generation:{route.generation}:participant:{route.participant_id}:"
        f"route:{route.route_revision}:wake:{wake_id}"
        if route.route_contract_version in {2, 3}
        else f"generation:{route.generation}:wake:{wake_id}"
    )
    request = {
        "effort": route.effort,
        "harness": route.harness,
        "model": route.model,
        "participant_id": route.participant_id,
        "provider": route.provider,
        "sprint_id": route.sprint_id,
        "wake_id": wake_id,
        "worktree": route.worktree,
    }
    if route.route_contract_version in {2, 3}:
        request.update(
            {
                "binding_digest": route.binding_digest,
                "route_contract_version": route.route_contract_version,
                "route_revision": route.route_revision,
            }
        )
    request_hash = _request_hash(request)
    existing = con.execute(
        "SELECT conversation_id,state,creation_request_hash FROM conversations "
        "WHERE owner_user_id=? AND creation_idempotency_key=?",
        (route.owner_user_id, key),
    ).fetchone()
    if existing is not None:
        if existing["state"] == "closed":
            raise SprintConversationError("idempotent wake chat is already closed")
        if existing["creation_request_hash"] != request_hash:
            raise SprintConversationError(
                "idempotent wake chat route no longer matches its request"
            )
        conversation_id = str(existing["conversation_id"])
        if not _linked_to_participant(con, route.participant_id, conversation_id):
            raise SprintConversationError(
                "idempotent wake chat belongs to another participant"
            )
        active_chat_registry.register(con, route.shell_id, conversation_id)
        return conversation_id

    conversation_id = "cv_" + uuid.uuid4().hex
    route_columns = {
        str(row["name"])
        for row in con.execute("PRAGMA table_info(conversations)")
    }
    if {"route_contract_version", "route_binding"} <= route_columns:
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
            "worktree,title,creation_idempotency_key,creation_request_hash,"
            "conversation_scope,route_contract_version,route_binding) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'sprint',?,?)",
            (
                conversation_id,
                route.shell_id,
                route.owner_user_id,
                route.harness,
                request["provider"],
                route.model,
                route.effort,
                request["worktree"],
                f"Sprint {route.sprint_id} · Wake {wake_id} · {route.shortname}",
                key,
                request_hash,
                route.route_contract_version,
                (
                    route_bindings.canonical_json(route.binding)
                    if route.binding is not None else None
                ),
            ),
        )
    else:
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
            "worktree,title,creation_idempotency_key,creation_request_hash,"
            "conversation_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,'sprint')",
            (
                conversation_id,
                route.shell_id,
                route.owner_user_id,
                route.harness,
                request["provider"],
                route.model,
                route.effort,
                request["worktree"],
                f"Sprint {route.sprint_id} · Wake {wake_id} · {route.shortname}",
                key,
                request_hash,
            ),
        )
    _append_created_event(
        con,
        conversation_id=conversation_id,
        participant_id=route.participant_id,
        sprint_id=route.sprint_id,
        wake_id=wake_id,
    )
    con.execute(
        "INSERT INTO sprint_participant_conversations "
        "(sprint_participant_id,conversation_id) VALUES (?,?)",
        (route.participant_id, conversation_id),
    )
    active_chat_registry.register(con, route.shell_id, conversation_id)
    return conversation_id


def create_wake_conversation(
    con,
    *,
    wake_id: int,
    sprint_id: int,
    participant_id: int,
) -> str:
    """Create and register one Sprint wake chat after close commits."""
    route = prepare_wake_conversation(
        con,
        sprint_id=sprint_id,
        participant_id=participant_id,
    )
    return create_prepared_wake_conversation(con, wake_id=wake_id, route=route)


def attach_live_participations(con, shells: list[dict]) -> list[dict]:
    """Attach the live/paused Sprint projection using registry state."""
    rows = con.execute(
        "SELECT p.shell_id,p.role,p.disposition,a.chat_id AS current_conversation_id,"
        "s.sprint_id,s.lifecycle "
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "LEFT JOIN active_shell_chats a ON a.shell_id=p.shell_id "
        "WHERE s.lifecycle IN ('armed','paused') "
        "ORDER BY s.lifecycle='armed' DESC,s.paused_at DESC,s.sprint_id DESC"
    ).fetchall()
    by_shell: dict[int, dict] = {}
    for row in rows:
        shell_id = int(row["shell_id"])
        if shell_id in by_shell:
            continue
        by_shell[shell_id] = {
            "sprint_id": int(row["sprint_id"]),
            "lifecycle": row["lifecycle"],
            "role": row["role"],
            "disposition": row["disposition"],
            "current_conversation_id": row["current_conversation_id"],
        }
    for shell in shells:
        shell["sprint"] = by_shell.get(int(shell["shell_id"]))
    return shells
