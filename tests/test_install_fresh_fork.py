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
    def prepare_repo(self, raw: str) -> tuple[Path, Path]:
        repo = Path(raw) / "host-project"
        home = Path(raw) / "home"
        repo.mkdir()
        home.mkdir()
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
            ["git", "init", "-b", "main"], cwd=repo, check=True,
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fresh Fork Test"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "fresh-fork@noreply.local"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "add", ".super-coder", "sc", "README.md"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "test fixture"], cwd=repo, check=True,
            capture_output=True, text=True,
        )
        return repo, home

    def run_install(self, repo: Path, home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(repo / ".super-coder" / "scripts" / "install.py"),
                "--skip-harness-install",
                "--username",
                "Gate",
            ],
            cwd=repo,
            env={**os.environ, "HOME": str(home), "NO_COLOR": "1"},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_noninteractive_install_reaches_durable_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)

            result = self.run_install(repo, home)

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

    def test_each_failed_critical_phase_withholds_marker_and_reruns_cleanly(self) -> None:
        phases = {
            "rebuild.py": "Building the system DB (schema + migrations)",
            "init_fork.py": "Seeding this fork's starting team",
            "map_setup.py": "Wiring map automation + mapping the repo",
            "snapshot.py": "Serializing the installed state",
            "render.py": "Rendering installed shell surfaces",
        }
        for script_name, phase_name in phases.items():
            with self.subTest(phase=script_name), tempfile.TemporaryDirectory() as raw:
                repo, home = self.prepare_repo(raw)
                script = repo / ".super-coder/scripts" / script_name
                marker = script.with_name("fail-critical-phase")
                marker.write_text("fail\n")
                source = script.read_text()
                injection = (
                    "from pathlib import Path as _FailurePath\n"
                    "if _FailurePath(__file__).with_name('fail-critical-phase').exists():\n"
                    f"    print('injected {script_name} detail', flush=True)\n"
                    "    raise SystemExit(17)\n"
                )
                script.write_text(source.replace(
                    "from __future__ import annotations\n",
                    "from __future__ import annotations\n" + injection,
                    1,
                ))

                failed = self.run_install(repo, home)
                self.assertEqual(failed.returncode, 17)
                self.assertIn(phase_name, failed.stdout)
                self.assertIn(f"injected {script_name} detail", failed.stdout)
                self.assertIn(f"critical phase failed: {phase_name}", failed.stderr)
                self.assertIn("exit code: 17", failed.stderr)
                self.assertIn("retry: ./sc install", failed.stderr)
                self.assertNotIn("Installed ✓", failed.stdout)
                config_path = repo / ".super-coder/instance.json"
                if config_path.exists():
                    self.assertNotIn("installed_at", json.loads(config_path.read_text()))

                marker.unlink()
                repaired = self.run_install(repo, home)
                self.assertEqual(
                    repaired.returncode,
                    0,
                    f"stdout:\n{repaired.stdout}\nstderr:\n{repaired.stderr}",
                )
                self.assertIn("Installed ✓", repaired.stdout)
                self.assertRegex(
                    json.loads(config_path.read_text())["installed_at"],
                    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
