#!/usr/bin/env python3
"""Prepare browser conversation turns through the canonical shell boot path."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import db_driver
import route_transport
import run as run_mod
import shell_liveness
from conversation_adapters import ConversationContext
from conversation_boot import BootDirective, BootSnapshotError

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
    CLI path remains the source of truth for shell identity, worktree,
    harness permissions, session archives, and injected environment. The boot
    document is the one exception (spec #163): the directive handed down names
    the conversation and its start/resume phase, so the conversation's single
    committed snapshot is bound or restored instead of freshly composed.
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

    def recovery(self, broker_run) -> ConversationContext:
        """Rebuild only canonical identity for crash recovery, with no launch effects."""
        con = db_driver.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT shell_id,shortname,flavor,api_key FROM shells "
                "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
                (broker_run.shell_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None or not row["api_key"]:
            raise ConversationLaunchError(
                "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
                "recovery shell no longer has canonical API identity",
            )
        worktree = run_mod.shell_work_dir(str(row["shortname"]), row["flavor"])
        if worktree.resolve() != broker_run.worktree.resolve():
            raise ConversationLaunchError(
                "HARNESS_WORKTREE_MISMATCH",
                "recovery shell no longer resolves to the conversation worktree",
            )
        api_port = run_mod.ports_mod.resolve().get("port")
        if not isinstance(api_port, int):
            raise ConversationLaunchError(
                "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
                "recovery could not resolve the shell API endpoint",
            )
        env = {
            **run_mod.os.environ,
            "SC_API_TOKEN": str(row["api_key"]),
            "SC_API_BASE": f"http://127.0.0.1:{api_port}",
            "SC_SHELL_ID": str(row["shell_id"]),
            "SC_SHELL_SHORTNAME": str(row["shortname"]),
            "SC_SHELL_WORKTREE": str(worktree),
            "SC_ENGINE_DIR": str(run_mod.ENGINE),
            "SC_HARNESS": broker_run.harness,
            "SC_ROOT": str(run_mod.REPO_ROOT),
        }
        env["PATH"] = run_mod._shell_path(worktree, env.get("PATH", ""))
        return ConversationContext(
            worktree=worktree.resolve(),
            provider=broker_run.provider,
            model=broker_run.model,
            effort=broker_run.effort,
            permission_mode="unrestricted",
            title=broker_run.title,
            env=env,
            route_binding=broker_run.route_binding,
            binding_digest=broker_run.binding_digest,
            conversation_id=broker_run.conversation_id,
        )

    def __call__(self, broker_run) -> tuple[ConversationContext, int]:
        shortname, flavor = self._shell(broker_run.shell_id)
        binding = None
        binding_digest = None
        if (
            broker_run.route_contract_version
            == route_transport.route_bindings.CONTRACT_VERSION
        ):
            binding = broker_run.route_binding
            binding_digest = broker_run.binding_digest
            try:
                route_transport.route_bindings.validate_v2_binding(binding)
                if (
                    route_transport.route_bindings.digest_json(binding)
                    != binding_digest
                ):
                    raise route_transport.route_bindings.RouteResolutionError(
                        "thinking_evidence_missing",
                        "stored route binding digest does not match",
                        {},
                    )
            except (
                TypeError,
                route_transport.route_bindings.RouteResolutionError,
            ) as exc:
                raise ConversationLaunchError(
                    "CONVERSATION_ROUTE_INVALID",
                    "stored version-two conversation route binding is invalid",
                ) from exc
            if (
                binding["harness"] != broker_run.harness
                or binding["requested_model"] != broker_run.model
                or binding["requested_effort"] != broker_run.effort
            ):
                raise ConversationLaunchError(
                    "CONVERSATION_ROUTE_INVALID",
                    "stored version-two binding disagrees with conversation route",
                )
        elif broker_run.route_contract_version != 1:
            raise ConversationLaunchError(
                "CONVERSATION_ROUTE_INVALID",
                "unsupported stored conversation route contract",
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
                "conversation_owned": True,
                "current_leased_run_id": broker_run.run_id,
                # Explicit conversation launch mode (spec #163): the broker's
                # leased run knows whether this turn starts a new native
                # session or resumes the persisted one; boot composition,
                # binding, and byte restoration key off that, never off file
                # existence.
                "boot": BootDirective(
                    conversation_id=broker_run.conversation_id,
                    phase=(
                        "resume" if broker_run.session_before else "start"
                    ),
                ),
            }
            if binding is not None:
                launch_args["route_binding"] = binding
                launch_args["binding_digest"] = binding_digest
            plan = self.prepare_launch(
                **launch_args,
            )
        except BootSnapshotError as exc:
            # Snapshot integrity failures keep their stable code so the run
            # record names the invariant that refused dispatch.
            raise ConversationLaunchError(exc.code, exc.detail) from exc
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
                route_binding=binding,
                binding_digest=binding_digest,
                conversation_id=broker_run.conversation_id,
                boot_content=getattr(plan, "boot_content", None),
            ),
            int(plan.archive_id),
        )
