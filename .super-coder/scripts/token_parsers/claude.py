"""claude parser — transcript JSONL under ~/.claude/projects/<encoded-cwd>/.

Fidelity Full: every assistant line carries a `usage` object with the four
token classes + the model. TWO verified hazards shape this parser:

  1. The same usage object repeats on every content-block line of a
     multi-block response (verified: 401 usage lines, 181 unique message ids
     in one transcript — naive summing overcounts ~2×). Dedupe by
     `message.id`, keeping the LAST occurrence (final counts).
  2. Resume/fork copies lines into a NEW session file, so the dedupe must be
     cross-file. Copies land in the same project dir (same cwd), so
     incrementality is DIR-scoped, not file-scoped: any changed file in a
     dir re-parses the whole dir (a fresh cross-file seen-set), unchanged
     dirs are skipped wholesale. File-level mtime skips would silently
     re-count ids living in the skipped files.

Repo filter: the `cwd` field on the lines, NOT the project-dir name — the
dir-name dash-encoding is lossy (`/` and `-` encode identically). The encoded
dir name is only a cheap prefilter (an encoded path under repo_root always
has the encoded root as a prefix); the per-line cwd decides.

No native title: derived from the first real user message (command wrappers
and meta lines skipped).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import in_repo, iso_utc, norm_iso, row

HARNESS = "claude"
PARSER_VERSION = "1"
DATA_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
TITLE_CAP = 500  # storage cap; the UI truncates at 100 for collapsed cards


def _encode(path: str) -> str:
    """The harness's project-dir encoding of a cwd (lossy: '/' and '.' and '-'
    all land on '-'). Good enough for a prefix prefilter, never for identity."""
    return "".join(c if c.isalnum() else "-" for c in path)


def _title_from(rec: dict) -> "str | None":
    """A displayable title candidate from a user line, or None."""
    if rec.get("isMeta"):
        return None
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), None)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith("<"):  # command/caveat/meta wrappers
        return None
    return text[:TITLE_CAP]


def _parse_file(path: Path, seen: set, log) -> "dict | None":
    """One transcript → session aggregate. `seen` is the dir-wide message-id
    set (mutated); ids already seen count 0 here (resume/fork copies)."""
    per_model: dict[str, dict] = {}   # model → {id: usage} (last occurrence wins)
    title = None
    ts_first = ts_last = None
    cwd = None
    bad_lines = 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError as e:
        log(f"claude: unreadable {path.name}: {e}")
        return None
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(rec, dict):
                bad_lines += 1
                continue
            cwd = rec.get("cwd") or cwd
            ts = rec.get("timestamp")
            if ts:
                ts_first = ts_first or ts
                ts_last = ts
            if title is None and rec.get("type") == "user":
                title = _title_from(rec)
            msg = rec.get("message") or {}
            usage = msg.get("usage") if isinstance(msg, dict) else None
            mid = msg.get("id") if isinstance(msg, dict) else None
            if not (isinstance(usage, dict) and mid):
                continue
            if mid in seen:
                # cross-file copy (resume/fork) — counted where first seen
                continue
            per_model.setdefault(msg.get("model") or "unknown", {})[mid] = usage
    for model, by_id in per_model.items():
        seen.update(by_id)
    if bad_lines:
        log(f"claude: {path.name}: {bad_lines} unparseable line(s) tolerated")
    return {"per_model": per_model, "title": title, "cwd": cwd,
            "started_at": norm_iso(ts_first), "ended_at": norm_iso(ts_last),
            "partial": bad_lines > 0}


def sweep(repo_root, since_epoch, log) -> list[dict]:
    if not DATA_DIR.is_dir():
        return []
    prefix = _encode(str(repo_root))
    rows: list[dict] = []
    for proj in sorted(DATA_DIR.iterdir()):
        if not (proj.is_dir() and proj.name.startswith(prefix)):
            continue
        files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            continue
        # dir-scoped incrementality (see module docstring)
        changed = any((since_epoch(str(p)) or 0) < p.stat().st_mtime for p in files)
        if not changed:
            continue
        seen: set = set()
        for path in files:  # mtime order: copies count where they first appeared
            parsed = _parse_file(path, seen, log)
            if parsed is None or not in_repo(parsed["cwd"], repo_root):
                continue
            common = dict(harness=HARNESS, parser_version=PARSER_VERSION,
                          provider="anthropic", title=parsed["title"],
                          started_at=parsed["started_at"],
                          ended_at=parsed["ended_at"], cwd=parsed["cwd"])
            if not parsed["per_model"]:
                rows.append(row(ref=str(path), model=None, status="no_usage", **common))
                continue
            status = "partial" if parsed["partial"] else "ok"
            for model, by_id in parsed["per_model"].items():
                def total(key):
                    return sum(u.get(key) or 0 for u in by_id.values())
                rows.append(row(
                    ref=str(path), model=model, status=status,
                    input_tokens=total("input_tokens"),
                    output_tokens=total("output_tokens"),
                    cache_read_tokens=total("cache_read_input_tokens"),
                    cache_write_tokens=total("cache_creation_input_tokens"),
                    **common))
    return rows
