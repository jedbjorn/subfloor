"""kimi parser — ~/.kimi-code/sessions/wd_*/session_*/ (Kimi Code CLI).

The cleanest source (fidelity Full, live-verified against a running K3
session): `state.json` carries createdAt/updatedAt, workDir
(worktree-precise), native title (+ isCustomTitle); each agent's
`agents/*/wire.jsonl` emits `usage.record` events whose four fields map 1:1
to the four token classes (inputOther IS fresh input — no arithmetic).
Sessions can run multiple agents (swarm mode) — usage sums across every
agents/*/wire.jsonl, grouped per model (`usage.record.model`, the
"kimi-code/k3" alias form). Provider is pinned to "kimi" — the same value run.py pins at boot.

State format drift (Kimi Code 2.x, `"version": 2`): `workDir` became `cwd`
and createdAt/updatedAt became epoch milliseconds; both shapes are read.
`llm.request.provider` now names the wire protocol ("openai"), not the
vendor, so the row provider is pinned to "kimi" (run.session_provider's
boot value) instead of read from the wire. `usage.record` with
usageScope="session" is a compaction call — real spend, summed like turns.

Ref = the session directory; incrementality is the max mtime of state.json
+ wire files (a live session keeps its dir "changed" and re-sums — the
upsert refreshes in place).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import in_repo, iso_utc, norm_iso, row

HARNESS = "kimi"
PARSER_VERSION = "2"
DATA_DIR = Path.home() / ".kimi-code/sessions"

USAGE_MAP = {  # wire.jsonl usage.record field → row key, 1:1
    "inputOther": "input_tokens",
    "output": "output_tokens",
    "inputCacheRead": "cache_read_tokens",
    "inputCacheCreation": "cache_write_tokens",
}


def _ts(value) -> str | None:
    """v1 ISO string or v2 epoch-milliseconds → ISO UTC."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return iso_utc(value / 1000)
    return norm_iso(value)


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
        cwd = state.get("workDir") or state.get("cwd")
        if not in_repo(cwd, repo_root):
            continue
        per_model: dict[str, dict] = {}
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
                    if rec.get("type") == "usage.record":
                        usage = rec.get("usage") or {}
                        agg = per_model.setdefault(rec.get("model") or "unknown", {})
                        for src, dst in USAGE_MAP.items():
                            if usage.get(src) is not None:
                                agg[dst] = agg.get(dst, 0) + usage[src]
        common = dict(harness=HARNESS, ref=str(sess_dir), parser_version=PARSER_VERSION,
                      provider="kimi", title=state.get("title"),
                      started_at=_ts(state.get("createdAt")),
                      ended_at=_ts(state.get("updatedAt")), cwd=cwd)
        if not per_model:
            rows.append(row(model=None, status="no_usage", **common))
            continue
        for model, agg in per_model.items():
            rows.append(row(model=model, **agg, **common))
    return rows
