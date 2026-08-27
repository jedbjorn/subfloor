"""Standalone stock DeepSeek Host runtime contracts retained for WU125."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import sys
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api")]

import deepseek_host  # noqa: E402
import harness_versions  # noqa: E402
from conversation_adapters.deepseek import (  # noqa: E402
    DeepSeekAdapter,
)
from conversation_adapters.base import AdapterError  # noqa: E402



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
    generation = "a" * 64

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
            "cookie": request.get_header("Cookie"),
            "timeout": timeout,
        })
        return Response()

    monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", "6509")
    monkeypatch.setattr(
        deepseek_host.ports,
        "resolve",
        mock.Mock(side_effect=AssertionError("private Host port consulted")),
    )
    monkeypatch.setattr(
        deepseek_host.uuid,
        "uuid4",
        lambda: "00000000-0000-0000-0000-000000000007",
    )
    client = deepseek_host.DeepSeekHostClient(
        opener=opener,
        authority_provider=lambda: {
            "endpoint": "http://127.0.0.1:6500",
            "generation": generation,
        },
    )

    assert client.call("host.describe", {}) == {"version": "0.1.1-rc.2"}
    assert captured == {
        "url": "http://127.0.0.1:6500/api/host.describe",
        "body": {
            "type": "client-request",
            "rpcId": "00000000-0000-0000-0000-000000000007",
            "method": "host.describe",
            "payload": {},
        },
        "cookie": f"sc_deepseek_managed_generation={generation}",
        "timeout": 15.0,
    }


def test_unary_client_refreshes_exact_stale_managed_generation(monkeypatch) -> None:
    generations = iter(("a" * 64, "b" * 64))
    authority_calls = []
    requests = []
    monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", "6509")
    resolve = mock.Mock(side_effect=AssertionError("private Host port consulted"))
    monkeypatch.setattr(deepseek_host.ports, "resolve", resolve)

    def authority():
        generation = next(generations)
        authority_calls.append(generation)
        return {
            "endpoint": "http://127.0.0.1:6500",
            "generation": generation,
        }

    class Response:
        def __init__(self, request):
            self.request = request

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            rpc_id = json.loads(self.request.data)["rpcId"]
            return json.dumps({
                "type": "server-response",
                "rpcId": rpc_id,
                "result": {"ok": True, "value": {"version": "0.1.1-rc.2"}},
            }).encode()

    def opener(request, *, timeout):
        requests.append((
            request.full_url,
            request.get_header("Cookie"),
            json.loads(request.data)["method"],
            timeout,
        ))
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                409,
                "Conflict",
                {},
                io.BytesIO(b'{"error":"HARNESS_WEB_GENERATION_STALE"}'),
            )
        return Response(request)

    client = deepseek_host.DeepSeekHostClient(
        opener=opener, authority_provider=authority
    )

    assert client.call("host.describe", {}) == {"version": "0.1.1-rc.2"}
    assert authority_calls == ["a" * 64, "b" * 64]
    assert requests == [
        (
            "http://127.0.0.1:6500/api/host.describe",
            f"sc_deepseek_managed_generation={'a' * 64}",
            "host.describe",
            15.0,
        ),
        (
            "http://127.0.0.1:6500/api/host.describe",
            f"sc_deepseek_managed_generation={'b' * 64}",
            "host.describe",
            15.0,
        ),
    ]
    assert resolve.call_count == 0


def test_event_client_refreshes_exact_stale_managed_generation() -> None:
    generations = iter(("a" * 64, "b" * 64))
    authority_calls = []

    def authority():
        generation = next(generations)
        authority_calls.append(generation)
        return {
            "endpoint": "http://127.0.0.1:6500",
            "generation": generation,
        }

    class StaleHandshake(Exception):
        response = type("Response", (), {
            "status_code": 409,
            "body": b'{"error":"HARNESS_WEB_GENERATION_STALE"}',
        })()

    class Socket:
        closed = False

        def close(self):
            self.closed = True

    socket = Socket()
    with mock.patch(
        "websockets.sync.client.connect",
        side_effect=(StaleHandshake(), socket),
    ) as connect:
        client = deepseek_host.DeepSeekHostClient(
            authority_provider=authority
        )
        stream = client.open_events()
        stream.close()

    assert authority_calls == ["a" * 64, "b" * 64]
    assert [call.args[0] for call in connect.call_args_list] == [
        "ws://127.0.0.1:6500/api/events.mux",
        "ws://127.0.0.1:6500/api/events.mux",
    ]
    assert [
        call.kwargs["additional_headers"]["Cookie"]
        for call in connect.call_args_list
    ] == [
        f"sc_deepseek_managed_generation={'a' * 64}",
        f"sc_deepseek_managed_generation={'b' * 64}",
    ]
    assert socket.closed is True


@pytest.mark.parametrize(
    ("authority", "code"),
    [
        (None, "HARNESS_HOST_UNAVAILABLE"),
        ({}, "HARNESS_HOST_UNAVAILABLE"),
        ({"endpoint": "http://127.0.0.1:6500"}, "HARNESS_HOST_UNAVAILABLE"),
        (
            {
                "endpoint": "http://127.0.0.1:6500",
                "generation": "not-current-authority",
            },
            "HARNESS_WEB_GENERATION_STALE",
        ),
        (
            {"endpoint": "http://127.0.0.2:6500", "generation": "a" * 64},
            "HARNESS_HOST_UNAVAILABLE",
        ),
    ],
)
def test_default_client_rejects_missing_or_malformed_managed_authority(
    authority, code, monkeypatch
) -> None:
    opened = mock.Mock(side_effect=AssertionError("Host request attempted"))
    monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", "6509")
    monkeypatch.setattr(
        deepseek_host.ports,
        "resolve",
        mock.Mock(side_effect=AssertionError("private Host port consulted")),
    )

    with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
        deepseek_host.DeepSeekHostClient(
            opener=opened, authority_provider=lambda: authority
        )

    assert refused.value.code == code
    assert opened.call_count == 0


def test_concurrent_default_clients_share_managed_authority_without_ambient_port(
    monkeypatch,
) -> None:
    generation = "c" * 64
    observed = []
    observed_lock = threading.Lock()
    monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", "6509")
    resolve = mock.Mock(side_effect=AssertionError("private Host port consulted"))
    monkeypatch.setattr(deepseek_host.ports, "resolve", resolve)

    class Response:
        def __init__(self, request):
            self.request = request

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            body = json.loads(self.request.data)
            return json.dumps({
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": {"version": "0.1.1-rc.2"}},
            }).encode()

    def opener(request, *, timeout):
        with observed_lock:
            observed.append({
                "url": request.full_url,
                "cookie": request.get_header("Cookie"),
                "method": json.loads(request.data)["method"],
                "timeout": timeout,
            })
        return Response(request)

    def attach(_index):
        client = deepseek_host.DeepSeekHostClient(
            opener=opener,
            authority_provider=lambda: {
                "endpoint": "http://127.0.0.1:6500",
                "generation": generation,
            },
        )
        return client.call("host.describe", {})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attach, range(24)))

    assert results == [{"version": "0.1.1-rc.2"}] * 24
    assert observed == [
        {
            "url": "http://127.0.0.1:6500/api/host.describe",
            "cookie": f"sc_deepseek_managed_generation={generation}",
            "method": "host.describe",
            "timeout": 15.0,
        }
    ] * 24
    assert resolve.call_count == 0

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
