#!/usr/bin/env python3
"""Smoke tests for the Windows VM broker (api/vm_broker.py + scripts/vm.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and the
sibling tests. The broker drives a real Windows VM via ssh/virsh, which no CI box
has; so we mock at the subprocess seam (`vm._run` / `subprocess.run`) and exercise
the parts that DO run everywhere: the verb dispatch + field validation, the JSON
shapes windows_devkit depends on, and the real unix-socket HTTP transport end to
end (a live broker on a temp socket, driven by the same `vm.broker_call` client
the in-sandbox server proxies through).

Run:
    python3 tests/test_vm_broker.py
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import vm  # noqa: E402
import vm_broker  # noqa: E402
import vm_mcp_relay  # noqa: E402

SAVED = {
    "domain": "win-test", "ssh_host": "127.0.0.1", "ssh_port": 22,
    "ssh_user": "tester", "ssh_key_path": "~/.ssh/sc_win_test",
    "transfer_dir": "/tmp", "snapshot": "clean",
}


class VerbDispatchTests(unittest.TestCase):
    """The verbs operate on the SAVED block and shape their result correctly."""

    def test_exec_returns_exit_stdout_stderr(self):
        fake = mock.Mock(returncode=0, stdout="hello\n", stderr="")
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            r = vm.do_exec("echo hello")
        self.assertEqual(r, {"ok": True, "exit": 0, "stdout": "hello\n", "stderr": ""})
        # SSH non-interactive + targets the saved guest, not a caller-named host.
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("tester@127.0.0.1", argv)

    def test_exec_missing_config_is_a_clean_error_not_a_crash(self):
        with mock.patch.object(vm, "read", return_value={}):
            r = vm.do_exec("whoami")
        self.assertFalse(r["ok"])
        self.assertIn("missing required field", r["stderr"])

    def test_configured_cli_reflects_a_linked_vm(self):
        # `./sc vm-broker-up` calls `vm.py configured` to self-skip when unlinked.
        with mock.patch.object(vm, "read", return_value=SAVED):
            self.assertEqual(vm.main(["configured"]), 0)
        with mock.patch.object(vm, "read", return_value=None):
            self.assertEqual(vm.main(["configured"]), 1)

    def test_virsh_calls_honor_libvirt_uri(self):
        cfg = dict(SAVED, libvirt_uri="qemu:///system")
        with mock.patch.object(vm, "read", return_value=cfg), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
            vm.do_reset()
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["virsh", "--connect", "qemu:///system"])

    def test_virsh_omits_connect_when_no_uri(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
            vm.do_reset()
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "virsh")
        self.assertNotIn("--connect", argv)  # default URI / env, unchanged behavior

    def test_reset_passes_running_for_the_offline_clean_snapshot(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run, \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")):
            r = vm.do_reset()
        self.assertTrue(r["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["virsh", "snapshot-revert", "win-test"])
        self.assertIn("--running", argv)  # else the box comes back powered-off

    def test_reset_running_false_lands_clean_and_powered_off(self):
        # End-of-loop: revert to the offline clean snapshot WITHOUT --running, so
        # the box returns clean *and* powered off (frees the host's ~12 GB).
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run, \
             mock.patch.object(
                 vm, "_domain_state", return_value=(True, "powered_off")
             ):
            r = vm.do_reset(running=False)
        self.assertTrue(r["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["virsh", "snapshot-revert", "win-test"])
        self.assertNotIn("--running", argv)  # left powered off
        self.assertIn("powered off", r["output"])

    def test_push_rejects_a_missing_source(self):
        # In-repo but nonexistent → clean "source not found", not a crash.
        with mock.patch.object(vm, "read", return_value=SAVED):
            r = vm.do_push(str(vm.ports.ENGINE / "no" / "such-artifact.msi"))
        self.assertFalse(r["ok"])
        self.assertIn("source not found", r["output"])

    def test_push_rejects_a_src_outside_the_repo(self):
        # A sandbox-reachable broker must not exfiltrate host files (~, absolute)
        # into the guest share — src is contained to the bind-mounted repo.
        with mock.patch.object(vm, "read", return_value=SAVED):
            r = vm.do_push("~/.ssh/id_ed25519")
        self.assertFalse(r["ok"])
        self.assertIn("inside the repo", r["output"])

    def test_push_rejects_a_dest_that_escapes_transfer_dir(self):
        # `dest` with .. must not walk out of transfer_dir and clobber host files.
        share = tempfile.mkdtemp(prefix="sc_share_")
        cfg = dict(SAVED, transfer_dir=share)
        src = str(vm.ports.ENGINE / "scripts" / "vm.py")  # a real in-repo file
        with mock.patch.object(vm, "read", return_value=cfg):
            r = vm.do_push(src, "../../etc/sc_escape_probe")
        self.assertFalse(r["ok"])
        self.assertIn("escapes transfer_dir", r["output"])
        self.assertFalse(Path("/etc/sc_escape_probe").exists())  # nothing written

    def test_push_stages_a_legit_repo_file_into_the_share(self):
        # The contained happy path still works: in-repo src → inside the share.
        share = tempfile.mkdtemp(prefix="sc_share_")
        cfg = dict(SAVED, transfer_dir=share)
        src = str(vm.ports.ENGINE / "scripts" / "vm.py")
        with mock.patch.object(vm, "read", return_value=cfg):
            r = vm.do_push(src, "staged.py")
        self.assertTrue(r["ok"], r)
        self.assertTrue((Path(share) / "staged.py").is_file())

    def test_exec_survives_non_utf8_guest_output(self):
        # #261: Windows guests routinely emit non-UTF-8 (UTF-16 files, OEM
        # codepages). A strict decode raised UnicodeDecodeError and the broker
        # 500'd the whole exec. Real subprocess, real bytes: decode must be
        # lossy (U+FFFD), never fatal, with exit + surrounding output intact.
        raw = [sys.executable, "-c",
               "import sys; sys.stdout.buffer.write(b'pre \\x83\\xff post')"]
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_ssh_argv", return_value=raw):
            r = vm.do_exec("type addin-manifest")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exit"], 0)
        self.assertIn("pre ", r["stdout"])
        self.assertIn(" post", r["stdout"])
        self.assertIn("�", r["stdout"])  # lossy marker, not an exception

    def test_run_survives_non_utf8_output(self):
        # Same seam for reset/capture/validate — _run shares the decode policy.
        ok, out = vm._run([sys.executable, "-c",
                           "import sys; sys.stdout.buffer.write(b'ok \\x9d')"])
        self.assertTrue(ok)
        self.assertIn("ok", out)

    def test_capture_returns_a_base64_screenshot(self):
        def fake_run(argv, timeout=30):
            # virsh screenshot writes the file the broker then reads back
            Path(argv[-1]).write_bytes(b"P6 fake ppm bytes")
            return True, "Screenshot saved"
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run):
            r = vm.do_capture()
        self.assertTrue(r["ok"])
        self.assertIn("screenshot_b64", r)
        self.assertEqual(r["screenshot_bytes"], len(b"P6 fake ppm bytes"))


class McpTunnelTests(unittest.TestCase):
    """The GUI seam's broker half (#263): a broker-owned `ssh -N -L` that
    forwards a unix socket in run/ to the guest's Windows-MCP port."""

    def setUp(self):
        # Redirect every tunnel artifact into a temp dir so tests never touch
        # (or depend on) the real run/ state.
        d = Path(tempfile.mkdtemp(prefix="sc_mcp_"))
        self.temp_dir = d
        self._patches = [
            mock.patch.object(vm, "MCP_SOCKET", d / "vm-mcp.sock"),
            mock.patch.object(vm, "MCP_PIDFILE", d / "vm-mcp-tunnel.pid"),
            mock.patch.object(vm, "MCP_LOCKFILE", d / "vm-mcp-tunnel.lock"),
            mock.patch.object(vm, "MCP_LOG", d / "vm-mcp-tunnel.log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    @staticmethod
    def _cleanup_pid_file(path):
        try:
            pid = int(path.read_text())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_mcp_up_missing_config_is_a_clean_error(self):
        with mock.patch.object(vm, "read", return_value={}):
            r = vm.do_mcp_up()
        self.assertFalse(r["ok"])
        self.assertIn("missing required field", r["output"])

    def test_mcp_up_forwards_a_unix_socket_to_the_saved_mcp_port(self):
        # The ssh argv is the security posture: socket forward (not a TCP
        # bind), 0600 socket, dead-forward = dead pid, target from the SAVED
        # block only.
        def fake_popen(argv, **kw):
            vm.MCP_SOCKET.touch()  # "ssh" bound its forward socket
            return mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        cfg = dict(SAVED, mcp_port=9000)
        with mock.patch.object(vm, "read", return_value=cfg), \
             mock.patch("subprocess.Popen", side_effect=fake_popen) as popen, \
             mock.patch.object(vm, "_tunnel_ready", side_effect=[False, True]), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_unix_listener", return_value=True), \
             mock.patch.object(vm, "_new_process_state", return_value={
                 "schema_version": 1, "kind": "vm-mcp-tunnel", "pid": 4242,
                 "start_ticks": 123, "executable": "/usr/bin/ssh", "port": 9000,
             }):
            r = vm.do_mcp_up(wait=5)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pid"], 4242)
        self.assertEqual(r["port"], 9000)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-N", argv)
        self.assertIn(f"{vm.MCP_SOCKET}:127.0.0.1:9000", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("StreamLocalBindUnlink=yes", argv)
        self.assertIn("StreamLocalBindMask=0177", argv)
        self.assertIn("tester@127.0.0.1", argv)

    def test_mcp_port_defaults_to_8000(self):
        def fake_popen(argv, **kw):
            vm.MCP_SOCKET.touch()
            return mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.Popen", side_effect=fake_popen) as popen, \
             mock.patch.object(vm, "_tunnel_ready", side_effect=[False, True]), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_unix_listener", return_value=True), \
             mock.patch.object(vm, "_new_process_state", return_value={
                 "schema_version": 1, "kind": "vm-mcp-tunnel", "pid": 4242,
                 "start_ticks": 123, "executable": "/usr/bin/ssh", "port": 8000,
             }):
            r = vm.do_mcp_up(wait=5)
        self.assertTrue(r["ok"], r)
        self.assertIn(f"{vm.MCP_SOCKET}:127.0.0.1:8000", popen.call_args[0][0])

    def test_mcp_up_is_idempotent_when_already_live(self):
        # A verified identity + present socket → report it, never stack ssh.
        state = {"pid": 4242, "port": 8000}
        vm.MCP_SOCKET.touch()
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_tunnel_process", return_value=state), \
             mock.patch.object(vm, "_tunnel_ready", return_value=True), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_unix_listener", return_value=True), \
             mock.patch("subprocess.Popen") as popen:
            r = vm.do_mcp_up()
        self.assertEqual(
            r,
            {
                "ok": True,
                "output": "tunnel already up (pid 4242)",
                "socket": str(vm.MCP_SOCKET),
                "pid": 4242,
                "port": 8000,
            },
        )
        popen.assert_not_called()

    def test_mcp_up_surfaces_a_dying_ssh_with_its_stderr(self):
        def fake_popen(argv, **kw):
            vm.MCP_LOG.write_bytes(b"Permission denied (publickey).")
            return mock.Mock(pid=4242, returncode=255,
                             poll=mock.Mock(return_value=255))
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.Popen", side_effect=fake_popen), \
             mock.patch.object(vm, "_new_process_state", return_value={
                 "schema_version": 1, "kind": "vm-mcp-tunnel", "pid": 4242,
                 "start_ticks": 123, "executable": "/usr/bin/ssh", "port": 8000,
             }):
            r = vm.do_mcp_up(wait=5)
        self.assertFalse(r["ok"])
        self.assertIn("Permission denied", r["output"])
        self.assertIsNone(vm._tunnel_pid())  # no stale pidfile left behind

    def test_mcp_up_timeout_is_paced_reports_log_and_cleans_state(self):
        state = {
            "schema_version": 1, "kind": "vm-mcp-tunnel", "pid": 4242,
            "start_ticks": 123, "executable": "/usr/bin/ssh", "port": 8000,
        }
        process = mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        clock = iter((0.0, 0.0, 0.2, 0.4))
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_tunnel_ready", return_value=False), \
             mock.patch.object(vm, "_new_process_state", return_value=state), \
             mock.patch.object(vm, "_terminate_owned_process", return_value=True), \
             mock.patch.object(vm, "_bounded_log_tail", return_value="forward pending"), \
             mock.patch.object(vm.time, "monotonic", side_effect=clock), \
             mock.patch.object(vm.time, "sleep") as sleep, \
             mock.patch("subprocess.Popen", return_value=process):
            r = vm.do_mcp_up(wait=0.4)
        self.assertEqual(
            r,
            {"ok": False,
             "output": "tunnel socket did not appear within 0.4s: forward pending"},
        )
        self.assertEqual(sleep.call_args_list, [mock.call(0.2)] * 2)
        self.assertFalse(vm.MCP_PIDFILE.exists())
        self.assertFalse(vm.MCP_SOCKET.exists())

    def test_foreign_tunnel_race_returns_child_bind_evidence(self):
        process = mock.Mock(pid=4242, returncode=255)
        process.poll.side_effect = [None, 255]

        def fake_popen(*args, **kwargs):
            vm.MCP_LOG.write_text("unix_listener: cannot bind to path")
            return process

        state = {
            "schema_version": 1, "kind": "vm-mcp-tunnel", "pid": 4242,
            "start_ticks": 123, "executable": "/usr/bin/ssh", "port": 8000,
        }
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_tunnel_ready", side_effect=[False, True]), \
             mock.patch.object(vm, "_new_process_state", return_value=state), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_unix_listener", return_value=False), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            result = vm.do_mcp_up(wait=5)
        self.assertEqual(
            result,
            {
                "ok": False,
                "output": (
                    "ssh tunnel exited (rc 255): "
                    "unix_listener: cannot bind to path"
                ),
            },
        )
        self.assertFalse(vm.MCP_PIDFILE.exists())
        self.assertFalse(vm.MCP_SOCKET.exists())

    def test_mcp_down_removes_legacy_state_without_signaling_recycled_pid(self):
        vm.MCP_PIDFILE.write_text("4242")
        vm.MCP_SOCKET.touch()
        with mock.patch.object(vm, "_terminate_owned_process") as terminate:
            r = vm.do_mcp_down()
        self.assertEqual(
            r,
            {"ok": True, "output": "tunnel not running (stale state removed)"},
        )
        terminate.assert_not_called()
        self.assertFalse(vm.MCP_PIDFILE.exists())
        self.assertFalse(vm.MCP_SOCKET.exists())

    def test_mcp_down_is_idempotent(self):
        r = vm.do_mcp_down()
        self.assertTrue(r["ok"])
        self.assertIn("not running", r["output"])

    def test_mcp_status_reports_not_running_without_a_tunnel(self):
        r = vm.mcp_status()
        self.assertEqual(
            r,
            {
                "ok": True,
                "running": False,
                "pid": None,
                "socket": None,
                "listening": False,
                "unverified": False,
            },
        )

    def test_mcp_status_requires_a_listening_socket_not_a_stale_path(self):
        vm.MCP_SOCKET.touch()
        with mock.patch.object(vm, "_tunnel_process", return_value={"pid": 4242}):
            r = vm.mcp_status()
        self.assertEqual(
            r,
            {
                "ok": True,
                "running": False,
                "pid": 4242,
                "socket": None,
                "listening": False,
                "unverified": False,
            },
        )

    def test_unverified_live_unix_listener_is_refused_and_preserved(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(vm.MCP_SOCKET))
        listener.listen(8)
        vm.MCP_PIDFILE.write_text("4242")

        status = vm.mcp_status()
        self.assertEqual(
            status,
            {
                "ok": True,
                "running": False,
                "pid": None,
                "socket": str(vm.MCP_SOCKET),
                "listening": True,
                "unverified": True,
            },
        )
        self.assertTrue(vm.MCP_PIDFILE.exists())

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.Popen") as popen:
            up = vm.do_mcp_up()
        self.assertEqual(
            up,
            {
                "ok": False,
                "running": True,
                "unverified": True,
                "socket": str(vm.MCP_SOCKET),
                "output": "tunnel socket is held by an unverified process; refusing to start",
            },
        )
        popen.assert_not_called()

        with mock.patch.object(vm, "_terminate_owned_process") as terminate:
            down = vm.do_mcp_down()
        self.assertEqual(
            down,
            {
                "ok": False,
                "running": True,
                "unverified": True,
                "socket": str(vm.MCP_SOCKET),
                "output": "unverified tunnel is still listening; state removed, process not signaled",
            },
        )
        terminate.assert_not_called()
        self.assertTrue(vm._tunnel_ready())
        self.assertTrue(vm.MCP_SOCKET.exists())
        self.assertFalse(vm.MCP_PIDFILE.exists())
        self.assertEqual(vm.mcp_status(), status)

    def test_unverified_ssh_wrapper_reports_identity_log_and_reaps_child(self):
        wrapper = self.temp_dir / "ssh"
        pid_file = self.temp_dir / "wrapper.pid"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "open(os.environ['PID_OUT'], 'w').write(str(os.getpid()))\n"
            "print('wrapper executable mismatch', file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n"
        )
        wrapper.chmod(0o755)
        self.addCleanup(self._cleanup_pid_file, pid_file)
        env = {
            "PATH": f"{self.temp_dir}:{os.environ.get('PATH', '')}",
            "PID_OUT": str(pid_file),
        }
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.dict(os.environ, env):
            result = vm.do_mcp_up(wait=2)

        pid = int(pid_file.read_text())
        self.assertFalse(result["ok"])
        self.assertIn(f"expected executable {wrapper}", result["output"])
        self.assertIn(f"observed {os.path.realpath(sys.executable)}", result["output"])
        self.assertIn("rc -15", result["output"])
        self.assertIn("wrapper executable mismatch", result["output"])
        self.assertIsNone(vm._process_snapshot(pid))
        self.assertFalse(vm.MCP_PIDFILE.exists())


class McpRelayTests(unittest.TestCase):
    """The GUI seam's sandbox half: TCP 127.0.0.1 → the tunnel's unix socket,
    exercised END TO END — a real unix echo server behind a real relay, driven
    by a real TCP client. Bytes must survive both directions unmodified."""

    def setUp(self):
        self.upstream_path = Path(tempfile.mkdtemp(prefix="sc_relay_")) / "vm-mcp.sock"
        self._patch = mock.patch.object(vm, "MCP_SOCKET", self.upstream_path)
        self._patch.start()
        # the stand-in for the guest's Windows-MCP behind the ssh forward
        self.upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.upstream.bind(str(self.upstream_path))
        self.upstream.listen(4)
        threading.Thread(target=self._echo_forever, daemon=True).start()
        # the relay under test, on an ephemeral port
        self.srv = vm_mcp_relay.make_server(0)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=vm_mcp_relay.run, args=(self.srv,), daemon=True).start()

    def tearDown(self):
        self.srv.close()
        self.upstream.close()
        self._patch.stop()
        self.upstream_path.unlink(missing_ok=True)

    def _echo_forever(self):
        while True:
            try:
                conn, _ = self.upstream.accept()
            except OSError:
                return
            def echo(c):
                while data := c.recv(65536):
                    c.sendall(data)
                c.close()
            threading.Thread(target=echo, args=(conn,), daemon=True).start()

    def test_bytes_round_trip_through_the_relay(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        payload = b"POST /mcp HTTP/1.1\r\n\r\n" + bytes(range(256))  # incl. non-UTF-8
        c.sendall(payload)
        got = b""
        while len(got) < len(payload):
            got += c.recv(65536)
        c.close()
        self.assertEqual(got, payload)

    def test_concurrent_connections_do_not_cross_streams(self):
        conns = [socket.create_connection(("127.0.0.1", self.port), timeout=5)
                 for _ in range(4)]
        for i, c in enumerate(conns):
            c.sendall(f"stream-{i}".encode())
        for i, c in enumerate(conns):
            self.assertEqual(c.recv(65536), f"stream-{i}".encode())
            c.close()

    def test_relay_closes_cleanly_when_upstream_is_absent(self):
        # Tunnel not up yet → the client sees EOF, not a hang.
        with mock.patch.object(vm, "MCP_SOCKET", self.upstream_path.with_name("absent.sock")):
            c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            c.settimeout(5)
            self.assertEqual(c.recv(1), b"")  # clean close
            c.close()


class ProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.state_file = Path(tempfile.mkdtemp(prefix="sc_process_state_")) / "state"
        self.expected = os.path.realpath(sys.executable)

    def _state(self, *, start_ticks=100, executable=None):
        state = {
            "schema_version": 1,
            "kind": "relay",
            "pid": 77,
            "start_ticks": start_ticks,
            "executable": executable or self.expected,
        }
        vm._atomic_write_process_state(self.state_file, state)
        return state

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def test_atomic_state_is_mode_0600_and_complete_json(self):
        state = self._state()
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(vm._read_process_state(self.state_file), state)
        self.assertEqual(list(self.state_file.parent.glob(".state.*")), [])

    def test_recycled_container_pid_fails_start_identity_check(self):
        self._state(start_ticks=100)
        recycled = {
            "pid": 77,
            "start_ticks": 101,
            "executable": self.expected,
            "cmdline": [self.expected, "relay-token"],
        }
        with mock.patch.object(vm, "_process_snapshot", return_value=recycled):
            owned = vm._owned_process(
                self.state_file,
                kind="relay",
                expected_executable=sys.executable,
                required_token="relay-token",
            )
        self.assertIsNone(owned)

    def test_matching_pid_and_ticks_with_wrong_executable_is_not_owned(self):
        self._state(start_ticks=100)
        unrelated = {
            "pid": 77,
            "start_ticks": 100,
            "executable": "/usr/bin/sleep",
            "cmdline": ["/usr/bin/sleep", "relay-token"],
        }
        with mock.patch.object(vm, "_process_snapshot", return_value=unrelated):
            owned = vm._owned_process(
                self.state_file,
                kind="relay",
                expected_executable=sys.executable,
                required_token="relay-token",
            )
        self.assertIsNone(owned)

    def test_mismatched_record_is_never_signaled(self):
        state = self._state(start_ticks=100)
        recycled = {
            "pid": 77,
            "start_ticks": 101,
            "executable": self.expected,
            "cmdline": [self.expected, "relay-token"],
        }
        with mock.patch.object(vm, "_process_snapshot", return_value=recycled), \
             mock.patch.object(vm.os, "kill") as kill:
            stopped = vm._terminate_owned_process(
                state,
                expected_executable=sys.executable,
                required_token="relay-token",
            )
        self.assertTrue(stopped)
        kill.assert_not_called()

    def test_live_proc_snapshot_becomes_unowned_after_real_child_exits(self):
        token = "sc-real-process-token"
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", token]
        )
        self.addCleanup(self._reap, process)
        deadline = time.monotonic() + 1
        snapshot = None
        while snapshot is None and time.monotonic() < deadline:
            snapshot = vm._process_snapshot(process.pid)
            time.sleep(0.01)
        state = vm._new_process_state(
            process.pid,
            kind="relay",
            expected_executable=sys.executable,
            required_token=token,
        )
        self.assertIsInstance(snapshot, dict)
        self.assertIsInstance(state, dict)
        vm._atomic_write_process_state(self.state_file, state)

        self.assertEqual(snapshot["pid"], process.pid)
        self.assertGreater(snapshot["start_ticks"], 0)
        self.assertEqual(snapshot["executable"], os.path.realpath(sys.executable))
        self.assertIn(token, snapshot["cmdline"])
        self.assertEqual(
            vm._owned_process(
                self.state_file,
                kind="relay",
                expected_executable=sys.executable,
                required_token=token,
            ),
            state,
        )

        process.terminate()
        process.wait(timeout=5)
        self.assertIsNone(vm._process_snapshot(process.pid))
        self.assertIsNone(
            vm._owned_process(
                self.state_file,
                kind="relay",
                expected_executable=sys.executable,
                required_token=token,
            )
        )

    def test_live_listener_inodes_are_owned_only_by_the_holding_process(self):
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_path = self.state_file.with_name("listener.sock")
        self.addCleanup(tcp.close)
        self.addCleanup(unix.close)
        self.addCleanup(unix_path.unlink, missing_ok=True)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        unix.bind(str(unix_path))
        unix.listen(1)

        self.assertTrue(vm._process_owns_tcp_listener(os.getpid(), tcp.getsockname()[1]))
        self.assertTrue(vm._process_owns_unix_listener(os.getpid(), unix_path))
        self.assertFalse(vm._process_owns_tcp_listener(os.getppid(), tcp.getsockname()[1]))
        self.assertFalse(vm._process_owns_unix_listener(os.getppid(), unix_path))


class McpRelayLifecycleTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="sc_relay_lifecycle_"))
        self._patches = [
            mock.patch.object(vm_mcp_relay, "PIDFILE", d / "relay.pid"),
            mock.patch.object(vm_mcp_relay, "PORTFILE", d / "relay.port"),
            mock.patch.object(vm_mcp_relay, "LOCKFILE", d / "relay.lock"),
            mock.patch.object(vm_mcp_relay, "LOG", d / "relay.log"),
        ]
        for patcher in self._patches:
            patcher.start()
        self.state = {
            "schema_version": 1,
            "kind": "vm-mcp-relay",
            "pid": 4242,
            "start_ticks": 123,
            "executable": os.path.realpath(sys.executable),
            "port": 18000,
        }

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    def test_up_is_idempotent_only_for_verified_listening_relay(self):
        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=self.state), \
             mock.patch.object(vm_mcp_relay, "_listener_ready", return_value=True), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_tcp_listener", return_value=True), \
             mock.patch("subprocess.Popen") as popen:
            r = vm_mcp_relay.up(18000)
        self.assertEqual(
            r,
            {
                "ok": True,
                "output": "relay already up (pid 4242)",
                "url": "http://127.0.0.1:18000/mcp",
                "running": True,
                "pid": 4242,
                "port": 18000,
                "listening": True,
                "unverified": False,
                "upstream": vm.MCP_SOCKET.exists(),
            },
        )
        popen.assert_not_called()

    def test_bind_exit_returns_bounded_log_and_cleans_all_state(self):
        def fake_popen(*args, **kwargs):
            vm_mcp_relay.LOG.write_text("prefix\nOSError: Address already in use")
            return mock.Mock(pid=4242, returncode=98,
                             poll=mock.Mock(return_value=98))

        vm_mcp_relay.PORTFILE.write_text("18000")
        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=None), \
             mock.patch.object(vm, "_new_process_state", return_value=self.state), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            r = vm_mcp_relay.up(18000)
        self.assertFalse(r["ok"])
        self.assertEqual(
            r["output"],
            "relay exited (rc 98): prefix\nOSError: Address already in use",
        )
        self.assertFalse(vm_mcp_relay.PIDFILE.exists())
        self.assertFalse(vm_mcp_relay.PORTFILE.exists())

    def test_fresh_up_returns_the_complete_success_shape(self):
        process = mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=None), \
             mock.patch.object(vm, "MCP_SOCKET", vm_mcp_relay.PIDFILE.with_name("upstream.sock")), \
             mock.patch.object(vm_mcp_relay, "_listener_ready", side_effect=[False, True]), \
             mock.patch.object(vm, "_new_process_state", return_value=self.state), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_tcp_listener", return_value=True), \
             mock.patch("subprocess.Popen", return_value=process):
            result = vm_mcp_relay.up(18000)
        self.assertEqual(
            result,
            {
                "ok": True,
                "running": True,
                "listening": True,
                "unverified": False,
                "pid": 4242,
                "port": 18000,
                "url": "http://127.0.0.1:18000/mcp",
                "upstream": False,
                "output": (
                    "relay up, but the broker tunnel socket is absent — "
                    "connections will fail until POST /mcp/up on the vm-broker"
                ),
            },
        )

    def test_readiness_timeout_is_paced_reports_log_and_cleans_state(self):
        process = mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        clock = iter((0.0, 0.0, 0.1, 0.2, 0.3))
        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=None), \
             mock.patch.object(vm_mcp_relay, "_listener_ready", return_value=False), \
             mock.patch.object(vm, "_new_process_state", return_value=self.state), \
             mock.patch.object(vm, "_terminate_owned_process", return_value=True), \
             mock.patch.object(vm, "_bounded_log_tail", return_value="startup pending"), \
             mock.patch.object(vm_mcp_relay.time, "monotonic", side_effect=clock), \
             mock.patch.object(vm_mcp_relay.time, "sleep") as sleep, \
             mock.patch("subprocess.Popen", return_value=process):
            r = vm_mcp_relay.up(18000, wait=0.3)
        self.assertFalse(r["ok"])
        self.assertEqual(
            r["output"],
            "relay did not start listening on 127.0.0.1:18000 within 0.3s: startup pending",
        )
        self.assertEqual(sleep.call_args_list, [mock.call(0.2)] * 3)
        self.assertFalse(vm_mcp_relay.PIDFILE.exists())
        self.assertFalse(vm_mcp_relay.PORTFILE.exists())

    def test_down_removes_namespace_stale_state_without_signaling(self):
        vm._atomic_write_process_state(vm_mcp_relay.PIDFILE, self.state)
        vm_mcp_relay.PORTFILE.write_text("18000")
        recycled = {
            "pid": 4242,
            "start_ticks": 124,
            "executable": os.path.realpath(sys.executable),
            "cmdline": [sys.executable, vm_mcp_relay._relay_token()],
        }
        with mock.patch.object(vm, "_process_snapshot", return_value=recycled), \
             mock.patch.object(vm.os, "kill") as kill:
            r = vm_mcp_relay.down()
        self.assertEqual(
            r,
            {"ok": True, "output": "relay not running (stale state removed)"},
        )
        kill.assert_not_called()
        self.assertFalse(vm_mcp_relay.PIDFILE.exists())
        self.assertFalse(vm_mcp_relay.PORTFILE.exists())

    def test_owned_down_waits_for_listener_to_disappear(self):
        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=self.state), \
             mock.patch.object(vm, "_terminate_owned_process", return_value=True), \
             mock.patch.object(vm_mcp_relay, "_listener_ready",
                               side_effect=[True, False]), \
             mock.patch.object(vm_mcp_relay.time, "sleep") as sleep:
            result = vm_mcp_relay.down(18000)
        self.assertEqual(
            result,
            {"ok": True, "output": "relay stopped (pid 4242)"},
        )
        self.assertEqual(sleep.call_args_list, [mock.call(0.05)])

    def test_real_occupied_port_is_refused_and_reported_as_unverified(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        vm_mcp_relay.PIDFILE.write_text("4242")
        vm_mcp_relay.PORTFILE.write_text(str(port))

        with mock.patch("subprocess.Popen") as popen:
            up = vm_mcp_relay.up(port)
        self.assertEqual(
            up,
            {
                "ok": False,
                "running": True,
                "listening": True,
                "unverified": True,
                "port": port,
                "output": (
                    f"port 127.0.0.1:{port} is held by an unverified process; "
                    "refusing to start relay"
                ),
            },
        )
        popen.assert_not_called()

        with mock.patch.object(vm.os, "kill") as kill:
            down = vm_mcp_relay.down(port)
        self.assertEqual(
            down,
            {
                "ok": False,
                "running": True,
                "listening": True,
                "unverified": True,
                "port": port,
                "output": (
                    f"unverified relay is still listening on 127.0.0.1:{port}; "
                    "state removed, process not signaled"
                ),
            },
        )
        kill.assert_not_called()
        self.assertTrue(vm_mcp_relay._listener_ready(port))
        self.assertFalse(vm_mcp_relay.PIDFILE.exists())
        self.assertFalse(vm_mcp_relay.PORTFILE.exists())

    def test_listener_success_is_rejected_when_spawned_identity_no_longer_matches(self):
        process = mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=None), \
             mock.patch.object(vm_mcp_relay, "_listener_ready",
                               side_effect=[False, True, True]), \
             mock.patch.object(vm, "_new_process_state", return_value=self.state), \
             mock.patch.object(vm, "_process_record_matches", return_value=False), \
             mock.patch.object(vm, "_unverified_process_error",
                               return_value="relay identity mismatch: bind evidence"), \
             mock.patch("subprocess.Popen", return_value=process):
            result = vm_mcp_relay.up(18000)
        self.assertEqual(
            result,
            {
                "ok": False,
                "running": True,
                "listening": True,
                "unverified": True,
                "port": 18000,
                "output": (
                    "relay identity mismatch: bind evidence; "
                    "port remains held by an unverified process"
                ),
            },
        )

    def test_foreign_listener_race_returns_child_bind_evidence(self):
        process = mock.Mock(pid=4242, returncode=98)
        process.poll.side_effect = [None, 98]

        def fake_popen(*args, **kwargs):
            vm_mcp_relay.LOG.write_text("OSError: [Errno 98] Address already in use")
            return process

        with mock.patch.object(vm_mcp_relay, "_relay_process", return_value=None), \
             mock.patch.object(vm_mcp_relay, "_listener_ready", side_effect=[False, True]), \
             mock.patch.object(vm, "_new_process_state", return_value=self.state), \
             mock.patch.object(vm, "_process_record_matches", return_value=True), \
             mock.patch.object(vm, "_process_owns_tcp_listener", return_value=False), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            result = vm_mcp_relay.up(18000)
        self.assertEqual(
            result,
            {
                "ok": False,
                "output": (
                    "relay exited (rc 98): "
                    "OSError: [Errno 98] Address already in use"
                ),
            },
        )
        self.assertFalse(vm_mcp_relay.PIDFILE.exists())


class SocketTransportTests(unittest.TestCase):
    """A live broker on a temp socket, driven by the real broker_call client —
    proves the unix-socket HTTP transport the container relies on actually works."""

    def setUp(self):
        self.sock = Path(__file__).resolve().parent / "_test_vm_broker.sock"
        self._orig_socket = vm.SOCKET
        vm.SOCKET = self.sock  # both server (vm_broker.main path) + client read this
        self.srv = vm_broker.UnixHTTPServer(str(self.sock), vm_broker.Handler)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        vm.SOCKET = self._orig_socket
        self.sock.unlink(missing_ok=True)

    def test_health(self):
        r = vm.broker_call("GET", "/health")
        self.assertEqual(r, {"ok": True, "service": "vm-broker"})

    def test_unknown_route_is_404_shaped(self):
        r = vm.broker_call("GET", "/nope")
        self.assertFalse(r["ok"])

    def test_validate_proxies_the_candidate_cfg_in_the_body(self):
        # The in-sandbox server proxies validate through exactly this path.
        with mock.patch.object(vm, "_run", return_value=(True, "Id: 3")):
            r = vm.broker_call("POST", "/validate/domain", {"vm": SAVED})
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "domain")

    def test_exec_round_trips_over_the_socket(self):
        fake = mock.Mock(returncode=2, stdout="out", stderr="err")
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake):
            r = vm.broker_call("POST", "/exec", {"command": "exit 2"})
        self.assertEqual(r["exit"], 2)
        self.assertEqual(r["stdout"], "out")

    def test_mcp_routes_dispatch_over_the_socket(self):
        # The sandbox drives the GUI seam through exactly these routes.
        with mock.patch.object(vm, "mcp_status",
                               return_value={"ok": True, "running": False,
                                             "pid": None, "socket": None}):
            r = vm.broker_call("GET", "/mcp/status")
        self.assertFalse(r["running"])
        with mock.patch.object(vm, "do_mcp_up",
                               return_value={"ok": True, "output": "tunnel up"}) as up:
            r = vm.broker_call("POST", "/mcp/up")
        self.assertTrue(r["ok"])
        up.assert_called_once_with()
        with mock.patch.object(vm, "do_mcp_down",
                               return_value={"ok": True, "output": "tunnel stopped"}):
            r = vm.broker_call("POST", "/mcp/down")
        self.assertTrue(r["ok"])

    def test_broker_call_raises_when_nothing_listens(self):
        vm.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            vm.broker_call("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
