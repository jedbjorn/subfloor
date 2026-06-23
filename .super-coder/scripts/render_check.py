#!/usr/bin/env python3
"""Fail if the committed flat `_sc` mirror drifts from the DB render.

`roadmap_sc.md` and everything under `specs_sc/`, `docs_sc/`, `skills_sc/` are
RENDERED from the DB (documents/roadmap/skills tables; a skill's source is
`assets/skills/<name>/SKILL.md` → seed migration → DB). Editing that source
without re-rendering and committing the mirror drifts it silently — the DB and
every shell's per-boot load stay correct, but the git-tracked browsable copy
goes stale, and nothing else catches it. This is that guard: render, then fail
on any diff in those paths.

Hermetic use (CI / clean checkout):

    ./sc rebuild && ./sc render-check

`rebuild` materializes the DB from committed text (schema + migrations +
content.sql), so the check compares the committed mirror against the committed
*source*. Local use assumes a current DB — snapshot first if you edited it live.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
RENDERED = ["roadmap_sc.md", "specs_sc", "docs_sc", "skills_sc"]


def main() -> int:
    # Render from the current DB into the tree (no-op for paths already current).
    rendered = subprocess.run(
        [sys.executable, str(ENGINE / "scripts" / "render.py"), "flat"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "SC_ADMIN": "1"},  # CI/admin verify — clear serialize guard
    )
    if rendered.returncode != 0:
        return rendered.returncode

    paths = [p for p in RENDERED if (REPO_ROOT / p).exists()]
    drifted = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--quiet", "--", *paths]
    ).returncode != 0
    if drifted:
        stat = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "--stat", "--", *paths],
            capture_output=True, text=True,
        ).stdout
        sys.stderr.write(
            "✗ render drift: the committed flat _sc mirror does not match the DB "
            "render.\n  A source edit (asset/DB) was committed without re-rendering "
            "the mirror.\n\n" + stat + "\n"
            "  fix:  ./sc render flat && git add " + " ".join(paths) + "\n"
        )
        return 1
    print("✓ render-check: flat _sc mirror matches the DB render")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
