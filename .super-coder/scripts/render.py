#!/usr/bin/env python3
"""Render DB content to disk on demand.

Wraps `render/flat.py`. The boot launcher (`run.py`) calls the render functions
directly for the chosen shell; this CLI is the standalone entry the sc dispatcher and
the (later) commit→PR automation use.

Usage:
    python3 .super-coder/scripts/render.py flat              # tracked _sc files
    python3 .super-coder/scripts/render.py skills <shortname> # .claude/skills/ for a shell
    python3 .super-coder/scripts/render.py all <shortname>    # both
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import flat  # noqa: E402
from _serialize_guard import require_admin  # noqa: E402
from seed_skills import stale_engine_skills  # noqa: E402


def _open() -> sqlite3.Connection:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(f"render: no usable DB at {DB_PATH} — run `./sc rebuild` first.")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _resolve_shell(con, shortname: str) -> int:
    row = con.execute(
        "SELECT shell_id FROM shells WHERE shortname=? AND COALESCE(is_deleted,0)=0",
        (shortname,),
    ).fetchone()
    if row is None:
        sys.exit(f"render: no shell '{shortname}'")
    return row["shell_id"]


def _guard_fresh(con) -> None:
    """Refuse to render the tracked mirror from a DB whose engine skills lag
    assets/skills/. Rendering a stale cache here is exactly how a shipped skill
    body silently gets DELETED from the committed `_sc` mirror — so we fail loud
    and point at the cure instead of writing the regression."""
    stale = stale_engine_skills(con)
    if stale:
        sys.exit(
            "render: refusing — the live DB's engine skills lag assets/skills/.\n"
            "  The DB is a cache built before a `seed-skills` change; the migrate\n"
            "  ledger won't re-seed `0001` in place, so rendering the flat mirror\n"
            "  now would write STALE content (and can DELETE shipped doc/skill text).\n"
            f"  stale: {', '.join(stale)}\n"
            "  fix:  ./sc rebuild   (or ./sc update — both re-sync the catalogue;\n"
            "        run ./sc seed-skills first if you just edited assets/skills/)"
        )


def _report(label: str, summary: dict) -> None:
    w, s = len(summary["written"]), len(summary["skipped"])
    print(f"render {label}: {w} written, {s} unchanged")
    for p in summary["written"]:
        print(f"  + {p.relative_to(flat.REPO_ROOT)}")


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(__doc__)
    mode = argv[0]
    con = _open()
    try:
        if mode == "flat":
            require_admin("render flat")
            _guard_fresh(con)
            _report("flat", flat.render_visibility(con))
        elif mode in ("skills", "all"):
            if len(argv) < 2:
                sys.exit(f"render: `{mode}` needs a shell shortname")
            if mode == "all":
                require_admin("render flat")
                _guard_fresh(con)
                _report("flat", flat.render_visibility(con))
            shell_id = _resolve_shell(con, argv[1])
            _report(f"skills[{argv[1]}]", flat.render_skill_md(con, shell_id))
        else:
            sys.exit(f"render: unknown mode '{mode}' (flat | skills <shell> | all <shell>)")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
