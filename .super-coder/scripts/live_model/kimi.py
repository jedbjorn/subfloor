"""kimi probe — ~/.kimi-code/sessions/wd_<name>_<hash>/session_<uuid>/.

`state.json` carries the absolute `workDir`, so the worktree match is exact —
no path encoding to undo (the `wd_` dir name embeds the cwd's basename, but the
hash makes it a hint, not an identity).

The transcript is a protocol wire log per agent, `agents/<agent>/wire.jsonl`.
Main vs subagent is STRUCTURAL and unambiguous: `agents/main/` is the main
thread, `agents/agent-N/` are subagents, and `state.json.agents` types each one
(`main` has `parentAgentId: null`). Reading only `agents/main/wire.jsonl` is
therefore the whole subagent defence — in a captured run the main thread ends
on `kimi-code/kimi-for-coding` while its subagent ran `kimi-code/k3`.

Model id: `llm.request` records carry BOTH `model` (`k3`) and `modelAlias`
(`kimi-code/k3`). The scout flagged the bare-vs-prefixed pair as a thing to
pick, not mix. We report `modelAlias`: it is the form the operator selects
(`kimi -m kimi-code/k3`), the form `config.toml`'s `default_model` uses, and
the form `usage.record` echoes — the bare `model` is the provider's wire name.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import iso_utc, placeholder

HARNESS = "kimi"
DATA_DIR = Path.home() / ".kimi-code/sessions"


def _last_model(wire: Path) -> "dict | None":
    """The last `llm.request` on this wire carrying an explicit alias."""
    try:
        lines = wire.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed line — keep walking back
        if not isinstance(rec, dict) or rec.get("type") != "llm.request":
            continue
        model = rec.get("modelAlias")
        if placeholder(model):
            continue
        ms = rec.get("time")
        return {"model": model,
                "observed_at": iso_utc(ms / 1000) if isinstance(ms, (int, float)) else None,
                "source": str(wire)}
    return None


def read(worktree) -> "dict | None":
    worktree = str(worktree)
    if not DATA_DIR.is_dir():
        return None
    best = None
    for sess in DATA_DIR.glob("wd_*/session_*"):
        try:
            state = json.loads((sess / "state.json").read_text(
                encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("workDir") != worktree:
            continue
        wire = sess / "agents" / "main" / "wire.jsonl"  # main thread ONLY
        try:
            mtime = wire.stat().st_mtime
        except OSError:
            continue
        key = (mtime, str(sess))
        if best is None or key > best[0]:
            best = (key, wire)
    return _last_model(best[1]) if best else None
