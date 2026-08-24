"""Stock DeepSeek Host projection, one-shot, and Browser lifecycle contracts."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
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
from conversation_adapters.base import AdapterError, ConversationContext  # noqa: E402
from conversation_adapters.deepseek import DeepSeekAdapter  # noqa: E402

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
    class Lease:
        def close(self) -> None:
            return None

    monkeypatch.setattr(deepseek_web, "acquire_shell_identity", lambda **_kwargs: Lease())
    monkeypatch.setattr(deepseek_web, "ensure", lambda *_args, **_kwargs: {})
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


def test_one_shot_uncertain_prompt_fences_future_handoff_before_lease_release(
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
    fenced: list[str] = []
    monkeypatch.setattr(deepseek_host, "DeepSeekHostClient", lambda: fake)
    monkeypatch.setattr(
        deepseek_web,
        "mark_unproven_execution",
        lambda _env, session: fenced.append(session),
    )

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_one_shot._run("acme-dynamic/model-7", "high", "prompt", worktree=tmp_path)

    assert refused.value.code == "HARNESS_ONE_SHOT_BUSY"
    prompts = [payload for method, payload in fake.calls if method == "session.prompt"]
    cancels = [payload for method, payload in fake.calls if method == "session.cancel"]
    assert len(prompts) == 1
    assert cancels == [{"sessionId": prompts[0]["sessionId"]}]
    assert fenced == [prompts[0]["sessionId"]]
