#!/usr/bin/env python3
"""Host Python selection and optional-TOML regression coverage."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
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
import sandbox_devkit  # noqa: E402
import toml_compat  # noqa: E402


class DispatcherRuntimeProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        scripts = self.root / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        self.dispatch_source = (ENGINE / "scripts" / "dispatch.sh").read_text()
        (scripts / "install.py").write_text(
            "from pathlib import Path\n"
            "Path(__file__).with_name('install-ran').write_text('yes')\n"
        )
        self.dispatch = scripts / "dispatch.sh"
        self.configure_host("Linux")

    def configure_host(self, kernel: str) -> None:
        # Only this disposable dispatcher copy receives a deterministic host.
        kernel_probe = 'SC_PLATFORM_KERNEL="$(command -p uname -s 2>/dev/null || true)"'
        self.assertIn(kernel_probe, self.dispatch_source)
        self.dispatch.write_text(
            self.dispatch_source.replace(
                kernel_probe, f"SC_PLATFORM_KERNEL={shlex.quote(kernel)}"
            )
        )

    def tracked_launcher_fixture(self) -> tuple[Path, Path, Path]:
        source = self.root / "source"
        caller = self.root / "caller"
        home = self.root / "home"
        scripts = source / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        home.mkdir()
        shutil.copy2(ROOT / "sc", source / "sc")
        (scripts / "dispatch.sh").write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(self.root / 'live-dispatch-ran'))}\n"
            "exit 99\n"
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "remote",
                "add",
                "origin",
                "https://github.com/jedbjorn/subfloor.git",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "add", "sc", ".super-coder/scripts/dispatch.sh"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Host gate test",
                "-c",
                "user.email=host-gate@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "worktree", "add", "-b", "fixture", str(caller)],
            check=True,
            capture_output=True,
            text=True,
        )
        return source, caller, home

    def invoke(
        self,
        python: str,
        *argv: str,
        extra_env: dict[str, str] | None = None,
        clear_env: tuple[str, ...] = (),
        bare: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = argv if bare else (argv or ("install",))
        environment = {
            **os.environ,
            "SC_CALLER_ROOT": str(self.root),
            "SC_PYTHON": python,
            "NO_COLOR": "1",
        }
        if extra_env:
            environment.update(extra_env)
        for name in clear_env:
            environment.pop(name, None)
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

    def versioned_python(self, name: str, version: tuple[int, int, int]) -> Path:
        path = self.root / name
        version_text = ".".join(str(part) for part in version)
        prelude = (
            "import platform,sys; "
            f"sys.version_info={version!r}; "
            f"platform.python_version=lambda: {version_text!r}; "
            "exec(sys.argv[1])"
        )
        path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -c ]; then\n"
            f"  exec {shlex.quote(sys.executable)} -c {shlex.quote(prelude)} \"$2\"\n"
            "fi\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
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

    def test_every_linux_kernel_reaches_the_dispatch_target(self) -> None:
        fixtures = {
            "ubuntu": "ID=ubuntu\nVERSION_ID=26.10\n",
            "fedora": "ID=fedora\nVERSION_ID=rawhide\n",
            "arch": "ID=arch\n",
            "cachyos": "ID=cachyos\nID_LIKE=arch\n",
            "unknown": "ID=notarch\nID_LIKE=notarch\n",
            "malformed": 'ID="ubuntu\nVERSION_ID=26.04\n',
            "missing": None,
        }
        for name, contents in fixtures.items():
            with self.subTest(name=name):
                release = self.root / name
                if contents is not None:
                    release.write_text(contents)
                self.configure_host("Linux")
                completed = self.invoke(sys.executable, "install")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    (self.root / ".super-coder/scripts/install-ran").read_text(),
                    "yes",
                )
                (self.root / ".super-coder/scripts/install-ran").unlink()

        for marker in ("WSL_DISTRO_NAME", "WSL_INTEROP"):
            with self.subTest(marker=marker):
                self.configure_host("Linux")
                completed = self.invoke(
                    sys.executable, "install", extra_env={marker: "fixture"}
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                (self.root / ".super-coder/scripts/install-ran").unlink()

    def test_global_help_states_linux_only_support_on_every_host(self) -> None:
        hosts = {"supported": "Linux", "unsupported": "Darwin"}
        support_line = "Host support: Linux-only — Ubuntu LTS, stable Fedora, and Arch-compatible Linux (including CachyOS) are tested examples."
        for name, kernel in hosts.items():
            self.configure_host(kernel)
            for command in ((), ("help",), ("-h",), ("--help",)):
                with self.subTest(host=name, command=command or ("bare",)):
                    result = self.invoke(
                        sys.executable, *command, bare=not command
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(support_line, result.stdout)

    def test_native_mac_success_surfaces_are_absent(self) -> None:
        installer = (ENGINE / "scripts" / "install.py").read_text()
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
        public_docs = [
            (ROOT / "README.md").read_text(),
            (ROOT / "docs" / "README.md").read_text(),
            (ROOT / "docs" / "quick-start.md").read_text(),
        ]

        for retired in ("brew", "colima", "Docker Desktop"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired.lower(), self.dispatch_source.lower())
                self.assertNotIn(retired.lower(), installer.lower())
                self.assertNotIn(retired.lower(), workflow.lower())
                for document in public_docs:
                    self.assertNotIn(retired.lower(), document.lower())
        self.assertNotIn("macos-latest", workflow)
        self.assertIn("create a linux vm", public_docs[0].lower())
        self.assertIn("guest-owned storage", public_docs[1])
        self.assertIn("make dos-l", public_docs[2])
        self.assertIn("make dos-e", public_docs[2])

    def test_installer_banner_uses_declared_make_aliases(self) -> None:
        installer = (ENGINE / "scripts" / "install.py").read_text()
        aliases = (ENGINE / "aliases.mk").read_text()

        self.assertIn("make dos-l", installer)
        self.assertIn("make dos-e", installer)
        self.assertNotIn("make launch", installer)
        self.assertNotIn("make enter", installer)
        self.assertIn("dos-launch", aliases)
        self.assertIn("dos-enter", aliases)

    def test_active_engine_surfaces_do_not_claim_native_mac_support(self) -> None:
        surfaces = [
            ENGINE / "scripts" / "install.py",
            ENGINE / "scripts" / "run.py",
            ENGINE / "docs" / "harness-freshness.md",
            ENGINE / "adapters" / "kimi" / "README.md",
            ENGINE / "Dockerfile",
        ]
        forbidden = ("darwin", "macos", "homebrew", "brew", "colima", "docker desktop")

        for path in surfaces:
            body = path.read_text().lower()
            for term in forbidden:
                with self.subTest(path=path, term=term):
                    self.assertNotIn(term, body)

    def test_unsupported_kernel_refuses_before_python_or_target(self) -> None:
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
        self.configure_host("Darwin")
        before = self.snapshot_tree(self.root)
        sentinels = {
            "PATH": f"{self.root}:{os.environ['PATH']}",
        }
        first = self.invoke(str(python), "install", extra_env=sentinels)
        second = self.invoke(str(python), "install", extra_env=sentinels)
        expected = (
            "✗ subfloor refused: unsupported host.\n"
            "  detected kernel: Darwin\n"
            "  subfloor runs on Linux.\n"
            "  Create a Linux VM, keep the checkout on the guest filesystem, then run ./sc install inside the guest.\n"
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

    def test_help_stays_readable_but_doctor_and_make_delegate_to_the_gate(self) -> None:
        python = self.sentinel_python()
        self.configure_host("Darwin")
        for command in ((), ("help",), ("-h",), ("--help",)):
            with self.subTest(command=command or ("bare",)):
                help_result = self.invoke(
                    str(python), *command, bare=not command
                )
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("super-coder", help_result.stdout)

        doctor = self.invoke(str(python), "doctor")
        self.assertEqual(doctor.returncode, 1)
        self.assertIn("Create a Linux VM", doctor.stderr)
        self.assertFalse((self.root / "python-ran").exists())

        self.configure_host("MINGW64_NT")
        windows = self.invoke(str(python), "install")
        self.assertEqual(windows.returncode, 1)
        self.assertIn("detected kernel: MINGW64_NT", windows.stderr)
        self.assertFalse((self.root / "python-ran").exists())

        (self.root / "Makefile").write_text(
            "dos-l:\n\tsh .super-coder/scripts/dispatch.sh install\n"
        )
        self.configure_host("Darwin")
        make_result = subprocess.run(
            ["make", "dos-l"],
            cwd=self.root,
            env={
                **os.environ,
                "SC_PYTHON": str(python),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(make_result.returncode, 0)
        self.assertIn("no native compatibility path exists", make_result.stderr)
        self.assertFalse((self.root / "python-ran").exists())

    def test_platform_environment_cannot_override_test_host(self) -> None:
        python = self.sentinel_python()
        self.configure_host("Darwin")

        completed = self.invoke(
            str(python),
            "install",
            extra_env={
                "SC_PLATFORM_UNAME": "Linux",
                "SC_PLATFORM_OS_RELEASE": "/tmp/ignored-os-release",
            },
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("detected kernel: Darwin", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())

    def test_tracked_launcher_leaves_operator_override_to_its_selected_body(self) -> None:
        _source, caller, home = self.tracked_launcher_fixture()
        override = self.root / "arbitrary-dispatch.sh"
        override.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(self.root / 'override-ran'))}\n"
            "exit 0\n"
        )
        environment = {
            **os.environ,
            "HOME": str(home),
            "SC_DISPATCH": str(override),
        }
        environment.pop("WSL_DISTRO_NAME", None)
        environment.pop("WSL_INTEROP", None)

        completed = subprocess.run(
            [str(caller / "sc"), "install"],
            cwd=caller,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.root / "override-ran").exists())
        self.assertFalse((self.root / "live-dispatch-ran").exists())

    def test_missing_explicit_interpreter_stops_before_target(self) -> None:
        selected = str(self.root / "missing-python")
        completed = self.invoke(selected)
        self.assertEqual(completed.returncode, 1)
        self.assertIn(f"SC_PYTHON '{selected}' is not executable", completed.stderr)
        self.assertIn("export SC_PYTHON=", completed.stderr)
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())

    def test_other_python_minors_stop_before_target(self) -> None:
        for version in ((3, 13, 9), (3, 15, 0)):
            with self.subTest(version=version):
                selected = self.versioned_python(
                    "python" + "".join(str(part) for part in version[:2]), version
                )
                completed = self.invoke(str(selected))
                self.assertEqual(completed.returncode, 1)
                self.assertIn("Python 3.14.x required", completed.stderr)
                self.assertIn(
                    "reports " + ".".join(map(str, version)), completed.stderr
                )
                self.assertIn(
                    "recovery: install Python 3.14.x with sqlite3",
                    completed.stderr,
                )
                self.assertFalse(
                    (self.root / ".super-coder/scripts/install-ran").exists()
                )

    def test_valid_override_executes_the_reported_exact_interpreter(self) -> None:
        selected = self.root / "python314"
        selected = self.wrapper(
            selected.name,
            f"{selected}|3.14.7|3.53.4",
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
        recovery = "export SC_PYTHON=/absolute/path/to/python3"

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


class Python314SourceContractTest(unittest.TestCase):
    def test_every_maintained_setup_python_selector_is_exact(self) -> None:
        workflow_roots = (
            ROOT / ".github" / "workflows",
            ENGINE / "templates" / "fork",
        )
        selectors: dict[str, list[str]] = {}
        for workflow_root in workflow_roots:
            for path in workflow_root.glob("*.yml"):
                matches = re.findall(
                    r"python-version:\s*['\"]?([^'\"\s]+)",
                    path.read_text(),
                )
                if matches:
                    selectors[str(path.relative_to(ROOT))] = matches

        self.assertEqual(
            selectors,
            {
                ".github/workflows/render-check.yml": ["3.14"],
                ".github/workflows/tests.yml": ["3.14", "3.14", "3.14", "3.14"],
                ".super-coder/templates/fork/subfloor-visual-qa.yml": ["3.14"],
            },
        )

    def test_host_contract_name_and_runtime_defaults_are_stable(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
        dockerfile = (ENGINE / "Dockerfile").read_text()

        self.assertIn("name: Python 3.14 Linux host contract", workflow)
        self.assertNotIn("Python 3.9 Linux host contract", workflow)
        self.assertIn("ARG SC_PARENT_IMAGE=python:3.14-slim", dockerfile)
        self.assertEqual(sandbox_devkit.DEFAULT_PARENT_IMAGE, "python:3.14-slim")

if __name__ == "__main__":
    unittest.main(verbosity=2)
