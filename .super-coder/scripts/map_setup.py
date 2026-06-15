#!/usr/bin/env python3
"""Wire the auto-remap automation, then map — the cartographer's one-shot.

The repo map (dr_* catalogue) is kept fresh by tracked git hooks that re-run
`./sc map` on pull / branch-switch / rebase. The hooks live in
`.super-coder/hooks/` and fire via `core.hooksPath` — a *per-clone* git setting,
so a fresh clone needs this run once to point git at them. This:

    1. points `core.hooksPath` at .super-coder/hooks/   (idempotent)
    2. ensures the hook scripts are executable
    3. runs an initial map

Run by `./sc install` and `./sc update` (so every fork is wired), and by the
cartographer shell on first boot / when healing. Re-running is safe.

Usage:
    ./sc map-setup
    python3 .super-coder/scripts/map_setup.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
HOOKS_DIR = ENGINE / "hooks"
# core.hooksPath must be ABSOLUTE. Git interprets a relative hooksPath against
# the *current working directory* — which in a linked worktree is the worktree
# root, where a fork's gitignored .super-coder/ does NOT exist. So a relative
# value made every git hook (pre-commit guard, post-commit preview URL, the
# post-checkout/merge/rewrite catalogue remap) silently no-op in exactly the
# shell worktrees they target. core.hooksPath is a per-clone setting (.git/config,
# uncommitted) re-applied by this script on every install/update, so an absolute
# host path costs nothing in portability and resolves from every worktree.
HOOKS_ABS = str(HOOKS_DIR)

sys.path.insert(0, str(ENGINE / "scripts"))
import map_repo  # noqa: E402


def _is_git_repo() -> bool:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True).returncode == 0


def wire_hooks() -> bool:
    """Point git at the tracked hooks dir + ensure the hooks are executable.
    Returns True if the wiring is in place, False if it couldn't be (not a git
    repo / no hooks dir) — mapping still proceeds either way."""
    if not HOOKS_DIR.is_dir():
        print(f"map-setup: no hooks dir at {HOOKS_ABS} — skipping hook wiring")
        return False
    hooks = sorted(p for p in HOOKS_DIR.iterdir() if p.is_file())
    for h in hooks:
        os.chmod(h, os.stat(h).st_mode | 0o111)
    if not _is_git_repo():
        print("map-setup: not a git repo — hooks left unwired "
              "(auto-remap needs git; `./sc map` / rebuild still refresh)")
        return False
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "config", "core.hooksPath", HOOKS_ABS],
        check=True)
    print(f"map-setup: core.hooksPath -> {HOOKS_ABS} "
          f"({len(hooks)} hook(s): {', '.join(h.name for h in hooks)})")
    return True


def main() -> int:
    wire_hooks()
    print("map-setup: mapping the repo")
    return map_repo.main()


if __name__ == "__main__":
    raise SystemExit(main())
