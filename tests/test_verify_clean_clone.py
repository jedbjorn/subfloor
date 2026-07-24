#!/usr/bin/env python3
"""Regression: a content-free engine-source checkout must still verify."""
from __future__ import annotations

import os
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
            # Include a locally edited dispatcher when this regression test is
            # run before its fix has been committed.
            shutil.copy2(ROOT / "sc", checkout / "sc")
            env = os.environ.copy()
            env.pop("SC_ARTIFACT_MODE", None)
            result = subprocess.run(
                ["./sc", "verify"], cwd=checkout, env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(
                result.returncode, 0,
                f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
