#!/usr/bin/env python3
"""The host-side engine-ops service (Feature #93, spec #270 acceptance 6 and 7).

`POST /ops/<update|restart|update-harnesses>` and `GET /status` on the service,
plus what the review API adds so the GUI can find it (`GET /api/engine-ops` and
one CSP `connect-src` origin). Nothing here runs a real engine verb or touches
a real instance: every service in this file serves a temporary root whose `sc`
is a stub, with logs and state in a temporary directory, all replaced in code.

Acceptance 6 — gated and fixed: a shell Bearer token is refused; a missing or
wrong Origin is refused on POST; a non-loopback Host is refused; an unknown
operation is 404; a second POST while one runs is 409; the service refuses to
start with SC_SANDBOX set; it binds loopback only.
Acceptance 7 — an operation outlives the review server and the service's own
stop path: a stub verb stops a test review server and the service that started
it, then exits, and the status shows its exit code afterwards. A second form
runs the service as a transient systemd user unit and stops the unit; it is
skipped where no user manager answers.

Run:
    python3 tests/test_engine_ops_service.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402  (adds scripts/ to the path on import)
import engine_ops  # noqa: E402
import engine_ops_service  # noqa: E402
import ports  # noqa: E402

GUI_PORT = 8800
GUI = f"http://127.0.0.1:{GUI_PORT}"
GUI_ORIGINS = frozenset({GUI, f"http://localhost:{GUI_PORT}"})

# A stand-in for `./sc`. It records the arguments it was given, then acts on
# the first one. Only the stub verbs below exist; a real verb name does nothing
# but record itself and exit 0.
STUB_SC = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, signal, socket, subprocess, sys, time
    from pathlib import Path
    here = Path(__file__).resolve().parent
    args = sys.argv[1:]
    (here / "argv.json").write_text(json.dumps(args))
    print("stub sc " + " ".join(args), flush=True)
    verb = args[0] if args else ""

    def port_closed(port):
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            except OSError:
                return True
            time.sleep(0.05)
        return False

    def pid_gone(pid):
        for _ in range(200):
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
            except OSError:
                return True
            if state == "Z":
                return True
            time.sleep(0.05)
        return False

    if verb == "hold":
        release = here / "release"
        for _ in range(600):
            if release.exists():
                break
            time.sleep(0.05)
        sys.exit(0)
    if verb == "fail":
        print("stub failure detail", flush=True)
        sys.exit(3)
    if verb == "chatter":
        sys.stdout.write("x" * 70000 + "\\nlast line\\n")
        sys.exit(0)
    if verb in ("stop-engine", "stop-unit"):
        # The real verbs work for a while before they stop anything; give the
        # service the same chance to answer the request that started this.
        time.sleep(0.5)
        port = int((here / "service.port").read_text())
        review = int((here / "review.pid").read_text())
        os.kill(review, signal.SIGTERM)          # the review server
        if verb == "stop-engine":
            # `./sc down`: signal the one pid in the broker's pidfile.
            os.kill(int((here / "service.pid").read_text()), signal.SIGTERM)
        else:
            # A unit stop: every process left in its control group is killed.
            unit = (here / "service.unit").read_text().strip()
            subprocess.run(["systemctl", "--user", "stop", unit], check=False)
        gone = port_closed(port) and pid_gone(review)
        print("engine gone" if gone else "engine still answering", flush=True)
        sys.exit(7 if gone else 1)
    sys.exit(0)
''')

# The service under test in its own process: the real handler and runner, over
# a temporary root, log directory, and state file.
SERVICE_LAUNCHER = textwrap.dedent('''\
    import json, os, sys
    from pathlib import Path
    engine, root, logs, scope, gui = sys.argv[1:6]
    sys.path.insert(0, str(Path(engine) / "api"))
    import engine_ops, engine_ops_service

    engine_ops.REPO_ROOT = Path(root)
    engine_ops.LOG_DIR = Path(logs)
    engine_ops.STATE_FILE = Path(root) / "engine-ops.json"
    engine_ops.detect_runtime = lambda: "host"
    engine_ops.live_sessions = lambda: None
    engine_ops.VERBS["restart"] = tuple(json.loads(sys.argv[6]))
    if scope != "systemd":
        engine_ops.scope_prefix = lambda op: []
    engine_ops_service.gui_origins = lambda: frozenset({gui})

    real_write_state = engine_ops_service._write_state

    def write_state(port):
        real_write_state(port)
        Path(root, "service.pid").write_text(str(os.getpid()))
        tmp = Path(root, "service.port.tmp")
        tmp.write_text(str(port))
        os.replace(tmp, Path(root, "service.port"))

    engine_ops_service._write_state = write_state
    raise SystemExit(engine_ops_service.serve(int(sys.argv[7])))
''')


def write_stub_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    stub = root / "sc"
    stub.write_text(STUB_SC)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def wait_for(predicate, timeout: float = 15.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def http_call(port: int, method: str, path: str, *, host: str | None = "",
              origin: str | None = None, token: str | None = None,
              body: dict | None = None, headers: dict | None = None,
              address: str = "127.0.0.1"):
    """One request. `host=""` sends the service's own loopback Host."""
    conn = http.client.HTTPConnection(address, port, timeout=20)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host == "":
            host = f"127.0.0.1:{port}"
        if host is not None:
            conn.putheader("Host", host)
        if origin is not None:
            conn.putheader("Origin", origin)
        if token is not None:
            conn.putheader("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        raw = json.dumps(body).encode() if body is not None else b""
        if method in ("POST", "PUT"):
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(raw)))
        conn.endheaders(raw if raw else None)
        response = conn.getresponse()
        data = response.read()
        return (response.status, dict(response.getheaders()),
                json.loads(data) if data else None)
    finally:
        conn.close()


class ServiceFixture(unittest.TestCase):
    """The real service on a kernel-chosen loopback port, in this process."""

    runtime = "host"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "root"
        write_stub_root(self.root)
        self.logs = base / "logs"
        self.status_file = self.logs / engine_ops.STATUS_NAME
        for patcher in (
            mock.patch.object(engine_ops, "REPO_ROOT", self.root),
            mock.patch.object(engine_ops, "LOG_DIR", self.logs),
            mock.patch.object(engine_ops, "STATE_FILE", base / "engine-ops.json"),
            mock.patch.object(engine_ops, "detect_runtime",
                              side_effect=lambda: self.runtime),
            mock.patch.object(engine_ops, "scope_prefix", return_value=[]),
            mock.patch.object(engine_ops, "live_sessions",
                              return_value={"count": 2, "browser": 1,
                                            "indeterminate": 0}),
            mock.patch.object(engine_ops_service, "gui_origins",
                              return_value=GUI_ORIGINS),
            mock.patch.object(engine_ops_service.Handler, "log_message",
                              lambda *args, **kwargs: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = engine_ops_service.make_server(0)
        self.port = self.service.server_address[1]
        thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.service.server_close)
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.release)

    def release(self) -> None:
        """Let a `hold` stub finish, and wait for it, before the tree is removed."""
        (self.root / "release").write_text("go")
        wait_for(lambda: not engine_ops.is_running(
            engine_ops.read_status(self.status_file)))

    def call(self, method, path, **kwargs):
        return http_call(self.port, method, path, **kwargs)

    def post(self, op, **kwargs):
        kwargs.setdefault("origin", GUI)
        return self.call("POST", f"/ops/{op}", **kwargs)

    def status(self):
        return self.call("GET", "/status", origin=GUI)

    def finished(self):
        def done():
            record = engine_ops.read_status(self.status_file)
            return record if record and record.get("state") == "finished" else None
        record = wait_for(done)
        self.assertIsNotNone(record, engine_ops.read_status(self.status_file))
        return record

    def stub_argv(self):
        # A 202 means the wrapper is running, not that the stub has got as far
        # as recording its arguments.
        self.assertTrue(wait_for(lambda: (self.root / "argv.json").exists()))
        return json.loads((self.root / "argv.json").read_text())

    def assert_nothing_ran(self):
        self.assertFalse(self.status_file.exists())
        self.assertFalse((self.root / "argv.json").exists())


class GateTest(ServiceFixture):
    """Acceptance 6: gated and fixed."""

    def test_shell_bearer_token_is_refused(self):
        for token in ("shell-token", "anything"):
            status, headers, out = self.call("GET", "/status", origin=GUI, token=token)
            self.assertEqual(status, 403, out)
            self.assertEqual(out["error"]["code"], "fnb_operator_required")
            for op in ("update", "restart", "update-harnesses"):
                status, _headers, out = self.post(op, token=token)
                self.assertEqual(status, 403, (op, out))
                self.assertEqual(out["error"]["code"], "fnb_operator_required")
        # Any Authorization header at all: the browser operator carries none.
        status, _headers, out = self.post(
            "update", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        self.assertEqual(status, 403, out)
        self.assert_nothing_ran()

    def test_missing_origin_is_refused_on_post(self):
        for op in ("update", "restart", "update-harnesses"):
            status, _headers, out = self.call("POST", f"/ops/{op}")
            self.assertEqual(status, 403, (op, out))
            self.assertEqual(out["error"]["code"], "same_origin_required")
        self.assert_nothing_ran()

    def test_wrong_origin_is_refused(self):
        for origin in (
            "https://attacker.example",
            f"http://127.0.0.1:{GUI_PORT + 1}",        # another fork's GUI
            f"https://127.0.0.1:{GUI_PORT}",           # wrong scheme
            f"http://127.0.0.1:{GUI_PORT}.attacker.example",
            f"http://127.0.0.1:{self.port}",           # the service itself
            "null",
        ):
            status, headers, out = self.post("restart", origin=origin)
            self.assertEqual(status, 403, (origin, out))
            self.assertEqual(out["error"]["code"], "same_origin_required")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            status, headers, out = self.call("GET", "/status", origin=origin)
            self.assertEqual(status, 403, (origin, out))
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _headers, out = self.post(
            "restart", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403, out)
        self.assert_nothing_ran()

    def test_non_loopback_host_is_refused(self):
        for host in ("attacker.example", f"attacker.example:{self.port}",
                     f"192.168.1.10:{self.port}", f"127.0.0.1:{self.port + 1}",
                     "127.0.0.1", f"127.0.0.1.attacker.example:{self.port}",
                     None):
            status, _headers, out = self.call("GET", "/status", host=host, origin=GUI)
            self.assertEqual(status, 403, (host, out))
            self.assertEqual(out["error"]["code"], "loopback_host_required")
            status, _headers, out = self.post("restart", host=host)
            self.assertEqual(status, 403, (host, out))
            self.assertEqual(out["error"]["code"], "loopback_host_required")
            status, _headers, out = self.call("GET", "/health", host=host)
            self.assertEqual(status, 403, (host, out))
        self.assert_nothing_ran()
        status, _headers, out = self.call(
            "GET", "/status", host=f"localhost:{self.port}", origin=GUI)
        self.assertEqual(status, 200, out)

    def test_unknown_operation_is_404(self):
        for op in ("launch", "down", "rollback", "", "update/extra", "..%2Fsc",
                   "status", "UPDATE", "restart%20-y"):
            status, _headers, out = self.post(op)
            self.assertEqual(status, 404, (op, out))
            self.assertEqual(out["error"]["code"], "unknown_engine_op")
        self.assertEqual(out["error"]["details"]["ops"],
                         ["restart", "update", "update-harnesses"])
        for path in ("/update", "/api/engine/ops/update", "/ops", "/"):
            status, _headers, out = self.call("POST", path, origin=GUI)
            self.assertEqual(status, 404, (path, out))
        for method in ("PUT", "DELETE", "PATCH"):
            status, _headers, out = self.call(method, "/ops/update", origin=GUI)
            self.assertEqual(status, 405, (method, out))
        self.assert_nothing_ran()

    def test_concurrent_post_is_409_with_the_running_operation(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("hold",)}):
            status, _headers, out = self.post("update")
            self.assertEqual(status, 202, out)
            self.assertTrue(out["started"])
            self.assertEqual(out["operation"]["op"], "update")
            self.assertEqual(out["operation"]["state"], "running")
            self.assertEqual(out["operation"]["detach"], "session")

            for op in ("restart", "update", "update-harnesses"):
                status, headers, out = self.post(op)
                self.assertEqual(status, 409, (op, out))
                self.assertEqual(out["error"]["code"], "engine_op_running")
                self.assertEqual(out["operation"]["op"], "update")
                self.assertEqual(out["operation"]["state"], "running")
                self.assertEqual(headers["Access-Control-Allow-Origin"], GUI)

            status, _headers, out = self.status()
            self.assertTrue(out["running"])
            self.assertEqual(out["operation"]["op"], "update")
        # Only the first request reached the command line.
        self.assertEqual(self.stub_argv(), ["hold"])
        self.release()
        self.assertEqual(self.finished()["exit_code"], 0)
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_verb_table_is_fixed_and_no_request_field_reaches_the_command(self):
        self.assertEqual(engine_ops.VERBS, {
            "update": ("update",),
            "restart": ("restart", "-y"),
            "update-harnesses": ("update-harnesses",),
        })
        status, _headers, out = self.call(
            "POST", "/ops/restart?args=--force&op=down", origin=GUI,
            body={"op": "down", "args": ["--force"], "cmd": "rm -rf /",
                  "ref": "evil", "cwd": "/"})
        self.assertEqual(status, 202, out)
        self.finished()
        self.assertEqual(self.stub_argv(), ["restart", "-y"])
        for op, expected in (("update", ["update"]),
                             ("update-harnesses", ["update-harnesses"])):
            status, _headers, out = self.post(op, body={"args": ["--ref", "evil"]})
            self.assertEqual(status, 202, out)
            self.finished()
            self.assertEqual(self.stub_argv(), expected)

    def test_binds_loopback_only(self):
        self.assertEqual(engine_ops_service.BIND, "127.0.0.1")
        self.assertEqual(self.service.server_address[0], "127.0.0.1")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))     # no packet is sent
            address = probe.getsockname()[0]
        except OSError:
            address = None
        finally:
            probe.close()
        if not address or address.startswith("127."):
            self.skipTest("this host has no non-loopback address to try")
        with self.assertRaises(OSError):
            socket.create_connection((address, self.port), timeout=2).close()


class CorsTest(ServiceFixture):
    def test_cors_answers_the_gui_origin_only(self):
        for origin in sorted(GUI_ORIGINS):
            status, headers, out = self.call("GET", "/status", origin=origin)
            self.assertEqual(status, 200, out)
            self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
            self.assertEqual(headers["Vary"], "Origin")
        status, headers, _out = self.call("GET", "/status")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _out = self.post("launch")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Access-Control-Allow-Origin"], GUI)

    def test_preflight(self):
        status, headers, out = self.call(
            "OPTIONS", "/ops/restart", origin=GUI,
            headers={"Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(status, 204, out)
        self.assertEqual(headers["Access-Control-Allow-Origin"], GUI)
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", headers["Access-Control-Allow-Headers"])
        self.assertNotIn("Authorization", headers["Access-Control-Allow-Headers"])
        for origin in ("https://attacker.example", None):
            status, headers, out = self.call(
                "OPTIONS", "/ops/restart", origin=origin,
                headers={"Access-Control-Request-Method": "POST"})
            self.assertEqual(status, 403, (origin, out))
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertNotIn("Access-Control-Allow-Methods", headers)
        self.assert_nothing_ran()


class StatusTest(ServiceFixture):
    def test_health(self):
        status, _headers, out = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(out, {"ok": True, "service": "engine-ops"})

    def test_status_on_the_host_runtime(self):
        status, _headers, out = self.status()
        self.assertEqual(status, 200, out)
        self.assertEqual(out["service"], "engine-ops")
        self.assertEqual(out["runtime"], "host")
        self.assertEqual(out["ops"], {
            "update": {"cli": "./sc update", "stops_engine": True,
                       "ends_sessions": "browser", "takes_effect": "now"},
            "restart": {"cli": "./sc restart", "stops_engine": True,
                        "ends_sessions": "browser", "takes_effect": "now"},
            "update-harnesses": {"cli": "./sc update-harnesses",
                                 "stops_engine": False, "ends_sessions": "none",
                                 "takes_effect": "now"},
        })
        self.assertEqual(out["recovery_cli"], "./sc launch")
        self.assertFalse(out["running"])
        self.assertIsNone(out["operation"])
        self.assertIsNone(out["log_tail"])
        self.assertEqual(out["live_sessions"],
                         {"count": 2, "browser": 1, "indeterminate": 0})

    def test_status_on_the_docker_runtime(self):
        self.runtime = "docker"
        status, _headers, out = self.status()
        self.assertEqual(status, 200, out)
        self.assertEqual(out["runtime"], "docker")
        # Restart removes the container and ends every session in it; a
        # harness update is an image rebuild that waits for the next restart.
        self.assertEqual(out["ops"]["restart"]["ends_sessions"], "all")
        self.assertEqual(out["ops"]["update"]["ends_sessions"], "all")
        self.assertEqual(out["ops"]["update-harnesses"], {
            "cli": "./sc update-harnesses", "stops_engine": False,
            "ends_sessions": "none", "takes_effect": "next_restart"})
        with mock.patch.dict(engine_ops.VERBS, {"update-harnesses": ("noop",)}):
            status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_failed_operation_reports_exit_code_and_log_and_does_not_block(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("fail",)}):
            status, _headers, out = self.post("update")
            self.assertEqual(status, 202, out)
            self.finished()
        status, _headers, out = self.status()
        self.assertFalse(out["running"])
        operation = out["operation"]
        self.assertEqual(operation["state"], "finished")
        self.assertEqual(operation["exit_code"], 3)
        self.assertFalse(operation["ok"])
        self.assertEqual(operation["cli"], "./sc update")
        self.assertTrue(operation["started_at"] and operation["finished_at"])
        self.assertEqual(operation["log"], str(self.logs / "engine-op-update.log"))
        self.assertIn("stub failure detail", out["log_tail"])
        self.assertIn("finished with exit code 3", out["log_tail"])
        self.assertFalse(out["log_truncated"])
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_log_tail_is_bounded(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("chatter",)}):
            self.post("update")
            self.finished()
        _status, _headers, out = self.status()
        self.assertTrue(out["log_truncated"])
        self.assertLessEqual(len(out["log_tail"].encode()), engine_ops.LOG_TAIL_BYTES)
        self.assertEqual(out["log_tail_limit"], engine_ops.LOG_TAIL_BYTES)
        self.assertIn("last line", out["log_tail"])

    def test_crashed_operation_does_not_block_forever(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        engine_ops.write_status(self.status_file, {
            "op": "update", "state": "running", "pid": dead.pid,
            "pid_start_ticks": 1, "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None, "exit_code": None,
            "log": str(self.logs / "engine-op-update.log"), "detach": "session",
        })
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "interrupted")
        self.assertIsNone(out["operation"]["ok"])
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_recycled_pid_is_not_mistaken_for_the_operation(self):
        if engine_ops.pid_start_ticks(os.getpid()) is None:
            self.skipTest("no /proc start time on this platform")
        # This test process is alive, but its start time is not the recorded one.
        engine_ops.write_status(self.status_file, {
            "op": "restart", "state": "running", "pid": os.getpid(),
            "pid_start_ticks": 1, "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None, "exit_code": None, "log": None,
            "detach": "session",
        })
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "interrupted")

    def test_stale_claim_without_a_wrapper_expires(self):
        engine_ops.write_status(self.status_file, {
            "op": "update", "state": "starting", "pid": None,
            "started_at": "2026-01-01T00:00:00Z", "finished_at": None,
            "exit_code": None, "log": None, "detach": "session",
        })
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "interrupted")

    def test_wrapper_that_cannot_start_is_reported_and_does_not_block(self):
        with mock.patch.object(engine_ops, "_spawn", side_effect=OSError("no exec")):
            status, _headers, out = self.post("update")
        self.assertEqual(status, 500, out)
        self.assertEqual(out["error"]["code"], "engine_op_start_failed")
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "finished")
        self.assertIsNone(out["operation"]["exit_code"])
        self.assertIn("no exec", out["operation"]["error"])


class SandboxRefusalTest(unittest.TestCase):
    """Acceptance 6: the service refuses to start with SC_SANDBOX set."""

    def test_refuses_to_start_in_the_sandbox(self):
        env = {**os.environ, "SC_SANDBOX": "1"}
        result = subprocess.run(
            [sys.executable, str(ENGINE / "api" / "engine_ops_service.py")],
            env=env, capture_output=True, text=True, timeout=60, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must run on the HOST", result.stderr)
        self.assertNotIn("listening", result.stderr)

    def test_refusal_precedes_any_port_or_listener(self):
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}), \
                mock.patch.object(ports, "ensure_engine_ops_port") as allocate, \
                mock.patch.object(engine_ops_service, "serve") as serve:
            with self.assertRaises(SystemExit) as raised:
                engine_ops_service.main([])
        self.assertIn("HOST", str(raised.exception.code))
        allocate.assert_not_called()
        serve.assert_not_called()

    def test_takes_no_arguments(self):
        with mock.patch.object(engine_ops_service, "in_sandbox", return_value=False), \
                mock.patch.object(engine_ops_service, "serve") as serve:
            with self.assertRaises(SystemExit):
                engine_ops_service.main(["--port", "9999"])
            with self.assertRaises(SystemExit):
                engine_ops_service.main(["--bind", "0.0.0.0"])
        serve.assert_not_called()


class RuntimeDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = Path(self.tmp.name)
        patcher = mock.patch.object(engine_ops, "ENGINE", self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runtime_follows_the_instance_selection(self):
        self.assertEqual(engine_ops.detect_runtime(), "docker")
        (self.engine / "instance.json").write_text(json.dumps({"runtime": "sandbox"}))
        self.assertEqual(engine_ops.detect_runtime(), "docker")
        (self.engine / "instance.json").write_text(json.dumps({"runtime": "host"}))
        self.assertEqual(engine_ops.detect_runtime(), "host")


class PortTest(unittest.TestCase):
    """The per-fork port: derived like the review port, allocated once."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_engine_ops_port_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "fork"
        self.config = self.root / ".super-coder" / "instance.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(
            {"repo": "fork", "port": 8812, "dev_port": 8844}))
        for patcher in (
            mock.patch.object(ports, "REPO_ROOT", self.root),
            mock.patch.object(ports, "CONFIG", self.config),
            mock.patch.object(ports, "_free", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_allocated_once_persisted_and_stable(self):
        self.assertIsNone(ports.stored_engine_ops_port())
        port = ports.ensure_engine_ops_port()
        self.assertTrue(8800 <= port < 8900)
        self.assertNotIn(port, (8812, 8844))
        self.assertEqual(json.loads(self.config.read_text())["engine_ops_port"], port)
        self.assertEqual(ports.stored_engine_ops_port(), port)
        # A listening service holds the port; that is not a reason to move it.
        with mock.patch.object(ports, "_free", return_value=False):
            self.assertEqual(ports.ensure_engine_ops_port(), port)
        stored = json.loads(self.config.read_text())
        self.assertEqual((stored["port"], stored["dev_port"]), (8812, 8844))
        self.assertEqual(ports.resolve()["engine_ops_port"], port)

    def test_avoids_this_forks_other_ports_and_siblings(self):
        seed = ports.PORT_BASE + ports._offset(str(self.root) + ":engine-ops")
        sibling = self.root.parent / "sibling" / ".super-coder" / "instance.json"
        sibling.parent.mkdir(parents=True)
        sibling.write_text(json.dumps({"port": seed, "dev_port": 8801}))
        port = ports.ensure_engine_ops_port()
        self.assertNotIn(port, (seed, 8801, 8812, 8844))

        # And another fork's allocation is in turn avoided by its siblings.
        self.assertIn(port, self._sibling_view())

    def _sibling_view(self) -> set[int]:
        other = self.root.parent / "other"
        with mock.patch.object(ports, "REPO_ROOT", other), \
                mock.patch.object(ports, "CONFIG",
                                  other / ".super-coder" / "instance.json"):
            return ports._sibling_ports()

    def test_an_unusable_stored_value_is_replaced(self):
        for bad in (9999, "8850", True, None):
            self.config.write_text(json.dumps(
                {"repo": "fork", "port": 8812, "dev_port": 8844,
                 "engine_ops_port": bad}))
            port = ports.ensure_engine_ops_port()
            self.assertTrue(8800 <= port < 8900, (bad, port))
            self.assertNotIn(port, (8812, 8844))
            self.assertEqual(ports.stored_engine_ops_port(), port)


def build_file_db(path: Path) -> None:
    source = sqlite3.connect(":memory:")
    source.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        source.executescript(migration.read_text())
    source.execute("INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)")
    source.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname, api_key) "
        "VALUES ('Dev', 'x', 'dev', 'dev', 'shell-token')")
    source.commit()
    target = sqlite3.connect(path)
    source.backup(target)
    target.close()
    source.close()


class ReviewApiDiscoveryTest(unittest.TestCase):
    """What the review API adds: where the service is, and one CSP origin."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "engine.db"
        build_file_db(self.db_path)
        self.state_file = Path(self.tmp.name) / "engine-ops.json"
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        self.stored = self.port
        for patcher in (
            mock.patch.object(server, "db", side_effect=self.connect),
            mock.patch.object(server, "in_container", return_value=False),
            mock.patch.object(ports, "stored_engine_ops_port",
                              side_effect=lambda: self.stored),
            mock.patch.object(engine_ops, "STATE_FILE", self.state_file),
            mock.patch.object(engine_ops, "detect_runtime", return_value="docker"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SC_SANDBOX", None)

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def get(self, path="/api/engine-ops", token=None):
        lines = ["Host: 127.0.0.1:8800", "Content-Length: 0"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        status, _headers, out = server.dispatch_http(
            "GET", path, "\r\n".join(lines), b"")
        return status, json.loads(out)

    def test_reports_port_url_and_probed_availability(self):
        status, out = self.get()
        self.assertEqual(status, 200, out)
        self.assertEqual(out["port"], self.port)
        self.assertEqual(out["url"], f"http://127.0.0.1:{self.port}")
        self.assertIs(out["up"], True)
        self.assertEqual(out["up_source"], "probe")
        self.assertEqual(out["runtime"], "docker")
        self.assertEqual(out["ops"]["restart"]["cli"], "./sc restart")
        self.assertEqual(out["ops"]["update-harnesses"]["takes_effect"], "next_restart")
        self.assertEqual(out["start_cli"], "./sc launch")

        self.listener.close()
        status, out = self.get()
        self.assertIs(out["up"], False)
        self.assertEqual(out["up_source"], "probe")

    def test_unallocated_port_is_down_with_the_cli_commands(self):
        self.stored = None
        status, out = self.get()
        self.assertEqual(status, 200, out)
        self.assertIsNone(out["port"])
        self.assertIsNone(out["url"])
        self.assertIs(out["up"], False)
        self.assertEqual(out["up_source"], "unallocated")
        self.assertEqual(
            {op: row["cli"] for op, row in out["ops"].items()},
            {"update": "./sc update", "restart": "./sc restart",
             "update-harnesses": "./sc update-harnesses"})

    def test_inside_the_sandbox_availability_comes_from_the_state_file(self):
        os.environ["SC_SANDBOX"] = "1"
        with mock.patch.object(engine_ops, "_port_answers") as probe:
            status, out = self.get()
            self.assertIs(out["up"], False)
            self.assertEqual(out["up_source"], "state_file")
            engine_ops.write_status(self.state_file, {"pid": 1, "port": self.port})
            status, out = self.get()
            self.assertIs(out["up"], True)
            engine_ops.write_status(self.state_file, {"pid": 1, "port": self.port + 1})
            status, out = self.get()
            self.assertIs(out["up"], False)
        # The sandbox has no route to the service; it never tries one.
        probe.assert_not_called()

    def test_shell_bearer_token_is_refused(self):
        status, out = self.get(token="shell-token")
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["code"], "fnb_operator_required")

    def test_review_api_runs_no_engine_operation(self):
        for method, path in (("POST", "/api/engine/ops/update"),
                             ("POST", "/api/engine-ops/update"),
                             ("POST", "/api/engine-ops"),
                             ("GET", "/api/engine/ops/status")):
            status, _headers, _out = server.dispatch_http(
                method, path,
                "Host: 127.0.0.1:8800\r\nContent-Length: 0\r\n"
                "Origin: http://127.0.0.1:8800", b"")
            self.assertEqual(status, 404, (method, path))

    def test_csp_names_the_service_origin_and_nothing_wider(self):
        base = server._csp(8800)
        self.assertNotIn("http://", base)
        csp = server._csp(8800, 8855)
        connect = next(part for part in csp.split("; ")
                       if part.startswith("connect-src"))
        sources = connect.split()[1:]
        self.assertIn("http://127.0.0.1:8855", sources)
        self.assertIn("http://localhost:8855", sources)
        self.assertEqual(
            [source for source in sources if source.startswith("http")],
            ["http://127.0.0.1:8855", "http://localhost:8855"])
        self.assertEqual(csp.replace(" http://127.0.0.1:8855 http://localhost:8855", ""),
                         base)


def user_systemd_available() -> bool:
    if not (shutil.which("systemd-run") and shutil.which("systemctl")):
        return False
    try:
        return subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class OutlivesTheEngineTest(unittest.TestCase):
    """Acceptance 7: the operation outlives the review server and the
    service's own stop path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "root"
        write_stub_root(self.root)
        self.logs = base / "logs"
        self.status_file = self.logs / engine_ops.STATUS_NAME
        self.script = base / "service_launcher.py"
        self.script.write_text(SERVICE_LAUNCHER)
        # The "review server": a process the stub verb stops, as the verbs do.
        self.review = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            start_new_session=True)
        self.addCleanup(self.reap, self.review)
        (self.root / "review.pid").write_text(str(self.review.pid))

    @staticmethod
    def reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def service_argv(self, scope: str, verb: str, port: int = 0) -> list[str]:
        return [sys.executable, str(self.script), str(ENGINE), str(self.root),
                str(self.logs), scope, GUI, json.dumps([verb]), str(port)]

    def start_service(self, scope: str, verb: str, port: int = 0) -> subprocess.Popen:
        (self.root / "service.port").unlink(missing_ok=True)
        proc = subprocess.Popen(
            self.service_argv(scope, verb, port), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(self.reap, proc)
        return proc

    def start_unit(self, scope: str) -> None:
        """Run the service as a transient user unit, the way a systemd-managed
        broker (or the host review server) is run."""
        if not user_systemd_available():
            self.skipTest("no systemd user manager answers")
        unit = f"sc-test-engine-ops-{scope}-{os.getpid()}-{int(time.time())}.service"
        (self.root / "service.unit").write_text(unit)
        started = subprocess.run(
            ["systemd-run", "--user", "--quiet", "--collect", "--unit", unit, "--",
             *self.service_argv(scope, "stop-unit")],
            capture_output=True, text=True, check=False)
        if started.returncode != 0:
            self.skipTest(f"cannot start a transient user unit: {started.stderr.strip()}")
        self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", unit],
                        capture_output=True, check=False)

    def port(self) -> int:
        found = wait_for(lambda: (self.root / "service.port").exists(), timeout=30)
        self.assertTrue(found, "the test engine-ops service did not start")
        return int((self.root / "service.port").read_text())

    def answers(self, port: int) -> bool:
        try:
            http_call(port, "GET", "/health")
        except OSError:
            return False
        return True

    def finished_record(self) -> dict:
        def done():
            record = engine_ops.read_status(self.status_file)
            return record if record and record.get("state") == "finished" else None
        record = wait_for(done, timeout=30)
        self.assertIsNotNone(record, engine_ops.read_status(self.status_file))
        return record

    def assert_recorded_after_the_engine_is_gone(self, port: int, detach: str) -> None:
        record = self.finished_record()
        # The stub exits 7 only once the review server is gone and nothing
        # answers the service's port, so this exit code was written by a
        # wrapper that outlived both.
        self.assertEqual(record["exit_code"], 7, record)
        self.assertEqual(record["op"], "restart")
        self.assertEqual(record["detach"], detach)
        self.assertTrue(record["started_at"] and record["finished_at"])
        self.assertIsNotNone(self.review.wait(timeout=30))
        self.assertFalse(self.answers(port))
        log = Path(record["log"]).read_text()
        self.assertIn("engine gone", log)
        self.assertIn("finished with exit code 7", log)

        # The engine returns: a new service reads the same record back.
        self.start_service("session", "noop")
        new_port = self.port()
        status, _headers, out = http_call(new_port, "GET", "/status", origin=GUI)
        self.assertEqual(status, 200, out)
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["op"], "restart")
        self.assertEqual(out["operation"]["state"], "finished")
        self.assertEqual(out["operation"]["exit_code"], 7)
        self.assertIs(out["operation"]["ok"], False)
        self.assertIn("engine gone", out["log_tail"])

    def test_stub_verb_stops_review_server_and_service_then_status_has_exit_code(self):
        service = self.start_service("session", "stop-engine")
        port = self.port()
        # The service's own pidfile-and-state bookkeeping is live.
        state = json.loads((self.root / "engine-ops.json").read_text())
        self.assertEqual((state["pid"], state["port"]), (service.pid, port))

        status, _headers, out = http_call(port, "POST", "/ops/restart", origin=GUI)
        self.assertEqual(status, 202, out)
        self.assertEqual(out["operation"]["detach"], "session")
        self.assertNotEqual(out["operation"]["pid"], service.pid)

        # The service itself is gone, cleanly, by the stub's signal …
        self.assertEqual(service.wait(timeout=30), 0)
        self.assertFalse((self.root / "engine-ops.json").exists())
        # … and so is the review server.
        self.assertEqual(self.review.wait(timeout=30), -signal.SIGTERM)
        self.assert_recorded_after_the_engine_is_gone(port, "session")

    def test_operation_survives_the_stop_of_a_systemd_unit_running_the_service(self):
        """A unit stop kills the unit's whole control group; the scope is why
        the operation is not in it."""
        self.start_unit("systemd")
        port = self.port()
        status, _headers, out = http_call(port, "POST", "/ops/restart", origin=GUI)
        self.assertEqual(status, 202, out)
        self.assertEqual(out["operation"]["detach"], "systemd-scope")
        self.assert_recorded_after_the_engine_is_gone(port, "systemd-scope")

    def test_a_new_session_alone_does_not_survive_a_unit_stop(self):
        """The control: without the scope, the unit stop takes the wrapper too."""
        self.start_unit("session")
        port = self.port()
        status, _headers, out = http_call(port, "POST", "/ops/restart", origin=GUI)
        self.assertEqual(status, 202, out)
        self.assertEqual(out["operation"]["detach"], "session")
        pid = out["operation"]["pid"]
        self.assertTrue(wait_for(lambda: not self.answers(port), timeout=30))
        self.assertTrue(wait_for(lambda: not engine_ops.pid_alive(pid), timeout=30))
        record = engine_ops.read_status(self.status_file)
        self.assertEqual(record["state"], "running", record)
        self.assertIsNone(record["exit_code"])
        self.assertEqual(engine_ops.effective_state(record), "interrupted")


if __name__ == "__main__":
    unittest.main()
