#!/usr/bin/env python3
"""Host Python selection and optional-TOML regression coverage."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))

import map_repo  # noqa: E402
import model_catalog  # noqa: E402
import toml_compat  # noqa: E402


class DispatcherRuntimeProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        scripts = self.root / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(ENGINE / "scripts" / "dispatch.sh", scripts / "dispatch.sh")
        (scripts / "install.py").write_text(
            "from pathlib import Path\n"
            "Path(__file__).with_name('install-ran').write_text('yes')\n"
        )
        self.dispatch = scripts / "dispatch.sh"

    def invoke(self, python: str, *argv: str) -> subprocess.CompletedProcess[str]:
        command = argv or ("install",)
        return subprocess.run(
            ["sh", str(self.dispatch), *command],
            cwd=self.root,
            env={
                **os.environ,
                "SC_CALLER_ROOT": str(self.root),
                "SC_PYTHON": python,
                "NO_COLOR": "1",
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def wrapper(self, name: str, probe: str, rc: int = 0) -> Path:
        path = self.root / name
        path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -c ]; then\n"
            f"  printf '%s\\n' '{probe}'\n"
            f"  exit {rc}\n"
            "fi\n"
            f"exec {sys.executable} \"$@\"\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_missing_explicit_interpreter_stops_before_target(self) -> None:
        selected = str(self.root / "missing-python")
        completed = self.invoke(selected)
        self.assertEqual(completed.returncode, 1)
        self.assertIn(f"SC_PYTHON '{selected}' is not executable", completed.stderr)
        self.assertIn("export SC_PYTHON=", completed.stderr)
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())

    def test_incompatible_interpreter_stops_before_target(self) -> None:
        selected = self.wrapper(
            "python-old",
            "Python 3.9+ required; /opt/python3.8 reports 3.8.20",
            rc=2,
        )
        completed = self.invoke(str(selected))
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(selected), completed.stderr)
        self.assertIn("Python 3.9+ required", completed.stderr)
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())

    def test_valid_override_executes_the_reported_exact_interpreter(self) -> None:
        selected = self.root / "python39"
        selected = self.wrapper(
            selected.name,
            f"{selected}|3.9.18|3.35.5",
        )
        completed = self.invoke(str(selected))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            (self.root / ".super-coder/scripts/install-ran").read_text(),
            "yes",
        )

    def test_running_interpreter_passes_the_real_probe(self) -> None:
        completed = self.invoke(sys.executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            (self.root / ".super-coder/scripts/install-ran").read_text(),
            "yes",
        )

    def test_only_shell_implemented_help_bypasses_the_probe(self) -> None:
        selected = str(self.root / "missing-python")
        for command in ("deps", "test", "lint", "typecheck"):
            with self.subTest(command=command):
                help_result = self.invoke(selected, command, "-h")
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertEqual(
                    help_result.stdout,
                    f"Usage: ./sc {command} [-h|--help]\n",
                )
                self.assertNotIn("host Python preflight", help_result.stderr)
        recovery = (
            'export SC_PYTHON="$(brew --prefix)/bin/python3"'
            if sys.platform == "darwin"
            else "export SC_PYTHON=/absolute/path/to/python3"
        )

        for command in ("rebuild",):
            with self.subTest(command=command):
                completed = self.invoke(selected, command, "-h")
                self.assertEqual(completed.returncode, 1)
                self.assertIn(
                    f"SC_PYTHON '{selected}' is not executable",
                    completed.stderr,
                )
                self.assertIn(
                    recovery,
                    completed.stderr,
                )
                self.assertNotIn("No such file or directory", completed.stderr)
                self.assertFalse((self.root / ".venv").exists())


class OptionalTomlTest(unittest.TestCase):
    def test_kimi_toml_is_skipped_when_parser_is_unavailable(self) -> None:
        with (
            mock.patch.object(toml_compat, "AVAILABLE", False),
            mock.patch.object(model_catalog.shutil, "which", return_value="/bin/kimi"),
        ):
            self.assertEqual(model_catalog._from_kimi_config({}, mock.Mock()), [])

    def test_non_toml_manifests_survive_without_parser(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requirements = root / "requirements.txt"
            package = root / "package.json"
            pyproject = root / "pyproject.toml"
            cargo = root / "Cargo.toml"
            requirements.write_text("httpx==1.2.3\n")
            package.write_text('{"dependencies":{"express":"4.0.0"}}')
            pyproject.write_text('[project]\ndependencies=["ignored"]\n')
            cargo.write_text('[dependencies]\nserde="1"\n')
            with mock.patch.object(toml_compat, "AVAILABLE", False):
                self.assertEqual(
                    map_repo.deps_requirements(requirements),
                    [("pip", "httpx", "==1.2.3", "runtime", "requirements.txt")],
                )
                self.assertEqual(
                    map_repo.deps_package_json(package),
                    [("npm", "express", "4.0.0", "runtime", "package.json")],
                )
                self.assertEqual(map_repo.deps_pyproject(pyproject), [])
                self.assertEqual(map_repo.deps_cargo(cargo), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
