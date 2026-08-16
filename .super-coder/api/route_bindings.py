#!/usr/bin/env python3
"""Canonical version-two model route bindings.

This module owns identity, validation, and participant revision persistence.
Launch adapters consume its result; they do not reinterpret nullable effort.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


CONTRACT_VERSION = 2
FRESH_HOURS = 24
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
    "kimi": {"kimi-alias-config"},
    "opencode": {"opencode-connected-variant"},
}

TRANSPORTS = {
    "claude": "claude-effort-argument",
    "codex": "codex-reasoning-config",
    "kimi": "kimi-effort-environment",
    "opencode": "opencode-route-agent",
}


@dataclass(frozen=True)
class RouteResolutionError(Exception):
    code: str
    message: str
    details: dict[str, Any]

    def __str__(self) -> str:
        return self.message


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


def _normalize_harness(harness: str) -> str:
    normalized = (harness or "").strip().lower()
    if not normalized:
        raise RouteResolutionError(
            "unsupported_thinking_level", "harness is required", {"harness": harness}
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


def _uncontrolled_binding(harness: str, model: str | None, effort: str | None) -> dict:
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


def resolve_v2(
    row: dict | None,
    harness: str,
    model: str | None,
    effort: str | None = None,
    *,
    now: datetime | None = None,
    current_source_fingerprint: str | None = None,
) -> tuple[dict, str]:
    """Resolve one v2 intent to a fixed-key immutable binding and digest."""
    harness = _normalize_harness(harness)
    model = _normalize_model(model)
    if model is None or harness == "vibe":
        binding = _uncontrolled_binding(harness, model, effort)
        return binding, digest_json(binding)

    requested = "high" if effort is None else effort.strip().lower()
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

    allowed = CONTROLLED_EVIDENCE.get(harness)
    evidence_kind = row.get("evidence_kind")
    if not allowed or evidence_kind not in allowed:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"No controlled-thinking evidence for {harness}/{model}",
            {"harness": harness, "model": model, "evidence_kind": evidence_kind},
        )
    if row.get("availability") != "available" or not row.get("headless_supported"):
        raise RouteResolutionError(
            "thinking_evidence_missing",
            f"Route {harness}/{model} is not locally callable",
            {"harness": harness, "model": model},
        )
    generation = row.get("generation_id")
    if not generation:
        raise RouteResolutionError(
            "thinking_evidence_missing",
            "Route has no successful catalogue generation",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )
    if row.get("stale"):
        raise RouteResolutionError(
            "thinking_evidence_stale",
            row.get("last_error") or "Route evidence is stale",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )
    check_time = now or datetime.now(timezone.utc)
    if _age_hours(row.get("last_seen_at"), check_time) > FRESH_HOURS:
        raise RouteResolutionError(
            "thinking_evidence_stale",
            "Route evidence is older than 24 hours",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )
    stored_fingerprint = row.get("source_fingerprint")
    if current_source_fingerprint is not None and current_source_fingerprint != stored_fingerprint:
        raise RouteResolutionError(
            "thinking_evidence_stale",
            "Installed route source changed after refresh",
            {"harness": harness, "model": model, "remediation": "sc models refresh"},
        )

    supported, effort_metadata = _supported_efforts(row)
    if requested not in supported:
        raise RouteResolutionError(
            "unsupported_thinking_level",
            f"Thinking level {requested!r} is unsupported for {harness}/{model}",
            {
                "harness": harness,
                "model": model,
                "requested_effort": requested,
                "supported_efforts": supported,
                "generation": generation,
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
        "adapter_metadata": _json_object(
            row.get("adapter_metadata"), field="adapter_metadata"
        ),
    }
    if tuple(binding) != BINDING_KEYS:
        raise AssertionError("route binding key order drifted")
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


class ParticipantRouteBindingStore:
    """Append and activate immutable participant-owned binding revisions."""

    def __init__(self, con):
        self.con = con

    def bind(self, participant_id: int, binding: dict, binding_digest: str, *,
             transition: str) -> dict:
        if tuple(binding) != BINDING_KEYS or digest_json(binding) != binding_digest:
            raise ValueError("binding does not match the canonical v2 contract")
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
            "selector_binding,adapter_metadata,binding_json,binding_digest"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                participant_id, revision, *values[:11],
                canonical_json(binding["selector_binding"])
                if binding["selector_binding"] is not None else None,
                canonical_json(binding["adapter_metadata"]),
                canonical_json(binding), binding_digest,
            ),
        )
        binding_id = int(cursor.lastrowid)
        self.con.execute(
            "UPDATE sprint_participants SET active_route_binding_id=? "
            "WHERE participant_id=?", (binding_id, participant_id)
        )
        return {"binding_id": binding_id, "participant_id": participant_id,
                "route_revision": revision, "binding_digest": binding_digest}
