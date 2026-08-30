"""Fresh-fork installer completion regression coverage."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
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
        self.assertIn(kernel_probe, source)
        dispatch.write_text(
            source.replace(
                kernel_probe, f"SC_PLATFORM_KERNEL={kernel!r}"
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
            args=["--force", "--skip-harness-install", "--username", "Gate"],
            timeout=120,
        )

    def fetch_json(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=2) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    @contextlib.contextmanager
    def live_api(self, repo: Path, home: Path):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(repo / ".super-coder/api/server.py"),
                    "--port",
                    str(port),
                ],
                cwd=repo,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SC_BIND": "127.0.0.1",
                },
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 20
                while True:
                    try:
                        self.fetch_json(f"{base}/api/health")
                        break
                    except (OSError, ValueError) as exc:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            self.fail(
                                "fresh-install API did not become ready: "
                                f"{exc}\n{log.read()}"
                            )
                        time.sleep(0.05)
                yield base
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def test_installer_help_forms_are_repository_and_state_pure(self) -> None:
        cases = (
            ["-h"],
            ["--help"],
            [
                "--force",
                "--skip-harness-install",
                "--username",
                "Gate",
                "--flavor",
                "planner",
                "--help",
            ],
        )
        for args in cases:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as raw:
                repo, home = self.prepare_repo(raw)
                before_repo = self.snapshot_tree(repo)
                before_home = self.snapshot_tree(home)
                before_index = subprocess.run(
                    ["git", "status", "--porcelain=v1"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout

                result = subprocess.run(
                    [str(repo / "sc"), "install", *args],
                    cwd=repo,
                    env={
                        **os.environ,
                        "HOME": str(home),
                        "SC_PYTHON": sys.executable,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage: ./sc install [options]", result.stdout)
                self.assertIn("--skip-harness-install", result.stdout)
                self.assertNotIn("Installed ✓", result.stdout)
                self.assertEqual(self.snapshot_tree(repo), before_repo)
                self.assertEqual(self.snapshot_tree(home), before_home)
                after_index = subprocess.run(
                    ["git", "status", "--porcelain=v1"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                self.assertEqual(after_index, before_index)

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
            self.assertRegex(config["instance_id"], re.compile(r"^[0-9a-f]{32}$"))
            private_root = (
                home
                / ".local/state/subfloor/instances"
                / config["instance_id"]
            )
            self.assertTrue((private_root / "owner.json").is_file())
            self.assertFalse((private_root / "shell_db.db").exists())
            self.assertTrue((repo / ".super-coder/shell_db.db").is_file())

            reinstalled = self.run_install(repo, home)
            self.assertEqual(
                reinstalled.returncode,
                0,
                f"stdout:\n{reinstalled.stdout}\nstderr:\n{reinstalled.stderr}",
            )
            reinstalled_config = json.loads(
                (repo / ".super-coder" / "instance.json").read_text()
            )
            self.assertEqual(reinstalled_config["instance_id"], config["instance_id"])
            roots = list((home / ".local/state/subfloor/instances").iterdir())
            self.assertEqual([root.name for root in roots], [config["instance_id"]])
            self.assertFalse((private_root / "shell_db.db").exists())
            self.assertTrue((repo / ".super-coder/shell_db.db").is_file())
            self.assertIn("Installed ✓", result.stdout)
            self.assertEqual(sys.version_info[:2], (3, 14))
            self.assertIn(
                f"python    {Path(sys.executable).resolve()} · 3.14.",
                result.stdout,
            )
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

            database = repo / ".super-coder/shell_db.db"
            with sqlite3.connect(database) as con:
                self.assertEqual(
                    con.execute(
                        "SELECT username FROM users WHERE is_active=1"
                    ).fetchall(),
                    [("Gate",)],
                )
                self.assertEqual(
                    con.execute(
                        "SELECT flavor,COUNT(*) FROM shells "
                        "WHERE COALESCE(is_deleted,0)=0 GROUP BY flavor"
                    ).fetchall(),
                    [
                        ("admin", 1),
                        ("cartographer", 1),
                        ("dev", 4),
                        ("planner", 2),
                        ("reviewer", 2),
                    ],
                )

            with self.live_api(repo, home) as base:
                shells = self.fetch_json(f"{base}/api/shells")["shells"]
                self.assertEqual(len(shells), 10)
                self.assertEqual(
                    {shell["shortname"] for shell in shells},
                    {
                        "ADM1",
                        "CART1",
                        "DEV1",
                        "DEV2",
                        "DEV3",
                        "DEV4",
                        "PLN1",
                        "PLN2",
                        "REV1",
                        "REV2",
                    },
                )
                conversations = self.fetch_json(
                    f"{base}/api/conversations?open=true&limit=100"
                )
                self.assertEqual(
                    conversations,
                    {"items": [], "next_cursor": None},
                )

    def test_direct_installer_rejects_other_python_minors_before_mutation(self) -> None:
        for version in ((3, 13, 9), (3, 15, 0)):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as raw,
            ):
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
                    python_version=version,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Python 3.14.x required", result.stderr)
                self.assertIn(
                    "reports " + ".".join(map(str, version)), result.stderr
                )
                self.assertIn(
                    "recovery: install Python 3.14.x with sqlite3", result.stderr
                )
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

    def test_direct_installer_platform_gate_is_kernel_only(self) -> None:
        fixtures = {
            "ubuntu": ("Linux", "ID=ubuntu\nVERSION_ID=26.10", True),
            "fedora": ("Linux", "ID=fedora\nVERSION_ID=rawhide", True),
            "arch": ("Linux", "ID=arch\n", True),
            "cachyos": ("Linux", "ID=cachyos\nID_LIKE=arch\n", True),
            "malformed": ("Linux", 'ID="ubuntu\nVERSION_ID=26.04\n', True),
            "unknown-linux": ("Linux", "ID=notarch\nID_LIKE=notarch\n", True),
            "missing-os-release": ("Linux", None, True),
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
                result = self.run_direct_host(repo, home, kernel, release)
                (repo / ".super-coder/scripts/install.py").write_text(
                    "#!/usr/bin/env python3\nprint('0')\n"
                )
                before_repo = self.snapshot_tree(repo)
                before_home = self.snapshot_tree(home)
                shell = subprocess.run(
                    [
                        "sh",
                        str(repo / ".super-coder/scripts/dispatch.sh"),
                        "install",
                        "--harness-epoch",
                    ],
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
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "0")
                    self.assertEqual(shell.returncode, 0, shell.stderr)
                    self.assertEqual(shell.stdout.strip(), "0")
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"detected kernel: {kernel}", result.stderr)
                    self.assertIn("Create a Linux VM", result.stderr)
                    self.assertEqual(shell.returncode, 1)
                    self.assertEqual(shell.stderr, result.stderr)
                    self.assert_direct_refusal_is_pristine(
                        repo, home, before_repo, before_home
                    )

        for marker in ("WSL_DISTRO_NAME", "WSL_INTEROP"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as raw:
                repo, home = self.prepare_repo(raw)
                release = Path(raw) / "os-release"
                release.write_text("ID=unknown\n")
                result = self.run_direct_host(
                    repo, home, "Linux", release, {marker: "fixture"}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "0")

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

    def test_direct_installer_ignores_unreadable_os_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "invalid-os-release"
            release.write_bytes(
                b"ID=ubuntu\nVERSION_ID=26.04\nPADDING="
                + b"x" * 20_000
                + b"\nBROKEN=\xff\n"
            )
            result = self.run_direct_host(repo, home, "Linux", release)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0")

    def test_direct_installer_ignores_nul_os_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home = self.prepare_repo(raw)
            release = Path(raw) / "nul-os-release"
            release.write_bytes(b"ID=ubuntu\nVERSION_ID=26.04\0")
            result = self.run_direct_host(repo, home, "Linux", release)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0")

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
                failed_config = json.loads(config_path.read_text())
                self.assertRegex(
                    failed_config["instance_id"],
                    re.compile(r"^[0-9a-f]{32}$"),
                )
                self.assertNotIn("installed_at", failed_config)
                failed_id = failed_config["instance_id"]
                roots = list((home / ".local/state/subfloor/instances").iterdir())
                self.assertEqual([root.name for root in roots], [failed_id])

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
                repaired_config = json.loads(config_path.read_text())
                self.assertEqual(repaired_config["instance_id"], failed_id)
                roots = list((home / ".local/state/subfloor/instances").iterdir())
                self.assertEqual([root.name for root in roots], [failed_id])


if __name__ == "__main__":
    unittest.main(verbosity=2)
