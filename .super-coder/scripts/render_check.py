#!/usr/bin/env python3
"""Fail if the committed flat `_sc` mirror drifts from the committed SOURCE.

`roadmap_sc.md` and everything under `specs_sc/`, `docs_sc/`, `skills_sc/` are
RENDERED from the DB (documents/roadmap/skills; a skill's source is
`assets/skills/<name>/SKILL.md` → seed migration → DB). Editing that source
without re-rendering and committing the mirror drifts it silently — the DB and
every shell's per-boot load stay correct, but the git-tracked browsable copy
goes stale, and nothing else catches it.

HERMETIC by construction: this builds a throwaway DB from git-tracked text
(schema + migrations + `.sc-state/content.sql`), renders the mirror from THAT
into a temp tree, and diffs it against the committed `_sc` files. It never opens
the live `shell_db.db` and never writes into the working tree. So — unlike the
old version, which rendered from the live DB *into the tree* and then told you to
`git add` whatever fell out — a stale or dirty local cache DB can no longer make
this pass or fail wrongly, and can never trick you into committing a regression.
A local `./sc render-check` is now byte-identical to CI; no `./sc rebuild` first.

    ./sc render-check
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SCHEMA = ENGINE / "schema.sql"
CONTENT_LEGACY = ENGINE / "snapshot" / "content.sql"   # pre-B7 fallback
RENDERED = ["roadmap_sc.md", "specs_sc", "docs_sc", "skills_sc"]

sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import flat  # noqa: E402
import artifact_policy  # noqa: E402
import migrate as migrate_mod  # noqa: E402

CONTENT = artifact_policy.content_path()
ACTIVE_ROOT = artifact_policy.render_root()


def _build_tracked_db(path: Path) -> None:
    """Materialize a DB from committed text only: schema → migrations →
    content.sql. No map step (the dr_* cache isn't part of the mirror) and no
    touch of the live DB. This is what a fresh `./sc rebuild` would produce, so
    its engine skills are always current — the mirror is a pure function of the
    sources about to be committed."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    con.commit()
    con.close()
    migrate_mod.migrate(str(path))
    content = CONTENT if CONTENT.exists() else CONTENT_LEGACY
    if content.exists():
        con = sqlite3.connect(path)
        con.executescript(content.read_text())
        con.commit()
        con.close()


def _rel_files(base: Path) -> set[str]:
    """Tracked-mirror files present under `base`, as repo-relative paths."""
    found: set[str] = set()
    for r in RENDERED:
        p = base / r
        if p.is_file():
            found.add(r)
        elif p.is_dir():
            found.update(str(f.relative_to(base)) for f in p.rglob("*") if f.is_file())
    return found


def main() -> int:
    artifact_policy.prepare_local_state()
    if not artifact_policy.tracks_local_artifacts() and not ACTIVE_ROOT.exists():
        print("✓ render-check: local artifact mode has no rendered instance state yet")
        return 0
    with tempfile.TemporaryDirectory(prefix="sc-render-check-") as td:
        tmp = Path(td)
        db = tmp / "hermetic.db"
        _build_tracked_db(db)

        out = tmp / "tree"
        out.mkdir()
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            flat.render_visibility(con, root=out)
        finally:
            con.close()

        # Drift = committed mirror != mirror rendered from committed source.
        rendered = _rel_files(out)
        committed = _rel_files(ACTIVE_ROOT)
        drifted = sorted(
            rel for rel in rendered | committed
            if not ((out / rel).is_file() and (ACTIVE_ROOT / rel).is_file()
                    and (out / rel).read_bytes() == (ACTIVE_ROOT / rel).read_bytes())
        )
        if drifted:
            sys.stderr.write(
                "✗ render drift: the active flat _sc mirror does not match the\n"
                "  mirror rendered from the active sources (schema + migrations +\n"
                f"  {CONTENT.relative_to(REPO_ROOT)}). A source edit was made without\n"
                "  re-rendering the mirror.\n\n  drifted:\n"
                + "".join(f"    {p}\n" for p in drifted)
                + "\n  fix:  ./sc rebuild && ./sc render flat"
                + (" && git add " + " ".join(RENDERED)
                   if artifact_policy.tracks_local_artifacts() else "") + "\n"
            )
            return 1
    print("✓ render-check: flat _sc mirror matches the render of the active sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
