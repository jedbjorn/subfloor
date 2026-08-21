#!/usr/bin/env python3
"""Model catalog — live model-id suggestions for the Default Models GUI.

Layered, best-effort sources. The GUI's model field stays free text, so none
of this is load-bearing — a source that fails just thins the suggestions:

  1. models.dev/api.json — the keyless catalog OpenCode itself consumes.
     One fetch covers all five harness providers (anthropic / openai /
     mistral / ollama-cloud / kimi-for-coding), with release dates for
     newest-first sorting.
  2. Provider list-models APIs — only when the matching env key is present.
     Harness logins are OAuth, not API keys, so these are usually absent.
  3. DeepSeek's authenticated list-models API — exact ids only, paired with
     the pinned runtime's documented provider-option contract.
  4. OpenCode's loopback `/provider` projection — only providers OpenCode
     reports as connected, with exactly the models each connected provider
     exposes. This live overlay is refreshed on every served catalog.
  5. A static floor (the ids the engine ships in flavor_defaults) when every
     live source fails and no cache exists.

Fetched server-side (no CORS), cached under the gitignored .super-coder/logs/
(ephemeral like webapp.log — NOT .sc-state/, where an auto-written file would
dirty the tree and trip the publish guard) with a TTL; a failed refresh serves
the stale cache and says so.

Payload v6 exposes the flat `models` list consumed by the shared searchable
picker. Legacy `families` metadata remains for API compatibility but is not a
selection surface: the harness is the only picker prefilter, and family-null
local routes are ordinary results. Entries carry their route source, local
availability, CLI version, and effort support.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import harness_versions
import route_bindings
import toml_compat
from conversation_adapters.opencode import (
    connected_models as opencode_connected_models,
)

ENGINE = Path(__file__).resolve().parents[1]
CACHE = ENGINE / "logs" / "model_catalog.json"
ADAPTERS = ENGINE / "adapters"
TTL_HOURS = 24
TIMEOUT = 8
MAX_HTTP_JSON_BYTES = 4 * 1024 * 1024
MODELS_DEV_URL = "https://models.dev/api.json"

# harness -> models.dev provider key. kimi maps to "kimi-for-coding" (the
# Kimi Code plan), not the general "moonshotai" API provider: its ids are the
# ones the CLI actually reports (k3 / kimi-for-coding[-highspeed]), so the GUI
# datalist suggests what a kimi session can really select. Provider attribution
# for analytics is NOT sourced here — run.py's session_provider pins kimi to
# "kimi" to match its native wire.jsonl, regardless of this catalog mapping.
HARNESS_PROVIDER = {
    "claude": "anthropic",
    "codex": "openai",
    "vibe": "mistral",
    "opencode": "ollama-cloud",
    "kimi": "kimi-for-coding",
}
# opencode model ids are provider-prefixed ("ollama-cloud/<model>") — the
# format flavor_defaults already stores for that harness.
PREFIXED_HARNESSES = {"opencode"}

CLAUDE_ALIASES = ["fable", "opus", "sonnet", "haiku"]

# Bump when the response/cache shape changes — a cached payload from another
# version is ignored (treated as no cache) instead of being served to a
# client that expects the new shape.
PAYLOAD_VERSION = 8
_GENERATION_TABLE_UNAVAILABLE = object()

# provider APIs, keyed by harness: (env var, url, header builder). Responses
# are the OpenAI-style {"data": [{"id": ...}, ...]} shape on all three.
PROVIDER_APIS = {
    "claude": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models",
               lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "codex": ("OPENAI_API_KEY", "https://api.openai.com/v1/models",
              lambda k: {"Authorization": f"Bearer {k}"}),
    "vibe": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1/models",
             lambda k: {"Authorization": f"Bearer {k}"}),
}

DEEPSEEK_SOURCE = "deepseek-provider-api"
OLLAMA_CLOUD_SOURCE = "ollama-cloud-provider-api"
DEEPSEEK_DISCOVERY_ERROR = "authenticated DeepSeek model discovery failed"
DEEPSEEK_AUTHENTICATION_ERROR = "authenticated DeepSeek credential was rejected"
DEEPSEEK_EXACT_MODEL_ABSENT = "authenticated DeepSeek exact model is absent"
DEEPSEEK_DISCOVERY_EVIDENCE_INVALID = (
    "authenticated DeepSeek model evidence is malformed or duplicated"
)
DEEPSEEK_PROVIDER_REGISTRY_INVALID = "DeepSeek provider registry is unavailable"
DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED = (
    "DeepSeek provider-option mapper has no outbound wire proof"
)
DEEPSEEK_DISCOVERY_LIMIT_ERROR = (
    "authenticated DeepSeek model response exceeds safety limits"
)
DEEPSEEK_MODEL_ID_MAX_CHARS = 256


class _DeepSeekWireProofError(RuntimeError):
    pass


class _DeepSeekExactModelAbsentError(RuntimeError):
    pass


class _ModelCatalogueLimitError(ValueError):
    pass


class _DeepSeekDiscoveryEvidenceError(ValueError):
    pass


# The ids the engine ships in flavor_defaults — surfaced only when every live
# source fails AND no cache exists, so the datalist is never empty.
STATIC_FLOOR = {
    "claude": ["fable", "opus", "sonnet", "haiku"],
    "codex": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"],
    "vibe": ["devstral-latest", "codestral-latest"],
    "opencode": ["ollama-cloud/deepseek-v4-pro", "ollama-cloud/glm-5.1",
                 "ollama-cloud/qwen3-coder-next", "ollama-cloud/gpt-oss:20b"],
}


def _http_json(url: str, headers: dict | None = None) -> dict:
    # models.dev's CDN 403s python-urllib's default agent — always identify.
    hdrs = {"User-Agent": "super-coder-model-catalog/1.0", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read(MAX_HTTP_JSON_BYTES + 1)
    if len(body) > MAX_HTTP_JSON_BYTES:
        raise _ModelCatalogueLimitError(
            "model catalogue response exceeds safety limits"
        )
    return json.loads(body.decode())


def _entry(mid: str, release_date: str = "", name: str = "",
           family: str | None = None, *, source: str = "models.dev",
           availability: str = "advisory", provider: str | None = None,
           provider_model: str | None = None,
           supported_efforts: list[str] | None = None,
           default_effort: str | None = None,
           cli_version: str | None = None,
           selector_binding: dict | None = None,
           adapter_metadata: dict | None = None,
           native_variant_ids: dict[str, str] | None = None) -> dict:
    return {"id": mid, "release_date": release_date, "name": name or mid,
            "family": family, "source": source,
            "availability": availability, "provider": provider,
            "provider_model": provider_model or mid,
            "supported_efforts": supported_efforts or [],
            "default_effort": default_effort, "cli_version": cli_version,
            "selector_binding": selector_binding,
            "adapter_metadata": adapter_metadata or {},
            "native_variant_ids": native_variant_ids or {}}


def _from_models_dev(fetch) -> dict[str, list[dict]]:
    data = fetch(MODELS_DEV_URL)
    out: dict[str, list[dict]] = {}
    for harness, provider in HARNESS_PROVIDER.items():
        models = (data.get(provider) or {}).get("models") or {}
        entries = []
        for mid, m in models.items():
            full = f"{provider}/{mid}" if harness in PREFIXED_HARNESSES else mid
            entries.append(_entry(
                full, m.get("release_date") or "", m.get("name") or mid,
                m.get("family"), provider=provider, provider_model=mid))
        entries.sort(key=lambda e: e["release_date"], reverse=True)
        out[harness] = entries
    return out


def _families(harness: str, entries: list[dict]) -> list[dict]:
    """Retained compatibility metadata, not a picker selection surface.

    `latest` is the newest
    release in the family — except claude families with a CLI alias
    (opus/sonnet/haiku), where it is the alias itself, which self-tracks
    upstream so the stored value never goes stale. Entries from sources
    without family data (keyed APIs, opencode CLI) simply don't group —
    they stay reachable through model search."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("family"):
            groups.setdefault(e["family"], []).append(e)
    fams = []
    for fam, es in groups.items():
        newest = max(es, key=lambda x: x["release_date"] or "")
        label = fam.removeprefix("claude-") if harness == "claude" else fam
        latest = label if harness == "claude" and label in CLAUDE_ALIASES \
            else newest["id"]
        fams.append({"family": label, "latest": latest,
                     "release_date": newest["release_date"], "n": len(es)})
    fams.sort(key=lambda f: f["release_date"], reverse=True)
    return fams


def _from_provider_apis(fetch, env) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for harness, (var, url, hdrs) in PROVIDER_APIS.items():
        key = env.get(var)
        if not key:
            continue
        try:
            data = fetch(url, hdrs(key))
        except Exception:
            continue  # opportunistic — a bad key never degrades the catalog
        ids = [m.get("id") for m in data.get("data") or [] if m.get("id")]
        if ids:
            out[harness] = [
                _entry(i, source=f"{HARNESS_PROVIDER[harness]}-api",
                       provider=HARNESS_PROVIDER[harness]) for i in ids]
    return out


def _deepseek_provider_registry() -> tuple[dict, dict, str]:
    import deepseek_runtime  # noqa: PLC0415

    manifest = deepseek_runtime.load_runtime_manifest()
    registry_evidence = manifest["provider_adapters"]
    registry = deepseek_runtime.load_provider_adapter_registry(
        expected_sha256=registry_evidence["sha256"]
    )
    return manifest, registry["providers"], registry_evidence["sha256"]


def _deepseek_provider_endpoint(provider: str, adapter: dict, env) -> tuple[str, str]:
    base = adapter["endpoint_default"]
    endpoint_env = adapter.get("endpoint_env")
    if endpoint_env and env.get(endpoint_env):
        base = env[endpoint_env]
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{provider} base URL must be credential-free HTTP(S)")
    identity = base.rstrip("/")
    return identity, identity + adapter["discovery_path"]


def _deepseek_carrier_options(provider: str = "deepseek-official") -> dict[str, dict[str, str]]:
    _manifest, providers, _registry_digest = _deepseek_provider_registry()
    adapter = providers.get(provider)
    if not isinstance(adapter, dict):
        raise ValueError("DeepSeek provider route is not reviewed")
    named = adapter["named_efforts"]
    thinking = "enabled" if adapter["wire_mode"] == "deepseek-request-patch" else "omit"
    return {
        route_bindings.DEFAULT_EFFORT: {
            "thinking": "omit", "reasoningEffort": "omit",
        },
        **{
            effort: {"thinking": thinking, "reasoningEffort": effort}
            for effort in named
        },
    }


def _deepseek_provider_metadata(
    provider: str,
    model: str,
    endpoint_identity: str,
    discovery_evidence_digest: str,
    proof: dict,
) -> dict:
    import deepseek_runtime  # noqa: PLC0415

    manifest, providers, registry_digest = _deepseek_provider_registry()
    adapter = providers[provider]
    expected_runtime = (manifest.get("runtime") or {}).get("version")
    expected_source = (manifest.get("source") or {}).get("commit")
    expected_patch = (manifest.get("patch") or {}).get("sha256")
    expected_composition = adapter.get("composition_sha256")
    adapter_digest = route_bindings.digest_json(adapter)
    if (
        not isinstance(proof, dict)
        or proof.get("contract") != deepseek_runtime.PROVIDER_WIRE_CONTRACT
        or proof.get("provider") != provider
        or proof.get("model") != model
        or not isinstance(proof.get("proofs"), dict)
    ):
        raise _DeepSeekWireProofError(DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED)
    carrier_options = _deepseek_carrier_options(provider)
    wire_proofs = proof["proofs"]
    mappings = {}
    digests = {}
    for effort, expected_options in carrier_options.items():
        if effort == route_bindings.DEFAULT_EFFORT:
            mapping = {
                "omit": list(route_bindings.DEEPSEEK_OVERRIDE_FIELDS), "set": {},
            }
            expected_wire = {}
        elif adapter["wire_mode"] == "deepseek-request-patch":
            mapping = {
                "omit": [],
                "set": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": effort,
                },
            }
            expected_wire = dict(mapping["set"])
        else:
            mapping = {
                "omit": ["thinking"],
                "set": {"reasoning_effort": effort},
            }
            expected_wire = dict(mapping["set"])
        mappings[effort] = mapping
        item = wire_proofs.get(effort)
        expected_native = {
            "event_type": "provider.request",
            "provider": provider,
            "model": model,
            "reasoning_effort": (
                None if effort == route_bindings.DEFAULT_EFFORT else effort
            ),
            "purpose": "conversation",
        }
        expected_purpose_proofs = {
            purpose: {
                "wire_options": expected_wire,
                "native_request": {
                    **expected_native,
                    "purpose": purpose,
                },
            }
            for purpose in deepseek_runtime.PROVIDER_WIRE_PURPOSES
        }
        digest_payload = {
            key: item.get(key) if isinstance(item, dict) else None
            for key in (
                "contract", "provider", "model", "effort", "provider_options",
                "wire_options", "native_request", "purpose_proofs",
                "runtime_version", "source_commit",
                "patch_sha256", "composition_sha256", "provider_registry_sha256",
                "provider_adapter_id", "provider_adapter_digest",
            )
        }
        if (
            not isinstance(item, dict)
            or item.get("contract") != deepseek_runtime.PROVIDER_WIRE_CONTRACT
            or item.get("provider") != provider
            or item.get("model") != model
            or item.get("effort") != effort
            or item.get("provider_options") != expected_options
            or item.get("wire_options") != expected_wire
            or item.get("native_request") != expected_native
            or item.get("purpose_proofs") != expected_purpose_proofs
            or item.get("runtime_version") != expected_runtime
            or item.get("source_commit") != expected_source
            or item.get("patch_sha256") != expected_patch
            or item.get("composition_sha256") != expected_composition
            or item.get("provider_registry_sha256") != registry_digest
            or item.get("provider_adapter_id") != adapter["adapter_id"]
            or item.get("provider_adapter_digest") != adapter_digest
            or not isinstance(item.get("digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["digest"]) is None
            or item["digest"] != route_bindings.digest_json(digest_payload)
        ):
            raise _DeepSeekWireProofError(DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED)
        digests[effort] = item["digest"]
    return {
        "provider_route": provider,
        "provider_adapter_id": adapter["adapter_id"],
        "provider_adapter_digest": adapter_digest,
        "provider_registry_sha256": registry_digest,
        "credential_kind": adapter["credential_kind"],
        "endpoint_identity": endpoint_identity,
        "discovery_evidence_digest": discovery_evidence_digest,
        "transport_contract": route_bindings.DEEPSEEK_TRANSPORT_CONTRACT,
        "provider_options_by_effort": mappings,
        "wire_contract": deepseek_runtime.PROVIDER_WIRE_CONTRACT,
        "wire_evidence_by_effort": digests,
        "runtime_version": expected_runtime,
        "source_commit": expected_source,
        "patch_sha256": expected_patch,
        "composition_sha256": expected_composition,
    }


def _deepseek_manifest_identity() -> tuple[str, str]:
    import deepseek_runtime  # noqa: PLC0415

    manifest = deepseek_runtime.load_runtime_manifest()
    sdk = manifest.get("sdk") or {}
    runtime = manifest.get("runtime") or {}
    if sdk.get("version") != runtime.get("version"):
        raise ValueError("DeepSeek SDK/runtime versions are not aligned")
    version = sdk.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("DeepSeek runtime has no exact version")
    commit = (manifest.get("source") or {}).get("commit")
    if (
        not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
    ):
        raise ValueError("DeepSeek runtime has no exact upstream commit")
    return version, commit


def _from_deepseek_provider(
    provider: str,
    fetch,
    env,
    wire_probe=None,
    *,
    selector=None,
    discovery_out=None,
) -> list[dict]:
    """Read exact models through one reviewed provider-specific credential."""
    _manifest, providers, registry_digest = _deepseek_provider_registry()
    adapter = providers.get(provider)
    if not isinstance(adapter, dict):
        raise ValueError("DeepSeek provider route is not reviewed")
    key = env.get(adapter["credential_source_env"])
    if not isinstance(key, str) or not key.strip():
        return []
    endpoint_identity, url = _deepseek_provider_endpoint(provider, adapter, env)
    payload = fetch(url, {"Authorization": f"Bearer {key}"})
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("DeepSeek model response has no data list")
    if len(rows) > adapter["max_models"]:
        raise _ModelCatalogueLimitError(DEEPSEEK_DISCOVERY_LIMIT_ERROR)
    version, source_commit = _deepseek_manifest_identity()
    if wire_probe is None:
        import deepseek_runtime  # noqa: PLC0415

        wire_probe = deepseek_runtime.provider_wire_evidence
    entries = []
    seen = set()
    exact_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise _DeepSeekDiscoveryEvidenceError(
                DEEPSEEK_DISCOVERY_EVIDENCE_INVALID
            )
        model = row.get("id")
        if not isinstance(model, str) or not model or model != model.strip():
            raise _DeepSeekDiscoveryEvidenceError(
                DEEPSEEK_DISCOVERY_EVIDENCE_INVALID
            )
        if model in seen:
            raise _DeepSeekDiscoveryEvidenceError(
                DEEPSEEK_DISCOVERY_EVIDENCE_INVALID
            )
        if len(model) > DEEPSEEK_MODEL_ID_MAX_CHARS:
            raise _ModelCatalogueLimitError(DEEPSEEK_DISCOVERY_LIMIT_ERROR)
        seen.add(model)
        exact_rows.append((row, model))
    discovery_evidence_digest = route_bindings.digest_json({
        "provider": provider,
        "endpoint_identity": endpoint_identity,
        "models": sorted(model for _row, model in exact_rows),
        "provider_registry_sha256": registry_digest,
    })
    if discovery_out is not None:
        discovery_out.update({
            "provider": provider,
            "selectors": [
                f"{provider}/{model}" if adapter["selector_prefix"] else model
                for _row, model in exact_rows
            ],
            "discovery_evidence_digest": discovery_evidence_digest,
        })
    configured = adapter["model_selectors"]
    if selector is not None:
        exact_rows = [
            (row, model)
            for row, model in exact_rows
            if (
                f"{provider}/{model}" if adapter["selector_prefix"] else model
            ) == selector
        ]
        if not exact_rows:
            raise _DeepSeekExactModelAbsentError(DEEPSEEK_EXACT_MODEL_ABSENT)
    elif configured:
        configured_set = set(configured)
        if not configured_set.issubset(seen):
            raise _DeepSeekExactModelAbsentError(DEEPSEEK_EXACT_MODEL_ABSENT)
        exact_rows = [
            (row, model) for row, model in exact_rows if model in configured_set
        ]
    else:
        if len(exact_rows) > adapter["wire_proof_budget"]:
            exact_rows = sorted(exact_rows, key=lambda item: item[1])[
                : adapter["wire_proof_budget"]
            ]
    if len(exact_rows) > adapter["wire_proof_budget"]:
        raise _DeepSeekWireProofError(DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED)
    for row, model in exact_rows:
        route_selector = f"{provider}/{model}" if adapter["selector_prefix"] else model
        try:
            proof = wire_probe(
                provider, model, _deepseek_carrier_options(provider), env=env
            )
            metadata = _deepseek_provider_metadata(
                provider, model, endpoint_identity, discovery_evidence_digest, proof
            )
        except Exception:  # noqa: BLE001 (proof diagnostics stay local/redacted)
            continue
        entries.append(_entry(
            route_selector,
            name=(row.get("name") if isinstance(row, dict) else None) or model,
            source=(DEEPSEEK_SOURCE if provider == "deepseek-official" else OLLAMA_CLOUD_SOURCE),
            availability="available",
            provider=provider,
            provider_model=model,
            supported_efforts=list(adapter["named_efforts"]),
            default_effort=("high" if "high" in adapter["named_efforts"] else None),
            cli_version=version,
            selector_binding={
                "kind": "authenticated-provider-model",
                "selector": route_selector,
                "provider_model": model,
                "provider_route": provider,
                "provider_adapter_id": adapter["adapter_id"],
                "provider_adapter_digest": metadata["provider_adapter_digest"],
                "provider_registry_sha256": registry_digest,
                "credential_kind": adapter["credential_kind"],
                "endpoint_identity": endpoint_identity,
                "discovery_evidence_digest": discovery_evidence_digest,
                "models_url": url,
                "runtime_source_commit": source_commit,
                "provider_wire_contract": metadata["wire_contract"],
                "provider_wire_digests": metadata["wire_evidence_by_effort"],
            },
            adapter_metadata=metadata,
        ))
    if exact_rows and not entries:
        selectors = {
            f"{provider}/{item}" if adapter["selector_prefix"] else item
            for _row, item in exact_rows
        }
        if selector is not None and selector not in selectors:
            raise ValueError("DeepSeek authenticated endpoint omitted exact model")
        raise _DeepSeekWireProofError(DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED)
    if not entries:
        raise ValueError("DeepSeek authenticated endpoint returned no exact models")
    return entries


def _from_deepseek_api(fetch, env, wire_probe=None, *, selector=None) -> list[dict]:
    """Compatibility wrapper for the retained DeepSeek-official route."""
    return _from_deepseek_provider(
        "deepseek-official", fetch, env, wire_probe=wire_probe, selector=selector
    )


def _cli_version(binary: str, run) -> str | None:
    try:
        r = run([binary, "--version"], capture_output=True, text=True,
                timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or r.stderr).strip().splitlines()[0] or None


def _from_claude_cli(run) -> list[dict]:
    """Claude's aliases are the portable local launch selectors.

    Claude Code does not expose an account-scoped list-models command, so the
    installed CLI proves selector syntax while models.dev supplies concrete
    family members for advisory browsing.
    """
    if not shutil.which("claude"):
        return []
    version = _cli_version("claude", run)
    return [
        _entry(alias, name=f"Claude {alias.title()} (alias)",
               family=f"claude-{alias}", source="claude-cli",
               availability="available", provider="anthropic",
               supported_efforts=["low", "medium", "high"],
               default_effort="high", cli_version=version)
        for alias in CLAUDE_ALIASES
    ]


def _from_codex_cache(env, run) -> list[dict]:
    """Read the signed-in Codex CLI's own model cache.

    This is stronger evidence than the public OpenAI model list: it describes
    what this installed ChatGPT-backed CLI was actually offered, including its
    supported reasoning-effort levels.
    """
    if not shutil.which("codex"):
        return []
    root = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
    try:
        data = json.loads((root / "models_cache.json").read_text())
    except Exception:  # noqa: BLE001  (missing/corrupt = no local evidence)
        return []
    version = _cli_version("codex", run)
    entries = []
    for m in data.get("models") or []:
        mid = m.get("slug")
        if not mid or m.get("visibility") == "hide":
            continue
        efforts = [e.get("effort") for e in m.get("supported_reasoning_levels") or []
                   if e.get("effort")]
        entries.append(_entry(
            mid, name=m.get("display_name") or mid, family=m.get("family"),
            source="codex-cache", availability="available", provider="openai",
            supported_efforts=efforts,
            default_effort=m.get("default_reasoning_level"), cli_version=version))
    return entries


def _from_kimi_config(env, run) -> list[dict]:
    """Read Kimi's exact user-defined aliases without touching credentials."""
    if not shutil.which("kimi") or not toml_compat.AVAILABLE:
        return []
    root = Path(env.get("KIMI_CODE_HOME") or (Path.home() / ".kimi-code"))
    try:
        data = toml_compat.loads((root / "config.toml").read_text())
    except Exception:  # noqa: BLE001
        return []
    version = _cli_version("kimi", run)
    entries = []
    for alias, cfg in (data.get("models") or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("model"):
            continue
        effective = {**cfg, **(cfg.get("overrides") or {})}
        entries.append(_entry(
            alias, name=effective.get("display_name") or cfg["model"],
            family=f"kimi-{cfg['model']}", source="kimi-config",
            availability="available", provider=cfg.get("provider"),
            provider_model=cfg["model"],
            supported_efforts=effective.get("support_efforts") or [],
            default_effort=effective.get("default_effort"),
            cli_version=version))
    return entries


def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
    seen = {e["id"] for e in base}
    return base + [e for e in extra if e["id"] not in seen]


def _prefer(preferred: list[dict], advisory: list[dict]) -> list[dict]:
    """Put locally authoritative entries first and replace duplicate ids."""
    return _merge(preferred, advisory)


def build(
    fetch=_http_json,
    env=os.environ,
    run=subprocess.run,
    deepseek_wire_probe=None,
    deepseek_selector=None,
) -> dict:
    """One live sweep across all sources. Raises only if EVERY source fails —
    partial results (e.g. models.dev down but a keyed API up) still count."""
    harnesses: dict[str, list[dict]] = {}
    harness_errors: dict[str, str] = {}
    sources: list[str] = []
    errors: list[str] = []
    try:
        harnesses = _from_models_dev(fetch)
        sources.append("models.dev")
    except Exception as e:  # noqa: BLE001
        errors.append(f"models.dev: {e}")
    for harness, extra in _from_provider_apis(fetch, env).items():
        harnesses[harness] = _merge(harnesses.get(harness, []), extra)
        sources.append(f"{HARNESS_PROVIDER[harness]}-api")
    deepseek = []
    authenticated_deepseek_routes = []
    attempted_providers = []
    provider_errors = []
    try:
        _manifest, deepseek_providers, _registry_digest = _deepseek_provider_registry()
    except Exception:  # noqa: BLE001 (DeepSeek failure remains route-local)
        deepseek_providers = {}
        harnesses.setdefault("deepseek", [])
        harness_errors["deepseek"] = DEEPSEEK_PROVIDER_REGISTRY_INVALID
        errors.append(
            f"deepseek-provider-registry: {DEEPSEEK_PROVIDER_REGISTRY_INVALID}"
        )
    for provider, adapter in deepseek_providers.items():
        if not env.get(adapter["credential_source_env"]):
            continue
        attempted_providers.append(provider)
        source = DEEPSEEK_SOURCE if provider == "deepseek-official" else OLLAMA_CLOUD_SOURCE
        selected_for_provider = (
            deepseek_selector
            if (
                (
                    provider == "ollama-cloud"
                    and str(deepseek_selector).startswith("ollama-cloud/")
                )
                or (
                    provider == "deepseek-official"
                    and deepseek_selector is not None
                    and not str(deepseek_selector).startswith("ollama-cloud/")
                )
            )
            else None
        )
        discovery = {}
        try:
            observed = _from_deepseek_provider(
                provider,
                fetch,
                env,
                wire_probe=deepseek_wire_probe,
                selector=selected_for_provider,
                discovery_out=discovery,
            )
        except _ModelCatalogueLimitError:
            provider_errors.append((source, DEEPSEEK_DISCOVERY_LIMIT_ERROR))
            continue
        except _DeepSeekDiscoveryEvidenceError:
            provider_errors.append((source, DEEPSEEK_DISCOVERY_EVIDENCE_INVALID))
            continue
        except _DeepSeekExactModelAbsentError:
            provider_errors.append((source, DEEPSEEK_EXACT_MODEL_ABSENT))
            continue
        except _DeepSeekWireProofError:
            provider_errors.append((source, DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED))
            continue
        except urllib.error.HTTPError as exc:
            stable_error = (
                DEEPSEEK_AUTHENTICATION_ERROR
                if exc.code in {401, 403}
                else DEEPSEEK_DISCOVERY_ERROR
            )
            provider_errors.append((source, stable_error))
            continue
        except Exception:  # noqa: BLE001 (secret-bearing discovery is redacted)
            provider_errors.append((source, DEEPSEEK_DISCOVERY_ERROR))
            continue
        deepseek = _merge(deepseek, observed)
        authenticated_deepseek_routes.append(discovery)
        if observed:
            sources.append(source)
    if attempted_providers:
        harnesses["deepseek"] = deepseek
        errors.extend(f"{source}: {error}" for source, error in provider_errors)
        if provider_errors and not deepseek:
            harness_errors["deepseek"] = provider_errors[-1][1]
    local = {
        "claude": _from_claude_cli(run),
        "codex": _from_codex_cache(env, run),
        "kimi": _from_kimi_config(env, run),
    }
    for harness, entries in local.items():
        if not entries:
            continue
        harnesses[harness] = _prefer(entries, harnesses.get(harness, []))
        sources.append(entries[0]["source"])
    if not sources:
        raise RuntimeError("; ".join(errors) or "no catalog sources available")
    result = {"v": PAYLOAD_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
            "partial": bool(errors),
            **({"errors": errors} if errors else {}),
            "harnesses": {h: {"families": _families(h, entries),
                              "models": entries,
                              **({"error": harness_errors[h]}
                                 if h in harness_errors else {})}
                          for h, entries in harnesses.items()}}
    if "deepseek" in result["harnesses"]:
        result["harnesses"]["deepseek"][
            "authenticated_routes"
        ] = authenticated_deepseek_routes
    return result


def _load_cache() -> dict | None:
    try:
        cached = json.loads(CACHE.read_text())
    except Exception:  # noqa: BLE001  (missing or corrupt — both mean "no cache")
        return None
    # A cache written by another payload version would hand the client a
    # shape it can't render — ignore it entirely.
    return cached if cached.get("v") == PAYLOAD_VERSION else None


def _authoritative_generation(con):
    if con is None:
        return _GENERATION_TABLE_UNAVAILABLE
    try:
        row = con.execute(
            "SELECT generation_id FROM model_catalog_generations "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return _GENERATION_TABLE_UNAVAILABLE
        raise
    return row[0] if row is not None else None


def _cache_matches_authority(cached: dict, con) -> bool:
    generation = _authoritative_generation(con)
    return (
        generation is _GENERATION_TABLE_UNAVAILABLE
        or cached.get("catalogue_generation") == generation
    )


@contextmanager
def _publication_lock():
    """Serialize generation projection, cache replacement, and its receipt."""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE.with_name(f"{CACHE.name}.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _publish_cache_locked(payload: dict, con=None) -> bool:
    """Replace the cache while the caller owns the publication lock."""
    temporary = CACHE.with_name(
        f".{CACHE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    serialized = json.dumps(payload, indent=1) + "\n"
    try:
        with temporary.open("x") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        authoritative = _authoritative_generation(con)
        if (
            authoritative is not _GENERATION_TABLE_UNAVAILABLE
            and payload.get("catalogue_generation") != authoritative
        ):
            return False
        os.replace(temporary, CACHE)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _publish_cache(payload: dict, con=None) -> bool:
    """Atomically replace the cache only when this payload is authoritative."""
    with _publication_lock():
        return _publish_cache_locked(payload, con)


def _finish_cache_publication(
    candidate: dict, response: dict, con, opencode_provider, *,
    publication_locked: bool = False,
) -> dict:
    for field in (
        "catalogue_generation", "generation_state", "generation_published",
        "refresh_started_at", "refresh_completed_at",
    ):
        if field in response:
            candidate[field] = response[field]
    publish = _publish_cache_locked if publication_locked else _publish_cache
    if publish(candidate, con):
        return response

    if "generation_published" in response:
        response["generation_published"] = False
    winner = _load_cache()
    if winner and _cache_matches_authority(winner, con):
        return _served(_with_live_opencode(
            {**winner, "stale": bool(winner.get("stale"))},
            opencode_provider,
        ), con)
    return {**response, "stale": True,
            "error": "Catalogue changed during refresh; retry"}


def _fresh(cached: dict) -> bool:
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
        return age.total_seconds() < TTL_HOURS * 3600
    except Exception:  # noqa: BLE001
        return False


_FLOOR_FAMILY = {"fable": "claude-fable", "opus": "claude-opus",
                 "sonnet": "claude-sonnet", "haiku": "claude-haiku"}


def _floor() -> dict[str, dict]:
    out = {}
    for h, ids in STATIC_FLOOR.items():
        entries = [_entry(i, family=_FLOOR_FAMILY.get(i), source="static",
                          availability="fallback") for i in ids]
        out[h] = {"families": _families(h, entries), "models": entries}
    return out


def _headless_supported(harness: str) -> bool:
    try:
        cfg = json.loads((ADAPTERS / harness / "adapter.json").read_text())
    except Exception:  # noqa: BLE001
        return False
    return bool((cfg.get("headless") or {}).get("launch"))


def harness_runtime_status(harness: str) -> dict:
    """Return exact version-bounded runtime evidence for one shipped harness."""
    if harness == "deepseek":
        try:
            import deepseek_runtime  # noqa: PLC0415

            status = deepseek_runtime.runtime_status()
            manifest = deepseek_runtime.load_runtime_manifest()
            verified = str((manifest.get("sdk") or {}).get("version") or "") or None
            scope = harness_versions.runtime_scope()
            version = status.sdk_version or status.runtime_version
            return {
                "harness": harness,
                **scope,
                "version": version,
                "observed_version": version,
                "compatibility": "verified" if status.available else None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": verified,
                "error": status.error,
            }
        except Exception:  # noqa: BLE001
            return {
                "harness": harness,
                **harness_versions.runtime_scope(),
                "version": None,
                "observed_version": None,
                "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": "HARNESS_PROBE_FAILED",
            }
    try:
        return dict(
            harness_versions.compatibility_status((harness,)).get(harness) or {}
        )
    except Exception:  # noqa: BLE001
        return {
            "harness": harness,
            **harness_versions.runtime_scope(),
            "version": None,
            "compatibility": None,
            "minimum_version": None,
            "maximum_version_exclusive": None,
            "verified_version": None,
            "error": "HARNESS_PROBE_FAILED",
        }


def _evidence_kind(harness: str, source: str) -> str | None:
    return {
        ("claude", "claude-cli"): "claude-portable-manifest",
        ("codex", "codex-cache"): "codex-model-cache",
        ("deepseek", DEEPSEEK_SOURCE): "deepseek-provider-authenticated-models-v2",
        ("deepseek", OLLAMA_CLOUD_SOURCE): "deepseek-provider-authenticated-models-v2",
        ("kimi", "kimi-config"): "kimi-alias-config",
        ("opencode", "opencode-provider-api"): "opencode-connected-variant",
    }.get((harness, source))


_SEMVER_TOKEN = re.compile(
    r"(?:(?:claude|codex(?:-cli)?|opencode|vibe|kimi) )?v?"
    r"((\d+\.\d+\.\d+)(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?)"
)


def _parsed_version(value: object) -> str | None:
    """Return one complete SemVer token, never a same-core substring."""
    match = _SEMVER_TOKEN.fullmatch(value.strip()) if isinstance(value, str) else None
    return match.group(1) if match else None


def _observed_version(status: dict) -> str | None:
    value = status.get("observed_version", status.get("version"))
    return value if isinstance(value, str) and value.strip() else None


def _support_state(status: dict) -> str | None:
    if status.get("error") or not _observed_version(status):
        return None
    return "tested" if (
        status.get("version")
        and status.get("version") == status.get("verified_version")
        and status.get("compatibility") == "verified"
    ) else "best-effort"


def _compatible_route_status(harness: str, entry: dict,
                             status: dict) -> bool:
    """Require executable and exact source evidence, never a version range."""
    if _evidence_kind(harness, entry.get("source") or "unknown") is None:
        return True
    if not isinstance(status, dict):
        return False
    return bool(
        not status.get("error")
        and _observed_version(status)
        and (
            entry.get("cli_version") == _observed_version(status)
            or (
                status.get("version")
                and _parsed_version(entry.get("cli_version")) == status.get("version")
            )
        )
    )


def _compatibility_error(harness: str, entry: dict, status: dict) -> str:
    status = status if isinstance(status, dict) else {}
    return (
        f"harness compatibility rejected {harness}/{entry['id']}: "
        f"version={status.get('version') or 'unknown'} "
        f"compatibility={status.get('compatibility') or 'none'} "
        f"error={status.get('error') or 'none'}"
    )


def _entry_evidence(harness: str, entry: dict,
                    status: dict | None = None) -> dict:
    source = entry.get("source") or "unknown"
    selector_binding = entry.get("selector_binding") or {
        "kind": {
            "claude": "portable-alias",
            "codex": "exact-model",
            "kimi": "configured-alias",
            "opencode": "connected-model",
        }.get(harness, "advisory"),
        "selector": entry["id"],
        "provider_model": entry.get("provider_model"),
    }
    efforts = list(dict.fromkeys(
        value for value in (entry.get("supported_efforts") or [])
        if isinstance(value, str) and value == value.strip().lower() and value
    ))
    catalogue_adapter_metadata = entry.get("adapter_metadata") or {}
    variants_by_effort = (
        catalogue_adapter_metadata.get("variant_options_by_effort") or {}
        if isinstance(catalogue_adapter_metadata, dict)
        else {}
    )

    def binding_adapter_metadata(effort: str) -> dict:
        if harness == "deepseek":
            mappings = catalogue_adapter_metadata.get(
                "provider_options_by_effort"
            ) or {}
            wire_digests = catalogue_adapter_metadata.get(
                "wire_evidence_by_effort"
            ) or {}
            return {
                "provider_route": catalogue_adapter_metadata.get("provider_route"),
                "provider_adapter_id": catalogue_adapter_metadata.get("provider_adapter_id"),
                "provider_adapter_digest": catalogue_adapter_metadata.get("provider_adapter_digest"),
                "provider_registry_sha256": catalogue_adapter_metadata.get("provider_registry_sha256"),
                "credential_kind": catalogue_adapter_metadata.get("credential_kind"),
                "endpoint_identity": catalogue_adapter_metadata.get("endpoint_identity"),
                "discovery_evidence_digest": catalogue_adapter_metadata.get("discovery_evidence_digest"),
                "transport_contract": catalogue_adapter_metadata.get(
                    "transport_contract"
                ),
                "provider_options": mappings.get(effort),
                "wire_evidence_digest": wire_digests.get(effort),
                "runtime_version": catalogue_adapter_metadata.get("runtime_version"),
                "source_commit": catalogue_adapter_metadata.get("source_commit"),
                "patch_sha256": catalogue_adapter_metadata.get("patch_sha256"),
                "composition_sha256": catalogue_adapter_metadata.get("composition_sha256"),
            }
        if harness != "opencode" or not variants_by_effort:
            return catalogue_adapter_metadata
        return {
            "compatibility_manifest": catalogue_adapter_metadata.get(
                "compatibility_manifest"
            ),
            "provider_family": catalogue_adapter_metadata.get("provider_family"),
            "variant_options": variants_by_effort.get(effort),
        }

    base = {
        "harness": harness,
        "selector": entry["id"],
        "provider": entry.get("provider"),
        "provider_model": entry.get("provider_model"),
        "source": source,
        "cli_version": entry.get("cli_version"),
        "selector_binding": selector_binding,
        "adapter_metadata": catalogue_adapter_metadata,
        "harness_version": _observed_version(status or {}),
        "harness_compatibility": (status or {}).get("compatibility"),
        "harness_support_state": _support_state(status or {}),
        "adapter_minimum_version": (status or {}).get("minimum_version"),
        "adapter_maximum_version_exclusive": (status or {}).get(
            "maximum_version_exclusive"
        ),
        "adapter_verified_version": (status or {}).get("verified_version"),
    }
    source_fingerprint = route_bindings.digest_json({
        **base,
        "supported_efforts": efforts,
        "default_effort": entry.get("default_effort"),
        "native_variant_ids": entry.get("native_variant_ids") or {},
    })
    metadata_efforts = [
        *([route_bindings.DEFAULT_EFFORT] if harness == "deepseek" else []),
        *efforts,
    ]
    effort_metadata = {
        "supported": efforts,
        "default": entry.get("default_effort"),
        "digests": {
            effort: route_bindings.digest_json({
                **base,
                "effort": effort,
                "binding_adapter_metadata": binding_adapter_metadata(effort),
            })
            for effort in efforts
        },
        "native_variant_ids": entry.get("native_variant_ids") or {},
        "adapter_metadata_by_effort": {
            effort: binding_adapter_metadata(effort) for effort in metadata_efforts
        },
    }
    return {
        "evidence_kind": _evidence_kind(harness, source),
        "evidence_digest": route_bindings.digest_json({
            **base, "effort_metadata": effort_metadata
        }),
        "source_fingerprint": source_fingerprint,
        "harness_version": _observed_version(status or {}),
        "harness_compatibility": (status or {}).get("compatibility"),
        "harness_support_state": _support_state(status or {}),
        "selector_binding": selector_binding,
        "effort_metadata": effort_metadata,
        "adapter_metadata": entry.get("adapter_metadata") or {},
        "supported_efforts": efforts,
    }


def _project_route_support(payload: dict, harnesses: dict[str, dict]) -> dict:
    """Attach route-local support metadata without changing route admission."""
    for harness, block in (payload.get("harnesses") or {}).items():
        status = harnesses.get(harness) or {}
        observed_version = _observed_version(status)
        support_state = _support_state(status)
        for entry in block.get("models") or []:
            if _evidence_kind(harness, entry.get("source") or "unknown"):
                entry["harness_version"] = observed_version
                entry["harness_support_state"] = support_state
    return payload


def _legacy_persist_routes(con, payload: dict) -> None:
    """Compatibility for an engine process crossing the 0212 migration."""
    try:
        con.execute("UPDATE model_routes SET stale=1, last_error=?",
                    (payload.get("error"),))
    except Exception:
        return
    seen_at = payload.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    stale = int(bool(payload.get("stale")))
    for harness, block in (payload.get("harnesses") or {}).items():
        headless = int(_headless_supported(harness))
        for entry in block.get("models") or []:
            efforts = entry.get("supported_efforts") or []
            con.execute(
                "INSERT INTO model_routes ("
                "harness, selector, provider, provider_model, display_name, family, "
                "source, availability, headless_supported, high_effort_supported, "
                "default_effort, supported_efforts, cli_version, last_seen_at, stale, "
                "last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(harness, selector) DO UPDATE SET "
                "provider=excluded.provider, provider_model=excluded.provider_model, "
                "display_name=excluded.display_name, family=excluded.family, "
                "source=excluded.source, availability=excluded.availability, "
                "headless_supported=excluded.headless_supported, "
                "high_effort_supported=excluded.high_effort_supported, "
                "default_effort=excluded.default_effort, "
                "supported_efforts=excluded.supported_efforts, "
                "cli_version=excluded.cli_version, last_seen_at=excluded.last_seen_at, "
                "stale=excluded.stale, last_error=excluded.last_error",
                (harness, entry["id"], entry.get("provider"),
                 entry.get("provider_model"), entry.get("name"), entry.get("family"),
                 entry.get("source") or "unknown",
                 entry.get("availability") or "advisory", headless,
                 int("high" in efforts), entry.get("default_effort"),
                 json.dumps(efforts), entry.get("cli_version"), seen_at, stale,
                 payload.get("error")))
    con.commit()


def persist_routes(con, payload: dict, *, publication_locked: bool = False) -> None:
    """Append an attempt; only the newest completed attempt changes routes."""
    if not publication_locked:
        with _publication_lock():
            persist_routes(con, payload, publication_locked=True)
        return
    try:
        con.execute("SELECT 1 FROM model_catalog_generations LIMIT 1")
    except Exception:
        _legacy_persist_routes(con, payload)
        return

    started_at = payload.get("refresh_started_at") or payload.get("fetched_at") \
        or datetime.now(timezone.utc).isoformat()
    completed_at = payload.get("refresh_completed_at") or payload.get("fetched_at") \
        or datetime.now(timezone.utc).isoformat()
    generation_id = uuid.uuid4().hex
    # A best-effort source failure is partial, not stale. Publish the routes
    # that were freshly observed and leave only the missing harness's prior
    # routes stale. A genuinely stale/fallback payload still fails closed.
    failed = bool(payload.get("stale"))
    state = "failed" if failed else "successful"
    verification = payload.get("verification") or {}
    harness_statuses = verification.get("harnesses") or {}
    source_fingerprints: dict[str, str] = {}
    carried_deepseek_routes: dict[str, str] = {}
    deepseek_block = (payload.get("harnesses") or {}).get("deepseek") or {}
    authenticated_routes = {
        item.get("provider"): item
        for item in deepseek_block.get("authenticated_routes") or []
        if isinstance(item, dict)
    }
    if authenticated_routes and not failed:
        for row in con.execute(
            "SELECT selector,provider,selector_binding,source_fingerprint "
            "FROM model_routes WHERE harness='deepseek' AND stale=0"
        ).fetchall():
            route = dict(row)
            authenticated = authenticated_routes.get(route["provider"])
            try:
                binding = json.loads(route["selector_binding"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(authenticated, dict)
                and route["selector"] in (authenticated.get("selectors") or [])
                and binding.get("discovery_evidence_digest")
                == authenticated.get("discovery_evidence_digest")
            ):
                carried_deepseek_routes[route["selector"]] = route[
                    "source_fingerprint"
                ]
                source_fingerprints[
                    f"deepseek/{route['selector']}"
                ] = route["source_fingerprint"]
    evidence_by_route: dict[tuple[str, str], dict] = {}
    rejected_routes: dict[tuple[str, str], str] = {}
    for harness, block in (payload.get("harnesses") or {}).items():
        for entry in block.get("models") or []:
            status = harness_statuses.get(harness) or {}
            if (
                (harness == "deepseek" and entry.get("availability") != "available")
                or not _compatible_route_status(harness, entry, status)
            ):
                rejected_routes[(harness, entry["id"])] = _compatibility_error(
                    harness, entry, status
                )
                continue
            evidence_status = status if _evidence_kind(
                harness, entry.get("source") or "unknown"
            ) else None
            evidence = _entry_evidence(harness, entry, evidence_status)
            evidence_by_route[(harness, entry["id"])] = evidence
            source_fingerprints[f"{harness}/{entry['id']}"] = evidence[
                "source_fingerprint"
            ]
    harness_errors = {
        harness: block["error"]
        for harness, block in (payload.get("harnesses") or {}).items()
        if isinstance(block, dict) and isinstance(block.get("error"), str)
    }
    error_summary = {
        "error": payload.get("error"),
        "errors": payload.get("errors") or [],
        "harness_errors": harness_errors,
    } if failed or payload.get("partial") else None
    payload_digest = route_bindings.digest_json({
        "v": payload.get("v", PAYLOAD_VERSION),
        "fetched_at": payload.get("fetched_at"),
        "sources": payload.get("sources") or [],
        "harnesses": payload.get("harnesses") or {},
        "harness_versions": harness_statuses,
        "state": state,
    })

    with con:
        con.execute(
            "INSERT INTO model_catalog_generations ("
            "generation_id,payload_version,contract_version,started_at,completed_at,"
            "state,runtime,source_summary,harness_versions,source_fingerprints,"
            "error_summary,payload_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                generation_id, payload.get("v", PAYLOAD_VERSION), 2,
                started_at, completed_at, state,
                verification.get("runtime") or (
                    "sandbox" if os.environ.get("SC_SANDBOX") else "host"
                ),
                route_bindings.canonical_json(payload.get("sources") or []),
                route_bindings.canonical_json(harness_statuses),
                route_bindings.canonical_json(source_fingerprints),
                route_bindings.canonical_json(error_summary) if error_summary else None,
                payload_digest,
            ),
        )
        authoritative = con.execute(
            "SELECT generation_id FROM model_catalog_generations "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()
        publish_projection = authoritative[0] == generation_id
        if publish_projection and failed:
            con.execute(
                "UPDATE model_routes SET stale=1,last_error=?",
                (payload.get("error") or "; ".join(payload.get("errors") or [])
                 or "partial model refresh",),
            )
        elif publish_projection:
            con.execute(
                "UPDATE model_routes SET stale=1,last_error='not observed in latest generation'"
            )
            for harness, block in (payload.get("harnesses") or {}).items():
                if block.get("error"):
                    con.execute(
                        "UPDATE model_routes SET last_error=? WHERE harness=?",
                        (block["error"], harness),
                    )
                headless = int(_headless_supported(harness))
                for entry in block.get("models") or []:
                    route_key = (harness, entry["id"])
                    if route_key in rejected_routes:
                        con.execute(
                            "UPDATE model_routes SET stale=1,last_error=? "
                            "WHERE harness=? AND selector=?",
                            (rejected_routes[route_key], harness, entry["id"]),
                        )
                        continue
                    evidence = evidence_by_route[route_key]
                    efforts = evidence["supported_efforts"]
                    con.execute(
                        "INSERT INTO model_routes ("
                        "harness,selector,provider,provider_model,display_name,family,"
                        "source,availability,headless_supported,high_effort_supported,"
                        "default_effort,supported_efforts,cli_version,last_seen_at,stale,"
                        "last_error,generation_id,evidence_kind,evidence_digest,"
                "source_fingerprint,harness_version,harness_compatibility,harness_support_state,"
                        "selector_binding,effort_metadata,"
                        "adapter_metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?) ON CONFLICT(harness,selector) DO UPDATE SET "
                        "provider=excluded.provider,provider_model=excluded.provider_model,"
                        "display_name=excluded.display_name,family=excluded.family,"
                        "source=excluded.source,availability=excluded.availability,"
                        "headless_supported=excluded.headless_supported,"
                        "high_effort_supported=excluded.high_effort_supported,"
                        "default_effort=excluded.default_effort,"
                        "supported_efforts=excluded.supported_efforts,"
                        "cli_version=excluded.cli_version,last_seen_at=excluded.last_seen_at,"
                        "stale=0,last_error=NULL,generation_id=excluded.generation_id,"
                        "evidence_kind=excluded.evidence_kind,"
                        "evidence_digest=excluded.evidence_digest,"
                        "source_fingerprint=excluded.source_fingerprint,"
                        "harness_version=excluded.harness_version,"
                        "harness_compatibility=excluded.harness_compatibility,"
                        "harness_support_state=excluded.harness_support_state,"
                        "selector_binding=excluded.selector_binding,"
                        "effort_metadata=excluded.effort_metadata,"
                        "adapter_metadata=excluded.adapter_metadata",
                        (
                            harness, entry["id"], entry.get("provider"),
                            entry.get("provider_model"), entry.get("name"),
                            entry.get("family"), entry.get("source") or "unknown",
                            entry.get("availability") or "advisory", headless,
                            int("high" in efforts), entry.get("default_effort"),
                            route_bindings.canonical_json(efforts),
                            entry.get("cli_version"), completed_at, 0, None,
                            generation_id, evidence["evidence_kind"],
                            evidence["evidence_digest"], evidence["source_fingerprint"],
                            evidence["harness_version"],
                            evidence["harness_compatibility"] if evidence["harness_compatibility"]
                            in {"verified", "supported"} else None,
                            evidence["harness_support_state"],
                            route_bindings.canonical_json(evidence["selector_binding"]),
                            route_bindings.canonical_json(evidence["effort_metadata"]),
                            route_bindings.canonical_json(evidence["adapter_metadata"]),
                        ),
                    )
                if harness == "deepseek":
                    for selector in carried_deepseek_routes:
                        con.execute(
                            "UPDATE model_routes SET stale=0,last_error=NULL,"
                            "last_seen_at=?,generation_id=? "
                            "WHERE harness='deepseek' AND selector=?",
                            (completed_at, generation_id, selector),
                        )
    payload["catalogue_generation"] = generation_id
    payload["generation_state"] = state
    payload["generation_published"] = publish_projection


def latest_harness_error(con, harness: str) -> str | None:
    """Return only the latest generation's stable harness-level failure."""
    try:
        row = con.execute(
            "SELECT error_summary FROM model_catalog_generations "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    try:
        summary = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    errors = summary.get("harness_errors") if isinstance(summary, dict) else None
    value = errors.get(harness) if isinstance(errors, dict) else None
    return value if isinstance(value, str) and len(value) <= 256 else None


def _requires_high_effort(harness: str) -> bool:
    """Match run.py: high is implicit only when the adapter transports it."""
    try:
        cfg = json.loads((ADAPTERS / harness / "adapter.json").read_text())
    except Exception:  # noqa: BLE001
        return False
    return bool(((cfg.get("headless") or {}).get("effort")))


def _default_route_verification(con, harnesses: dict[str, dict]) -> list[dict]:
    """Validate every configured flavor route against this fork's live rows."""
    defaults = []
    for configured in con.execute(
        "SELECT flavor,harness,model,is_default FROM flavor_defaults "
        "ORDER BY flavor,harness"
    ):
        flavor = configured["flavor"]
        harness = configured["harness"]
        model = configured["model"]
        status = harnesses.get(harness) or {}
        harness_error = status.get("error")
        if not _observed_version(status):
            harness_error = harness_error or "HARNESS_UNAVAILABLE"

        route = None
        if model is not None:
            found = con.execute(
                "SELECT availability,headless_supported,"
                "high_effort_supported,stale,last_error FROM model_routes "
                "WHERE harness=? AND selector=?",
                (harness, model),
            ).fetchone()
            route = dict(found) if found is not None else None

        runnable: bool | None = False
        if harness_error:
            state = "harness-error"
            reason = harness_error
        elif model is None:
            state = "harness-default"
            runnable = None
            reason = "model selected by harness at launch"
        elif route is None:
            state = "route-missing"
            reason = "exact model route was not discovered locally"
        elif route["stale"]:
            state = "route-stale"
            reason = route["last_error"] or "last-known route is stale"
        elif route["availability"] != "available":
            state = "route-unavailable"
            reason = f"route is {route['availability']}, not locally available"
        elif not route["headless_supported"]:
            state = "headless-unsupported"
            reason = "harness has no headless launch seam"
        elif _requires_high_effort(harness) and not route["high_effort_supported"]:
            state = "effort-unsupported"
            reason = "default high-effort route was not locally verified"
        else:
            state = "runnable"
            runnable = True
            reason = None

        defaults.append({
            "flavor": flavor,
            "harness": harness,
            "model": model,
            "is_default": bool(configured["is_default"]),
            "state": state,
            "runnable": runnable,
            "reason": reason,
        })
    return defaults


def runtime_verification(con, *, env=os.environ,
                         harness_probe=harness_versions.compatibility_status) -> dict:
    """Fork-local harness and configured-route evidence for one refresh."""
    checked_at = datetime.now(timezone.utc).isoformat()
    report = {
        "checked_at": checked_at,
        "runtime": "sandbox" if env.get("SC_SANDBOX") else "host",
        "harnesses": {},
        "defaults": [],
        "summary": {
            "harnesses_checked": 0,
            "harnesses_ready": 0,
            "exact_routes": 0,
            "exact_routes_runnable": 0,
            "harness_defaults": 0,
        },
    }
    try:
        harnesses = harness_probe()
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        return report

    harnesses = {
        harness: {**status, "support_state": _support_state(status)}
        for harness, status in harnesses.items()
    }
    report["harnesses"] = harnesses
    report["summary"]["harnesses_checked"] = len(harnesses)
    report["summary"]["harnesses_ready"] = sum(
        1 for harness, status in harnesses.items()
        if _observed_version(status) is not None and not status.get("error")
    )
    try:
        defaults = _default_route_verification(con, harnesses)
    except Exception as exc:  # noqa: BLE001
        report["route_error"] = str(exc)
        return report

    report["defaults"] = defaults
    report["summary"]["exact_routes"] = sum(
        1 for route in defaults if route["model"] is not None
    )
    report["summary"]["exact_routes_runnable"] = sum(
        1 for route in defaults if route["runnable"] is True
    )
    report["summary"]["harness_defaults"] = sum(
        1 for route in defaults if route["state"] == "harness-default"
    )
    return report


def _served(payload: dict, con=None, *, publish: bool = False,
            publication_locked: bool = False) -> dict:
    if con is not None and publish:
        persist_routes(con, payload, publication_locked=publication_locked)
    return payload


def _with_live_opencode(payload: dict, provider_models) -> dict:
    """Replace advisory OpenCode routes with its live connected-provider set."""
    result = {**payload}
    harnesses = dict(payload.get("harnesses") or {})
    sources = [
        source for source in (payload.get("sources") or [])
        if source != "opencode-provider-api"
    ]
    entries = []
    error = None
    if shutil.which("opencode"):
        try:
            entries = [
                _entry(
                    model["id"],
                    model.get("release_date") or "",
                    model.get("name") or model["id"],
                    model.get("family"),
                    source="opencode-provider-api",
                    availability="available",
                    provider=model.get("provider"),
                    provider_model=model.get("provider_model"),
                    supported_efforts=model.get("supported_efforts") or [],
                    default_effort=model.get("default_effort"),
                    cli_version=model.get("cli_version"),
                    selector_binding=model.get("selector_binding"),
                    adapter_metadata=model.get("adapter_metadata"),
                    native_variant_ids=model.get("native_variant_ids"),
                )
                for model in provider_models()
            ]
            sources.append("opencode-provider-api")
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    harnesses["opencode"] = {
        "families": _families("opencode", entries),
        "models": entries,
        **({"error": error} if error else {}),
    }
    result["harnesses"] = harnesses
    result["sources"] = sources
    if error:
        result["partial"] = True
        result["errors"] = [*(result.get("errors") or []),
                            f"opencode-provider-api: {error}"]
    return result


def _runtime_statuses(harness_probe, *, include_deepseek: bool) -> dict:
    statuses = dict(harness_probe())
    if include_deepseek and "deepseek" not in statuses:
        statuses["deepseek"] = harness_runtime_status("deepseek")
    return statuses


def controlled_route_evidence(
    harness: str,
    selector: str,
    *,
    env=None,
    run=None,
    opencode_provider=None,
    harness_probe=None,
    deepseek_fetch=None,
    deepseek_wire_probe=None,
) -> dict:
    """Probe one controlled route and bind its source to this runtime seat."""
    env = os.environ if env is None else env
    run = subprocess.run if run is None else run
    opencode_provider = (
        opencode_connected_models if opencode_provider is None
        else opencode_provider
    )
    harness_probe = (
        harness_versions.compatibility_status
        if harness_probe is None else harness_probe
    )
    deepseek_fetch = _http_json if deepseek_fetch is None else deepseek_fetch
    harness = (harness or "").strip().lower()
    scope = harness_versions.runtime_scope()
    entries: list[dict]
    status: dict = {}
    fingerprint = None
    try:
        statuses = _runtime_statuses(
            harness_probe, include_deepseek=harness == "deepseek"
        )
        status = dict(statuses.get(harness) or {})
        if harness == "claude":
            entries = _from_claude_cli(run)
        elif harness == "codex":
            entries = _from_codex_cache(env, run)
        elif harness == "kimi":
            entries = _from_kimi_config(env, run)
        elif harness == "opencode":
            if not shutil.which("opencode"):
                entries = []
            else:
                entries = [
                    _entry(
                        model["id"], model.get("release_date") or "",
                        model.get("name") or model["id"], model.get("family"),
                        source="opencode-provider-api",
                        availability="available",
                        provider=model.get("provider"),
                        provider_model=model.get("provider_model"),
                        supported_efforts=model.get("supported_efforts") or [],
                        default_effort=model.get("default_effort"),
                        cli_version=model.get("cli_version"),
                        selector_binding=model.get("selector_binding"),
                        adapter_metadata=model.get("adapter_metadata"),
                        native_variant_ids=model.get("native_variant_ids"),
                    )
                    for model in opencode_provider()
                ]
        elif harness == "deepseek":
            provider = (
                "ollama-cloud"
                if selector.startswith("ollama-cloud/")
                else "deepseek-official"
            )
            entries = _from_deepseek_provider(
                provider,
                deepseek_fetch,
                env,
                wire_probe=deepseek_wire_probe,
                selector=selector,
            )
        else:
            entries = []
    except Exception:  # noqa: BLE001 (unreadable live evidence is stale)
        entries = []
    entry = next((item for item in entries if item["id"] == selector), None)
    if (
        entry is not None
        and entry.get("availability") == "available"
        and _compatible_route_status(harness, entry, status)
    ):
        fingerprint = _entry_evidence(
            harness, entry, status
        )["source_fingerprint"]
    return {
        "runtime_status": status,
        "runtime_scope": scope,
        "source_fingerprint": fingerprint,
    }


def current_source_fingerprint(harness: str, selector: str, *, env=os.environ,
                               run=subprocess.run,
                               opencode_provider=opencode_connected_models,
                               harness_probe=harness_versions.compatibility_status,
                               ) -> str | None:
    """Compatibility helper for non-resolution drift displays."""
    evidence = controlled_route_evidence(
        harness, selector, env=env, run=run,
        opencode_provider=opencode_provider, harness_probe=harness_probe,
    )
    return evidence["source_fingerprint"]


def ensure_deepseek_route(
    con,
    selector: str,
    *,
    fetch=_http_json,
    env=os.environ,
    run=subprocess.run,
    opencode_provider=opencode_connected_models,
    harness_probe=harness_versions.compatibility_status,
    deepseek_wire_probe=None,
) -> dict | None:
    """Publish one explicitly selected authenticated route outside the sample."""
    row = con.execute(
        "SELECT * FROM model_routes WHERE harness='deepseek' AND selector=?",
        (selector,),
    ).fetchone()
    if row is not None and not row["stale"]:
        return dict(row)
    try:
        catalog(
            refresh=True,
            fetch=fetch,
            env=env,
            run=run,
            con=con,
            opencode_provider=opencode_provider,
            harness_probe=harness_probe,
            deepseek_wire_probe=deepseek_wire_probe,
            deepseek_selector=selector,
        )
    except Exception:  # noqa: BLE001 (provider diagnostics remain redacted)
        return None
    row = con.execute(
        "SELECT * FROM model_routes WHERE harness='deepseek' AND selector=?",
        (selector,),
    ).fetchone()
    return dict(row) if row is not None and not row["stale"] else None


def catalog(refresh: bool = False, fetch=_http_json, env=os.environ,
            run=subprocess.run, con=None,
            opencode_provider=opencode_connected_models,
            harness_probe=harness_versions.compatibility_status,
            deepseek_wire_probe=None, deepseek_selector=None) -> dict:
    """The cached-with-fallbacks entry point the API serves.

    fresh cache → serve it; miss/stale/refresh → live sweep, cache the result;
    sweep failed → stale cache if any, else the static floor. Every response
    carries `stale` + `fetched_at` so the GUI can say how current it is."""
    with _publication_lock():
        cached = _load_cache()
        authority = (
            _GENERATION_TABLE_UNAVAILABLE
            if refresh else _authoritative_generation(con)
        )
        refresh_required = not refresh and con is not None and authority is None
        serve_cached = bool(
            cached and not refresh and not refresh_required and _fresh(cached)
            and (
                authority is _GENERATION_TABLE_UNAVAILABLE
                or cached.get("catalogue_generation") == authority
            )
        )
    if serve_cached:
        return _served(_with_live_opencode(
            {**cached, "stale": bool(cached.get("stale"))},
            opencode_provider), con)
    refresh_started_at = datetime.now(timezone.utc).isoformat()
    try:
        fresh = build(
            fetch, env, run, deepseek_wire_probe=deepseek_wire_probe,
            deepseek_selector=deepseek_selector,
        )
    except Exception as e:  # noqa: BLE001
        refresh_completed_at = datetime.now(timezone.utc).isoformat()
        if cached:
            response = _with_live_opencode(
                {
                    **cached,
                    "stale": True,
                    "error": str(e),
                    "refresh_started_at": refresh_started_at,
                    "refresh_completed_at": refresh_completed_at,
                },
                opencode_provider,
            )
            if refresh and con is not None:
                verification = runtime_verification(
                    con, env=env, harness_probe=harness_probe
                )
                response["verification"] = verification
            if not refresh:
                return _served(response, con)
            cached_failure = {
                **cached,
                "stale": True,
                "error": str(e),
                "refresh_started_at": refresh_started_at,
                "refresh_completed_at": refresh_completed_at,
            }
            if "verification" in response:
                cached_failure["verification"] = response["verification"]
            if con is not None:
                with _publication_lock():
                    response = _served(
                        response, con, publish=True, publication_locked=True
                    )
                    return _finish_cache_publication(
                        cached_failure, response, con, opencode_provider,
                        publication_locked=True,
                    )
            return _finish_cache_publication(
                cached_failure, response, con, opencode_provider
            )
        fallback = {
            "v": PAYLOAD_VERSION, "fetched_at": None,
            "sources": ["static"], "stale": True,
            "error": str(e), "harnesses": _floor(),
            "refresh_started_at": refresh_started_at,
            "refresh_completed_at": refresh_completed_at,
        }
        response = _with_live_opencode(
            fallback, opencode_provider,
        )
        if refresh and con is not None:
            verification = runtime_verification(
                con, env=env, harness_probe=harness_probe
            )
            fallback["verification"] = verification
            response["verification"] = verification
        if not refresh:
            return _served(response, con)
        if con is not None:
            with _publication_lock():
                response = _served(
                    response, con, publish=True, publication_locked=True
                )
                return _finish_cache_publication(
                    fallback, response, con, opencode_provider,
                    publication_locked=True,
                )
        return _finish_cache_publication(
            fallback, response, con, opencode_provider
        )
    response = _with_live_opencode(
        {**fresh, "stale": False}, opencode_provider
    )
    response["refresh_started_at"] = refresh_started_at
    response["refresh_completed_at"] = datetime.now(timezone.utc).isoformat()
    if refresh_required and not refresh:
        message = "Catalogue refresh required after runtime evidence rebuild"
        response["stale"] = True
        response["error"] = message
        fresh["stale"] = True
        fresh["error"] = message
    if refresh and con is not None:
        probe_error = None
        try:
            harnesses = _runtime_statuses(
                harness_probe,
                include_deepseek="deepseek" in (response.get("harnesses") or {}),
            )
        except Exception as exc:  # noqa: BLE001
            harnesses = {}
            probe_error = str(exc)
            response["partial"] = True
            response["stale"] = True
            response["errors"] = [*(response.get("errors") or []),
                                  f"harness verification: {probe_error}"]
        response["verification"] = {
            "runtime": "sandbox" if env.get("SC_SANDBOX") else "host",
            "harnesses": harnesses,
        }
        _project_route_support(response, harnesses)
        _project_route_support(fresh, harnesses)
        with _publication_lock():
            response = _served(
                response, con, publish=True, publication_locked=True
            )

            def captured_probe():
                if probe_error is not None:
                    raise RuntimeError(probe_error)
                return harnesses

            verification = runtime_verification(
                con, env=env, harness_probe=captured_probe
            )
            fresh["verification"] = verification
            response["verification"] = verification
            if response.get("partial"):
                fresh["partial"] = True
                fresh["errors"] = (
                    response.get("errors") or fresh.get("errors") or []
                )
            return _finish_cache_publication(
                fresh, response, con, opencode_provider,
                publication_locked=True,
            )
    else:
        response = _served(response, con)
    if response.get("partial"):
        fresh["partial"] = True
        fresh["errors"] = response.get("errors") or fresh.get("errors") or []
    return _finish_cache_publication(
        fresh, response, con, opencode_provider
    )
