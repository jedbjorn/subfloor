#!/usr/bin/env python3
"""Updater repair for absolute paths embedded in installed broker units."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import update  # noqa: E402


class BrokerRefreshTest(unittest.TestCase):
    def test_refreshes_only_units_already_installed_for_this_fork(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "Repos" / "fork"
            repo.mkdir(parents=True)
            (repo / "sc").write_text("#!/bin/sh\n")
            unit_dir = base / "config" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            for kind in ("vm", "ts"):
                (unit_dir / f"sc-{kind}-broker-fork.service").write_text(
                    "old absolute path\n"
                )

            completed = subprocess.CompletedProcess([], 0)
            with mock.patch.object(update, "REPO_ROOT", repo), mock.patch.dict(
                update.os.environ,
                {"XDG_CONFIG_HOME": str(base / "config")},
                clear=False,
            ), mock.patch.object(
                update.shutil, "which", return_value="/usr/bin/systemctl"
            ), mock.patch.object(
                update.subprocess, "run", return_value=completed
            ) as run:
                refreshed = update.refresh_installed_brokers()

        self.assertEqual(("vm", "ts"), refreshed)
        self.assertEqual(
            [
                mock.call([str(repo / "sc"), "vm-broker-install"], cwd=repo),
                mock.call([str(repo / "sc"), "ts-broker-install"], cwd=repo),
            ],
            run.call_args_list,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
