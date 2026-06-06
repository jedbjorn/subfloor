#!/usr/bin/env python3
"""Reconcile a fork after a super-coder engine update — IN PLACE.

The shell updates its own substrate: it pulls the new engine, lays new
migrations under its own feet, and keeps every row it has written. This is the
local shell handing off to its next boot — not a destructive rebuild. Because
all state lives in the DB and engine code is read live each session, a code-only
update needs no DB work; only schema changes touch the DB, and they do so as
in-place migrations (never a rebuild-from-snapshot, which would revert the DB to
the last snapshot and lose unsnapshotted in-session writes).

Flow:
    1. fetch + checkout the engine from the super-coder remote (code, schema,
       migrations, skills). Per-instance content (snapshot/, the DB,
       instance.json) is never listed, so it survives untouched. --no-fetch
       uses the working tree as-is.
    2. back up the live DB (restore point).
    3. migrate IN PLACE — apply only un-applied migrations (ledger-tracked),
       preserving all rows incl. in-session writes. No DB yet (fresh fork) ->
       fall back to a from-text rebuild.
    4. sync the skills catalogue (idempotent, id-stable UPSERT) — new/changed
       skills reach the fork without a rebuild.
    5. re-grant common skills to all shells.
    6. wire the auto-remap hooks + map the repo + snapshot the (live) state.

Then review + commit. Restart the session to boot onto the new floor.

Usage:
    ./sc update [--no-fetch] [--branch <name>]
    python3 .super-coder/scripts/update.py [--no-fetch] [--branch <name>]
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
PY = sys.executable

sys.path.insert(0, str(ENGINE / "scripts"))
import migrate as migrate_mod  # noqa: E402
import rebuild as rebuild_mod  # noqa: E402
import seed_skills  # noqa: E402

# The ENGINE = system content that propagates to every fork; all of it is safe
# to overwrite from the super-coder remote. The per-instance set is deliberately
# NOT listed, so a checkout never touches it: snapshot/ (this fork's content),
# shell_db.db* (gitignored), instance.json (gitignored), map.config.json (the
# cartographer's per-repo map tuning). assets/seed/ is super-coder-only (stripped
# on install); assets/shells/ is empty/vestigial.
ENGINE_PATHS = [
    "sc",
    ".super-coder/aliases.mk",
    ".super-coder/schema.sql",
    ".super-coder/ecosystem.config.cjs",
    ".super-coder/README.md",
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


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"update: `git {' '.join(args)}` failed:\n{r.stderr.strip()}")
    return r


def run_script(name: str) -> None:
    if subprocess.run([PY, str(ENGINE / "scripts" / name)]).returncode != 0:
        sys.exit(f"update: {name} failed.")


def super_coder_remote() -> str:
    """The remote pointing at super-coder. Prefer a URL match (robust to a
    rename), else a remote literally named 'super-coder'."""
    named = None
    for line in git("remote", "-v", check=False).stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        if "super-coder" in url:
            return name
        if name == "super-coder":
            named = name
    if named:
        return named
    sys.exit("update: no super-coder remote found. Add it:\n"
             "  git remote add super-coder https://github.com/jedbjorn/super-coder.git")


def fetch_engine(branch: str) -> None:
    remote = super_coder_remote()
    print(f"→ fetch {remote} + checkout engine ({remote}/{branch})")
    git("fetch", remote, branch)
    # Only the engine paths — per-instance content is never named, so it is
    # untouched. A single canonical list (ENGINE_PATHS), not README prose.
    git("checkout", f"{remote}/{branch}", "--", *ENGINE_PATHS)


def migrate_or_rebuild() -> None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        print("→ no live DB (fresh fork) — building from text")
        rebuild_mod.main([])
        return
    rebuild_mod.backup_existing()  # restore point before any structural change
    print("→ migrate in place (pending migrations → the live DB; data preserved)")
    migrate_mod.migrate(str(DB_PATH))


def sync_skills() -> None:
    """Re-apply the skills seed against the live DB. The seed is id-stable
    (retire-missing + UPSERT by name), so new/changed catalogue skills land
    without a rebuild and existing skill_ids — and the grants that reference
    them — stay valid. The migrate ledger would otherwise skip the already-
    stamped seed file; catalogue currency is a per-update sync, not a one-time
    migration."""
    seed = seed_skills.OUT
    if not seed.exists():
        print("  (no skills seed to sync)")
        return
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(seed.read_text())
        con.commit()
    finally:
        con.close()
    print(f"  synced catalogue from {seed.name}")


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


def main(argv: list[str]) -> int:
    no_fetch = "--no-fetch" in argv
    branch = "main"
    if "--branch" in argv:
        i = argv.index("--branch")
        if i + 1 < len(argv):
            branch = argv[i + 1]

    if no_fetch:
        print("→ --no-fetch: reconciling against the current working tree")
    else:
        fetch_engine(branch)

    migrate_or_rebuild()

    print("→ sync skills catalogue (id-stable)")
    sync_skills()
    print("→ re-grant catalogue skills to all shells")
    print(f"  {regrant()} new grant(s)")
    print("→ wire map automation + map the repo")
    run_script("map_setup.py")
    print("→ snapshot the live state")
    run_script("snapshot.py")

    print("\nupdate: done — new floor laid in place; your rows are intact.")
    print("  Review + commit: schema.sql / migrations / snapshot/content.sql / _sc renders.")
    print("  Restart your session to boot onto the new floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
