#!/usr/bin/env python3
"""Web search (Tavily) — host-side key store, search client, `./sc search`.

Three pieces, one module (spec doc #215, feature #69):

  store   The Tavily API key lives in ONE mode-0600 JSON file, `web_search.json`,
          inside the private instance-state root (legacy floors: .sc-state/local/).
          Never instance.json (bind-mounted into the sandbox, no-secrets by
          design), never the engine DB, never the snapshot or a render. Only the
          browser operator writes it (GUI → Scripts → Web Search); `status()` is
          the only read other code sees, and it carries at most the last four
          characters of the key.

  client  `search()` calls Tavily with the stored key from the API PROCESS. A
          shell never holds the key: it posts to `/_sc/search` with its own
          bearer token and the host makes the outbound call, so a sandboxed
          shell needs no egress. Every failure is a `WebSearchError` with an
          HTTP status the API forwards and a message redacted of the key.

  cli     `./sc search "<query>" [--max N] [--depth basic|advanced] [--json]`
          — the shell verb, wired through the same API lane as `sc mem`.

Rotation is a replace: `write()` lands a fresh temp inode and renames it over
the old file; the next `search()` reads the new key. Nothing caches it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import instance_state

ENGINE = Path(__file__).resolve().parents[1]

PROVIDER = "tavily"
TAVILY_URL = "https://api.tavily.com/search"
STORE_NAME = "web_search.json"
TIMEOUT = 20.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 20
DEPTHS = ("basic", "advanced")
PROBE_QUERY = "subfloor agent substrate"

UNCONFIGURED = (
    "web search is not configured — ask the FnB to set the Tavily API key in "
    "the GUI: Scripts → Web Search"
)


class WebSearchError(Exception):
    """A secret-free failure carrying the HTTP status the API should return."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# -- store --------------------------------------------------------------------

def store_path() -> Path:
    """The one file that holds the key: private instance root, else the
    legacy owner-only `.sc-state/local/` namespace."""
    return instance_state.maintenance_state(ENGINE).root / STORE_NAME


def _redact(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _hint(key: str) -> str | None:
    if not key:
        return None
    return "…" + key[-4:] if len(key) >= 8 else "…"


def _load(path: Path | None = None) -> dict:
    path = path or store_path()
    if path.is_symlink():
        raise WebSearchError(500, "store_unsafe",
                             f"{path} is a symlink — refusing to read the key store")
    if not path.exists():
        return {}
    if not path.is_file():
        raise WebSearchError(500, "store_unsafe",
                             f"{path} is not a regular file — refusing to read the key store")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WebSearchError(500, "store_unreadable",
                             f"{path} is unreadable: {exc}") from exc
    return data if isinstance(data, dict) else {}


def status(path: Path | None = None) -> dict:
    """What the GUI and API may see: never the key itself."""
    data = _load(path)
    key = str(data.get("api_key") or "")
    return {
        "configured": bool(key),
        "provider": PROVIDER,
        "key_hint": _hint(key),
        "updated_at": data.get("updated_at"),
    }


def write(api_key: str, path: Path | None = None) -> dict:
    """Set or rotate the key: fresh 0600 inode, renamed over the old file."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("api_key is required")
    if any(ch.isspace() for ch in api_key):
        raise ValueError("api_key must not contain whitespace")
    path = path or store_path()
    if path.is_symlink():
        raise WebSearchError(500, "store_unsafe",
                             f"{path} is a symlink — refusing to write the key store")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": PROVIDER,
        "api_key": api_key,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fd, tmp = tempfile.mkstemp(prefix=f".{STORE_NAME}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return status(path)


def clear(path: Path | None = None) -> dict:
    path = path or store_path()
    if path.is_symlink():
        raise WebSearchError(500, "store_unsafe",
                             f"{path} is a symlink — refusing to touch the key store")
    path.unlink(missing_ok=True)
    return status(path)


# -- client -------------------------------------------------------------------

def _upstream_error(code: int, body: bytes, key: str) -> WebSearchError:
    try:
        detail = json.loads(body or b"")
        detail = detail.get("detail", detail) if isinstance(detail, dict) else detail
        if isinstance(detail, dict):
            detail = detail.get("error") or detail.get("message") or json.dumps(detail)
        detail = str(detail)
    except (ValueError, UnicodeDecodeError):
        detail = (body or b"").decode("utf-8", "replace")
    detail = _redact(detail, key)[:300]
    if code in (401, 403):
        return WebSearchError(
            502, "key_rejected",
            f"Tavily rejected the API key (HTTP {code}) — the FnB should rotate it "
            f"in the GUI: Scripts → Web Search. {detail}".rstrip())
    if code == 429:
        return WebSearchError(429, "rate_limited", f"Tavily rate limit hit: {detail}")
    if code == 432:
        return WebSearchError(429, "usage_limit",
                              f"Tavily plan usage limit reached: {detail}")
    if code == 400:
        return WebSearchError(400, "bad_request", f"Tavily rejected the query: {detail}")
    return WebSearchError(502, "upstream_error", f"Tavily HTTP {code}: {detail}")


def _normalize(raw: dict, query: str) -> dict:
    results = []
    for item in raw.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append({
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("content") or "",
            "score": item.get("score"),
            "published": item.get("published_date"),
        })
    return {
        "provider": PROVIDER,
        "query": query,
        "answer": raw.get("answer") or None,
        "results": results,
        "response_time": raw.get("response_time"),
    }


def search(query: str, *, max_results: int = DEFAULT_MAX_RESULTS,
           depth: str = "basic", api_key: str | None = None,
           path: Path | None = None, opener=None) -> dict:
    """One Tavily search with the stored key (or an explicit candidate key for
    the GUI's test-before-save). Raises WebSearchError; never leaks the key."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    if not isinstance(max_results, int) or isinstance(max_results, bool) \
            or not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ValueError(f"max_results must be an integer 1..{MAX_RESULTS_CAP}")
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(DEPTHS)}")
    key = (api_key or "").strip() or str(_load(path).get("api_key") or "")
    if not key:
        raise WebSearchError(409, "unconfigured", UNCONFIGURED)
    body = json.dumps({
        "query": query,
        "max_results": max_results,
        "search_depth": depth,
        "include_answer": True,
    }).encode()
    req = urllib.request.Request(TAVILY_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    opener = opener or urllib.request.urlopen
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            raw = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise _upstream_error(exc.code, exc.read(), key) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise WebSearchError(502, "unreachable",
                             f"Tavily unreachable: {_redact(str(exc), key)}") from None
    if not isinstance(raw, dict):
        raise WebSearchError(502, "upstream_error", "Tavily returned a non-object body")
    return _normalize(raw, query)


def validate(api_key: str | None = None, path: Path | None = None,
             opener=None) -> dict:
    """The GUI's test button: {ok, output}, mirroring the vm/ts check contract.
    Tests the candidate key when given, else the stored one."""
    try:
        out = search(PROBE_QUERY, max_results=1, api_key=api_key, path=path,
                     opener=opener)
    except WebSearchError as exc:
        return {"ok": False, "output": exc.message}
    except ValueError as exc:
        return {"ok": False, "output": str(exc)}
    n = len(out["results"])
    first = out["results"][0]["url"] if n else "(no results)"
    return {"ok": True, "output": f"Tavily answered: {n} result(s) — {first}"}


# -- cli ----------------------------------------------------------------------

def render(payload: dict) -> str:
    lines = []
    if payload.get("answer"):
        lines.append(payload["answer"].strip())
        lines.append("")
    results = payload.get("results") or []
    if not results:
        lines.append("(no results)")
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title') or '(untitled)'}")
        lines.append(f"   {r.get('url') or ''}")
        snippet = " ".join((r.get("snippet") or "").split())
        if snippet:
            lines.append("   " + (snippet[:297] + "…" if len(snippet) > 300 else snippet))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sc search",
        description="Web search through the engine (Tavily); the key stays on the host.")
    p.add_argument("query", nargs="+", help="what to search for")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_RESULTS,
                   help=f"results to return, 1..{MAX_RESULTS_CAP} (default {DEFAULT_MAX_RESULTS})")
    p.add_argument("--depth", choices=DEPTHS, default="basic",
                   help="basic (default, fast) or advanced (deeper, slower)")
    p.add_argument("--json", action="store_true", help="print the raw payload")
    return p


def main(argv: list[str]) -> int:
    import mem
    args = build_parser().parse_args(argv)
    mem._PROG = "search"
    mem._require_api()
    payload = mem._api("POST", "/_sc/search", {
        "query": " ".join(args.query),
        "max_results": args.max,
        "depth": args.depth,
    }, idempotent=True, timeout=TIMEOUT + 10)
    print(json.dumps(payload, indent=2) if args.json else render(payload))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
