"""DeepSeek browser adapter lifecycle, isolation, and event contracts."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import deepseek_runtime  # noqa: E402
import route_bindings  # noqa: E402
from conversation_adapters import ADAPTER_TYPES, adapter_for  # noqa: E402
from conversation_adapters.base import (  # noqa: E402
    AdapterError,
    ConversationContext,
    NativeTurn,
    checked_version_compatibility,
)
from conversation_adapters.deepseek import DeepSeekAdapter  # noqa: E402

CREATED_ADAPTERS: list[DeepSeekAdapter] = []


@pytest.fixture(autouse=True)
def close_created_adapters():
    boundary = len(CREATED_ADAPTERS)
    yield
    for adapter in reversed(CREATED_ADAPTERS[boundary:]):
        adapter.close()
    del CREATED_ADAPTERS[boundary:]


def runtime_status(*, available: bool = True) -> deepseek_runtime.RuntimeStatus:
    return deepseek_runtime.RuntimeStatus(
        available=available,
        enabled=True,
        error=None if available else "HARNESS_RUNTIME_MISSING",
        detail=None if available else "carrier absent",
        carrier_python="/carrier/bin/python" if available else None,
        python_version="3.12.1" if available else None,
        sdk_version="0.1.0rc7" if available else None,
        runtime_version="0.1.0rc7" if available else None,
        composition_sha256="a" * 64,
    )


def deepseek_binding(*, effort: str = "default") -> tuple[dict[str, Any], str]:
    if effort == "default":
        options = {"omit": ["thinking", "reasoning_effort"], "set": {}}
        evidence = None
    else:
        options = {
            "omit": [],
            "set": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            },
        }
        evidence = "d" * 64
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "deepseek",
        "requested_model": "deepseek-v4-pro",
        "provider_model": "deepseek-v4-pro",
        "requested_effort": effort,
        "effective_effort": effort,
        "native_variant_id": None,
        "transport": "deepseek-provider-options-v1",
        "catalogue_generation": "a" * 32,
        "evidence_digest": evidence,
        "selector_binding": {
            "kind": "authenticated-provider-model",
            "selector": "deepseek-v4-pro",
        },
        "adapter_metadata": {
            "provider_route": "deepseek-official",
            "transport_contract": "deepseek-provider-options-v1",
            "wire_evidence_digest": "c" * 64,
            "provider_options": options,
        },
    }
    route_bindings.validate_v2_binding(binding)
    return binding, route_bindings.digest_json(binding)


def context(
    worktree: Path,
    *,
    conversation_id: str = "cv_" + "1" * 32,
    boot: str | None = "immutable boot bytes",
    env: Mapping[str, str] | None = None,
    effort: str = "default",
) -> ConversationContext:
    binding, digest = deepseek_binding(effort=effort)
    return ConversationContext(
        worktree=worktree,
        provider="deepseek-official",
        model="deepseek-v4-pro",
        effort=effort,
        permission_mode="unrestricted",
        env={
            "DEEPSEEK_API_KEY": "sk-test-secret-value",
            "DEEPSEEK_BASE_URL": "https://gateway.example/v1",
            **dict(env or {}),
        },
        route_binding=binding,
        binding_digest=digest,
        conversation_id=conversation_id,
        boot_content=boot,
    )


class FakeTransport:
    def __init__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        notifications: list[Mapping[str, Any]] | None = None,
        reconcile_outcome: str = "succeeded",
        pid: int = 54321,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = dict(env)
        self.process = SimpleNamespace(pid=pid)
        self.items = list(notifications or [])
        self.reconcile_outcome = reconcile_outcome
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        self.requests.append((method, dict(params)))
        session_id = params.get("sessionId")
        if method == "session/start":
            return {
                "sessionId": session_id,
                "presence": "persisted",
                "status": "idle",
                "eventCount": 5,
                "lastEventSeq": 4,
            }
        if method == "session/prompt":
            return {"messageId": "native-message-7"}
        if method == "session/cancel":
            return {
                "sessionId": session_id,
                "accepted": True,
                "status": "idle",
                "outcome": "cancelled",
            }
        if method == "session/inspect":
            return {
                "sessionId": session_id,
                "presence": "persisted",
                "status": "idle",
                "eventCount": 12,
                "lastEventSeq": 11,
            }
        if method == "session/reconcile":
            return {
                "sessionId": session_id,
                "presence": "persisted",
                "status": "idle",
                "eventCount": 14,
                "lastEventSeq": 13,
                "outcome": self.reconcile_outcome,
            }
        if method == "shutdown":
            return {}
        raise AssertionError(f"unexpected fake method {method}")

    def notifications(self):
        yield from self.items

    def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, **transport_options: Any) -> None:
        self.transport_options = transport_options
        self.instances: list[FakeTransport] = []

    def __call__(self, **kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs, **self.transport_options)
        self.instances.append(transport)
        return transport


def make_adapter(state_root: Path, factory: Factory) -> DeepSeekAdapter:
    adapter = DeepSeekAdapter(
        runtime_probe=lambda **_: runtime_status(),
        transport_factory=factory,
        state_root=state_root,
        start_ticks=lambda _pid: 77,
    )
    CREATED_ADAPTERS.append(adapter)
    return adapter


def native_notification(
    session_ref: str,
    event_type: str,
    seq: int,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "method": "native/notification",
        "params": {
            "method": "session.event",
            "payload": {
                "sessionId": session_ref,
                "event": {"type": event_type, "seq": seq, "time": seq, "data": dict(data)},
            },
        },
    }


def test_manifest_registry_probe_and_surface_contract_are_live() -> None:
    adapter = DeepSeekAdapter(runtime_probe=lambda **_: runtime_status())
    probe = adapter.probe()

    assert ADAPTER_TYPES["deepseek"] is DeepSeekAdapter
    assert isinstance(adapter_for("deepseek", runtime_probe=lambda **_: runtime_status()), DeepSeekAdapter)
    assert probe.version == probe.verified_version == "0.1.0rc7"
    assert probe.compatibility == "verified"
    assert probe.capabilities.exact_session_resume is True
    assert probe.capabilities.structured_streaming is True
    assert probe.capabilities.interruption is True
    assert probe.capabilities.session_inspection is True
    manifest = json.loads((ENGINE / "adapters" / "deepseek" / "adapter.json").read_text())
    assert [
        name for name, enabled in manifest["surfaces"].items() if enabled
    ] == ["browser"]

    next_release = checked_version_compatibility(
        harness="deepseek",
        compatibility=manifest["conversation"],
        version="0.1.0rc8",
    )
    assert next_release.compatibility == "newer-unverified"


def test_start_binds_exact_route_boot_and_isolated_process_identity(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory = Factory()
    adapter = make_adapter(tmp_path / "state", factory)
    current = context(
        worktree,
        env={"DSH_HOME": "/attacker/home", "CURRENT_GRANT": "filesystem"},
    )

    turn = adapter.start(current, "Do the work")
    transport = factory.instances[0]
    layout = deepseek_runtime.conversation_layout(
        current.conversation_id, state_root=tmp_path / "state"
    )
    identity = json.loads(layout.adapter_identity.read_text())
    process = json.loads(layout.process_identity.read_text())

    assert turn.session_ref.startswith("deepseek-")
    assert turn.process_ref == "54321"
    assert turn.metadata["from_event_seq"] == 5
    assert transport.requests == [
        ("session/start", {"sessionId": turn.session_ref}),
        ("session/prompt", {"sessionId": turn.session_ref, "message": "Do the work"}),
    ]
    assert transport.env["DSH_SYSTEM_PROMPT"] == "immutable boot bytes"
    assert transport.env["DSH_CWD"] == str(worktree)
    assert transport.env["DSH_HOME"] == str(layout.home)
    assert transport.env["CURRENT_GRANT"] == "filesystem"
    assert json.loads(transport.env["SC_DEEPSEEK_PROVIDER_OPTIONS"]) == {
        "thinking": "omit",
        "reasoningEffort": "omit",
    }
    assert identity == {
        "schema_version": 1,
        "conversation_id": current.conversation_id,
        "session_ref": turn.session_ref,
        "worktree": str(worktree),
        "binding_digest": current.binding_digest,
        "boot_sha256": hashlib_sha256("immutable boot bytes"),
        "model": "deepseek-v4-pro",
        "effort": "default",
    }
    assert process["pid"] == 54321
    assert process["start_ticks"] == 77
    assert "sk-test-secret-value" not in layout.adapter_identity.read_text()
    assert "sk-test-secret-value" not in layout.process_identity.read_text()
    assert os.stat(layout.adapter_identity).st_mode & 0o777 == 0o600
    adapter.close()
    assert transport.closed is True


def hashlib_sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def test_stream_normalizes_deduplicates_usage_tools_unknowns_and_terminal(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory = Factory()
    adapter = make_adapter(tmp_path / "state", factory)
    turn = adapter.start(context(worktree), "Do it")
    transport = factory.instances[0]
    other = "deepseek-" + "f" * 32
    transport.items = [
        native_notification(other, "turn/start", 5, {"turn": 2}),
        native_notification(turn.session_ref, "turn/start", 5, {"turn": 2}),
        native_notification(turn.session_ref, "turn/start", 5, {"turn": 2}),
        native_notification(
            turn.session_ref,
            "assistant/chunk",
            6,
            {"turn": 2, "step": 1, "chunk": {"type": "text-delta", "index": 0, "text": "answer"}},
        ),
        native_notification(
            turn.session_ref,
            "assistant/chunk",
            7,
            {"turn": 2, "step": 1, "chunk": {"type": "reasoning-delta", "index": 1, "text": "reason"}},
        ),
        native_notification(
            turn.session_ref,
            "assistant/chunk",
            8,
            {"turn": 2, "step": 1, "chunk": {"type": "usage", "usage": {"inputTokens": 8, "outputTokens": 3}}},
        ),
        native_notification(
            turn.session_ref,
            "assistant/message",
            9,
            {"turn": 2, "step": 1, "message": {"content": []}, "usage": {"inputTokens": 8, "outputTokens": 3}},
        ),
        native_notification(
            turn.session_ref,
            "tool/call",
            10,
            {"turn": 2, "step": 1, "callId": "call-1", "name": "bash", "arguments": '{"cmd":"pwd"}'},
        ),
        native_notification(
            turn.session_ref,
            "tool/result",
            11,
            {"turn": 2, "step": 1, "message": {"toolCallId": "call-1", "content": [], "isError": False}},
        ),
        native_notification(
            turn.session_ref,
            "future/event",
            12,
            {"credential": "must-not-survive", "value": "kept"},
        ),
        native_notification(
            turn.session_ref,
            "turn/end",
            13,
            {"turn": 2, "reason": {"kind": "completed"}},
        ),
        native_notification(turn.session_ref, "turn/end", 14, {"turn": 2, "reason": {"kind": "error"}}),
    ]

    events = list(adapter.stream(turn))

    assert [event.type for event in events] == [
        "session.started",
        "run.started",
        "assistant.delta",
        "assistant.delta",
        "usage",
        "tool.started",
        "tool.completed",
        "run.completed",
    ]
    assert events[2].payload["text"] == "answer"
    assert events[2].payload["segment"] == "answer"
    assert events[3].payload["text"] == "reason"
    assert events[3].payload["segment"] == "reasoning"
    assert events[4].payload["tokens"] == {"input_tokens": 8, "output_tokens": 3}
    assert events[5].payload["tool_ref"] == events[6].payload["tool_ref"] == "call-1"
    assert events[-1].payload["status"] == "completed"
    assert events[-1].payload["unknown_native_events"] == [
        {
            "type": "future/event",
            "seq": 12,
            "time": 12,
            "data": {"credential": "[REDACTED]", "value": "kept"},
        }
    ]
    assert "must-not-survive" not in repr(events)
    adapter.close()


def test_approval_event_fails_only_the_owned_run_and_releases_no_other_transport(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory_a = Factory()
    factory_b = Factory(pid=54322)
    adapter_a = make_adapter(tmp_path / "state", factory_a)
    adapter_b = make_adapter(tmp_path / "state", factory_b)
    turn_a = adapter_a.start(context(worktree, conversation_id="cv_" + "a" * 32), "A")
    turn_b = adapter_b.start(context(worktree, conversation_id="cv_" + "b" * 32), "B")
    factory_a.instances[0].items = [
        native_notification(
            turn_a.session_ref,
            "approval/asked",
            5,
            {"id": "approval-1", "toolName": "bash", "reason": "escalate"},
        )
    ]

    events = list(adapter_a.stream(turn_a))

    assert [event.type for event in events] == [
        "session.started",
        "permission.requested",
        "run.failed",
    ]
    assert events[-1].payload["error"] == "HARNESS_APPROVAL_UNSUPPORTED"
    assert factory_a.instances[0].requests[-1] == (
        "session/cancel",
        {"sessionId": turn_a.session_ref},
    )
    assert all(method != "session/cancel" for method, _ in factory_b.instances[0].requests)
    assert turn_b.session_ref != turn_a.session_ref
    adapter_a.close()
    adapter_b.close()


def test_same_conversation_cannot_spawn_two_carriers(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state = tmp_path / "state"
    first_factory = Factory()
    second_factory = Factory(pid=54322)
    first = make_adapter(state, first_factory)
    second = make_adapter(state, second_factory)
    current = context(worktree)
    first.start(current, "first")
    # Exercise the lock itself, including the native-start crash window before
    # adapter identity publication.
    deepseek_runtime.conversation_layout(
        current.conversation_id, state_root=state
    ).adapter_identity.unlink()

    with pytest.raises(AdapterError) as refused:
        second.start(current, "second")

    assert refused.value.code == "HARNESS_PROCESS_ALREADY_RUNNING"
    assert second_factory.instances == []
    first.close()


def test_exact_resume_refreshes_turn_environment_without_changing_boot_or_identity(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    skill = worktree / ".agents" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("version one")
    state = tmp_path / "state"
    first_factory = Factory()
    first = make_adapter(state, first_factory)
    initial = context(worktree, env={"GRANTED_TOOL": "old", "TURN_MARKER": "one"})
    first_turn = first.start(initial, "first")
    layout = deepseek_runtime.conversation_layout(initial.conversation_id, state_root=state)
    stored_before = json.loads(layout.adapter_identity.read_text())
    first.close()
    layout.process_identity.unlink()

    skill.write_text("version two")
    second_factory = Factory(pid=54322)
    second = make_adapter(state, second_factory)
    refreshed = context(worktree, env={"TURN_MARKER": "two"})
    resumed = second.resume(first_turn.session_ref, refreshed, "second")
    stored_after = json.loads(layout.adapter_identity.read_text())

    assert resumed.session_ref == first_turn.session_ref
    assert resumed.metadata["resumed"] is True
    assert stored_after == stored_before
    assert stored_after["boot_sha256"] == hashlib_sha256("immutable boot bytes")
    assert second_factory.instances[0].env["TURN_MARKER"] == "two"
    assert "GRANTED_TOOL" not in second_factory.instances[0].env
    assert skill.read_text() == "version two"
    second.close()


def test_reconcile_uses_durable_run_boundary_and_never_invents_unknown_success(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state = tmp_path / "state"
    first_factory = Factory()
    first = make_adapter(state, first_factory)
    current = context(worktree)
    turn = first.start(current, "first")
    layout = deepseek_runtime.conversation_layout(current.conversation_id, state_root=state)
    first.close()
    layout.process_identity.unlink()

    recovery_factory = Factory(reconcile_outcome="unknown", pid=54322)
    recovery = make_adapter(state, recovery_factory)
    recovered = NativeTurn(
        harness="deepseek",
        session_ref=turn.session_ref,
        run_ref=turn.run_ref,
        worktree=worktree,
    )
    result = recovery.reconcile(recovered, current)

    assert result.outcome == "unknown"
    assert result.proven is False
    assert recovery_factory.instances[0].requests[-1] == (
        "session/reconcile",
        {"sessionId": turn.session_ref, "fromEventSeq": 5},
    )
    recovery.close()


def test_recovery_refuses_changed_worktree_before_starting_a_carrier(tmp_path: Path) -> None:
    original = tmp_path / "original"
    changed = tmp_path / "changed"
    original.mkdir()
    changed.mkdir()
    state = tmp_path / "state"
    first_factory = Factory()
    first = make_adapter(state, first_factory)
    initial = context(original)
    turn = first.start(initial, "first")
    layout = deepseek_runtime.conversation_layout(initial.conversation_id, state_root=state)
    first.close()
    layout.process_identity.unlink()

    recovery_factory = Factory(pid=54322)
    recovery = make_adapter(state, recovery_factory)
    recovered = NativeTurn(
        harness="deepseek",
        session_ref=turn.session_ref,
        run_ref=turn.run_ref,
        worktree=changed,
    )
    with pytest.raises(AdapterError) as refused:
        recovery.reconcile(recovered, context(changed))

    assert refused.value.code == "HARNESS_WORKTREE_MISMATCH"
    assert recovery_factory.instances == []


def test_missing_boot_or_runtime_fails_before_native_prompt(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory = Factory()
    missing_boot = make_adapter(tmp_path / "state-a", factory)
    with pytest.raises(AdapterError) as boot_refused:
        missing_boot.start(context(worktree, boot=None), "work")
    assert boot_refused.value.code == "HARNESS_BOOT_SNAPSHOT_MISSING"
    assert factory.instances == []

    unavailable_factory = Factory()
    unavailable = DeepSeekAdapter(
        runtime_probe=lambda **_: runtime_status(available=False),
        transport_factory=unavailable_factory,
        state_root=tmp_path / "state-b",
        start_ticks=lambda _pid: 77,
    )
    with pytest.raises(AdapterError) as runtime_refused:
        unavailable.start(context(worktree), "work")
    assert runtime_refused.value.code == "HARNESS_RUNTIME_MISSING"
    assert unavailable_factory.instances == []
