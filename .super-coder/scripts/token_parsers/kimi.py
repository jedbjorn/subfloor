"""kimi parser — ~/.kimi-code/sessions/wd_*/session_*/ (Kimi Code CLI).

The cleanest source (fidelity Full, live-verified against a running K3
session): `state.json` carries createdAt/updatedAt, workDir
(worktree-precise), native title (+ isCustomTitle); each agent's
`agents/*/wire.jsonl` emits `usage.record` events whose four fields map 1:1
to the four token classes (inputOther IS fresh input — no arithmetic).
Sessions can run multiple agents (swarm mode) — usage sums across every
agents/*/wire.jsonl, grouped per model (`usage.record.model`, the
"kimi-code/k3" alias form). Provider comes from `llm.request` events, which
report provider="kimi" natively — the same value run.py pins at boot.

Ref = the session directory; incrementality is the max mtime of state.json
+ wire files (a live session keeps its dir "changed" and re-sums — the
upsert refreshes in place).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import in_repo, norm_iso, row

HARNESS = "kimi"
PARSER_VERSION = "1"
DATA_DIR = Path.home() / ".kimi-code/sessions"

USAGE_MAP = {  # wire.jsonl usage.record field → row key, 1:1
    "inputOther": "input_tokens",
    "output": "output_tokens",
    "inputCacheRead": "cache_read_tokens",
    "inputCacheCreation": "cache_write_tokens",
}


def sweep(repo_root, since_epoch, log, cache=None) -> list[dict]:
    if not DATA_DIR.is_dir():
        return []
    rows: list[dict] = []
    for sess_dir in sorted(DATA_DIR.glob("wd_*/session_*")):
        state_path = sess_dir / "state.json"
        if not state_path.exists():
            continue
        wires = sorted(sess_dir.glob("agents/*/wire.jsonl"))
        try:
            newest = max(p.stat().st_mtime for p in [state_path, *wires])
        except OSError:
            continue
        last = since_epoch(str(sess_dir))
        if last is not None and newest <= last:
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"kimi: unreadable {sess_dir.name}/state.json: {e} — skipped")
            continue
        cwd = state.get("workDir")
        if not in_repo(cwd, repo_root):
            continue
        per_model: dict[str, dict] = {}
        provider = None
        for wire in wires:
            try:
                fh = open(wire, encoding="utf-8", errors="replace")
            except OSError as e:
                log(f"kimi: unreadable {sess_dir.name}/{wire.parent.name}/wire.jsonl: {e}")
                continue
            with fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("type") == "llm.request":
                        provider = rec.get("provider") or provider
                    elif rec.get("type") == "usage.record":
                        usage = rec.get("usage") or {}
                        agg = per_model.setdefault(rec.get("model") or "unknown", {})
                        for src, dst in USAGE_MAP.items():
                            if usage.get(src) is not None:
                                agg[dst] = agg.get(dst, 0) + usage[src]
        common = dict(harness=HARNESS, ref=str(sess_dir), parser_version=PARSER_VERSION,
                      provider=provider or "kimi", title=state.get("title"),
                      started_at=norm_iso(state.get("createdAt")),
                      ended_at=norm_iso(state.get("updatedAt")), cwd=cwd,
                      native_session_id=state.get("id"))
        if not per_model:
            rows.append(row(model=None, status="no_usage", **common))
            continue
        for model, agg in per_model.items():
            rows.append(row(model=model, **agg, **common))
    return rows
