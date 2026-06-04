#!/usr/bin/env python3
"""Install super-coder into a fork — first-launch bootstrap.

Run once, in a host repo that has just pulled the engine
(`git checkout super-coder/main -- .super-coder sc`). It takes that repo
from "engine present" to "a shell you can launch":

    1. Guard   — refuse to run in the super-coder SOURCE repo, or on a fork that
                 is already installed (both would destroy content). --force skips.
    2. Require — python3 + sqlite3 (+ a heads-up if git or a harness CLI is missing).
    3. Detect  — the coding harness on PATH (claude / opencode) → instance.json.
    4. Strip   — super-coder's own per-instance content; a fork inherits the
                 SYSTEM (schema + skill catalogue + render chain), never the memory.
    5. Build   — the system DB (schema + migrations; no per-instance content yet).
    6. Seed    — the fork's first user + shell (delegates to init_fork; CC lineage
                 + genesis seed + skill grants).
    7. Persist — `./sc snapshot` (serialize the new shell) + `./sc render` (flat _sc).
    8. Done    — print how to launch.

Usage:
    ./sc install                      # interactive (prompts for user/shell)
    python3 .super-coder/scripts/install.py [init_fork flags] [--force]
        e.g. … --username Sam --name Dev --shortname dev --role "Dev shell"
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
PY = sys.executable

sys.path.insert(0, str(ENGINE / "scripts"))
import ports as ports_mod  # noqa: E402

# super-coder's own per-instance content — present in a freshly-pulled fork
# because the git checkout brought it along. A fork must not inherit it.
STRIP = [
    ENGINE / "snapshot" / "content.sql",
    ENGINE / "assets" / "seed" / "super-coder-founding-spec.md",
]


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def origin_basename() -> str | None:
    p = sh("git", "-C", str(REPO_ROOT), "remote", "get-url", "origin")
    if p.returncode != 0:
        return None
    return p.stdout.strip().rstrip("/").split("/")[-1].removesuffix(".git")


def is_source_repo() -> bool:
    """The super-coder source repo's origin is …/super-coder. A fork's origin is
    its own repo (super-coder is a separate, differently-named remote)."""
    return origin_basename() == "super-coder"


def already_installed() -> bool:
    if not ports_mod.CONFIG.exists():
        return False
    try:
        return "installed_at" in json.loads(ports_mod.CONFIG.read_text())
    except json.JSONDecodeError:
        return False


def detect_harness() -> str | None:
    for h in ("claude", "opencode"):
        if shutil.which(h):
            return h
    return None


# Ignore lines a fork needs — the rebuilt/derived artifacts. The git checkout
# that brings the engine in doesn't carry super-coder's .gitignore, so the
# installer appends them to the host repo's .gitignore (idempotent via marker).
_GITIGNORE_MARKER = "# super-coder — rebuilt/derived; never commit"
_GITIGNORE_BLOCK = f"""
{_GITIGNORE_MARKER}
/.super-coder/shell_db.db
/.super-coder/shell_db.db-wal
/.super-coder/shell_db.db-shm
/.super-coder/instance.json
/CLAUDE.md
/AGENTS.md
/opencode.json
/.claude/skills/
"""


def ensure_gitignore() -> bool:
    gi = REPO_ROOT / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if _GITIGNORE_MARKER in existing:
        return False
    with gi.open("a") as f:
        f.write(("" if existing.endswith("\n") or not existing else "\n") + _GITIGNORE_BLOCK)
    return True


def step(msg: str) -> None:
    print(f"\n\033[1m→ {msg}\033[0m")


def main(argv: list[str]) -> int:
    force = "--force" in argv
    fork_args = [a for a in argv if a != "--force"]

    # 1. Guards ---------------------------------------------------------------
    if is_source_repo() and not force:
        sys.exit("install: this is the super-coder SOURCE repo — the installer is "
                 "for forks. (Run it in a host repo that pulled the engine.) "
                 "Use --force only if you really mean to re-init the source.")
    if already_installed() and not force:
        sys.exit("install: this fork is already installed (.super-coder/instance.json "
                 "has installed_at). Re-installing destroys content — pass --force "
                 "to override, or just `./sc launch`.")

    # 2. Requirements ---------------------------------------------------------
    step("Checking requirements")
    try:
        import sqlite3  # noqa: F401
        print("  python3 + sqlite3 ✓")
    except ImportError:
        sys.exit("  python3 is missing the sqlite3 module — install it and retry.")
    if not shutil.which("git"):
        print("  ⚠ git not on PATH — needed for the commit→PR flow later.")

    # 3. Detect harness -------------------------------------------------------
    # We DETECT, we don't install — a fork's installer shouldn't silently run a
    # global npm/curl install of someone else's CLI. If neither is present we
    # print how to get one and carry on (the harness is only needed at launch).
    step("Detecting harness")
    harness = detect_harness()
    if harness:
        print(f"  found '{harness}' on PATH")
    else:
        harness = "claude"
        print("  ⚠ no harness CLI found. Install one before `./sc launch`:")
        print("      claude    npm i -g @anthropic-ai/claude-code   ·  https://docs.claude.com/claude-code")
        print("      opencode  npm i -g opencode-ai                 ·  https://opencode.ai")
        print("    Defaulting instance.json harness to 'claude' (edit it to switch).")

    # 3.5 Wire the host repo's .gitignore -------------------------------------
    step("Wiring .gitignore")
    print("  added super-coder ignore lines" if ensure_gitignore()
          else "  (already present)")

    # 4. Strip super-coder's per-instance content -----------------------------
    step("Stripping super-coder's per-instance content (a fork inherits the system, not the memory)")
    for p in STRIP:
        if p.exists():
            p.unlink()
            print(f"  removed {p.relative_to(REPO_ROOT)}")
        else:
            print(f"  (already absent) {p.relative_to(REPO_ROOT)}")

    # 5. Build the system DB --------------------------------------------------
    step("Building the system DB (schema + migrations)")
    r = subprocess.run([PY, str(ENGINE / "scripts/rebuild.py")])
    if r.returncode != 0:
        sys.exit("install: rebuild failed.")

    # 6. Seed the first shell (interactive unless flags were passed) ----------
    step("Seeding this fork's first shell")
    r = subprocess.run([PY, str(ENGINE / "scripts/init_fork.py"), *fork_args])
    if r.returncode != 0:
        sys.exit("install: first-shell seeding failed (or was aborted).")

    # 7. Map the host repo into the dr_* catalogue (so the shell can read it) --
    step("Mapping the repo (dr_* catalogue)")
    subprocess.run([PY, str(ENGINE / "scripts/map_repo.py")])

    # 8. Persist: snapshot + render ------------------------------------------
    step("Serializing + rendering")
    subprocess.run([PY, str(ENGINE / "scripts/snapshot.py")])
    subprocess.run([PY, str(ENGINE / "scripts/render.py"), "flat"])

    # Record harness + installed marker in instance.json ---------------------
    cfg = ports_mod.resolve(persist=False)
    cfg["harness"] = harness
    cfg["installed_at"] = date.today().isoformat()
    ports_mod.CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")

    # 9. Done -----------------------------------------------------------------
    step("Installed ✓")
    print(f"  harness : {harness}")
    print(f"  GUI port: {cfg['port']}  (http://127.0.0.1:{cfg['port']})")
    print("\nNext:")
    print("  git add -A && git commit -m 'install super-coder'")
    print("  ./sc launch        # starts the review GUI + boots your shell")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
