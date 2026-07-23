#!/usr/bin/env python3
"""Behavioral coverage for restricted-seat launch/restart supervision."""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SupervisionFixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "restricted-fork"
        self.engine = self.root / ".super-coder"
        self.scripts = self.engine / "scripts"
        self.fakebin = Path(self._tmp.name) / "bin"
        self.home = Path(self._tmp.name) / "home"
        self.log = Path(self._tmp.name) / "calls.log"
        self.root.mkdir()
        self.scripts.mkdir(parents=True)
        self.fakebin.mkdir()
        self.home.mkdir()
        shutil.copy2(ROOT / "sc", self.root / "sc")
        shutil.copy2(
            ROOT / ".super-coder" / "scripts" / "db_backup.py",
            self.scripts / "db_backup.py",
        )
        (self.engine / "Dockerfile").write_text("FROM scratch\n")
        self._write_scripts()
        self._write_fake_commands()
        for directory in (
            ".claude",
            ".config/opencode",
            ".local/share/opencode",
            ".codex",
            ".vibe",
            ".kimi-code",
        ):
            (self.home / directory).mkdir(parents=True)
        (self.home / ".claude.json").write_text("{}\n")
        with sqlite3.connect(self.engine / "shell_db.db") as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE state (value TEXT)")
            con.execute("INSERT INTO state VALUES ('durable')")
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fakebin}:{self.env['PATH']}",
                "SC_PYTHON": sys.executable,
                "SC_TEST_LOG": str(self.log),
                "SC_TEST_IMAGE": "present",
                "NO_COLOR": "1",
            }
        )
        self._sockets: list[socket.socket] = []

    def close(self) -> None:
        for sock in self._sockets:
            sock.close()
        self._tmp.cleanup()

    def _write_scripts(self) -> None:
        (self.scripts / "ports.py").write_text(
            textwrap.dedent(
                """\
                import sys
                if sys.argv[1] == "port":
                    print(18800)
                elif sys.argv[1] == "devport":
                    print(15173)
                elif sys.argv[1] == "ensure":
                    pass
                """
            )
        )
        broker = textwrap.dedent(
            """\
            import os
            import sys
            from pathlib import Path
            name = Path(sys.argv[0]).stem
            command = sys.argv[1]
            configured = set(filter(None, os.environ.get(
                "SC_TEST_CONFIGURED", "").split(",")))
            if command == "configured":
                raise SystemExit(0 if name in configured else 1)
            if command == "sock":
                print(os.environ.get(f"SC_TEST_{name.upper()}_SOCK",
                                     f"/absent/{name}.sock"))
            """
        )
        for name in ("vm", "ts", "pm2", "dbq"):
            (self.scripts / f"{name}.py").write_text(broker)

    def _write_fake_commands(self) -> None:
        self._write_executable(
            "docker",
            """\
            #!/bin/sh
            printf 'docker' >> "$SC_TEST_LOG"
            printf ' %s' "$@" >> "$SC_TEST_LOG"
            printf '\\n' >> "$SC_TEST_LOG"
            if [ "$1" = info ]; then exit 0; fi
            if [ "$1" = image ] && [ "$2" = inspect ]; then
              [ "$SC_TEST_IMAGE" = present ]
              exit
            fi
            if [ "$1" = network ] && [ "$2" = inspect ]; then exit 0; fi
            if [ "$1" = network ] && [ "$2" = create ]; then exit 0; fi
            if [ "$1" = build ]; then exit 0; fi
            if [ "$1" = rm ]; then exit 0; fi
            if [ "$1" = run ]; then echo fake-container-id; exit 0; fi
            if [ "$1" = inspect ] && [ "$2" = --format ]; then
              echo true
              exit 0
            fi
            if [ "$1" = inspect ]; then exit 1; fi
            if [ "$1" = exec ]; then exit 0; fi
            exit 0
            """,
        )
        self._write_executable(
            "curl",
            """\
            #!/bin/sh
            printf 'curl' >> "$SC_TEST_LOG"
            printf ' %s' "$@" >> "$SC_TEST_LOG"
            printf '\\n' >> "$SC_TEST_LOG"
            printf '{"ok": true}\\n'
            """,
        )
        self._write_executable(
            "gh",
            """\
            #!/bin/sh
            if [ "$1" = auth ] && [ "$2" = token ]; then echo test-token; fi
            """,
        )
        self._write_executable(
            "systemctl",
            """\
            #!/bin/sh
            printf 'systemctl' >> "$SC_TEST_LOG"
            printf ' %s' "$@" >> "$SC_TEST_LOG"
            printf '\\n' >> "$SC_TEST_LOG"
            if [ "$2" = show ]; then
              case ",$SC_TEST_SYSTEMD_UNITS," in
                *,"$3",*) echo loaded ;;
                *) echo not-found ;;
              esac
              exit 0
            fi
            if [ "$2" = is-active ]; then exit 1; fi
            if [ "$2" = restart ] && [ "$3" = "$SC_TEST_SYSTEMD_FAIL" ]; then
              exit 1
            fi
            exit 0
            """,
        )

    def _write_executable(self, name: str, body: str) -> None:
        path = self.fakebin / name
        path.write_text(textwrap.dedent(body))
        path.chmod(0o755)

    def add_socket(self, name: str) -> Path:
        path = Path(self._tmp.name) / f"{name}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        self._sockets.append(sock)
        self.env[f"SC_TEST_{name.upper()}_SOCK"] = str(path)
        return path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "sc"), *args],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


class RestrictedLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = SupervisionFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_launch_no_build_reuses_existing_image_without_buildx(self):
        result = self.fx.run("launch", "--no-build")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.fx.calls()
        self.assertIn("docker image inspect super-coder-sandbox", calls)
        self.assertTrue(any(line.startswith("docker run -d") for line in calls))
        self.assertFalse(any(line.startswith("docker build ") for line in calls))

    def test_launch_no_build_missing_image_refuses_before_runtime_change(self):
        self.fx.env["SC_TEST_IMAGE"] = "missing"
        result = self.fx.run("launch", "--no-build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Run ./sc build", result.stderr)
        calls = self.fx.calls()
        self.assertFalse(any(line.startswith("docker rm ") for line in calls))
        self.assertFalse(any(line.startswith("docker run ") for line in calls))
        self.assertFalse(any(line.startswith("docker build ") for line in calls))

    def test_restart_missing_image_does_not_backup_or_stop(self):
        self.fx.env["SC_TEST_IMAGE"] = "missing"
        result = self.fx.run("restart", "--yes", "--no-build")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.fx.home / "db_backups").exists())
        calls = self.fx.calls()
        self.assertFalse(any(line.startswith("docker rm ") for line in calls))

    def test_restart_no_writable_backup_destination_refuses_before_down(self):
        bad_home = Path(self.fx._tmp.name) / "home-file"
        bad_override = Path(self.fx._tmp.name) / "override-file"
        bad_home.write_text("not a directory\n")
        bad_override.write_text("not a directory\n")
        (self.fx.root / ".sc-state").write_text("not a directory\n")
        self.fx.env["HOME"] = str(bad_home)
        self.fx.env["SC_DB_BACKUP_DIR"] = str(bad_override)

        result = self.fx.run("restart", "--yes", "--no-build")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Set SC_DB_BACKUP_DIR", result.stderr)
        self.assertFalse(
            any(line.startswith("docker rm ") for line in self.fx.calls())
        )

    def test_restart_bounces_systemd_broker_and_reports_full_inventory(self):
        self.fx.env["SC_TEST_CONFIGURED"] = "vm,ts,pm2,dbq"
        units = {
            name: f"sc-{name}-broker-{self.fx.root.name}.service"
            for name in ("vm", "ts", "pm2", "db")
        }
        self.fx.env["SC_TEST_SYSTEMD_UNITS"] = ",".join(units.values())
        for name in ("vm", "ts", "pm2", "dbq"):
            self.fx.add_socket(name)

        result = self.fx.run("restart", "--no-build", "--yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backup_files = list(
            (self.fx.home / "db_backups" / self.fx.root.name).glob(
                "shell_db.prerestart.*.db"
            )
        )
        self.assertEqual(len(backup_files), 1)
        with sqlite3.connect(backup_files[0]) as con:
            self.assertEqual(
                con.execute("SELECT value FROM state").fetchall(),
                [("durable",)],
            )
        calls = self.fx.calls()
        for unit in units.values():
            self.assertIn(f"systemctl --user restart {unit}", calls)
        self.assertFalse(any(line.startswith("docker build ") for line in calls))
        self.assertIn("  sandbox: restarted", result.stdout)
        self.assertIn("  vm-broker: restarted (systemd)", result.stdout)
        self.assertIn("  ts-broker: restarted (systemd)", result.stdout)
        self.assertIn("  pm2-broker: restarted (systemd)", result.stdout)
        self.assertIn("  db-broker: restarted (systemd)", result.stdout)
        self.assertIn("  postgres: skipped (unconfigured)", result.stdout)
        self.assertIn(
            "  legacy-watch-daemon: skipped (retired; confirmed stopped)",
            result.stdout,
        )

    def test_restart_aggregates_health_failure_and_returns_nonzero(self):
        self.fx.env["SC_TEST_CONFIGURED"] = "vm"
        unit = f"sc-vm-broker-{self.fx.root.name}.service"
        self.fx.env["SC_TEST_SYSTEMD_UNITS"] = unit
        self.fx.env["SC_TEST_SYSTEMD_FAIL"] = unit
        self.fx.add_socket("vm")

        result = self.fx.run("restart", "--yes", "--no-build")

        self.assertEqual(result.returncode, 1)
        self.assertIn("  sandbox: restarted", result.stdout)
        self.assertIn("  vm-broker: failed (systemd restart)", result.stdout)
        self.assertIn("  postgres: skipped (unconfigured)", result.stdout)


class MakeDispatchTests(unittest.TestCase):
    def test_dos_restart_forwards_yes_and_no_build(self):
        result = subprocess.run(
            ["make", "-n", "dos-r", "ARGS=--yes --no-build"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "./sc restart --yes --no-build")


if __name__ == "__main__":
    unittest.main()
