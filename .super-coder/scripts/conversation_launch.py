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

    def _conversation_surface(self, conversation_id: str) -> str:
        con = db_driver.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT EXISTS(SELECT 1 FROM sprint_participant_conversations "
                "WHERE conversation_id=?) AS is_sprint",
                (conversation_id,),
            ).fetchone()
        finally:
            con.close()
        return "sprint" if row is not None and row["is_sprint"] else "browser"

    @staticmethod
    def _execution_view(
        flavor: str | None,
    ) -> run_mod.execution_view.ExecutionView:
        """Rebuild and prove policy from canonical installation identity."""
        try:
            view = run_mod.execution_view.build(
                engine=run_mod.ENGINE,
                repo_root=run_mod.REPO_ROOT,
                flavor=flavor,
                source_mode=run_mod.install.is_source_repo(),
            )
            view.preflight()
            return view
        except run_mod.execution_view.ExecutionViewError as exc:
            raise ConversationLaunchError(
                "CONVERSATION_LAUNCH_REFUSED",
                str(exc),
            ) from exc

    def recovery(self, broker_run) -> ConversationContext:
        """Rebuild canonical identity and execution policy for crash recovery."""
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
        view = self._execution_view(row["flavor"])
        base_env = {
            **run_mod.os.environ,
            "SC_API_TOKEN": str(row["api_key"]),
            "SC_API_BASE": f"http://127.0.0.1:{api_port}",
            "SC_SHELL_ID": str(row["shell_id"]),
            "SC_SHELL_SHORTNAME": str(row["shortname"]),
            "SC_SHELL_WORKTREE": str(worktree),
            "SC_HARNESS": broker_run.harness,
            "SC_CONVERSATION_SURFACE": self._conversation_surface(
                broker_run.conversation_id
            ),
        }
        if row["flavor"] == "admin":
            base_env["SC_ENGINE_DIR"] = str(run_mod.ENGINE)
            base_env["SC_ROOT"] = str(run_mod.REPO_ROOT)
        env = view.environment(base_env)
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
            lifecycle_epoch=broker_run.lifecycle_epoch,
            execution_prefix=view.prefix,
        )

    def __call__(self, broker_run) -> tuple[ConversationContext, int]:
        shortname, flavor = self._shell(broker_run.shell_id)
        binding = None
        binding_digest = None
        if broker_run.route_contract_version in {
            route_transport.route_bindings.V2_CONTRACT_VERSION,
            route_transport.route_bindings.LIVE_NATIVE_CONTRACT_VERSION,
        }:
            binding = broker_run.route_binding
            binding_digest = broker_run.binding_digest
            try:
                route_transport.route_bindings.validate_binding(binding)
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
                    "stored versioned conversation route binding is invalid",
                ) from exc
            if (
                binding["harness"] != broker_run.harness
                or binding["requested_model"] != broker_run.model
                or binding["requested_effort"] != broker_run.effort
            ):
                raise ConversationLaunchError(
                    "CONVERSATION_ROUTE_INVALID",
                    "stored versioned binding disagrees with conversation route",
                )
        elif broker_run.route_contract_version != 1:
            raise ConversationLaunchError(
                "CONVERSATION_ROUTE_INVALID",
                "unsupported stored conversation route contract",
            )

        def occupying_state() -> tuple[str | None, dict | None]:
            """The slot verdict, plus the foreign browser session that holds it.

            A browser-owned process of THIS conversation is this chat's own
            lingering turn: the broker owns continuation and queueing, so it
            never refuses its own follow-up.  Any other holder is named.
            """
            snapshot = self.liveness()
            if flavor == "admin" and snapshot.get("admin_root_pids"):
                return "busy", None
            state = shell_liveness.session_state(shortname, snapshot)
            if state != "browser":
                return state, None
            sessions = shell_liveness.browser_sessions(shortname, snapshot)
            foreign = [
                session
                for session in sessions
                if session.get("conversation_id") != broker_run.conversation_id
            ]
            if foreign:
                return state, foreign[0]
            if not sessions:
                return None, None
            # This chat's own previous turn is still running. Two print-mode
            # processes on one native session file is the hazard, so the
            # follow-up waits for exit or Stop; it never dispatches over it.
            return "lingering", sessions[0]

        def wait_for_free_slot() -> tuple[str | None, dict | None]:
            state, session = occupying_state()
            for _ in range(self.liveness_retries):
                if state is None:
                    return None, None
                self.sleep(self.liveness_delay)
                state, session = occupying_state()
            return state, session

        def refusal(
            state: str, session: dict | None, tail: str
        ) -> ConversationLaunchError:
            if session is None:
                return ConversationLaunchError(
                    "SHELL_BUSY",
                    f"shell {shortname!r} already has a live CLI session "
                    f"({state}); {tail}",
                )
            if state == "lingering":
                return ConversationLaunchError(
                    "SHELL_LINGERING",
                    f"this chat's previous turn is still running "
                    f"(pid {session.get('pid')}); wait for it to finish or "
                    f"press Stop, then send again",
                )
            return ConversationLaunchError(
                "SHELL_BUSY",
                f"shell {shortname!r} is held by browser chat "
                f"{session.get('conversation_id')} (pid {session.get('pid')}); "
                f"{tail}",
            )

        # Native harnesses can emit their terminal result just before their
        # process disappears from /proc. Give that browser-owned teardown a
        # short drain window; a genuinely occupied CLI slot remains a refusal.
        state, session = wait_for_free_slot()
        if state is not None:
            raise refusal(
                state,
                session,
                "interrupt or close it before starting a browser turn",
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
        state, session = wait_for_free_slot()
        if state is not None:
            raise refusal(
                state,
                session,
                "it was acquired during browser preparation, so no prompt "
                "was dispatched",
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

        prepared_env = dict(plan.env)
        prepared_env["SC_CONVERSATION_SURFACE"] = self._conversation_surface(
            broker_run.conversation_id
        )
        return (
            ConversationContext(
                worktree=actual,
                provider=broker_run.provider,
                model=plan.model,
                effort=plan.effort,
                permission_mode="unrestricted",
                title=broker_run.title,
                env=prepared_env,
                route_binding=binding,
                binding_digest=binding_digest,
                conversation_id=broker_run.conversation_id,
                lifecycle_epoch=broker_run.lifecycle_epoch,
                boot_content=getattr(plan, "boot_content", None),
                execution_prefix=getattr(
                    getattr(plan, "execution_view", None), "prefix", ()
                ),
            ),
            int(plan.archive_id),
        )
