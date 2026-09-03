#!/usr/bin/env python3
"""./sc make-cleanup — retire a fork's `make dos-*` wiring (one-time).

Before the `subfloor` command existed, `./sc install` and every `./sc update`
wired a fork's Makefile to the engine's alias file: a one-line Makefile when the
fork had none, otherwise an appended `-include .super-coder/aliases.mk` block.
The alias file is retired and no longer materialized, but an update never
deletes files the engine dropped upstream, so an updated fork keeps the old
file — and the include — on disk until this command removes them.

What it does, in order, and only when the evidence matches:

    Makefile                    — deleted when its content is exactly the
                                  installer's one-liner; otherwise the appended
                                  block / include line is removed and the rest
                                  of the fork's own Makefile is left intact.
    .super-coder/aliases.mk     — deleted (the materialized copy, gitignored).

Nothing else is touched. Idempotent — a second run reports nothing to do.
`--dry-run` prints the plan without writing. The runbook is in
docs/README.md, "Retire the make aliases (one-time)".

Usage:
    ./sc make-cleanup            # apply
    ./sc make-cleanup --dry-run  # plan only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent

ALIASES_INCLUDE = "-include .super-coder/aliases.mk"
# The Makefile the retired installer wrote when a fork had none. Kept verbatim:
# the cleanup deletes a Makefile only when it is byte-for-byte this file.
INSTALLER_MAKEFILE = (
    "# Fork Makefile — super-coder convenience aliases (make dos-e / dos-enter).\n"
    "# Every target is dos--prefixed; add your own targets below the include.\n"
    f"{ALIASES_INCLUDE}\n"
)
# The block the retired installer appended to a fork's existing Makefile.
APPENDED_ALIASES_BLOCK = (
    "\n# super-coder convenience aliases (designs-OS 'dos-' command standard).\n"
    "# Appended by ./sc; every target is dos--prefixed so it can't collide with\n"
    "# this Makefile's own targets. Delete this line to opt out — `./sc <cmd>`\n"
    "# stays equivalent.\n"
    f"{ALIASES_INCLUDE}\n"
)
# Any include of the alias file: hard `include` or soft `-include`, with
# arbitrary surrounding whitespace.
ALIASES_RE = re.compile(r"^\s*-?include\s+\.super-coder/aliases\.mk\s*$", re.M)


def plan(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """(action, path) pairs the cleanup would perform; empty when nothing to do.

    actions: 'delete-makefile' · 'unwire-makefile' · 'delete-aliases'
    """
    steps: list[tuple[str, str]] = []
    makefile = repo_root / "Makefile"
    if makefile.is_file():
        text = makefile.read_text()
        if text == INSTALLER_MAKEFILE:
            steps.append(("delete-makefile", str(makefile)))
        elif ALIASES_RE.search(text):
            steps.append(("unwire-makefile", str(makefile)))
    aliases = repo_root / ".super-coder" / "aliases.mk"
    if aliases.is_file() or aliases.is_symlink():
        steps.append(("delete-aliases", str(aliases)))
    return steps


def unwire_makefile_text(text: str) -> str:
    updated = text.replace(APPENDED_ALIASES_BLOCK, "")
    updated = ALIASES_RE.sub("", updated)
    return re.sub(r"\n{3,}", "\n\n", updated)


def cleanup_makefile(repo_root: Path = REPO_ROOT) -> bool:
    """Delete the installer's Makefile or strip the include; True when changed."""
    makefile = repo_root / "Makefile"
    if not makefile.is_file():
        return False
    text = makefile.read_text()
    if text == INSTALLER_MAKEFILE:
        makefile.unlink()
        return True
    updated = unwire_makefile_text(text)
    if updated == text:
        return False
    makefile.write_text(updated)
    return True


def makefile_still_wired(repo_root: Path = REPO_ROOT) -> bool:
    makefile = repo_root / "Makefile"
    return makefile.is_file() and bool(ALIASES_RE.search(makefile.read_text()))


def apply(repo_root: Path = REPO_ROOT) -> list[str]:
    """Run the cleanup; returns one report line per action taken."""
    done: list[str] = []
    for action, path in plan(repo_root):
        target = Path(path)
        if action == "delete-makefile":
            target.unlink()
            done.append(f"deleted {path} (the installer's one-line Makefile)")
        elif action == "unwire-makefile":
            if cleanup_makefile(repo_root):
                done.append(f"removed the aliases.mk include from {path}")
        elif action == "delete-aliases":
            target.unlink()
            done.append(f"deleted {path} (retired engine alias file)")
    return done


def describe(action: str, path: str) -> str:
    return {
        "delete-makefile": f"delete {path} — it is the installer's one-line Makefile",
        "unwire-makefile": f"remove the `{ALIASES_INCLUDE}` line from {path}",
        "delete-aliases": f"delete {path} — retired engine alias file",
    }[action]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sc make-cleanup",
        description="retire this fork's make dos-* wiring in favour of the subfloor command",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    args = parser.parse_args(argv)

    steps = plan(REPO_ROOT)
    if not steps:
        print("  nothing to do — no Makefile include and no .super-coder/aliases.mk")
        return 0
    if args.dry_run:
        print("  would:")
        for action, path in steps:
            print(f"    {describe(action, path)}")
        return 0
    for line in apply(REPO_ROOT):
        print(f"  {line}")
    if (REPO_ROOT / "Makefile").exists():
        print("  Makefile kept (your own targets) — commit it if it changed")
    print("  make dos-* is retired; use `subfloor <verb>` (./sc alias re-installs the command)")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
