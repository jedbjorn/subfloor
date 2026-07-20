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
     dir re-aggregates the whole dir (a fresh cross-file seen-set), unchanged
     dirs are skipped wholesale. File-level mtime skips would silently
     re-count ids living in the skipped files.

Re-AGGREGATES, not re-parses: per-file parse state (full id→usage maps, no
cross-file dedupe applied) persists in the collector-provided `cache`, keyed
by path with a byte offset high-water mark. An unchanged file replays from
cache without touching disk; a grown file (transcripts are append-only)
parses only its tail delta; a shrunk/rewritten file re-parses in full. The
cross-file dedupe then replays over the cached maps in mtime order — cheap,
and byte-identical to a from-scratch parse. Without this, a live session
keeps its dir permanently hot and every boot re-pays a full multi-hundred-MB
parse (CC-145: the 10-15s boot stall). A trailing line without '\n' (a live
session mid-append) is left unconsumed for the next sweep.

Subagent transcripts live in SUBDIRECTORIES of the project dir
(<session-uuid>/subagents/agent-*.jsonl) — a non-recursive glob misses them
entirely (measured: ~29% of fresh input on a multi-agent-heavy corpus). The
walk is recursive, and a subdirectory file folds into the top-level session
named by its first path component: subagent spend is the parent session's
spend, one row per (session x model). Titles come from top-level files only
(a subagent's first user message is its task prompt, not a display title).

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

from . import in_repo, norm_iso, row

HARNESS = "claude"
PARSER_VERSION = "2"  # 2: recursive walk — subagent transcripts fold into parent
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


# Compact usage storage for the cache: id → [input, output, cache_read,
# cache_write], indices aligned with the row() emission below.
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens")


def _blank_state() -> dict:
    return {"per_model": {}, "title": None, "cwd": None,
            "ts_first": None, "ts_last": None, "bad": 0}


def _parse_into(path: Path, state: dict, offset: int, log) -> "int | None":
    """Parse complete lines from byte `offset` into `state` (a _blank_state
    shape, possibly resumed from cache), returning the new offset. NO
    cross-file dedupe here — per_model keeps every id in this file; the
    dedupe replays at aggregation. Binary read for byte-accurate offsets; a
    trailing line without '\\n' (live mid-append) is left unconsumed. None on
    an unreadable file."""
    try:
        fh = open(path, "rb")
    except OSError as e:
        log(f"claude: unreadable {path.name}: {e}")
        return None
    bad = 0
    with fh:
        if offset:
            fh.seek(offset)
        pos = offset
        for raw in fh:
            line = raw.decode("utf-8", errors="replace")
            if not line.strip():
                pos += len(raw)
                continue
            try:
                rec = json.loads(line)
                ok = isinstance(rec, dict)
            except json.JSONDecodeError:
                ok, rec = False, None
            if not ok and not raw.endswith(b"\n"):
                # unterminated AND unparseable: a live mid-append — leave it
                # unconsumed for the next sweep. (A finished file's last line
                # may lack '\n' but parses fine — that one is consumed.)
                break
            pos += len(raw)
            if not ok:
                bad += 1
                continue
            state["cwd"] = rec.get("cwd") or state["cwd"]
            ts = rec.get("timestamp")
            if ts:
                state["ts_first"] = state["ts_first"] or ts
                state["ts_last"] = ts
            if state["title"] is None and rec.get("type") == "user":
                state["title"] = _title_from(rec)
            msg = rec.get("message") or {}
            usage = msg.get("usage") if isinstance(msg, dict) else None
            mid = msg.get("id") if isinstance(msg, dict) else None
            if not (isinstance(usage, dict) and mid):
                continue
            # last occurrence wins (final counts) — plain dict overwrite
            state["per_model"].setdefault(msg.get("model") or "unknown", {})[mid] = \
                [usage.get(k) for k in USAGE_KEYS]
    state["bad"] += bad
    if bad:
        log(f"claude: {path.name}: {bad} unparseable line(s) tolerated")
    return pos


def _session_ref(proj: Path, path: Path) -> str:
    """The session a transcript belongs to. Top-level files ARE sessions;
    files in subdirectories (<uuid>/subagents/agent-*.jsonl) belong to the
    top-level session named by their first path component."""
    rel = path.relative_to(proj)
    if len(rel.parts) == 1:
        return str(path)
    return str(proj / (rel.parts[0] + ".jsonl"))


def _file_state(path: Path, fstate: dict, log) -> "dict | None":
    """This file's parse state — from cache when (size, mtime) match, tail-
    parsed from the cached offset when grown, re-parsed in full otherwise.
    Updates fstate in place; None for an unreadable file."""
    key = str(path)
    try:
        st = path.stat()
    except OSError as e:
        log(f"claude: unreadable {path.name}: {e}")
        fstate.pop(key, None)
        return None
    ent = fstate.get(key)
    if (isinstance(ent, dict) and ent.get("size") == st.st_size
            and ent.get("mtime") == st.st_mtime):
        return ent["state"]
    if (isinstance(ent, dict) and isinstance(ent.get("offset"), int)
            and 0 < ent["offset"] <= st.st_size and st.st_size > ent.get("size", 0)):
        state, offset = ent["state"], ent["offset"]  # append-only tail delta
    else:
        state, offset = _blank_state(), 0  # new / shrunk / rewritten → full parse
    new_off = _parse_into(path, state, offset, log)
    if new_off is None:
        fstate.pop(key, None)
        return None
    fstate[key] = {"size": st.st_size, "mtime": st.st_mtime,
                   "offset": new_off, "state": state}
    return state


def sweep(repo_root, since_epoch, log, cache=None) -> list[dict]:
    if not DATA_DIR.is_dir():
        return []
    cache = cache if isinstance(cache, dict) else {}
    fstate = cache.get("files") if isinstance(cache.get("files"), dict) else {}
    prefix = _encode(str(repo_root))
    rows: list[dict] = []
    alive: set[str] = set()
    for proj in sorted(DATA_DIR.iterdir()):
        if not (proj.is_dir() and proj.name.startswith(prefix)):
            continue
        files = sorted(proj.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            continue
        alive.update(str(p) for p in files)
        # dir-scoped incrementality (see module docstring)
        changed = any((since_epoch(_session_ref(proj, p)) or 0) < p.stat().st_mtime
                      for p in files)
        if not changed:
            continue
        seen: set = set()
        sessions: dict[str, dict] = {}  # ref → merged aggregate (insertion order)
        for path in files:  # mtime order: copies count where they first appeared
            state = _file_state(path, fstate, log)
            if state is None:
                continue
            # cross-file dedupe replay: ids count where they FIRST appeared —
            # including in off-repo files (they claim ids before the cwd filter,
            # exactly as the from-scratch parse did)
            fresh: dict[str, dict] = {}
            for model, by_id in state["per_model"].items():
                f = {mid: u for mid, u in by_id.items() if mid not in seen}
                if f:
                    fresh[model] = f
                seen.update(by_id)
            if not in_repo(state["cwd"], repo_root):
                continue
            agg = sessions.setdefault(_session_ref(proj, path), {
                "per_model": {}, "title": None, "cwd": state["cwd"],
                "started_at": None, "ended_at": None, "partial": False})
            for model, by_id in fresh.items():
                agg["per_model"].setdefault(model, {}).update(by_id)
            if path.parent == proj and agg["title"] is None:
                agg["title"] = state["title"]
            agg["started_at"] = min(
                (t for t in (agg["started_at"], norm_iso(state["ts_first"])) if t),
                default=None)
            agg["ended_at"] = max(
                (t for t in (agg["ended_at"], norm_iso(state["ts_last"])) if t),
                default=None)
            agg["partial"] = agg["partial"] or state["bad"] > 0
        for ref, agg in sessions.items():
            common = dict(harness=HARNESS, parser_version=PARSER_VERSION,
                          provider="anthropic", title=agg["title"],
                          started_at=agg["started_at"],
                          ended_at=agg["ended_at"], cwd=agg["cwd"])
            if not agg["per_model"]:
                rows.append(row(ref=ref, model=None, status="no_usage", **common))
                continue
            status = "partial" if agg["partial"] else "ok"
            for model, by_id in agg["per_model"].items():
                def total(i):
                    return sum((u[i] or 0) for u in by_id.values())
                rows.append(row(
                    ref=ref, model=model, status=status,
                    input_tokens=total(0), output_tokens=total(1),
                    cache_read_tokens=total(2), cache_write_tokens=total(3),
                    **common))
    cache["files"] = {k: v for k, v in fstate.items() if k in alive}
    return rows
