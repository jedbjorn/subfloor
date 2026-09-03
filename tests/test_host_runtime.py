#!/usr/bin/env python3
"""Behavioral coverage for the optional host runtime (instance.json `runtime`).

A `host` install runs the review server as a supervised host process and
boots shells on the host; the lifecycle verbs must never touch docker. The
dispatcher fixture here is deliberately small: the real `sc` bootstrap and
`dispatch.sh` over a fake engine whose `api/server.py` is a genuine listening
HTTP server, so `launch`/`down`/`restart` are proven against a live port and a
live pid, not against a stubbed curl.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import install as install_mod  # noqa: E402
import runtime as runtime_mod  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HostRuntimeFixture:
    """One fake fork with a host runtime selected, driven through ./sc."""

    def __init__(self, *, runtime: str | None = "host") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "hostfork"
        self.engine = self.root / ".super-coder"
        self.scripts = self.engine / "scripts"
        self.api = self.engine / "api"
        self.fakebin = Path(self._tmp.name) / "bin"
        self.home = Path(self._tmp.name) / "home"
        self.log = Path(self._tmp.name) / "calls.log"
        self.run_argv = Path(self._tmp.name) / "run-argv.json"
        self.port = free_port()
        for directory in (self.scripts, self.api, self.fakebin, self.home):
            directory.mkdir(parents=True)
        shutil.copy2(ROOT / "sc", self.root / "sc")
        for script in (
            "dispatch.sh",
            "runtime.py",
            "cli_entry.py",
            "instance_state.py",
        ):
            shutil.copy2(ENGINE / "scripts" / script, self.scripts / script)
        self._write_fake_engine()
        self._write_fake_commands()
        config: dict[str, object] = {
            "repo": "hostfork",
            "port": self.port,
            "dev_port": self.port + 1,
        }
        if runtime is not None:
            config["runtime"] = runtime
        (self.engine / "instance.json").write_text(json.dumps(config, indent=2) + "\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fakebin}:{self.env['PATH']}",
                "SC_PYTHON": sys.executable,
                "SC_TEST_LOG": str(self.log),
                "SC_TEST_PORT": str(self.port),
                "SC_TEST_RUN_ARGV": str(self.run_argv),
                "NO_COLOR": "1",
            }
        )
        for key in ("SC_SANDBOX", "SC_DEV_PORT", "SC_CALLER_ROOT"):
            self.env.pop(key, None)

    # -- fixture engine -------------------------------------------------------
    def _write_fake_engine(self) -> None:
        (self.api / "server.py").write_text(textwrap.dedent(
            """\
            import http.server
            import socketserver
            import sys

            port = int(sys.argv[sys.argv.index("--port") + 1])


            class Health(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b'{"ok": true}'
                    self.send_response(200 if self.path == "/api/health" else 404)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass


            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("127.0.0.1", port), Health) as server:
                server.serve_forever()
            """
        ))
        (self.scripts / "ports.py").write_text(textwrap.dedent(
            """\
            import json
            import os
            import sys

            port = int(os.environ["SC_TEST_PORT"])
            mode = sys.argv[1] if len(sys.argv) > 1 else "show"
            if mode == "port":
                print(port)
            elif mode == "devport":
                print(port + 1)
            elif mode == "show":
                print(json.dumps({"repo": "hostfork", "port": port, "dev_port": port + 1}))
            """
        ))
        (self.scripts / "db_backup.py").write_text(textwrap.dedent(
            """\
            import os
            import sys

            with open(os.environ["SC_TEST_LOG"], "a") as log:
                log.write("db_backup " + " ".join(sys.argv[1:]) + "\\n")
            if sys.argv[1] == "select":
                print(os.path.join(os.environ["HOME"], "backups"))
            """
        ))
        (self.scripts / "run.py").write_text(textwrap.dedent(
            """\
            import json
            import os
            import sys

            with open(os.environ["SC_TEST_RUN_ARGV"], "w") as out:
                json.dump({
                    "argv": sys.argv[1:],
                    "dev_port": os.environ.get("SC_DEV_PORT"),
                    "sandbox": os.environ.get("SC_SANDBOX"),
                    "cwd": os.getcwd(),
                }, out)
            """
        ))
        (self.scripts / "install.py").write_text(textwrap.dedent(
            """\
            import os
            import sys

            with open(os.environ["SC_TEST_LOG"], "a") as log:
                log.write("install.py " + " ".join(sys.argv[1:]) + "\\n")
            """
        ))
        broker = textwrap.dedent(
            """\
            import sys
            if sys.argv[1] == "configured":
                raise SystemExit(1)
            if sys.argv[1] == "sock":
                print("/absent.sock")
            """
        )
        for name in ("vm", "ts", "pm2", "dbq"):
            (self.scripts / f"{name}.py").write_text(broker)

    def _write_fake_commands(self) -> None:
        # docker exists but is unusable: the host path must never reach it,
        # and the sandbox path must fail on it visibly.
        self._write_executable(
            "docker",
            """\
            #!/bin/sh
            printf 'docker %s\\n' "$*" >> "$SC_TEST_LOG"
            exit 1
            """,
        )

    def _write_executable(self, name: str, body: str) -> None:
        path = self.fakebin / name
        path.write_text(textwrap.dedent(body))
        path.chmod(0o755)

    # -- driving --------------------------------------------------------------
    def run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "sc"), *args],
            cwd=self.root,
            env=self.env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def pidfile(self) -> Path:
        return self.engine / "run" / "server.pid"

    def server_pid(self) -> int | None:
        try:
            return int(self.pidfile().read_text().split()[0])
        except (OSError, IndexError, ValueError):
            return None

    def health(self) -> bool:
        probe = subprocess.run(
            ["curl", "-fsS", f"http://127.0.0.1:{self.port}/api/health"],
            capture_output=True, text=True, check=False,
        )
        return probe.returncode == 0

    def close(self) -> None:
        pid = self.server_pid()
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        self._tmp.cleanup()


class HostRuntimeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = HostRuntimeFixture()
        self.addCleanup(self.fx.close)

    def assert_no_docker(self) -> None:
        self.assertFalse(
            [line for line in self.fx.calls() if line.startswith("docker")],
            "the host runtime reached docker",
        )

    def test_launch_starts_a_supervised_host_server_without_docker(self):
        result = self.fx.run("launch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host review server up", result.stdout)
        self.assertIn(f"http://127.0.0.1:{self.fx.port}", result.stdout)
        pid = self.fx.server_pid()
        self.assertIsNotNone(pid)
        os.kill(pid, 0)  # alive
        self.assertTrue(self.fx.health())
        self.assert_no_docker()

        again = self.fx.run("launch", "--no-build")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already running", again.stdout)
        self.assertEqual(self.fx.server_pid(), pid)

        down = self.fx.run("down")
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertIn("host review server stopped", down.stdout)
        self.assertFalse(self.fx.pidfile().exists())
        self.assertFalse(self.fx.health())
        self.assert_no_docker()

    def test_down_without_a_server_is_a_calm_no_op(self):
        result = self.fx.run("down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host review server not running", result.stdout)
        self.assert_no_docker()

    def test_enter_requires_the_host_server_then_boots_through_run_py(self):
        blocked = self.fx.run("enter-cc")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("./sc launch first", blocked.stderr)
        self.assertFalse(self.fx.run_argv.exists())

        self.assertEqual(self.fx.run("launch").returncode, 0)
        entered = self.fx.run("enter-cc", "--harness", "opencode")
        self.assertEqual(entered.returncode, 0, entered.stderr)
        self.assertIn("Review GUI", entered.stdout)
        booted = json.loads(self.fx.run_argv.read_text())
        self.assertEqual(booted["argv"], ["cc", "--harness", "opencode"])
        self.assertEqual(booted["dev_port"], str(self.fx.port + 1))
        self.assertIsNone(booted["sandbox"])
        self.assertEqual(Path(booted["cwd"]).resolve(), self.fx.root.resolve())

        picker = self.fx.run("enter")
        self.assertEqual(picker.returncode, 0, picker.stderr)
        self.assertEqual(json.loads(self.fx.run_argv.read_text())["argv"], [])
        self.assert_no_docker()

    def test_devkit_repair_is_refused_under_the_host_runtime(self):
        result = self.fx.run("enter", "--devkit-repair")
        self.assertEqual(result.returncode, 2)
        self.assertIn("runtime is host", result.stderr)

    def test_restart_backs_up_stops_and_relaunches(self):
        self.assertEqual(self.fx.run("launch").returncode, 0)
        first = self.fx.server_pid()

        aborted = self.fx.run("restart", stdin="no\n")
        self.assertEqual(aborted.returncode, 1)
        self.assertIn("restart aborted", aborted.stdout)
        self.assertEqual(self.fx.server_pid(), first)

        result = self.fx.run("restart", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("host server: restarted", result.stdout)
        second = self.fx.server_pid()
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertTrue(self.fx.health())
        backups = [line for line in self.fx.calls() if line.startswith("db_backup backup")]
        self.assertEqual(len(backups), 1)
        self.assertIn("prerestart", backups[0])
        self.assert_no_docker()

    def test_build_is_refused_and_logs_needs_a_launch(self):
        build = self.fx.run("build")
        self.assertEqual(build.returncode, 2)
        self.assertIn("no sandbox image", build.stderr)
        logs = self.fx.run("logs")
        self.assertEqual(logs.returncode, 1)
        self.assertIn("./sc launch first", logs.stderr)
        self.assert_no_docker()

    def test_update_harnesses_runs_the_host_installers(self):
        result = self.fx.run("update-harnesses")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runtime host", result.stdout)
        self.assertIn("install.py --update-harnesses", self.fx.calls())
        self.assert_no_docker()

    def test_runtime_verb_shows_and_switches_the_selection(self):
        shown = self.fx.run("runtime")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("runtime: host", shown.stdout)

        bad = self.fx.run("runtime", "cloud")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("unsupported runtime", bad.stderr)

        switched = self.fx.run("runtime", "sandbox")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertIn("host → sandbox", switched.stdout)
        config = json.loads((self.fx.engine / "instance.json").read_text())
        self.assertEqual(config["runtime"], "sandbox")
        self.assertEqual(config["port"], self.fx.port)  # other keys retained

        # The switch is the only thing that moved: the next lifecycle verb is
        # the docker path, which the unusable fake docker refuses visibly.
        launch = self.fx.run("launch")
        self.assertEqual(launch.returncode, 1)
        self.assertIn("docker daemon not reachable", launch.stderr)


class SandboxDefaultTest(unittest.TestCase):
    def test_an_install_without_the_key_keeps_the_docker_lifecycle(self):
        fx = HostRuntimeFixture(runtime=None)
        self.addCleanup(fx.close)
        self.assertEqual(fx.run("runtime", "get").stdout.strip(), "sandbox")
        launch = fx.run("launch")
        self.assertEqual(launch.returncode, 1)
        self.assertIn("docker daemon not reachable", launch.stderr)
        self.assertTrue(any(line.startswith("docker info") for line in fx.calls()))
        self.assertFalse(fx.pidfile().exists())


class RuntimeModuleTest(unittest.TestCase):
    def test_read_mode_defaults_to_sandbox_for_absent_or_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "instance.json"
            self.assertEqual(runtime_mod.read_mode(config), "sandbox")
            config.write_text("{not json")
            self.assertEqual(runtime_mod.read_mode(config), "sandbox")
            config.write_text(json.dumps({"runtime": "cloud"}))
            self.assertEqual(runtime_mod.read_mode(config), "sandbox")
            config.write_text(json.dumps({"runtime": "Host"}))
            self.assertEqual(runtime_mod.read_mode(config), "host")
            self.assertTrue(runtime_mod.is_host(config))

    def test_validate_rejects_unknown_modes(self):
        self.assertEqual(runtime_mod.validate(" HOST "), "host")
        with self.assertRaises(runtime_mod.RuntimeError_):
            runtime_mod.validate("vm")


class InstallRuntimeFlagTest(unittest.TestCase):
    def test_runtime_flag_is_split_from_the_fork_arguments(self):
        mode, rest = install_mod.split_runtime_flag(
            ["--runtime", "host", "--username", "jed", "--force"]
        )
        self.assertEqual(mode, "host")
        self.assertEqual(rest, ["--username", "jed", "--force"])
        mode, rest = install_mod.split_runtime_flag(["--runtime=sandbox"])
        self.assertEqual((mode, rest), ("sandbox", []))
        self.assertEqual(install_mod.split_runtime_flag(["--force"]), (None, ["--force"]))

    def test_runtime_flag_without_a_value_fails_before_install_runs(self):
        with self.assertRaisesRegex(SystemExit, "needs a value"):
            install_mod.split_runtime_flag(["--runtime"])

    def test_install_help_documents_the_flag(self):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            install_mod.print_help()
        self.assertIn("--runtime MODE", out.getvalue())

    def test_doctor_reports_the_host_runtime_instead_of_docker(self):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with mock.patch.object(install_mod, "_host_runtime_selected", return_value=True), \
                mock.patch.object(install_mod, "report_docker") as docker, \
                mock.patch.object(install_mod, "report_logins"), \
                mock.patch.object(install_mod, "report_host_runtime"), \
                redirect_stdout(out):
            self.assertEqual(install_mod.main(["--check-docker"]), 0)
        docker.assert_not_called()
        self.assertIn("runtime   ✓ host", out.getvalue())


class UpdateHostCutoverTest(unittest.TestCase):
    """The updater stops and relaunches the host runtime around DB maintenance."""

    def setUp(self) -> None:
        import update  # noqa: PLC0415 — engine scripts are path-loaded

        self.update = update
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pidfile = Path(self.tmp.name) / "server.pid"
        patcher = mock.patch.object(update, "HOST_SERVER_PID", self.pidfile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spawn_fake_server(self) -> subprocess.Popen:
        # The argv carries `api/server.py` so the pidfile identity check
        # recognizes it as this fork's review server.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "api/server.py"]
        )
        def reap() -> None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

        self.addCleanup(reap)
        self.pidfile.write_text(f"{proc.pid}\n")
        return proc

    def test_intent_accepts_the_host_runtime(self):
        state = mock.Mock()
        intent = Path(self.tmp.name) / "intent.json"
        with mock.patch.object(self.update, "_runtime_intent_path", return_value=intent):
            self.update._write_runtime_intent(state, {"host"})
            self.assertEqual(self.update._load_runtime_intent(state), {"host"})
        pm2, docker, host = self.update._service_targets_from_intent({"host"}, None, None)
        self.assertIsNone(pm2)
        self.assertIsNone(docker)
        self.assertEqual(host, (str(self.update.REPO_ROOT / "sc"), "host"))

    def test_sandbox_runtime_never_touches_the_host_server(self):
        self.spawn_fake_server()
        with mock.patch.object(self.update, "_host_runtime_selected", return_value=False):
            self.assertIsNone(self.update.stop_host_review_server())
        self.assertTrue(self.pidfile.exists())

    def test_host_runtime_with_nothing_live_has_nothing_to_stop(self):
        with mock.patch.object(self.update, "_host_runtime_selected", return_value=True), \
                mock.patch.object(self.update, "_host_api_answers", return_value=False):
            self.assertIsNone(self.update.stop_host_review_server())

    def test_host_runtime_refuses_beside_a_server_it_did_not_start(self):
        with mock.patch.object(self.update, "_host_runtime_selected", return_value=True), \
                mock.patch.object(self.update, "_host_api_answers", return_value=True), \
                self.assertRaisesRegex(SystemExit, "did not start it"):
            self.update.stop_host_review_server()

    def test_host_runtime_stops_the_pidfile_server_and_records_intent(self):
        proc = self.spawn_fake_server()
        with mock.patch.object(self.update, "_host_runtime_selected", return_value=True):
            service = self.update.stop_host_review_server()
        self.assertEqual(service, (str(self.update.REPO_ROOT / "sc"), "host"))
        self.assertIsNotNone(proc.wait(timeout=5))
        self.assertFalse(self.pidfile.exists())

    def test_relaunch_goes_through_the_dispatcher_and_failure_stops_it_again(self):
        sc = Path(self.tmp.name) / "sc"
        log = Path(self.tmp.name) / "sc.log"
        sc.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {log}\n"
            "[ \"$1\" != launch ] || exit \"${SC_TEST_LAUNCH_RC:-0}\"\n"
        )
        sc.chmod(0o755)
        service = (str(sc), "host")
        self.update.start_host_review_server(service)
        self.assertEqual(log.read_text().splitlines(), ["launch"])

        with mock.patch.object(self.update, "require_restarted_runtime_health"):
            self.update.restart_review_servers(None, None, service)
        self.assertEqual(log.read_text().splitlines(), ["launch", "launch"])

        with mock.patch.dict(os.environ, {"SC_TEST_LAUNCH_RC": "3"}), \
                self.assertRaisesRegex(SystemExit, "could not start"):
            self.update.start_host_review_server(service)

        failure = mock.Mock(side_effect=SystemExit("injected readiness failure"))
        with mock.patch.object(self.update, "require_restarted_runtime_health", failure), \
                self.assertRaisesRegex(SystemExit, "all managed runtimes are stopped"):
            self.update.restart_review_servers(None, None, service)
        self.assertEqual(log.read_text().splitlines()[-2:], ["launch", "down"])


if __name__ == "__main__":
    unittest.main()
