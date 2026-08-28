"""Deterministic route candidates for Sprint domain tests.

Route-source and runtime admission are covered independently in
test_route_bindings.py.  Sprint tests use this helper to exercise transactional
binding behavior without depending on the host's installed harness versions.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import harness_versions  # noqa: E402
import route_bindings  # noqa: E402
import sprint_domain  # noqa: E402


def _runtime(harness: str) -> tuple[dict, dict]:
    compatibility = route_bindings._runtime_manifest_compatibility(
        harness,
        {
            "claude": "2.1.223",
            "codex": "0.146.0",
            "kimi": "0.33.0",
            "opencode": "1.18.9",
            "vibe": "2.22.0",
        }[harness],
    )
    scope = harness_versions.runtime_scope()
    return (
        {
            "harness": harness,
            **scope,
            "version": compatibility.version,
            "compatibility": compatibility.compatibility,
            "minimum_version": compatibility.minimum_version,
            "maximum_version_exclusive": compatibility.maximum_version_exclusive,
            "verified_version": compatibility.verified_version,
            "error": None,
        },
        scope,
    )


def candidate(_con, participant) -> sprint_domain.ParticipantBindingCandidate:
    harness = route_bindings.normalize_harness(str(participant["harness"]))
    model = participant["model"]
    effort = participant["effort"]
    runtime_status = runtime_scope = None
    source_fingerprint = None
    if model is None or harness == "vibe":
        runtime_status, runtime_scope = _runtime(harness)
        binding, digest = route_bindings.resolve_v2(
            None,
            harness,
            model,
            effort,
            runtime_status=runtime_status,
            runtime_scope=runtime_scope,
        )
        harness_version = runtime_status["version"]
    else:
        selected = "high" if effort is None else str(effort).strip().lower()
        model_default = selected == route_bindings.DEFAULT_EFFORT
        provider_model = model
        adapter_metadata = {}
        selector_binding = {"kind": "test", "selector": model}
        binding = {
            "contract_version": 2,
            "control_state": "controlled",
            "harness": harness,
            "requested_model": model,
            "provider_model": provider_model,
            "requested_effort": selected,
            "effective_effort": selected,
            "native_variant_id": (
                selected
                if harness == "opencode" and not model_default
                else None
            ),
            "transport": route_bindings.TRANSPORTS[harness],
            "catalogue_generation": "1" * 32,
            "evidence_digest": None if model_default else "2" * 64,
            "selector_binding": selector_binding,
            "adapter_metadata": adapter_metadata,
        }
        route_bindings.validate_v2_binding(binding)
        digest = route_bindings.digest_json(binding)
        source_fingerprint = "3" * 64
        harness_version = {
            "claude": "2.1.223",
            "codex": "0.146.0",
            "kimi": "0.33.0",
            "opencode": "1.18.9",
        }[harness]
    return sprint_domain.ParticipantBindingCandidate(
        participant_id=int(participant["participant_id"]),
        binding=binding,
        binding_digest=digest,
        evidence_snapshot=None,
        runtime_status=runtime_status,
        runtime_scope=runtime_scope,
        source_fingerprint=source_fingerprint,
        harness_version=harness_version,
        harness_support_state="tested",
    )
