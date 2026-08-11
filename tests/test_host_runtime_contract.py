#!/usr/bin/env python3
"""Host Python selection and optional-TOML regression coverage."""
from __future__ import annotations

import hashlib
import os
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
        self.supported_release = self.root / "os-release"
        self.supported_release.write_text("ID=ubuntu\nVERSION_ID=26.04\n")
        self.configure_host("Linux", self.supported_release)

    def configure_host(
        self, kernel: str, os_release: Path, runtime: Path | None = None
    ) -> None:
        # Only this disposable dispatcher copy receives a deterministic host.
        kernel_probe = 'SC_PLATFORM_KERNEL="$(command -p uname -s 2>/dev/null || true)"'
        release_probe = "_platform_release=/etc/os-release"
        runtime_probe = "_platform_wsl_release=/proc/sys/kernel/osrelease"
        self.assertIn(kernel_probe, self.dispatch_source)
        self.assertIn(release_probe, self.dispatch_source)
        self.assertIn(runtime_probe, self.dispatch_source)
        self.dispatch.write_text(
            self.dispatch_source.replace(
                kernel_probe, f"SC_PLATFORM_KERNEL={shlex.quote(kernel)}"
            )
            .replace(release_probe, f"_platform_release={shlex.quote(str(os_release))}")
            .replace(
                runtime_probe,
                f"_platform_wsl_release={shlex.quote(str(runtime or Path('/proc/sys/kernel/osrelease')))}",
            )
        )

    def configure_launcher_host(
        self,
        launcher: Path,
        kernel: str,
        os_release: Path,
        runtime: Path | None = None,
    ) -> None:
        source = launcher.read_text()
        kernel_probe = (
            'SC_BOOTSTRAP_PLATFORM_KERNEL="$(command -p uname -s 2>/dev/null || true)"'
        )
        release_probe = "_platform_release=/etc/os-release"
        runtime_probe = "_platform_wsl_release=/proc/sys/kernel/osrelease"
        self.assertIn(kernel_probe, source)
        self.assertIn(release_probe, source)
        self.assertIn(runtime_probe, source)
        launcher.write_text(
            source.replace(
                kernel_probe,
                f"SC_BOOTSTRAP_PLATFORM_KERNEL={shlex.quote(kernel)}",
            )
            .replace(release_probe, f"_platform_release={shlex.quote(str(os_release))}")
            .replace(
                runtime_probe,
                f"_platform_wsl_release={shlex.quote(str(runtime or Path('/proc/sys/kernel/osrelease')))}",
            )
        )

    def tracked_launcher_fixture(
        self,
        kernel: str,
        release_contents: str,
        origin: str = "https://github.com/jedbjorn/subfloor.git",
    ) -> tuple[Path, Path, Path]:
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
                origin,
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
        release = self.os_release("launcher-os-release", release_contents)
        self.configure_launcher_host(caller / "sc", kernel, release)
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
            "ubuntu-lts": "ID=ubuntu\nVERSION_ID=26.04",
            "fedora-stable": "ID=fedora\nVERSION_ID=44",
            "arch": "ID=arch\n",
            "cachyos": "ID=cachyos\nID_LIKE=arch\n",
        }
        for name, contents in fixtures.items():
            with self.subTest(name=name):
                release = self.os_release(name, contents)
                self.configure_host("Linux", release)
                completed = self.invoke(sys.executable, "install")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    (self.root / ".super-coder/scripts/install-ran").read_text(),
                    "yes",
                )
                (self.root / ".super-coder/scripts/install-ran").unlink()

    def test_release_allowlist_rejects_ubuntu_interim_and_fedora_rawhide(self) -> None:
        python = self.sentinel_python()
        fixtures = {
            "ubuntu-interim": "ID=ubuntu\nVERSION_ID=26.10\n",
            "fedora-rawhide": "ID=fedora\nVERSION_ID=rawhide\n",
        }
        for name, contents in fixtures.items():
            with self.subTest(name=name):
                release = self.os_release(name, contents)
                self.configure_host("Linux", release)
                completed = self.invoke(str(python), "install")
                self.assertEqual(completed.returncode, 1)
                self.assertIn("supported Linux VM", completed.stderr)
                self.assertIn(
                    f"VERSION_ID={'26.10' if name == 'ubuntu-interim' else 'rawhide'}",
                    completed.stderr,
                )
                self.assertFalse((self.root / "python-ran").exists())
                self.assertFalse(
                    (self.root / ".super-coder/scripts/install-ran").exists()
                )

    def test_global_help_states_linux_only_support_on_every_host(self) -> None:
        hosts = {
            "supported": ("Linux", self.supported_release),
            "unsupported": ("Darwin", self.os_release("darwin", "ID=macos\n")),
        }
        support_line = (
            "Host support: Linux-only — Ubuntu LTS, Fedora stable, "
            "Arch-compatible Linux."
        )
        for name, (kernel, release) in hosts.items():
            self.configure_host(kernel, release)
            for command in ((), ("help",), ("-h",), ("--help",)):
                with self.subTest(host=name, command=command or ("bare",)):
                    result = self.invoke(
                        sys.executable, *command, bare=not command
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(support_line, result.stdout)

    def test_wsl_refuses_allowlisted_ubuntu_before_python_or_target(self) -> None:
        release = self.os_release("wsl-ubuntu", "ID=ubuntu\nVERSION_ID=26.04\n")
        python = self.sentinel_python()
        self.configure_host("Linux", release)
        before = self.snapshot_tree(self.root)
        wsl = {
            "WSL_DISTRO_NAME": "Ubuntu",
            "WSL_INTEROP": "/run/WSL/1_interop",
        }

        for command in ((), ("help",), ("-h",), ("--help",)):
            with self.subTest(command=command or ("bare",)):
                help_result = self.invoke(
                    str(python), *command, extra_env=wsl, bare=not command
                )
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("super-coder", help_result.stdout)
                self.assertIn(
                    "Host support: Linux-only — Ubuntu LTS, Fedora stable, "
                    "Arch-compatible Linux.",
                    help_result.stdout,
                )

        expected = (
            "✗ subfloor refused: unsupported host.\n"
            "  detected kernel: Linux\n"
            "  detected distribution: ID=ubuntu; ID_LIKE=unknown; VERSION_ID=26.04\n"
            "  supported hosts: Ubuntu LTS, Fedora stable, Arch-compatible Linux.\n"
            "  Create a supported Linux VM, keep the checkout on the guest filesystem, then run ./sc install inside the guest.\n"
            "  The rejected command was not run and no native compatibility path exists.\n"
        )
        for marker, value in wsl.items():
            with self.subTest(marker=marker):
                completed = self.invoke(
                    str(python), "install", extra_env={marker: value}
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(completed.stderr, expected)
                self.assertFalse((self.root / "python-ran").exists())
                self.assertFalse(
                    (self.root / ".super-coder/scripts/install-ran").exists()
                )
                self.assertEqual(self.snapshot_tree(self.root), before)

    def test_wsl_runtime_signature_refuses_without_marker_variables(self) -> None:
        release = self.os_release("wsl-ubuntu", "ID=ubuntu\nVERSION_ID=26.04\n")
        runtime = self.os_release(
            "wsl-runtime", "5.15.167.4-microsoft-standard-WSL2\n"
        )
        python = self.sentinel_python()
        self.configure_host("Linux", release, runtime)
        before = self.snapshot_tree(self.root)
        completed = self.invoke(
            str(python),
            "install",
            clear_env=("WSL_DISTRO_NAME", "WSL_INTEROP"),
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("ID=ubuntu; ID_LIKE=unknown; VERSION_ID=26.04", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())
        self.assertEqual(self.snapshot_tree(self.root), before)

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
        self.configure_host("Linux", release)
        before = self.snapshot_tree(self.root)
        sentinels = {
            "PATH": f"{self.root}:{os.environ['PATH']}",
        }
        first = self.invoke(str(python), "install", extra_env=sentinels)
        second = self.invoke(str(python), "install", extra_env=sentinels)
        expected = (
            "✗ subfloor refused: unsupported host.\n"
            "  detected kernel: Linux\n"
            "  detected distribution: ID=notarch; ID_LIKE=notarch; VERSION_ID=unknown\n"
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

    def test_corrupt_os_release_refuses_before_python_probe(self) -> None:
        release = self.root / "invalid-os-release"
        release.write_bytes(b"ID=ubuntu\nVERSION_ID=26.04\nBROKEN=\xff\n")
        python = self.sentinel_python()
        self.configure_host("Linux", release)
        completed = self.invoke(str(python), "install")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("ID=unknown; ID_LIKE=unknown", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())

    def test_malformed_os_release_quote_refuses_before_python_probe(self) -> None:
        release = self.os_release("malformed-os-release", 'ID="ubuntu\nVERSION_ID=26.04\n')
        python = self.sentinel_python()
        self.configure_host("Linux", release)
        before = self.snapshot_tree(self.root)
        completed = self.invoke(str(python), "install")
        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            "ID=unknown; ID_LIKE=unknown; VERSION_ID=unknown", completed.stderr
        )
        self.assertFalse((self.root / "python-ran").exists())
        self.assertEqual(self.snapshot_tree(self.root), before)

    def test_missing_os_release_refuses_before_the_python_probe(self) -> None:
        python = self.sentinel_python()
        self.configure_host("Linux", self.root / "missing-os-release")
        completed = self.invoke(str(python), "install")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("ID=unknown; ID_LIKE=unknown", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())

    def test_help_stays_readable_but_doctor_and_make_delegate_to_the_gate(self) -> None:
        release = self.os_release("darwin", "ID=macos\n")
        python = self.sentinel_python()
        self.configure_host("Darwin", release)
        for command in ((), ("help",), ("-h",), ("--help",)):
            with self.subTest(command=command or ("bare",)):
                help_result = self.invoke(
                    str(python), *command, bare=not command
                )
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("super-coder", help_result.stdout)

        doctor = self.invoke(str(python), "doctor")
        self.assertEqual(doctor.returncode, 1)
        self.assertIn("Create a supported Linux VM", doctor.stderr)
        self.assertFalse((self.root / "python-ran").exists())

        self.configure_host("MINGW64_NT", release)
        windows = self.invoke(str(python), "install")
        self.assertEqual(windows.returncode, 1)
        self.assertIn("detected kernel: MINGW64_NT", windows.stderr)
        self.assertFalse((self.root / "python-ran").exists())

        (self.root / "Makefile").write_text(
            "dos-l:\n\tsh .super-coder/scripts/dispatch.sh install\n"
        )
        self.configure_host("Darwin", release)
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
        release = self.os_release("ubuntu", "ID=ubuntu\nVERSION_ID=26.04\n")
        python = self.sentinel_python()
        self.configure_host("Darwin", release)

        completed = self.invoke(
            str(python),
            "install",
            extra_env={
                "SC_PLATFORM_UNAME": "Linux",
                "SC_PLATFORM_OS_RELEASE": str(release),
            },
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("detected kernel: Darwin", completed.stderr)
        self.assertFalse((self.root / "python-ran").exists())
        self.assertFalse((self.root / ".super-coder/scripts/install-ran").exists())

    def test_tracked_launcher_refuses_unsupported_host_before_dispatch_override(self) -> None:
        source, caller, home = self.tracked_launcher_fixture(
            "Darwin", "ID=macos\n"
        )
        override = caller / ".super-coder" / "scripts" / "dispatch.sh"
        override.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(self.root / 'override-ran'))}\n"
            "exit 0\n"
        )
        before_source = self.snapshot_tree(source)
        before_caller = self.snapshot_tree(caller)
        before_home = self.snapshot_tree(home)
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

        expected = (
            "✗ subfloor refused: unsupported host.\n"
            "  detected kernel: Darwin\n"
            "  detected distribution: ID=macos; ID_LIKE=unknown; VERSION_ID=unknown\n"
            "  supported hosts: Ubuntu LTS, Fedora stable, Arch-compatible Linux.\n"
            "  Create a supported Linux VM, keep the checkout on the guest filesystem, then run ./sc install inside the guest.\n"
            "  The rejected command was not run and no native compatibility path exists.\n"
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, expected)
        self.assertFalse((self.root / "override-ran").exists())
        self.assertFalse((self.root / "live-dispatch-ran").exists())
        self.assertEqual(self.snapshot_tree(source), before_source)
        self.assertEqual(self.snapshot_tree(caller), before_caller)
        self.assertEqual(self.snapshot_tree(home), before_home)

    def test_dispatch_override_rejects_arbitrary_body_on_supported_host(self) -> None:
        _source, caller, home = self.tracked_launcher_fixture(
            "Linux", "ID=ubuntu\nVERSION_ID=26.04\n"
        )
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

        expected_dispatch = caller / ".super-coder" / "scripts" / "dispatch.sh"
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stderr,
            "✗ ./sc: SC_DISPATCH is restricted to the canonical source "
            f"checkout's tracked dispatcher: {expected_dispatch}\n",
        )
        self.assertFalse((self.root / "override-ran").exists())
        self.assertFalse((self.root / "live-dispatch-ran").exists())

    def test_dispatch_override_rejects_tracked_body_outside_source_repo(self) -> None:
        _source, caller, home = self.tracked_launcher_fixture(
            "Linux",
            "ID=ubuntu\nVERSION_ID=26.04\n",
            origin="https://github.com/example/application.git",
        )
        override = caller / ".super-coder" / "scripts" / "dispatch.sh"
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

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stderr,
            "✗ ./sc: SC_DISPATCH is restricted to the canonical source "
            f"checkout's tracked dispatcher: {override}\n",
        )
        self.assertFalse((self.root / "override-ran").exists())
        self.assertFalse((self.root / "live-dispatch-ran").exists())

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
