#!/usr/bin/env python3
"""Regression: a content-free engine-source checkout must still verify."""
from __future__ import annotations

import os
import pty
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VerifyCleanCloneTest(unittest.TestCase):
    def test_verify_initializes_an_empty_source_instance(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td) / "checkout"
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", str(ROOT), str(checkout)],
                check=True,
            )
            subprocess.run(
                ["git", "checkout", "--quiet", sha], cwd=checkout, check=True,
            )
            # Include locally edited launch-floor files when this regression
            # test is run before their fixes have been committed. This install
            # keeps the engine as a gitignored dependency, so the clone carries
            # no .super-coder/ — copy the whole materialized floor in (minus
            # runtime state and the instance binding), matching what an
            # install would leave on disk. Dropping instance.json is the
            # point: the clone must verify as its OWN instance, never bind to
            # this checkout's live private state.
            shutil.copy2(ROOT / "sc", checkout / "sc")
            ignore = shutil.ignore_patterns(
                "__pycache__", "run", "instance.json",
                "*.db", "*.db-wal", "*.db-shm")
            shutil.copytree(
                ROOT / ".super-coder",
                checkout / ".super-coder",
                ignore=ignore,
                dirs_exist_ok=True,
            )
            env = os.environ.copy()
            env.pop("SC_ARTIFACT_MODE", None)
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen(
                    ["./sc", "verify"],
                    cwd=checkout,
                    env=env,
                    stdin=slave,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                os.close(slave)
                slave = -1
                try:
                    stdout, stderr = process.communicate(timeout=120)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
            finally:
                if slave >= 0:
                    os.close(slave)
                os.close(master)
            self.assertEqual(
                process.returncode, 0,
                f"stdout:\n{stdout}\n\nstderr:\n{stderr}",
            )
            self.assertNotIn("Username:", stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
