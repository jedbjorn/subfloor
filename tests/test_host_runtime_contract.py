#!/usr/bin/env python3
"""Host Python selection and optional-TOML regression coverage."""
from __future__ import annotations

import hashlib
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
        self.supported_release = self.root / "os-release"
        self.supported_release.write_text("ID=ubuntu\n")

    def invoke(
        self,
        python: str,
        *argv: str,
        kernel: str | None = None,
        os_release: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = argv or ("install",)
        environment = {
            **os.environ,
            "SC_CALLER_ROOT": str(self.root),
            "SC_PYTHON": python,
            "NO_COLOR": "1",
            "SC_PLATFORM_UNAME": kernel or "Linux",
            "SC_PLATFORM_OS_RELEASE": str(os_release or self.supported_release),
        }
        if extra_env:
            environment.update(extra_env)
        return subprocess.run(
            ["sh", str(self.dispatch), *command],
            cwd=self.root,
            env=environment,
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

    def os_release(self, name: str, contents: str) -> Path:
        path = self.root / name
        path.write_text(contents)
        return path

    def sentinel_python(self) -> Path:
        path = self.root / "python-sentinel"
        path.write_text(
            "#!/bin/sh\n"
            f"touch {self.root / 'python-ran'}\n"
            "exit 99\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def snapshot_tree(self, root: Path) -> list[tuple[str, str, int, str]]:
        snapshot = []
        for path in sorted(root.rglob("*")):
            relative = str(path.relative_to(root))
            mode = path.lstat().st_mode
            if path.is_symlink():
                snapshot.append((relative, "symlink", mode, os.readlink(path)))
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                snapshot.append((relative, "file", mode, digest))
            elif path.is_dir():
                snapshot.append((relative, "directory", mode, ""))
        return snapshot

    def test_linux_allowlist_accepts_exact_supported_families(self) -> None:
        fixtures = {
            "ubuntu": "ID=ubuntu\n",
            "fedora": "ID=fedora\n",
            "arch": "ID=arch\n",
            "cachyos": "ID=cachyos\nID_LIKE=arch\n",
        }
        for name, contents in fixtures.items():
            with self.subTest(name=name):
                release = self.os_release(name, contents)
                completed = self.invoke(
                    sys.executable,
                    "install",
                    kernel="Linux",
                    os_release=release,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    (self.root / ".super-coder/scripts/install-ran").read_text(),
                    "yes",
                )
                (self.root / ".super-coder/scripts/install-ran").unlink()

    def test_unsupported_host_refuses_before_python_or_target_with_stable_bytes(self) -> None:
        release = self.os_release("misleading", "ID=notarch\nID_LIKE=notarch\n")
        python = self.sentinel_python()
        git = self.root / "git"
        git.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = rev-parse ]; then exit 1; fi\n"
            f"touch {self.root / 'git-mutator-ran'}\n"
            "exit 99\n"
        )
        git.chmod(git.stat().st_mode | stat.S_IXUSR)
        docker = self.root / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            f"touch {self.root / 'docker-ran'}\n"
            "exit 99\n"
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        before = self.snapshot_tree(self.root)
        sentinels = {
            "PATH": f"{self.root}:{os.environ['PATH']}",
        }
        first = self.invoke(
            str(python), "install", kernel="Linux", os_release=release,
            extra_env=sentinels,
        )
        second = self.invoke(
            str(python), "install", kernel="Linux", os_release=release,
            extra_env=sentinels,
        )
        expected = (
            "✗ subfloor refused: unsupported host.\n"
            "  detected kernel: Linux\n"
            "  detected distribution: ID=notarch; ID_LIKE=notarch\n"
            "  supported hosts: Ubuntu LTS, Fedora stable, Arch-compatible Linux.\n"
            "  Create a supported Linux VM, keep the checkout on the guest filesystem, then run ./sc install inside the guest.\n"
            "  The rejected command was not run and no native compatibility path exists.\n"
        )
        self.assertEqual(first.returncode, 1)
        self.assertEqual(first.stderr, expected)
        self.assertEqual(second.stderr, expected)
        self.assertFalse((self.root / "python-ran").exists())
        self.assertFalse((self.root / "docker-ran").exists())
        self.assertFalse((self.root / "git-mutator-ran").exists())
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())
        self.assertEqual(self.snapshot_tree(self.root), before)

    def test_unreadable_os_release_uses_the_stable_refusal(self) -> None:
        release = self.root / "invalid-os-release"
        release.write_bytes(b"\xff\n")
        python = self.sentinel_python()
        completed = self.invoke(
            str(python), "install", kernel="Linux", os_release=release
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("ID=unknown; ID_LIKE=unknown", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())

    def test_missing_os_release_refuses_before_the_python_probe(self) -> None:
        python = self.sentinel_python()
        completed = self.invoke(
            str(python),
            "install",
            kernel="Linux",
            os_release=self.root / "missing-os-release",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("ID=unknown; ID_LIKE=unknown", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())

    def test_help_stays_readable_but_doctor_and_make_delegate_to_the_gate(self) -> None:
        release = self.os_release("darwin", "ID=macos\n")
        python = self.sentinel_python()
        help_result = self.invoke(
            str(python), "help", kernel="Darwin", os_release=release
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("super-coder", help_result.stdout)

        doctor = self.invoke(
            str(python), "doctor", kernel="Darwin", os_release=release
        )
        self.assertEqual(doctor.returncode, 1)
        self.assertIn("Create a supported Linux VM", doctor.stderr)
        self.assertFalse((self.root / "python-ran").exists())

        windows = self.invoke(
            str(python), "install", kernel="MINGW64_NT", os_release=release
        )
        self.assertEqual(windows.returncode, 1)
        self.assertIn("detected kernel: MINGW64_NT", windows.stderr)
        self.assertFalse((self.root / "python-ran").exists())

        (self.root / "Makefile").write_text(
            "dos-l:\n\tsh .super-coder/scripts/dispatch.sh install\n"
        )
        make_result = subprocess.run(
            ["make", "dos-l"],
            cwd=self.root,
            env={
                **os.environ,
                "SC_PLATFORM_UNAME": "Darwin",
                "SC_PLATFORM_OS_RELEASE": str(release),
                "SC_PYTHON": str(python),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(make_result.returncode, 0)
        self.assertIn("no native compatibility path exists", make_result.stderr)
        self.assertFalse((self.root / "python-ran").exists())

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
