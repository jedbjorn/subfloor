#!/usr/bin/env python3
"""Prepare browser conversation turns through the canonical shell boot path."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import db_driver
import run as run_mod
import shell_liveness
from conversation_adapters import ConversationContext


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

    def __call__(self, broker_run) -> tuple[ConversationContext, int]:
        shortname, flavor = self._shell(broker_run.shell_id)
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
            plan = self.prepare_launch(
                shell_id=broker_run.shell_id,
                harness=broker_run.harness,
                model=broker_run.model,
                effort=broker_run.effort,
                headless_prompt=broker_run.body,
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
