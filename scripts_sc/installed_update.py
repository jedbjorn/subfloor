#!/usr/bin/env python3
"""Run the live engine updater with sc-cachy's installed-fork identity."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCRIPTS = ROOT / ".super-coder" / "scripts"
ENGINE_REMOTE = "sc-engine-local"


def load_installed_updater():
    """Load replaceable engine code, then apply this installation's identity."""
    sys.path.insert(0, str(ENGINE_SCRIPTS))
    import update  # type: ignore[import-not-found]

    # sc-cachy fixes and runs subfloor. Its origin therefore has the canonical
    # source repository name even though .super-coder is a materialized,
    # gitignored dependency here. Keep that one-off identity outside the engine
    # so an engine materialization cannot overwrite it mid-update.
    update.is_source_repo = lambda: False
    update.super_coder_remote = lambda: ENGINE_REMOTE
    return update


def main(argv: list[str]) -> int:
    update = load_installed_updater()
    from cli_entry import run_cli

    return run_cli(update.main, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
