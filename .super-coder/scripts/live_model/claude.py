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
that could be this worktree's — verified live: the dev5 project dir holds 8
sessions and the newest is the running one, its mtime advancing during the
scan. "Could be" and not "is": a file we cannot read, or one written before
its session's first user turn, carries no `cwd` to match, and treating that
silence as someone else's file is what turns the PREVIOUS session's model into
a confident `ok` (see `_scan`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import norm_iso, note, placeholder

HARNESS = "claude"
DATA_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"

# What a transcript file says about the worktree it belongs to.
MINE, FOREIGN, UNKNOWN = "mine", "foreign", "unknown"


def _encode(path: str) -> str:
    """The harness's project-dir encoding of a cwd. Lossy — prefilter only.

    Lossy in ONE direction only: it maps each character to exactly one
    character, so several cwds can share a dir name but a cwd can never
    produce a name of a different length. That is what makes a project dir
    name LONGER than `_encode(worktree)` provably not this worktree's.
    """
    return "".join(c if c.isalnum() else "-" for c in path)


def _scan(path: Path, worktree: str) -> tuple:
    """(claim, hit) for one transcript file, walking BACKWARD.

    Backward is the point: the last explicit id wins, so an A->B->A switch
    resolves to the final A without ever consulting what came before it. A
    line that will not parse is skipped and the walk continues (spec doc 44:
    "malformed tail line -> skip backward to the last parseable record").

    The claim is three-valued, and `unknown` is NOT `foreign`:

      mine     a record carries `cwd == worktree`
      foreign  a record carries a different `cwd` — the encoding collided
      unknown  the file said nothing about its cwd: unreadable, or read in
               full without a single `cwd` record. Every real transcript
               opens with cwd-less lines (`queue-operation`, `ai-title`,
               `mode`), so a session that has not reached its first user turn
               is exactly this shape.

    Answering `foreign` for an unreadable file is how an unreadable CURRENT
    transcript fell through to an OLDER session of the same worktree and got
    reported as the live model — a false `ok`, which is the one answer this
    feature exists to never give.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        note(HARNESS, f"unreadable transcript {path}: {type(e).__name__}: {e}")
        return UNKNOWN, None
    claim = UNKNOWN
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
        if cwd:
            if cwd != worktree:
                return FOREIGN, None  # another worktree's session — encoding collided
            claim = MINE
        if rec.get("type") != "assistant":
            continue
        model = (rec.get("message") or {}).get("model")
        if placeholder(model):
            continue
        return claim, {"model": model,
                       "observed_at": norm_iso(rec.get("timestamp")),
                       "source": str(path)}
    return claim, None


def read(worktree) -> "dict | None":
    worktree = str(worktree)
    if not DATA_DIR.is_dir():
        return None
    prefix = _encode(worktree)
    cands = []
    for proj in DATA_DIR.iterdir():
        if not (proj.is_dir() and proj.name.startswith(prefix)):
            continue
        # Only a dir named EXACTLY this cwd's encoding can hold this cwd's
        # sessions (see `_encode`) — so in a longer-named dir, a file that
        # claims nothing claims nothing about US and is skipped rather than
        # allowed to shadow the real session with `none`.
        exact = proj.name == prefix
        for p in proj.glob("*.jsonl"):  # NON-recursive: subagents live below this
            try:
                cands.append((p.stat().st_mtime, str(p), p, exact))
            except OSError as e:
                note(HARNESS, f"unreadable transcript {p}: {type(e).__name__}: {e}")
                continue
    # Newest first; the first file that could be this worktree's current
    # session is the answer, and its answer stands even when that answer is
    # "nothing yet" — walking on past it reports a DEAD session's model as
    # live, which is worse than reporting nothing.
    for _, _, path, exact in sorted(cands, key=lambda c: (c[0], c[1]), reverse=True):
        claim, hit = _scan(path, worktree)
        if claim == MINE or (claim == UNKNOWN and exact):
            return hit
    return None
