"""DeepSeek browser adapter lifecycle, isolation, and event contracts."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import deepseek_runtime  # noqa: E402
import route_bindings  # noqa: E402
from conversation_broker import BrokerRun, ConversationBroker  # noqa: E402
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


def deepseek_binding(
    *, effort: str = "default", provider_route: str = "deepseek-official"
) -> tuple[dict[str, Any], str]:
    manifest = deepseek_runtime.load_runtime_manifest()
    provider = deepseek_runtime.provider_adapter(provider_route)
    provider_model = (
        "deepseek-v4-pro"
        if provider_route == "deepseek-official"
        else "deepseek-v4-pro:0813"
    )
    selector = (
        provider_model
        if provider_route == "deepseek-official"
        else f"{provider_route}/{provider_model}"
    )
    endpoint = (
        "https://gateway.example/v1"
        if provider_route == "deepseek-official"
        else provider["endpoint_default"]
    )
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
        "requested_model": selector,
        "provider_model": provider_model,
        "requested_effort": effort,
        "effective_effort": effort,
        "native_variant_id": None,
        "transport": "deepseek-provider-options-v1",
        "catalogue_generation": "a" * 32,
        "evidence_digest": evidence,
        "selector_binding": {
            "kind": "authenticated-provider-model",
            "selector": selector,
        },
        "adapter_metadata": {
            "provider_route": provider_route,
            "provider_adapter_id": provider["adapter_id"],
            "provider_adapter_digest": route_bindings.digest_json(provider),
            "provider_registry_sha256": manifest["provider_adapters"]["sha256"],
            "credential_kind": provider["credential_kind"],
            "endpoint_identity": endpoint,
            "discovery_evidence_digest": "b" * 64,
            "transport_contract": "deepseek-provider-options-v1",
            "wire_evidence_digest": "c" * 64,
            "runtime_version": manifest["runtime"]["version"],
            "source_commit": manifest["source"]["commit"],
            "patch_sha256": manifest["patch"]["sha256"],
            "composition_sha256": provider["composition_sha256"],
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
    provider: str = "deepseek-official",
) -> ConversationContext:
    binding, digest = deepseek_binding(effort=effort, provider_route=provider)
    credential = (
        {"DEEPSEEK_API_KEY": "sk-test-secret-value",
         "DEEPSEEK_BASE_URL": "https://gateway.example/v1"}
        if provider == "deepseek-official"
        else {"OLLAMA_API_KEY": "ollama-test-secret-value"}
    )
    return ConversationContext(
        worktree=worktree,
        provider=provider,
        model=binding["requested_model"],
        effort=effort,
        permission_mode="unrestricted",
        env={
            **credential,
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
        prompt_error: AdapterError | None = None,
        prompt_result: Mapping[str, Any] | None = None,
        silent: bool = False,
        inspect_status: str = "idle",
        cancel_accepted: bool = True,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = dict(env)
        self.process = SimpleNamespace(pid=pid)
        self.items = list(notifications or [])
        self.reconcile_outcome = reconcile_outcome
        self.prompt_error = prompt_error
        self.prompt_result = prompt_result
        self.silent = silent
        self.inspect_status = inspect_status
        self.cancel_accepted = cancel_accepted
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
            if self.prompt_error is not None:
                raise self.prompt_error
            return dict(
                self.prompt_result
                if self.prompt_result is not None
                else {"messageId": "native-message-7"}
            )
        if method == "session/cancel":
            return {
                "sessionId": session_id,
                "accepted": self.cancel_accepted,
                "status": "idle" if self.cancel_accepted else "running",
                "outcome": "cancelled" if self.cancel_accepted else "running",
            }
        if method == "session/inspect":
            return {
                "sessionId": session_id,
                "presence": "persisted",
                "status": self.inspect_status,
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

    def poll_notification(self, _timeout: float):
        if self.items:
            return self.items.pop(0)
        if self.silent:
            return None
        raise AdapterError(
            "HARNESS_UNAVAILABLE",
            "fake carrier stream closed",
            retryable=True,
        )

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


def make_adapter(
    state_root: Path,
    factory: Factory,
    **adapter_options: Any,
) -> DeepSeekAdapter:
    adapter = DeepSeekAdapter(
        runtime_probe=lambda **_: runtime_status(),
        transport_factory=factory,
        state_root=state_root,
        start_ticks=lambda _pid: 77,
        **adapter_options,
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
    ] == ["browser", "sprint"]

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
        env={
            "DSH_HOME": "/attacker/home",
            "CURRENT_GRANT": "filesystem",
            "ANTHROPIC_API_KEY": "ambient-anthropic-secret",
            "KIMI_API_KEY": "ambient-kimi-secret",
            "MISTRAL_API_KEY": "ambient-mistral-secret",
            "OLLAMA_API_KEY": "ambient-ollama-secret",
            "OPENAI_API_KEY": "ambient-openai-secret",
        },
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
    assert {
        name
        for name in transport.env
        if name in {
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "MISTRAL_API_KEY",
            "OLLAMA_API_KEY",
            "OPENAI_API_KEY",
        }
    } == {"DEEPSEEK_API_KEY"}
    for leaked in (
        "ambient-anthropic-secret",
        "ambient-kimi-secret",
        "ambient-mistral-secret",
        "ambient-ollama-secret",
        "ambient-openai-secret",
    ):
        assert leaked not in transport.env.values()
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


def test_ollama_start_uses_raw_provider_model_and_only_ollama_credential(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory = Factory()
    adapter = make_adapter(tmp_path / "state", factory)

    adapter.start(
        context(
            worktree,
            provider="ollama-cloud",
            env={
                "ANTHROPIC_API_KEY": "ambient-anthropic-secret",
                "DEEPSEEK_API_KEY": "ambient-deepseek-secret",
                "KIMI_API_KEY": "ambient-kimi-secret",
                "MISTRAL_API_KEY": "ambient-mistral-secret",
                "OPENAI_API_KEY": "ambient-openai-secret",
            },
        ),
        "Do the work",
    )
    transport = factory.instances[0]

    assert transport.env["SC_DEEPSEEK_PROVIDER"] == "ollama-cloud"
    assert transport.env["SC_DEEPSEEK_MODEL"] == "deepseek-v4-pro:0813"
    assert transport.env["OLLAMA_API_KEY"] == "ollama-test-secret-value"
    assert "DEEPSEEK_API_KEY" not in transport.env
    assert "DEEPSEEK_BASE_URL" not in transport.env
    assert {
        name
        for name in transport.env
        if name in {
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "MISTRAL_API_KEY",
            "OLLAMA_API_KEY",
            "OPENAI_API_KEY",
        }
    } == {"OLLAMA_API_KEY"}
    for leaked in (
        "ambient-anthropic-secret",
        "ambient-deepseek-secret",
        "ambient-kimi-secret",
        "ambient-mistral-secret",
        "ambient-openai-secret",
    ):
        assert leaked not in transport.env.values()


@pytest.mark.parametrize(
    ("provider", "selected", "excluded"),
    (
        (
            "deepseek-official",
            "@deepseek-ai/dsh-llm-deepseek",
            "@deepseek-ai/dsh-llm-pi-ai",
        ),
        (
            "ollama-cloud",
            "@deepseek-ai/dsh-llm-pi-ai",
            "@deepseek-ai/dsh-llm-deepseek",
        ),
    ),
)
def test_exact_resume_preserves_selected_provider_composition(
    tmp_path: Path, provider: str, selected: str, excluded: str
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state = tmp_path / "state"
    first_factory = Factory()
    first = make_adapter(state, first_factory)
    current = context(worktree, provider=provider)
    turn = first.start(current, "first")
    layout = deepseek_runtime.conversation_layout(current.conversation_id, state_root=state)
    first.close()
    layout.process_identity.unlink()

    second_factory = Factory(pid=54322)
    second = make_adapter(state, second_factory)
    resumed = second.resume(turn.session_ref, current, "second")
    first_config = first_factory.instances[0].env["DSH_CORDIS_CONFIG"]
    second_config = second_factory.instances[0].env["DSH_CORDIS_CONFIG"]
    body = Path(second_config).read_text()

    assert resumed.session_ref == turn.session_ref
    assert first_config == second_config
    assert selected in body
    assert excluded not in body
    second.close()


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


@pytest.mark.parametrize(
    ("native_code", "native_message", "expected_code"),
    [
        (
            "INVALID_CREDENTIAL",
            "opaque provider detail",
            "HARNESS_NATIVE_RUN_INVALID_CREDENTIAL",
        ),
        ("HTTP_503", "opaque provider detail", "HARNESS_NATIVE_RUN_HTTP_503"),
        (
            "PI_AI_ERROR",
            "request failed with 404 status code (no body)",
            "HARNESS_NATIVE_RUN_HTTP_404",
        ),
        (
            "PI_AI_ERROR",
            "opaque provider detail token=404-must-not-survive",
            "HARNESS_NATIVE_RUN_PI_AI_ERROR",
        ),
        (
            "invalid credential token=must-not-survive",
            "opaque provider detail",
            "HARNESS_NATIVE_RUN_FAILED",
        ),
        (
            "PLAUSIBLE_BUT_UNKNOWN",
            "opaque provider detail",
            "HARNESS_NATIVE_RUN_FAILED",
        ),
        (None, "opaque provider detail", "HARNESS_NATIVE_RUN_FAILED"),
    ],
)
def test_turn_failure_projects_only_a_strict_native_error_code(
    tmp_path: Path,
    native_code: str | None,
    native_message: str,
    expected_code: str,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory = Factory()
    adapter = make_adapter(tmp_path / "state", factory)
    try:
        turn = adapter.start(context(worktree), "Do it")
        reason: dict[str, Any] = {
            "kind": "error",
            "error": {"message": native_message},
        }
        if native_code is not None:
            reason["error"]["code"] = native_code
        factory.instances[0].items = [
            native_notification(
                turn.session_ref,
                "turn/end",
                5,
                {"turn": 1, "reason": reason},
            )
        ]

        events = list(adapter.stream(turn))

        assert [event.type for event in events] == ["session.started", "run.failed"]
        assert events[-1].payload["error"] == expected_code
        assert "must-not-survive" not in events[-1].payload["error"]
    finally:
        adapter.close()


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


@pytest.mark.parametrize(
    ("transport_options", "expected_code"),
    [
        (
            {
                "prompt_error": AdapterError(
                    "HARNESS_TIMEOUT", "native prompt response timed out"
                )
            },
            "HARNESS_TIMEOUT",
        ),
        (
            {
                "prompt_error": AdapterError(
                    "HARNESS_PROTOCOL_ERROR", "native prompt was rejected"
                )
            },
            "HARNESS_PROTOCOL_ERROR",
        ),
        ({"prompt_result": {}}, "HARNESS_PROTOCOL_ERROR"),
    ],
)
def test_prompt_failure_returns_recoverable_identity_and_exact_resume(
    tmp_path: Path,
    transport_options: Mapping[str, Any],
    expected_code: str,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state = tmp_path / "state"
    first_factory = Factory(**dict(transport_options))
    first = make_adapter(state, first_factory)
    current = context(worktree)

    uncertain = first.start(current, "first")
    layout = deepseek_runtime.conversation_layout(
        current.conversation_id, state_root=state
    )
    events = list(first.stream(uncertain))

    assert uncertain.session_ref.startswith("deepseek-")
    assert uncertain.run_ref.startswith("deepseek-run-v1:")
    assert uncertain.metadata["dispatch_error"]["code"] == expected_code
    assert json.loads(layout.adapter_identity.read_text())["session_ref"] == (
        uncertain.session_ref
    )
    assert [event.type for event in events] == ["session.started", "run.failed"]
    assert events[-1].payload["error"] == expected_code
    assert events[-1].payload["native_cancelled"] is True
    assert first_factory.instances[0].requests[-1] == (
        "session/cancel",
        {"sessionId": uncertain.session_ref},
    )

    first.close()
    layout.process_identity.unlink()
    recovery_factory = Factory(pid=54322)
    recovery = make_adapter(state, recovery_factory)
    resumed = recovery.start(current, "retry")

    assert resumed.session_ref == uncertain.session_ref
    assert resumed.metadata["resumed"] is True
    assert recovery_factory.instances[0].requests[:2] == [
        ("session/start", {"sessionId": uncertain.session_ref}),
        ("session/prompt", {"sessionId": uncertain.session_ref, "message": "retry"}),
    ]


def test_broker_captures_prompt_failure_identity_before_terminal(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    factory = Factory(
        prompt_error=AdapterError(
            "HARNESS_TIMEOUT", "native prompt response timed out"
        )
    )
    adapter = make_adapter(tmp_path / "state", factory)
    current = context(worktree)
    store = mock.Mock()
    store.append_events.side_effect = lambda _run_id, events: list(
        range(1, len(events) + 1)
    )
    store.finish_run.return_value = True
    broker = ConversationBroker(
        tmp_path / "unused.db",
        store=store,
        adapter_factory=lambda _harness: adapter,
        launch_preparer=lambda _run: (current, 17),
    )
    run = BrokerRun(
        run_id=7,
        conversation_id=current.conversation_id or "",
        message_id=11,
        shell_id=1,
        harness="deepseek",
        provider=current.provider,
        model=current.model,
        effort=current.effort,
        worktree=worktree,
        title=None,
        body="first",
        session_before=None,
        session_after=None,
        runner_ref=None,
        state="leased",
        route_contract_version=2,
        route_binding=current.route_binding,
        binding_digest=current.binding_digest,
    )
    active = SimpleNamespace(
        run=run,
        adapter=None,
        turn=None,
        interrupt_requested=False,
        interrupt_sent=False,
    )

    broker._execute(active)

    captured = store.mark_native_started.call_args.args[2]
    assert captured.session_ref.startswith("deepseek-")
    assert captured.run_ref.startswith("deepseek-run-v1:")
    assert captured.metadata["dispatch_error"]["code"] == "HARNESS_TIMEOUT"
    terminal_call = store.finish_run.call_args
    assert terminal_call.args[:2] == (7, "failed")
    assert terminal_call.kwargs["error_code"] == "HARNESS_TIMEOUT"


def test_uncancelled_prompt_failure_reconciles_exactly_after_close(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state = tmp_path / "state"
    first_factory = Factory(
        prompt_error=AdapterError(
            "HARNESS_TIMEOUT", "native prompt response timed out"
        ),
        cancel_accepted=False,
    )
    first = make_adapter(state, first_factory)
    current = context(worktree)
    uncertain = first.start(current, "first")

    assert [event.type for event in first.stream(uncertain)] == [
        "session.started"
    ]
    first.close()
    layout = deepseek_runtime.conversation_layout(
        current.conversation_id, state_root=state
    )
    layout.process_identity.unlink()
    recovery_factory = Factory(reconcile_outcome="unknown", pid=54322)
    recovery = make_adapter(state, recovery_factory)

    result = recovery.reconcile(uncertain, current)

    assert result.outcome == "unknown"
    assert result.proven is False
    assert recovery_factory.instances[0].requests[-1] == (
        "session/reconcile",
        {"sessionId": uncertain.session_ref, "fromEventSeq": 5},
    )


def test_silent_live_carrier_is_bounded_reconciled_and_scoped(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state = tmp_path / "state"
    factory = Factory(
        silent=True,
        inspect_status="running",
        reconcile_outcome="running",
    )
    adapter = make_adapter(
        state,
        factory,
        stream_inactivity_seconds=0.001,
        silent_probe_limit=2,
    )
    current = context(worktree)
    turn = adapter.start(current, "long work")

    events = list(adapter.stream(turn))

    assert [event.type for event in events] == ["session.started", "run.failed"]
    assert events[-1].payload == {
        "status": "failed",
        "error": "HARNESS_STREAM_INACTIVE",
        "detail": "DeepSeek carrier stayed silent through bounded liveness probes",
        "native_cancelled": True,
        "last_inspected_state": "running",
        "session_ref": turn.session_ref,
        "run_ref": turn.run_ref,
    }
    assert factory.instances[0].requests[-5:] == [
        ("session/inspect", {"sessionId": turn.session_ref}),
        (
            "session/reconcile",
            {"sessionId": turn.session_ref, "fromEventSeq": 5},
        ),
        ("session/inspect", {"sessionId": turn.session_ref}),
        (
            "session/reconcile",
            {"sessionId": turn.session_ref, "fromEventSeq": 5},
        ),
        ("session/cancel", {"sessionId": turn.session_ref}),
    ]
    assert factory.instances[0].closed is True

    layout = deepseek_runtime.conversation_layout(
        current.conversation_id, state_root=state
    )
    layout.process_identity.unlink()
    recovery_factory = Factory(pid=54322)
    recovery = make_adapter(state, recovery_factory)
    resumed = recovery.resume(turn.session_ref, current, "after silence")
    assert resumed.session_ref == turn.session_ref


def test_exact_resume_refreshes_skill_root_without_changing_boot_or_identity(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    skill_root = worktree / ".agents" / "skills"
    skill_root.mkdir(parents=True)
    state = tmp_path / "state"
    first_factory = Factory()
    first = make_adapter(state, first_factory)
    initial = context(worktree, env={"TURN_MARKER": "one"})
    first_turn = first.start(initial, "first")
    layout = deepseek_runtime.conversation_layout(initial.conversation_id, state_root=state)
    stored_before = json.loads(layout.adapter_identity.read_text())
    first.close()
    layout.process_identity.unlink()

    assert first_factory.instances[0].env["DSH_SKILL_ROOT"] == str(skill_root)

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
    assert second_factory.instances[0].env["DSH_SKILL_ROOT"] == str(skill_root)
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
