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
        self.docker_state = Path(self._tmp.name) / "docker-state"
        self.root.mkdir()
        self.scripts.mkdir(parents=True)
        self.fakebin.mkdir()
        self.home.mkdir()
        self.docker_state.mkdir()
        shutil.copy2(ROOT / "sc", self.root / "sc")
        shutil.copy2(
            ROOT / ".super-coder" / "scripts" / "artifact_policy.py",
            self.scripts / "artifact_policy.py",
        )
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
                "SC_TEST_DOCKER_STATE": str(self.docker_state),
                "SC_TEST_PG_NAME": f"sc-pg-{self.root.name}",
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
            state_dir="$SC_TEST_DOCKER_STATE"
            if [ "$1" = info ]; then exit 0; fi
            if [ "$1" = image ] && [ "$2" = inspect ]; then
              [ "$SC_TEST_IMAGE" = present ]
              exit
            fi
            if [ "$1" = network ] && [ "$2" = inspect ]; then exit 0; fi
            if [ "$1" = network ] && [ "$2" = create ]; then exit 0; fi
            if [ "$1" = build ]; then exit 0; fi
            if [ "$1" = rm ]; then
              name="$3"
              if [ "$name" = "$SC_TEST_PG_NAME" ] &&
                 [ "${SC_TEST_PG_REMOVE_FAIL:-}" = 1 ]; then
                exit 1
              fi
              rm -f "$state_dir/$name.id"
              exit 0
            fi
            if [ "$1" = run ]; then
              shift
              name=""
              while [ "$#" -gt 0 ]; do
                if [ "$1" = --name ]; then
                  name="$2"
                  break
                fi
                shift
              done
              if [ -n "$name" ]; then
                next_file="$state_dir/next-id"
                next=0
                if [ -f "$next_file" ]; then next="$(cat "$next_file")"; fi
                next=$((next + 1))
                printf '%s\\n' "$next" > "$next_file"
                printf 'container-%s\\n' "$next" > "$state_dir/$name.id"
                echo "container-$next"
              else
                echo fake-container-id
              fi
              exit 0
            fi
            if [ "$1" = inspect ] && [ "$2" = --format ]; then
              if [ -f "$state_dir/$4.id" ]; then
                echo true
                exit 0
              fi
              exit 1
            fi
            if [ "$1" = inspect ]; then
              [ -f "$state_dir/$2.id" ]
              exit
            fi
            if [ "$1" = ps ]; then
              if [ -f "$state_dir/$SC_TEST_PG_NAME.id" ]; then
                echo "$SC_TEST_PG_NAME"
              fi
              exit 0
            fi
            if [ "$1" = exec ]; then
              [ -f "$state_dir/$2.id" ]
              exit
            fi
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

    def configure_pg(self) -> None:
        (self.engine / "instance.json").write_text('{"pg": {}}\n')

    def pg_identity(self) -> str:
        return (
            self.docker_state / f"{self.env['SC_TEST_PG_NAME']}.id"
        ).read_text().strip()


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

    def test_restart_replaces_configured_postgres_identity(self):
        self.fx.configure_pg()
        initial = self.fx.run("pg-up")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        old_identity = self.fx.pg_identity()

        result = self.fx.run("restart", "--yes", "--no-build")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.fx.pg_identity(), old_identity)
        pg_runs = [
            call
            for call in self.fx.calls()
            if f"--name {self.fx.env['SC_TEST_PG_NAME']}" in call
        ]
        self.assertEqual(len(pg_runs), 2)
        self.assertIn("  postgres: restarted", result.stdout)

    def test_restart_refuses_when_configured_postgres_removal_fails(self):
        self.fx.configure_pg()
        initial = self.fx.run("pg-up")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        old_identity = self.fx.pg_identity()
        self.fx.env["SC_TEST_PG_REMOVE_FAIL"] = "1"

        result = self.fx.run("restart", "--yes", "--no-build")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.fx.pg_identity(), old_identity)
        self.assertIn("could not verify removal", result.stderr)
        self.assertIn("run ./sc pg-down, then retry ./sc restart", result.stderr)
        self.assertIn("no replacement services were launched", result.stderr)
        self.assertNotIn("postgres: restarted", result.stdout)
        pg_runs = [
            call
            for call in self.fx.calls()
            if f"--name {self.fx.env['SC_TEST_PG_NAME']}" in call
        ]
        self.assertEqual(len(pg_runs), 1)
        self.assertFalse(
            any(
                f"--name sc-{self.fx.root.name}" in call
                for call in self.fx.calls()
            )
        )


class MakeDispatchTests(unittest.TestCase):
    def test_dos_launch_forwards_no_build(self):
        result = subprocess.run(
            ["make", "-n", "dos-l", "ARGS=--no-build"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "./sc launch --no-build")

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
