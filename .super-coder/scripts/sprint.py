#!/usr/bin/env python3
"""Forward stale worktree dispatchers to the current Sprint v2 CLI.

Long-lived shell branches retain the ``sc`` file from their branch point. Some
of those launchers still dispatch ``sc sprint`` to this module even after the
live, materialized engine has moved to ``sprint_cli.py``. Keep this entrypoint
as a compatibility alias only; all command behavior remains owned by
``sprint_cli``.
"""
from __future__ import annotations

from sprint_cli import main


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
