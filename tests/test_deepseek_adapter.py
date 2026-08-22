"""Stock DeepSeek Host projection, one-shot, and Browser lifecycle contracts."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api")]

import deepseek_host  # noqa: E402
import deepseek_one_shot  # noqa: E402
import route_bindings  # noqa: E402
from conversation_adapters.base import AdapterError, ConversationContext  # noqa: E402
from conversation_adapters.deepseek import DeepSeekAdapter  # noqa: E402


def configuration(*, credential: Mapping[str, Any] | None = None) -> dict:
    return {
        "host.describe": {"version": "0.1.1-rc.2"},
        "llm.providers": {"providers": [{
            "provider": "acme-dynamic", "active": True,
            "settingsNs": "llm", "settingsPath": ["providers", "acme-dynamic"],
        }]},
        "llm.models": {"groups": [{
            "id": "acme-dynamic",
            "models": [{
                "id": "model-7", "name": "Model Seven",
                "reasoning": {
                    "efforts": [{"id": "low"}, {"id": "high"}],
                    "defaultEffort": "high",
                },
            }],
        }]},
        "settings.describe": {"namespaces": [{
            "ns": "llm",
            "value": {"providers": {"acme-dynamic": {
                "baseURL": "https://models.acme.test/v1",
                "apiKeyEnv": "ACME_API_KEY",
            }}},
        }]},
        "credentials.describe": {"credentials": {"ACME_API_KEY": dict(
            credential or {
                "configured": True, "source": "environment", "writable": False,
            }
        )}},
    }


class FakeStream:
    def __init__(self, frames=()) -> None:
        self.frames = list(frames)
        self.closed = False

    def __iter__(self):
        yield from self.frames

    def close(self) -> None:
        self.closed = True


class FakeHost:
    def __init__(self, *, config=None, frames=(), history=None) -> None:
        self.config = copy.deepcopy(config or configuration())
        self.frames = list(frames)
        self.history = list(history or [])
        self.calls: list[tuple[str, dict]] = []
        self.streams: list[FakeStream] = []

    def call(self, method: str, payload: Mapping[str, Any]) -> Any:
        request = dict(payload)
        self.calls.append((method, request))
        if method in self.config:
            if method == "credentials.describe":
                assert request == {"refs": ["ACME_API_KEY"]}
            return copy.deepcopy(self.config[method])
        if method == "session.create":
            return {"sessionId": request["sessionId"]}
        if method == "session.history":
            return {"events": [{"event": copy.deepcopy(row)} for row in self.history]}
        if method == "session.selectModel":
            return {"selected": {
                key: request[key]
                for key in ("provider", "model", "reasoningEffort")
                if key in request
            }}
        if method == "session.prompt":
            return {"accepted": True}
        if method == "session.cancel":
            return {"accepted": True}
        if method == "session.list":
            return {"items": []}
        raise AssertionError(f"unexpected Host method: {method}")

    def open_events(self) -> FakeStream:
        stream = FakeStream(self.frames)
        self.streams.append(stream)
        return stream


def selected_route(fake: FakeHost) -> deepseek_host.ConfiguredRoute:
    return deepseek_host.route_for(fake, "acme-dynamic/model-7")


def context(tmp_path: Path, fake: FakeHost, effort: str = "high") -> ConversationContext:
    route = selected_route(fake)
    binding = {
        "contract_version": 2, "control_state": "controlled",
        "harness": "deepseek", "requested_model": route.selector,
        "provider_model": route.model, "requested_effort": effort,
        "effective_effort": effort, "native_variant_id": None,
        "transport": deepseek_host.TRANSPORT_CONTRACT,
        "catalogue_generation": "a" * 32,
        "evidence_digest": None if effort == "default" else "b" * 64,
        "selector_binding": {
            "kind": "official-host-configured-model", "selector": route.selector,
        },
        "adapter_metadata": route.binding_metadata(effort),
    }
    route_bindings.validate_v2_binding(binding)
    return ConversationContext(
        worktree=tmp_path, provider=route.provider, model=route.selector,
        effort=effort, route_binding=binding,
        binding_digest=route_bindings.digest_json(binding),
        conversation_id="cv_" + "1" * 32,
    )


def frame(session_id: str, event: dict) -> dict:
    return {"type": "server-request", "rpcId": "event-1", "payload": {
        "type": "session/event", "sessionId": session_id, "event": event,
    }}


def test_configuration_is_dynamic_redacted_and_official_only() -> None:
    fake = FakeHost()
    route = selected_route(fake)
    assert route.selector == "acme-dynamic/model-7"
    assert route.reasoning_efforts == ("low", "high")
    assert route.credential_status == {
        "configured": True, "source": "environment", "writable": False,
    }
    assert [method for method, _ in fake.calls] == [
        "host.describe", "llm.providers", "llm.models", "settings.describe",
        "credentials.describe",
    ]
    assert "secret" not in repr(route).lower()


def test_secret_bearing_credential_descriptor_is_rejected() -> None:
    fake = FakeHost(config=configuration(credential={
        "configured": True, "writable": False, "value": "sk-never-project",
    }))
    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        selected_route(fake)
    assert refused.value.code == "HARNESS_HOST_RESPONSE_INVALID"
    assert "sk-never-project" not in str(refused.value)


def test_unconfigured_credential_excludes_route() -> None:
    fake = FakeHost(config=configuration(credential={
        "configured": False, "writable": True,
    }))
    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        selected_route(fake)
    assert refused.value.code == "HARNESS_ROUTE_UNAVAILABLE"


@pytest.mark.parametrize("value", [
    "http://localhost:1234", "https://127.0.0.1:1234", "http://127.0.0.1",
    "http://user:pass@127.0.0.1:1234", "http://127.0.0.1:1234/api",
    "http://127.0.0.1:99999",
])
def test_host_endpoint_is_exact_loopback(value: str) -> None:
    with pytest.raises(deepseek_host.DeepSeekHostError):
        deepseek_host.checked_host_url(value)


@pytest.mark.parametrize("value", [None, "", "0", "06500", "65536", " 6500"])
def test_injected_host_port_is_required_and_exact(value: str | None) -> None:
    env = {} if value is None else {"SC_DEEPSEEK_HOST_PORT": value}
    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.configured_host_url(env)
    assert refused.value.code == "HARNESS_HOST_UNAVAILABLE"


def test_injected_host_port_derives_the_only_endpoint() -> None:
    assert deepseek_host.configured_host_url({
        "SC_DEEPSEEK_HOST_PORT": "6500",
    }) == "http://127.0.0.1:6500"


def test_unary_client_uses_exact_official_envelope(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({
                "type": "server-response",
                "rpcId": "00000000-0000-0000-0000-000000000007",
                "result": {"ok": True, "value": {"version": "0.1.1-rc.2"}},
            }).encode()

    def opener(request, *, timeout):
        captured.update({
            "url": request.full_url,
            "body": json.loads(request.data),
            "timeout": timeout,
        })
        return Response()

    monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", "6500")
    monkeypatch.setattr(
        deepseek_host.uuid,
        "uuid4",
        lambda: "00000000-0000-0000-0000-000000000007",
    )
    client = deepseek_host.DeepSeekHostClient(opener=opener)

    assert client.call("host.describe", {}) == {"version": "0.1.1-rc.2"}
    assert captured == {
        "url": "http://127.0.0.1:6500/api/host.describe",
        "body": {
            "type": "client-request",
            "rpcId": "00000000-0000-0000-0000-000000000007",
            "method": "host.describe",
            "payload": {},
        },
        "timeout": 15.0,
    }


def test_stale_route_fails_before_session_mutation(tmp_path: Path) -> None:
    bound = FakeHost()
    ctx = context(tmp_path, bound)
    changed = configuration()
    changed["settings.describe"]["namespaces"][0]["value"]["providers"][
        "acme-dynamic"
    ]["baseURL"] = "https://changed.acme.test/v1"
    live = FakeHost(config=changed)
    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).start(ctx, "hello")
    assert refused.value.code == "HARNESS_ROUTE_STALE"
    assert "session.create" not in [method for method, _ in live.calls]


def test_browser_start_stream_and_exact_call_order(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(frames=[
        frame(session, {"seq": 0, "type": "turn/start", "data": {}}),
        frame(session, {"seq": 1, "type": "assistant/chunk", "data": {
            "chunk": {"type": "text-delta", "text": "hello"},
        }}),
        frame(session, {"seq": 2, "type": "turn/end", "data": {
            "reason": {"kind": "completed"},
        }}),
    ])
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    events = list(adapter.stream(adapter.start(ctx, "do work")))
    assert [event.type for event in events] == [
        "session.started", "run.started", "assistant.delta", "run.completed",
    ]
    assert events[2].payload["text"] == "hello"
    assert live.calls[-4:] == [
        ("session.create", {"sessionId": session, "cwd": str(tmp_path)}),
        ("session.history", {"sessionId": session, "maxMessages": 200}),
        ("session.selectModel", {
            "sessionId": session, "provider": "acme-dynamic", "model": "model-7",
            "reasoningEffort": "high",
        }),
        ("session.prompt", {
            "sessionId": session, "mode": "queue",
            "content": [{"type": "text", "text": "do work"}],
        }),
    ]
    assert live.streams[0].closed


def test_cold_resume_reuses_session_and_prior_history(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(history=[{
        "seq": 0, "type": "turn/end", "data": {"reason": {"kind": "completed"}},
    }])
    turn = DeepSeekAdapter(client_factory=lambda: live).resume(session, ctx, "continue")
    assert turn.session_ref == session
    assert ("session.create", {"sessionId": session, "cwd": str(tmp_path)}) in live.calls
    assert ("session.history", {"sessionId": session, "maxMessages": 200}) in live.calls


def test_stream_loss_never_invents_success(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = FakeHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    events = list(adapter.stream(adapter.start(ctx, "work")))
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"] == "HARNESS_RECONCILIATION_UNKNOWN"


def test_approval_cancels_only_owning_session(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(frames=[{
        "type": "server-request", "rpcId": "approval-1", "payload": {
            "type": "approval/requested", "sessionId": session,
        },
    }])
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    events = list(adapter.stream(adapter.start(ctx, "work")))
    assert events[-1].payload["error"] == "HARNESS_APPROVAL_UNSUPPORTED"
    assert live.calls[-1] == ("session.cancel", {"sessionId": session})


def test_one_shot_uses_same_exact_route(tmp_path: Path, capsys, monkeypatch) -> None:
    fake = FakeHost()
    original_call = fake.call

    def call(method: str, payload: Mapping[str, Any]) -> Any:
        result = original_call(method, payload)
        if method == "session.create":
            session = payload["sessionId"]
            fake.frames = [
                frame(session, {"seq": 0, "type": "assistant/chunk", "data": {
                    "chunk": {"type": "text-delta", "text": "answer"},
                }}),
                frame(session, {"seq": 1, "type": "turn/end", "data": {
                    "reason": {"kind": "completed"},
                }}),
            ]
        return result

    fake.call = call  # type: ignore[method-assign]
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_host, "DeepSeekHostClient", lambda: fake)
    assert deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt") == 0
    assert capsys.readouterr().out == "answer\n"
    select = next(payload for method, payload in fake.calls if method == "session.selectModel")
    assert select["provider"] == "acme-dynamic"
    assert select["model"] == "model-7"
    assert select["reasoningEffort"] == "high"


def test_one_shot_rejects_wrong_native_selection_before_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    class WrongSelectionHost(FakeHost):
        def call(self, method, payload):
            result = super().call(method, payload)
            if method == "session.selectModel":
                return {"selected": {"provider": "other", "model": "fallback"}}
            return result

    fake = WrongSelectionHost()
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_host, "DeepSeekHostClient", lambda: fake)

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")

    assert refused.value.code == "HARNESS_ROUTE_MISMATCH"
    assert "session.prompt" not in [method for method, _payload in fake.calls]
