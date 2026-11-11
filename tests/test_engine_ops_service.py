#!/usr/bin/env python3
"""The host-side engine-ops service (Feature #93, spec #270 acceptance 6 and 7).

`POST /ops/<update|restart|update-harnesses>` and `GET /status` on the service,
plus what the review API adds so the GUI can find it (`GET /api/engine-ops` and
one CSP `connect-src` origin). Nothing here runs a real engine verb or touches
a real instance: every service in this file serves a temporary checkout whose
`sc` is a stub, with its private state in a temporary directory, all replaced
in code.

Acceptance 6 — gated and fixed: a shell Bearer token is refused; a missing or
wrong Origin is refused on POST; a non-loopback Host is refused; an unknown
operation is 404; a second POST while one runs is 409; the service refuses to
start with SC_SANDBOX set; it binds loopback only.
Acceptance 7 — an operation outlives the review server and the service's own
stop path: a stub verb stops a test review server and the service that started
it, then exits, and the status shows its exit code afterwards. A second form
runs the service as a transient systemd user unit and stops the unit; it is
skipped where no user manager answers.

The sandbox boundary — on docker the checkout is bind-mounted writable into
the sandbox, so `SandboxCannotSteerTest` rewrites every file under the engine
directory "from the sandbox side" and shows that the accepted origin, the
files read and written, and the pid signalled do not move.

Run:
    python3 tests/test_engine_ops_service.py
"""
from __future__ import annotations

import contextlib
import http.client
import io
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
        for _ in range(1200):
            if release.exists():
                break
            time.sleep(0.05)
        sys.exit(0)
    if verb == "fail":
        print("stub failure detail", flush=True)
        sys.exit(3)
    if verb == "spawn-child":
        # Work the operation started that outlives the command itself.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        (here / "child.pid").write_text(str(child.pid))
        time.sleep(600)
    if verb == "leave-busy-broker":
        # A broker a restart leaves behind, already serving a long request:
        # it has spawned a worker, the way a broker does per request.
        broker = here / ".super-coder" / "api" / "ts_broker.py"
        broker.parent.mkdir(parents=True, exist_ok=True)
        broker.write_text(
            "import pathlib, subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(c.pid)); time.sleep(600)")
        child = subprocess.Popen([sys.executable, str(broker), str(here / "worker.pid")])
        (here / "broker.pid").write_text(str(child.pid))
        for _ in range(200):
            if (here / "worker.pid").exists():
                break
            time.sleep(0.05)
        sys.exit(0)
    if verb == "leave-broker":
        # What a restart's launch step does: leave a known service running.
        broker = here / ".super-coder" / "api" / "pm2_broker.py"
        broker.parent.mkdir(parents=True, exist_ok=True)
        broker.write_text("import time; time.sleep(600)")
        child = subprocess.Popen([sys.executable, str(broker)])
        (here / "broker.pid").write_text(str(child.pid))
        sys.exit(0)
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
            # What `./sc down` runs: the real stop path, against this test's
            # private state.
            sys.path.insert(0, (here / "engine.path").read_text() + "/api")
            sys.path.insert(0, (here / "engine.path").read_text() + "/scripts")
            import engine_ops
            engine_ops.PRIVATE_DIR = Path((here / "private.path").read_text())
            engine_ops.SERVICE_SCRIPT = Path((here / "launcher.path").read_text())
            code, message = engine_ops.service_down()
            print(message, flush=True)
        else:
            # A unit stop: every process left in its control group is killed.
            unit = (here / "service.unit").read_text().strip()
            subprocess.run(["systemctl", "--user", "stop", unit], check=False)
        gone = port_closed(port) and pid_gone(review)
        print("engine gone" if gone else "engine still answering", flush=True)
        sys.exit(7 if gone else 1)
    sys.exit(0)
''')

# The service under test in its own process: the real handler, runner, pinned
# configuration, and registration, over a temporary checkout and private
# directory. The file is named like the real service so that the stop path's
# identity check (pid, start time, command line) sees one.
SERVICE_LAUNCHER = textwrap.dedent('''\
    import json, os, sys
    from pathlib import Path
    engine, root, private, scope = sys.argv[1:5]
    sys.path.insert(0, str(Path(engine) / "api"))
    sys.path.insert(0, str(Path(engine) / "scripts"))
    import engine_ops, engine_ops_service, ports

    engine_ops.REPO_ROOT = Path(root)
    engine_ops.ENGINE = Path(root) / ".super-coder"
    engine_ops.PRIVATE_DIR = Path(private)
    engine_ops.SERVICE_SCRIPT = Path(sys.argv[0]).resolve()
    engine_ops.live_sessions = lambda: None
    engine_ops.VERBS["restart"] = tuple(json.loads(sys.argv[5]))
    if scope != "systemd":
        engine_ops.scope_prefix = lambda op: []
    ports.REPO_ROOT = Path(root)
    ports.CONFIG = Path(root) / ".super-coder" / "instance.json"
    ports._free = lambda port: True
    ports._sibling_ports = lambda: set()
    engine_ops._allocate_port = lambda: 0      # the kernel picks; tests read it

    real_register = engine_ops.register_service

    def register(port):
        real_register(port)
        tmp = Path(root, "service.port.tmp")
        tmp.write_text(str(port))
        os.replace(tmp, Path(root, "service.port"))

    engine_ops.register_service = register
    engine_ops._valid_port = lambda value: type(value) is int and 0 <= value < 65536
    config = engine_ops.ensure_config()
    raise SystemExit(engine_ops_service.serve(config["port"]))
''')


def write_stub_root(root: Path, review_port: int = GUI_PORT) -> None:
    """A checkout: the stub `sc` and an engine directory with its usual files."""
    (root / ".super-coder" / "run").mkdir(parents=True, exist_ok=True)
    (root / ".super-coder" / "logs").mkdir(exist_ok=True)
    (root / ".super-coder" / "instance.json").write_text(json.dumps(
        {"repo": root.name, "port": review_port, "dev_port": review_port + 1,
         "runtime": "host"}))
    stub = root / "sc"
    stub.write_text(STUB_SC)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def make_private(base: Path) -> Path:
    private = base / "private"
    private.mkdir(mode=0o700)
    return private


def operator_pins(private: Path, gui_port: int = GUI_PORT, port: int | None = None) -> None:
    """The state an operator's `./sc engine-ops-pin` leaves, written directly
    so that no test touches a real instance config."""
    config = {"gui_port": gui_port, "pinned_at": "2026-01-01T00:00:00Z"}
    if port is not None:
        config["port"] = port
    engine_ops.write_private(private / "config.json", config)


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


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                            start_new_session=True)


def reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()


class ServiceFixture(unittest.TestCase):
    """The real service on a kernel-chosen loopback port, in this process."""

    runtime = "host"
    pinned = True

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.base = base
        self.root = base / "root"
        write_stub_root(self.root)
        self.private = make_private(base)
        self.status_file = self.private / engine_ops.STATUS_NAME
        for patcher in (
            mock.patch.object(engine_ops, "REPO_ROOT", self.root),
            mock.patch.object(engine_ops, "ENGINE", self.root / ".super-coder"),
            mock.patch.object(engine_ops, "PRIVATE_DIR", self.private),
            mock.patch.object(engine_ops, "detect_runtime",
                              side_effect=lambda: self.runtime),
            mock.patch.object(engine_ops, "scope_prefix", return_value=[]),
            mock.patch.object(engine_ops, "live_sessions",
                              return_value={"count": 2, "browser": 1,
                                            "indeterminate": 0}),
            mock.patch.object(engine_ops_service.Handler, "log_message",
                              lambda *args, **kwargs: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        engine_ops._host_private_checked.clear()
        if self.pinned:
            operator_pins(self.private)
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
        # A 202 means the runner is running, not that the stub has got as far
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
            f"http://127.0.0.1:{GUI_PORT + 1}",        # the fork's dev server
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

    def test_gate_holds_while_the_command_outlives_a_killed_runner(self):
        """Killing the runner leaves its command running: still one operation."""
        with mock.patch.dict(engine_ops.VERBS, {"update": ("hold",)}):
            status, _headers, out = self.post("update")
            self.assertEqual(status, 202, out)
            runner = out["operation"]["pid"]
            record = wait_for(lambda: (
                (engine_ops.read_status(self.status_file) or {}).get("cmd_pid")
                and engine_ops.read_status(self.status_file)))
            command = record["cmd_pid"]
            self.assertNotEqual(command, runner)

            os.kill(runner, signal.SIGKILL)
            self.assertTrue(wait_for(
                lambda: not engine_ops.pid_alive(runner, record["pid_start_ticks"])))
            self.assertTrue(engine_ops.pid_alive(command, record["cmd_start_ticks"]))

            _status, _headers, out = self.status()
            self.assertTrue(out["running"])
            self.assertEqual(out["operation"]["state"], "running")
            for op in ("restart", "update", "update-harnesses"):
                status, _headers, out = self.post(op)
                self.assertEqual(status, 409, (op, out))
                self.assertEqual(out["error"]["code"], "engine_op_running")
            self.assertEqual(self.stub_argv(), ["hold"])

            # Once the command itself is gone the gate opens; nobody was left
            # to record an exit code, so the operation reads as interrupted.
            (self.root / "release").write_text("go")
            self.assertTrue(wait_for(
                lambda: not engine_ops.pid_alive(command, record["cmd_start_ticks"])))
            _status, _headers, out = self.status()
            self.assertFalse(out["running"])
            self.assertEqual(out["operation"]["state"], "interrupted")
            self.assertIsNone(out["operation"]["exit_code"])
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def kill_later(self, name: str) -> None:
        def kill():
            try:
                os.kill(int((self.root / name).read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(kill)

    def test_gate_holds_while_work_the_operation_started_is_still_running(self):
        """Kill the command after it spawned a child: the runner records
        `finished`, but the child is still the operation's work."""
        self.kill_later("child.pid")
        with mock.patch.dict(engine_ops.VERBS, {"update": ("spawn-child",)}):
            status, _headers, out = self.post("update")
            self.assertEqual(status, 202, out)
            self.assertTrue(wait_for(lambda: (self.root / "child.pid").exists()))
            child = int((self.root / "child.pid").read_text())
            record = wait_for(lambda: (
                (engine_ops.read_status(self.status_file) or {}).get("cmd_pid")
                and engine_ops.read_status(self.status_file)))
            os.kill(record["cmd_pid"], signal.SIGKILL)
            stored = self.finished()
            self.assertEqual(stored["exit_code"], -signal.SIGKILL)
            self.assertTrue(wait_for(
                lambda: not engine_ops.pid_alive(stored["pid"], stored["pid_start_ticks"])))
            self.assertTrue(engine_ops.pid_alive(child))
            self.assertEqual(engine_ops.operation_pids(stored), [child])

            _status, _headers, out = self.status()
            self.assertTrue(out["running"])
            self.assertEqual(out["operation"]["state"], "running")
            self.assertIsNone(out["operation"]["ok"])
            for op in ("restart", "update", "update-harnesses"):
                status, _headers, out = self.post(op)
                self.assertEqual(status, 409, (op, out))
                self.assertEqual(out["error"]["code"], "engine_op_running")

            os.kill(child, signal.SIGKILL)
            self.assertTrue(wait_for(lambda: not engine_ops.operation_pids(stored)))
            _status, _headers, out = self.status()
            self.assertFalse(out["running"])
            self.assertEqual(out["operation"]["state"], "finished")
            self.assertEqual(out["operation"]["exit_code"], -signal.SIGKILL)
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_known_services_left_by_launch_do_not_hold_the_gate(self):
        """A restart leaves brokers running by design; they are not its work."""
        self.kill_later("broker.pid")
        with mock.patch.dict(engine_ops.VERBS, {"restart": ("leave-broker",)}):
            status, _headers, out = self.post("restart")
            self.assertEqual(status, 202, out)
            stored = self.finished()
        broker = int((self.root / "broker.pid").read_text())
        self.assertTrue(wait_for(
            lambda: not engine_ops.pid_alive(stored["pid"], stored["pid_start_ticks"])))
        self.assertTrue(engine_ops.pid_alive(broker))
        self.assertTrue(engine_ops.runs_script(
            broker, self.root / ".super-coder" / "api" / "pm2_broker.py"))
        self.assertEqual(engine_ops.operation_pids(stored), [])
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "finished")
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_work_done_by_a_service_launch_left_behind_does_not_hold_the_gate(self):
        """A broker started by a restart inherits its session, and so does the
        worker it spawns for a request (from the sandbox, on docker). That is
        the broker's work: it must not turn a finished restart back into a
        running one."""
        self.kill_later("worker.pid")
        self.kill_later("broker.pid")
        with mock.patch.dict(engine_ops.VERBS, {"restart": ("leave-busy-broker",)}):
            status, _headers, out = self.post("restart")
            self.assertEqual(status, 202, out)
            stored = self.finished()
        self.assertEqual(stored["exit_code"], 0)
        broker = int((self.root / "broker.pid").read_text())
        worker = int((self.root / "worker.pid").read_text())
        self.assertTrue(wait_for(
            lambda: not engine_ops.pid_alive(stored["pid"], stored["pid_start_ticks"])))
        # Both are alive and both are in the finished operation's session.
        for pid in (broker, worker):
            self.assertTrue(engine_ops.pid_alive(pid))
            self.assertEqual(os.getsid(pid), stored["pid"])

        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "finished")
        self.assertIs(out["operation"]["ok"], True)
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_a_reused_session_id_is_not_the_operation(self):
        ticks = engine_ops.pid_start_ticks(os.getpid())
        if ticks is None:
            self.skipTest("no /proc start time on this platform")
        # This process and its session are alive, but with another start time.
        record = {"op": "update", "state": "finished", "pid": os.getsid(0),
                  "pid_start_ticks": 1, "exit_code": 0,
                  "started_at": "2026-01-01T00:00:00Z"}
        self.assertEqual(engine_ops.operation_pids(record), [])

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


class UnpinnedTest(ServiceFixture):
    """Until an operator pins the GUI origin, nothing starts, and it says why."""

    pinned = False

    def test_status_says_unpinned_and_post_is_refused_with_the_reason(self):
        status, headers, out = self.status()
        self.assertEqual(status, 200, out)
        self.assertIs(out["pinned"], False)
        self.assertIsNone(out["gui_origin"])
        self.assertEqual(out["pin_cli"], "./sc engine-ops-pin")
        self.assertIn("./sc engine-ops-pin", out["reason"])
        # Readable by the GUI page so it can show the reason; carries no state.
        self.assertEqual(headers["Access-Control-Allow-Origin"], GUI)
        self.assertEqual(
            sorted(out), ["gui_origin", "ok", "operation", "pin_cli", "pinned",
                          "reason", "running", "service"])

        for op in ("update", "restart", "update-harnesses"):
            status, headers, out = self.post(op)
            self.assertEqual(status, 409, (op, out))
            self.assertEqual(out["error"]["code"], "gui_origin_unpinned")
            self.assertEqual(out["error"]["details"]["pin_cli"], "./sc engine-ops-pin")
            self.assertEqual(headers["Access-Control-Allow-Origin"], GUI)
        # Whatever loopback page asks, the answer is the same refusal.
        status, _headers, out = self.post("update", origin="http://127.0.0.1:8813")
        self.assertEqual(status, 409, out)
        status, _headers, out = self.post("update", origin="https://attacker.example")
        self.assertEqual(status, 403, out)
        status, _headers, out = self.call("POST", "/ops/update")
        self.assertEqual(status, 403, out)
        self.assert_nothing_ran()

    def test_instance_config_never_becomes_the_pin(self):
        """The reviewer's case: before the first pin the sandbox points the
        review port at its own dev server. Nothing is authorised by that."""
        (self.root / ".super-coder" / "instance.json").write_text(json.dumps(
            {"repo": "root", "port": 8813, "dev_port": 8813}))
        with mock.patch.object(ports, "REPO_ROOT", self.root), \
                mock.patch.object(ports, "CONFIG",
                                  self.root / ".super-coder" / "instance.json"), \
                mock.patch.object(ports, "_free", return_value=True), \
                mock.patch.object(ports, "_sibling_ports", return_value=set()):
            config = engine_ops.ensure_config()
            self.assertIsNone(config["gui_port"])
            self.assertIsNone(engine_ops.pinned_gui_port())
            for origin in ("http://127.0.0.1:8813", GUI):
                status, _headers, out = self.post("update", origin=origin)
                self.assertEqual(status, 409, (origin, out))
                self.assertEqual(out["error"]["code"], "gui_origin_unpinned")
            # A value with no operator act behind it (what 33157d82 stored on
            # first use) is not a pin either.
            engine_ops.write_private(self.private / "config.json",
                                     {"port": config["port"], "gui_port": 8813})
            self.assertIsNone(engine_ops.pinned_gui_port())
            status, _headers, out = self.post("update", origin="http://127.0.0.1:8813")
            self.assertEqual(status, 409, out)
            self.assertIsNone(engine_ops.ensure_config()["gui_port"])
        self.assert_nothing_ran()

    def test_pin_takes_effect_without_restarting_the_service(self):
        operator_pins(self.private)
        status, _headers, out = self.status()
        self.assertIs(out["pinned"], True)
        self.assertEqual(out["gui_origin"], GUI)
        status, _headers, out = self.post("update", origin="http://127.0.0.1:8813")
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["code"], "same_origin_required")
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)


class PinTransitionTest(ServiceFixture):
    """The origin decision is made once, against the pin, at the point of
    acting. A request admitted while no pin existed gets nothing once one does."""

    pinned = False
    DEV = "http://127.0.0.1:8813"      # the sandbox's published dev server

    def pin_lands_before(self, name: str):
        """Make the operator's pin land after the request was admitted and
        just before `engine_ops.<name>` runs — the transition's worst moment."""
        real = getattr(engine_ops, name)

        def then(*args, **kwargs):
            operator_pins(self.private)
            return real(*args, **kwargs)

        return mock.patch.object(engine_ops, name, side_effect=then)

    def test_post_admitted_before_the_pin_starts_nothing_once_pinned(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)}), \
                self.pin_lands_before("start"):
            status, _headers, out = self.post("update", origin=self.DEV)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["code"], "same_origin_required")
        time.sleep(0.5)
        self.assert_nothing_ran()
        self.assertEqual(engine_ops.pinned_gui_port(), GUI_PORT)

    def test_status_admitted_before_the_pin_returns_nothing_once_pinned(self):
        with self.pin_lands_before("status"):
            status, _headers, out = self.call("GET", "/status", origin=self.DEV)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["code"], "same_origin_required")
        self.assertNotIn("runtime", out)

    def test_the_gui_origin_crossing_the_same_transition_is_served(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)}), \
                self.pin_lands_before("start"):
            status, _headers, out = self.post("update")
        self.assertEqual(status, 202, out)
        self.finished()
        self.assertEqual(self.stub_argv(), ["noop"])

    def test_start_decides_the_origin_itself(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)}):
            with self.assertRaises(engine_ops.Unpinned):
                engine_ops.start("update", GUI)
            operator_pins(self.private)
            for origin in (self.DEV, None, "", "null", f"https://127.0.0.1:{GUI_PORT}",
                           GUI + "/", GUI_PORT):
                with self.assertRaises(engine_ops.ForeignOrigin, msg=repr(origin)):
                    engine_ops.start("update", origin)
            with self.assertRaises(engine_ops.ForeignOrigin):
                engine_ops.status(self.DEV)
            self.assert_nothing_ran()
            self.assertEqual(engine_ops.status(GUI)["pinned"], True)
            self.assertEqual(engine_ops.status(None)["pinned"], True)
            self.assertEqual(engine_ops.start("update", GUI)["op"], "update")
            self.finished()


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
        self.assertEqual(operation["log"], str(self.private / "engine-op-update.log"))
        self.assertIn("stub failure detail", out["log_tail"])
        self.assertIn("finished with exit code 3", out["log_tail"])
        self.assertFalse(out["log_truncated"])
        status, _headers, out = self.post("update-harnesses")
        self.assertEqual(status, 202, out)

    def test_state_is_owner_only_and_outside_the_checkout(self):
        with mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)}):
            self.post("update")
            self.finished()
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        for name in ("engine-op.json", "engine-op-update.log"):
            self.assertEqual(stat.S_IMODE((self.private / name).stat().st_mode), 0o600)
        self.assertNotIn(self.root, self.private.parents)
        # Nothing of the operation is written under the engine directory.
        written = sorted(p.name for p in (self.root / ".super-coder").rglob("*")
                         if p.is_file())
        self.assertEqual(written, ["instance.json"])

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
        engine_ops.write_private(self.status_file, {
            "op": "update", "state": "running", "pid": dead.pid,
            "pid_start_ticks": 1, "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None, "exit_code": None, "detach": "session",
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
        for field in ("pid", "cmd_pid"):
            engine_ops.write_private(self.status_file, {
                "op": "restart", "state": "running", field: os.getpid(),
                f"{field}_start_ticks" if field == "pid" else "cmd_start_ticks": 1,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None, "exit_code": None, "detach": "session",
            })
            _status, _headers, out = self.status()
            self.assertFalse(out["running"], field)
            self.assertEqual(out["operation"]["state"], "interrupted")

    def test_record_from_before_a_reboot_does_not_block(self):
        if engine_ops.boot_id() is None:
            self.skipTest("no boot id on this platform")
        engine_ops.write_private(self.status_file, {
            "op": "restart", "state": "running", "pid": os.getpid(),
            "pid_start_ticks": engine_ops.pid_start_ticks(os.getpid()),
            "boot_id": "00000000-0000-0000-0000-000000000000",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None, "exit_code": None, "detach": "session",
        })
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "interrupted")

    def test_stale_claim_without_a_runner_expires(self):
        engine_ops.write_private(self.status_file, {
            "op": "update", "state": "starting", "pid": None,
            "started_at": "2026-01-01T00:00:00Z", "finished_at": None,
            "exit_code": None, "detach": "session",
        })
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "interrupted")

    def test_runner_that_cannot_start_is_reported_and_does_not_block(self):
        with mock.patch.object(engine_ops, "_spawn", side_effect=OSError("no exec")):
            status, _headers, out = self.post("update")
        self.assertEqual(status, 500, out)
        self.assertEqual(out["error"]["code"], "engine_op_start_failed")
        _status, _headers, out = self.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["operation"]["state"], "finished")
        self.assertIsNone(out["operation"]["exit_code"])
        self.assertIn("no exec", out["operation"]["error"])


class ForgedStateTest(ServiceFixture):
    """The service never reads a path from a record and never follows a link."""

    def setUp(self) -> None:
        super().setUp()
        self.victim = self.base / "host-secret.txt"
        self.victim.write_text("HOST SECRET CONTENT\n")

    def assert_victim_untouched(self):
        self.assertEqual(self.victim.read_text(), "HOST SECRET CONTENT\n")

    def test_status_never_opens_a_path_named_by_a_record(self):
        forged = {
            "op": "update", "state": "finished", "pid": 1,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z", "exit_code": 0,
            "log": str(self.victim), "detach": "session",
        }
        # Where the status used to live, inside the checkout: not read at all.
        old = self.root / ".super-coder" / "logs" / "engine-op.json"
        old.write_text(json.dumps(forged))
        _status, _headers, out = self.status()
        self.assertIsNone(out["operation"])
        self.assertIsNone(out["log_tail"])

        # Even a record in the private directory cannot name a file to read.
        engine_ops.write_private(self.status_file, forged)
        _status, _headers, out = self.status()
        self.assertEqual(out["operation"]["log"],
                         str(self.private / "engine-op-update.log"))
        self.assertIsNone(out["log_tail"])
        self.assertNotIn("HOST SECRET", json.dumps(out))

        for op in ("../../host-secret", "/etc/hostname", "update/../x", 7, None):
            engine_ops.write_private(self.status_file, {**forged, "op": op})
            _status, _headers, out = self.status()
            self.assertIsNone(out["log_tail"], op)
            self.assertIsNone(out["operation"]["log"], op)
            self.assertNotIn("HOST SECRET", json.dumps(out))

    def test_log_and_status_writes_do_not_follow_links(self):
        # A link where the log used to be written, inside the checkout.
        planted = self.root / ".super-coder" / "logs" / "engine-op-update.log"
        planted.symlink_to(self.victim)
        with mock.patch.dict(engine_ops.VERBS, {"update": ("fail",)}):
            status, _headers, out = self.post("update")
            self.assertEqual(status, 202, out)
            self.finished()
        self.assert_victim_untouched()
        self.assertTrue(planted.is_symlink())

        # And a link at the real log path is refused, not followed.
        log = self.private / "engine-op-restart.log"
        log.symlink_to(self.victim)
        status, _headers, out = self.post("restart")
        self.assertEqual(status, 500, out)
        self.assert_victim_untouched()
        _status, _headers, out = self.status()
        self.assertIsNone(out["log_tail"])
        self.assertNotIn("HOST SECRET", json.dumps(out))

        status_link = self.private / "engine-op.json"
        status_link.unlink()
        status_link.symlink_to(self.victim)
        _status, _headers, out = self.status()
        self.assertIsNone(out["operation"])
        self.assert_victim_untouched()

    def test_published_hint_never_follows_a_link(self):
        run = self.root / ".super-coder" / "run"
        hint = run / "engine-ops.json"
        engine_ops.register_service(self.port)
        self.assertEqual(json.loads(hint.read_text())["port"], self.port)

        # The hint file replaced by a link: replaced back, target untouched.
        hint.unlink()
        hint.symlink_to(self.victim)
        engine_ops.register_service(self.port)
        self.assert_victim_untouched()
        self.assertFalse(hint.is_symlink())

        # Its predictable temporary name planted as a link.
        (run / f".engine-ops.json.{os.getpid()}.tmp").symlink_to(self.victim)
        engine_ops.register_service(self.port)
        self.assert_victim_untouched()

        # The run directory itself replaced by a link to a host directory.
        outside = self.base / "host-dir"
        outside.mkdir()
        shutil.rmtree(run)
        run.symlink_to(outside, target_is_directory=True)
        engine_ops.register_service(self.port)
        engine_ops.unregister_service()
        self.assertEqual(list(outside.iterdir()), [])

    def test_engine_ref_is_read_without_following_a_link(self):
        state = self.root / ".sc-state"
        state.mkdir()
        (state / "engine.ref").write_text("a" * 40 + "\n")
        self.assertEqual(engine_ops.engine_ref(), "a" * 40)
        (state / "engine.ref").unlink()
        (state / "engine.ref").symlink_to(self.victim)
        self.assertIsNone(engine_ops.engine_ref())
        (state / "engine.ref").unlink()
        (state / "engine.ref").write_text("HOST SECRET CONTENT with spaces\n")
        self.assertIsNone(engine_ops.engine_ref())
        shutil.rmtree(state)
        state.symlink_to(self.base, target_is_directory=True)
        (self.base / "engine.ref").write_text("b" * 40)
        self.assertIsNone(engine_ops.engine_ref())

    def test_private_directory_must_be_ours_and_real(self):
        link = self.base / "private-link"
        link.symlink_to(self.private, target_is_directory=True)
        with self.assertRaises(engine_ops.StateError):
            engine_ops.private_dir(link)
        loose = self.base / "loose"
        loose.mkdir(mode=0o755)
        engine_ops.private_dir(loose)
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o700)

    def test_state_directory_inside_the_sandbox_mounts_is_refused(self):
        engine = self.root / ".super-coder"
        before = sorted(str(p) for p in self.root.rglob("*"))
        link = self.base / "state-link"
        link.symlink_to(engine / "run", target_is_directory=True)
        state_home = self.base / "state-home"
        instance_state_dir = state_home / "subfloor" / "instances" / "abc"
        instance_state_dir.mkdir(parents=True)
        cases = {
            "an XDG state home inside the checkout": (None, engine / "state"),
            "a state home that is a link into the checkout": (None, link),
        }
        for label, (_override, home) in cases.items():
            engine_ops._host_private_checked.clear()
            with mock.patch.object(engine_ops, "PRIVATE_DIR", None), \
                    mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(home)}):
                with self.assertRaises(engine_ops.StateError, msg=label) as raised:
                    engine_ops.private_dir()
                self.assertIn("mounted into the sandbox", str(raised.exception))
                for call in (engine_ops.ensure_config, engine_ops.status_path,
                             lambda: engine_ops.start("update", GUI),
                             lambda: engine_ops.pin_gui_port(GUI_PORT)):
                    with self.assertRaises(engine_ops.StateError, msg=label):
                        call()
                self.assertIsNone(engine_ops.pinned_gui_port())
        # A directory handed in directly gets the same check, and so does the
        # instance state directory the launcher mounts beside the checkout.
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
            for bad in (engine / "run", engine / "logs" / "new", link,
                        instance_state_dir, instance_state_dir / "engine-ops"):
                engine_ops._host_private_checked.clear()
                with self.assertRaises(engine_ops.StateError, msg=str(bad)):
                    engine_ops.private_dir(bad)
                with mock.patch.object(engine_ops, "PRIVATE_DIR", bad):
                    with self.assertRaises(engine_ops.StateError, msg=str(bad)):
                        engine_ops.status_path()
            engine_ops._host_private_checked.clear()
            with mock.patch.object(engine_ops, "PRIVATE_DIR", None):
                good = engine_ops.private_dir()
            self.assertEqual(good.parent, state_home / "subfloor" / "engine-ops")
        # Nothing was created under the checkout by any refused attempt.
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)
        self.assertFalse((instance_state_dir / "engine-ops").exists())

    def home_with_profile_link(self):
        """A home whose `.claude.json` is a link to a directory elsewhere —
        the launcher mounts that path writable, whatever it resolves to."""
        home = self.base / "home"
        home.mkdir()
        target = self.base / "profile-target"
        target.mkdir()
        (home / ".claude.json").symlink_to(target, target_is_directory=True)
        return home, target

    def test_state_reached_through_the_mounted_claude_profile_is_refused(self):
        home, target = self.home_with_profile_link()
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.dict(os.environ, {
                    "HOME": str(home),
                    "XDG_STATE_HOME": str(self.base / "state-home")}):
            for bad in (target / "state", target):
                engine_ops._host_private_checked.clear()
                with self.assertRaises(engine_ops.StateError, msg=str(bad)):
                    engine_ops.private_dir(bad)
            # An XDG state home that resolves there is refused the same way.
            link = self.base / "state-link"
            link.symlink_to(target, target_is_directory=True)
            engine_ops._host_private_checked.clear()
            with mock.patch.object(engine_ops, "PRIVATE_DIR", None), \
                    mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(link)}):
                with self.assertRaises(engine_ops.StateError):
                    engine_ops.private_dir()
        self.assertEqual(list(target.iterdir()), [])

    def test_mount_source_inside_the_state_directory_is_refused(self):
        """The other direction: the state directory is not inside a mount, a
        mount is inside it. `~/.claude.json`, mounted writable, is a link to
        the service's own `config.json`."""
        home = self.base / "home"
        home.mkdir()
        (home / ".claude.json").symlink_to(self.private / "config.json")
        env = {"HOME": str(home), "XDG_STATE_HOME": str(self.base / "state-home")}
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.dict(os.environ, env):
            engine_ops._host_private_checked.clear()
            with self.assertRaises(engine_ops.StateError) as raised:
                engine_ops.private_dir()
            self.assertIn("mounted into the sandbox", str(raised.exception))
            # Nothing that trusts the directory proceeds: the pin a sandbox
            # could rewrite through that mount is not read.
            self.assertIsNone(engine_ops.pinned_gui_port())
            for call in (engine_ops.ensure_config, engine_ops.status_path,
                         lambda: engine_ops.pin_gui_port(GUI_PORT)):
                with self.assertRaises(engine_ops.StateError):
                    call()
            # A mounted directory inside the state directory, likewise.
            (home / ".claude.json").unlink()
            inner = self.private / "harness-home"
            inner.mkdir()
            (home / ".codex").symlink_to(inner, target_is_directory=True)
            engine_ops._host_private_checked.clear()
            with self.assertRaises(engine_ops.StateError):
                engine_ops.private_dir()
            # And the state directory itself as the mount.
            (home / ".codex").unlink()
            (home / ".vibe").symlink_to(self.private, target_is_directory=True)
            engine_ops._host_private_checked.clear()
            with self.assertRaises(engine_ops.StateError):
                engine_ops.private_dir()
            (home / ".vibe").unlink()
            engine_ops._host_private_checked.clear()
            self.assertEqual(engine_ops.private_dir(), self.private)

    def test_a_mount_that_starts_resolving_into_the_state_is_noticed(self):
        home = self.base / "home"
        home.mkdir()
        env = {"HOME": str(home), "XDG_STATE_HOME": str(self.base / "state-home")}
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.dict(os.environ, env):
            engine_ops._host_private_checked.clear()
            self.assertEqual(engine_ops.private_dir(), self.private)
            (home / ".claude.json").symlink_to(self.private / "config.json")
            with mock.patch.object(engine_ops, "HOST_PRIVATE_RECHECK_SECONDS", 0.0):
                with self.assertRaises(engine_ops.StateError):
                    engine_ops.private_dir()

    def test_interpreter_mount_over_the_state_directory_is_refused(self):
        """A checkout venv whose interpreter is `~/.local/bin/python` makes the
        launcher mount `~/.local` read-only — and the default state directory
        is under `~/.local/state`. Read-only still leaks."""
        home = self.base / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        (home / ".local" / "bin" / "python").write_text("")
        venv = self.root / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(home / ".local" / "bin" / "python")
        env = {"HOME": str(home)}
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.object(engine_ops, "PRIVATE_DIR", None), \
                mock.patch.dict(os.environ, env):
            os.environ.pop("XDG_STATE_HOME", None)
            engine_ops._host_private_checked.clear()
            with self.assertRaises(engine_ops.StateError) as raised:
                engine_ops.private_dir()
            self.assertIn(str(home / ".local"), str(raised.exception))
            self.assertFalse((home / ".local" / "state").exists())
            # A state home outside that mount is accepted.
            os.environ["XDG_STATE_HOME"] = str(self.base / "state-home")
            engine_ops._host_private_checked.clear()
            good = engine_ops.private_dir()
            self.assertEqual(good.parent,
                             self.base / "state-home" / "subfloor" / "engine-ops")

    def test_service_refuses_to_start_with_its_state_inside_the_checkout(self):
        state_home = ENGINE.parent / ".sc-state" / f"engine-ops-test-{os.getpid()}"
        self.assertFalse(state_home.exists())
        env = {**os.environ, "XDG_STATE_HOME": str(state_home)}
        env.pop("SC_SANDBOX", None)
        if engine_ops.in_sandbox():
            self.skipTest("this test host is itself a container")
        for script, args in (("engine_ops_service.py", []),
                             ("engine_ops.py", ["up"]),
                             ("engine_ops.py", ["port"]),
                             ("engine_ops.py", ["pin", "8800"])):
            result = subprocess.run(
                [sys.executable, str(ENGINE / "api" / script), *args],
                env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertNotEqual(result.returncode, 0, (script, args, result.stdout))
            self.assertIn("mounted into the sandbox", result.stderr, (script, args))
            self.assertNotIn("listening", result.stderr)
        self.assertFalse(state_home.exists())

    def test_default_private_directory_is_derived_from_the_path_not_the_checkout(self):
        home = self.base / "state-home"
        with mock.patch.object(engine_ops, "PRIVATE_DIR", None), \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(home)}):
            first = engine_ops.private_dir()
            # Rewriting the instance config (id, ports, anything) moves nothing.
            (self.root / ".super-coder" / "instance.json").write_text(
                json.dumps({"instance_id": "forged", "port": 1}))
            self.assertEqual(engine_ops.private_dir(), first)
        self.assertEqual(first.parent, home / "subfloor" / "engine-ops")
        self.assertTrue(first.name.startswith("root-"))
        self.assertNotIn(self.root, first.parents)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)


class LauncherMountParityTest(ServiceFixture):
    """The launcher's mounts and the state-directory exclusion are one list."""

    def launch_block(self) -> list[str]:
        """The `docker run` argument lines of the dispatcher's sandbox launch."""
        text = (ENGINE / "scripts" / "dispatch.sh").read_text()
        start = text.index('"$PY" "$S/sandbox_devkit.py" launch-container')
        end = text.index('"$IMG" ./sc serve', start)
        return [line.strip().rstrip("\\").strip()
                for line in text[start:end].splitlines()[1:]]

    def test_dispatcher_names_no_mount_outside_the_shared_list(self):
        lines = self.launch_block()
        self.assertEqual(lines.count("SC_HOME_MOUNTS"), 1)
        # Every way the block can mount something is accounted for: the
        # checkout, three computed variables, and the shared marker.
        literal = [line for line in lines
                   if line.startswith(("-v", "--volume", "--mount", "--tmpfs"))]
        self.assertEqual(literal, ['-v "$here:$here"'], (
            "a bind mount was added to the launcher's docker run. Add it to "
            "sandbox_devkit.HOME_BIND_MOUNTS (or teach "
            "engine_ops.sandbox_visible_roots about it) instead."))
        variables = sorted(line for line in lines if line.startswith("$") and "mount" in line)
        self.assertEqual(
            variables, ["$py_mount", "$state_mount", "$state_namespace_mounts"])
        for line in lines:
            self.assertNotIn("$HOME/.", line.replace('HOME="$HOME"', ""), line)
        # The interpreter mount is read-only, and the state mount is this
        # install's directory in the instance collection.
        text = (ENGINE / "scripts" / "dispatch.sh").read_text()
        py = [line.strip() for line in text.splitlines()
              if line.strip().startswith("py_mount=") and "-v" in line]
        self.assertTrue(py)
        for line in py:
            self.assertTrue(line.endswith(':ro"'), line)
        self.assertIn(
            'state_target="$HOME/.local/state/subfloor/instances/$(basename "$state_dir")"',
            text)

    def test_the_marker_expands_to_exactly_the_shared_list(self):
        import sandbox_devkit

        home = self.base / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            expanded = sandbox_devkit.expand_home_mounts(
                ["-d", "SC_HOME_MOUNTS", "SC_DEVKIT_MOUNTS", "image"])
        mounts = []
        for relative in sandbox_devkit.HOME_BIND_MOUNTS:
            mounts += ["-v", f"{home / relative}:{home / relative}"]
        self.assertEqual(expanded, ["-d", *mounts, "SC_DEVKIT_MOUNTS", "image"])
        self.assertIn(".claude.json", sandbox_devkit.HOME_BIND_MOUNTS)
        self.assertEqual(sandbox_devkit.expand_home_mounts(["a", "b"]), ["a", "b"])

    def test_every_launcher_mount_is_excluded_and_a_new_one_follows(self):
        import sandbox_devkit

        home = self.base / "home"
        home.mkdir()
        env = {"HOME": str(home), "XDG_STATE_HOME": str(self.base / "state-home")}
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.dict(os.environ, env):
            roots = engine_ops.sandbox_visible_roots()
            for relative in sandbox_devkit.HOME_BIND_MOUNTS:
                self.assertIn(Path(os.path.realpath(home / relative)), roots)
                engine_ops._host_private_checked.clear()
                with self.assertRaises(engine_ops.StateError, msg=relative):
                    engine_ops.private_dir(home / relative / "state")
            # A harness added to the launcher tomorrow: mounted and excluded
            # by the same edit.
            fresh = home / ".newharness" / "state"
            engine_ops._host_private_checked.clear()
            engine_ops.private_dir(home / ".unmounted")
            with mock.patch.object(
                    sandbox_devkit, "HOME_BIND_MOUNTS",
                    (*sandbox_devkit.HOME_BIND_MOUNTS, ".newharness")):
                self.assertIn(
                    f"{home / '.newharness'}:{home / '.newharness'}",
                    sandbox_devkit.expand_home_mounts(["SC_HOME_MOUNTS"]))
                engine_ops._host_private_checked.clear()
                with self.assertRaises(engine_ops.StateError):
                    engine_ops.private_dir(fresh)
            # If the launcher's list cannot be read, nothing is trusted.
            engine_ops._host_private_checked.clear()
            with mock.patch.object(sandbox_devkit, "home_bind_paths",
                                   side_effect=RuntimeError("unreadable")):
                with self.assertRaises(engine_ops.StateError):
                    engine_ops.private_dir(self.base / "anywhere")


class LauncherGuardTest(ServiceFixture):
    """The launcher refuses, at the one place that sees every mount, a
    `docker run` whose binds overlap the engine-ops state in either direction.
    It reads the option region with the launcher's own grammar, not Docker's
    whole command line: an option it does not know is refused, and nothing
    after the image is looked at."""

    def setUp(self) -> None:
        super().setUp()
        import sandbox_devkit

        self.devkit = sandbox_devkit
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}
        self.state = sandbox_devkit.engine_ops_state_dir(self.root, self.env)

    def check(self, arguments) -> None:
        with mock.patch.object(os, "getcwd", return_value=str(self.base)):
            self.devkit.guard_engine_ops_state(list(arguments), self.root, self.env)

    def guard(self, *arguments: str) -> None:
        self.check(["docker", "run", "-d", *arguments, "image", "./sc", "serve"])

    def refused(self, *arguments: str) -> str:
        with self.assertRaises(self.devkit.SandboxImageError, msg=arguments) as raised:
            self.guard(*arguments)
        self.assertIn("refusing to launch", str(raised.exception))
        return str(raised.exception)

    def allowed(self, arguments) -> None:
        try:
            self.check(arguments)
        except self.devkit.SandboxImageError as exc:
            self.fail(f"{arguments} was refused: {exc}")

    def test_state_directory_is_the_one_the_service_uses(self):
        self.assertEqual(
            self.state.parent, self.home / ".local" / "state" / "subfloor" / "engine-ops")
        self.assertFalse(self.state.exists())
        with mock.patch.object(engine_ops, "PRIVATE_DIR", None), \
                mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.dict(os.environ, self.env):
            os.environ.pop("XDG_STATE_HOME", None)
            engine_ops._host_private_checked.clear()
            self.assertEqual(engine_ops.private_dir(), self.state)
        custom = self.devkit.engine_ops_state_dir(
            self.root, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.base / "x")})
        self.assertEqual(custom.parent, self.base / "x" / "subfloor" / "engine-ops")

    # ── the three bypasses of the looser parser, each by name ────────────────

    def test_trailing_whitespace_in_a_mount_value_is_a_bind_of_the_state(self):
        """Docker trims the whole `--mount` value before reading it."""
        self.state.mkdir(parents=True)
        for value in (f"type=bind,target=/leak,source={self.state} ",
                      f"type=bind,target=/leak,source={self.state}\t",
                      f"  type=bind,source={self.state},target=/leak\n"):
            self.assertIn("overlaps", self.refused("--mount", value))

    def test_short_flag_cluster_with_an_attached_value_is_refused(self):
        """pflag reads `-dvSRC:DST` as `-d -v SRC:DST`. The launcher never
        writes that, so the guard does not try to: it is not in the table."""
        self.state.mkdir(parents=True)
        for argument in (f"-dv{self.state}:/leak", f"-dv={self.state}:/leak",
                         f"-itv{self.state}:/leak", f"-v{self.state}:/leak",
                         f"-v={self.state}:/leak"):
            message = self.refused(argument)
            self.assertIn(repr(argument), message)
            self.assertIn("RUN_OPTIONS", message)

    def test_container_command_arguments_are_never_inspected(self):
        """Everything from the image on is the container's command."""
        self.state.mkdir(parents=True)
        for arguments in (
            ["docker", "run", "image", "tool", "-v"],
            ["docker", "run", "image", "tool", "--", "-v"],
            ["docker", "run", "-d", "--name", "c", "image", "tool", "--mount",
             "garbage", "-dv", f"{self.state}:/x", "--volumes-from", "y",
             "--no-such-option", f"--mount=type=bind,src={self.state},dst=/z"],
            ["image", "./sc", "serve", "--port", "8800"],
        ):
            self.allowed(arguments)
            self.assertEqual(self.devkit.bind_sources(arguments), [])
        # A mount before the image is still a mount, whatever follows.
        with self.assertRaises(self.devkit.SandboxImageError):
            self.check(["docker", "run", "-v", f"{self.state}:/leak", "image",
                        "tool", "-v"])
        self.assertEqual(
            self.devkit.run_options(["docker", "run", "-d", "-e", "A=1", "img", "-v", "x"]),
            [("-d", None), ("-e", "A=1")])

    # ── values, read the way Docker reads them ───────────────────────────────

    def test_quoted_csv_source_field_is_a_bind_of_the_state(self):
        self.state.mkdir(parents=True)
        self.assertIn("overlaps", self.refused(
            "--mount", f'type=bind,"source={self.state}",target=/leak'))

    def test_capitalised_keys_and_type_are_a_bind_of_the_state(self):
        self.state.mkdir(parents=True)
        self.assertIn("overlaps", self.refused(
            "--mount", f"Type=bind,Source={self.state},target=/leak"))

    def test_dot_relative_source_is_a_bind_of_the_state(self):
        self.state.mkdir(parents=True)
        relative = os.path.relpath(self.state, self.base)
        self.assertIn("overlaps", self.refused(
            "--mount", f"type=bind,source=./{relative},target=/leak"))

    def test_mount_values_are_read_the_way_docker_reads_them(self):
        self.state.mkdir(parents=True)
        cwd = self.base
        relative = os.path.relpath(self.state, cwd)
        forms = {
            "a quoted CSV field": f'type=bind,"source={self.state}",target=/leak',
            "capitalised keys and type": f"Type=bind,Source={self.state},target=/leak",
            "upper-case type value": f"type=BIND,SRC={self.state},DST=/leak",
            "a dot-relative source": f"type=bind,source=./{relative},target=/leak",
            "a bare relative source": f"type=bind,source={relative},target=/leak",
            "a parent-relative source":
                f"type=bind,source=./x/../{relative},target=/leak,readonly",
            "every field quoted":
                f'"type=bind","src={self.state}","dst=/leak",ro',
            "padded with whitespace": f" type=bind,src={self.state},dst=/leak \t",
        }
        for label, value in forms.items():
            self.assertIn("overlaps", self.refused("--mount", value), label)
            self.assertEqual(
                self.devkit.bind_sources(["docker", "run", "--mount", value, "img"], cwd),
                [str(self.state)], label)
        # -v: a source starting with `.` is a path against the working directory.
        for value in (f"./{relative}:/leak", f"./{relative}/../{self.state.name}:/leak:ro"):
            self.assertIn("overlaps", self.refused("-v", value), value)
            self.assertEqual(
                self.devkit.bind_sources(["docker", "run", "-v", value, "img"], cwd),
                [str(self.state)], value)

    def test_bind_sources_reads_the_launchers_spellings(self):
        arguments = [
            "docker", "run", "-d", "--name", "c",
            "-v", "/a:/a", "-v", "/b:/b:ro",
            "--mount", "type=bind,src=/g,dst=/g",
            "--mount", "type=bind,source=/h,target=/h,readonly",
            # Not binds: named and anonymous volumes, tmpfs.
            "-v", "named-volume:/data", "-v", "/anonymous",
            "--mount", "type=volume,src=named,dst=/sc-devkit-volume",
            "--mount", "type=tmpfs,dst=/t", "--mount", "src=named,dst=/n",
            "--tmpfs", "/u:uid=0",
            "-p", "127.0.0.1:1:1", "-e", "HOME=/home/x", "-w", "/a",
            "image", "./sc", "serve",
        ]
        self.assertEqual(self.devkit.bind_sources(arguments), ["/a", "/b", "/g", "/h"])

    # ── fail closed ──────────────────────────────────────────────────────────

    def test_an_option_outside_the_table_is_refused_and_named(self):
        for arguments in (
            ["--cap-add", "SYS_ADMIN"], ["--privileged"], ["--volumes-from", "other"],
            ["--volume", "/a:/b"], ["--mount=type=bind,src=/a,dst=/b"],
            ["--volume=/a:/b"], ["-v=/a:/b"], ["-v/a:/b"], ["-dv", "/a:/b"],
            ["-it"], ["--name=c"], ["-eA=1"], ["--"], ["-"],
        ):
            message = self.refused(*arguments)
            self.assertIn(repr(arguments[0]), message)
            self.assertIn("sandbox_devkit.RUN_OPTIONS", message)
        # A table option with its value missing.
        for option in ("-v", "--mount", "-e", "--name"):
            with self.assertRaises(self.devkit.SandboxImageError) as raised:
                self.devkit.bind_sources(["docker", "run", "-d", option])
            self.assertIn("has no value", str(raised.exception))

    def test_what_cannot_be_classified_is_refused(self):
        unclassifiable = [
            ["--mount", 'type=bind,"source=/a,target=/b'],     # unterminated quote
            ["--mount", "type=bind,target=/b"],                # a bind with no source
            ["--mount", "type=bind,source=,target=/b"],        # an empty source
            ["--mount", "type=bind,source=/a,src=/b,target=/c"],
            ["--mount", "type=bind,source=/a ,target=/c"],     # a padded source
            ["--mount", "type=overlay,source=/a,target=/b"],   # no such type
            ["--mount", ""],
            ["--mount", "   "],
            ["--mount", "type=bind,source=/a"],                # no target
            ["--mount", "type=bind,source=/a,target=/b,whatever"],
            ["--mount", "source=/a,whatever,target=/b"],
            ["-v", ""],
            ["-v", ":/b"],                                     # an empty source
            ["-v", "~/state:/b"],                              # unexpanded home
            ["-v", " /a:/b"],                                  # padded
            ["-v", "/a :/b"],
            ["-v", "na me:/b"],                                # not a volume name
            ["-v", "relative-without-a-colon"],
        ]
        for arguments in unclassifiable:
            message = self.refused(*arguments)
            self.assertIn("cannot be checked", message)

    def test_what_the_launcher_really_produces_is_classified(self):
        """Fail-closed must not refuse the launcher's own arguments."""
        home = self.home
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            expanded = self.devkit.expand_home_mounts(["SC_HOME_MOUNTS"])
        arguments = [
            "docker", "run", "-d", "--name", "sc-x", "--restart", "unless-stopped",
            "--init", "--network", "sc-net", "--user", "1000:1000", "-e", "GH_TOKEN",
            "--mount", "type=bind,src=/run/user/1000/agent.sock,dst=/run/super-coder/ssh-agent,readonly",
            "-e", f"HOME={home}", "-e", "SC_SANDBOX=1",
            "-w", str(self.root), "-v", f"{self.root}:{self.root}",
            "--tmpfs", f"{home}/.local/state:uid=0,gid=0,mode=0700",
            "-v", f"{home}/.local/state/subfloor/instances/abc:{home}/.local/state/subfloor/instances/abc",
            "-v", f"{home}/.local/share/uv/python:{home}/.local/share/uv/python:ro",
            *expanded,
            "-p", "127.0.0.1:8800:8800", "-p", "127.0.0.1:8801:8801",
            "--memory", "1024", "--memory-swap", "1024",
            # The dev-kit volumes, exactly as docker_run spells them.
            "--mount", "type=volume,src=sc-devkit-abc,dst=/opt/devkit",
            "--mount", 'type=volume,src=sc-devkit-def,dst="/opt/with,comma"',
            "image", "./sc", "serve", "--port", "8800",
        ]
        sources = self.devkit.bind_sources(arguments)
        self.assertEqual(sources[:2], ["/run/user/1000/agent.sock", str(self.root)])
        self.assertEqual(len(sources), 4 + len(self.devkit.HOME_BIND_MOUNTS))
        # None of those paths exists; that is not a reason to refuse.
        self.assertFalse((home / ".claude").exists())
        self.allowed(arguments)

    def test_a_normal_install_launches_with_or_without_state(self):
        normal = []
        for relative in self.devkit.HOME_BIND_MOUNTS:
            normal += ["-v", f"{self.home / relative}:{self.home / relative}"]
        normal += ["-v", f"{self.root}:{self.root}",
                   "-v", f"{self.home}/.local/state/subfloor/instances/abc:/state",
                   "-v", f"{self.home}/.local/share/uv/python:/py:ro",
                   "--mount", "type=volume,src=devkit,dst=/sc-devkit-volume"]
        self.guard(*normal)                       # no engine-ops state yet
        self.state.mkdir(parents=True)
        (self.state / "config.json").write_text("{}")
        self.guard(*normal)                       # and with it

    def test_overlap_is_refused_in_both_directions(self):
        self.state.mkdir(parents=True)
        (self.state / "config.json").write_text("{}")
        profile = self.home / ".claude.json"
        profile.symlink_to(self.state / "config.json")
        link_home = self.home / "harness"
        link_home.symlink_to(self.state, target_is_directory=True)
        cases = {
            "a mounted file that is one of its files":
                ["-v", f"{profile}:{profile}"],
            "the directory itself, through a link":
                ["-v", f"{link_home}:/h"],
            "a directory containing it (read-only interpreter mount)":
                ["-v", f"{self.home}/.local:{self.home}/.local:ro"],
            "the state home": ["-v", f"{self.home}/.local/state:/s"],
            "the whole home, as a bind mount":
                ["--mount", f"type=bind,src={self.home},dst=/home"],
            "a directory inside it": ["-v", f"{self.state}/sub:/sub"],
        }
        for label, arguments in cases.items():
            message = self.refused(*arguments)
            self.assertIn("overlaps", message, label)
            self.assertIn(str(self.state), message)
            self.assertIn("XDG_STATE_HOME", message)

    def test_a_checkout_inside_reach_of_the_state_home_is_refused(self):
        inside = {"HOME": str(self.home),
                  "XDG_STATE_HOME": str(self.root / ".super-coder" / "state")}
        with self.assertRaises(self.devkit.SandboxImageError):
            self.devkit.guard_engine_ops_state(
                ["docker", "run", "-v", f"{self.root}:{self.root}", "image"],
                self.root, inside)

    def test_docker_run_applies_the_guard_to_its_final_arguments(self):
        text = (ENGINE / "scripts" / "sandbox_devkit.py").read_text()
        body = text[text.index("def docker_run("):text.index("def ", text.index("def docker_run(") + 10)]
        self.assertIn("guard_engine_ops_state(command, plan.engine.parent)", body)
        self.assertLess(body.index("guard_engine_ops_state(command"),
                        body.index("_run(command"))


class StopIdentityTest(ServiceFixture):
    """`down` signals only a process proven to be this service."""

    def setUp(self) -> None:
        super().setUp()
        self.canary = sleeper()
        self.addCleanup(reap, self.canary)

    def assert_canary_alive(self):
        time.sleep(0.2)
        self.assertIsNone(self.canary.poll())

    def test_a_pid_planted_in_the_checkout_is_never_signalled(self):
        run = self.root / ".super-coder" / "run"
        (run / "engine-ops.pid").write_text(f"{self.canary.pid}\n")
        (run / "engine-ops.json").write_text(json.dumps(
            {"pid": self.canary.pid, "port": self.port}))
        code, message = engine_ops.service_down()
        self.assertEqual(code, 0, message)
        self.assertIn("not running", message)
        self.assert_canary_alive()

    def test_a_recorded_pid_that_is_not_the_service_is_never_signalled(self):
        ticks = engine_ops.pid_start_ticks(self.canary.pid)
        if ticks is None:
            self.skipTest("no /proc start time on this platform")
        path = self.private / engine_ops.SERVICE_NAME
        for record in (
            # Right pid and start time, but the process is not the service.
            {"pid": self.canary.pid, "pid_start_ticks": ticks, "port": self.port},
            # A reused pid: alive, wrong start time.
            {"pid": self.canary.pid, "pid_start_ticks": ticks + 1, "port": self.port},
            # No start time recorded at all.
            {"pid": self.canary.pid, "port": self.port},
            {"pid": str(self.canary.pid), "pid_start_ticks": ticks},
            {"pid": -1, "pid_start_ticks": ticks},
        ):
            engine_ops.write_private(path, record)
            self.assertIsNone(engine_ops.service_record(), record)
            code, message = engine_ops.service_down()
            self.assertEqual(code, 0, message)
            self.assertNotIn("stopped", message)
            self.assert_canary_alive()

    def test_no_process_handle_means_no_signal(self):
        """If the service cannot be held by a handle, it is not signalled by
        number either: that is where a reused pid would get the signal."""
        real = self.base / "install" / "engine_ops_service.py"
        real.parent.mkdir()
        real.write_text("import time; time.sleep(600)\n")
        proc = subprocess.Popen([sys.executable, str(real)], start_new_session=True)
        self.addCleanup(reap, proc)
        with mock.patch.object(engine_ops, "SERVICE_SCRIPT", real):
            self.assertTrue(wait_for(lambda: engine_ops.runs_script(proc.pid, real)))
            engine_ops.write_private(self.private / engine_ops.SERVICE_NAME, {
                "pid": proc.pid,
                "pid_start_ticks": engine_ops.pid_start_ticks(proc.pid),
                "port": self.port})
            for failure in (OSError(24, "Too many open files"),
                            AttributeError("pidfd_open")):
                with mock.patch.object(os, "pidfd_open", side_effect=failure), \
                        mock.patch.object(os, "kill", wraps=os.kill) as kill:
                    code, message = engine_ops.service_down()
                self.assertEqual(code, 1, message)
                self.assertIn("could not be stopped safely", message)
                self.assertIn(f"kill {proc.pid}", message)
                # No real signal was sent by number (signal 0 is only a probe).
                self.assertEqual(
                    [call for call in kill.call_args_list if call.args[1] != 0], [])
                time.sleep(0.2)
                self.assertIsNone(proc.poll())
            # With a handle, the same service is stopped.
            code, message = engine_ops.service_down()
            self.assertEqual((code, message), (0, "→ engine-ops stopped"))

    def test_a_process_that_only_mentions_the_service_script_is_not_the_service(self):
        """Right pid, right start time, and the script's name on its command
        line: still not this install's service, so never signalled."""
        real = self.base / "install" / "engine_ops_service.py"
        real.parent.mkdir()
        real.write_text("import time; time.sleep(600)\n")
        elsewhere = self.base / "elsewhere" / "engine_ops_service.py"
        elsewhere.parent.mkdir()
        elsewhere.write_text("import time; time.sleep(600)\n")
        impostors = [
            # The name as a later argument.
            [sys.executable, "-c", "import time; time.sleep(600)", str(real)],
            [sys.executable, "-c", "import time; time.sleep(600)",
             "engine_ops_service.py"],
            # A same-named script that is not this install's.
            [sys.executable, str(elsewhere)],
            # Not an interpreter at all.
            ["tail", "-f", "/dev/null", str(real)],
        ]
        path = self.private / engine_ops.SERVICE_NAME
        with mock.patch.object(engine_ops, "SERVICE_SCRIPT", real):
            for argv in impostors:
                proc = subprocess.Popen(argv, start_new_session=True,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                self.addCleanup(reap, proc)
                ticks = wait_for(lambda: engine_ops.pid_start_ticks(proc.pid))
                self.assertTrue(wait_for(lambda: len(engine_ops._cmdline(proc.pid)) >= 2))
                engine_ops.write_private(path, {
                    "pid": proc.pid, "pid_start_ticks": ticks, "port": self.port})
                self.assertFalse(engine_ops.runs_script(proc.pid, real), argv)
                self.assertIsNone(engine_ops.service_record(), argv)
                code, message = engine_ops.service_down()
                self.assertEqual(code, 0, message)
                self.assertNotIn("stopped", message)
                time.sleep(0.2)
                self.assertIsNone(proc.poll(), argv)
            # The real thing is recognised, by absolute or relative path.
            for argv, cwd in (([sys.executable, str(real)], None),
                              ([sys.executable, "engine_ops_service.py", "x"],
                               str(real.parent))):
                proc = subprocess.Popen(argv, cwd=cwd, start_new_session=True)
                self.addCleanup(reap, proc)
                ticks = wait_for(lambda: engine_ops.pid_start_ticks(proc.pid))
                self.assertTrue(wait_for(lambda: engine_ops.runs_script(proc.pid, real)))
                engine_ops.write_private(path, {
                    "pid": proc.pid, "pid_start_ticks": ticks, "port": self.port})
                code, message = engine_ops.service_down()
                self.assertEqual((code, message), (0, "→ engine-ops stopped"))


class SandboxRefusalTest(unittest.TestCase):
    """Acceptance 6: the service refuses to start with SC_SANDBOX set."""

    def test_refuses_to_start_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as state_home:
            env = {**os.environ, "SC_SANDBOX": "1", "XDG_STATE_HOME": state_home}
            for script, args in (("engine_ops_service.py", []),
                                 ("engine_ops.py", ["port"]),
                                 ("engine_ops.py", ["alive"]),
                                 ("engine_ops.py", ["down"]),
                                 ("engine_ops.py", ["pin", "8800"]),
                                 ("engine_ops.py", ["pin", "--show"])):
                result = subprocess.run(
                    [sys.executable, str(ENGINE / "api" / script), *args],
                    env=env, capture_output=True, text=True, timeout=60, check=False)
                self.assertNotEqual(result.returncode, 0, (script, args))
                self.assertNotIn("listening", result.stderr)
            result = subprocess.run(
                [sys.executable, str(ENGINE / "api" / "engine_ops.py"), "up"],
                env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NOT started", result.stdout)
            # Nothing was allocated, pinned, or started.
            self.assertEqual(list(Path(state_home).iterdir()), [])

    def test_refusal_precedes_any_port_or_listener(self):
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}), \
                mock.patch.object(engine_ops, "ensure_config") as config, \
                mock.patch.object(engine_ops_service, "serve") as serve:
            with self.assertRaises(SystemExit) as raised:
                engine_ops_service.main([])
        self.assertIn("HOST", str(raised.exception.code))
        config.assert_not_called()
        serve.assert_not_called()

    def test_takes_no_arguments(self):
        with mock.patch.object(engine_ops, "in_sandbox", return_value=False), \
                mock.patch.object(engine_ops, "ensure_config") as config, \
                mock.patch.object(engine_ops_service, "serve") as serve:
            with self.assertRaises(SystemExit):
                engine_ops_service.main(["--port", "9999"])
            with self.assertRaises(SystemExit):
                engine_ops_service.main(["--bind", "0.0.0.0"])
        config.assert_not_called()
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


class PinnedConfigTest(unittest.TestCase):
    """The port and the GUI origin: derived like the review port, pinned once
    in private state, and not re-read from the instance config."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_engine_ops_cfg_")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "fork"
        self.config = self.root / ".super-coder" / "instance.json"
        self.config.parent.mkdir(parents=True)
        self.write_instance({"repo": "fork", "port": 8812, "dev_port": 8844})
        self.private = make_private(base)
        for patcher in (
            mock.patch.object(ports, "REPO_ROOT", self.root),
            mock.patch.object(ports, "CONFIG", self.config),
            mock.patch.object(ports, "_free", return_value=True),
            mock.patch.object(engine_ops, "REPO_ROOT", self.root),
            mock.patch.object(engine_ops, "PRIVATE_DIR", self.private),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        engine_ops._host_private_checked.clear()

    def write_instance(self, payload: dict) -> None:
        self.config.write_text(json.dumps(payload))

    def test_port_is_pinned_once_and_published(self):
        self.assertIsNone(ports.stored_engine_ops_port())
        config = engine_ops.ensure_config()
        port = config["port"]
        self.assertTrue(8800 <= port < 8900)
        self.assertNotIn(port, (8812, 8844))
        # Published for the review API …
        self.assertEqual(ports.stored_engine_ops_port(), port)
        stored = json.loads(self.config.read_text())
        self.assertEqual((stored["port"], stored["dev_port"]), (8812, 8844))
        # … and a listening service holding the port is no reason to move it.
        with mock.patch.object(ports, "_free", return_value=False):
            self.assertEqual(engine_ops.ensure_config(), config)
        # Editing the published value moves nothing; it is put back.
        self.write_instance({"repo": "fork", "port": 8812, "dev_port": 8844,
                             "engine_ops_port": 8899})
        self.assertEqual(engine_ops.ensure_config(), config)
        self.assertEqual(ports.stored_engine_ops_port(), port)
        self.config.unlink()
        self.assertEqual(engine_ops.ensure_config(), config)

    def test_gui_origin_is_never_taken_from_the_instance_config(self):
        config = engine_ops.ensure_config()
        self.assertIsNone(config["gui_port"])
        self.assertIsNone(engine_ops.pinned_gui_port())
        self.assertEqual(engine_ops.gui_origins(engine_ops.pinned_gui_port()),
                         frozenset())
        self.assertNotIn("8812", (self.private / "config.json").read_text())
        # It is only ever offered to the operator as a suggestion.
        self.assertEqual(engine_ops.suggested_gui_port(), 8812)

    def test_operator_pin_holds_against_the_instance_config(self):
        pinned = engine_ops.pin_gui_port(8812)
        self.assertEqual(pinned["gui_port"], 8812)
        self.assertTrue(pinned["pinned_at"])
        self.write_instance({"repo": "fork", "port": 8844, "dev_port": 8844})
        self.assertEqual(engine_ops.ensure_config()["gui_port"], 8812)
        self.assertEqual(engine_ops.pinned_gui_port(), 8812)
        self.assertEqual(engine_ops.gui_origins(8812),
                         frozenset({"http://127.0.0.1:8812", "http://localhost:8812"}))
        for bad in (0, 70000, "8812", True, None):
            with self.assertRaises(ValueError):
                engine_ops.pin_gui_port(bad)
        self.assertEqual(engine_ops.pinned_gui_port(), 8812)

    def run_pin(self, argv, *, tty=True, answer=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        answers = mock.Mock(side_effect=[answer] if answer is not None else EOFError)
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch("builtins.input", answers), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = engine_ops.pin_command(argv)
        return code, out.getvalue(), err.getvalue(), answers

    def test_pin_command_is_an_explicit_operator_act(self):
        code, out, _err, _asked = self.run_pin(["--show"])
        self.assertEqual(code, 1)
        self.assertIn("NOT pinned", out)

        # No terminal and no port: nothing is pinned, and nothing is guessed.
        code, _out, err, asked = self.run_pin([], tty=False)
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", err)
        asked.assert_not_called()
        self.assertIsNone(engine_ops.pinned_gui_port())

        # At a terminal the suggestion is shown, with where it came from, but
        # only a typed port pins. Enter, "yes", or anything else aborts.
        for answer in ("", "yes", "y", "8812 please"):
            code, out, _err, _asked = self.run_pin([], answer=answer)
            self.assertEqual(code, 1, answer)
            self.assertIn("http://127.0.0.1:8812", out)
            self.assertIn("writable from the sandbox", out)
            self.assertIn("aborted", out)
            self.assertIsNone(engine_ops.pinned_gui_port())

        code, out, _err, _asked = self.run_pin([], answer="8812")
        self.assertEqual(code, 0)
        self.assertIn("pinned: engine-ops accepts the GUI at http://127.0.0.1:8812", out)
        self.assertEqual(engine_ops.pinned_gui_port(), 8812)

        # The operator can see what is pinned, and change it deliberately.
        code, out, _err, _asked = self.run_pin(["--show"])
        self.assertEqual(code, 0)
        self.assertIn("accepts the GUI at http://127.0.0.1:8812", out)
        code, out, _err, asked = self.run_pin(["8830"], tty=False)
        self.assertEqual(code, 0)
        asked.assert_not_called()
        self.assertEqual(engine_ops.pinned_gui_port(), 8830)
        for bad in (["abc"], ["8812", "extra"], ["0"]):
            self.assertNotEqual(self.run_pin(bad)[0], 0)
        self.assertEqual(engine_ops.pinned_gui_port(), 8830)

    def test_avoids_this_forks_other_ports_and_siblings(self):
        seed = ports.PORT_BASE + ports._offset(str(self.root) + ":engine-ops")
        sibling = self.root.parent / "sibling" / ".super-coder" / "instance.json"
        sibling.parent.mkdir(parents=True)
        sibling.write_text(json.dumps({"port": seed, "dev_port": 8801}))
        port = engine_ops.ensure_config()["port"]
        self.assertNotIn(port, (seed, 8801, 8812, 8844))
        # And the published value is in turn avoided by sibling forks.
        other = self.root.parent / "other"
        with mock.patch.object(ports, "REPO_ROOT", other), \
                mock.patch.object(ports, "CONFIG",
                                  other / ".super-coder" / "instance.json"):
            self.assertIn(port, ports._sibling_ports())

    def test_an_unusable_pinned_value_is_replaced(self):
        for bad in (99999, "8850", True, None, 0):
            engine_ops.write_private(
                self.private / "config.json",
                {"port": bad, "gui_port": bad, "pinned_at": "2026-01-01T00:00:00Z"})
            config = engine_ops.ensure_config()
            self.assertTrue(8800 <= config["port"] < 8900, (bad, config))
            self.assertIsNone(config["gui_port"])
            self.assertIsNone(engine_ops.pinned_gui_port())


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
        base = Path(self.tmp.name)
        self.db_path = base / "engine.db"
        build_file_db(self.db_path)
        self.root = base / "root"
        write_stub_root(self.root)
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
            mock.patch.object(engine_ops, "REPO_ROOT", self.root),
            mock.patch.object(engine_ops, "PRIVATE_DIR", make_private(base)),
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

    def test_inside_the_sandbox_availability_comes_from_the_published_hint(self):
        os.environ["SC_SANDBOX"] = "1"
        with mock.patch.object(engine_ops, "_port_answers") as probe:
            status, out = self.get()
            self.assertIs(out["up"], False)
            self.assertEqual(out["up_source"], "state_file")
            engine_ops.register_service(self.port)
            status, out = self.get()
            self.assertIs(out["up"], True)
            engine_ops.register_service(self.port + 1)
            status, out = self.get()
            self.assertIs(out["up"], False)
            engine_ops.register_service(self.port)
            engine_ops.unregister_service()
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


class ServiceProcessFixture(unittest.TestCase):
    """The service in its own process, over a temporary checkout."""

    review_port = GUI_PORT

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.base = base
        self.root = base / "root"
        write_stub_root(self.root, self.review_port)
        self.private = make_private(base)
        self.status_file = self.private / engine_ops.STATUS_NAME
        launcher_dir = base / "launcher"
        launcher_dir.mkdir()
        self.script = launcher_dir / "engine_ops_service.py"
        self.script.write_text(SERVICE_LAUNCHER)
        (self.root / "engine.path").write_text(str(ENGINE))
        (self.root / "private.path").write_text(str(self.private))
        (self.root / "launcher.path").write_text(str(self.script))
        engine_ops._host_private_checked.clear()
        operator_pins(self.private, self.review_port)
        # The "review server": a process the stub verb stops, as the verbs do.
        self.review = sleeper()
        self.addCleanup(reap, self.review)
        (self.root / "review.pid").write_text(str(self.review.pid))
        for patcher in (
            mock.patch.object(engine_ops, "PRIVATE_DIR", self.private),
            mock.patch.object(engine_ops, "SERVICE_SCRIPT", self.script),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def service_argv(self, scope: str, verb: str) -> list[str]:
        return [sys.executable, str(self.script), str(ENGINE), str(self.root),
                str(self.private), scope, json.dumps([verb])]

    def start_service(self, scope: str = "session", verb: str = "noop"):
        (self.root / "service.port").unlink(missing_ok=True)
        proc = subprocess.Popen(
            self.service_argv(scope, verb), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(reap, proc)
        return proc

    def start_unit(self, scope: str) -> None:
        """Run the service as a transient user unit, the way the host review
        server is run."""
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


class OutlivesTheEngineTest(ServiceProcessFixture):
    """Acceptance 7: the operation outlives the review server and the
    service's own stop path."""

    def assert_recorded_after_the_engine_is_gone(self, port: int, detach: str) -> None:
        record = self.finished_record()
        # The stub exits 7 only once the review server is gone and nothing
        # answers the service's port, so this exit code was written by a
        # runner that outlived both.
        self.assertEqual(record["exit_code"], 7, record)
        self.assertEqual(record["op"], "restart")
        self.assertEqual(record["detach"], detach)
        self.assertTrue(record["started_at"] and record["finished_at"])
        self.assertIsNotNone(self.review.wait(timeout=30))
        self.assertFalse(self.answers(port))
        log = (self.private / "engine-op-restart.log").read_text()
        self.assertIn("engine gone", log)
        self.assertIn("finished with exit code 7", log)

        # The engine returns: a new service reads the same record back.
        self.start_service()
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
        service = self.start_service(verb="stop-engine")
        port = self.port()
        # The service recorded its own identity, privately.
        record = engine_ops.service_record()
        self.assertEqual((record["pid"], record["port"]), (service.pid, port))

        status, _headers, out = http_call(port, "POST", "/ops/restart", origin=GUI)
        self.assertEqual(status, 202, out)
        self.assertEqual(out["operation"]["detach"], "session")
        self.assertNotEqual(out["operation"]["pid"], service.pid)

        # The service itself is gone, cleanly, by the real stop path …
        self.assertEqual(service.wait(timeout=30), 0)
        self.assertFalse((self.private / engine_ops.SERVICE_NAME).exists())
        self.assertFalse((self.root / ".super-coder" / "run" / "engine-ops.json").exists())
        # … and so is the review server.
        self.assertEqual(self.review.wait(timeout=30), -signal.SIGTERM)
        self.assert_recorded_after_the_engine_is_gone(port, "session")
        self.assertIn("→ engine-ops stopped",
                      (self.private / "engine-op-restart.log").read_text())

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
        """The control: without the scope, the unit stop takes the runner too."""
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


class SandboxCannotSteerTest(ServiceProcessFixture):
    """On docker the checkout is bind-mounted writable into the sandbox. After
    "the sandbox" rewrites every file under the engine directory, the service
    still accepts the same origin, reads and writes the same files, and its
    stop path signals the same process."""

    review_port = 8812
    DEV_PORT = 8813
    GUI = "http://127.0.0.1:8812"
    EVIL = "http://127.0.0.1:8813"     # the sandbox's own published dev server

    def sandbox_rewrites_the_engine_directory(self, canary: subprocess.Popen,
                                              victim: Path) -> None:
        engine = self.root / ".super-coder"
        forged_status = json.dumps({
            "op": "update", "state": "finished", "pid": canary.pid,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z", "exit_code": 0,
            "log": str(victim), "detach": "session"})
        # Every existing file: replaced.
        for path in sorted(engine.rglob("*")):
            if path.is_file() or path.is_symlink():
                path.unlink()
                path.write_text(forged_status)
        # The instance config: the GUI is "now" the sandbox's dev server, and
        # the service port is "now" somewhere else.
        (engine / "instance.json").write_text(json.dumps({
            "repo": "root", "port": self.DEV_PORT, "dev_port": self.DEV_PORT,
            "engine_ops_port": self.DEV_PORT, "instance_id": "forged",
            "runtime": "host"}))
        # Pid files and state files naming another host process.
        for name in ("engine-ops.pid", "server.pid"):
            (engine / "run" / name).write_text(f"{canary.pid}\n")
        (engine / "run" / "engine-ops.json").unlink(missing_ok=True)
        (engine / "run" / "engine-ops.json").symlink_to(victim)
        (engine / "run" / "service.json").write_text(json.dumps({
            "pid": canary.pid,
            "pid_start_ticks": engine_ops.pid_start_ticks(canary.pid)}))
        # Status, config, and logs where a service might look, as forgeries
        # and as links to a host file.
        for name in ("engine-op.json", "config.json", "service.json"):
            (engine / "logs" / name).write_text(forged_status)
        for op in ("update", "restart", "update-harnesses"):
            (engine / "logs" / f"engine-op-{op}.log").symlink_to(victim)
            (engine / "run" / f"engine-op-{op}.log").symlink_to(victim)

    def test_rewriting_the_engine_directory_changes_nothing_the_service_does(self):
        victim = self.base / "host-secret.txt"
        victim.write_text("HOST SECRET CONTENT\n")
        canary = sleeper()
        self.addCleanup(reap, canary)

        service = self.start_service()
        port = self.port()
        self.assertEqual(http_call(port, "GET", "/status", origin=self.GUI)[0], 200)

        self.sandbox_rewrites_the_engine_directory(canary, victim)

        # 1. Which origin is accepted.
        for method, path in (("POST", "/ops/update"), ("OPTIONS", "/ops/update"),
                             ("GET", "/status")):
            status, headers, out = http_call(port, method, path, origin=self.EVIL)
            self.assertEqual(status, 403, (method, out))
            self.assertEqual(out["error"]["code"], "same_origin_required")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertFalse(self.status_file.exists())
        self.assertFalse((self.root / "argv.json").exists())

        # 2. Which files are read: the forged status and its `log` are not.
        status, headers, out = http_call(port, "GET", "/status", origin=self.GUI)
        self.assertEqual(status, 200, out)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.GUI)
        self.assertIsNone(out["operation"])
        self.assertIsNone(out["log_tail"])
        self.assertNotIn("HOST SECRET", json.dumps(out))

        # 3. Which files are written: an operation from the real GUI runs, and
        #    its status and log land in the private directory only.
        status, _headers, out = http_call(port, "POST", "/ops/update", origin=self.GUI)
        self.assertEqual(status, 202, out)
        record = self.finished_record()
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(json.loads((self.root / "argv.json").read_text()), ["update"])
        self.assertEqual(victim.read_text(), "HOST SECRET CONTENT\n")
        self.assertIn("stub sc update", (self.private / "engine-op-update.log").read_text())
        _status, _headers, out = http_call(port, "GET", "/status", origin=self.GUI)
        self.assertEqual(out["operation"]["log"],
                         str(self.private / "engine-op-update.log"))
        self.assertIn("stub sc update", out["log_tail"])
        self.assertNotIn("HOST SECRET", json.dumps(out))

        # 4. Which pid is signalled: the service, proven; never the canary.
        self.assertEqual(engine_ops.service_record()["pid"], service.pid)
        code, message = engine_ops.service_down()
        self.assertEqual((code, message), (0, "→ engine-ops stopped"))
        self.assertEqual(service.wait(timeout=30), 0)
        self.assertIsNone(canary.poll())
        self.assertEqual(victim.read_text(), "HOST SECRET CONTENT\n")

        # 5. And at the next start: the pinned origin and port are unchanged.
        pinned = json.loads((self.private / "config.json").read_text())
        self.assertEqual(pinned["gui_port"], self.review_port)
        self.start_service()
        new_port = self.port()
        status, _headers, out = http_call(new_port, "POST", "/ops/update",
                                          origin=self.EVIL)
        self.assertEqual(status, 403, out)
        status, _headers, out = http_call(new_port, "POST", "/ops/update",
                                          origin=self.GUI)
        self.assertEqual(status, 202, out)
        self.finished_record()
        self.assertIsNone(canary.poll())
        self.assertEqual(victim.read_text(), "HOST SECRET CONTENT\n")


class ServiceLifecycleTest(ServiceProcessFixture):
    """`engine_ops.py up` / `down`, as the dispatcher calls them."""

    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            mock.patch.object(engine_ops, "REPO_ROOT", self.root),
            mock.patch.object(engine_ops, "in_sandbox", return_value=False),
            mock.patch.object(engine_ops, "service_argv",
                              return_value=self.service_argv("session", "noop")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(engine_ops.service_down)

    def pin(self) -> int:
        """Pin a free port the launcher will then serve on."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        operator_pins(self.private, GUI_PORT, port)
        return port

    def test_up_is_idempotent_and_down_stops_only_the_service(self):
        port = self.pin()
        with mock.patch.object(engine_ops, "_publish_port"):
            code, message = engine_ops.service_up()
            self.assertEqual(code, 0, message)
            self.assertIn(f"http://127.0.0.1:{port} (host loopback only)", message)
            # The operator sees what is pinned every time it starts.
            self.assertIn(f"accepts the GUI at {GUI}", message)
            self.assertTrue(engine_ops.service_answers(port))
            record = engine_ops.service_record()
            self.assertEqual(record["port"], port)
            # Its output is in the private directory, not the checkout.
            self.assertTrue((self.private / "service.log").exists())
            self.assertFalse(list((self.root / ".super-coder" / "run").glob("*.log")))
            self.assertFalse(list((self.root / ".super-coder" / "run").glob("*.pid")))

            code, message = engine_ops.service_up()
            self.assertEqual(code, 0, message)
            self.assertIn("already serving", message)
            self.assertEqual(engine_ops.service_record()["pid"], record["pid"])

        code, message = engine_ops.service_down()
        self.assertEqual((code, message), (0, "→ engine-ops stopped"))
        self.assertTrue(wait_for(lambda: not engine_ops.service_answers(port)))
        code, message = engine_ops.service_down()
        self.assertEqual((code, message), (0, "→ engine-ops not running"))

    def test_up_in_the_sandbox_starts_nothing(self):
        self.pin()
        with mock.patch.object(engine_ops, "in_sandbox", return_value=True), \
                mock.patch.object(engine_ops, "_spawn") as spawn:
            code, message = engine_ops.service_up()
        self.assertEqual(code, 0)
        self.assertIn("NOT started", message)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
