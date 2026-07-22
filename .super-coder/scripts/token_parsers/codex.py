"""codex parser — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

`token_count` events carry CUMULATIVE totals (`info.total_token_usage`), so
the LAST event is the session's spend — summing per-turn values would
overcount. Subset rules (spec doc #11, or the classes double-count):
`input_tokens` includes `cached_input_tokens` → fresh input is the
difference, cache_read is the cached part; `reasoning_output_tokens` is
INSIDE `output_tokens` → stored informationally, never added to totals.
No cache-write class (fidelity Good).

cwd + provider live in `session_meta.payload`; the model id in
`turn_context.payload.model`. No native title — derived from the first user
message that isn't scaffolding (codex injects AGENTS.md instructions and
<environment_context> as user-role messages; anything opening with '#' or
'<' is skipped).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import in_repo, norm_iso, row

HARNESS = "codex"
PARSER_VERSION = "1"
DATA_DIR = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
TITLE_CAP = 500


def _title_from(payload: dict) -> "str | None":
    content = payload.get("content")
    if isinstance(content, list):
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("text")), None)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text[0] in "<#":  # injected instructions / env context
        return None
    return text[:TITLE_CAP]


def _parse_file(path: Path, log, repo_root) -> "dict | None":
    """Full parse of one rollout — except off-repo files, which bail on the
    session_meta line (line 1 in practice). The bail matters: off-repo files
    never produce rows, so they never get a captured_at watermark, and without
    it the since_epoch gate re-admits them on EVERY sweep — a perpetual full
    re-parse of every other project's codex history (hundreds of MB) just to
    re-discover their cwd is elsewhere."""
    meta = last_tc = title = model = None
    ts_first = ts_last = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError as e:
        log(f"codex: unreadable {path.name}: {e}")
        return None
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = rec.get("timestamp")
            if ts:
                ts_first = ts_first or ts
                ts_last = ts
            p = rec.get("payload") or {}
            t = rec.get("type")
            if t == "session_meta":
                meta = p
                if not in_repo(p.get("cwd"), repo_root):
                    return {"off_repo": True}
            elif t == "turn_context":
                model = p.get("model") or model
            elif t == "event_msg" and p.get("type") == "token_count":
                last_tc = p.get("info") or last_tc
            elif (title is None and t == "response_item"
                  and p.get("type") == "message" and p.get("role") == "user"):
                title = _title_from(p)
    if meta is None:
        log(f"codex: {path.name}: no session_meta — format drift? skipped")
        return None
    return {"meta": meta, "last_tc": last_tc, "title": title, "model": model,
            "started_at": norm_iso(ts_first), "ended_at": norm_iso(ts_last)}


def sweep(repo_root, since_epoch, log, cache=None) -> list[dict]:
    if not DATA_DIR.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(DATA_DIR.glob("*/*/*/rollout-*.jsonl")):
        last = since_epoch(str(path))
        if last is not None and path.stat().st_mtime <= last:
            continue
        parsed = _parse_file(path, log, repo_root)
        if parsed is None or parsed.get("off_repo"):
            continue
        cwd = parsed["meta"].get("cwd")
        if not in_repo(cwd, repo_root):
            continue
        common = dict(harness=HARNESS, ref=str(path), parser_version=PARSER_VERSION,
                      provider=parsed["meta"].get("model_provider"),
                      model=parsed["model"], title=parsed["title"],
                      started_at=parsed["started_at"], ended_at=parsed["ended_at"],
                      cwd=cwd)
        total = (parsed["last_tc"] or {}).get("total_token_usage")
        if not total:
            rows.append(row(status="no_usage", **common))
            continue
        cached = total.get("cached_input_tokens") or 0
        rows.append(row(
            input_tokens=max((total.get("input_tokens") or 0) - cached, 0),
            output_tokens=total.get("output_tokens"),
            cache_read_tokens=cached,
            reasoning_tokens=total.get("reasoning_output_tokens"),
            **common))
    return rows
