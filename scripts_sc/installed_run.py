#!/usr/bin/env python3
"""Run the engine's shell boot with sc-cachy's installed-fork identity.

The engine decides repo stance via install.is_source_repo(): an origin whose
basename sits in SOURCE_REPO_NAMES reads as the engine SOURCE repo. This
install's origin IS jedbjorn/subfloor.git (the canonical upstream), so the
bare engine check would flip every boot to the wrong "you are upstream — the
engine is your work surface" stance even though .super-coder/ is a
materialized, gitignored dependency pinned via .sc-state/engine.ref.

Keep that one-off identity outside the replaceable engine tree: this wrapper
pre-imports the engine's install module, pins installed-mode identity, then
executes the engine's run.py as __main__ with the caller's argv.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCRIPTS = ROOT / ".super-coder" / "scripts"


def apply_installed_identity() -> None:
    sys.path.insert(0, str(ENGINE_SCRIPTS))
    import install  # type: ignore[import-not-found]
    import update  # type: ignore[import-not-found]

    install.is_source_repo = lambda: False
    update.is_source_repo = lambda: False


def dispatch_args(argv: list[str]) -> list[str]:
    """Mirror the engine dispatcher's `boot` / `boot-<shortname>` verbs.

    host_sc.sh hands this wrapper the verb the dispatcher would have received
    (`boot`, `boot-cc`). run.py takes only the shortname positional, so the
    bare verb is dropped and `boot-<shortname>` becomes `<shortname>` — the
    same rewrite as dispatch.sh's `boot-*)` case. Anything else passes through.
    """
    if argv and argv[0] == "boot":
        return argv[1:]
    if argv and argv[0].startswith("boot-"):
        return [argv[0][len("boot-"):], *argv[1:]]
    return argv


def main(argv: list[str]) -> int:
    apply_installed_identity()
    sys.argv = [str(ENGINE_SCRIPTS / "run.py"), *dispatch_args(argv)]
    runpy.run_path(str(ENGINE_SCRIPTS / "run.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))