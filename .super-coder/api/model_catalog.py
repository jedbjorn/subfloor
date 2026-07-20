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
  3. `opencode models` CLI — exactly what the local install can resolve.
  4. A static floor (the ids the engine ships in flavor_defaults) when every
     live source fails and no cache exists.

Fetched server-side (no CORS), cached under the gitignored .super-coder/logs/
(ephemeral like webapp.log — NOT .sc-state/, where an auto-written file would
dirty the tree and trip the publish guard) with a TTL; a failed refresh serves
the stale cache and says so.

Payload (v2) is family-first, matching the picker: per harness, `families`
([{family, latest, release_date, n}], newest-first — "pick opus / sonnet /
fable / deepseek and track its latest") plus the flat `models` list for
sub-version search. For claude, a family with a CLI alias (opus / sonnet /
haiku) resolves `latest` to the alias — an alias floats to the newest model
in its family, so the stored value never goes stale; families without one
(the fable case) resolve to the newest concrete id.
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
PAYLOAD_VERSION = 2

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
        return json.loads(r.read().decode())


def _entry(mid: str, release_date: str = "", name: str = "",
           family: str | None = None) -> dict:
    return {"id": mid, "release_date": release_date, "name": name or mid,
            "family": family}


def _from_models_dev(fetch) -> dict[str, list[dict]]:
    data = fetch(MODELS_DEV_URL)
    out: dict[str, list[dict]] = {}
    for harness, provider in HARNESS_PROVIDER.items():
        models = (data.get(provider) or {}).get("models") or {}
        entries = []
        for mid, m in models.items():
            full = f"{provider}/{mid}" if harness in PREFIXED_HARNESSES else mid
            entries.append(_entry(full, m.get("release_date") or "",
                                  m.get("name") or mid, m.get("family")))
        entries.sort(key=lambda e: e["release_date"], reverse=True)
        out[harness] = entries
    return out


def _families(harness: str, entries: list[dict]) -> list[dict]:
    """Group a harness's models by models.dev family, newest family first.
    A family chip means "track this line's latest": `latest` is the newest
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
    return {"v": PAYLOAD_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
            "harnesses": {h: {"families": _families(h, entries),
                              "models": entries}
                          for h, entries in harnesses.items()}}


def _load_cache() -> dict | None:
    try:
        cached = json.loads(CACHE.read_text())
    except Exception:  # noqa: BLE001  (missing or corrupt — both mean "no cache")
        return None
    # A cache written by another payload version would hand the client a
    # shape it can't render — ignore it entirely.
    return cached if cached.get("v") == PAYLOAD_VERSION else None


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
        entries = [_entry(i, family=_FLOOR_FAMILY.get(i)) for i in ids]
        out[h] = {"families": _families(h, entries), "models": entries}
    return out


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
        return {"v": PAYLOAD_VERSION, "fetched_at": None,
                "sources": ["static"], "stale": True,
                "error": str(e), "harnesses": _floor()}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(fresh, indent=1) + "\n")
    return {**fresh, "stale": False}
