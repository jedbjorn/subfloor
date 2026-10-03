"""Finite Claude Request stop evidence over an already owned controller.

No process launch, consent, credential lookup or native task-to-PID inference.
A successful native TaskStop plus a later complete Stop inventory proves the
accepted tool outcome. It does not prove OS interruption cause or a continuous
registry. The checker independently requires composite Close/OS cleanup.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from conversation_runtime_checks import CapabilityEvidence, Diagnostic
from conversation_runtime_contract import (
    CAP_STOP_WORK,
    CAP_SUBMISSION,
    NativeReference,
    NativeSnapshot,
    RuntimeContractError,
    RuntimeEvent,
    RuntimeIdentity,
)
from conversation_runtime_controller import observed_claude_memory
from conversation_runtime_native_probes import _Scenarios


def _qualified(event: RuntimeEvent, root: str, *, work: str | None = None) -> bool:
    ref = event.reference
    return bool(ref and ref.root_id == root and ref.thread_id is None and ref.parent_thread_id is None
        and ref.item_id is None and ref.native_process_id is None and ref.os_process is None
        and (work is None or ref.work_id == work) and ref.activity_id
        and not event.partial and event.grade not in {"incompatible", "inconclusive"})


def complete_snapshot(event: RuntimeEvent, root: str, prompt: str) -> bool:
    """Consumed, whole-array Stop provenance; timestamps remain last_observed."""
    ids = event.data.get("work_ids")
    return bool(event.kind == "snapshot.observed" and _qualified(event, root)
        and event.reference and event.reference.activity_id == prompt
        and event.provenance == "claude:Stop-snapshot" and event.freshness == "last_observed"
        and event.data.get("snapshot_complete") is True
        and type(event.data.get("observed_at")) in {int, float}
        and event.data["observed_at"] == event.observed_at
        and isinstance(ids, list) and len(ids) <= 64
        and all(isinstance(item, str) and 0 < len(item) <= 255 for item in ids)
        and len(set(ids)) == len(ids))


class ClaudeScenarios(_Scenarios):
    def _retained(self, root: RuntimeIdentity, snapshot: NativeSnapshot) -> bool:
        return bool(snapshot.freshness in {"current", "last_observed"} and snapshot.primary_state == "idle"
            and snapshot.identity and snapshot.identity.root_id == root.root_id
            and snapshot.identity.process == root.process and root.process and self._alive(root.process))

    def _launched(self, request: str, activity: str, command: str) -> str | None:
        for event in self._events():
            if (event.kind == "work.observed" and self.driver.identity
                    and _qualified(event, self.driver.identity.root_id)
                    and event.reference and event.reference.activity_id == activity
                    and event.request_id in {None, request}
                    and event.provenance == "claude:Bash-PostToolUse" and event.reference
                    and event.reference.work_id and event.data.get("kind") == "terminal"
                    and event.data.get("state") == "running" and event.data.get("command") == command
                    and isinstance(event.data.get("tool_use_id"), str) and event.data["tool_use_id"]):
                return event.reference.work_id
        return None

    def _initial(self, prompt: str, target: str, sibling: str) -> RuntimeEvent | None:
        root = self.driver.identity
        if root is None or target == sibling:
            return None
        events = self._events()
        for snapshot in reversed(events):
            if (complete_snapshot(snapshot, root.root_id, prompt)
                    and target in snapshot.data["work_ids"] and sibling in snapshot.data["work_ids"]):
                rows = [e for e in events if e.kind == "work.observed" and _qualified(e, root.root_id)
                    and e.reference and e.reference.activity_id == prompt
                    and e.observed_at == snapshot.observed_at and e.provenance == snapshot.provenance
                    and e.freshness == "last_observed" and e.data.get("snapshot_complete") is True
                    and e.data.get("kind") == "terminal" and e.data.get("state") == "running"]
                if {target, sibling} <= {e.reference.work_id for e in rows if e.reference}:
                    return snapshot
        return None

    def _native_stopped(self, control: str, target: str, sibling: str, after: int) -> bool:
        root = self.driver.identity
        if root is None:
            return False
        events = self._events()[after:]
        for index, ack in enumerate(events):
            result = ack.data.get("native_result")
            if not (ack.kind == "control.acknowledged" and ack.control_id == control
                    and _qualified(ack, root.root_id, work=target) and ack.reference
                    and ack.freshness == "current" and ack.provenance == "claude:TaskStop-PostToolUse"
                    and ack.data.get("outcome") == "pending_snapshot"
                    and isinstance(ack.data.get("tool_use_id"), str) and ack.data["tool_use_id"]
                    and isinstance(result, Mapping) and dict(result) == {"task_id": target, "task_type": "local_bash"}):
                continue
            prompt = ack.reference.activity_id
            assert prompt
            for snapshot in events[index+1:]:
                if not (complete_snapshot(snapshot, root.root_id, prompt)
                        and snapshot.observed_at >= ack.observed_at
                        and target not in snapshot.data["work_ids"] and sibling in snapshot.data["work_ids"]):
                    continue
                sibling_rows = [e for e in events[index+1:] if e.kind == "work.observed"
                    and _qualified(e, root.root_id, work=sibling) and e.reference
                    and e.reference.activity_id == prompt and e.observed_at == snapshot.observed_at
                    and e.provenance == "claude:Stop-snapshot" and e.freshness == "last_observed"
                    and e.data.get("snapshot_complete") is True
                    and e.data.get("kind") == "terminal" and e.data.get("state") == "running"]
                outcome = any(e.kind == "control.outcome" and e.control_id == control
                    and _qualified(e, root.root_id, work=target) and e.reference
                    and e.reference.activity_id == prompt and e.freshness == "current"
                    and e.provenance == "claude:native-result+later-snapshot"
                    and e.data.get("outcome") == "complete" and e.data.get("native_outcome") == "native_stopped"
                    and e.data.get("snapshot_complete") is True
                    and e.data.get("snapshot_observed_at") == snapshot.observed_at
                    and e.data.get("os_verified") is False for e in events[index+1:])
                if sibling_rows and outcome:
                    return True
        return False

    def _sibling_reconciled(self, target: str, sibling: str, after: int) -> bool:
        # Completion is attributed by the existing native task notification;
        # absence or an OS exit alone never manufactures a terminal outcome.
        root = self.driver.identity
        if root is None:
            return False
        events = self._events()[after:]
        for index, terminal in enumerate(events):
            if not (terminal.kind == "work.terminal" and _qualified(terminal, root.root_id, work=sibling)
                    and terminal.freshness == "current" and terminal.provenance == "claude:task-notification"
                    and terminal.data.get("kind") == "terminal"
                    and terminal.data.get("state") in {"completed", "failed", "stopped"}):
                continue
            isolated = any(e.reference and e.reference.activity_id
                and complete_snapshot(e, root.root_id, e.reference.activity_id)
                and e.observed_at <= terminal.observed_at
                and target not in e.data["work_ids"] and sibling in e.data["work_ids"]
                for e in events[:index])
            if not isolated:
                continue
            for snapshot in events[index+1:]:
                prompt = snapshot.reference.activity_id if snapshot.reference else None
                if (prompt and complete_snapshot(snapshot, root.root_id, prompt)
                        and snapshot.observed_at >= terminal.observed_at
                        and not {target, sibling} & set(snapshot.data["work_ids"])):
                    return True
        return False

    def run(self) -> Mapping[str, CapabilityEvidence]:
        if not self.caps & {CAP_SUBMISSION, CAP_STOP_WORK}:
            return {cap: CapabilityEvidence(cap, "inconclusive", diagnostics=(
                Diagnostic(cap, "inconclusive", "CLAUDE_CAPABILITY_UNVERIFIED"),)) for cap in self.caps}
        root, context = self.driver.identity, self.owned.context
        route = root.protocol.get("native_route", {}) if root else {}
        policy = root.protocol.get("memory_policy", {}) if root else {}
        _, memory = observed_claude_memory(policy, context, root)
        if (root is None or root.process is None or not self._alive(root.process)
                or not memory or not isinstance(route, Mapping) or route.get("account_type") != "claude.ai"
                or route.get("model") != context.model or not isinstance(route.get("efforts"), list)
                or context.effort not in route["efforts"] or self.owned.observe_marker is None
                or CAP_SUBMISSION not in context.probe_capabilities):
            return {cap: CapabilityEvidence(cap, "inconclusive", diagnostics=(
                Diagnostic(cap, "inconclusive", "PROBE_ROUTE_MEMORY_BOOT_OR_GRANT_UNPROVED"),)) for cap in self.caps}
        work_enabled = CAP_STOP_WORK in self.caps and "stop_work_terminal" in context.probe_capabilities
        # Both commands are finite and harmless. No task ID is treated as an OS identity.
        target_command = "sleep 105 # F89_TARGET_"+self.nonce
        sibling_command = "sleep 110 # F89_SIBLING_"+self.nonce
        prompt = ("Authorized finite synthetic compatibility probe. Obey the no-memory boot. Never inspect "
            "credentials/environment or unrelated files. Call managed browser fixture_identity and "
            f"fixture_state with marker {self.nonce}. Remember nonce {self.nonce}. ")
        if work_enabled:
            prompt += (f"Use Bash run_in_background=true exactly twice, commands {target_command!r} and "
                f"{sibling_command!r}. Do not wait on them, spawn agents, create schedules or other resources. ")
        prompt += "Immediately reply READY plus the nonce. No other tools/resources."
        try:
            self._phase("first_processing")
            first, receipt = self._submit(prompt)
            self._phase("first_reply")
            self._wait(lambda: self._successful_reply(first, receipt, "READY "+self.nonce))
            if not self.owned.observe_marker(self.nonce, self.deadline):
                raise RuntimeContractError("PROBE_BOOT_FIXTURE_UNPROVED", "physical managed marker unproved")
            self._phase("nonce_recall")
            second, recalled = self._submit("Reply only with the remembered nonce from the previous message. No tools or resources.")
            self._wait(lambda: self._successful_reply(second, recalled, self.nonce))
            if not self._retained(root, self.driver.inventory(deadline=self.deadline)):
                raise RuntimeContractError("PROBE_ROOT_IDENTITY_UNPROVED", "same owned root/process required")
            if CAP_SUBMISSION in self.coverage:
                self.coverage[CAP_SUBMISSION].update({"repeated_input", "root_identity", "processing_terminal", "boot_fixture", "memory_disabled"})
            if work_enabled:
                self.stage = "stop_work_terminal"
                self._phase("initial_snapshot")
                target = self._wait(lambda: self._launched(first, receipt.native_activity_id or "", target_command))
                sibling = self._wait(lambda: self._launched(first, receipt.native_activity_id or "", sibling_command))
                self._wait(lambda: self._initial(recalled.native_activity_id or "", target, sibling))
                with self.driver._lock:
                    boundary = len(self.driver.events)
                self._phase("stop_terminal")
                control = self._control("stop_work", NativeReference(root.root_id, work_id=target))
                self._wait(lambda: self._native_stopped(control, target, sibling, boundary))
                if not self._retained(root, self.driver.inventory(deadline=self.deadline)):
                    raise RuntimeContractError("PROBE_SIBLING_ISOLATION_BROKEN", "native stop changed owned root")
                self._wait(lambda: self._sibling_reconciled(target, sibling, boundary))
                if not self._retained(root, self.driver.inventory(deadline=self.deadline)):
                    raise RuntimeContractError("PROBE_SIBLING_ISOLATION_BROKEN", "reconciliation changed owned root")
                self.coverage[CAP_STOP_WORK].update({"owned_work", "terminal_outcome", "sibling_isolation",
                    "target:terminal", "native_TaskStop", "matched_success", "later_complete_snapshot"})
        except (RuntimeError, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, RuntimeContractError) else "NATIVE_BEHAVIOR_UNPROVED"
            self.failures.append(Diagnostic(self.stage, "incompatible" if code == "PROBE_SIBLING_ISOLATION_BROKEN" else "inconclusive", code))
        result: dict[str, CapabilityEvidence] = {}
        for cap in self.caps:
            failures = tuple(d for d in (*self.driver.diagnostics, *self.failures)
                if d.capability == cap or cap == CAP_STOP_WORK and d.capability == "stop_work_terminal")
            grade: Any = "incompatible" if any(d.grade == "incompatible" for d in failures) else "compatible" if self.coverage[cap] and not failures else "inconclusive"
            if not self.coverage[cap] and not failures:
                failures = (Diagnostic(cap, "inconclusive", "CLAUDE_CAPABILITY_UNVERIFIED"),)
            result[cap] = CapabilityEvidence(cap, grade, frozenset(self.coverage[cap]),
                ("claude:owned-hooks-native-result+timestamped-Stop",), failures)
        return result
