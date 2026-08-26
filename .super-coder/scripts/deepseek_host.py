#!/usr/bin/env python3
"""Thin client for the stock DeepSeek Harness loopback Host API."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol

import ports

ENGINE = Path(__file__).resolve().parents[1]
ADAPTER = ENGINE / "adapters" / "deepseek" / "adapter.json"
TRANSPORT_CONTRACT = "deepseek-stock-host-v1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_MODELS = 2_000
MAX_PROVIDERS = 256
MAX_MODEL_GROUPS = 256
MAX_SETTINGS_NAMESPACES = 512
MAX_REASONING_OPTIONS = 256
MAX_IDENTIFIER_CHARS = 512
SAFE_CREDENTIAL_REF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")
SAFE_CREDENTIAL_SOURCE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SENSITIVE = re.compile(
    r"(?:authorization|bearer|api[-_]?key|token|secret|password|credential)",
    re.I,
)


class DeepSeekHostError(RuntimeError):
    """Stable, redacted Host integration failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = _redact_text(detail)
        super().__init__(f"{code}: {self.detail}")


class HostRpcError(DeepSeekHostError):
    """Business error returned by the official Host RPC envelope."""


class HostEventStream(Protocol):
    def __iter__(self) -> Iterator[Mapping[str, Any]]: ...

    def close(self) -> None: ...


class HostTransport(Protocol):
    def call(self, method: str, payload: Mapping[str, Any]) -> Any: ...

    def open_events(self) -> HostEventStream: ...


def _redact_text(value: object, *, limit: int = 1_024) -> str:
    text = str(value).replace("\x00", "")
    text = re.sub(
        r"(?i)(authorization|bearer|api[-_]?key|token|secret|password|credential)"
        r"(?:\s*[:=]\s*|\s+)[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:limit]


def _exact_string(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_CHARS
    ):
        raise DeepSeekHostError("HARNESS_HOST_RESPONSE_INVALID", f"invalid {field}")
    return value


def _credential_free_endpoint(value: object, provider: str) -> str:
    if value is None:
        return f"dsh-provider:{provider}"
    endpoint = _exact_string(value, "provider endpoint")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID",
            f"provider {provider} returned a non-public endpoint identity",
        )
    return endpoint.rstrip("/")


def checked_host_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeepSeekHostError(
            "HARNESS_HOST_ENDPOINT_INVALID",
            "DeepSeek Host must be one exact http://127.0.0.1:<port> endpoint",
        )
    return f"http://127.0.0.1:{port}"


def configured_host_url(env: Mapping[str, str] = os.environ) -> str:
    value = env.get("SC_DEEPSEEK_HOST_PORT")
    if value is None:
        try:
            value = str(ports.resolve(persist=False)["deepseek_host_port"])
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DeepSeekHostError(
                "HARNESS_HOST_UNAVAILABLE",
                "managed DeepSeek Host port is unavailable",
            ) from exc
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,4}", value) is None:
        raise DeepSeekHostError(
            "HARNESS_HOST_UNAVAILABLE",
            "SC_DEEPSEEK_HOST_PORT is missing or invalid",
        )
    port = int(value)
    if port > 65_535:
        raise DeepSeekHostError(
            "HARNESS_HOST_UNAVAILABLE",
            "SC_DEEPSEEK_HOST_PORT is missing or invalid",
        )
    return f"http://127.0.0.1:{port}"


class _WebSocketStream:
    def __init__(self, socket: Any, *, timeout: float) -> None:
        self.socket = socket
        self.timeout = timeout

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        while True:
            try:
                raw = self.socket.recv(timeout=self.timeout)
            except TimeoutError as exc:
                raise DeepSeekHostError(
                    "HARNESS_HOST_STREAM_TIMEOUT",
                    "DeepSeek Host event stream became inactive",
                ) from exc
            except Exception as exc:
                name = type(exc).__name__
                if name in {"ConnectionClosedOK", "ConnectionClosed"}:
                    return
                raise DeepSeekHostError(
                    "HARNESS_HOST_STREAM_LOST",
                    f"DeepSeek Host event stream failed: {name}",
                ) from exc
            if raw is None:
                return
            if not isinstance(raw, str):
                raise DeepSeekHostError(
                    "HARNESS_HOST_STREAM_INVALID",
                    "DeepSeek Host emitted a binary event frame",
                )
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DeepSeekHostError(
                    "HARNESS_HOST_STREAM_INVALID",
                    "DeepSeek Host emitted malformed JSON",
                ) from exc
            if (
                not isinstance(envelope, dict)
                or envelope.get("type") != "server-request"
                or not isinstance(envelope.get("rpcId"), str)
                or not isinstance(envelope.get("payload"), dict)
            ):
                raise DeepSeekHostError(
                    "HARNESS_HOST_STREAM_INVALID",
                    "DeepSeek Host emitted an invalid server-request envelope",
                )
            yield envelope

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


class DeepSeekHostClient:
    """Official unary HTTP plus multiplexed WebSocket event transport."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        stream_timeout: float = 300.0,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.endpoint = configured_host_url()
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self._opener = opener

    def call(self, method: str, payload: Mapping[str, Any]) -> Any:
        method = _exact_string(method, "Host method")
        if re.fullmatch(r"[A-Za-z0-9_$.-]+", method) is None:
            raise DeepSeekHostError(
                "HARNESS_HOST_REQUEST_INVALID", "invalid Host method"
            )
        rpc_id = str(uuid.uuid4())
        body = json.dumps(
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": method,
                "payload": dict(payload),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/api/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DeepSeekHostError(
                "HARNESS_HOST_UNAVAILABLE",
                f"DeepSeek Host request failed: {type(exc).__name__}",
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID",
                "DeepSeek Host response exceeded the safety bound",
            )
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID",
                "DeepSeek Host returned malformed JSON",
            ) from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("type") != "server-response"
            or envelope.get("rpcId") != rpc_id
            or not isinstance(envelope.get("result"), dict)
        ):
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID",
                "DeepSeek Host returned an invalid response envelope",
            )
        result = envelope["result"]
        if result.get("ok") is True and "value" in result:
            return result["value"]
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise HostRpcError(
            f"HARNESS_HOST_RPC_{str(code or 'UNKNOWN').upper().replace('-', '_')}",
            message if isinstance(message, str) else "DeepSeek Host request failed",
        )

    def open_events(self) -> HostEventStream:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise DeepSeekHostError(
                "HARNESS_HOST_CLIENT_UNAVAILABLE",
                "Python websockets support is unavailable",
            ) from exc
        url = self.endpoint.replace("http://", "ws://", 1) + "/api/events.mux"
        try:
            socket = connect(
                url,
                open_timeout=self.timeout,
                close_timeout=2,
                max_size=MAX_RESPONSE_BYTES,
            )
        except Exception as exc:
            raise DeepSeekHostError(
                "HARNESS_HOST_UNAVAILABLE",
                f"DeepSeek Host event connection failed: {type(exc).__name__}",
            ) from exc
        return _WebSocketStream(socket, timeout=self.stream_timeout)


@dataclass(frozen=True)
class ConfiguredRoute:
    selector: str
    provider: str
    model: str
    name: str
    endpoint_identity: str
    credential_ref: str | None
    credential_status: Mapping[str, Any] | None
    reasoning_efforts: tuple[str, ...]
    default_effort: str | None
    runtime_version: str
    source_commit: str
    configuration_digest: str

    def binding_metadata(self, effort: str) -> dict[str, Any]:
        return {
            "provider_route": self.provider,
            "endpoint_identity": self.endpoint_identity,
            "credential_ref": self.credential_ref,
            "credential_status": (
                dict(self.credential_status)
                if self.credential_status is not None
                else None
            ),
            "configuration_digest": self.configuration_digest,
            "transport_contract": TRANSPORT_CONTRACT,
            "reasoning_effort": None if effort == "default" else effort,
            "runtime_version": self.runtime_version,
            "source_commit": self.source_commit,
        }


def _manifest_identity() -> tuple[str, str]:
    try:
        manifest = json.loads(ADAPTER.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepSeekHostError(
            "HARNESS_MANIFEST_INVALID", "DeepSeek adapter manifest is unavailable"
        ) from exc
    official = manifest.get("official_runtime") or {}
    version = _exact_string(official.get("version"), "official runtime version")
    commit = official.get("commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise DeepSeekHostError(
            "HARNESS_MANIFEST_INVALID", "official runtime commit is invalid"
        )
    return version, commit


def _at_path(value: object, path: Iterable[object]) -> Mapping[str, Any]:
    current = value
    for segment in path:
        if not isinstance(segment, str) or not isinstance(current, Mapping):
            return {}
        current = current.get(segment)
    return current if isinstance(current, Mapping) else {}


def _credential_status(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID", "credential descriptor is invalid"
        )
    allowed = {"configured", "source", "writable"}
    if set(value) - allowed or not isinstance(value.get("configured"), bool) or not isinstance(value.get("writable"), bool):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID", "credential descriptor is not value-free"
        )
    source = value.get("source")
    if (
        source is not None
        and (
            not isinstance(source, str)
            or SAFE_CREDENTIAL_SOURCE.fullmatch(source) is None
        )
    ):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID", "credential source is invalid"
        )
    return {
        "configured": value["configured"],
        **({"source": source} if source is not None else {}),
        "writable": value["writable"],
    }


def configured_routes(
    client: HostTransport,
    *,
    selector: str | None = None,
) -> list[ConfiguredRoute]:
    """Project configured routes only from public, redacted Host RPC methods."""
    described = client.call("host.describe", {})
    if not isinstance(described, Mapping):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID", "configuration projection is malformed"
        )
    host_version = described.get("version")
    if not isinstance(host_version, str) or not host_version:
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID",
            "DeepSeek Host descriptor version is invalid",
        )
    providers_value = client.call("llm.providers", {})
    models_value = client.call("llm.models", {})
    settings_value = client.call("settings.describe", {})
    if not all(
        isinstance(item, Mapping)
        for item in (providers_value, models_value, settings_value)
    ):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID", "configuration projection is malformed"
        )
    runtime_version, source_commit = _manifest_identity()
    provider_rows = providers_value.get("providers")
    model_groups = models_value.get("groups")
    namespaces = settings_value.get("namespaces")
    if not isinstance(provider_rows, list) or not isinstance(model_groups, list) or not isinstance(namespaces, list):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID", "configuration projection lists are malformed"
        )
    if (
        len(provider_rows) > MAX_PROVIDERS
        or len(model_groups) > MAX_MODEL_GROUPS
        or len(namespaces) > MAX_SETTINGS_NAMESPACES
    ):
        raise DeepSeekHostError(
            "HARNESS_HOST_RESPONSE_INVALID",
            "configuration projection exceeds safety limits",
        )
    provider_map: dict[str, Mapping[str, Any]] = {}
    for row in provider_rows:
        if not isinstance(row, Mapping) or row.get("active") is not True:
            continue
        provider = _exact_string(row.get("provider"), "provider id")
        if provider in provider_map:
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID", "duplicate active provider id"
            )
        provider_map[provider] = row
    namespace_map = {
        row.get("ns"): row
        for row in namespaces
        if isinstance(row, Mapping) and isinstance(row.get("ns"), str)
    }
    profiles: dict[str, tuple[Mapping[str, Any], str, str | None]] = {}
    credential_refs: set[str] = set()
    for provider, row in provider_map.items():
        ns = row.get("settingsNs")
        path = row.get("settingsPath")
        descriptor = namespace_map.get(ns)
        if (
            not isinstance(ns, str)
            or not ns
            or not isinstance(path, list)
            or not isinstance(descriptor, Mapping)
        ):
            continue
        profile = _at_path(descriptor.get("value"), path)
        endpoint = _credential_free_endpoint(profile.get("baseURL"), provider)
        credential_ref = profile.get("apiKeyEnv")
        if credential_ref is not None:
            if not isinstance(credential_ref, str) or SAFE_CREDENTIAL_REF.fullmatch(credential_ref) is None:
                raise DeepSeekHostError(
                    "HARNESS_HOST_RESPONSE_INVALID",
                    f"provider {provider} has an invalid credential reference",
                )
            credential_refs.add(credential_ref)
        profiles[provider] = (profile, endpoint, credential_ref)
    credential_map: dict[str, Any] = {}
    if credential_refs:
        refs = sorted(credential_refs)
        for offset in range(0, len(refs), 64):
            described_credentials = client.call(
                "credentials.describe", {"refs": refs[offset:offset + 64]}
            )
            if (
                not isinstance(described_credentials, Mapping)
                or not isinstance(
                    described_credentials.get("credentials"), Mapping
                )
            ):
                raise DeepSeekHostError(
                    "HARNESS_HOST_RESPONSE_INVALID",
                    "credential projection is malformed",
                )
            credential_map.update(described_credentials["credentials"])
    routes: list[ConfiguredRoute] = []
    seen: set[str] = set()
    model_rows_seen = 0
    for group in model_groups:
        if not isinstance(group, Mapping):
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID", "model group is malformed"
            )
        provider = _exact_string(group.get("id"), "model provider id")
        if provider not in provider_map:
            continue
        models = group.get("models")
        if not isinstance(models, list):
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID", f"provider {provider} model list is invalid"
            )
        model_rows_seen += len(models)
        if model_rows_seen > MAX_MODELS:
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID", f"provider {provider} model list is invalid"
            )
        route_profile = profiles.get(provider)
        if route_profile is None:
            raise DeepSeekHostError(
                "HARNESS_HOST_RESPONSE_INVALID",
                f"provider {provider} has no usable settings profile",
            )
        _profile, endpoint, credential_ref = route_profile
        status = None
        if credential_ref is not None:
            status = _credential_status(credential_map.get(credential_ref))
            if status["configured"] is not True:
                continue
        for model_row in models:
            if not isinstance(model_row, Mapping):
                raise DeepSeekHostError(
                    "HARNESS_HOST_RESPONSE_INVALID", "model row is malformed"
                )
            model = _exact_string(model_row.get("id"), "model id")
            route_selector = f"{provider}/{model}"
            if selector is not None and route_selector != selector:
                continue
            if route_selector in seen:
                raise DeepSeekHostError(
                    "HARNESS_HOST_RESPONSE_INVALID", "duplicate exact model route"
                )
            seen.add(route_selector)
            reasoning = model_row.get("reasoning")
            efforts: list[str] = []
            default_effort = None
            if reasoning is not None:
                if not isinstance(reasoning, Mapping):
                    raise DeepSeekHostError(
                        "HARNESS_HOST_RESPONSE_INVALID",
                        "model reasoning projection is malformed",
                    )
                raw_efforts = reasoning.get("efforts", [])
                if (
                    not isinstance(raw_efforts, list)
                    or len(raw_efforts) > MAX_REASONING_OPTIONS
                ):
                    raise DeepSeekHostError(
                        "HARNESS_HOST_RESPONSE_INVALID",
                        "model reasoning options exceed safety limits",
                    )
                seen_efforts: set[str] = set()
                for effort_row in raw_efforts:
                    if not isinstance(effort_row, Mapping):
                        raise DeepSeekHostError(
                            "HARNESS_HOST_RESPONSE_INVALID",
                            "model reasoning option is malformed",
                        )
                    effort = _exact_string(
                        effort_row.get("id"), "reasoning option id"
                    )
                    if effort in seen_efforts:
                        raise DeepSeekHostError(
                            "HARNESS_HOST_RESPONSE_INVALID",
                            "duplicate exact reasoning option id",
                        )
                    seen_efforts.add(effort)
                    efforts.append(effort)
                default = reasoning.get("defaultEffort")
                if default is not None:
                    if not isinstance(default, str) or default not in efforts:
                        raise DeepSeekHostError(
                            "HARNESS_HOST_RESPONSE_INVALID",
                            "model reasoning default is not an advertised effort",
                        )
                    default_effort = default
            identity = {
                "provider": provider,
                "model": model,
                "endpoint_identity": endpoint,
                "credential_ref": credential_ref,
                "credential_status": status,
                "settings_ns": provider_map[provider].get("settingsNs"),
                "settings_path": provider_map[provider].get("settingsPath"),
                "runtime_version": runtime_version,
            }
            digest = __import__("hashlib").sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            raw_name = model_row.get("name")
            route_name = raw_name if isinstance(raw_name, str) else model
            routes.append(ConfiguredRoute(
                selector=route_selector,
                provider=provider,
                model=model,
                name=route_name,
                endpoint_identity=endpoint,
                credential_ref=credential_ref,
                credential_status=status,
                reasoning_efforts=tuple(dict.fromkeys(efforts)),
                default_effort=default_effort,
                runtime_version=runtime_version,
                source_commit=source_commit,
                configuration_digest=digest,
            ))
    if selector is not None and not routes:
        raise DeepSeekHostError(
            "HARNESS_ROUTE_UNAVAILABLE", f"configured DeepSeek route is unavailable: {selector}"
        )
    return routes


def route_for(client: HostTransport, selector: str) -> ConfiguredRoute:
    routes = configured_routes(client, selector=selector)
    if len(routes) != 1:
        raise DeepSeekHostError(
            "HARNESS_ROUTE_UNAVAILABLE", "exact DeepSeek route did not resolve uniquely"
        )
    return routes[0]
