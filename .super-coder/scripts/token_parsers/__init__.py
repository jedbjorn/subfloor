"""Per-harness token-usage parsers — the plugin seam of token analytics.

Each module (claude/opencode/codex/vibe/kimi) parses what its harness leaves
on disk and returns normalized row dicts for `session_token_usage` (migration
0071). These are plugins over third-party formats we don't control: version
drift is accepted — a parser that breaks gets fixed forward. Loud failure,
never silent zeros (design stance, spec doc #11).

Contract — every module exposes:

    HARNESS = "<name>"
    PARSER_VERSION = "<pin>"        # bumped when the expected format shape changes
    def sweep(repo_root, since_epoch, log, cache=None) -> list[dict]

  repo_root    Path — only sessions whose recorded cwd is this repo (root or a
               .sc-worktrees/ tree) are returned; the cwd rides on the row as a
               transient key for attribution, never stored.
  since_epoch  callable(ref) -> float|None — the row's last captured_at as an
               epoch, for mtime-gated incremental skips. None = never captured.
  log          callable(str) — parser-level notices (shape drift, skipped
               files). Loud by design.
  cache        dict — parser-owned incremental state, mutated in place; the
               collector persists it per harness (analytics_parse_cache,
               migration 0073) pinned to PARSER_VERSION, and passes {} on a
               version mismatch or --full. A DISPOSABLE accelerator, never a
               source of truth: parsers must produce identical rows with
               cache={}. Only claude uses it today (per-file byte-offset
               tail parsing, CC-145).

Row keys: harness, harness_session_ref, provider, model, title, started_at,
ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
reasoning_tokens, status ('ok'|'partial'|'no_usage'), parser_version, cwd
(transient). Token-class rule: NULL means "not exposed by this harness",
0 means "measured zero" — never write zeros as if measured.

reasoning_tokens is informational and ALWAYS a subset of output_tokens
(codex reports it that way natively; a parser whose source reports
reasoning separately folds it into output_tokens — see opencode). Total
spend is therefore input + output + cache_read + cache_write, never
+ reasoning.

PARSER_VERSION doubles as the incrementality pin: the collector skips a
ref only when its stored rows carry the CURRENT version, so bumping it
forces a full re-parse and count-affecting fixes reach already-swept
sessions.
"""
from __future__ import annotations

from datetime import datetime, timezone

HARNESSES = ["claude", "opencode", "codex", "vibe", "kimi"]


def iso_utc(epoch: "float | None") -> "str | None":
    """Epoch seconds → ISO UTC (second precision), the storage format."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_iso(ts: "str | None") -> "str | None":
    """Normalize a harness timestamp (ISO with ms / offset / Z) to ISO UTC at
    second precision. Unparseable input returns None — never a fake time."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_repo(cwd: "str | None", repo_root) -> bool:
    """True when cwd is the repo root or inside it (worktrees included)."""
    if not cwd:
        return False
    root = str(repo_root).rstrip("/")
    return cwd == root or cwd.startswith(root + "/")


def row(*, harness: str, ref: str, parser_version: str, provider=None,
        model=None, title=None, started_at=None, ended_at=None,
        input_tokens=None, output_tokens=None, cache_read_tokens=None,
        cache_write_tokens=None, reasoning_tokens=None, status="ok",
        cwd=None) -> dict:
    return {
        "harness": harness, "harness_session_ref": ref,
        "parser_version": parser_version, "provider": provider,
        "model": model, "title": (title or None),
        "started_at": started_at, "ended_at": ended_at,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "status": status, "cwd": cwd,
    }
