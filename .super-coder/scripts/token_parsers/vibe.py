"""vibe parser — ~/.vibe/logs/session/<id>/meta.json.

One JSON per session: `stats.session_prompt_tokens` /
`session_completion_tokens`, native `title` (+ title_source), lifecycle from
start_time/end_time, repo filter from environment.working_directory, model
from config.active_model. Fidelity Partial — no cache split, so the cache
classes stay NULL ("not exposed"), never 0.

Provider: from config.models[].provider when the active model is listed;
parser-level default "mistral" otherwise (vibe is Mistral's CLI).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import in_repo, norm_iso, row

HARNESS = "vibe"
PARSER_VERSION = "1"
DATA_DIR = Path.home() / ".vibe/logs/session"


def _provider(cfg: dict, model: "str | None") -> str:
    for m in (cfg.get("models") or []):
        if isinstance(m, dict) and m.get("name") == model and m.get("provider"):
            return m["provider"]
    return "mistral"


def sweep(repo_root, since_epoch, log) -> list[dict]:
    if not DATA_DIR.is_dir():
        return []
    rows: list[dict] = []
    for meta_path in sorted(DATA_DIR.glob("*/meta.json")):
        last = since_epoch(str(meta_path))
        if last is not None and meta_path.stat().st_mtime <= last:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"vibe: unreadable {meta_path.parent.name}/meta.json: {e} — skipped")
            continue
        cwd = (meta.get("environment") or {}).get("working_directory")
        if not in_repo(cwd, repo_root):
            continue
        stats = meta.get("stats") or {}
        cfg = meta.get("config") or {}
        model = cfg.get("active_model")
        prompt, completion = stats.get("session_prompt_tokens"), stats.get("session_completion_tokens")
        rows.append(row(
            harness=HARNESS, ref=str(meta_path), parser_version=PARSER_VERSION,
            provider=_provider(cfg, model), model=model,
            title=meta.get("title"),
            started_at=norm_iso(meta.get("start_time")),
            ended_at=norm_iso(meta.get("end_time")),
            input_tokens=prompt, output_tokens=completion,
            status="ok" if (prompt is not None or completion is not None) else "no_usage",
            cwd=cwd))
    return rows
