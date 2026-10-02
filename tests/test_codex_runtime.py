"""Recorded native shapes and real bounded JSONL pipes; no inference/login/services."""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))
from conversation_adapters.codex_runtime import CodexRuntimeDriver, JsonlRpc, RpcError
from conversation_runtime_contract import (
    CAP_AUTOMATION,
    ExecutableBinding,
    NativeControl,
    NativeReference,
    NativeSubmission,
    RuntimeContext,
)


def deadline():
    return time.monotonic() + 3


class NativeFixture:
    process = None

    def __init__(self, **kwargs):
        self.settings = kwargs
        self.message = kwargs["on_message"]
        self.before_write = kwargs["before_write"]
        self.calls = []
        self.turns = {"root": []}
        self.parents = {}
        self.terminals = {"root": []}
        self.on_submit = None
        self.closed = False
        self.fail_method = None
        self.memory_config = {"memories": {"generate_memories": False, "use_memories": False},
                              "features": {"memories": False}}
        self.account = {"type": "chatgpt", "email": "not-exported@example.invalid"}
        self.models = [{"id": "gpt-6.1-sol", "model": "gpt-6.1-sol",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]

    def frame(self, method, thread="root", turn=None, **params):
        payload = {"threadId": thread, **params}
        if turn:
            payload["turn" if isinstance(turn, dict) else "turnId"] = turn
        self.message({"method": method, "params": payload})

    def child(self, name="child", parent="root", turn="child-turn"):
        self.parents[name] = parent
        self.turns[name] = [{"id": turn, "status": "inProgress"}] if turn else []
        self.terminals[name] = []

    def terminal(self, process="opaque-1234", thread="root"):
        self.terminals[thread].append({"processId": process, "itemId": "item-" + process,
                                       "command": "finite fixture command", "cwd": str(self.settings["cwd"])})

    def request(self, method, params, *, deadline):
        self.before_write()
        self.calls.append((method, dict(params)))
        if self.fail_method == method:
            raise RpcError("NATIVE_TIMEOUT", "native acknowledgement unknown")
        if method == "initialize":
            return {"userAgent": "fixture"}
        if method == "account/read":
            return {"account": self.account}
        if method == "model/list":
            return {"data": self.models, "nextCursor": None}
        if method == "config/read":
            return {"config": self.memory_config}
        if method == "thread/start":
            return {"thread": {"id": "root", "sessionId": "different-metadata", "cwd": str(self.settings["cwd"])}}
        if method == "thread/memoryMode/set":
            return {}
        if method == "turn/start":
            turn = "turn-" + str(sum(name == method for name, _ in self.calls))
            self.turns["root"].append({"id": turn, "status": "inProgress"})
            if self.on_submit:
                self.on_submit(turn)
            return {"turn": {"id": turn, "status": "inProgress"}}
        if method == "thread/list":
            return {"data": [{"id": child, "parentThreadId": parent, "status": {"type": "active"},
                              "canAcceptDirectInput": False, "sessionId": child}
                             for child, parent in self.parents.items()], "nextCursor": None}
        if method == "thread/read":
            thread = params["threadId"]
            return {"thread": {"id": thread, "turns": [dict(turn) for turn in self.turns[thread]],
                               "status": {"type": "active" if any(turn["status"] == "inProgress"
                                                                     for turn in self.turns[thread]) else "idle"},
                               "canAcceptDirectInput": thread == "root"}}
        if method == "thread/backgroundTerminals/list":
            return {"data": list(self.terminals[params["threadId"]]), "nextCursor": None}
        if method == "turn/interrupt":
            thread, turn = params["threadId"], params["turnId"]
            for native in self.turns[thread]:
                if native["id"] == turn:
                    native["status"] = "interrupted"
            self.frame("turn/completed", thread, turn={"id": turn, "status": "interrupted"})
            return {}
        if method == "thread/backgroundTerminals/terminate":
            thread, process = params["threadId"], params["processId"]
            self.terminals[thread] = [item for item in self.terminals[thread] if item["processId"] != process]
            self.frame("item/completed", thread, item={"type": "commandExecution", "id": "item-" + process,
                                                       "processId": process, "status": "failed", "exitCode": -1,
                                                       "aggregatedOutput": "final-only output"})
            return {"terminated": True}
        if method == "thread/backgroundTerminals/clean":
            self.terminals[params["threadId"]] = []
            return {}
        raise AssertionError(method)

    def notify(self, method, params, *, deadline):
        self.before_write()
        self.calls.append((method, dict(params)))

    def fence(self, *, deadline):
        return True

    def close(self, *, deadline):
        self.closed = True
        return True


@pytest.fixture
def seat(tmp_path):
    executable = tmp_path / "native-fixture"
    executable.write_text("not executed: injected protocol fixture")
    context = RuntimeContext(
        "generation", "conversation", 1, 1, "codex", tmp_path, tmp_path,
        ExecutableBinding(executable, hashlib.sha256(executable.read_bytes()).hexdigest(), "fixture"),
        "f89-codex-app-server-v1", hashlib.sha256(b"Managed boot; no memory").hexdigest(), "policy-digest", "unrestricted",
        provider="openai", model="gpt-6.1-sol", effort="high", boot_content="Managed boot; no memory",
        env={"PATH": "/usr/bin", "SC_API_TOKEN": "synthetic-fixture-only", "OPENAI_API_KEY": "remove"},
        probe_capabilities=("submission", "stop_reply", "stop_work", "stop_work_terminal", "stop_work_child"),
    )
    events, transports = [], []

    def factory(**kwargs):
        transport = NativeFixture(**kwargs)
        transports.append(transport)
        return transport

    driver = CodexRuntimeDriver(rpc_factory=factory)
    assert driver.start(context, events.append, deadline=deadline()).state == "ready"
    yield driver, transports[0], events, context
    driver.cleanup(deadline=deadline())


def submission(name="request"):
    return NativeSubmission(name, 1, "payload-digest", "finite user input", message_id="message")


def control(action, target, expected=None):
    return NativeControl("control", 2, "payload-digest", action, target, expected)


def test_start_preserves_boot_route_effort_and_subscription_without_policy_fallback(seat):
    driver, rpc, events, context = seat
    params = next(params for method, params in rpc.calls if method == "thread/start")
    assert params["developerInstructions"] == context.boot_content
    assert params["model"] == "gpt-6.1-sol" and params["allowProviderModelFallback"] is False
    assert params["approvalPolicy"] == "never" and params["config"]["memories.use_memories"] is False
    assert "OPENAI_API_KEY" not in rpc.settings["env"]
    assert rpc.settings["env"]["SC_API_TOKEN"] == "synthetic-fixture-only"
    assert driver.submit(submission(), deadline=deadline()).state == "written"
    params = next(params for method, params in rpc.calls if method == "turn/start")
    assert params["effort"] == "high" and params["clientUserMessageId"] == "message"
    assert events[0].kind == "runtime.ready"


@pytest.mark.parametrize("change", [{"permission_mode": "interactive"}, {"provider": "paid-provider"},
                                   {"boot_content": ""}, {"model": None},
                                   {"driver_revision": "different-driver"}, {"boot_digest": "stale-digest"},
                                   {"boot_content": "different boot with stale digest"}])
def test_unproved_policy_or_route_fails_before_native_launch(seat, change):
    _, _, _, context = seat
    launches = []
    driver = CodexRuntimeDriver(rpc_factory=lambda **kw: launches.append(kw))
    assert driver.start(replace(context, **change), lambda event: None, deadline=deadline()).state == "unavailable"
    assert launches == []


def test_capability_gate_does_not_promote_schema_or_empty_evidence_to_inference(seat):
    driver, rpc, _, context = seat
    driver._context = replace(context, probe_capabilities=())
    assert driver.submit(submission(), deadline=deadline()).state == "unsupported"
    assert driver.control(control("stop_reply", NativeReference("root", "root", activity_id="a"), "a"),
                          deadline=deadline()).state == "unsupported"
    assert not any(method == "turn/start" for method, _ in rpc.calls)


def test_events_before_response_correlate_and_final_item_survives_reply_terminal(seat):
    driver, rpc, events, _ = seat

    def early(turn):
        rpc.frame("turn/started", turn={"id": turn, "status": "inProgress"})
        rpc.turns["root"][-1]["status"] = "completed"
        rpc.frame("turn/completed", turn={"id": turn, "status": "completed"})

    rpc.on_submit = early
    receipt = driver.submit(submission(), deadline=deadline())
    assert receipt.native_activity_id == "turn-1"
    started = next(event for event in events if event.kind == "activity.started")
    terminal = next(event for event in events if event.kind == "activity.terminal")
    assert started.request_id == terminal.request_id == "request"
    assert driver.inventory(deadline=deadline()).primary_state == "idle"
    rpc.frame("item/completed", turn="turn-1", item={"type": "commandExecution", "id": "late-item",
                                                   "processId": "1234", "status": "completed", "exitCode": 0,
                                                   "aggregatedOutput": "late final-only completion"})
    final = events[-1]
    assert final.kind == "output.final" and final.data["text"] == "late final-only completion"
    assert final.reference.activity_id == "turn-1" and final.request_id == "request"
    assert final.reference.native_process_id == "1234" and final.reference.os_process is None
    assert sum(method == "turn/start" for method, _ in rpc.calls) == 1


def test_acknowledgement_without_processing_event_does_not_manufacture_processed(seat):
    driver, _, events, _ = seat
    assert driver.submit(submission(), deadline=deadline()).acknowledged
    assert not any(event.kind == "activity.processed" for event in events)


@pytest.mark.parametrize("readback", [[], [{"id": "turn-1", "status": "unknown-new-state"}]])
def test_missing_or_unknown_acknowledged_turn_retains_occupancy_until_exact_terminal(seat, readback):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.turns["root"] = readback
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.primary_state == "unknown" and snapshot.partial
    assert snapshot.primary.activity_id == turn
    assert driver.submit(submission("second"), deadline=deadline()).state == "not_written"
    assert sum(method == "turn/start" for method, _ in rpc.calls) == 1
    assert not any(event.kind == "activity.terminal" for event in events)
    rpc.turns["root"] = [{"id": turn, "status": "completed"}]
    assert driver.inventory(deadline=deadline()).primary_state == "idle"
    terminal = next(event for event in events if event.kind == "activity.terminal")
    assert terminal.request_id == "request" and terminal.reference.activity_id == turn
    assert terminal.provenance == "codex:thread/read"
    driver.inventory(deadline=deadline())
    assert sum(event.kind == "activity.terminal" for event in events) == 1
    assert driver.submit(submission("second"), deadline=deadline()).state == "written"


def test_child_turn_alias_does_not_borrow_gui_request_or_source(seat):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.child(turn=turn)
    driver.inventory(deadline=deadline())
    rpc.frame("item/completed", thread="child", turn=turn,
              item={"type": "agentMessage", "id": "child-output", "text": "child output"})
    assert events[-1].reference.thread_id == "child"
    assert events[-1].request_id is None and events[-1].source == "system"


def test_inventory_and_late_native_event_emit_one_exact_terminal(seat):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.turns["root"][0]["status"] = "completed"
    driver.inventory(deadline=deadline())
    rpc.frame("turn/completed", turn={"id": turn, "status": "completed"})
    assert sum(event.kind == "activity.terminal" for event in events) == 1
    assert driver.submit(submission("second"), deadline=deadline()).state == "written"


@pytest.mark.parametrize("other_unknown", [False, True])
def test_exact_late_terminal_resolves_only_matching_readback_uncertainty(seat, other_unknown):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.turns["root"] = [{"id": "other", "status": "unknown"}] if other_unknown else []
    assert driver.inventory(deadline=deadline()).primary_state == "unknown"
    rpc.frame("turn/completed", turn={"id": turn, "status": "completed"})
    receipt = driver.submit(submission("second"), deadline=deadline())
    assert receipt.state == ("not_written" if other_unknown else "written")
    assert sum(event.kind == "activity.terminal" for event in events) == 1


@pytest.mark.parametrize("params", [{"delta": "unbound"}, {"turnId": "turn", "delta": "unbound"},
                                   {"itemId": "item", "delta": "unbound"},
                                   {"turnId": [], "itemId": "item", "delta": "unbound"}])
def test_delta_missing_required_identity_is_partial_without_losing_reader(seat, params):
    driver, rpc, events, _ = seat
    rpc.frame("item/agentMessage/delta", **params)
    assert events[-1].kind == "output.delta" and events[-1].partial
    assert events[-1].request_id is None and not driver._lost
    rpc.frame("item/completed", turn="owned-turn", item={"type": "agentMessage", "id": "final", "text": "intact"})
    assert events[-1].kind == "output.final" and not events[-1].partial


def test_late_start_cannot_reopen_terminal_primary_but_final_output_is_retained(seat):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.frame("turn/started", turn={"id": turn, "status": "inProgress"})
    rpc.turns["root"][-1]["status"] = "completed"
    rpc.frame("turn/completed", turn={"id": turn, "status": "completed"})
    before = len(events)
    rpc.frame("turn/started", turn={"id": turn, "status": "inProgress"})
    assert len(events) == before and driver._active["root"] is None
    rpc.frame("item/completed", turn=turn, item={"type": "agentMessage", "id": "late-final", "text": "late output"})
    assert events[-1].kind == "output.final" and events[-1].request_id == "request"
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.primary_state == "idle"
    assert snapshot.capabilities[CAP_AUTOMATION] == "unverified"


@pytest.mark.parametrize("event", ["started", "completed"])
def test_inventory_cannot_replace_activity_event_received_during_native_read(seat, event):
    driver, rpc, _, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    original = rpc.request

    def racing_read(method, params, *, deadline):
        result = original(method, params, deadline=deadline)
        if method == "thread/read" and params["threadId"] == "root":
            if event == "started":
                result["thread"]["turns"] = []  # older idle read races newer start
                rpc.frame("turn/started", turn={"id": turn, "status": "inProgress"})
            else:
                rpc.frame("turn/completed", turn={"id": turn, "status": "completed"})
        return result

    rpc.request = racing_read
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.partial and snapshot.primary_state == "unknown"
    assert driver._active["root"] == (turn if event == "started" else None)


def test_unknown_submission_retains_reservation_and_never_replays(seat):
    driver, rpc, _, _ = seat
    rpc.fail_method = "turn/start"
    assert driver.submit(submission(), deadline=deadline()).state == "unknown"
    rpc.fail_method = None
    assert driver.inventory(deadline=deadline()).primary_state == "unknown"
    assert driver.submit(submission("new-request"), deadline=deadline()).state == "not_written"
    assert sum(method == "turn/start" for method, _ in rpc.calls) == 1


def test_descendants_use_parent_links_not_session_id_and_disallow_direct_child_send(seat):
    driver, rpc, events, _ = seat
    rpc.child()
    rpc.child("grandchild", "child", "grandchild-turn")
    snapshot = driver.inventory(deadline=deadline())
    assert not snapshot.partial
    child = next(work for work in snapshot.work if work.reference.thread_id == "child")
    assert child.reference.root_id == "root" and child.reference.parent_thread_id == "root"
    assert child.reference.activity_id == "child-turn"
    assert child.data["can_accept_direct_input"] is False
    rpc.frame("turn/completed", thread="child", turn={"id": "child-turn", "status": "completed"})
    assert events[-1].reference.root_id == "root" and events[-1].reference.thread_id == "child"
    assert not any(method == "turn/start" and params["threadId"] == "child" for method, params in rpc.calls)


def test_child_with_foreign_parent_cannot_become_control_target(seat):
    driver, rpc, _, _ = seat
    rpc.child(parent="foreign-root")
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.partial
    count = len(rpc.calls)
    receipt = driver.control(control("stop_work", NativeReference("root", "child", work_id="child"), "child-turn"),
                             deadline=deadline())
    assert receipt.state == "rejected" and len(rpc.calls) == count


@pytest.mark.parametrize("root_active", [False, True])
@pytest.mark.parametrize("background_gap", ["unknown_child_turn", "truncated_terminals", "foreign_child"])
def test_current_root_occupancy_is_separate_from_incomplete_background_inventory(seat, root_active, background_gap):
    driver, rpc, _, _ = seat
    if root_active:
        driver.submit(submission(), deadline=deadline())
    if background_gap == "unknown_child_turn":
        rpc.child()
        rpc.turns["child"] = [{"id": "unclassified-child", "status": "newUnknownStatus"}]
    elif background_gap == "foreign_child":
        rpc.child(parent="foreign-root")
    else:
        original = driver._pages

        def truncated(method, params, *, deadline):
            rows, partial = original(method, params, deadline=deadline)
            return rows, partial or method == "thread/backgroundTerminals/list"

        driver._pages = truncated
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.partial is True
    assert snapshot.freshness == "current"
    assert snapshot.primary_state == ("active" if root_active else "idle")
    if not root_active:
        assert snapshot.primary is None


@pytest.mark.parametrize("root_gap", ["unknown_turn", "missing_active_id", "missing_ack", "wrong_read_root",
                                      "wrong_captured_root", "missing_owned_root", "global_partial", "global_loss"])
def test_root_occupancy_never_inferred_from_missing_or_uncertain_root_proof(seat, root_gap):
    driver, rpc, _, _ = seat
    if root_gap == "unknown_turn":
        rpc.turns["root"] = [{"id": "unknown-root", "status": "newUnknownStatus"}]
    elif root_gap == "missing_active_id":
        rpc.turns["root"] = [{"status": "inProgress"}]
    elif root_gap == "missing_ack":
        driver.submit(submission(), deadline=deadline())
        rpc.turns["root"] = []
    elif root_gap == "wrong_read_root":
        original = rpc.request

        def foreign(method, params, *, deadline):
            result = original(method, params, deadline=deadline)
            if method == "thread/read":
                result["thread"]["id"] = "foreign-root"
            return result

        rpc.request = foreign
    elif root_gap == "wrong_captured_root":
        driver._identity = replace(driver._identity, root_id="foreign-root")
    elif root_gap == "missing_owned_root":
        driver._parents.pop("root")
    elif root_gap == "global_partial":
        driver._partial = True
    else:
        driver._lost = True
    assert driver.inventory(deadline=deadline()).primary_state == "unknown"


def test_root_event_during_child_read_invalidates_earlier_idle_root_read(seat):
    driver, rpc, _, _ = seat
    rpc.child()
    original = rpc.request

    def racing_child(method, params, *, deadline):
        result = original(method, params, deadline=deadline)
        if method == "thread/read" and params["threadId"] == "child":
            rpc.frame("turn/started", turn={"id": "new-root-turn", "status": "inProgress"})
        return result

    rpc.request = racing_child
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.primary_state == "unknown"
    assert snapshot.primary.activity_id == "new-root-turn"


def test_root_interrupt_preserves_child_and_both_background_terminals(seat):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.child()
    rpc.terminal("root-terminal")
    rpc.terminal("child-terminal", "child")
    driver.inventory(deadline=deadline())
    receipt = driver.control(control("stop_reply", NativeReference("root", "root", activity_id=turn), turn),
                             deadline=deadline())
    assert receipt.acknowledged
    assert rpc.turns["child"][0]["status"] == "inProgress"
    assert rpc.terminals["root"] and rpc.terminals["child"]
    outcome = next(event for event in events if event.kind == "control.outcome")
    assert outcome.data["outcome"] == "complete"


def test_terminal_stop_is_exact_and_reconciles_without_os_pid_claim(seat):
    driver, rpc, events, _ = seat
    rpc.terminal("selected")
    rpc.terminal("survivor")
    receipt = driver.control(control("stop_work", NativeReference("root", "root", native_process_id="selected")),
                             deadline=deadline())
    assert receipt.acknowledged and [item["processId"] for item in rpc.terminals["root"]] == ["survivor"]
    outcome = next(event for event in events if event.kind == "control.outcome")
    assert outcome.data["outcome"] == "complete" and outcome.reference.os_process is None


def test_child_stop_interrupts_inference_and_separately_terminates_frozen_terminal(seat):
    driver, rpc, events, _ = seat
    rpc.child()
    rpc.terminal("root-survives")
    rpc.terminal("child-stopped", "child")
    driver.inventory(deadline=deadline())
    target = NativeReference("root", "child", "root", "child-turn", work_id="child")
    receipt = driver.control(control("stop_work", target, "child-turn"), deadline=deadline())
    assert receipt.acknowledged and rpc.turns["child"][0]["status"] == "interrupted"
    assert not rpc.terminals["child"] and rpc.terminals["root"]
    methods = [method for method, _ in rpc.calls]
    assert "turn/interrupt" in methods and "thread/backgroundTerminals/terminate" in methods
    assert next(event for event in events if event.kind == "control.outcome").data["outcome"] == "complete"


def test_later_turn_and_foreign_generation_are_never_interrupted(seat):
    driver, rpc, _, _ = seat
    rpc.turns["root"] = [{"id": "later", "status": "inProgress"}]
    target = NativeReference("root", "root", activity_id="earlier")
    assert driver.control(control("stop_reply", target, "earlier"), deadline=deadline()).state == "rejected"
    assert driver.control(control("stop_reply", replace(target, root_id="foreign"), "earlier"),
                          deadline=deadline()).state == "rejected"
    assert not any(method == "turn/interrupt" for method, _ in rpc.calls)


def test_unknown_turn_state_is_partial_not_idle_and_reasoning_is_not_emitted(seat):
    driver, rpc, events, _ = seat
    rpc.turns["root"] = [{"id": "turn", "status": "new-state"}]
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.partial and snapshot.primary_state == "unknown"
    count = len(events)
    rpc.frame("item/completed", turn="turn", item={"type": "reasoning", "id": "private", "text": "never export"})
    assert len(events) == count


def test_missing_required_turn_identity_loses_runtime_without_false_terminal(seat):
    driver, rpc, events, _ = seat
    rpc.frame("turn/completed", turn={"status": "completed"})
    assert events[-1].kind == "runtime.lost"
    assert driver.submit(submission(), deadline=deadline()).state == "rejected"
    assert not any(event.kind == "activity.terminal" for event in events)


def test_cleanup_fences_new_submissions_and_reports_native_separately_from_unit(seat):
    driver, rpc, _, _ = seat
    driver.submit(submission(), deadline=deadline())
    rpc.terminal()
    cleanup = driver.cleanup(deadline=deadline())
    assert cleanup.outcome == "complete" and rpc.closed
    assert "unit/cgroup" in cleanup.detail
    count = len(rpc.calls)
    assert driver.submit(submission("later"), deadline=deadline()).state == "rejected"
    assert len(rpc.calls) == count


def test_scheduling_remains_unavailable(seat):
    driver, _, _, _ = seat
    assert driver.control(control("stop_automation", NativeReference("root")), deadline=deadline()).state == "unsupported"


@pytest.mark.parametrize("target_field", ["item_id", "work_id"])
def test_terminal_explicit_identity_mismatch_cannot_stop_current_handle(seat, target_field):
    driver, rpc, _, _ = seat
    rpc.terminal("selected")
    target = replace(NativeReference("root", "root", native_process_id="selected"),
                     **{target_field: "earlier-different-item"})
    assert driver.control(control("stop_work", target), deadline=deadline()).state == "rejected"
    assert rpc.terminals["root"] and not any(method == "thread/backgroundTerminals/terminate"
                                            for method, _ in rpc.calls)


def test_stale_interrupt_unknown_does_not_retry_or_clean_background_work(seat):
    driver, rpc, events, _ = seat
    turn = driver.submit(submission(), deadline=deadline()).native_activity_id
    rpc.terminal("survivor")
    rpc.fail_method = "turn/interrupt"
    receipt = driver.control(control("stop_reply", NativeReference("root", "root", activity_id=turn), turn),
                             deadline=deadline())
    assert receipt.state == "unknown" and rpc.terminals["root"]
    assert sum(method == "turn/interrupt" for method, _ in rpc.calls) == 1
    assert next(event for event in events if event.kind == "control.outcome").data["outcome"] == "inconclusive"
    rpc.fail_method = None
    assert driver.inventory(deadline=deadline()).primary_state == "active"


def test_child_pagination_bound_is_partial_but_does_not_replace_root_idle_proof(seat):
    driver, rpc, _, _ = seat
    real_request = rpc.request

    def pages(method, params, *, deadline):
        if method == "thread/list":
            rpc.calls.append((method, dict(params)))
            return {"data": [], "nextCursor": "page-" + str(len(rpc.calls))}
        return real_request(method, params, deadline=deadline)

    rpc.request = pages
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.partial and snapshot.primary_state == "idle"
    assert sum(method == "thread/list" for method, _ in rpc.calls) == 8


def test_root_idle_with_incomplete_background_still_refuses_target_stop(seat):
    driver, rpc, _, _ = seat
    rpc.child(parent="foreign-root")
    rpc.terminal("target")
    target = NativeReference("root", "root", item_id="item-target", work_id="target", native_process_id="target")
    snapshot = driver.inventory(deadline=deadline())
    assert snapshot.primary_state == "idle" and snapshot.partial
    assert driver.control(control("stop_work", target), deadline=deadline()).state == "rejected"
    assert not any(method == "thread/backgroundTerminals/terminate" for method, _ in rpc.calls)


@pytest.mark.parametrize('native_kind,output_kind',[('agentMessage','assistant'),('commandExecution','terminal')])
def test_additive_fields_and_large_final_output_preserve_owned_text(seat,native_kind,output_kind):
    _, rpc, events, _ = seat
    text = "unicode λ " * 5000
    rpc.frame("item/completed", turn="prior-turn", extra="unused", item={
        "type": native_kind, "id": "final", "text": text, "aggregatedOutput": text,
        "status": "completed", "new_optional_field": {"ok": True}})
    assert "".join(event.data["text"] for event in events if event.kind == "output.final") == text
    assert events[-1].data["complete"] is True
    assert all(e.data['kind']==output_kind for e in events if e.kind=='output.final')
    rpc.frame("item/completed", turn="prior-turn", item={"type": "newWorkType", "id": "new-work"})
    assert events[-1].kind == "work.observed" and events[-1].partial
    assert events[-1].data["state"] == "unknown"


@pytest.mark.parametrize('method,output_kind',[
    ('item/agentMessage/delta','assistant'),('item/commandExecution/outputDelta','terminal')])
def test_delta_output_kind_preserves_exact_turn_item_text_and_offsets(seat,method,output_kind):
    _,rpc,events,_=seat
    text='visible λ '*1000
    rpc.frame(method,turnId='prior-turn',itemId='owned-item',delta=text)
    output=[e for e in events if e.kind=='output.delta']
    assert ''.join(e.data['text'] for e in output)==text
    assert [e.data['offset'] for e in output]==list(range(0,len(text),4096))
    assert all(e.data['kind']==output_kind and e.reference.activity_id=='prior-turn'
               and e.reference.item_id=='owned-item' and not e.partial for e in output)
    assert output[-1].data['complete'] is True


def test_close_does_not_wait_for_inflight_submission_rpc_acknowledgement(seat):
    driver, rpc, _, _ = seat
    written, return_reply = threading.Event(), threading.Event()
    receipts = []

    def hold_response(turn):
        written.set()
        assert return_reply.wait(timeout=2)

    rpc.on_submit = hold_response
    worker = threading.Thread(target=lambda: receipts.append(driver.submit(submission(), deadline=deadline())))
    worker.start()
    assert written.wait(timeout=1)
    try:
        assert driver.cleanup(deadline=deadline()).outcome in {"complete", "pending"}
        assert rpc.closed
        assert driver.submit(submission("later"), deadline=deadline()).state == "rejected"
    finally:
        return_reply.set()
        worker.join(timeout=2)
    assert not worker.is_alive() and len(receipts) == 1
    assert sum(method == "turn/start" for method, _ in rpc.calls) == 1


@pytest.mark.parametrize("native_line", ['not-json', '[]', '{"id":true,"result":{}}'])
def test_real_rpc_bad_frame_or_eof_marks_loss_and_never_replays(tmp_path, native_line):
    script = tmp_path / "finite_bad_rpc.py"
    script.write_text("import sys\nsys.stdin.readline()\nprint(" + repr(native_line) + ",flush=True)\n")
    losses = []
    rpc = JsonlRpc(argv=[sys.executable, str(script)], cwd=tmp_path, env={"PATH": "/usr/bin"},
                   on_message=lambda frame: None, on_loss=losses.append, before_write=lambda: None)
    try:
        with pytest.raises(RpcError, match="reader failed"):
            rpc.request("one-write", {}, deadline=deadline())
        assert len(losses) == 1 and rpc._next_id == 2
    finally:
        assert rpc.close(deadline=deadline())


def test_real_rpc_timeout_does_not_stall_notifications_other_rpc_or_close(tmp_path):
    script = tmp_path / "finite_rpc.py"
    script.write_text('''import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if request['method']=='stale':
        print(json.dumps({'method':'idle-completion','params':{'marker':'visible'}}),flush=True)
    else:
        print(json.dumps({'id':request['id'],'result':{'ok':True}}),flush=True)
''')
    frames, losses = [], []
    rpc = JsonlRpc(argv=[sys.executable, str(script)], cwd=tmp_path, env={"PATH": "/usr/bin"},
                   on_message=frames.append, on_loss=losses.append, before_write=lambda: None)
    errors = []

    def stale():
        try:
            rpc.request("stale", {}, deadline=time.monotonic() + .15)
        except RpcError as error:
            errors.append(error)

    worker = threading.Thread(target=stale)
    worker.start()
    try:
        assert rpc.request("working", {}, deadline=deadline()) == {"ok": True}
        worker.join(timeout=1)
        assert len(errors) == 1 and errors[0].state == "unknown"
        assert frames[0]["method"] == "idle-completion"
        assert rpc.fence(deadline=deadline())
    finally:
        assert rpc.close(deadline=deadline())
    assert rpc.process.poll() is not None


def test_expired_write_deadline_proves_no_write_and_transport_remains_usable(tmp_path):
    script = tmp_path / "finite_echo.py"
    script.write_text("import json,sys\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':{}}),flush=True)\n")
    losses = []
    rpc = JsonlRpc(argv=[sys.executable, str(script)], cwd=tmp_path, env={"PATH": "/usr/bin"},
                   on_message=lambda frame: None, on_loss=losses.append, before_write=lambda: None)
    try:
        with pytest.raises(RpcError) as error:
            rpc.request("expired", {}, deadline=time.monotonic() - 1)
        assert error.value.state == "not_written" and not losses
        assert rpc.request("usable", {}, deadline=deadline()) == {}
    finally:
        assert rpc.close(deadline=deadline())


def test_partial_pipe_write_loses_transport_and_cannot_append_second_rpc(tmp_path):
    # An owned finite process deliberately does not read its stdin. The frame
    # exceeds pipe capacity, so only its prefix can be written before deadline.
    script = tmp_path / "finite_unread_pipe.py"
    script.write_text("import time\ntime.sleep(2)\n")
    losses = []
    rpc = JsonlRpc(argv=[sys.executable, str(script)], cwd=tmp_path, env={"PATH": "/usr/bin"},
                   on_message=lambda frame: None, on_loss=losses.append, before_write=lambda: None)
    try:
        with pytest.raises(RpcError) as partial:
            rpc.request("partial", {"text": "x" * 200000}, deadline=time.monotonic() + .1)
        assert partial.value.state == "unknown" and len(losses) == 1
        with pytest.raises(RpcError) as later:
            rpc.request("must-not-append", {}, deadline=deadline())
        assert later.value.state == "not_written"
    finally:
        assert rpc.close(deadline=deadline())
    assert rpc.process.poll() is not None


@pytest.mark.parametrize("proved_variant,target_kind", [("stop_work_terminal", "child"), ("stop_work_child", "terminal")])
def test_target_stop_requires_compatible_matching_variant_without_native_write(seat, proved_variant, target_kind):
    driver, rpc, _, context = seat
    rpc.child()
    rpc.terminal("owned-terminal")
    driver.inventory(deadline=deadline())
    driver._context = replace(context, probe_capabilities=(), capability_evidence={
        "stop_work": "compatible", proved_variant: "compatible"})
    target = (NativeReference("root", "child", "root", "child-turn", work_id="child")
              if target_kind == "child" else NativeReference("root", "root", native_process_id="owned-terminal"))
    before = len(rpc.calls)
    receipt = driver.control(control("stop_work", target, "child-turn" if target_kind == "child" else None), deadline=deadline())
    assert receipt.state == "unsupported" and len(rpc.calls) == before
    assert rpc.terminals["root"] and rpc.turns["child"][0]["status"] == "inProgress"


def test_finite_probe_grant_names_the_specific_stop_target(seat):
    driver, rpc, _, context = seat
    rpc.child()
    rpc.terminal("owned-terminal")
    driver.inventory(deadline=deadline())
    driver._context = replace(context, probe_capabilities=("stop_work", "stop_work_terminal"))
    target = NativeReference("root", "child", "root", "child-turn", work_id="child")
    assert driver.control(control("stop_work", target, "child-turn"), deadline=deadline()).state == "unsupported"
    receipt = driver.control(control("stop_work", NativeReference("root", "root", native_process_id="owned-terminal")), deadline=deadline())
    assert receipt.state == "written" and not rpc.terminals["root"]
    assert rpc.turns["child"][0]["status"] == "inProgress"


def test_managed_mcp_config_targets_app_server_subcommand_with_execution_view(seat):
    _, _, _, context = seat
    managed = ("-c", 'mcp_servers.browser.url="http://127.0.0.1:12345/mcp/fixture"')
    transports = []
    def factory(**kwargs):
        rpc = NativeFixture(**kwargs)
        transports.append(rpc)
        return rpc
    context = replace(context, managed_mcp_args=managed, execution_prefix=("prepared-execution-view", "--"))
    driver = CodexRuntimeDriver(rpc_factory=factory)
    assert driver.start(context, lambda _: None, deadline=deadline()).state == "ready"
    argv = transports[0].settings["argv"]
    assert argv[:3] == ["prepared-execution-view", "--", str(context.executable.path)]
    assert argv[3:7] == ["app-server", "--stdio", *managed]
    assert 'forced_login_method="chatgpt"' in argv and "--disable" in argv


def test_selected_route_observation_is_native_bounded_and_excludes_account_pii(seat):
    driver, rpc, _, context = seat
    assert driver._identity.protocol["native_route"] == {
        "account_type": "chatgpt", "model": context.model, "efforts": ["high"]}
    assert "email" not in str(driver._identity.protocol)
    methods = [method for method, _ in rpc.calls]
    assert methods.index("account/read") < methods.index("model/list") < methods.index("thread/start")


@pytest.mark.parametrize("models", [[], [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": []}],
                                       [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [42]}]])
def test_unobserved_selected_native_route_refuses_thread_creation(seat, models):
    _, _, _, context = seat
    transports = []
    def factory(**kwargs):
        rpc = NativeFixture(**kwargs)
        rpc.models = models
        transports.append(rpc)
        return rpc
    driver = CodexRuntimeDriver(rpc_factory=factory)
    result = driver.start(context, lambda _: None, deadline=deadline())
    assert result.state == "unavailable"
    assert result.capabilities["submission"] == "inconclusive"
    assert not any(method == "thread/start" for method, _ in transports[0].calls)
    driver.cleanup(deadline=deadline())


def test_additional_native_efforts_are_observed_without_breaking_selected_high(seat):
    _, _, _, context = seat
    def factory(**kwargs):
        rpc = NativeFixture(**kwargs)
        rpc.models[0]["supportedReasoningEfforts"].append({"reasoningEffort": "future-effort", "extra": True})
        rpc.models[0]["harmless"] = True
        return rpc
    driver = CodexRuntimeDriver(rpc_factory=factory)
    result = driver.start(context, lambda _: None, deadline=deadline())
    assert result.state == "ready"
    assert result.identity.protocol["native_route"]["efforts"] == ["future-effort", "high"]
    driver.cleanup(deadline=deadline())


def test_native_account_mismatch_is_inconclusive_before_model_or_thread(seat):
    _, _, _, context = seat
    transports = []
    def factory(**kwargs):
        rpc = NativeFixture(**kwargs)
        rpc.account = {"type": "apiKey"}
        transports.append(rpc)
        return rpc
    driver = CodexRuntimeDriver(rpc_factory=factory)
    result = driver.start(context, lambda _: None, deadline=deadline())
    assert result.state == "unavailable" and result.capabilities["submission"] == "inconclusive"
    assert not any(method in {"model/list", "thread/start"} for method, _ in transports[0].calls)
    driver.cleanup(deadline=deadline())


def test_selected_model_observation_follows_bounded_native_pagination(seat):
    _, _, _, context = seat
    class Paginated(NativeFixture):
        def request(self, method, params, *, deadline):
            if method == "model/list" and params["cursor"] is None:
                self.calls.append((method, dict(params)))
                return {"data": [{"model": "other"}], "nextCursor": "second-page"}
            return super().request(method, params, deadline=deadline)
    transport = []
    def factory(**kwargs):
        rpc = Paginated(**kwargs)
        transport.append(rpc)
        return rpc
    driver = CodexRuntimeDriver(rpc_factory=factory)
    result = driver.start(context, lambda _: None, deadline=deadline())
    assert result.state == "ready"
    assert [p["cursor"] for m, p in transport[0].calls if m == "model/list"] == [None, "second-page"]
    driver.cleanup(deadline=deadline())


def test_native_memory_policy_observed_before_readiness(seat):
    driver, rpc, _, _ = seat
    assert driver._identity.protocol["memory_policy"] == {
        "generate_memories": False, "use_memories": False, "feature_enabled": False, "root_mode": "disabled"}
    methods = [m for m, _ in rpc.calls]
    assert methods.index("config/read") < methods.index("thread/start") < methods.index("thread/memoryMode/set")


@pytest.mark.parametrize("config", [{}, {"memories": {}, "features": {"memories": False}},
                                    {"memories": {"generate_memories": 0, "use_memories": False},
                                     "features": {"memories": False}}])
def test_unobserved_memory_policy_refuses_native_thread_before_inference(seat, config):
    _, _, _, context = seat
    transports = []
    def factory(**kwargs):
        rpc = NativeFixture(**kwargs)
        rpc.memory_config = config
        transports.append(rpc)
        return rpc
    driver = CodexRuntimeDriver(rpc_factory=factory)
    result = driver.start(context, lambda _: None, deadline=deadline())
    assert result.state == "unavailable" and result.capabilities["submission"] == "inconclusive"
    assert not any(m == "thread/start" for m, _ in transports[0].calls)
    driver.cleanup(deadline=deadline())
