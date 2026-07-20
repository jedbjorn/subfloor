#!/usr/bin/env python3
"""Engine hash manifest — the local-edit guard for the materialized engine.

The engine (`.super-coder/` + `sc`) is a wholesale-overwritten dependency:
`./sc update` materializes upstream's tree over the top with no merge. That is
correct for the B7 model — engine files are not fork-edited — but it means a
local patch to an engine file would be DISCARDED SILENTLY the next time the
fork updates. This module closes that hole:

    write_manifest()   after every materialize (and at install), record a
                       sha256 per engine file → .super-coder/engine.manifest
    local_edits()      before the next materialize, hash the same files and
                       report any that were modified or deleted since

update.py blocks on a non-empty local_edits() unless --force, pointing the
operator at the real choices: revert the edit, upstream it, or `./sc eject`.

The manifest is derived machine state, not fork content: it lives inside the
gitignored engine dir and is never committed (eject deletes it — an ejected
engine is fork source, and git itself tracks edits from there). Lives in its
own module because update.py imports install.py (shared helpers) — putting
this in either would make the other's import circular.

Limits, by design: files ADDED locally under `.super-coder/` are not in the
manifest and are not detected — materialize never touches them, so nothing is
lost. A fork whose engine predates the manifest (no file yet) gets its first
one written by the next update; that first update cannot check.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
MANIFEST = ENGINE / "engine.manifest"

# Fork-facing templates are materialized through the templates directory in
# ENGINE_PATHS below. Keep their exact paths named here so distribution tests
# can guard both the template files and their engine-manifest coverage.
FORK_TEMPLATE_PATHS = (
    ".super-coder/templates/fork/subfloor-visual-qa.yml",
    ".super-coder/templates/fork/visual-qa.example.json",
)

# The ENGINE = system content that propagates to every fork; all of it is safe
# to materialize from the super-coder remote (it is wholesale-replaced, never
# fork-edited). The per-instance set is deliberately NOT listed, so a materialize
# never touches it: `.sc-state/` (this fork's content.sql + map tuning + engine
# pin), shell_db.db* (gitignored), instance.json (gitignored). assets/seed/ is
# super-coder-only (stripped on install); assets/shells/ is empty/vestigial.
# Lives here (not update.py) so install.py can write the first manifest without
# a circular import; update.py re-exports it as update.ENGINE_PATHS.
ENGINE_PATHS = [
    "sc",
    ".super-coder/aliases.mk",
    ".super-coder/Dockerfile",
    ".super-coder/schema.sql",
    ".super-coder/map_schema.sql",
    ".super-coder/ecosystem.config.cjs",
    ".super-coder/README.md",
    ".super-coder/docs",
    ".super-coder/migrations",
    ".super-coder/scripts",
    ".super-coder/render",
    ".super-coder/templates",
    ".super-coder/adapters",
    ".super-coder/api",
    ".super-coder/ui",
    ".super-coder/assets/skills",
    ".super-coder/hooks",
]

# Never hashed: bytecode + caches that churn without a source edit.
_SKIP_NAMES = {"__pycache__", "node_modules", ".svelte-kit"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(engine_paths: list[str]) -> list[Path]:
    """Every regular file the materialize set covers, as REPO_ROOT-relative
    paths. `engine_paths` entries are files or directories (update.ENGINE_PATHS
    form); directories are walked whole, mirroring what `git archive` emits."""
    out: list[Path] = []
    for entry in engine_paths:
        p = REPO_ROOT / entry
        if p.is_file():
            out.append(p.relative_to(REPO_ROOT))
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.suffix == ".pyc":
                    continue
                if _SKIP_NAMES & set(f.relative_to(REPO_ROOT).parts):
                    continue
                out.append(f.relative_to(REPO_ROOT))
    return out


def write_manifest(engine_paths: list[str], files: list[str] | None = None) -> int:
    """Record the engine's materialized state (call right after a materialize,
    when disk == upstream). Returns the file count.

    `files` — update/rollback pass the `git ls-tree` listing at the
    materialized ref — is the EXACT upstream file set, so the manifest never
    absorbs files that merely sit under an engine dir without being
    upstream-owned: a fork-local skill's SKILL.md added to assets/skills/
    (#253), or an upstream-retired file lingering on disk. Neither may guard —
    and later block — a future update. Without `files` (install: disk == a
    pristine clone), the engine dirs are walked on disk."""
    if files is None:
        rels = _iter_files(engine_paths)
    else:
        rels = [Path(f) for f in files
                if not (_SKIP_NAMES & set(Path(f).parts))
                and (REPO_ROOT / f).is_file()]
    entries = {str(rel): _sha256(REPO_ROOT / rel) for rel in rels}
    MANIFEST.write_text(json.dumps(entries, indent=0, sort_keys=True) + "\n")
    return len(entries)


def local_edits() -> dict[str, str]:
    """Engine files changed since the manifest was written: {path: 'modified' |
    'deleted'}. Empty dict = clean, or no manifest yet (nothing to compare)."""
    if not MANIFEST.exists():
        return {}
    try:
        recorded: dict[str, str] = json.loads(MANIFEST.read_text())
    except json.JSONDecodeError:
        return {}  # corrupt manifest — treat as absent; the next write heals it
    edits: dict[str, str] = {}
    for rel, digest in recorded.items():
        p = REPO_ROOT / rel
        if not p.is_file():
            edits[rel] = "deleted"
        elif _sha256(p) != digest:
            edits[rel] = "modified"
    return edits
