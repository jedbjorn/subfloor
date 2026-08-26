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
  3. DeepSeek's stock loopback Host API — configured providers/models plus
     value-free credential readiness, bound to the pinned official runtime.
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
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import harness_versions
import route_bindings
import toml_compat
import deepseek_host
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

DEEPSEEK_SOURCE = "deepseek-host-api"


class _ModelCatalogueLimitError(ValueError):
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


def _from_deepseek_host(
    client=None,
    *,
    selector: str | None = None,
    discovery_out: dict | None = None,
) -> list[dict]:
    """Project exact configured routes through the stock official Host API."""
    client = deepseek_host.DeepSeekHostClient() if client is None else client
    routes = deepseek_host.configured_routes(client, selector=selector)
    entries = []
    selectors = [route.selector for route in routes]
    if discovery_out is not None:
        discovery_out.update({
            "provider": "deepseek-host",
            "selectors": selectors,
            "attempted_selectors": selectors,
            "proved_selectors": selectors,
        })
    for route in routes:
        efforts = list(route.reasoning_efforts)
        metadata_by_effort = {
            effort: route.binding_metadata(effort)
            for effort in [route_bindings.DEFAULT_EFFORT, *efforts]
        }
        entries.append(_entry(
            route.selector,
            name=route.name,
            source=DEEPSEEK_SOURCE,
            availability="available",
            provider=route.provider,
            provider_model=route.model,
            supported_efforts=efforts,
            default_effort=route.default_effort,
            cli_version=route.runtime_version,
            selector_binding={
                "kind": "official-host-configured-model",
                "selector": route.selector,
                "provider_model": route.model,
                "provider_route": route.provider,
                "endpoint_identity": route.endpoint_identity,
                "credential_ref": route.credential_ref,
                "configuration_digest": route.configuration_digest,
                "runtime_source_commit": route.source_commit,
            },
            adapter_metadata={
                "route_metadata_by_effort": metadata_by_effort,
            },
        ))
    return entries


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
    deepseek_client=None,
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
    try:
        deepseek = _from_deepseek_host(
            deepseek_client,
        )
        harnesses["deepseek"] = deepseek
        if deepseek:
            sources.append(DEEPSEEK_SOURCE)
    except Exception:  # noqa: BLE001 (official Host errors stay redacted)
        harnesses.setdefault("deepseek", [])
        harness_errors["deepseek"] = "official DeepSeek Host configuration unavailable"
        errors.append("deepseek-host-api: official DeepSeek Host configuration unavailable")
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
    headless = cfg.get("headless") or {}
    return bool(headless.get("launch") or headless.get("engine_script"))


def harness_runtime_status(harness: str) -> dict:
    """Return exact version-bounded runtime evidence for one shipped harness."""
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
        ("deepseek", DEEPSEEK_SOURCE): "deepseek-host-config-v1",
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
    return (
        "tested"
        if status.get("compatibility") == "verified"
        else "best-effort"
    )


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
                "route_metadata_by_effort"
            ) or {}
            value = mappings.get(effort)
            return dict(value) if isinstance(value, dict) else {}
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
    deepseek_client=None,
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
            entries = _from_deepseek_host(
                deepseek_client,
                selector=selector,
            )
        else:
            entries = []
    except Exception:  # noqa: BLE001 (unreadable live evidence is stale)
        entries = []
    entry = next((item for item in entries if item["id"] == selector), None)
    route_advertised = bool(
        entry is not None
        and entry.get("availability") == "available"
        and _compatible_route_status(harness, entry, status)
    )
    if route_advertised:
        fingerprint = _entry_evidence(
            harness, entry, status
        )["source_fingerprint"]
    advertised_options_by_model = None
    if route_advertised:
        assert entry is not None
        if harness == "opencode":
            native_variants = entry.get("native_variant_ids") or {}
            native_option_ids = (
                list(native_variants.values())
                if isinstance(native_variants, dict)
                else []
            )
        else:
            supported = entry.get("supported_efforts") or []
            native_option_ids = list(supported) if isinstance(supported, list) else []
        advertised_options_by_model = {selector: native_option_ids}
    return {
        "runtime_status": status,
        "runtime_scope": scope,
        "source_fingerprint": fingerprint,
        "advertised_options_by_model": advertised_options_by_model,
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
    deepseek_client=None,
) -> dict | None:
    """Publish one exact route from the official Host configuration."""
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
            deepseek_client=deepseek_client,
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
            deepseek_client=None) -> dict:
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
            fetch, env, run, deepseek_client=deepseek_client,
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
