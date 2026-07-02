#!/usr/bin/env python3
"""./sc eject — one-way: stop tracking upstream and OWN the engine.

The B7 model keeps `.super-coder/` a gitignored, materialized dependency —
upstream-owned, wholesale-overwritten by `./sc update`, never fork-edited. That
is the right default: as long as customization fits the fork-owned extension
points (local skills, instance.json, .sc-state/, everything outside the
engine), a fork tracks upstream forever and keeps receiving fixes.

Eject is for the moment that stops being true: the fork needs engine changes
that upstream would rightly not take. It flips the model — the engine becomes
FORK SOURCE, committed and edited like any other code in the repo:

    1. .gitignore     drop the `/.super-coder/` rule; keep only the engine's
                      runtime/per-instance files ignored (DB, instance.json,
                      run/, logs/, the hash manifest)
    2. .sc-state/     delete engine.ref + engine.ref.prev (there is no pin —
                      no upstream); delete the hash manifest (git tracks edits
                      now); write the `ejected` marker recording the SHA the
                      fork diverged at
    3. remote         remove the super-coder remote (unless --keep-remote)
    4. stage          git add the engine + the edits above; COMMITTING STAYS
                      YOURS (review the diff first)

After eject, `./sc update` and `./sc rollback` refuse (the marker); everything
else — launch, enter, snapshot, render, the GUI — works unchanged, reading the
same engine files from the same paths.

What you give up: upstream fixes, migrations, and new skills stop flowing.
Upstream-first is the strong default — PR the change to super-coder instead if
the next fork would want it too. Eject only when the divergence is genuinely
yours. (README → 'Customize a fork vs diverge from it'.)

Reversing: before the eject commit, `git reset` + restore .sc-state from HEAD
+ re-add the remote. After it, re-adopting upstream is a manual re-fork.

Usage:
    ./sc eject [--yes] [--keep-remote]
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
STATE_DIR = REPO_ROOT / ".sc-state"
ENGINE_REF = STATE_DIR / "engine.ref"
ENGINE_REF_PREV = STATE_DIR / "engine.ref.prev"
EJECTED_MARKER = STATE_DIR / "ejected"
MANIFEST = ENGINE / "engine.manifest"

sys.path.insert(0, str(ENGINE / "scripts"))
import update as update_mod  # noqa: E402  (is_source_repo, git helper)

# Post-eject .gitignore block: the engine is tracked now, but its runtime /
# per-instance files never are (same set the SOURCE repo ignores — it tracks
# its own engine too, so this is a proven ignore surface).
EJECTED_IGNORE_MARKER = "# super-coder — EJECTED: engine is fork source (tracked);"
EJECTED_IGNORE_BLOCK = f"""
{EJECTED_IGNORE_MARKER} only its
# runtime/per-instance files stay ignored.
/.super-coder/shell_db.db
/.super-coder/shell_db.db-wal
/.super-coder/shell_db.db-shm
/.super-coder/instance.json
/.super-coder/run/
/.super-coder/logs/
/.super-coder/engine.manifest
"""

WARNING = """\
╔══════════════════════════════════════════════════════════════════════════╗
║  ./sc eject — ONE-WAY DOOR                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

This makes the engine (.super-coder/ + sc) FORK SOURCE — tracked, committed,
and edited like any other code in this repo. In exchange you give up the
upstream lifeline, permanently:

  · `./sc update` stops working — no more upstream fixes, migrations, or new
    catalogue skills. Every engine change from here on is yours to author.
  · `./sc rollback` stops working — use plain git on the tracked engine.
  · Re-adopting upstream later is a manual re-fork, not a command.

Everything else is unchanged: launch, enter, snapshot, render, the GUI, and
your DB/memory are untouched. Nothing is committed by this command — it stages
the change and you review + commit.

Is this the right move? Only if you need engine changes upstream would rightly
not take. If the next fork would want your change too, PR it to super-coder
instead and stay on updates (the strong default).
"""


def _gitignore_eject() -> str:
    """Drop the `/.super-coder/` rule; append the runtime-ignore block. Returns
    a one-line status. Idempotent."""
    gi = REPO_ROOT / ".gitignore"
    if not gi.exists():
        gi.write_text(EJECTED_IGNORE_BLOCK)
        return "wrote .gitignore (engine runtime ignores)"
    lines = gi.read_text().splitlines()
    kept = [ln for ln in lines if ln.strip() != "/.super-coder/"]
    dropped = len(lines) - len(kept)
    text = "\n".join(kept) + ("\n" if kept else "")
    if EJECTED_IGNORE_MARKER not in text:
        text += EJECTED_IGNORE_BLOCK
    gi.write_text(text)
    return (f"dropped the /.super-coder/ rule ({dropped} line) + kept runtime files ignored"
            if dropped else "runtime ignores ensured (/.super-coder/ rule was already gone)")


def _confirm() -> None:
    if not sys.stdin.isatty():
        sys.exit("eject: refusing without confirmation on a non-interactive "
                 "stdin — pass --yes to script it.")
    try:
        answer = input("Type 'eject' to proceed (anything else aborts): ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "eject":
        sys.exit("eject: aborted — nothing changed.")


def main(argv: list[str]) -> int:
    yes = "--yes" in argv
    keep_remote = "--keep-remote" in argv

    if update_mod.is_source_repo():
        sys.exit("eject: this is the super-coder SOURCE repo — the engine is "
                 "already tracked source here; there is nothing to eject.")
    if EJECTED_MARKER.exists():
        print("eject: already ejected — the engine is fork source "
              f"(marker: {EJECTED_MARKER.relative_to(REPO_ROOT)}).")
        return 0

    pinned = ENGINE_REF.read_text().strip() if ENGINE_REF.exists() else ""

    print(WARNING)
    print(f"  engine currently pinned at: {pinned[:12] if pinned else '(no engine.ref — unpinned)'}\n")
    if not yes:
        _confirm()

    print("→ ejecting")

    # 1. gitignore: the engine becomes visible to git; runtime files stay dark.
    print(f"  .gitignore: {_gitignore_eject()}")

    # 2. .sc-state: no upstream → no pin, no restore pointer, no edit manifest.
    for p in (ENGINE_REF, ENGINE_REF_PREV, MANIFEST):
        if p.exists():
            p.unlink()
            print(f"  removed {p.relative_to(REPO_ROOT)}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    remote_url = ""
    r = update_mod.git("remote", "get-url", "super-coder", check=False)
    if r.returncode == 0:
        remote_url = r.stdout.strip()
    EJECTED_MARKER.write_text(json.dumps({
        "ejected_at": date.today().isoformat(),
        "engine_ref": pinned,          # the upstream SHA the fork diverged at
        "upstream": remote_url,
    }, indent=2) + "\n")
    print(f"  wrote .sc-state/ejected (diverged at {pinned[:12] if pinned else 'unknown'})")

    # 3. The upstream remote: gone by default — eject means no upstream. It is
    # one command to re-add, and --keep-remote preserves it for reference.
    if keep_remote:
        print("  --keep-remote: super-coder remote left in place (reference only)")
    elif remote_url:
        update_mod.git("remote", "remove", "super-coder", check=False)
        print(f"  removed the super-coder remote ({remote_url})")
    else:
        print("  (no super-coder remote to remove)")

    # 4. Stage — the operator reviews + commits. `git add .super-coder` works
    # now that the ignore rule is gone; runtime files stay excluded by the new
    # block. `.sc-state` stages the ref deletions + the marker.
    update_mod.git("add", ".gitignore", ".sc-state", ".super-coder")
    n = len(update_mod.git("diff", "--cached", "--name-only",
                           check=False).stdout.splitlines())
    print(f"  staged {n} file(s)")

    print("\neject: done — the engine is fork source. Review + commit:")
    print("    git status && git diff --cached --stat")
    print(f"    git commit -m 'chore: eject super-coder engine (diverged at "
          f"{pinned[:12] if pinned else 'unpinned'})'")
    print("  From here, edit .super-coder/ directly; update/rollback now refuse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
