#!/usr/bin/env python3
"""Model catalog — live model-id suggestions for the Default Models GUI.

Layered, best-effort sources. The GUI's model field stays free text, so none
of this is load-bearing — a source that fails just thins the suggestions:

  1. models.dev/api.json — the keyless catalog OpenCode itself consumes.
     One fetch covers all four harness providers (anthropic / openai /
     mistral / ollama-cloud), with release dates for newest-first sorting.
  2. Provider list-models APIs — only when the matching env key is present.
     Harness logins are OAuth, not API keys, so these are usually absent.
  3. `opencode models` CLI — exactly what the local install can resolve.
  4. A static floor (the ids the engine ships in flavor_defaults) when every
     live source fails and no cache exists.

Fetched server-side (no CORS), cached under the gitignored .super-coder/logs/
(ephemeral like webapp.log — NOT .sc-state/, where an auto-written file would
dirty the tree and trip the publish guard) with a TTL; a failed refresh serves
the stale cache and says so. Claude aliases
(opus / sonnet / haiku) lead their list: an alias floats to the newest model
in its family, so a stored alias never goes stale — full ids cover NEW
families, which is the case aliases can't (a fable-shaped release).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
CACHE = ENGINE / "logs" / "model_catalog.json"
TTL_HOURS = 24
TIMEOUT = 8
MODELS_DEV_URL = "https://models.dev/api.json"

# harness -> models.dev provider key
HARNESS_PROVIDER = {
    "claude": "anthropic",
    "codex": "openai",
    "vibe": "mistral",
    "opencode": "ollama-cloud",
}
# opencode model ids are provider-prefixed ("ollama-cloud/<model>") — the
# format flavor_defaults already stores for that harness.
PREFIXED_HARNESSES = {"opencode"}

CLAUDE_ALIASES = ["opus", "sonnet", "haiku"]

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

# The ids the engine ships in flavor_defaults — surfaced only when every live
# source fails AND no cache exists, so the datalist is never empty.
STATIC_FLOOR = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": ["gpt-5.5", "gpt-5.4-mini"],
    "vibe": ["devstral-latest", "codestral-latest"],
    "opencode": ["ollama-cloud/deepseek-v4-pro", "ollama-cloud/glm-5.1",
                 "ollama-cloud/qwen3-coder-next", "ollama-cloud/gpt-oss:20b"],
}


def _http_json(url: str, headers: dict | None = None) -> dict:
    # models.dev's CDN 403s python-urllib's default agent — always identify.
    hdrs = {"User-Agent": "super-coder-model-catalog/1.0", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _entry(mid: str, release_date: str = "", name: str = "") -> dict:
    return {"id": mid, "release_date": release_date, "name": name or mid}


def _from_models_dev(fetch) -> dict[str, list[dict]]:
    data = fetch(MODELS_DEV_URL)
    out: dict[str, list[dict]] = {}
    for harness, provider in HARNESS_PROVIDER.items():
        models = (data.get(provider) or {}).get("models") or {}
        entries = []
        for mid, m in models.items():
            full = f"{provider}/{mid}" if harness in PREFIXED_HARNESSES else mid
            entries.append(_entry(full, m.get("release_date") or "",
                                  m.get("name") or mid))
        entries.sort(key=lambda e: e["release_date"], reverse=True)
        out[harness] = entries
    return out


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
            out[harness] = [_entry(i) for i in ids]
    return out


def _from_opencode_cli(run) -> list[dict]:
    """`opencode models` lists provider/model ids the LOCAL install resolves —
    the most accurate opencode source, merged in when the binary exists."""
    if not shutil.which("opencode"):
        return []
    try:
        r = run(["opencode", "models"], capture_output=True, text=True,
                timeout=15)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    # The CLI lists every provider/model the install resolves (hundreds).
    # ollama-cloud leads (the engine's opencode defaults live there), the
    # rest sorted — the datalist filters as the operator types.
    ids = sorted({line.strip() for line in r.stdout.splitlines()
                  if "/" in line.strip() and " " not in line.strip()},
                 key=lambda i: (not i.startswith("ollama-cloud/"), i))
    return [_entry(i) for i in ids]


def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
    seen = {e["id"] for e in base}
    return base + [e for e in extra if e["id"] not in seen]


def build(fetch=_http_json, env=os.environ, run=subprocess.run) -> dict:
    """One live sweep across all sources. Raises only if EVERY source fails —
    partial results (e.g. models.dev down but a keyed API up) still count."""
    harnesses: dict[str, list[dict]] = {}
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
    oc = _from_opencode_cli(run)
    if oc:
        harnesses["opencode"] = _merge(harnesses.get("opencode", []), oc)
        sources.append("opencode-cli")
    if not sources:
        raise RuntimeError("; ".join(errors) or "no catalog sources available")
    # claude aliases lead — self-tracking within a family, so they never stale.
    aliases = [_entry(a, name=f"{a} (alias — tracks the family's latest)")
               for a in CLAUDE_ALIASES]
    harnesses["claude"] = aliases + [e for e in harnesses.get("claude", [])
                                     if e["id"] not in CLAUDE_ALIASES]
    return {"fetched_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources, "harnesses": harnesses}


def _load_cache() -> dict | None:
    try:
        return json.loads(CACHE.read_text())
    except Exception:  # noqa: BLE001  (missing or corrupt — both mean "no cache")
        return None


def _fresh(cached: dict) -> bool:
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
        return age.total_seconds() < TTL_HOURS * 3600
    except Exception:  # noqa: BLE001
        return False


def _floor() -> dict[str, list[dict]]:
    return {h: [_entry(i) for i in ids] for h, ids in STATIC_FLOOR.items()}


def catalog(refresh: bool = False, fetch=_http_json, env=os.environ,
            run=subprocess.run) -> dict:
    """The cached-with-fallbacks entry point the API serves.

    fresh cache → serve it; miss/stale/refresh → live sweep, cache the result;
    sweep failed → stale cache if any, else the static floor. Every response
    carries `stale` + `fetched_at` so the GUI can say how current it is."""
    cached = _load_cache()
    if cached and not refresh and _fresh(cached):
        return {**cached, "stale": False}
    try:
        fresh = build(fetch, env, run)
    except Exception as e:  # noqa: BLE001
        if cached:
            return {**cached, "stale": True, "error": str(e)}
        return {"fetched_at": None, "sources": ["static"], "stale": True,
                "error": str(e), "harnesses": _floor()}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(fresh, indent=1) + "\n")
    return {**fresh, "stale": False}
