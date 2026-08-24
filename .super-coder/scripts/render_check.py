#!/usr/bin/env python3
"""Validate a local flat `_sc` mirror against this checkout's engine sources.

`roadmap_sc.md` and everything under `specs_sc/`, `docs_sc/`, `skills_sc/` are
RENDERED from the DB (documents/roadmap/skills; a skill's source is
`assets/skills/<name>/SKILL.md` → seed migration → DB). Editing that source
without re-rendering leaves the local browsable copy stale.

HERMETIC verdict: this builds a throwaway DB from authored engine text
(schema + migrations), the local instance snapshot, and the fork skill-retire
list, renders the mirror from THAT into a temp tree, and diffs it against the
active local `_sc` files. It never writes into the working tree. On drift only,
it may read the live `shell_db.db` to report which document source fields differ;
those diagnostics never influence the verdict. So — unlike the old version,
which rendered from the live DB *into the tree* and then told you to `git add`
whatever fell out — a stale or dirty local cache DB can no longer make this pass
or fail wrongly, and can never trick you into committing a regression. A local
`./sc render-check` verdict is byte-identical to CI; no `./sc rebuild` first.

    ./sc render-check
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SCHEMA = ENGINE / "schema.sql"
CONTENT_LEGACY = ENGINE / "snapshot" / "content.sql"   # pre-B7 fallback
RENDERED = ["roadmap_sc.md", "specs_sc", "docs_sc", "skills_sc"]

sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import flat  # noqa: E402
import migrate as migrate_mod  # noqa: E402
import seed_skills  # noqa: E402

CONTENT = artifact_policy.content_path()
ACTIVE_ROOT = artifact_policy.render_root()
LIVE_DB = ENGINE / "shell_db.db"


def _build_tracked_db(path: Path) -> None:
    """Materialize a DB from authored engine text plus local instance state:
    content.sql plus the fork skill-retire list. No map step (the dr_* cache
    isn't part of the mirror) and no touch of the live DB. This is what a fresh
    `./sc rebuild` would produce, so its engine skills are always current — the
    mirror is a pure function of the sources about to be committed."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    con.commit()
    con.close()
    migrate_mod.migrate(str(path))
    content = CONTENT if CONTENT.exists() else CONTENT_LEGACY
    con = sqlite3.connect(path)
    try:
        if content.exists():
            con.executescript(content.read_text())
            con.commit()
        seed_skills.apply_retired(con)
    finally:
        con.close()


def _active_content() -> Path:
    """The content file `_build_tracked_db` will actually read."""
    return CONTENT if CONTENT.exists() else CONTENT_LEGACY


def _target_lines() -> list[str]:
    """The paths this verdict is ABOUT (spec #68 req 1).

    `./sc render-check` runs the CALLER's engine, so "✓ matches" is a claim about
    one specific checkout — and it used to be the main one, whichever worktree
    you typed it in. Printed BEFORE the work, so a crash mid-render leaves the
    same attribution a success or a drift report does.
    """
    return [
        f"  source root : {REPO_ROOT}",
        f"  engine      : {ENGINE}",
        f"  content     : {_active_content()}",
        f"  mirror      : {ACTIVE_ROOT}",
    ]


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


def _document_source_rows(con: sqlite3.Connection, rel: str) -> list[dict]:
    """Summarize every DB row capable of rendering one managed document."""
    rows = con.execute(
        "SELECT d.document_id, d.feature_id, d.kind, d.seq, d.title, d.body, "
        "d.render_path, d.frozen, r.title AS feature_title, r.roadmap_status "
        "FROM documents d LEFT JOIN roadmap r ON r.feature_id=d.feature_id "
        "ORDER BY d.document_id"
    ).fetchall()
    summaries = []
    for row in rows:
        if flat.document_rel_path(row) != rel:
            continue
        body = row["body"] or ""
        summaries.append({
            "document_id": row["document_id"],
            "feature_id": row["feature_id"],
            "kind": row["kind"],
            "seq": row["seq"],
            "title": row["title"],
            "render_path": row["render_path"],
            "frozen": bool(row["frozen"]),
            "feature_title": row["feature_title"],
            "roadmap_status": row["roadmap_status"],
            "body_bytes": len(body.encode()),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        })
    return summaries


def _document_source_diagnostics(
    source: sqlite3.Connection,
    live: sqlite3.Connection,
    drifted: list[str],
) -> str:
    lines = []
    for rel in drifted:
        if not rel.startswith(("specs_sc/", "docs_sc/")):
            continue
        source_rows = _document_source_rows(source, rel)
        live_rows = _document_source_rows(live, rel)
        lines.append(f"  source rows for {rel}:")
        if source_rows == live_rows:
            lines.append("    snapshot and live DB rows match; the active mirror is stale")
            continue
        lines.append(f"    snapshot: {json.dumps(source_rows, sort_keys=True)}")
        lines.append(f"    live DB : {json.dumps(live_rows, sort_keys=True)}")
    return "\n".join(lines)


def main() -> int:
    print("render-check: verifying the tracked sources of this checkout")
    for line in _target_lines():
        print(line)
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

        # Drift = active local mirror != mirror rendered from active sources.
        rendered = _rel_files(out)
        committed = _rel_files(ACTIVE_ROOT)
        drifted = sorted(
            rel for rel in rendered | committed
            if not ((out / rel).is_file() and (ACTIVE_ROOT / rel).is_file()
                    and (out / rel).read_bytes() == (ACTIVE_ROOT / rel).read_bytes())
        )
        if drifted:
            diagnostics = ""
            if LIVE_DB.is_file():
                try:
                    with (
                        closing(
                            sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
                        ) as live,
                        closing(sqlite3.connect(db)) as source,
                    ):
                        live.row_factory = sqlite3.Row
                        source.row_factory = sqlite3.Row
                        diagnostics = _document_source_diagnostics(
                            source, live, drifted
                        )
                except sqlite3.Error as exc:
                    diagnostics = f"  source-row diagnostics unavailable: {exc}"
            sys.stderr.write(
                "✗ render drift: the active flat _sc mirror does not match the\n"
                "  mirror rendered from the active sources (schema + migrations +\n"
                f"  {CONTENT.relative_to(REPO_ROOT)}). A source edit was made without\n"
                "  re-rendering the mirror.\n\n"
                + "".join(f"{line}\n" for line in _target_lines())
                + "\n  drifted:\n"
                + "".join(f"    {p}\n" for p in drifted)
                + (f"\n{diagnostics}\n" if diagnostics else "")
                + "\n  fix:  ./sc rebuild && ./sc render flat\n"
            )
            return 1
    print("✓ render-check: flat _sc mirror matches the render of the active sources")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
