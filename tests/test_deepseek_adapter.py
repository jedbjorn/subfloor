"""Stock DeepSeek Host projection, one-shot, and Browser lifecycle contracts."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api")]

import deepseek_host  # noqa: E402
import deepseek_one_shot  # noqa: E402
import deepseek_web  # noqa: E402
import harness_versions  # noqa: E402
import route_bindings  # noqa: E402
import conversation_adapters.deepseek as deepseek_adapter  # noqa: E402
from conversation_adapters.base import (  # noqa: E402
    AdapterError,
    ConversationContext,
    NativeTurn,
)
from conversation_adapters.deepseek import (  # noqa: E402
    DeepSeekAdapter,
    _run_ref,
)

REAL_RESERVE_MANAGED_SESSION = deepseek_web.reserve_managed_session
REAL_RELEASE_MANAGED_SESSION = deepseek_web.release_managed_session


def configuration(*, credential: Mapping[str, Any] | None = None) -> dict:
    return {
        "host.describe": {"version": "0.0.1"},
        "agentPreset.list": {
            "presets": [{"id": "standard", "trust": "system", "isDefault": True}],
            "authorable": True,
            "hasDocument": False,
        },
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


@pytest.fixture(autouse=True)
def stock_cli_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        harness_versions, "probe", lambda harness: "0.1.1-rc.2"
    )


@pytest.fixture(autouse=True)
def canonical_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit Hosts isolated while every managed path has canonical identity."""
    monkeypatch.setattr(deepseek_web, "ensure", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        deepseek_web, "bind_session_identity", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        deepseek_web, "retire_session_identity", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        deepseek_web, "preflight_candidate_execution", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        deepseek_web, "admit_candidate_execution", lambda **_kwargs: None
    )
    monkeypatch.setattr(deepseek_web, "reserve_managed_session", lambda _session: None)
    monkeypatch.setattr(deepseek_web, "release_managed_session", lambda _session: None)


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
        self.existing_session = history is not None
        self.permission_default = "workspace-write"
        self.calls: list[tuple[str, dict]] = []
        self.streams: list[FakeStream] = []
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, str] = {}

    def seed_session(self, session_id: str, cwd: str, *, workspace_id: str = "ws-1") -> None:
        self.workspaces[workspace_id] = {
            "workspaceId": workspace_id,
            "path": cwd,
            "sessionIds": [session_id],
            "archivedSessionIds": [],
        }
        self.sessions[session_id] = cwd

    def call(self, method: str, payload: Mapping[str, Any]) -> Any:
        request = dict(payload)
        self.calls.append((method, request))
        if method in self.config:
            if method == "credentials.describe":
                assert request == {"refs": ["ACME_API_KEY"]}
            return copy.deepcopy(self.config[method])
        if method == "settings.update":
            self.permission_default = request["patch"]["defaultPreset"]
            return {
                "ns": request["ns"],
                "schema": {},
                "value": {"defaultPreset": self.permission_default},
                "applies": "live",
                "secrets": [],
                "revision": 1,
            }
        if method == "workspace.create":
            path = request["path"]
            row = next(
                (item for item in self.workspaces.values() if item["path"] == path),
                None,
            )
            if row is None:
                workspace_id = f"ws-{len(self.workspaces) + 1}"
                row = {
                    "workspaceId": workspace_id,
                    "path": path,
                    "sessionIds": [],
                    "archivedSessionIds": [],
                }
                self.workspaces[workspace_id] = row
            return {"created": False, "workspace": copy.deepcopy(row)}
        if method == "workspace.list":
            return {
                "items": [
                    {
                        key: copy.deepcopy(value)
                        for key, value in row.items()
                        if key != "archivedSessionIds"
                    }
                    for row in self.workspaces.values()
                ],
                "archivedSessionIds": [
                    session_id
                    for row in self.workspaces.values()
                    for session_id in row["archivedSessionIds"]
                ],
            }
        if method == "session.create":
            if not self.existing_session:
                self.history = [
                    {"seq": 0, "type": "permission/preset", "data": {
                        "preset": self.permission_default,
                    }},
                    {"seq": 1, "type": "sandbox/mode", "data": {
                        "mode": self.permission_default,
                    }},
                    {"seq": 2, "type": "approval/policy", "data": {
                        "policy": "never"
                        if self.permission_default == "danger-full-access"
                        else "ask",
                    }},
                ]
                self.existing_session = True
            workspace_id = request.get("workspaceId")
            cwd = request.get("cwd")
            if isinstance(workspace_id, str):
                workspace = self.workspaces[workspace_id]
                cwd = workspace["path"]
                if request["sessionId"] not in workspace["sessionIds"]:
                    workspace["sessionIds"].append(request["sessionId"])
            assert isinstance(cwd, str)
            self.sessions[request["sessionId"]] = cwd
            return {
                "sessionId": request["sessionId"],
                "agentPreset": request.get("agentPreset"),
            }
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
            return {"items": [
                {"sessionId": session_id, "cwd": cwd, "running": False}
                for session_id, cwd in self.sessions.items()
            ]}
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
        env={
            "SC_API_TOKEN": "test-shell-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_ID": "4",
            "SC_SHELL_SHORTNAME": "DEV4",
        },
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


def test_configuration_preserves_exact_reasoning_ids_order_and_default() -> None:
    config = configuration()
    config["llm.models"]["groups"][0]["models"][0]["reasoning"] = {
        "efforts": [
            {"id": "MAX.Future"},
            {"id": "low"},
            {"id": "Provider/Exact"},
        ],
        "defaultEffort": "MAX.Future",
    }

    route = deepseek_host.configured_routes(FakeHost(config=config))[0]

    assert route.reasoning_efforts == (
        "MAX.Future", "low", "Provider/Exact",
    )
    assert route.default_effort == "MAX.Future"


def test_configuration_allows_a_model_with_no_reasoning_options() -> None:
    config = configuration()
    del config["llm.models"]["groups"][0]["models"][0]["reasoning"]

    route = deepseek_host.configured_routes(FakeHost(config=config))[0]

    assert route.reasoning_efforts == ()
    assert route.default_effort is None


def test_configuration_rejects_duplicate_and_oversized_reasoning_options() -> None:
    duplicate = configuration()
    duplicate["llm.models"]["groups"][0]["models"][0]["reasoning"] = {
        "efforts": [{"id": "Exact"}, {"id": "Exact"}],
    }
    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.configured_routes(FakeHost(config=duplicate))
    assert refused.value.code == "HARNESS_HOST_RESPONSE_INVALID"
    assert "duplicate exact reasoning option id" in str(refused.value)

    oversized = configuration()
    oversized["llm.models"]["groups"][0]["models"][0]["reasoning"] = {
        "efforts": [
            {"id": f"option-{index}"}
            for index in range(deepseek_host.MAX_REASONING_OPTIONS + 1)
        ],
    }
    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.configured_routes(FakeHost(config=oversized))
    assert refused.value.code == "HARNESS_HOST_RESPONSE_INVALID"
    assert "reasoning options exceed safety limits" in str(refused.value)


def test_configuration_rejects_missing_host_api_version() -> None:
    config = configuration()
    config["host.describe"] = {}
    fake = FakeHost(config=config)

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.configured_routes(fake)

    assert refused.value.code == "HARNESS_HOST_RESPONSE_INVALID"
    assert "llm.providers" not in [method for method, _ in fake.calls]


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


def test_active_provider_without_settings_profile_has_stable_failure() -> None:
    config = configuration()
    config["llm.providers"]["providers"] = [{
        "provider": "odd", "active": True,
    }]
    config["llm.models"]["groups"] = [{
        "id": "odd", "models": [{"id": "model-1"}],
    }]
    fake = FakeHost(config=config)

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.configured_routes(fake)

    assert refused.value.code == "HARNESS_HOST_RESPONSE_INVALID"
    assert str(refused.value) == (
        "HARNESS_HOST_RESPONSE_INVALID: provider odd has no usable settings profile"
    )
    assert "credentials.describe" not in [method for method, _ in fake.calls]


@pytest.mark.parametrize("value", [
    "http://localhost:1234", "https://127.0.0.1:1234", "http://127.0.0.1",
    "http://user:pass@127.0.0.1:1234", "http://127.0.0.1:1234/api",
    "http://127.0.0.1:99999",
])
def test_host_endpoint_is_exact_loopback(value: str) -> None:
    with pytest.raises(deepseek_host.DeepSeekHostError):
        deepseek_host.checked_host_url(value)


@pytest.mark.parametrize("value", ["", "0", "06500", "65536", " 6500"])
def test_injected_host_port_is_exact(value: str) -> None:
    env = {"SC_DEEPSEEK_HOST_PORT": value}
    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.configured_host_url(env)
    assert refused.value.code == "HARNESS_HOST_UNAVAILABLE"


def test_missing_host_port_uses_the_managed_fork_seat() -> None:
    with mock.patch.object(
        deepseek_host.ports,
        "resolve",
        return_value={"deepseek_host_port": 6501},
    ) as resolve:
        endpoint = deepseek_host.configured_host_url({})

    assert endpoint == "http://127.0.0.1:6501"
    resolve.assert_called_once_with(persist=False)


def test_injected_host_port_derives_the_only_endpoint() -> None:
    assert deepseek_host.configured_host_url({
        "SC_DEEPSEEK_HOST_PORT": "6500",
    }) == "http://127.0.0.1:6500"


def test_stock_evidence_records_unattended_permission_contract() -> None:
    evidence = json.loads(
        (ENGINE / "assets/deepseek/stock-host-seam.json").read_text()
    )
    assert evidence["managed_session"]["unattended_permission_preset"] == {
        "settings_write": {
            "method": "settings.update",
            "transport": "loopback HTTP POST /api/settings.update",
            "payload": {
                "ns": "permission",
                "patch": {"defaultPreset": "danger-full-access"},
            },
        },
        "create_hook_event": {
            "type": "permission/preset",
            "data": {"preset": "danger-full-access"},
        },
        "verification": {
            "method": "session.history",
            "transport": "loopback HTTP POST /api/session.history",
        },
        "resume_default_write": False,
        "pinned_sources": [
            {
                "path": "packages/host/apiproxy/src/api/rpc-map.ts",
                "lines": "22-33,63-67",
                "proves": (
                    "settings.update and session.history are registered HTTP "
                    "unary methods"
                ),
            },
            {
                "path": "packages/host/apiproxy/src/api/settings.ts",
                "lines": "71-77",
                "proves": "settings.update accepts ns plus a merge patch",
            },
            {
                "path": "packages/host/apiproxy/src/api-proxy.ts",
                "lines": "1792-1838,3077",
                "proves": (
                    "the settings write commits before its redacted descriptor "
                    "response"
                ),
            },
            {
                "path": (
                    "packages/interaction/permission-presets/src/index.ts"
                ),
                "lines": "66-68,208-220,388-400",
                "proves": (
                    "permission.defaultPreset is validated and pinned as session "
                    "creation events"
                ),
            },
            {
                "path": "packages/host/apiproxy/src/api/sessions.ts",
                "lines": "247-265",
                "proves": "session.history returns raw session events",
            },
        ],
    }


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


def test_stale_v2_metadata_does_not_block_current_exact_route(tmp_path: Path) -> None:
    bound = FakeHost()
    ctx = context(tmp_path, bound)
    changed = configuration()
    changed["settings.describe"]["namespaces"][0]["value"]["providers"][
        "acme-dynamic"
    ]["baseURL"] = "https://changed.acme.test/v1"
    live = FakeHost(config=changed)
    route = DeepSeekAdapter._route(live, ctx)

    assert route.selector == "acme-dynamic/model-7"
    assert route.endpoint_identity == "https://changed.acme.test/v1"
    assert "session.create" not in [method for method, _ in live.calls]


def test_missing_current_native_option_refuses_before_session_mutation(
    tmp_path: Path,
) -> None:
    bound = FakeHost()
    ctx = context(tmp_path, bound)
    changed = configuration()
    changed["llm.models"]["groups"][0]["models"][0]["reasoning"] = {
        "efforts": [{"id": "low"}],
        "defaultEffort": "low",
    }
    live = FakeHost(config=changed)

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter._route(live, ctx)

    assert refused.value.code == "native_route_unavailable"
    assert "session.create" not in [method for method, _ in live.calls]


def test_probe_uses_cli_version_and_requires_shipped_managed_agent_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = FakeHost()
    result = DeepSeekAdapter(client_factory=lambda: healthy).probe()
    assert result.version == "0.1.1-rc.2"
    assert [method for method, _ in healthy.calls] == [
        "host.describe", "agentPreset.list",
    ]

    monkeypatch.setattr(harness_versions, "probe", lambda harness: "0.1.0")
    incompatible = FakeHost()
    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: incompatible).probe()
    assert refused.value.code == "HARNESS_VERSION_UNSUPPORTED"
    assert incompatible.calls == [("host.describe", {})]

    monkeypatch.setattr(
        harness_versions, "probe", lambda harness: "0.1.1-rc.2"
    )
    broken = configuration()
    broken["agentPreset.list"]["presets"][0]["broken"] = "missing plugin"
    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: FakeHost(config=broken)).probe()
    assert refused.value.code == "HARNESS_AGENT_PRESET_UNAVAILABLE"


def test_browser_start_stream_and_exact_call_order(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(frames=[
        frame(session, {"seq": 3, "type": "turn/start", "data": {}}),
        frame(session, {"seq": 4, "type": "assistant/chunk", "data": {
            "chunk": {"type": "text-delta", "text": "hello"},
        }}),
        frame(session, {"seq": 5, "type": "turn/end", "data": {
            "reason": {"kind": "completed"},
        }}),
    ])
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    events = list(adapter.stream(adapter.start(ctx, "do work")))
    assert [event.type for event in events] == [
        "session.started", "run.started", "assistant.delta", "run.completed",
    ]
    assert events[2].payload["text"] == "hello"
    assert ("workspace.create", {"path": str(tmp_path)}) in live.calls
    assert ("session.create", {
        "workspaceId": "ws-1", "sessionId": session, "agentPreset": "standard",
    }) in live.calls
    assert ("workspace.list", {}) in live.calls
    assert ("session.list", {}) in live.calls
    assert ("session.prompt", {
        "sessionId": session, "mode": "queue",
        "content": [{"type": "text", "text": "do work"}],
    }) in live.calls
    assert not any(
        method == "session.create" and "cwd" in payload
        for method, payload in live.calls
    )
    assert live.streams[0].closed


def test_managed_clients_overlap_without_a_global_identity_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Barrier(2)
    ensured: list[str] = []

    def ensure(_worktree, **kwargs):
        ensured.append(kwargs["env"]["SC_SHELL_SHORTNAME"])
        entered.wait(timeout=2)
        return {}

    monkeypatch.setattr(deepseek_web, "ensure", ensure)
    hosts = (FakeHost(), FakeHost())
    contexts = (context(tmp_path, hosts[0]), context(tmp_path, hosts[1]))
    contexts[0].env["SC_SHELL_SHORTNAME"] = "ALICE"
    contexts[1].env["SC_SHELL_SHORTNAME"] = "BOB"
    adapters = tuple(DeepSeekAdapter(client_factory=lambda host=host: host) for host in hosts)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                adapter._managed_client,
                managed_context,
                "sc-" + digit * 32,
            )
            for adapter, managed_context, digit in zip(adapters, contexts, ("8", "9"))
        ]
        assert [future.result(timeout=3) for future in futures] == list(hosts)

    assert sorted(ensured) == ["ALICE", "BOB"]
    assert not hasattr(deepseek_web, "acquire_shell_identity")
    assert all(not hasattr(adapter, "_shell_lease") for adapter in adapters)


def test_managed_identity_retries_only_transient_readiness_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def bind(**_kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise deepseek_web.DeepSeekWebError(
                "HARNESS_REGISTRY_STALE_WRITER", "snapshot raced"
            )
        return {}

    monkeypatch.setattr(deepseek_web, "bind_session_identity", bind)
    monkeypatch.setattr(deepseek_adapter.time, "sleep", sleeps.append)
    live = FakeHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)

    assert adapter._managed_client(
        context(tmp_path, live), "sc-" + "8" * 32
    ) is live
    adapter.close()

    assert attempts == [1, 2, 3]
    assert sleeps == [0.05, 0.05]
    assert sum(sleeps) < 5.0


def test_managed_identity_mismatch_refuses_immediately_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def refuse(**_kwargs):
        attempts.append(1)
        raise deepseek_web.DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_MISMATCH", "wrong exact shell"
        )

    monkeypatch.setattr(deepseek_web, "bind_session_identity", refuse)
    monkeypatch.setattr(deepseek_adapter.time, "sleep", sleeps.append)
    live = FakeHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)

    with pytest.raises(AdapterError) as denied:
        adapter._managed_client(context(tmp_path, live), "sc-" + "7" * 32)

    assert denied.value.code == "HARNESS_SHELL_IDENTITY_MISMATCH"
    assert attempts == [1]
    assert sleeps == []


def test_managed_authority_refusal_preserves_model_and_native_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[dict[str, object]] = []

    def refuse(**_kwargs):
        raise deepseek_web.DeepSeekWebError(
            "HARNESS_API_IDENTITY_MISMATCH", "managed API identity changed"
        )

    monkeypatch.setattr(deepseek_web, "bind_session_identity", refuse)
    monkeypatch.setattr(
        deepseek_web,
        "stop",
        lambda **kwargs: stopped.append(dict(kwargs)) or {"stopped": True},
    )
    live = FakeHost()
    ctx = context(tmp_path, FakeHost())
    adapter = DeepSeekAdapter(client_factory=lambda: live)

    with pytest.raises(AdapterError) as denied:
        adapter.start(ctx, "managed turn must fail closed")

    assert denied.value.code == "HARNESS_API_IDENTITY_MISMATCH"
    assert live.calls == []
    assert stopped == []

    route = selected_route(live)
    native_session = "native-unbound-session"
    created = live.call(
        "session.create", {"sessionId": native_session, "cwd": str(tmp_path)}
    )
    selected = live.call(
        "session.selectModel",
        {
            "sessionId": native_session,
            "provider": route.provider,
            "model": route.model,
            "reasoningEffort": "high",
        },
    )
    accepted = live.call(
        "session.prompt",
        {
            "sessionId": native_session,
            "mode": "queue",
            "content": [{"type": "text", "text": "native chat remains available"}],
        },
    )

    assert route.selector == "acme-dynamic/model-7"
    assert created == {"sessionId": native_session, "agentPreset": None}
    assert selected == {
        "selected": {
            "provider": "acme-dynamic",
            "model": "model-7",
            "reasoningEffort": "high",
        }
    }
    assert accepted == {"accepted": True}
    assert [method for method, _payload in live.calls][-3:] == [
        "session.create",
        "session.selectModel",
        "session.prompt",
    ]


def test_browser_readiness_exhaustion_becomes_alias_free_chat_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    retired: list[dict[str, object]] = []

    def unavailable(**_kwargs):
        attempts.append(1)
        raise deepseek_web.DeepSeekWebError(
            "HARNESS_PLUGIN_HEALTH_UNAVAILABLE", "health publication pending"
        )

    monkeypatch.setattr(deepseek_web, "bind_session_identity", unavailable)
    monkeypatch.setattr(deepseek_adapter.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        deepseek_web,
        "retire_session_identity",
        lambda **kwargs: retired.append(dict(kwargs)) or {"state": "terminal"},
    )
    live = FakeHost()
    ctx = context(tmp_path, live)
    adapter = DeepSeekAdapter(client_factory=lambda: live)

    turn = adapter.start(ctx, "chat without protected effects")
    try:
        assert attempts == [1, 1, 1]
        assert retired == [{
            "env": ctx.env,
            "root_session_id": turn.session_ref,
            "quiesced": True,
        }]
        assert turn.metadata["identity_degradation"] == {
            "mode": "chat-only",
            "reason": "HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
            "protected_effects": False,
        }
        assert turn.metadata["proof_authority"] is None
        assert [
            payload
            for method, payload in live.calls
            if method == "session.prompt"
        ] == [{
            "sessionId": turn.session_ref,
            "mode": "queue",
            "content": [{"type": "text", "text": "chat without protected effects"}],
        }]
    finally:
        adapter.close()


def test_sprint_readiness_exhaustion_refuses_only_pre_prompt_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    retired: list[dict[str, object]] = []

    def unavailable(**_kwargs):
        attempts.append(1)
        raise deepseek_web.DeepSeekWebError(
            "HARNESS_REGISTRY_UNAVAILABLE", "snapshot publication pending"
        )

    monkeypatch.setattr(deepseek_web, "bind_session_identity", unavailable)
    monkeypatch.setattr(deepseek_adapter.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        deepseek_web,
        "retire_session_identity",
        lambda **kwargs: retired.append(dict(kwargs)) or {"state": "terminal"},
    )
    live = FakeHost()
    ctx = context(tmp_path, live)
    ctx.env["SC_CONVERSATION_SURFACE"] = "sprint"
    calls_before = list(live.calls)
    adapter = DeepSeekAdapter(client_factory=lambda: live)

    with pytest.raises(AdapterError) as denied:
        adapter.start(ctx, "must not prompt")

    assert denied.value.code == "HARNESS_REGISTRY_UNAVAILABLE"
    assert attempts == [1, 1, 1]
    assert retired == []
    assert live.calls == calls_before


@pytest.mark.parametrize("mode", ["candidate", "promoted"])
def test_proof_managed_turn_uses_per_execution_binding_without_containment(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        deepseek_web,
        "ensure",
        lambda *_args, **_kwargs: events.append("host-proven") or {},
    )
    monkeypatch.setattr(
        deepseek_web,
        "preflight_candidate_execution",
        lambda **_kwargs: events.append("candidate-preflight") or {
            "mode": mode,
            "generation": 1,
            "root_session_id": "sc-" + "9" * 32,
            "plugin_contract_generation": "contract-one",
            "binding_snapshot_generation": 0,
            "binding_record_generation": None,
        },
    )
    monkeypatch.setattr(
        deepseek_web,
        "bind_session_identity",
        lambda **_kwargs: events.append("binding-proven") or {},
    )
    fake = FakeHost()
    adapter = DeepSeekAdapter(client_factory=lambda: fake)
    ctx = context(tmp_path, fake)
    session_ref = adapter._new_session_ref(ctx)
    client = adapter._managed_client(ctx, session_ref)

    assert client is fake
    assert events == [
        "host-proven",
        "candidate-preflight",
        "binding-proven",
    ]
    assert not hasattr(adapter, "_shell_lease")
    assert adapter._proof_authority == {
        "mode": mode,
        "generation": 1,
        "root_session_id": "sc-" + "9" * 32,
        "plugin_contract_generation": "contract-one",
        "binding_snapshot_generation": 0,
        "binding_record_generation": None,
    }


@pytest.mark.parametrize("surface", ["browser", "sprint"])
@pytest.mark.parametrize(
    "code",
    ["HARNESS_PROOF_CAPABILITY_REVOKED", "HARNESS_PROOF_CAPABILITY_STALE"],
)
def test_candidate_managed_reentry_revalidates_before_prompt_and_closes_unknown(
    surface: str,
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHost()
    ctx = context(tmp_path, fake)
    session_ref = DeepSeekAdapter._new_session_ref(ctx)
    retired: list[dict[str, object]] = []
    monkeypatch.setattr(
        deepseek_web,
        "preflight_candidate_execution",
        lambda **_kwargs: {
            "mode": "candidate",
            "generation": 1,
            "proof_run_id": "proof-managed",
            "root_session_id": session_ref,
            "plugin_contract_generation": "contract-one",
            "binding_snapshot_generation": 0,
            "binding_record_generation": None,
        },
    )
    monkeypatch.setattr(deepseek_web, "bind_session_identity", lambda **_kwargs: {})

    def refuse(**_kwargs):
        raise deepseek_web.DeepSeekWebError(code, "proof authority refused")

    monkeypatch.setattr(deepseek_web, "admit_candidate_execution", refuse)
    monkeypatch.setattr(
        deepseek_web,
        "retire_session_identity",
        lambda **kwargs: retired.append(dict(kwargs)) or {"state": "closing"},
    )
    adapter = DeepSeekAdapter(client_factory=lambda: fake)
    if surface == "sprint":
        fake.seed_session(session_ref, str(tmp_path))

    with pytest.raises(AdapterError) as denied:
        if surface == "browser":
            adapter.start(ctx, "work")
        else:
            adapter.resume(session_ref, ctx, "work")

    assert denied.value.code == code
    assert [method for method, _payload in fake.calls if method == "session.prompt"] == []
    assert retired == [{
        "env": ctx.env,
        "root_session_id": session_ref,
        "quiesced": False,
    }]


def test_candidate_recovered_binding_starts_with_quiescence_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeHost()
    ctx = context(tmp_path, fake)
    session_ref = DeepSeekAdapter._new_session_ref(ctx)
    retired: list[dict[str, object]] = []
    monkeypatch.setattr(
        deepseek_web,
        "preflight_candidate_execution",
        lambda **_kwargs: {
            "mode": "candidate",
            "generation": 1,
            "proof_run_id": "proof-recovery",
            "root_session_id": session_ref,
            "plugin_contract_generation": "contract-one",
            "binding_snapshot_generation": 4,
            "binding_record_generation": 7,
        },
    )
    monkeypatch.setattr(deepseek_web, "bind_session_identity", lambda **_kwargs: {})
    monkeypatch.setattr(
        deepseek_web,
        "retire_session_identity",
        lambda **kwargs: retired.append(dict(kwargs)) or {"state": "closing"},
    )
    adapter = DeepSeekAdapter(client_factory=lambda: fake)

    adapter._bind_execution_identity(ctx, session_ref)
    adapter.close()

    assert retired == [{
        "env": ctx.env,
        "root_session_id": session_ref,
        "quiesced": False,
    }]


@pytest.mark.parametrize("surface", ["browser", "sprint"])
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("stale", "HARNESS_PROOF_CAPABILITY_STALE"),
        ("expired", "HARNESS_PROOF_CAPABILITY_EXPIRED"),
        ("wrong-ref", "HARNESS_PROOF_CAPABILITY_MISMATCH"),
        ("wrong-generation", "HARNESS_PROOF_CAPABILITY_STALE"),
        ("wrong-root", "HARNESS_PROOF_ROOT_REFUSED"),
        ("partially-recovered", "HARNESS_PROOF_BINDING_MISMATCH"),
    ],
)
def test_candidate_browser_and_sprint_preflight_refusal_never_binds(
    surface: str,
    failure: str,
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent = {
        "snapshot": b"registry-before",
        "generation": 7,
        "credentials": (b"credential-before",),
        "lineage": ("child-before",),
    }
    before = copy.deepcopy(persistent)

    def refuse(**_kwargs):
        raise deepseek_web.DeepSeekWebError(
            code, f"{surface} {failure} proof refused"
        )

    def mutate_binding(**_kwargs):
        persistent["generation"] = 8
        persistent["credentials"] += (b"credential-after",)
        pytest.fail("candidate refusal reached binding mutation")

    monkeypatch.setattr(deepseek_web, "preflight_candidate_execution", refuse)
    monkeypatch.setattr(deepseek_web, "bind_session_identity", mutate_binding)
    fake = FakeHost()
    adapter = DeepSeekAdapter(client_factory=lambda: fake)
    with pytest.raises(AdapterError) as refused:
        adapter._managed_client(
            context(tmp_path, fake),
            "sc-" + ("b" if surface == "browser" else "c") * 32,
        )
    assert refused.value.code == code
    assert persistent == before


def test_candidate_one_shot_refuses_ambient_capability_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []
    monkeypatch.setattr(
        deepseek_web,
        "ensure",
        lambda *_args, **_kwargs: effects.append("ensure") or pytest.fail(
            "ambient proof authority reached Host readiness"
        ),
    )
    env = {
        "SC_API_TOKEN": "test-shell-token",
        "SC_API_BASE": "http://127.0.0.1:8837",
        "SC_SHELL_ID": "4",
        "SC_SHELL_SHORTNAME": "DEV4",
        "SC_SHELL_WORKTREE": str(tmp_path),
        "SC_DSH_PROOF_CAPABILITY_FILE": str(tmp_path / "capability.json"),
    }
    with (
        mock.patch.dict(deepseek_one_shot.os.environ, env, clear=True),
        pytest.raises(deepseek_host.DeepSeekHostError) as refused,
    ):
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")
    assert refused.value.code == "HARNESS_PROOF_RUNNER_REQUIRED"
    assert effects == []


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("stale", "HARNESS_PROOF_CAPABILITY_STALE"),
        ("expired", "HARNESS_PROOF_CAPABILITY_EXPIRED"),
        ("wrong-ref", "HARNESS_PROOF_CAPABILITY_MISMATCH"),
        ("wrong-generation", "HARNESS_PROOF_CAPABILITY_STALE"),
        ("wrong-root", "HARNESS_PROOF_ROOT_REFUSED"),
        ("partially-recovered", "HARNESS_PROOF_BINDING_MISMATCH"),
    ],
)
def test_candidate_one_shot_preflight_refusal_never_binds(
    failure: str,
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent = {
        "snapshot": b"registry-before",
        "generation": 11,
        "credentials": (b"credential-before",),
        "lineage": ("child-before",),
    }
    before = copy.deepcopy(persistent)

    def refuse(**_kwargs):
        raise deepseek_web.DeepSeekWebError(
            code, f"one-shot {failure} proof refused"
        )

    def mutate_binding(**_kwargs):
        persistent["generation"] = 12
        persistent["credentials"] += (b"credential-after",)
        pytest.fail("candidate refusal reached binding mutation")

    monkeypatch.setattr(
        deepseek_web,
        "proof_root_from_environment",
        lambda **_kwargs: "sc-" + "d" * 32,
    )
    monkeypatch.setattr(deepseek_web, "ensure", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(deepseek_web, "preflight_candidate_execution", refuse)
    monkeypatch.setattr(deepseek_web, "bind_session_identity", mutate_binding)
    env = {
        "SC_API_TOKEN": "test-shell-token",
        "SC_API_BASE": "http://127.0.0.1:8837",
        "SC_SHELL_ID": "4",
        "SC_SHELL_SHORTNAME": "DEV4",
        "SC_SHELL_WORKTREE": str(tmp_path),
        "SC_DSH_PROOF_CAPABILITY_FILE": str(tmp_path / "capability.json"),
        "SC_DSH_PROOF_ROOT_SESSION_ID": "sc-" + "d" * 32,
    }
    with (
        mock.patch.dict(deepseek_one_shot.os.environ, env, clear=True),
        pytest.raises(deepseek_host.DeepSeekHostError) as refused,
    ):
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")
    assert refused.value.code == code
    assert persistent == before


def test_start_reserves_the_deterministic_session_before_host_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    reservations: list[str] = []
    releases: list[str] = []

    class BarrierHost(FakeHost):
        def call(self, method, payload):
            if method == "session.create":
                # This is the first point at which the stock Host can publish
                # the chat to native Web.  Reservation must already exist.
                assert reservations == [session]
            return super().call(method, payload)

    monkeypatch.setattr(
        deepseek_web, "reserve_managed_session", lambda value: reservations.append(value)
    )
    monkeypatch.setattr(
        deepseek_web, "release_managed_session", lambda value: releases.append(value)
    )
    adapter = DeepSeekAdapter(client_factory=BarrierHost)
    turn = adapter.start(ctx, "work")
    assert turn.session_ref == session
    assert reservations == [session]
    adapter.close()
    assert releases == [session]


def test_start_reservation_blocks_native_prompt_after_session_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pause stock publication and prove only a distinct native chat proceeds."""
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "state.json").write_text(json.dumps({"service_port": 8942}))
    environment = {
        "SC_DEEPSEEK_WEB_STATE": str(runtime / "state.json"),
        "SC_DEEPSEEK_WEB_LOCK": str(runtime / "service.lock"),
    }
    browser_results: list[str] = []

    class PublicationBarrierHost(FakeHost):
        def call(self, method, payload):
            result = super().call(method, payload)
            if method == "session.create":
                # ``super`` has made the native chat observable.  Native Web
                # must still be rejected before a Host prompt can be sent.
                with pytest.raises(deepseek_web.DeepSeekWebError) as refused:
                    deepseek_web._record_browser_prompt(8942, session)
                browser_results.append(refused.value.code)
            return result

    async def distinct_browser_prompt() -> list[dict[str, object]]:
        forwarded: list[dict[str, object]] = []

        async def upstream_handler(reader, writer) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(
                line.split(b":", 1)[1].strip()
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ))
            payload = json.loads(await reader.readexactly(length))
            forwarded.append(payload)
            body = json.dumps({
                "type": "server-response",
                "rpcId": payload["rpcId"],
                "result": {"ok": True, "value": {"accepted": True}},
            }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        native_session = "session-550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({"rpcId": "native-distinct", "payload": {"sessionId": native_session}}).encode()
        try:
            with mock.patch.object(deepseek_web, "_history_boundary", return_value=0):
                reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
                try:
                    writer.write(
                        b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\n"
                        + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                        + b"\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                    )
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), timeout=1)
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert b'"accepted": true' in response
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()
        return forwarded

    with mock.patch.dict(deepseek_web.os.environ, environment, clear=False):
        deepseek_web._initialize_activity()
        monkeypatch.setattr(
            deepseek_web, "reserve_managed_session", REAL_RESERVE_MANAGED_SESSION
        )
        monkeypatch.setattr(
            deepseek_web, "release_managed_session", REAL_RELEASE_MANAGED_SESSION
        )
        live = PublicationBarrierHost()
        adapter = DeepSeekAdapter(client_factory=lambda: live)
        turn = adapter.start(ctx, "work")
        forwarded = asyncio.run(distinct_browser_prompt())
        adapter.close()

    assert turn.session_ref == session
    assert browser_results == ["HARNESS_WEB_SESSION_BUSY"]
    assert forwarded == [{
        "rpcId": "native-distinct",
        "payload": {
            "sessionId": "session-550e8400-e29b-41d4-a716-446655440000",
        },
    }]
    assert [payload for method, payload in live.calls if method == "session.prompt"] == [{
        "sessionId": session,
        "mode": "queue",
        "content": [{"type": "text", "text": "work"}],
    }]


def test_start_releases_early_reservation_when_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    reservations: list[str] = []
    releases: list[str] = []

    class RefusingHost(FakeHost):
        def call(self, method, payload):
            if method == "session.create":
                self.calls.append((method, dict(payload)))
                raise deepseek_host.HostRpcError(
                    "HARNESS_HOST_RPC_SESSION_REJECTED", "publication refused"
                )
            return super().call(method, payload)

    monkeypatch.setattr(
        deepseek_web, "reserve_managed_session", lambda value: reservations.append(value)
    )
    monkeypatch.setattr(
        deepseek_web, "release_managed_session", lambda value: releases.append(value)
    )
    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=RefusingHost).start(ctx, "work")

    assert refused.value.code == "HARNESS_HOST_RPC_SESSION_REJECTED"
    assert reservations == [session]
    assert releases == [session]


def test_cold_resume_reuses_session_and_prior_history(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(history=[
        {"seq": 0, "type": "permission/preset", "data": {
            "preset": "danger-full-access",
        }},
        {"seq": 1, "type": "sandbox/mode", "data": {
            "mode": "danger-full-access",
        }},
        {"seq": 2, "type": "approval/policy", "data": {"policy": "never"}},
        {"seq": 3, "type": "turn/end", "data": {
            "reason": {"kind": "completed"},
        }},
    ])
    live.seed_session(session, str(tmp_path))
    turn = DeepSeekAdapter(client_factory=lambda: live).resume(session, ctx, "continue")
    assert turn.session_ref == session
    assert ("session.create", {
        "workspaceId": "ws-1", "sessionId": session, "agentPreset": "standard",
    }) in live.calls
    assert "settings.update" not in [method for method, _payload in live.calls]
    assert [payload for method, payload in live.calls if method == "session.history"] == [
        {"sessionId": session, "maxMessages": 200},
    ]


def test_resume_missing_session_refuses_before_create_or_prompt(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost()

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).resume(session, ctx, "continue")

    assert refused.value.code == "HARNESS_SESSION_LOST"
    assert not any(method == "session.create" for method, _ in live.calls)
    assert not any(method == "session.prompt" for method, _ in live.calls)


def test_resume_archived_session_refuses_without_replacement(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost()
    live.seed_session(session, str(tmp_path))
    live.workspaces["ws-1"]["archivedSessionIds"].append(session)

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).resume(session, ctx, "continue")

    assert refused.value.code == "HARNESS_SESSION_ARCHIVED"
    assert live.sessions == {session: str(tmp_path)}
    assert not any(method == "session.create" for method, _ in live.calls)
    assert not any(method == "session.prompt" for method, _ in live.calls)


@pytest.mark.parametrize(
    "protected_effect",
    (
        "host_create",
        "host_prompt",
        "memory_write",
        "sprint_action",
        "message_send",
        "wake_enqueue",
    ),
)
@pytest.mark.parametrize(
    ("refusal", "expected_code"),
    (
        ("missing", "HARNESS_SESSION_LOST"),
        ("archived", "HARNESS_SESSION_ARCHIVED"),
        ("workspace_mismatch", "HARNESS_SESSION_WORKSPACE_MISMATCH"),
        ("shell_identity", "HARNESS_SHELL_IDENTITY_MISMATCH"),
    ),
)
def test_refusal_matrix_has_zero_independent_protected_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    expected_code: str,
    protected_effect: str,
) -> None:
    attempted = mock.Mock(name=protected_effect)

    class ProbedHost(FakeHost):
        def call(self, method, payload):
            if method == "session.create" and protected_effect == "host_create":
                attempted(dict(payload))
            if method == "session.prompt":
                if protected_effect == "host_prompt":
                    attempted(dict(payload))
                elif protected_effect not in {"host_create", "host_prompt"}:
                    # These are independent fake post-prompt authority effects,
                    # one per parametrized case.  A shared forwarding sentinel
                    # could conceal a path-specific mutation; this selected
                    # probe is attempted only if this refusal wrongly prompts.
                    attempted(dict(payload))
            return super().call(method, payload)

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = ProbedHost()
    operation = "resume"
    if refusal == "archived":
        live.seed_session(session, str(tmp_path))
        live.workspaces["ws-1"]["archivedSessionIds"].append(session)
    elif refusal == "workspace_mismatch":
        live.seed_session(session, str(tmp_path))
        live.workspaces["ws-1"]["sessionIds"] = []
        live.existing_session = True
        operation = "inspect"
    elif refusal == "shell_identity":
        operation = "start"

        def refuse_identity(*_args, **_kwargs):
            raise deepseek_web.DeepSeekWebError(
                "HARNESS_SHELL_IDENTITY_MISMATCH",
                "controlled shell mismatch",
            )

        monkeypatch.setattr(deepseek_web, "ensure", refuse_identity)

    adapter = DeepSeekAdapter(client_factory=lambda: live)
    try:
        with pytest.raises(AdapterError) as refused:
            if operation == "start":
                adapter.start(ctx, "must not act")
            elif operation == "inspect":
                adapter.inspect(session, ctx)
            else:
                adapter.resume(session, ctx, "must not act")
        assert refused.value.code == expected_code
        attempted.assert_not_called()
        assert [call for call in live.calls if call[0] == "session.prompt"] == []
    finally:
        adapter.close()


def test_managed_prompt_rechecks_archive_state_at_final_admission(
    tmp_path: Path,
) -> None:
    class ArchiveAtStreamHost(FakeHost):
        def open_events(self) -> FakeStream:
            session_id = next(iter(self.sessions))
            self.workspaces["ws-1"]["archivedSessionIds"].append(session_id)
            return super().open_events()

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = ArchiveAtStreamHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    try:
        with pytest.raises(AdapterError) as refused:
            adapter.start(ctx, "work")

        assert refused.value.code == "HARNESS_SESSION_ARCHIVED"
        assert len(live.streams) == 1
        assert live.streams[0].closed is True
        assert "session.prompt" not in [method for method, _ in live.calls]
    finally:
        adapter.close()


def test_recovery_refuses_detached_session_before_history_or_prompt(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(history=[{
        "seq": 3, "type": "turn/end", "data": {"reason": {"kind": "completed"}},
    }])
    live.seed_session(session, str(tmp_path))
    live.workspaces["ws-1"]["sessionIds"] = []

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).inspect(session, ctx)

    assert refused.value.code == "HARNESS_SESSION_WORKSPACE_MISMATCH"
    assert [method for method, _ in live.calls] == [
        "workspace.list", "session.list", "workspace.list",
    ]
    assert "session.history" not in [method for method, _ in live.calls]
    assert "session.prompt" not in [method for method, _ in live.calls]


def test_recovery_classifies_foreign_workspace_as_mismatch_before_history(
    tmp_path: Path,
) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    foreign = tmp_path.parent / "foreign"
    foreign.mkdir()
    live = FakeHost(history=[{
        "seq": 3, "type": "turn/end", "data": {"reason": {"kind": "completed"}},
    }])
    live.seed_session(session, str(foreign), workspace_id="ws-foreign")
    live.workspaces["ws-1"] = {
        "workspaceId": "ws-1", "path": str(tmp_path), "sessionIds": [],
        "archivedSessionIds": [],
    }

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).inspect(session, ctx)

    assert refused.value.code == "HARNESS_SESSION_WORKSPACE_MISMATCH"
    assert [method for method, _ in live.calls] == ["workspace.list", "session.list"]
    assert "session.history" not in [method for method, _ in live.calls]
    assert "session.prompt" not in [method for method, _ in live.calls]


def test_recovery_refuses_archived_session_before_history_or_prompt(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(history=[{
        "seq": 3, "type": "turn/end", "data": {"reason": {"kind": "completed"}},
    }])
    live.seed_session(session, str(tmp_path))
    live.workspaces["ws-1"]["archivedSessionIds"].append(session)

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).inspect(session, ctx)

    assert refused.value.code == "HARNESS_SESSION_ARCHIVED"
    assert [method for method, _ in live.calls] == [
        "workspace.list", "session.list", "workspace.list",
    ]
    assert "session.history" not in [method for method, _ in live.calls]
    assert "session.prompt" not in [method for method, _ in live.calls]


def test_running_recovery_reuses_one_client_and_delivers_interrupt(
    tmp_path: Path,
) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = FakeHost(history=[{
        "seq": 3, "type": "turn/start", "data": {},
    }])
    live.seed_session(session, str(tmp_path))
    clients: list[FakeHost] = []

    def client_factory() -> FakeHost:
        clients.append(live)
        return live

    adapter = DeepSeekAdapter(client_factory=client_factory)
    turn = NativeTurn(
        "deepseek",
        session,
        _run_ref(3),
        tmp_path,
        metadata={"recovered": True},
    )
    try:
        first = adapter.reconcile(turn, ctx)
        second = adapter.reconcile(turn, ctx)
        interrupted = adapter.interrupt(turn)
        live.history.append({
            "seq": 4,
            "type": "turn/end",
            "data": {"reason": {"kind": "cancelled"}},
        })
        terminal = adapter.reconcile(turn, ctx)

        assert first.outcome == "running"
        assert second.outcome == "running"
        assert clients == [live]
        assert turn.metadata["client"] is live
        assert interrupted.acknowledged is True
        assert interrupted.detail is None
        assert terminal.outcome == "cancelled"
        assert terminal.proven is True
        assert [
            payload for method, payload in live.calls if method == "session.cancel"
        ] == [{"sessionId": session}]
    finally:
        adapter.close()


def test_partial_workspace_attach_retries_the_same_exact_session(tmp_path: Path) -> None:
    class PartialAttachHost(FakeHost):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        def call(self, method, payload):
            if method == "session.create" and not self.failed_once:
                self.failed_once = True
                super().call(method, payload)
                raise deepseek_host.HostRpcError(
                    "HARNESS_HOST_RPC_WORKSPACE_ATTACH_FAILED", "published then detached"
                )
            return super().call(method, payload)

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    session = DeepSeekAdapter._new_session_ref(ctx)
    live = PartialAttachHost()

    turn = DeepSeekAdapter(client_factory=lambda: live).start(ctx, "work")

    creates = [payload for method, payload in live.calls if method == "session.create"]
    assert creates == [
        {"workspaceId": "ws-1", "sessionId": session, "agentPreset": "standard"},
        {"workspaceId": "ws-1", "sessionId": session, "agentPreset": "standard"},
    ]
    assert live.workspaces["ws-1"]["sessionIds"] == [session]
    assert turn.session_ref == session


def test_unattended_permission_history_mismatch_stops_before_prompt(
    tmp_path: Path,
) -> None:
    class MismatchedHost(FakeHost):
        def call(self, method, payload):
            result = super().call(method, payload)
            if method == "session.history":
                return {"events": [entry for entry in result["events"]
                                   if entry["event"]["type"] != "permission/preset"]}
            return result

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = MismatchedHost()

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).start(ctx, "work")

    assert refused.value.code == "HARNESS_PERMISSION_POLICY_UNAVAILABLE"
    assert "session.prompt" not in [method for method, _payload in live.calls]


def test_unattended_permission_settings_refusal_stops_before_session_create(
    tmp_path: Path,
) -> None:
    class RefusingHost(FakeHost):
        def call(self, method, payload):
            if method == "settings.update":
                self.calls.append((method, dict(payload)))
                raise deepseek_host.HostRpcError(
                    "HARNESS_HOST_RPC_SETTINGS_REJECTED",
                    "permission default rejected",
                )
            return super().call(method, payload)

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = RefusingHost()

    with pytest.raises(AdapterError) as refused:
        DeepSeekAdapter(client_factory=lambda: live).start(ctx, "work")

    assert refused.value.code == "HARNESS_PERMISSION_POLICY_UNAVAILABLE"
    assert live.calls[-1] == ("settings.update", {
        "ns": "permission",
        "patch": {"defaultPreset": "danger-full-access"},
    })
    assert "session.create" not in [method for method, _payload in live.calls]
    assert "session.prompt" not in [method for method, _payload in live.calls]


def test_stream_loss_never_invents_success(tmp_path: Path) -> None:
    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = FakeHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    events = list(adapter.stream(adapter.start(ctx, "work")))
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"] == "HARNESS_RECONCILIATION_UNKNOWN"


def test_stream_loss_cancels_proven_running_turn_before_failure(
    tmp_path: Path,
) -> None:
    class RunningHost(FakeHost):
        def call(self, method, payload):
            result = super().call(method, payload)
            if method == "session.prompt" and payload.get("content") == [{
                "type": "text", "text": "work",
            }]:
                self.history.append({
                    "seq": len(self.history), "type": "turn/start", "data": {},
                })
            return result

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = RunningHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    turn = adapter.start(ctx, "work")

    events = list(adapter.stream(turn))

    assert [event.type for event in events] == ["session.started", "run.failed"]
    assert events[-1].payload["status"] == "failed"
    assert events[-1].payload["error"] == "HARNESS_HOST_STREAM_LOST"
    assert [
        payload for method, payload in live.calls if method == "session.cancel"
    ] == [{"sessionId": turn.session_ref}]
    assert live.streams[0].closed is True


def test_stream_loss_without_acknowledged_cancel_emits_no_false_terminal(
    tmp_path: Path,
) -> None:
    class RunningHost(FakeHost):
        def call(self, method, payload):
            result = super().call(method, payload)
            if method == "session.prompt" and payload.get("content") == [{
                "type": "text", "text": "work",
            }]:
                self.history.append({
                    "seq": len(self.history), "type": "turn/start", "data": {},
                })
            if method == "session.cancel":
                return {"accepted": False}
            return result

    seed = FakeHost()
    ctx = context(tmp_path, seed)
    live = RunningHost()
    adapter = DeepSeekAdapter(client_factory=lambda: live)
    turn = adapter.start(ctx, "work")
    produced = []

    with pytest.raises(AdapterError) as refused:
        for event in adapter.stream(turn):
            produced.append(event)

    assert refused.value.code == "HARNESS_HOST_STREAM_LOST"
    assert [event.type for event in produced] == ["session.started"]
    assert turn.metadata.get("terminal") is None
    assert [
        payload for method, payload in live.calls if method == "session.cancel"
    ] == [{"sessionId": turn.session_ref}]
    assert live.streams[0].closed is True


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
    monkeypatch.setenv("SC_API_TOKEN", "test-shell-token")
    monkeypatch.setenv("SC_API_BASE", "http://127.0.0.1:8837")
    monkeypatch.setenv("SC_SHELL_ID", "4")
    monkeypatch.setenv("SC_SHELL_SHORTNAME", "DEV4")
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
    monkeypatch.setenv("SC_API_TOKEN", "test-shell-token")
    monkeypatch.setenv("SC_API_BASE", "http://127.0.0.1:8837")
    monkeypatch.setenv("SC_SHELL_ID", "4")
    monkeypatch.setenv("SC_SHELL_SHORTNAME", "DEV4")
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_host, "DeepSeekHostClient", lambda: fake)

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")

    assert refused.value.code == "HARNESS_ROUTE_MISMATCH"
    assert "session.prompt" not in [method for method, _payload in fake.calls]


def test_one_shot_without_canonical_identity_refuses_before_host_access(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeHost()
    for name in ("SC_API_TOKEN", "SC_API_BASE", "SC_SHELL_ID", "SC_SHELL_SHORTNAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_host, "DeepSeekHostClient", lambda: fake)

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")

    assert refused.value.code == "HARNESS_SHELL_IDENTITY_UNAVAILABLE"
    assert fake.calls == []


def test_one_shot_retries_transient_readiness_then_runs_only_that_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    invocations: list[dict[str, object]] = []
    retirements: list[dict[str, object]] = []

    def ensure(*_args, **_kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise deepseek_web.DeepSeekWebError(
                "HARNESS_PLUGIN_HEALTH_UNAVAILABLE", "Host is publishing health"
            )
        return {}

    monkeypatch.setenv("SC_API_TOKEN", "test-shell-token")
    monkeypatch.setenv("SC_API_BASE", "http://127.0.0.1:8837")
    monkeypatch.setenv("SC_SHELL_ID", "4")
    monkeypatch.setenv("SC_SHELL_SHORTNAME", "DEV4")
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_web, "ensure", ensure)
    monkeypatch.setattr(deepseek_one_shot.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        deepseek_one_shot,
        "_run",
        lambda selector, effort, prompt, **kwargs: invocations.append({
            "selector": selector,
            "effort": effort,
            "prompt": prompt,
            **kwargs,
        }) or 0,
    )
    monkeypatch.setattr(
        deepseek_web,
        "retire_session_identity",
        lambda **kwargs: retirements.append(dict(kwargs)) or {"state": "terminal"},
    )

    assert deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt") == 0

    assert attempts == [1, 2, 3]
    assert sleeps == [0.05, 0.05]
    assert len(invocations) == 1
    assert invocations[0]["prompt"] == "prompt"
    assert invocations[0]["worktree"] == tmp_path
    assert retirements == [{
        "env": deepseek_one_shot.os.environ,
        "root_session_id": invocations[0]["session_ref"],
        "quiesced": True,
    }]


def test_one_shot_readiness_exhaustion_fails_only_that_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    invoked = mock.Mock()
    retirements: list[dict[str, object]] = []

    def unavailable(*_args, **_kwargs):
        attempts.append(1)
        raise deepseek_web.DeepSeekWebError(
            "HARNESS_HOST_UNAVAILABLE", "Host is still restarting"
        )

    monkeypatch.setenv("SC_API_TOKEN", "test-shell-token")
    monkeypatch.setenv("SC_API_BASE", "http://127.0.0.1:8837")
    monkeypatch.setenv("SC_SHELL_ID", "4")
    monkeypatch.setenv("SC_SHELL_SHORTNAME", "DEV4")
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_web, "ensure", unavailable)
    monkeypatch.setattr(deepseek_one_shot.time, "sleep", sleeps.append)
    monkeypatch.setattr(deepseek_one_shot, "_run", invoked)
    monkeypatch.setattr(
        deepseek_web,
        "retire_session_identity",
        lambda **kwargs: retirements.append(dict(kwargs)) or {"state": "terminal"},
    )

    with pytest.raises(deepseek_host.DeepSeekHostError) as denied:
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")

    assert denied.value.code == "HARNESS_HOST_UNAVAILABLE"
    assert attempts == [1, 1, 1]
    assert sleeps == [0.05, 0.05]
    invoked.assert_not_called()
    assert retirements == []


def test_one_shot_authority_mismatch_refuses_without_readiness_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    invoked = mock.Mock()

    def refuse(**_kwargs):
        attempts.append(1)
        raise deepseek_web.DeepSeekWebError(
            "HARNESS_API_IDENTITY_MISMATCH", "API identity changed"
        )

    monkeypatch.setenv("SC_API_TOKEN", "test-shell-token")
    monkeypatch.setenv("SC_API_BASE", "http://127.0.0.1:8837")
    monkeypatch.setenv("SC_SHELL_ID", "4")
    monkeypatch.setenv("SC_SHELL_SHORTNAME", "DEV4")
    monkeypatch.setenv("SC_SHELL_WORKTREE", str(tmp_path))
    monkeypatch.setattr(deepseek_web, "bind_session_identity", refuse)
    monkeypatch.setattr(deepseek_one_shot.time, "sleep", sleeps.append)
    monkeypatch.setattr(deepseek_one_shot, "_run", invoked)

    with pytest.raises(deepseek_host.DeepSeekHostError) as denied:
        deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")

    assert denied.value.code == "HARNESS_API_IDENTITY_MISMATCH"
    assert attempts == [1]
    assert sleeps == []
    invoked.assert_not_called()


def test_one_shot_uncertain_prompt_creates_no_global_authority_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UncertainHost(FakeHost):
        def call(self, method, payload):
            if method == "session.prompt":
                self.calls.append((method, dict(payload)))
                raise deepseek_host.DeepSeekHostError(
                    "HARNESS_HOST_UNAVAILABLE", "prompt acknowledgement lost"
                )
            if method == "session.cancel":
                self.calls.append((method, dict(payload)))
                return {"accepted": False}
            return super().call(method, payload)

    fake = UncertainHost()
    monkeypatch.setattr(deepseek_host, "DeepSeekHostClient", lambda: fake)
    state = tmp_path / "deepseek-web-state.json"
    with (
        mock.patch.dict(
            deepseek_one_shot.os.environ,
            {
                "SC_API_TOKEN": "one-shot-token",
                "SC_API_BASE": "http://127.0.0.1:8837",
                "SC_SHELL_ID": "4",
                "SC_SHELL_SHORTNAME": "DEV4",
                "SC_DEEPSEEK_WEB_STATE": str(state),
            },
            clear=False,
        ),
        pytest.raises(deepseek_host.DeepSeekHostError) as refused,
    ):
        deepseek_one_shot._run(
            "acme-dynamic/model-7", "high", "prompt", worktree=tmp_path
        )

    assert refused.value.code == "HARNESS_ONE_SHOT_BUSY"
    prompts = [payload for method, payload in fake.calls if method == "session.prompt"]
    cancels = [payload for method, payload in fake.calls if method == "session.cancel"]
    assert len(prompts) == 1
    assert cancels == [{"sessionId": prompts[0]["sessionId"]}]
    assert not state.with_name("deepseek-shell-identity-unproven.json").exists()
    assert not hasattr(deepseek_web, "mark_unproven_execution")
