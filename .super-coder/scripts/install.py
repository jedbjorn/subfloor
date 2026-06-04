#!/usr/bin/env python3
"""Install super-coder into a fork — first-launch bootstrap.

Run once, in a host repo that has just pulled the engine
(`git checkout super-coder/main -- .super-coder sc`). It takes that repo
from "engine present" to "a shell you can launch":

    1. Guard   — refuse to run in the super-coder SOURCE repo, or on a fork that
                 is already installed (both would destroy content). --force skips.
    2. Require — python3 + sqlite3 (+ a heads-up if git or curl is missing).
    3. Harness — ensure claude + opencode are installed (official native
                 installers, no npm); pick the launch default → instance.json.
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
        if _harness_installed(h):
            return h
    return None


# Official NATIVE installers — no npm. Claude Code dropped npm as the primary
# path (https://code.claude.com/docs/en/setup); opencode ships its own script
# too. Pipe-to-bash, latest version.
HARNESS_INSTALL = {
    "claude":   "curl -fsSL https://claude.ai/install.sh | bash",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
}
# Where each installer drops its binary. Checked post-install because the new
# bin dir is NOT on this process's PATH — the installer edits shell rc files,
# which only a fresh shell picks up. shutil.which alone would miss a just-
# installed CLI.
HARNESS_BIN = {
    "claude":   Path.home() / ".local" / "bin" / "claude",
    "opencode": Path.home() / ".opencode" / "bin" / "opencode",
}


def _harness_installed(name: str) -> bool:
    return bool(shutil.which(name)) or HARNESS_BIN.get(name, Path("/nonexistent")).exists()


def ensure_harnesses() -> dict[str, str]:
    """Install any missing harness CLI via its official native installer (no
    npm) — both claude + opencode, so a fork can launch and run either. Best
    effort: a failed install warns and continues (the harness is only needed at
    launch and can be installed by hand later). Returns {name: status}."""
    status: dict[str, str] = {}
    have_curl = bool(shutil.which("curl"))
    for name, cmd in HARNESS_INSTALL.items():
        if _harness_installed(name):
            print(f"  {name:9} ✓ already installed")
            status[name] = "present"
            continue
        if not have_curl:
            print(f"  {name:9} ⚠ missing, and curl is unavailable — install by hand: {cmd}")
            status[name] = "no-curl"
            continue
        print(f"  {name:9} … not found — installing  ($ {cmd})")
        rc = subprocess.run(["bash", "-c", cmd]).returncode
        if rc == 0 and _harness_installed(name):
            print(f"  {name:9} ✓ installed")
            status[name] = "installed"
        else:
            print(f"  {name:9} ⚠ install failed (rc={rc}) — retry by hand: {cmd}")
            status[name] = "failed"
    fresh = [n for n, s in status.items() if s == "installed"]
    if fresh:
        dirs = sorted({str(HARNESS_BIN[n].parent) for n in fresh})
        print(f"  ↪ new CLIs live in {', '.join(dirs)} — open a NEW shell (or update "
              f"PATH) before `./sc launch`, since this shell's PATH predates them.")
    return status


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
    skip_harness = "--skip-harness-install" in argv
    # super-coder's own flags — strip them so they don't reach init_fork's parser.
    own = {"--force", "--skip-harness-install", "--ensure-harness"}
    fork_args = [a for a in argv if a not in own]

    # Standalone: just ensure the harness CLIs and exit (for an already-installed
    # fork). Runs before the guards so it works anywhere.
    if "--ensure-harness" in argv:
        step("Ensuring harness CLIs (claude + opencode)")
        ensure_harnesses()
        return 0

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
    if not shutil.which("curl"):
        print("  ⚠ curl not on PATH — needed to auto-install a missing harness.")

    # 3. Ensure harness CLIs --------------------------------------------------
    # Install both claude + opencode if missing, via their official NATIVE
    # installers (no npm). The new harness picker lets a fork launch + run
    # either, so we want both present. --skip-harness-install detects only
    # (CI / air-gapped). instance.json's harness is the launch default; the
    # picker overrides it per-launch.
    step("Ensuring harness CLIs (claude + opencode)")
    if skip_harness:
        print("  --skip-harness-install set — detecting only, not installing")
        for n in HARNESS_INSTALL:
            print(f"  {n:9} {'✓ present' if _harness_installed(n) else 'absent'}")
    else:
        ensure_harnesses()
    harness = detect_harness() or "claude"  # claude preferred; both should be present
    print(f"  → default harness for instance.json: {harness}")

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
