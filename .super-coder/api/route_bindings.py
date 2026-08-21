#!/usr/bin/env python3
"""Canonical version-two model route bindings.

This module owns identity, validation, and participant revision persistence.
Launch adapters consume its result; they do not reinterpret nullable effort.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import harness_versions
from conversation_adapters.base import AdapterError, checked_version_compatibility

CONTRACT_VERSION = 2
FRESH_HOURS = 24
HARNESS_SUPPORT_STATES = frozenset({"tested", "best-effort"})
LEGACY_HARNESS_EVIDENCE_FORMAT = "legacy-semver"
RAW_HARNESS_EVIDENCE_FORMAT = "raw-observed-v1"
BINDING_KEYS = (
    "contract_version",
    "control_state",
    "harness",
    "requested_model",
    "provider_model",
    "requested_effort",
    "effective_effort",
    "native_variant_id",
    "transport",
    "catalogue_generation",
    "evidence_digest",
    "selector_binding",
    "adapter_metadata",
)

CONTROLLED_EVIDENCE = {
    "claude": {"claude-portable-manifest"},
    "codex": {"codex-model-cache"},
    "deepseek": {
        "deepseek-authenticated-models",
        "deepseek-provider-authenticated-models-v2",
    },
    "kimi": {"kimi-alias-config"},
    "opencode": {"opencode-connected-variant"},
}
SUPPORTED_HARNESSES = frozenset((*CONTROLLED_EVIDENCE, "vibe"))

TRANSPORTS = {
    "claude": "claude-effort-argument",
    "codex": "codex-reasoning-config",
    "deepseek": "deepseek-provider-options-v1",
    "kimi": "kimi-effort-environment",
    "opencode": "opencode-route-agent",
}

DEEPSEEK_PROVIDER_ROUTE = "deepseek-official"
DEEPSEEK_PROVIDER_ROUTES = frozenset({DEEPSEEK_PROVIDER_ROUTE, "ollama-cloud"})
DEEPSEEK_TRANSPORT_CONTRACT = TRANSPORTS["deepseek"]
DEEPSEEK_OVERRIDE_FIELDS = ("thinking", "reasoning_effort")
DEEPSEEK_EFFORTS = frozenset({"default", "low", "high", "max"})

# Reserved canonical effort: bind the exact model with no effort transport and
# let the harness or alias's own default govern thinking.  Admitted for every
# controlled exact route regardless of its advertised supported_efforts.
DEFAULT_EFFORT = "default"

LOWER_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)
ADAPTERS = Path(__file__).resolve().parents[1] / "adapters"


@dataclass(frozen=True)
class RouteResolutionError(Exception):
    code: str
    message: str
    details: dict[str, Any]

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class RuntimeEvidence:
    harness: str | None
    runtime: str | None
    runtime_identity: str | None
    version: str | None
    observed_version: str | None
    compatibility: str | None
    minimum_version: str | None
    maximum_version_exclusive: str | None
    verified_version: str | None
    error: str | None

    @classmethod
    def from_value(cls, value: Any) -> RuntimeEvidence:
        status = value if isinstance(value, dict) else {}
        observed_version = status.get("observed_version", status.get("version"))
        return cls(**{
            field: observed_version if field == "observed_version" else status.get(field)
            for field in cls.__dataclass_fields__
        })


_CONTROLLED_PROOF_ISSUER = object()


class _ControlledRouteProof:
    """Private result of one resolver-owned runtime and source observation."""

    __slots__ = (
        "_harness", "_selector", "_runtime_status", "_runtime",
        "_runtime_identity", "_source_fingerprint", "_issuer",
    )

    def __init__(
        self,
        issuer,
        *,
        harness: str,
        selector: str,
        runtime_status: RuntimeEvidence,
        runtime_scope: dict,
        source_fingerprint: str | None,
    ) -> None:
        if issuer is not _CONTROLLED_PROOF_ISSUER:
            raise TypeError(
                "controlled route proofs are issued only by the canonical probe"
            )
        object.__setattr__(self, "_harness", harness)
        object.__setattr__(self, "_selector", selector)
        object.__setattr__(self, "_runtime_status", runtime_status)
        object.__setattr__(self, "_runtime", runtime_scope.get("runtime"))
        object.__setattr__(
            self, "_runtime_identity", runtime_scope.get("runtime_identity")
        )
        object.__setattr__(self, "_source_fingerprint", source_fingerprint)
        object.__setattr__(self, "_issuer", issuer)

    def __setattr__(self, _name, _value) -> None:
        raise AttributeError("controlled route proofs are immutable")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_object(value: Any, *, field: str) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RouteResolutionError(
                "thinking_evidence_missing",
                f"route {field} is not valid JSON",
                {"field": field},
            ) from exc
    if not isinstance(value, dict):
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"route {field} must be an object",
            {"field": field},
        )
    return value


def _supported_efforts(row: dict) -> tuple[list[str], dict]:
    raw = row.get("supported_efforts") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RouteResolutionError(
                "thinking_evidence_missing",
                "route supported efforts are invalid",
                {},
            ) from exc
    if not isinstance(raw, list):
        raise RouteResolutionError(
            "thinking_evidence_missing", "route supported efforts are invalid", {}
        )
    supported = [item for item in raw if isinstance(item, str)]
    metadata = _json_object(row.get("effort_metadata"), field="effort_metadata")
    return supported, metadata


def normalize_harness(harness: str) -> str:
    normalized = (harness or "").strip().lower()
    if not normalized:
        raise RouteResolutionError(
            "unsupported_thinking_level", "harness is required", {"harness": harness}
        )
    if normalized not in SUPPORTED_HARNESSES:
        raise RouteResolutionError(
            "unsupported_thinking_level",
            "Harness is not supported",
            {"harness": normalized},
        )
    return normalized


def _normalize_model(model: str | None) -> str | None:
    if model is None:
        return None
    if not isinstance(model, str) or not model or model != model.strip():
        raise RouteResolutionError(
            "unsupported_thinking_level",
            "model must be an exact non-blank selector",
            {"model": model},
        )
    return model


def _exact_nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _stored_support_state(row: dict) -> str | None:
    """Read pre-0217 rows as their legacy verified support claim."""
    state = row.get("harness_support_state")
    if state in HARNESS_SUPPORT_STATES:
        return state
    return "tested" if row.get("harness_compatibility") in {
        "verified", "supported"
    } else None


def _matches_captured_version(
    captured: str | None,
    runtime: RuntimeEvidence,
    *,
    evidence_format: str = RAW_HARNESS_EVIDENCE_FORMAT,
) -> bool:
    """Compare the exact new encoding, or the explicit legacy semver encoding."""
    if evidence_format == LEGACY_HARNESS_EVIDENCE_FORMAT:
        return captured == runtime.version
    return captured == runtime.observed_version


def _runtime_support_state(runtime: RuntimeEvidence) -> str | None:
    if runtime.error or not _exact_nonblank(runtime.observed_version):
        return None
    return "tested" if (
        runtime.version == runtime.verified_version
        and runtime.compatibility == "verified"
    ) else "best-effort"


def _lower_hex(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _ascii_lower(value: str) -> str:
    return value.translate(ASCII_LOWER_TRANSLATION)


def _binding_error(reason: str) -> RouteResolutionError:
    return RouteResolutionError(
        "thinking_evidence_missing",
        "Invalid version-two route binding",
        {"reason": reason},
    )


def _validate_deepseek_metadata(binding: dict) -> None:
    """Pin the evidence-selected provider request expectation in the binding."""
    metadata = binding["adapter_metadata"]
    if set(metadata) != {
        "provider_route", "provider_adapter_id", "provider_adapter_digest",
        "provider_registry_sha256", "credential_kind", "endpoint_identity",
        "discovery_evidence_digest", "transport_contract", "provider_options",
        "wire_evidence_digest", "runtime_version", "source_commit",
        "patch_sha256", "composition_sha256",
    }:
        raise _binding_error("DeepSeek metadata must contain the fixed provider evidence")
    provider = metadata["provider_route"]
    if provider not in DEEPSEEK_PROVIDER_ROUTES:
        raise _binding_error("DeepSeek provider route is not reviewed")
    if metadata["transport_contract"] != DEEPSEEK_TRANSPORT_CONTRACT:
        raise _binding_error("DeepSeek transport contract is not canonical")
    expected_credential = {
        "deepseek-official": "deepseek-api-key",
        "ollama-cloud": "ollama-api-key",
    }[provider]
    if metadata["credential_kind"] != expected_credential:
        raise _binding_error("DeepSeek credential kind does not match its provider")
    for field in (
        "provider_adapter_digest", "provider_registry_sha256",
        "discovery_evidence_digest", "wire_evidence_digest", "patch_sha256",
        "composition_sha256",
    ):
        if not _lower_hex(metadata[field], LOWER_HEX_64):
            raise _binding_error(f"DeepSeek {field} must be a SHA-256 digest")
    if not _exact_nonblank(metadata["provider_adapter_id"]):
        raise _binding_error("DeepSeek provider adapter identity is missing")
    if not _exact_nonblank(metadata["runtime_version"]):
        raise _binding_error("DeepSeek runtime version is missing")
    if (
        not isinstance(metadata["source_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", metadata["source_commit"]) is None
    ):
        raise _binding_error("DeepSeek source commit is invalid")
    endpoint = metadata["endpoint_identity"]
    parsed_endpoint = urlsplit(endpoint) if _exact_nonblank(endpoint) else None
    if (
        parsed_endpoint is None
        or parsed_endpoint.scheme not in {"https", "http"}
        or parsed_endpoint.hostname is None
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise _binding_error("DeepSeek endpoint identity must be credential-free HTTP(S)")
    requested_model = binding["requested_model"]
    provider_model = binding["provider_model"]
    expected_selector = (
        provider_model
        if provider == DEEPSEEK_PROVIDER_ROUTE
        else f"{provider}/{provider_model}"
    )
    if requested_model != expected_selector:
        raise _binding_error("DeepSeek selector does not match its provider/model route")
    options = metadata["provider_options"]
    if not isinstance(options, dict) or set(options) != {"omit", "set"}:
        raise _binding_error("DeepSeek provider options must contain omit and set")
    omitted = options["omit"]
    selected = options["set"]
    if not isinstance(omitted, list) or not isinstance(selected, dict):
        raise _binding_error("DeepSeek provider option values have invalid types")

    effort = binding["requested_effort"]
    if effort not in DEEPSEEK_EFFORTS:
        raise _binding_error("DeepSeek effort is outside the carrier contract")
    if effort == DEFAULT_EFFORT:
        if omitted != list(DEEPSEEK_OVERRIDE_FIELDS) or selected != {}:
            raise _binding_error(
                "DeepSeek model default must omit every reasoning override"
            )
        return
    if provider == DEEPSEEK_PROVIDER_ROUTE:
        if omitted != [] or set(selected) != set(DEEPSEEK_OVERRIDE_FIELDS):
            raise _binding_error("DeepSeek-official named effort must set the exact override pair")
        if selected["thinking"] != {"type": "enabled"}:
            raise _binding_error("DeepSeek-official named effort must enable thinking")
    elif omitted != ["thinking"] or set(selected) != {"reasoning_effort"}:
        raise _binding_error("Ollama named effort must set only reasoning_effort")
    wire_effort = selected["reasoning_effort"]
    if wire_effort != effort:
        raise _binding_error(
            "DeepSeek wire effort must equal the immutable requested effort"
        )


def validate_v2_binding(binding: dict) -> None:
    """Enforce the one semantic state contract accepted by every v2 consumer."""
    if not isinstance(binding, dict) or set(binding) != set(BINDING_KEYS):
        raise _binding_error("binding must contain exactly the canonical fixed keys")
    if binding["contract_version"] != CONTRACT_VERSION:
        raise _binding_error("contract_version must be 2")

    harness = binding["harness"]
    if not _exact_nonblank(harness) or harness != harness.lower():
        raise _binding_error("harness must be a normalized non-blank identifier")
    if harness not in SUPPORTED_HARNESSES:
        raise _binding_error("harness must identify a shipped adapter")
    state = binding["control_state"]
    adapter_metadata = binding["adapter_metadata"]
    if not isinstance(adapter_metadata, dict):
        raise _binding_error("adapter_metadata must be an object")

    if state == "controlled":
        if harness not in TRANSPORTS:
            raise _binding_error("controlled bindings require a supported harness")
        for field in (
            "requested_model", "provider_model", "requested_effort",
            "effective_effort",
        ):
            if not _exact_nonblank(binding[field]):
                raise _binding_error(f"controlled {field} must be exact and non-blank")
        requested_effort = binding["requested_effort"]
        if requested_effort != requested_effort.lower():
            raise _binding_error("controlled effort must be canonical lowercase")
        if binding["effective_effort"] != requested_effort:
            raise _binding_error("requested and effective effort must match")
        if binding["transport"] != TRANSPORTS[harness]:
            raise _binding_error("controlled transport does not match harness")
        if not _lower_hex(binding["catalogue_generation"], LOWER_HEX_32):
            raise _binding_error(
                "catalogue_generation must be 32 lowercase hex characters"
            )
        if requested_effort == DEFAULT_EFFORT:
            if binding["evidence_digest"] is not None:
                raise _binding_error(
                    "model-default bindings carry no effort-value digest"
                )
        elif not _lower_hex(binding["evidence_digest"], LOWER_HEX_64):
            raise _binding_error("evidence_digest must be a SHA-256 hex digest")
        selector_binding = binding["selector_binding"]
        if not isinstance(selector_binding, dict) or not selector_binding:
            raise _binding_error("controlled selector_binding must be a non-empty object")
        native_variant = binding["native_variant_id"]
        if requested_effort == DEFAULT_EFFORT:
            if native_variant is not None:
                raise _binding_error(
                    "model-default bindings carry no native variant"
                )
        elif harness == "opencode":
            if native_variant != requested_effort:
                raise _binding_error(
                    "OpenCode native variant must equal the canonical effort"
                )
        elif native_variant is not None:
            raise _binding_error("native variants are exclusive to OpenCode")
        if harness == "deepseek":
            _validate_deepseek_metadata(binding)
        return

    if state not in {"harness-default", "native-uncontrolled"}:
        raise _binding_error("control_state is not recognized")
    if binding["transport"] != "native-default":
        raise _binding_error("uncontrolled bindings require native-default transport")
    for field in (
        "provider_model", "requested_effort", "effective_effort",
        "native_variant_id", "catalogue_generation", "evidence_digest",
        "selector_binding",
    ):
        if binding[field] is not None:
            raise _binding_error(f"uncontrolled {field} must be null")
    if adapter_metadata != {}:
        raise _binding_error("uncontrolled adapter_metadata must be empty")
    if state == "harness-default":
        if binding["requested_model"] is not None:
            raise _binding_error("harness-default requested_model must be null")
        return
    if harness != "vibe" or not _exact_nonblank(binding["requested_model"]):
        raise _binding_error(
            "native-uncontrolled bindings require an exact Vibe model"
        )


def _runtime_manifest_compatibility(harness: str, version: str):
    try:
        manifest = json.loads((ADAPTERS / harness / "adapter.json").read_text())
        declared = (
            manifest.get("runtime_compatibility")
            if harness == "vibe"
            else manifest.get("conversation")
        ) or {}
        return checked_version_compatibility(
            harness=harness, compatibility=declared, version=version
        )
    except AdapterError as exc:
        message = (
            f"Harness '{harness}' has no valid compatibility manifest"
            if exc.code == "HARNESS_MANIFEST_INVALID"
            else f"Harness '{harness}' has no compatible installed runtime"
        )
        raise RouteResolutionError(
            "thinking_evidence_missing",
            message,
            {"harness": harness, "runtime_error": exc.code},
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"Harness '{harness}' has no valid compatibility manifest",
            {"harness": harness, "runtime_error": getattr(exc, "code", None)},
        ) from exc


def _require_runtime(
    harness: str,
    model: str | None,
    runtime_status: dict | RuntimeEvidence | None,
    runtime_scope: dict | None = None,
    *,
    error_code: str = "thinking_evidence_missing",
) -> RuntimeEvidence:
    """Validate harness compatibility evidence for the exact execution seat."""
    status = runtime_status if isinstance(runtime_status, RuntimeEvidence) \
        else RuntimeEvidence.from_value(runtime_status)
    scope = runtime_scope if isinstance(runtime_scope, dict) \
        else harness_versions.runtime_scope()
    expected_runtime = scope.get("runtime")
    expected_identity = scope.get("runtime_identity")
    observed_version = status.observed_version
    if (
        status.harness != harness
        or status.runtime not in {"host", "sandbox"}
        or status.runtime != expected_runtime
        or not isinstance(status.runtime_identity, str)
        or status.runtime_identity != expected_identity
        or not status.runtime_identity.startswith(f"{status.runtime}:")
        or not isinstance(expected_identity, str)
        or not expected_identity.startswith(f"{expected_runtime}:")
        or status.error is not None
        or not _exact_nonblank(observed_version)
    ):
        raise RouteResolutionError(
            error_code,
            f"Harness '{harness}' has no compatible installed runtime",
            {
                "harness": harness,
                "model": model,
                "version": observed_version,
                "compatibility": status.compatibility,
                "evidence_harness": status.harness,
                "evidence_runtime": status.runtime,
                "evidence_runtime_identity": status.runtime_identity,
                "expected_runtime": expected_runtime,
                "expected_runtime_identity": expected_identity,
                "runtime_error": status.error,
                "persist_route_stale": error_code != "thinking_evidence_stale",
                "remediation": "install or repair the harness runtime",
            },
        )
    return status


def _require_uncontrolled_runtime(
    harness: str,
    model: str | None,
    runtime_status: dict | RuntimeEvidence | None,
    runtime_scope: dict | None = None,
) -> None:
    """Admit typed-uncontrolled identity only for the exact execution seat."""
    _require_runtime(harness, model, runtime_status, runtime_scope)


def _require_controlled_runtime(
    row: dict,
    harness: str,
    model: str,
    runtime_status: dict | RuntimeEvidence | None,
    runtime_scope: dict | None = None,
    *,
    error_code: str = "thinking_evidence_stale",
) -> RuntimeEvidence:
    """Bind controlled evidence to the runtime that will execute the route."""
    status = _require_runtime(
        harness, model, runtime_status, runtime_scope,
        error_code=error_code,
    )
    if not _matches_captured_version(row.get("harness_version"), status):
        raise RouteResolutionError(
            error_code,
            "Installed harness version changed after refresh",
            {
                "harness": harness,
                "model": model,
                "harness_version": row.get("harness_version"),
                "installed_version": status.observed_version,
                "runtime": status.runtime,
                "runtime_identity": status.runtime_identity,
                "remediation": "sc models refresh",
            },
        )
    return status


def _probe_controlled_route(harness: str, model: str) -> _ControlledRouteProof:
    """Collect one indivisible execution-seat proof for an exact route.

    Only the owning resolution/availability operation carries this result;
    public resolvers never accept a reusable source observation from callers.
    Importing lazily avoids the catalogue's binding-schema import cycle.
    """
    harness = normalize_harness(harness)
    model = _normalize_model(model)
    if model is None or harness == "vibe":
        raise RouteResolutionError(
            "thinking_evidence_missing",
            "Controlled route proof requires an exact controlled route",
            {"harness": harness, "model": model},
        )
    import model_catalog  # noqa: PLC0415

    observation = model_catalog.controlled_route_evidence(harness, model)
    status = RuntimeEvidence.from_value(observation.get("runtime_status"))
    scope = observation.get("runtime_scope")
    if not isinstance(scope, dict):
        scope = {}
    return _ControlledRouteProof(
        _CONTROLLED_PROOF_ISSUER,
        harness=harness,
        selector=model,
        runtime_status=status,
        runtime_scope=scope,
        source_fingerprint=observation.get("source_fingerprint"),
    )


def _controlled_proof(
    proof: _ControlledRouteProof | None,
    harness: str,
    model: str,
    *,
    collect: bool,
) -> _ControlledRouteProof:
    if proof is None and collect:
        proof = _probe_controlled_route(harness, model)
    if (
        not isinstance(proof, _ControlledRouteProof)
        or proof._issuer is not _CONTROLLED_PROOF_ISSUER
        or proof._harness != harness
        or proof._selector != model
    ):
        raise RouteResolutionError(
            "thinking_evidence_stale",
            "Route proof was not collected by the canonical execution-seat probe",
            {
                "harness": harness,
                "model": model,
                "persist_route_stale": False,
                "remediation": "re-probe the route in the execution runtime",
            },
        )
    return proof


def _require_controlled_source(
    row: dict,
    harness: str,
    model: str,
    runtime: RuntimeEvidence,
    proof: _ControlledRouteProof,
    *,
    error_code: str = "thinking_evidence_stale",
) -> None:
    """Require the canonical probe's source to match its runtime and the row."""
    coherent = (
        proof._harness == harness
        and proof._selector == model
        and proof._runtime_status.runtime == runtime.runtime
        and proof._runtime_status.runtime_identity == runtime.runtime_identity
        and proof._runtime_status.observed_version == runtime.observed_version
    )
    if not coherent:
        raise RouteResolutionError(
            error_code,
            "Route source evidence does not match the execution runtime",
            {
                "harness": harness,
                "model": model,
                "evidence_harness": proof._harness,
                "evidence_model": proof._selector,
                "evidence_runtime": proof._runtime_status.runtime,
                "evidence_runtime_identity": proof._runtime_status.runtime_identity,
                "evidence_harness_version": proof._runtime_status.observed_version,
                "runtime": runtime.runtime,
                "runtime_identity": runtime.runtime_identity,
                "runtime_harness_version": runtime.observed_version,
                "persist_route_stale": False,
                "remediation": "re-probe the route in the execution runtime",
            },
        )
    stored_fingerprint = row.get("source_fingerprint")
    if not stored_fingerprint:
        raise RouteResolutionError(
            error_code,
            "Route has no local source fingerprint",
            {"harness": harness, "model": model,
             "remediation": "sc models refresh"},
        )
    if proof._source_fingerprint != stored_fingerprint:
        raise RouteResolutionError(
            error_code,
            "Installed route source changed after refresh",
            {"harness": harness, "model": model,
             "remediation": "sc models refresh"},
        )


def _uncontrolled_binding(harness: str, model: str | None, effort: str | None) -> dict:
    if harness == "deepseek":
        raise RouteResolutionError(
            "thinking_evidence_missing",
            "DeepSeek requires an authenticated exact model route",
            {"harness": harness, "model": model,
             "remediation": "sc models refresh"},
        )
    if effort is not None:
        raise RouteResolutionError(
            "unsupported_thinking_level",
            "Thinking control is unavailable for this route",
            {"harness": harness, "model": model, "requested_effort": effort},
        )
    state = "harness-default" if model is None else "native-uncontrolled"
    if state == "native-uncontrolled" and harness != "vibe":
        raise AssertionError("only Vibe has an exact native-uncontrolled route")
    return {
        "contract_version": CONTRACT_VERSION,
        "control_state": state,
        "harness": harness,
        "requested_model": model,
        "provider_model": None,
        "requested_effort": None,
        "effective_effort": None,
        "native_variant_id": None,
        "transport": "native-default",
        "catalogue_generation": None,
        "evidence_digest": None,
        "selector_binding": None,
        "adapter_metadata": {},
    }


def _age_hours(value: str, now: datetime) -> float:
    try:
        seen = datetime.fromisoformat(value)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise RouteResolutionError(
            "thinking_evidence_stale",
            "route evidence has no valid completion time",
            {"last_seen_at": value},
        ) from exc
    return (now - seen.astimezone(timezone.utc)).total_seconds() / 3600


def _validate_route_freshness(
    row: dict,
    harness: str,
    model: str,
    *,
    now: datetime,
) -> None:
    if row.get("stale"):
        raise RouteResolutionError(
            "thinking_evidence_stale",
            row.get("last_error") or "Route evidence is stale",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )
    captured_version = row.get("harness_version")
    support_state = _stored_support_state(row)
    if support_state not in HARNESS_SUPPORT_STATES or not _exact_nonblank(captured_version):
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"Route {harness}/{model} has no observed harness transport",
            {
                "harness": harness,
                "model": model,
                "harness_version": captured_version,
                "harness_support_state": support_state,
            },
        )
    if (
        row.get("availability") != "available"
        or (harness != "deepseek" and not row.get("headless_supported"))
    ):
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"Route {harness}/{model} is not locally callable",
            {"harness": harness, "model": model},
        )
    if not row.get("generation_id"):
        raise RouteResolutionError(
            "thinking_evidence_missing",
            "Route has no successful catalogue generation",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )
    if _age_hours(row.get("last_seen_at"), now) > FRESH_HOURS:
        raise RouteResolutionError(
            "thinking_evidence_stale",
            "Route evidence is older than 24 hours",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )


def _require_fresh_route(
    con,
    row: dict | None,
    harness: str,
    model: str | None,
    *,
    now: datetime | None = None,
    route_proof: _ControlledRouteProof | None,
) -> dict | None:
    """Validate and stage one exact route's freshness in the caller's write."""
    harness = normalize_harness(harness)
    model = _normalize_model(model)
    if model is None or harness == "vibe":
        return row
    if row is None or row.get("harness") != harness or row.get("selector") != model:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"No local route evidence for {harness}/{model}",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )
    if not con.in_transaction:
        raise RuntimeError(
            "require_fresh_route requires a caller-owned write transaction"
        )

    def identity(value: dict) -> tuple[Any, Any]:
        return value.get("generation_id"), value.get("source_fingerprint")

    def changed() -> RouteResolutionError:
        return RouteResolutionError(
            "thinking_evidence_stale",
            "Route evidence changed during resolution; retry",
            {"harness": harness, "model": model,
             "remediation": "retry route resolution"},
        )

    authoritative = con.execute(
        "SELECT * FROM model_routes WHERE harness=? AND selector=?",
        (harness, model),
    ).fetchone()
    if authoritative is None:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"No local route evidence for {harness}/{model}",
            {"harness": harness, "model": model,
             "remediation": "sc models refresh"},
        )
    authoritative = dict(authoritative)
    if identity(authoritative) != identity(row):
        raise changed()
    row = authoritative

    check_time = now or datetime.now(timezone.utc)
    try:
        _validate_route_freshness(
            row, harness, model, now=check_time,
        )
        proof = _controlled_proof(
            route_proof, harness, model, collect=False
        )
        runtime = _require_controlled_runtime(
            row, harness, model,
            proof._runtime_status,
            {"runtime": proof._runtime,
             "runtime_identity": proof._runtime_identity},
        )
        _require_controlled_source(row, harness, model, runtime, proof)
        latest = con.execute(
            "SELECT generation_id,completed_at FROM model_catalog_generations "
            "WHERE state='successful' "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise RouteResolutionError(
                "thinking_evidence_missing",
                "No successful catalogue generation is available",
                {"harness": harness, "model": model,
                 "remediation": "sc models refresh"},
            )
        if row["generation_id"] != latest["generation_id"]:
            raise RouteResolutionError(
                "thinking_evidence_stale",
                "Route does not belong to the latest successful generation",
                {"harness": harness, "model": model,
                 "remediation": "sc models refresh"},
            )
        if _age_hours(latest["completed_at"], check_time) > FRESH_HOURS:
            raise RouteResolutionError(
                "thinking_evidence_stale",
                "Latest successful catalogue generation is older than 24 hours",
                {"harness": harness, "model": model,
                 "remediation": "sc models refresh"},
            )
    except RouteResolutionError as exc:
        if (
            exc.code == "thinking_evidence_stale"
            and exc.details.get("persist_route_stale", True)
            and not row.get("stale")
        ):
            remediation = exc.details.get("remediation") or "sc models refresh"
            reason = f"{exc.code}: {exc.message}; remediation: {remediation}"
            updated = con.execute(
                "UPDATE model_routes SET stale=1,last_error=? "
                "WHERE harness=? AND selector=? AND generation_id IS ? "
                "AND source_fingerprint IS ? AND stale=0",
                (reason, harness, model, row.get("generation_id"),
                 row.get("source_fingerprint")),
            )
            if updated.rowcount != 1:
                current = con.execute(
                    "SELECT generation_id,source_fingerprint,stale "
                    "FROM model_routes WHERE harness=? AND selector=?",
                    (harness, model),
                ).fetchone()
                if (
                    current is None
                    or identity(dict(current)) != identity(row)
                    or not current["stale"]
                ):
                    raise changed() from exc
        raise
    return row


def require_fresh_route(
    con,
    row: dict | None,
    harness: str,
    model: str | None,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Validate freshness using a current canonical execution-seat probe.

    Callers cannot supply or replay a prior source observation.  The helper is
    composable with a caller-owned transaction; standalone binding resolution
    uses :func:`resolve_persisted_v2` so its probe stays outside the write.
    """
    normalized_harness = normalize_harness(harness)
    normalized_model = _normalize_model(model)
    if normalized_model is None or normalized_harness == "vibe":
        return _require_fresh_route(
            con, row, normalized_harness, normalized_model, now=now,
            route_proof=None,
        )
    proof = _probe_controlled_route(normalized_harness, normalized_model)
    return _require_fresh_route(
        con, row, normalized_harness, normalized_model, now=now,
        route_proof=proof,
    )


def resolve_persisted_v2(
    con,
    row: dict | None,
    harness: str,
    model: str | None,
    effort: str | None = None,
    *,
    now: datetime | None = None,
    runtime_status: dict | None = None,
    runtime_scope: dict | None = None,
) -> tuple[dict, str]:
    """Probe and resolve through one transaction-owned freshness operation."""
    harness = normalize_harness(harness)
    model = _normalize_model(model)
    if con.in_transaction:
        raise RuntimeError(
            "resolve_persisted_v2 owns its write transaction"
        )
    proof = None
    if model is not None and harness != "vibe":
        proof = _probe_controlled_route(harness, model)

    import db_driver  # noqa: PLC0415

    result = None
    resolution_error = None
    with db_driver.write_transaction(con, "model_route.resolve"):
        try:
            fresh_row = _require_fresh_route(
                con, row, harness, model, now=now, route_proof=proof,
            )
            result = _resolve_v2(
                fresh_row, harness, model, effort, now=now,
                route_proof=proof,
                runtime_status=runtime_status,
                runtime_scope=runtime_scope,
            )
        except RouteResolutionError as exc:
            # Freshness rejection may stage stale=1.  Commit that projection,
            # then raise outside the generator-based transaction context so
            # frozen typed errors remain safe on Python 3.14.
            resolution_error = exc
    if resolution_error is not None:
        raise resolution_error
    assert result is not None
    return result


def resolve_v2(
    row: dict | None,
    harness: str,
    model: str | None,
    effort: str | None = None,
    *,
    now: datetime | None = None,
    runtime_status: dict | None = None,
    runtime_scope: dict | None = None,
) -> tuple[dict, str]:
    """Resolve one intent after collecting current execution-seat evidence."""
    return _resolve_v2(
        row, harness, model, effort, now=now,
        route_proof=None,
        runtime_status=runtime_status,
        runtime_scope=runtime_scope,
    )


def _resolve_v2(
    row: dict | None,
    harness: str,
    model: str | None,
    effort: str | None = None,
    *,
    now: datetime | None = None,
    route_proof: _ControlledRouteProof | None,
    runtime_status: dict | None = None,
    runtime_scope: dict | None = None,
) -> tuple[dict, str]:
    """Build a binding from proof private to this resolution operation."""
    harness = normalize_harness(harness)
    model = _normalize_model(model)
    if model is None or harness == "vibe":
        binding = _uncontrolled_binding(harness, model, effort)
        _require_uncontrolled_runtime(
            harness, model, runtime_status, runtime_scope
        )
        validate_v2_binding(binding)
        return binding, digest_json(binding)

    # Decision #223: an omitted effort is resolved against the route's
    # advertised levels below (high where advertised, else Model default);
    # only an explicitly supplied effort is normalized and validated here.
    requested: str | None = None
    if effort is not None:
        requested = effort.strip()
        if harness == "opencode":
            requested = _ascii_lower(requested)
        else:
            requested = requested.lower()
        if not requested:
            raise RouteResolutionError(
                "unsupported_thinking_level",
                "Thinking level must be non-blank",
                {"harness": harness, "model": model},
            )
    if row is None:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"No local route evidence for {harness}/{model}",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )

    row_harness = row.get("harness")
    row_selector = row.get("selector")
    if (
        not isinstance(row_harness, str)
        or row_harness.strip().lower() != harness
        or row_selector != model
    ):
        raise RouteResolutionError(
            "thinking_evidence_missing",
            "Route evidence does not match the requested exact route",
            {
                "requested_harness": harness,
                "requested_model": model,
                "evidence_harness": row_harness,
                "evidence_model": row_selector,
            },
        )

    allowed = CONTROLLED_EVIDENCE.get(harness)
    evidence_kind = row.get("evidence_kind")
    if not allowed or evidence_kind not in allowed:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"No controlled-thinking evidence for {harness}/{model}",
            {"harness": harness, "model": model, "evidence_kind": evidence_kind},
        )
    generation = row.get("generation_id")
    check_time = now or datetime.now(timezone.utc)
    _validate_route_freshness(
        row, harness, model, now=check_time,
    )
    proof = _controlled_proof(route_proof, harness, model, collect=True)
    runtime = _require_controlled_runtime(
        row, harness, model, proof._runtime_status,
        {"runtime": proof._runtime,
         "runtime_identity": proof._runtime_identity},
    )
    _require_controlled_source(row, harness, model, runtime, proof)

    supported, effort_metadata = _supported_efforts(row)
    if requested is None:
        # Decision #223 bind-time fallback chain: omitted/unselected effort
        # on a controlled exact route resolves high where advertised, else
        # the reserved Model default (no effort transport) — every exact
        # model stays bindable even with no thinking support.
        requested = "high" if "high" in supported else DEFAULT_EFFORT
    if requested == DEFAULT_EFFORT:
        # Model default: the exact-model evidence, freshness, runtime, and
        # source gates above still apply; only the effort-value membership and
        # digest gates are bypassed.  The binding carries no effort digest or
        # native variant, and launch applies no effort transport.
        evidence_digest = None
        native_variant = None
    else:
        if requested not in supported:
            raise RouteResolutionError(
                "unsupported_thinking_level",
                f"Thinking level {requested!r} is unsupported for {harness}/{model}",
                {
                    "harness": harness,
                    "model": model,
                    "requested_effort": requested,
                    "supported_efforts": supported,
                    "default_effort": DEFAULT_EFFORT,
                    "generation": generation,
                    "remediation": (
                        "choose an advertised level or 'default' "
                        "(Model default)"
                    ),
                },
            )
        effort_digests = effort_metadata.get("digests") or {}
        evidence_digest = effort_digests.get(requested)
        if not evidence_digest:
            raise RouteResolutionError(
                "thinking_evidence_missing",
                "Selected thinking level has no route evidence digest",
                {"harness": harness, "model": model, "requested_effort": requested},
            )
        native_variants = effort_metadata.get("native_variant_ids") or {}
        native_variant = native_variants.get(requested) if harness == "opencode" else None
        if harness == "opencode" and not native_variant:
            raise RouteResolutionError(
                "thinking_evidence_missing",
                "OpenCode route has no admitted exact native variant",
                {"harness": harness, "model": model, "requested_effort": requested},
            )

    adapter_metadata = _json_object(
        row.get("adapter_metadata"), field="adapter_metadata"
    )
    if harness == "deepseek":
        metadata_by_effort = effort_metadata.get("adapter_metadata_by_effort") or {}
        selected_metadata = metadata_by_effort.get(requested)
        if not isinstance(selected_metadata, dict):
            raise RouteResolutionError(
                "thinking_evidence_missing",
                "DeepSeek route has no admitted provider-option mapping",
                {
                    "harness": harness,
                    "model": model,
                    "requested_effort": requested,
                },
            )
        adapter_metadata = selected_metadata
    if harness == "opencode" and requested != DEFAULT_EFFORT:
        metadata_by_effort = effort_metadata.get("adapter_metadata_by_effort") or {}
        selected_metadata = metadata_by_effort.get(requested)
        if (
            not isinstance(selected_metadata, dict)
            or selected_metadata.get("compatibility_manifest")
            != "opencode-1.18.9-v1"
            or selected_metadata.get("provider_family")
            not in {"openai-ai-sdk", "anthropic-ai-sdk"}
            or not isinstance(selected_metadata.get("variant_options"), dict)
            or not selected_metadata["variant_options"]
        ):
            raise RouteResolutionError(
                "thinking_evidence_missing",
                "OpenCode route has no admitted variant option overlay",
                {
                    "harness": harness,
                    "model": model,
                    "requested_effort": requested,
                },
            )
        adapter_metadata = selected_metadata

    binding = {
        "contract_version": CONTRACT_VERSION,
        "control_state": "controlled",
        "harness": harness,
        "requested_model": model,
        "provider_model": row.get("provider_model") or model,
        "requested_effort": requested,
        "effective_effort": requested,
        "native_variant_id": native_variant,
        "transport": TRANSPORTS[harness],
        "catalogue_generation": generation,
        "evidence_digest": evidence_digest,
        "selector_binding": _json_object(
            row.get("selector_binding"), field="selector_binding"
        ),
        "adapter_metadata": adapter_metadata,
    }
    if tuple(binding) != BINDING_KEYS:
        raise AssertionError("route binding key order drifted")
    validate_v2_binding(binding)
    return binding, digest_json(binding)


def legacy_route(*, row_contract_version: int, harness: str, model: str | None,
                 effort: str | None) -> dict:
    """Return raw v1 fields only for a caller that proved a stored v1 row."""
    if row_contract_version != 1:
        raise RouteResolutionError(
            "legacy_route_not_permitted",
            "Legacy resolution requires a stored contract-version-one row",
            {"route_contract_version": row_contract_version},
        )
    return {"contract_version": 1, "harness": harness, "model": model,
            "effort": effort, "legacy": True}


def verify_stored_v2_before_first_turn(
    _con,
    binding: dict,
    *,
    source_fingerprint: str | None,
    harness_version: str | None,
    harness_evidence_format: str = RAW_HARNESS_EVIDENCE_FORMAT,
) -> None:
    """Refuse drift without replacing an immutable stored binding.

    Armed routes retain their recorded generation.  This check observes the
    current execution seat and source, but never rebuilds or updates the
    binding from a newer catalogue generation.
    """
    validate_v2_binding(binding)
    harness = binding["harness"]
    model = binding["requested_model"]
    if binding["control_state"] != "controlled":
        import model_catalog  # noqa: PLC0415

        runtime = _require_runtime(
            harness,
            model,
            model_catalog.harness_runtime_status(harness),
            harness_versions.runtime_scope(),
            error_code="route_evidence_stale",
        )
        if source_fingerprint is not None or (
            harness_version is not None
            and (
                not _exact_nonblank(harness_version)
                or not _matches_captured_version(
                    harness_version, runtime,
                    evidence_format=harness_evidence_format,
                )
            )
        ):
            raise RouteResolutionError(
                "route_evidence_stale",
                "Stored Sprint route evidence changed before its first native turn",
                {
                    "harness": harness,
                    "model": model,
                    "captured_harness_version": harness_version,
                    "installed_harness_version": runtime.observed_version,
                    "remediation": "pause and reroute",
                },
            )
        return

    if (
        not _exact_nonblank(harness_version)
    ):
        raise RouteResolutionError(
            "route_evidence_stale",
            "Stored Sprint route has no immutable harness-version evidence",
            {"harness": harness, "model": model, "remediation": "pause and reroute"},
        )
    if not _lower_hex(source_fingerprint, LOWER_HEX_64):
        raise RouteResolutionError(
            "route_evidence_stale",
            "Stored Sprint route has no immutable source fingerprint",
            {"harness": harness, "model": model, "remediation": "pause and reroute"},
        )
    proof = _probe_controlled_route(harness, model)
    runtime = _require_runtime(
        harness,
        model,
        proof._runtime_status,
        {"runtime": proof._runtime, "runtime_identity": proof._runtime_identity},
        error_code="route_evidence_stale",
    )
    if (
        not _matches_captured_version(
            harness_version, runtime,
            evidence_format=harness_evidence_format,
        )
        or proof._source_fingerprint != source_fingerprint
    ):
        raise RouteResolutionError(
            "route_evidence_stale",
            "Stored Sprint route evidence changed before its first native turn",
            {
                "harness": harness,
                "model": model,
                "captured_harness_version": harness_version,
                "installed_harness_version": runtime.observed_version,
                "remediation": "pause and reroute",
            },
        )


class ParticipantRouteBindingStore:
    """Append and activate immutable participant-owned binding revisions."""

    def __init__(self, con):
        self.con = con

    def bind(self, participant_id: int, binding: dict, binding_digest: str, *,
             transition: str, runtime_status: dict | None = None,
             runtime_scope: dict | None = None,
             source_fingerprint: str | None = None,
             harness_version: str | None = None,
             harness_support_state: str | None = None) -> dict:
        validate_v2_binding(binding)
        if (not _lower_hex(binding_digest, LOWER_HEX_64)
                or digest_json(binding) != binding_digest):
            raise ValueError("binding does not match the canonical v2 contract")
        if binding["control_state"] == "controlled":
            if (
                not _lower_hex(source_fingerprint, LOWER_HEX_64)
                or not _exact_nonblank(harness_version)
                or harness_support_state not in HARNESS_SUPPORT_STATES
            ):
                raise ValueError(
                    "controlled Sprint bindings require immutable source provenance "
                    "and support state"
                )
        else:
            runtime = _require_runtime(
                binding["harness"], binding["requested_model"], runtime_status,
                runtime_scope,
            )
            if source_fingerprint is not None:
                raise ValueError(
                    "uncontrolled Sprint bindings cannot store a source fingerprint"
                )
            harness_version = runtime.observed_version
            harness_support_state = _runtime_support_state(runtime)
            if harness_support_state not in HARNESS_SUPPORT_STATES:
                raise ValueError(
                    "uncontrolled Sprint bindings require immutable support provenance"
                )
        row = self.con.execute(
            "SELECT participant.active_route_binding_id,sprint.lifecycle "
            "FROM sprint_participants participant "
            "JOIN sprints sprint ON sprint.sprint_id=participant.sprint_id "
            "WHERE participant.participant_id=?",
            (participant_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint participant: {participant_id}")
        lifecycle = row["lifecycle"]
        if transition == "arm":
            if lifecycle != "prepared" or row["active_route_binding_id"] is not None:
                raise ValueError("arm binding requires an unbound prepared participant")
            revision = 1
        elif transition == "reroute":
            if lifecycle != "paused":
                raise ValueError("reroute binding requires a paused Sprint")
            previous = self.con.execute(
                "SELECT MAX(route_revision) FROM sprint_participant_route_bindings "
                "WHERE participant_id=?", (participant_id,)
            ).fetchone()[0]
            revision = int(previous or 0) + 1
        else:
            raise ValueError("transition must be arm or reroute")

        values = [binding[key] for key in BINDING_KEYS]
        cursor = self.con.execute(
            "INSERT INTO sprint_participant_route_bindings ("
            "participant_id,route_revision,contract_version,control_state,harness,"
            "requested_model,provider_model,requested_effort,effective_effort,"
            "native_variant_id,transport,catalogue_generation,evidence_digest,"
            "selector_binding,adapter_metadata,binding_json,binding_digest,"
            "source_fingerprint,harness_version,harness_evidence_format,"
            "harness_support_state"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                participant_id, revision, *values[:11],
                canonical_json(binding["selector_binding"])
                if binding["selector_binding"] is not None else None,
                canonical_json(binding["adapter_metadata"]),
                canonical_json(binding), binding_digest,
                source_fingerprint, harness_version, RAW_HARNESS_EVIDENCE_FORMAT,
                harness_support_state,
            ),
        )
        binding_id = int(cursor.lastrowid)
        self.con.execute(
            "UPDATE sprint_participants SET active_route_binding_id=? "
            "WHERE participant_id=?", (binding_id, participant_id)
        )
        return {"binding_id": binding_id, "participant_id": participant_id,
                "route_revision": revision, "binding_digest": binding_digest,
                "harness_version": harness_version,
                "harness_support_state": harness_support_state}
