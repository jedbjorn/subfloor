#!/usr/bin/env python3
"""Reconcile a fork after pulling a super-coder engine update.

A `git checkout super-coder/main -- .super-coder` brings new schema, migrations,
skill bodies, and render/engine code. This re-assembles the fork against it
without losing per-instance content:

    1. rebuild   — fresh .db from the new schema + migrations + your snapshot.
    2. re-grant  — grant every catalogue skill to every shell, so NEWLY-added
                   system skills reach shells that predate them (grants are
                   per-instance; the catalogue propagates, the grant doesn't).
    3. map       — refresh the dr_* repo catalogue (the repo moved on too).
    4. snapshot  — serialize the reconciled state (grants are dumped by skill
                   NAME, so this is id-churn-proof).

Then review + commit the changed text. Per-instance content (identity, memory,
roadmap, docs, flags) survives — it rides in your snapshot.

v1 grant policy: all catalogue skills → all shells (matches install). When
per-shell skill scoping lands, this step gets a policy.

Usage:
    ./sc update
    python3 .super-coder/scripts/update.py
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"
PY = sys.executable


def run(*script_args: str) -> None:
    if subprocess.run([PY, str(ENGINE / "scripts" / script_args[0]),
                       *script_args[1:]]).returncode != 0:
        sys.exit(f"update: {script_args[0]} failed.")


def regrant() -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        # Grant newly-added COMMON skills to every shell. Opt-in (common=0)
        # skills are per-shell assignments — left untouched so an update never
        # overrides who-has-which catalogue skill.
        cur = con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
            "SELECT s.shell_id, k.skill_id FROM shells s, skills k "
            "WHERE COALESCE(s.is_deleted,0)=0 AND k.is_deleted=0 AND k.common=1")
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def main() -> int:
    print("→ rebuild (new schema + migrations + your snapshot)")
    run("rebuild.py")
    print("→ re-grant catalogue skills to all shells")
    n = regrant()
    print(f"  {n} new grant(s)")
    print("→ map the repo")
    run("map_repo.py")
    print("→ snapshot")
    run("snapshot.py")
    print("\nupdate: done. Review + commit: schema.sql / migrations / "
          "snapshot/content.sql / _sc renders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
