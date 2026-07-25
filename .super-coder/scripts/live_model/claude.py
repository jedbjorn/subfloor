"""claude probe — transcript JSONL under ~/.claude/projects/<encoded-cwd>/.

Every assistant record carries `message.model` verbatim (`claude-opus-5`,
`claude-fable-5`, …), so the read is a backward walk to the last one.

Two shapes decide correctness here, both verified in real bytes by the 44-U0
scout:

  * Subagents are a SEPARATE FILE, not a flagged record —
    `<project-dir>/<session-uuid>/subagents/agent-<id>.jsonl`. Zero of 1234
    top-level session files carry `isSidechain:true`. So a NON-RECURSIVE glob
    of the project dir is already main-thread-only, and that glob is the whole
    subagent defence: a captured run has the main thread on `claude-sonnet-5`
    and its subagent on `claude-haiku-4-5-20251001`, which is exactly the id
    that must never be reported as the session model. (A second `isSidechain`
    guard would be redundant today and would only blunt the mutation that
    pins this one.)

  * `<synthetic>` is a real value in the wild — 50 records on the scout's
    host, e.g. an assistant record whose content is "No response requested."
    It is skipped like a malformed line and the walk continues (`placeholder`).

The project-dir name is a lossy encoding of the cwd (`/`, `.` and `-` all land
on `-`), so it is only a prefix PREFILTER; the per-record `cwd` decides which
worktree a file belongs to. Current session = the newest-mtime top-level file
whose `cwd` matches — verified live: the dev5 project dir holds 8 sessions and
the newest is the running one, its mtime advancing during the scan.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import norm_iso, placeholder

HARNESS = "claude"
DATA_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _encode(path: str) -> str:
    """The harness's project-dir encoding of a cwd. Lossy — prefilter only."""
    return "".join(c if c.isalnum() else "-" for c in path)


def _scan(path: Path, worktree: str) -> tuple:
    """(belongs_to_worktree, hit) for one transcript file, walking BACKWARD.

    Backward is the point: the last explicit id wins, so an A->B->A switch
    resolves to the final A without ever consulting what came before it. A
    line that will not parse is skipped and the walk continues (spec doc 44:
    "malformed tail line -> skip backward to the last parseable record").
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        cwd = rec.get("cwd")
        if cwd and cwd != worktree:
            return False, None  # another worktree's session — the encoding collided
        if rec.get("type") != "assistant":
            continue
        model = (rec.get("message") or {}).get("model")
        if placeholder(model):
            continue
        return True, {"model": model,
                      "observed_at": norm_iso(rec.get("timestamp")),
                      "source": str(path)}
    return True, None


def read(worktree) -> "dict | None":
    worktree = str(worktree)
    if not DATA_DIR.is_dir():
        return None
    prefix = _encode(worktree)
    cands = []
    for proj in DATA_DIR.iterdir():
        if not (proj.is_dir() and proj.name.startswith(prefix)):
            continue
        for p in proj.glob("*.jsonl"):  # NON-recursive: subagents live below this
            try:
                cands.append((p.stat().st_mtime, str(p), p))
            except OSError:
                continue
    # Newest first; the first file that IS this worktree's is the current
    # session, and its answer stands even when that answer is "nothing yet".
    for _, _, path in sorted(cands, key=lambda c: (c[0], c[1]), reverse=True):
        mine, hit = _scan(path, worktree)
        if mine:
            return hit
    return None
