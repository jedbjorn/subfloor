"""Fresh-fork installer completion regression coverage."""
from __future__ import annotations

import hashlib
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

    def assert_direct_refusal_is_pristine(
        self,
        repo: Path,
        home: Path,
        before_repo: list[tuple[str, str, int, str]],
        before_home: list[tuple[str, str, int, str]],
    ) -> None:
        self.assertEqual(self.snapshot_tree(repo), before_repo)
        self.assertEqual(self.snapshot_tree(home), before_home)
        self.assertFalse((repo / ".gitignore").exists())
        self.assertFalse((repo / "Makefile").exists())
        self.assertFalse((repo / ".sc-state").exists())
        self.assertFalse((repo / ".super-coder" / "instance.json").exists())
        self.assertFalse((repo / ".super-coder" / "shell_db.db").exists())
        self.assertFalse((home / ".local" / "bin" / "sc").exists())
        self.assertFalse((home / ".local" / "state" / "super-coder" / "installs.json").exists())
        self.assertFalse((home / ".profile").exists())

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
            ["git", "config", "maintenance.auto", "false"],
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

    def configure_dispatch_host(
        self, repo: Path, kernel: str, release: Path, runtime: Path | None = None
    ) -> None:
        # Only this disposable dispatcher copy receives a deterministic host.
        dispatch = repo / ".super-coder" / "scripts" / "dispatch.sh"
        source = dispatch.read_text()
        kernel_probe = 'SC_PLATFORM_KERNEL="$(command -p uname -s 2>/dev/null || true)"'
        release_probe = "_platform_release=/etc/os-release"
        runtime_probe = "_platform_wsl_release=/proc/sys/kernel/osrelease"
        self.assertIn(kernel_probe, source)
        self.assertIn(release_probe, source)
        self.assertIn(runtime_probe, source)
        dispatch.write_text(
            source.replace(
                kernel_probe, f"SC_PLATFORM_KERNEL={kernel!r}"
            )
            .replace(release_probe, f"_platform_release={str(release)!r}")
            .replace(
                runtime_probe,
                f"_platform_wsl_release={str(runtime or Path('/proc/sys/kernel/osrelease'))!r}",
            )
        )

    def run_direct_host(
        self,
        repo: Path,
        home: Path,
        kernel: str,
        release: Path,
        extra_env: dict[str, str] | None = None,
        args: list[str] | None = None,
        timeout: int | None = None,
        python_version: tuple[int, int, int] | None = None,
        runtime: Path | None = None,
        clear_env: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        # The subprocess patches host probes before executing the unmodified installer.
        install = repo / ".super-coder" / "scripts" / "install.py"
        argv = [str(install), *(args or ["--harness-epoch"])]
        python_probe = ""
        if python_version:
            version_text = ".".join(str(part) for part in python_version)
            python_probe = (
                f"sys.version_info = {python_version!r}\n"
                f"platform.python_version = lambda: {version_text!r}\n"
            )
        direct_entry = (
            "import builtins\n"
            "import platform\n"
            "import runpy\n"
            "import sys\n"
            "original_open = builtins.open\n"
            f"release = {str(release)!r}\n"
            f"runtime = {str(runtime) if runtime else None!r}\n"
            f"platform.system = lambda: {kernel!r}\n"
            "def host_open(path, *args, **kwargs):\n"
            "    if path == '/etc/os-release':\n"
            "        return original_open(release, *args, **kwargs)\n"
            "    if path == '/proc/sys/kernel/osrelease' and runtime is not None:\n"
            "        return original_open(runtime, *args, **kwargs)\n"
            "    return original_open(path, *args, **kwargs)\n"
            "builtins.open = host_open\n"
            + python_probe
            + f"sys.argv = {argv!r}\n"
            + f"runpy.run_path({str(install)!r}, run_name='__main__')\n"
        )
        environment = {
            **os.environ,
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            **(extra_env or {}),
        }
        for name in clear_env:
            environment.pop(name, None)
        return subprocess.run(
            [sys.executable, "-c", direct_entry],
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def run_install(self, repo: Path, home: Path) -> subprocess.CompletedProcess[str]:
        release = home / "os-release"
        release.write_text("ID=ubuntu\nVERSION_ID=26.04\n")
        return self.run_direct_host(
            repo,
            home,
            "Linux",
            release,
            {
                "XDG_STATE_HOME": str(home / ".local/state"),
                "NO_COLOR": "1",
            },
            args=["--skip-harness-install", "--username", "Gate"],
            timeout=120,
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
            wrapper = home / ".local/bin/sc"
            registry = home / ".local/state/super-coder/installs.json"
            wrapper_text = wrapper.read_text()
            self.assertIn("# managed-by: subfloor sc-wrapper v1", wrapper_text)
            self.assertIn("git rev-parse --show-toplevel", wrapper_text)
            self.assertNotIn("SC_ROOT", wrapper_text)
            self.assertEqual(
                json.loads(registry.read_text())["installs"],
                [str(repo.resolve())],
            )
            self.assertIn("subfloor managed PATH", (home / ".profile").read_text())

    def test_direct_installer_rejects_old_python_before_repository_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            before = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            release = home / "os-release"
            release.write_text("ID=ubuntu\nVERSION_ID=26.04\n")
            result = self.run_direct_host(
                repo,
                home,
                "Linux",
                release,
                {"NO_COLOR": "1"},
                args=["--skip-harness-install"],
                python_version=(3, 8, 20),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Python 3.9+ required", result.stderr)
            self.assertIn("reports 3.8.20", result.stderr)
            self.assertFalse((repo / ".gitignore").exists())
            self.assertFalse((repo / ".sc-state").exists())
            self.assertFalse((repo / ".super-coder" / "instance.json").exists())
            after = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(after, before)

    def test_direct_installer_platform_gate_matches_allowlist_without_mutation(self) -> None:
        fixtures = {
            "ubuntu-lts": ("Linux", "ID=ubuntu\nVERSION_ID=26.04", True),
            "fedora-stable": ("Linux", "ID=fedora\nVERSION_ID=44", True),
            "arch": ("Linux", "ID=arch\n", True),
            "cachyos": ("Linux", "ID=cachyos\nID_LIKE=arch\n", True),
            "ubuntu-interim": ("Linux", "ID=ubuntu\nVERSION_ID=26.10\n", False),
            "fedora-rawhide": ("Linux", "ID=fedora\nVERSION_ID=rawhide\n", False),
            "malformed-quote": ("Linux", 'ID="ubuntu\nVERSION_ID=26.04\n', False),
            "unknown-linux": ("Linux", "ID=notarch\nID_LIKE=notarch\n", False),
            "missing-os-release": ("Linux", None, False),
            "darwin": ("Darwin", "ID=macos\n", False),
            "windows": ("MINGW64_NT", "ID=windows\n", False),
        }
        for name, (kernel, contents, accepted) in fixtures.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                repo, home = self.prepare_repo(raw)
                release = Path(raw) / "os-release"
                if contents is not None:
                    release.write_text(contents)
                self.configure_dispatch_host(repo, kernel, release)
                before_repo = self.snapshot_tree(repo)
                before_home = self.snapshot_tree(home)
                result = self.run_direct_host(repo, home, kernel, release)
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "0")
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("supported Linux VM", result.stderr)
                    if name == "ubuntu-interim":
                        self.assertIn("VERSION_ID=26.10", result.stderr)
                    if name == "fedora-rawhide":
                        self.assertIn("VERSION_ID=rawhide", result.stderr)
                    if name == "malformed-quote":
                        self.assertIn("ID=unknown; ID_LIKE=unknown; VERSION_ID=unknown", result.stderr)
                    shell = subprocess.run(
                        ["sh", str(repo / ".super-coder/scripts/dispatch.sh"), "install"],
                        cwd=repo,
                        env={
                            **os.environ,
                            "HOME": str(home),
                            "SC_CALLER_ROOT": str(repo),
                            "SC_PYTHON": sys.executable,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(shell.returncode, 1)
                    self.assertEqual(shell.stderr, result.stderr)
                    self.assert_direct_refusal_is_pristine(
                        repo, home, before_repo, before_home
                    )

    def test_wsl_runtime_signature_refuses_without_marker_variables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "os-release"
            runtime = Path(raw) / "wsl-runtime"
            release.write_text("ID=ubuntu\nVERSION_ID=26.04\n")
            runtime.write_text("5.15.167.4-microsoft-standard-WSL2\n")
            sentinel = home / "python-sentinel"
            sentinel.write_text(
                "#!/bin/sh\n"
                f"touch {home / 'python-ran'}\n"
                "exit 99\n"
            )
            sentinel.chmod(sentinel.stat().st_mode | 0o100)
            self.configure_dispatch_host(repo, "Linux", release, runtime)
            before_repo = self.snapshot_tree(repo)
            before_home = self.snapshot_tree(home)
            direct = self.run_direct_host(
                repo,
                home,
                "Linux",
                release,
                runtime=runtime,
                clear_env=("WSL_DISTRO_NAME", "WSL_INTEROP"),
            )
            shell_env = {
                **os.environ,
                "HOME": str(home),
                "SC_CALLER_ROOT": str(repo),
                "SC_PYTHON": str(sentinel),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            shell_env.pop("WSL_DISTRO_NAME", None)
            shell_env.pop("WSL_INTEROP", None)
            shell = subprocess.run(
                ["sh", str(repo / ".super-coder/scripts/dispatch.sh"), "install"],
                cwd=repo,
                env=shell_env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(direct.returncode, 1)
            self.assertEqual(shell.returncode, 1)
            self.assertEqual(shell.stderr, direct.stderr)
            self.assertIn("ID=ubuntu; ID_LIKE=unknown; VERSION_ID=26.04", direct.stderr)
            self.assertFalse((home / "python-ran").exists())
            self.assert_direct_refusal_is_pristine(
                repo, home, before_repo, before_home
            )

    def test_wsl_ubuntu_refuses_with_dispatcher_parity_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "os-release"
            release.write_text("ID=ubuntu\nVERSION_ID=26.04\n")
            sentinel = home / "python-sentinel"
            sentinel.write_text(
                "#!/bin/sh\n"
                f"touch {home / 'python-ran'}\n"
                "exit 99\n"
            )
            sentinel.chmod(sentinel.stat().st_mode | 0o100)
            self.configure_dispatch_host(repo, "Linux", release)
            before_repo = self.snapshot_tree(repo)
            before_home = self.snapshot_tree(home)
            wsl = {
                "WSL_DISTRO_NAME": "Ubuntu",
                "WSL_INTEROP": "/run/WSL/1_interop",
            }

            for marker, value in wsl.items():
                with self.subTest(marker=marker):
                    direct = self.run_direct_host(
                        repo, home, "Linux", release, {marker: value}
                    )
                    shell = subprocess.run(
                        ["sh", str(repo / ".super-coder/scripts/dispatch.sh"), "install"],
                        cwd=repo,
                        env={
                            **os.environ,
                            "HOME": str(home),
                            "SC_CALLER_ROOT": str(repo),
                            "SC_PYTHON": str(sentinel),
                            "PYTHONDONTWRITEBYTECODE": "1",
                            marker: value,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(direct.returncode, 1)
                    self.assertEqual(shell.returncode, 1)
                    self.assertEqual(shell.stderr, direct.stderr)
                    self.assertIn(
                        "ID=ubuntu; ID_LIKE=unknown; VERSION_ID=26.04",
                        direct.stderr,
                    )
                    self.assertFalse((home / "python-ran").exists())
                    self.assert_direct_refusal_is_pristine(
                        repo, home, before_repo, before_home
                    )

    def test_direct_installer_ignores_platform_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "os-release"
            release.write_text("ID=ubuntu\nVERSION_ID=26.04\n")
            before_repo = self.snapshot_tree(repo)
            before_home = self.snapshot_tree(home)

            result = self.run_direct_host(
                repo,
                home,
                "Darwin",
                release,
                {
                    "SC_PLATFORM_UNAME": "Linux",
                    "SC_PLATFORM_OS_RELEASE": str(release),
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("detected kernel: Darwin", result.stderr)
            self.assert_direct_refusal_is_pristine(
                repo, home, before_repo, before_home
            )

    def test_direct_installer_refuses_late_unreadable_os_release_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "invalid-os-release"
            release.write_bytes(
                b"ID=ubuntu\nVERSION_ID=26.04\nPADDING="
                + b"x" * 20_000
                + b"\nBROKEN=\xff\n"
            )
            sentinel = home / "python-sentinel"
            sentinel.write_text(
                "#!/bin/sh\n"
                f"touch {home / 'python-ran'}\n"
                "exit 99\n"
            )
            sentinel.chmod(sentinel.stat().st_mode | 0o100)
            self.configure_dispatch_host(repo, "Linux", release)
            before_repo = self.snapshot_tree(repo)
            before_home = self.snapshot_tree(home)
            shell = subprocess.run(
                ["sh", str(repo / ".super-coder/scripts/dispatch.sh"), "install"],
                cwd=repo,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "SC_CALLER_ROOT": str(repo),
                    "SC_PYTHON": str(sentinel),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            result = self.run_direct_host(repo, home, "Linux", release)
            self.assertEqual(shell.returncode, 1)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(shell.stderr, result.stderr)
            self.assertIn("ID=unknown; ID_LIKE=unknown", result.stderr)
            self.assertFalse((home / "python-ran").exists())
            self.assert_direct_refusal_is_pristine(
                repo, home, before_repo, before_home
            )

    def test_direct_installer_refuses_nul_os_release_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "nul-os-release"
            release.write_bytes(b"ID=ubuntu\nVERSION_ID=26.04\0")
            sentinel = home / "python-sentinel"
            sentinel.write_text(
                "#!/bin/sh\n"
                f"touch {home / 'python-ran'}\n"
                "exit 99\n"
            )
            sentinel.chmod(sentinel.stat().st_mode | 0o100)
            self.configure_dispatch_host(repo, "Linux", release)
            before_repo = self.snapshot_tree(repo)
            before_home = self.snapshot_tree(home)
            shell = subprocess.run(
                ["sh", str(repo / ".super-coder/scripts/dispatch.sh"), "install"],
                cwd=repo,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "SC_CALLER_ROOT": str(repo),
                    "SC_PYTHON": str(sentinel),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            result = self.run_direct_host(repo, home, "Linux", release)

            self.assertEqual(shell.returncode, 1)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(shell.stderr, result.stderr)
            self.assertIn("ID=unknown; ID_LIKE=unknown", result.stderr)
            self.assertFalse((home / "python-ran").exists())
            self.assert_direct_refusal_is_pristine(
                repo, home, before_repo, before_home
            )

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
