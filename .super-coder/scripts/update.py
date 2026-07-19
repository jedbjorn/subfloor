#!/usr/bin/env python3
"""Reconcile a fork after a super-coder engine update — IN PLACE.

The shell updates its own substrate: it pulls the new engine, lays new
migrations under its own feet, and keeps every row it has written. This is the
local shell handing off to its next boot — not a destructive rebuild. Because
all state lives in the DB and engine code is read live each session, a code-only
update needs no DB work; only schema changes touch the DB, and they do so as
in-place migrations (never a rebuild-from-snapshot, which would revert the DB to
the last snapshot and lose unsnapshotted in-session writes).

B7 model: the engine is a **gitignored, materialized dependency** — it is not
committed to the fork. So an update FETCHES the engine and MATERIALIZES it into
`.super-coder/` (copy from the fetched ref), instead of `git checkout`ing tracked
paths. The upstream SHA is pinned in `.sc-state/engine.ref`; the previous pin is
kept as `.sc-state/engine.ref.prev` — the engine half of the restore point that
makes `./sc rollback` sound (DB + engine restored together).

Flow:
    1. capture the restore point: the current `engine.ref` → `engine.ref.prev`.
    2. fetch upstream; materialize the engine paths at the new ref into the
       gitignored `.super-coder/` dir; write the new `engine.ref`. Per-instance
       content (`.sc-state/`, the DB, instance.json) is never in the materialize
       set, so it survives untouched. --no-fetch reconciles the working tree as-is.
    3. back up the live DB (the other half of the restore point).
    4. migrate IN PLACE — apply only un-applied migrations (ledger-tracked),
       preserving all rows incl. in-session writes. No DB yet (fresh fork) ->
       fall back to a from-text rebuild.
    5. sync the engine skills catalogue (idempotent, id-stable UPSERT) —
       new/changed engine skills reach the fork without a rebuild, while
       project-local skills are left intact.
    6. re-grant common skills to all shells.
    7. wire the auto-remap hooks + map the repo + snapshot the (live) state.

Then review + commit (only `.sc-state/` — content.sql + engine.ref — moves; the
engine is ignored). Restart the session to boot onto the new floor.

The materialize is guarded by the engine hash manifest (engine_manifest.py):
an engine file locally modified since the last materialize BLOCKS the update
(instead of being silently overwritten) until the operator reverts it,
upstreams it, or passes --force to discard it. `--ref <tag|sha>` pins the
materialize to a specific upstream version instead of the branch head.

Usage:
    ./sc update [--no-fetch] [--branch <name>] [--ref <tag|sha>] [--force]
    python3 .super-coder/scripts/update.py [same flags]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
STATE_DIR = REPO_ROOT / ".sc-state"
ENGINE_REF = STATE_DIR / "engine.ref"
ENGINE_REF_PREV = STATE_DIR / "engine.ref.prev"
PY = sys.executable

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
import engine_manifest  # noqa: E402
import install as install_mod  # noqa: E402  (ensure_harnesses)
import migrate as migrate_mod  # noqa: E402
import rebuild as rebuild_mod  # noqa: E402
import seed_skills  # noqa: E402

EJECTED_MARKER = STATE_DIR / "ejected"

# The materialize set — canonical list lives in engine_manifest.py (shared with
# install.py's first-manifest write); re-exported here as the name every caller
# and test already knows.
ENGINE_PATHS = engine_manifest.ENGINE_PATHS


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"update: `git {' '.join(args)}` failed:\n{r.stderr.strip()}")
    return r


def run_script(name: str) -> None:
    # update is an admin operation — pass SC_ADMIN so snapshot/render clear the
    # serialize guard (harmless for non-serializing scripts like map_setup.py).
    env = {**os.environ, "SC_ADMIN": "1"}
    if subprocess.run([PY, str(ENGINE / "scripts" / name)], env=env).returncode != 0:
        sys.exit(f"update: {name} failed.")


def is_source_repo() -> bool:
    """The super-coder SOURCE repo (origin basename == super-coder) tracks the
    engine as its canonical source — it must NEVER untrack or materialize over
    it. A fork's origin is its own repo (super-coder is a separate remote)."""
    url = git("remote", "get-url", "origin", check=False).stdout.strip()
    return bool(url) and url.rstrip("/").split("/")[-1].removesuffix(".git") == "super-coder"


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


def _engine_paths_at(ref: str) -> list[str]:
    """ENGINE_PATHS that actually exist at `ref` (blob or tree).

    `git archive` aborts wholesale if any pathspec matches nothing, so a single
    engine file retired upstream (e.g. a dropped schema variant) would otherwise
    break every fork's update the one time it crosses that deletion. Filter to
    the paths present at `ref` — and report what was dropped, never silently."""
    present, missing = [], []
    for p in ENGINE_PATHS:
        exists = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{ref}:{p}"],
            capture_output=True).returncode == 0
        (present if exists else missing).append(p)
    if missing:
        print(f"  note: {len(missing)} engine path(s) absent at {ref[:12]} "
              f"(retired upstream) — skipping: {', '.join(missing)}")
    if not present:
        sys.exit(f"update: no engine paths exist at {ref} — wrong ref or remote?")
    return present


def _engine_files_at(ref: str) -> list[str]:
    """The exact FILE list upstream ships at `ref` under the engine paths —
    what a materialize writes, so what the manifest must cover and nothing
    more. Locally-added files under engine dirs (e.g. a fork-local skill's
    SKILL.md) and upstream-retired stragglers on disk stay out of the manifest:
    they are not upstream-owned, so they must never guard — and later block —
    a future update (see engine_manifest.write_manifest)."""
    return git("ls-tree", "-r", "--name-only", ref,
               "--", *_engine_paths_at(ref)).stdout.splitlines()


def materialize_engine(ref: str) -> None:
    """Write the engine paths at `ref` into the working tree WITHOUT touching the
    git index — the engine is gitignored, so a `git checkout -- <paths>` (which
    stages) is wrong. `git archive | tar -x` copies the fetched tree over the
    top, leaving the gitignored per-instance files (shell_db.db*, instance.json)
    in place. (Files deleted upstream linger until a future doctor sweep — same
    gap the old checkout had; acceptable for a wholesale-overwrite dependency.)"""
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", ref, "--", *_engine_paths_at(ref)],
        capture_output=True)
    if archive.returncode != 0:
        sys.exit("update: git archive of the engine failed:\n"
                 + archive.stderr.decode(errors="replace").strip())
    extract = subprocess.run(["tar", "-x", "-C", str(REPO_ROOT)], input=archive.stdout)
    if extract.returncode != 0:
        sys.exit("update: extracting the engine archive failed.")


def check_local_edits(force: bool) -> None:
    """Block the materialize when engine files were edited locally since the
    last one — a wholesale overwrite would discard those edits silently. The
    operator's real options are stated; --force is the explicit discard."""
    edits = engine_manifest.local_edits()
    if not edits:
        return
    print(f"✗ {len(edits)} engine file(s) locally modified since the last materialize:")
    for rel, kind in sorted(edits.items()):
        print(f"    {kind:8} {rel}")
    if force:
        print("  --force: discarding the local edits (overwritten by the new engine).")
        return
    sys.exit(
        "update: refusing to overwrite local engine edits. Your options:\n"
        "  - revert them (the engine is upstream-owned; see README →\n"
        "    'Customize a fork vs diverge from it')\n"
        "  - upstream them: PR the change to super-coder, then update normally\n"
        "  - ./sc update --force   discard the local edits and take upstream's engine\n"
        "  - ./sc eject            one-way: stop tracking upstream and own the engine")


def fetch_and_materialize(branch: str, ref: str | None = None,
                          force: bool = False) -> None:
    remote = super_coder_remote()
    if ref:
        # Pin to an explicit upstream version. `git fetch <remote> <ref>` serves
        # a branch, a tag, or (on GitHub) a reachable commit SHA; FETCH_HEAD is
        # the one name that works for all three.
        print(f"→ fetch {remote} + materialize engine (pinned ref: {ref})")
        git("fetch", remote, ref)
        sha = git("rev-parse", "FETCH_HEAD").stdout.strip()
    else:
        print(f"→ fetch {remote} + materialize engine ({remote}/{branch})")
        git("fetch", remote, branch)
        sha = git("rev-parse", f"{remote}/{branch}").stdout.strip()

    check_local_edits(force)

    # Restore point (engine half): remember where we were before overwriting.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ENGINE_REF.exists():
        shutil.copy2(ENGINE_REF, ENGINE_REF_PREV)
    else:
        # First update after B7 (or a fresh fork): no prior pin. Record HEAD's
        # engine ref if discoverable; else leave prev absent (rollback will warn).
        ENGINE_REF_PREV.unlink(missing_ok=True)

    materialize_engine(sha)
    ENGINE_REF.write_text(sha + "\n")
    n = engine_manifest.write_manifest(_engine_paths_at(sha),
                                       files=_engine_files_at(sha))
    print(f"  engine pinned at {sha[:12]} (.sc-state/engine.ref) · manifest over {n} files")


def migrate_engine_untrack() -> None:
    """One-time B7 migration for a fork that predates the gitignore model: stop
    tracking `.super-coder/` and ensure .gitignore keeps it out. Idempotent — a
    no-op once done. (Fresh installs are already untracked by install.py.)"""
    tracked = git("ls-files", "--error-unmatch", ".super-coder",
                  check=False).returncode == 0
    if tracked:
        git("rm", "-r", "--cached", "--quiet", ".super-coder", check=False)
        print("→ B7: untracked .super-coder/ (engine is now a gitignored dependency)")
    gi = REPO_ROOT / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    if "/.super-coder/" not in text.splitlines():
        with gi.open("a") as f:
            f.write(("" if text.endswith("\n") or not text else "\n")
                    + "\n# super-coder — engine is a gitignored materialized dependency (B7)\n"
                    + "/.super-coder/\n/.sc-state/engine.ref.prev\n")
        print("→ B7: added /.super-coder/ to .gitignore")


def migrate_or_rebuild() -> None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        print("→ no live DB (fresh fork) — building from text")
        rebuild_mod.main([])
        return
    rebuild_mod.backup_existing()  # restore point before any structural change
    print("→ migrate in place (pending migrations → the live DB; data preserved)")
    migrate_mod.migrate(str(DB_PATH))


def sync_skills() -> None:
    """Re-apply the engine skills seed against the live DB.

    The seed is id-stable and UPSERTs by name, so new/changed engine catalogue
    skills land without a rebuild and existing skill_ids — and the grants that
    reference them — stay valid. It deliberately does not retire names absent
    from assets/skills because those may be project-local skills serialized by
    the fork snapshot. The migrate ledger would otherwise skip the already-
    stamped seed file; catalogue currency is a per-update sync, not a one-time
    migration.
    """
    seed = seed_skills.OUT
    if not seed.exists():
        print("  (no skills seed to sync)")
        return
    con = db_driver.connect(DB_PATH)
    try:
        con.executescript(seed.read_text())
        con.commit()
        # The seed just reset every engine row to is_deleted=0 — re-assert the
        # fork retire list (.sc-state/skills_retired.json) before regrant()
        # hands the common catalogue back to every shell.
        flipped = seed_skills.apply_retired(con)
    finally:
        con.close()
    print(f"  synced catalogue from {seed.name}")
    if flipped:
        print(f"  fork retire list re-applied: {', '.join(flipped)}")


def regrant() -> int:
    con = db_driver.connect(DB_PATH)
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
    force = "--force" in argv
    branch = "main"
    if "--branch" in argv:
        i = argv.index("--branch")
        if i + 1 < len(argv):
            branch = argv[i + 1]
    ref = None
    if "--ref" in argv:
        i = argv.index("--ref")
        if i + 1 < len(argv):
            ref = argv[i + 1]
        if "--branch" in argv:
            sys.exit("update: --ref and --branch are mutually exclusive — a ref "
                     "IS the pin; a branch is what to track.")

    source = is_source_repo()
    if EJECTED_MARKER.exists() and not source:
        sys.exit("update: this fork has EJECTED — the engine is fork source now, "
                 "not an upstream dependency (.sc-state/ejected). There is no "
                 "upstream to update from; edit .super-coder/ directly and commit "
                 "like any other code. (To re-adopt upstream, that's a manual "
                 "re-fork — see README → 'Customize a fork vs diverge from it'.)")
    if source:
        # The source repo IS the engine — it has no upstream to materialize from
        # and must keep tracking .super-coder/. Reconcile its own tree only.
        print("→ super-coder SOURCE repo — engine is tracked here; "
              "skipping fetch/materialize/untrack (reconcile in place only)")
        no_fetch = True
    else:
        migrate_engine_untrack()  # one-time B7: untrack the engine (idempotent)
        # Top up the fork's .gitignore with any engine ignore rules added since it
        # was installed (e.g. the map DB cache /.sc-state/map.db) — line-additive,
        # so an already-installed fork never silently commits a new derived cache.
        if install_mod.ensure_gitignore():
            print("→ .gitignore: added engine ignore rule(s) for this release")

    if no_fetch:
        print("→ --no-fetch: reconciling against the current working tree "
              "(engine + engine.ref unchanged)")
    else:
        fetch_and_materialize(branch, ref=ref, force=force)

    # Harnesses can be ADDED upstream between releases (e.g. codex landed after
    # dos-arch installed), so a fork that updates must pick up any newly-required
    # harness — not just the ones present at first install. Best-effort + native
    # installers (no npm); a failure warns and continues (install by hand later).
    # Auth/login stays manual; this only ensures the CLI binary is present.
    print("→ ensure harnesses installed (claude + opencode + codex + vibe + kimi)")
    install_mod.ensure_harnesses()

    migrate_or_rebuild()

    print("→ sync skills catalogue (id-stable)")
    sync_skills()
    print("→ re-grant catalogue skills to all shells")
    print(f"  {regrant()} new grant(s)")
    print("→ wire map automation + map the repo")
    run_script("map_setup.py")
    print("→ snapshot the live state")
    run_script("snapshot.py")

    # Self-heal the make wiring: forks installed before the engine scripted this
    # (or whose include was removed) get the `dos-*` aliases appended now. Source
    # repo manages its own Makefile — skip it. Idempotent; a no-op if already wired.
    if not source:
        print("→ wire make aliases (dos- command standard)")
        print(f"  {install_mod.wire_make_aliases()}")

    print("\nupdate: done — new floor laid in place; your rows are intact.")
    if source:
        # Source repo tracks the engine itself — no fork repin PR; just commit
        # the reconciled tree on a branch as usual.
        print("  Review + commit the reconciled tree (the engine is tracked here).")
    else:
        # Fork repin: the update edited tracked files in place but did NOT touch
        # git — it never branches, commits, or changes branch, so a bare `./sc
        # update` on `main` leaves the repin uncommitted on main. Spell out the
        # full flow so the operator lands a PR and returns to main instead of
        # sitting stranded on the repin branch (the engine is gitignored — only
        # .sc-state/ + any _sc renders + a first-time Makefile include change).
        try:
            pin = (REPO_ROOT / ".sc-state" / "engine.ref").read_text().strip()[:12]
        except Exception:
            pin = ""
        branch_hint = f"repin-{pin}" if pin else "repin-<sha>"
        print("  This edited tracked files in place but did NOT touch git. Recommended flow:")
        print(f"    git checkout -b {branch_hint}")
        print("    git add .sc-state/engine.ref .sc-state/content.sql   # + any _sc renders / Makefile")
        print("    git commit -m 'chore(engine): repin' && git push -u origin HEAD")
        print("    gh pr create")
        print("    git checkout main        # return to main — don't stay stranded on the repin branch")
        print("  After the PR merges:")
        print("    git pull --ff-only       # brings the repin onto local main")
    print("  A bad update? `./sc rollback` restores the DB + engine together.")
    print("  Restart your session to boot onto the new floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
