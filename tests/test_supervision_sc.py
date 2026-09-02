#!/usr/bin/env python3
"""Behavioral coverage for restricted-seat launch/restart supervision."""
from __future__ import annotations

import json
import os
import pwd
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
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))


class SupervisionFixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "restricted-fork"
        self.engine = self.root / ".super-coder"
        self.scripts = self.engine / "scripts"
        self.fakebin = Path(self._tmp.name) / "bin"
        self.home = Path(self._tmp.name) / "home"
        self.log = Path(self._tmp.name) / "calls.log"
        self.epoch_file = Path(self._tmp.name) / "harness-epoch"
        self.docker_state = Path(self._tmp.name) / "docker-state"
        self.root.mkdir()
        self.scripts.mkdir(parents=True)
        self.fakebin.mkdir()
        self.home.mkdir()
        self.docker_state.mkdir()
        shutil.copy2(ROOT / "sc", self.root / "sc")
        # cli_entry.py rides along with every script copied here: each one
        # imports it from its __main__ block (SIGPIPE hygiene, #384).
        for script in (
            "dispatch.sh",
            "artifact_policy.py",
            "callable_floor.py",
            "db_backup.py",
            "cli_entry.py",
            "docker_cache.py",
            "engine_manifest.py",
            "engine_paths.py",
            "global_pointer.py",
            "install.py",
            "instance_state.py",
            "devkit.py",
            "github_auth.py",
            "sandbox_github_auth.py",
            "runtime_flags.py",
            "sandbox_devkit.py",
        ):
            shutil.copy2(
                ROOT / ".super-coder" / "scripts" / script,
                self.scripts / script,
            )
        (self.engine / "assets").mkdir()
        shutil.copy2(
            ROOT / ".super-coder" / "assets" / "github_known_hosts",
            self.engine / "assets" / "github_known_hosts",
        )
        (self.engine / "Dockerfile").write_text("FROM scratch\n")
        (self.root / ".sc-state").mkdir()
        (self.root / ".sc-state" / "engine.ref").write_text("a" * 40 + "\n")
        (self.root / ".gitignore").write_text(
            "/.sc-state/local/\n"
            "/.super-coder/shell_db.db\n"
            "/.super-coder/shell_db.db-wal\n"
            "/.super-coder/shell_db.db-shm\n"
        )
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
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
                "SC_HARNESS_EPOCH_FILE": str(self.epoch_file),
                "SC_TEST_LOG": str(self.log),
                "SC_TEST_IMAGE": "present",
                "SC_TEST_DOCKER_STATE": str(self.docker_state),
                "SC_TEST_PG_NAME": f"sc-pg-{self.root.name}",
                "NO_COLOR": "1",
            }
        )
        self.env.pop("SC_DB_BACKUP_DIR", None)
        from sandbox_devkit import image_plan  # noqa: PLC0415

        plan = image_plan(
            self.root,
            self.engine,
            "0",
            user=pwd.getpwuid(os.getuid()).pw_name,
            uid=str(os.getuid()),
            gid=str(os.getgid()),
        )
        (self.docker_state / "image.json").write_text(json.dumps([{
            "Id": "sha256:" + "b" * 64,
            "Config": {"Labels": {
                **plan.runtime_labels,
                "sc.parent_id": "sha256:" + "a" * 64,
            }},
        }]))
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
            if [ "$1" = info ]; then
              [ "${SC_TEST_ROOTLESS:-}" != 1 ] || echo rootless
              exit 0
            fi
            if [ "$1" = image ] && [ "$2" = inspect ]; then
              [ "$SC_TEST_IMAGE" = present ] || exit 1
              case " $* " in
                *" --format "*) echo 0 ;;
                *" python:3.14-slim "*)
                  printf '[{"Id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","Config":{"Labels":{}}}]\\n' ;;
                *) cat "$state_dir/image.json" ;;
              esac
              exit 0
            fi
            if [ "$1" = network ] && [ "$2" = inspect ]; then exit 0; fi
            if [ "$1" = network ] && [ "$2" = create ]; then exit 0; fi
            if [ "$1" = build ]; then
              [ "${SC_TEST_BUILD_FAIL:-}" != 1 ] || exit 1
              labels="$state_dir/image.labels"
              : > "$labels"
              while [ "$#" -gt 0 ]; do
                if [ "$1" = --label ]; then
                  printf '%s\\n' "$2" >> "$labels"
                  shift 2
                else
                  shift
                fi
              done
              printf '[{"Id":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","Config":{"Labels":{' > "$state_dir/image.json"
              first=1
              while IFS= read -r label; do
                key="${label%%=*}"
                value="${label#*=}"
                [ "$first" -eq 1 ] || printf ',' >> "$state_dir/image.json"
                printf '"%s":"%s"' "$key" "$value" >> "$state_dir/image.json"
                first=0
              done < "$labels"
              printf '}}}]' >> "$state_dir/image.json"
              exit 0
            fi
            if [ "$1" = rm ]; then
              name="$3"
              if [ "$name" = "$SC_TEST_PG_NAME" ] &&
                 [ "${SC_TEST_PG_REMOVE_FAIL:-}" = 1 ]; then
                exit 1
              fi
              rm -f "$state_dir/$name.id"
              rm -f "$state_dir/$name.image"
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
                printf 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n' > "$state_dir/$name.image"
                echo "container-$next"
              else
                echo fake-container-id
              fi
              exit 0
            fi
            if [ "$1" = inspect ] && [ "$2" = --format ]; then
              if [ -f "$state_dir/$4.id" ]; then
                if [ "$3" = "{{.Image}}" ]; then
                  cat "$state_dir/$4.image"
                else
                  [ "${SC_TEST_CONTAINER_CRASH_LOOP:-}" != 1 ] || echo false
                  [ "${SC_TEST_CONTAINER_CRASH_LOOP:-}" = 1 ] || echo true
                fi
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
              original="$*"
              shift
              while [ "$#" -gt 0 ]; do
                case "$1" in
                  -e|--env) shift 2 ;;
                  -*) shift ;;
                  *) break ;;
                esac
              done
              name="$1"
              [ -f "$state_dir/$name.id" ] || exit 1
              case " $original " in
                *devkit.py*)
                  if [ -n "${SC_TEST_PROVISION_FAIL:-}" ]; then
                    echo provision-failed >&2
                    exit "$SC_TEST_PROVISION_FAIL"
                  fi
                  [ -z "${SC_TEST_PROVISION_OUTPUT:-}" ] ||
                    printf '%s\\n' "$SC_TEST_PROVISION_OUTPUT" ;;
              esac
              exit 0
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
            [ "${SC_TEST_CURL_FAIL:-}" != 1 ] || exit 7
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

    def configure_provision(self) -> None:
        subfloor = self.root / ".subfloor"
        subfloor.mkdir(exist_ok=True)
        hook = subfloor / "provision"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        (subfloor / "dev-kit.json").write_text(json.dumps({
            "version": 1,
            "hooks": {"deps": {"argv": ["./.subfloor/provision"]}},
            "provision": {"hook": "deps", "inputs": []},
        }))
        (self.root / ".gitignore").write_text(
            "/.sc-state/local/\n"
            "/.super-coder/shell_db.db\n"
            "/.super-coder/shell_db.db-wal\n"
            "/.super-coder/shell_db.db-shm\n"
        )
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        subprocess.run(("git", "-C", str(self.root), "add", "."), check=True)
        subprocess.run(
            (
                "git", "-C", str(self.root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm",
                "provision fixture",
            ),
            check=True,
        )

    def bind_private_state(self) -> Path:
        from instance_state import resolve

        instance_id = "f" * 32
        (self.engine / "instance.json").write_text(
            json.dumps({"instance_id": instance_id}) + "\n"
        )
        state = resolve(
            instance_config=self.engine / "instance.json",
            environ=self.env,
        )
        for path in self.engine.glob("shell_db.db*"):
            path.unlink()
        return state.root

    def pg_identity(self) -> str:
        return (
            self.docker_state / f"{self.env['SC_TEST_PG_NAME']}.id"
        ).read_text().strip()

    def tracked_status(self) -> str:
        return subprocess.run(
            (
                "git", "-C", str(self.root), "status", "--short",
                "--untracked-files=no",
            ),
            check=True,
            text=True,
            capture_output=True,
        ).stdout


class RestrictedLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = SupervisionFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_launch_no_build_reuses_existing_image_without_buildx(self):
        result = self.fx.run("launch", "--no-build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provision state: absent", result.stdout)
        self.assertNotIn("container-1", result.stdout)
        calls = self.fx.calls()
        image_inspects = [
            line for line in calls if line.startswith("docker image inspect ")
        ]
        self.assertEqual(len(image_inspects), 4)
        self.assertEqual(
            sum("python:3.14-slim" in line for line in image_inspects), 1
        )
        self.assertTrue(any("super-coder-base:" in line for line in image_inspects))
        sandbox_run = next(
            line
            for line in calls
            if line.startswith("docker run -d")
            and f"--name sc-{self.fx.root.name}" in line
        )
        self.assertIn(" --init ", sandbox_run)
        self.assertFalse(any(line.startswith("docker build ") for line in calls))

    def test_launch_mounts_only_the_bound_private_instance_state(self):
        self.fx.env["XDG_STATE_HOME"] = str(Path(self.fx._tmp.name) / "xdg-state")
        state_root = self.fx.bind_private_state()

        result = self.fx.run("launch", "--no-build")

        self.assertEqual(result.returncode, 0, result.stderr)
        sandbox_run = next(
            line
            for line in self.fx.calls()
            if line.startswith("docker run -d")
            and f"--name sc-{self.fx.root.name}" in line
        )
        state_target = (
            self.fx.home / ".local" / "state" / "subfloor" / "instances"
            / state_root.name
        )
        self.assertIn(f" -v {state_root}:{state_target} ", sandbox_run)
        self.assertNotIn(
            f" -v {state_root.parent}:{state_root.parent} ", sandbox_run
        )
        self.assertNotIn(" --tmpfs ", sandbox_run)

    def test_rootless_launch_owns_private_namespace_and_mounts_only_bound_leaf(
        self,
    ):
        self.fx.env["XDG_STATE_HOME"] = str(Path(self.fx._tmp.name) / "xdg-state")
        self.fx.env["SC_TEST_ROOTLESS"] = "1"
        state_root = self.fx.bind_private_state()

        result = self.fx.run("launch", "--no-build")

        self.assertEqual(result.returncode, 0, result.stderr)
        sandbox_run = next(
            line
            for line in self.fx.calls()
            if line.startswith("docker run -d")
            and f"--name sc-{self.fx.root.name}" in line
        )
        namespace = self.fx.home / ".local" / "state"
        for target in (
            namespace,
            namespace / "subfloor",
            namespace / "subfloor" / "instances",
        ):
            self.assertIn(
                f" --tmpfs {target}:uid=0,gid=0,mode=0700 ", sandbox_run
            )
        state_target = namespace / "subfloor" / "instances" / state_root.name
        self.assertIn(f" -v {state_root}:{state_target} ", sandbox_run)
        self.assertNotIn(
            f" -v {state_root.parent}:{state_root.parent} ", sandbox_run
        )
        self.assertIn(" --user 0:0 ", sandbox_run)

    def test_launch_fails_closed_when_review_api_never_becomes_healthy(self):
        self.fx.env["SC_TEST_CURL_FAIL"] = "1"

        result = self.fx.run("launch", "--no-build")

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("sandbox up", result.stdout)
        self.assertIn("review API did not become healthy", result.stderr)
        self.assertIn("retained", result.stderr)
        container = f"sc-{self.fx.root.name}"
        self.assertTrue((self.fx.docker_state / f"{container}.id").exists())

    def test_launch_fails_closed_and_retains_a_crash_looping_container(self):
        self.fx.env["SC_TEST_CONTAINER_CRASH_LOOP"] = "1"

        result = self.fx.run("launch", "--no-build")

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("sandbox up", result.stdout)
        self.assertIn("review API did not become healthy", result.stderr)
        self.assertIn("retained", result.stderr)
        container = f"sc-{self.fx.root.name}"
        self.assertTrue((self.fx.docker_state / f"{container}.id").exists())
        self.assertFalse(any(call.startswith("curl ") for call in self.fx.calls()))

    def test_docker_cache_gc_is_explicit_and_age_bounded_by_default(self):
        result = self.fx.run("docker-cache-gc")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host-global cache older than 168h", result.stdout)
        self.assertIn(
            "docker builder prune --all --force --filter until=168h",
            self.fx.calls(),
        )

    def test_docker_cache_gc_all_drops_the_age_filter(self):
        result = self.fx.run("docker-cache-gc", "--all")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all unused host-global cache", result.stdout)
        self.assertIn("docker builder prune --all --force", self.fx.calls())
        self.assertFalse(any("until=" in line for line in self.fx.calls()))

    def test_successful_provision_reports_hook_output_and_ready_state(self):
        self.fx.configure_provision()
        self.assertEqual(self.fx.tracked_status(), "")
        self.fx.env["SC_TEST_PROVISION_OUTPUT"] = "dependencies installed"

        result = self.fx.run("launch", "--no-build")

        self.assertEqual(
            result.returncode,
            0,
            f"{result.stderr}\ntracked status:\n{self.fx.tracked_status()}",
        )
        self.assertIn("dependencies installed", result.stdout)
        self.assertIn("provision state: ready", result.stdout)
        self.assertNotIn("container-1", result.stdout)

    def test_launch_no_build_missing_image_refuses_before_runtime_change(self):
        self.fx.env["SC_TEST_IMAGE"] = "missing"
        result = self.fx.run("launch", "--no-build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Run ./sc build", result.stderr)
        calls = self.fx.calls()
        self.assertFalse(any(line.startswith("docker rm ") for line in calls))
        self.assertFalse(any(line.startswith("docker run ") for line in calls))
        self.assertFalse(any(line.startswith("docker build ") for line in calls))

    def test_failed_provision_retains_container_and_prints_retry_and_repair(self):
        self.fx.configure_provision()
        self.fx.env["SC_TEST_PROVISION_FAIL"] = "23"

        result = self.fx.run("launch", "--no-build")

        self.assertEqual(result.returncode, 23)
        self.assertIn("dev-kit state: failed", result.stderr)
        self.assertIn("retained sandbox", result.stderr)
        self.assertIn("./sc launch --no-build", result.stderr)
        self.assertIn("./sc enter --devkit-repair", result.stderr)
        self.assertTrue(
            (self.fx.docker_state / f"sc-{self.fx.root.name}.id").exists()
        )
        artifact = self.fx.root / ".sc-state" / "local" / "dev-kit"
        self.assertEqual(len(list(artifact.glob("*/attempts/*.json"))), 1)
        self.assertFalse(list(artifact.glob("*/ready.json")))

    def test_normal_entry_blocks_stale_provision_but_repair_bypasses_without_claim(self):
        self.fx.configure_provision()
        container = f"sc-{self.fx.root.name}"
        (self.fx.docker_state / f"{container}.id").write_text("container-1\n")

        normal = self.fx.run("enter")

        self.assertEqual(normal.returncode, 1)
        self.assertIn("dev-kit state: stale", normal.stderr)
        self.assertIn("normal entry blocked", normal.stderr)
        self.assertFalse(
            [line for line in self.fx.calls() if line.startswith("docker exec ")]
        )
        repair = self.fx.run("enter", "--devkit-repair")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertIn("dev-kit state: repair", repair.stderr)
        self.assertIn("no readiness claim", repair.stderr)
        self.assertIn(
            f"docker exec -it -e SC_DEVKIT_REPAIR=1 {container} ./sc boot",
            self.fx.calls(),
        )

    def test_restart_missing_image_does_not_backup_or_stop(self):
        self.fx.env["SC_TEST_IMAGE"] = "missing"
        result = self.fx.run("restart", "--yes", "--no-build")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.fx.home / "db_backups").exists())
        calls = self.fx.calls()
        self.assertFalse(any(line.startswith("docker rm ") for line in calls))

    def test_restart_refreshes_harnesses_before_build_and_teardown(self):
        result = self.fx.run("restart", "--yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        epoch = self.fx.epoch_file.read_text().strip()
        self.assertRegex(epoch, r"\A\d{8}T\d{6}\.\d{6}Z\Z")
        calls = self.fx.calls()
        build_at = next(
            i for i, line in enumerate(calls)
            if line.startswith("docker build ")
        )
        down_at = next(
            i for i, line in enumerate(calls)
            if line.startswith("docker rm -f ")
        )
        self.assertLess(build_at, down_at)
        self.assertIn(f"SC_HARNESS_EPOCH={epoch}", calls[build_at])
        self.assertIn("refresh harnesses for restart", result.stdout)

    def test_restart_refresh_failure_keeps_running_sandbox_intact(self):
        self.fx.env["SC_TEST_BUILD_FAIL"] = "1"

        result = self.fx.run("restart", "--yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.fx.epoch_file.exists())
        self.assertFalse((self.fx.home / "db_backups").exists())
        self.assertFalse(
            any(line.startswith("docker rm ") for line in self.fx.calls())
        )

    def test_restart_epoch_write_failure_prevents_build_and_teardown(self):
        self.fx.epoch_file.mkdir()

        result = self.fx.run("restart", "--yes")

        self.assertNotEqual(result.returncode, 0)
        calls = self.fx.calls()
        self.assertFalse(any(line.startswith("docker build ") for line in calls))
        self.assertFalse(any(line.startswith("docker rm ") for line in calls))

    def test_restart_no_build_neither_rolls_nor_builds(self):
        result = self.fx.run("restart", "--yes", "--no-build")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.fx.epoch_file.exists())
        self.assertFalse(
            any(line.startswith("docker build ") for line in self.fx.calls())
        )

    def test_restart_no_writable_backup_destination_refuses_before_down(self):
        bad_home = Path(self.fx._tmp.name) / "home-file"
        bad_override = Path(self.fx._tmp.name) / "override-file"
        bad_home.write_text("not a directory\n")
        bad_override.write_text("not a directory\n")
        (self.fx.root / ".sc-state" / "db_backups").write_text(
            "not a directory\n"
        )
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
        self.assertNotIn("watch-daemon", result.stdout)

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

    def test_broker_up_clears_stale_run_artifacts_and_starts(self):
        self.fx.env["SC_TEST_CONFIGURED"] = "ts"
        run_dir = self.fx.engine / "run"
        run_dir.mkdir()
        # Stale leftovers from a dead broker; the socket path the engine
        # resolves must point inside run/ so preflight clears it.
        self.fx.env["SC_TEST_TS_SOCK"] = str(run_dir / "ts-broker.sock")
        for name in ("ts-broker.pid", "ts-broker.log", "ts-broker.sock"):
            (run_dir / name).write_text("stale\n")

        result = self.fx.run("ts-broker-up")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("→ ts-broker up (pid ", result.stdout)
        self.assertNotEqual((run_dir / "ts-broker.pid").read_text(), "stale\n")
        self.assertFalse((run_dir / "ts-broker.sock").exists())

    def test_broker_up_refuses_unwritable_run_dir_with_remediation(self):
        self.fx.env["SC_TEST_CONFIGURED"] = "ts"
        run_dir = self.fx.engine / "run"
        run_dir.mkdir()
        (run_dir / "ts-broker.log").write_text("stale\n")
        # A sudo restart or container-mapped write leaves run/ unreachable for
        # the invoking user; up must fail fast and say how to fix it instead
        # of printing "up" for a broker that never started.
        run_dir.chmod(0o555)
        try:
            result = self.fx.run("ts-broker-up")
        finally:
            # Restore writability before the fixture's rmtree cleanup runs.
            run_dir.chmod(0o755)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("→ ts-broker up", result.stdout)
        self.assertIn("✗ ts-broker:", result.stderr)
        self.assertIn("sudo chown", result.stderr)
        self.assertFalse((run_dir / "ts-broker.pid").exists())

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
    @staticmethod
    def make_env():
        # The suite itself may be launched through `make dos-test`. Inheriting
        # MAKELEVEL makes this direct probe look recursive and adds GNU Make's
        # Entering/Leaving directory wrappers to otherwise exact output.
        return {key: value for key, value in os.environ.items()
                if key != "MAKELEVEL"}

    def test_dos_launch_forwards_no_build(self):
        result = subprocess.run(
            ["make", "-n", "dos-l", "ARGS=--no-build"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=self.make_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # This install routes lifecycle targets through the host adapter
        # (Makefile SC override), not bare ./sc.
        self.assertEqual(
            result.stdout.strip(), "sh scripts_sc/host_sc.sh launch --no-build")

    def test_dos_restart_forwards_yes_and_no_build(self):
        result = subprocess.run(
            ["make", "-n", "dos-r", "ARGS=--yes --no-build"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=self.make_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "sh scripts_sc/host_sc.sh restart --yes --no-build")


if __name__ == "__main__":
    unittest.main()
