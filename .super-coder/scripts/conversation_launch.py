#!/usr/bin/env python3
"""Prepare browser conversation turns through the canonical shell boot path."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import db_driver
import conductor_policy
import run as run_mod
import shell_liveness
from conversation_adapters import ConversationContext

_ROLE_FLAVORS = {
    "conductor": "conductor",
    "planner": "planner",
    "developer": "dev",
    "reviewer": "reviewer",
    "conformance": "reviewer",
}
_ROLE_SKILLS = {
    "conductor": "sprint_cond",
    "planner": "sprint_pln",
    "developer": "sprint_dev",
    "reviewer": "sprint_rev",
    "conformance": "sprint_rev",
}
_ROLE_ROUTES = {
    "planner": "planner_route",
    "developer": "dev_route",
    "reviewer": "reviewer_route",
    "conformance": "reviewer_route",
}


class ConversationLaunchError(RuntimeError):
    """Stable pre-dispatch refusal owned by browser conversation launching."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ConversationLaunchPreparer:
    """Turn an immutable broker route into a fully prepared adapter context.

    ``run.prepare_launch`` is intentionally reused without executing its argv:
    native conversation adapters own dispatch and streaming, while the normal
    CLI path remains the source of truth for shell identity, worktree, rendered
    boot files, harness permissions, session archives, and injected environment.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        prepare_launch: Callable[..., Any] = run_mod.prepare_launch,
        liveness: Callable[[], dict] = shell_liveness.compute,
        liveness_retries: int = 40,
        liveness_delay: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db_path = str(db_path)
        self.prepare_launch = prepare_launch
        self.liveness = liveness
        self.liveness_retries = liveness_retries
        self.liveness_delay = liveness_delay
        self.sleep = sleep

    def _shell(self, shell_id: int) -> tuple[str, str | None]:
        con = db_driver.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT shortname,flavor FROM shells WHERE shell_id=? "
                "AND COALESCE(is_deleted,0)=0",
                (shell_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise ConversationLaunchError(
                "SHELL_NOT_LAUNCHABLE",
                f"shell_id {shell_id} is unknown or deleted",
            )
        return str(row["shortname"]), row["flavor"]

    def _sprint_context(
        self,
        broker_run,
        *,
        shortname: str,
        flavor: str | None,
    ) -> dict[str, Any] | None:
        """Resolve one immutable Sprint binding before canonical preparation."""
        con = db_driver.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT c.mode,c.sprint_doc_id AS conversation_sprint_doc_id,"
                "b.binding_id,b.sprint_doc_id,b.role,b.lifecycle,b.slot,"
                "b.unit_id,b.source_directive_id,b.source_message_id,"
                "b.required_result_kind,b.state AS binding_state,"
                "(SELECT cb.slot FROM sprint_conversation_bindings cb "
                " WHERE cb.sprint_doc_id=b.sprint_doc_id "
                " AND cb.role='conductor' AND cb.state<>'terminal' "
                " ORDER BY cb.binding_id DESC LIMIT 1) AS conductor_slot,"
                "sp.state AS sprint_state,sp.spec_doc_id,sp.planner_shell_id,"
                "sp.planner_route,"
                "sp.dev_route,sp.reviewer_route,"
                "cancel.state AS cancellation_state,"
                "cancel.planner_conversation_id AS cancellation_planner_id,"
                "sprint_doc.title AS sprint_title,"
                "spec_doc.title AS spec_title,"
                "u.seq AS unit_seq,u.unit_title,u.state AS unit_state,"
                "u.dev_shell_id,u.reviewer_shell_id,"
                "u.depends_on,u.overlap,u.branch,u.pr_number,"
                "sk.name AS skill_name,sk.content AS skill_body "
                "FROM conversations c "
                "LEFT JOIN sprint_conversation_bindings b "
                " ON b.conversation_id=c.conversation_id "
                "LEFT JOIN sprints sp ON sp.sprint_doc_id=b.sprint_doc_id "
                "LEFT JOIN sprint_cancellations cancel "
                " ON cancel.sprint_doc_id=b.sprint_doc_id "
                "LEFT JOIN documents sprint_doc "
                " ON sprint_doc.document_id=b.sprint_doc_id "
                "LEFT JOIN documents spec_doc "
                " ON spec_doc.document_id=sp.spec_doc_id "
                "LEFT JOIN sprint_units u ON u.unit_id=b.unit_id "
                "LEFT JOIN skills sk ON sk.name=CASE b.role "
                " WHEN 'conductor' THEN 'sprint_cond' "
                " WHEN 'planner' THEN 'sprint_pln' "
                " WHEN 'developer' THEN 'sprint_dev' "
                " ELSE 'sprint_rev' END "
                " AND COALESCE(sk.is_deleted,0)=0 "
                "WHERE c.conversation_id=?",
                (broker_run.conversation_id,),
            ).fetchone()
        finally:
            con.close()

        if row is None:
            raise ConversationLaunchError(
                "CONVERSATION_NOT_FOUND",
                f"conversation {broker_run.conversation_id!r} no longer exists",
            )
        if row["mode"] == "normal":
            if row["binding_id"] is not None:
                raise ConversationLaunchError(
                    "SPRINT_BINDING_INVALID",
                    "a normal conversation cannot carry a Sprint binding",
                )
            return None
        if row["binding_id"] is None:
            raise ConversationLaunchError(
                "SPRINT_BINDING_REQUIRED",
                "Sprint conversation has no validated role binding",
            )

        role = str(row["role"])
        lifecycle = str(row["lifecycle"])
        expected_flavor = _ROLE_FLAVORS.get(role)
        expected_lifecycle = (
            "persistent" if role == "conductor" else "one_shot"
        )
        if (
            expected_flavor is None
            or flavor != expected_flavor
            or row["slot"] != shortname
            or row["conversation_sprint_doc_id"] != row["sprint_doc_id"]
            or lifecycle != expected_lifecycle
        ):
            raise ConversationLaunchError(
                "SPRINT_BINDING_INVALID",
                "Sprint binding does not match the conversation shell, role, "
                "lifecycle, or Sprint",
            )
        if row["binding_state"] == "terminal":
            raise ConversationLaunchError(
                "SPRINT_BINDING_TERMINAL",
                f"Sprint assignment {row['binding_id']} is already terminal",
            )
        cancelled_closeout = (
            role == "planner"
            and row["cancellation_state"] == "requested"
            and row["cancellation_planner_id"] == broker_run.conversation_id
        )
        if row["sprint_state"] not in {"active", "closing"} \
                and not cancelled_closeout:
            raise ConversationLaunchError(
                "SPRINT_NOT_LAUNCHABLE",
                f"Sprint {row['sprint_doc_id']} is "
                f"{row['sprint_state'] or 'missing'}, not active or closing",
            )
        if row["skill_name"] != _ROLE_SKILLS[role] or not row["skill_body"]:
            raise ConversationLaunchError(
                "SPRINT_ROLE_SKILL_MISSING",
                f"Sprint role skill {_ROLE_SKILLS[role]!r} is unavailable",
            )
        if (
            (role == "planner" and row["planner_shell_id"] != broker_run.shell_id)
            or (
                role == "developer"
                and row["dev_shell_id"] != broker_run.shell_id
            )
            or (
                role == "reviewer"
                and row["reviewer_shell_id"] != broker_run.shell_id
            )
        ):
            raise ConversationLaunchError(
                "SPRINT_ASSIGNMENT_MISMATCH",
                f"{role} binding is not assigned to shell {shortname!r}",
            )

        if role == "conductor":
            try:
                conductor_policy.require_harness(flavor, broker_run.harness)
            except ValueError as exc:
                raise ConversationLaunchError(
                    "SPRINT_ROUTE_MISMATCH",
                    str(exc),
                ) from exc
        else:
            route_column = _ROLE_ROUTES[role]
            try:
                expected_harness, expected_model = (
                    run_mod.sprint_lifecycle.split_route(row[route_column])
                )
            except run_mod.sprint_lifecycle.SprintLifecycleError as exc:
                raise ConversationLaunchError(
                    "SPRINT_ROUTE_INVALID",
                    str(exc),
                ) from exc
            if (
                broker_run.harness != expected_harness
                or broker_run.model != expected_model
            ):
                raise ConversationLaunchError(
                    "SPRINT_ROUTE_MISMATCH",
                    f"{role} binding requires "
                    f"{expected_harness}/{expected_model}; conversation has "
                    f"{broker_run.harness}/{broker_run.model}",
                )
            if broker_run.session_before is not None:
                raise ConversationLaunchError(
                    "SPRINT_ONE_SHOT_ALREADY_STARTED",
                    f"{role} assignment {row['binding_id']} must start a fresh "
                    "native session and cannot resume one",
                )

        unit = None
        if row["unit_id"] is not None:
            unit = {
                "unit_id": int(row["unit_id"]),
                "seq": row["unit_seq"],
                "unit_title": row["unit_title"],
                "state": row["unit_state"],
                "depends_on": row["depends_on"],
                "overlap": row["overlap"],
                "branch": row["branch"],
                "pr_number": row["pr_number"],
            }
        return {
            "binding_id": int(row["binding_id"]),
            "role": role,
            "lifecycle": lifecycle,
            "slot": row["slot"],
            "skill_name": row["skill_name"],
            "skill_body": row["skill_body"],
            "sprint_doc_id": int(row["sprint_doc_id"]),
            "sprint_title": row["sprint_title"],
            "spec_doc_id": row["spec_doc_id"],
            "spec_title": row["spec_title"],
            "source_directive_id": row["source_directive_id"],
            "source_message_id": row["source_message_id"],
            "required_result_kind": row["required_result_kind"],
            "conductor_slot": row["conductor_slot"],
            "units": [unit] if unit is not None else [],
        }

    def __call__(self, broker_run) -> tuple[ConversationContext, int]:
        shortname, flavor = self._shell(broker_run.shell_id)
        sprint_context = self._sprint_context(
            broker_run,
            shortname=shortname,
            flavor=flavor,
        )

        def occupying_state() -> str | None:
            snapshot = self.liveness()
            return (
                "busy"
                if flavor == "admin" and snapshot.get("admin_root_pids")
                else shell_liveness.session_state(shortname, snapshot)
            )

        def wait_for_free_slot() -> str | None:
            state = occupying_state()
            for _ in range(self.liveness_retries):
                if state is None:
                    return None
                self.sleep(self.liveness_delay)
                state = occupying_state()
            return state

        # Native harnesses can emit their terminal result just before their
        # process disappears from /proc. Give that browser-owned teardown a
        # short drain window; a genuinely occupied CLI slot remains a refusal.
        state = wait_for_free_slot()
        if state is not None:
            raise ConversationLaunchError(
                "SHELL_BUSY",
                f"shell {shortname!r} already has a live CLI session "
                f"({state}); close it before starting a browser turn",
            )

        try:
            launch_args = {
                "shell_id": broker_run.shell_id,
                "harness": broker_run.harness,
                "model": broker_run.model,
                "effort": broker_run.effort,
                "headless_prompt": broker_run.body,
            }
            if sprint_context is not None:
                launch_args["slot_context"] = sprint_context
            plan = self.prepare_launch(
                **launch_args,
            )
        except (run_mod.LaunchError, SystemExit) as exc:
            raise ConversationLaunchError(
                "CONVERSATION_LAUNCH_REFUSED",
                str(exc),
            ) from exc

        # Preparation renders and may create/sync the worktree. Recheck at the
        # dispatch edge so a CLI launch that raced that work is still refused
        # before the adapter can send the prompt.
        state = wait_for_free_slot()
        if state is not None:
            raise ConversationLaunchError(
                "SHELL_BUSY",
                f"shell {shortname!r} acquired a live CLI session during "
                f"browser preparation ({state}); no prompt was dispatched",
            )

        expected = broker_run.worktree.resolve()
        actual = Path(plan.cwd).resolve()
        if actual != expected:
            raise ConversationLaunchError(
                "HARNESS_WORKTREE_MISMATCH",
                f"conversation is bound to {expected}, launch prepared {actual}",
            )
        if (
            plan.harness != broker_run.harness
            or plan.model != broker_run.model
            or plan.effort != broker_run.effort
        ):
            raise ConversationLaunchError(
                "HARNESS_ROUTE_MISMATCH",
                "canonical launch preparation changed the conversation's "
                "immutable harness, model, or effort",
            )

        return (
            ConversationContext(
                worktree=actual,
                provider=broker_run.provider,
                model=plan.model,
                effort=plan.effort,
                permission_mode="unrestricted",
                title=broker_run.title,
                env=plan.env,
            ),
            int(plan.archive_id),
        )
