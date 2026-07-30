"""Fresh-fork installer completion regression coverage."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FreshForkInstallTest(unittest.TestCase):
    def test_noninteractive_install_reaches_durable_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "host-project"
            repo.mkdir()
            shutil.copytree(
                ROOT / ".super-coder",
                repo / ".super-coder",
                ignore=shutil.ignore_patterns(
                    "shell_db.db*",
                    "instance.json",
                    "run",
                    "logs",
                    "__pycache__",
                ),
            )
            shutil.copy2(ROOT / "sc", repo / "sc")
            (repo / "README.md").write_text("# disposable host project\n")

            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fresh Fork Test"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "fresh-fork@noreply.local"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "add", ".super-coder", "sc", "README.md"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test fixture"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(repo / ".super-coder" / "scripts" / "install.py"),
                    "--skip-harness-install",
                    "--username",
                    "Gate",
                ],
                cwd=repo,
                env={**os.environ, "NO_COLOR": "1"},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            config = json.loads(
                (repo / ".super-coder" / "instance.json").read_text()
            )
            self.assertRegex(
                config["installed_at"],
                re.compile(r"^\d{4}-\d{2}-\d{2}$"),
            )
            self.assertIn("Installed ✓", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
