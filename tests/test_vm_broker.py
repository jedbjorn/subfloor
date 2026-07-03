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
import socket
import sys
import tempfile
import threading
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
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
            r = vm.do_reset()
        self.assertTrue(r["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["virsh", "snapshot-revert", "win-test"])
        self.assertIn("--running", argv)  # else the box comes back powered-off

    def test_reset_running_false_lands_clean_and_powered_off(self):
        # End-of-loop: revert to the offline clean snapshot WITHOUT --running, so
        # the box returns clean *and* powered off (frees the host's ~12 GB).
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
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
        self._patches = [
            mock.patch.object(vm, "MCP_SOCKET", d / "vm-mcp.sock"),
            mock.patch.object(vm, "MCP_PIDFILE", d / "vm-mcp-tunnel.pid"),
            mock.patch.object(vm, "MCP_LOG", d / "vm-mcp-tunnel.log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

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
             mock.patch("subprocess.Popen", side_effect=fake_popen) as popen:
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
             mock.patch("subprocess.Popen", side_effect=fake_popen) as popen:
            r = vm.do_mcp_up(wait=5)
        self.assertTrue(r["ok"], r)
        self.assertIn(f"{vm.MCP_SOCKET}:127.0.0.1:8000", popen.call_args[0][0])

    def test_mcp_up_is_idempotent_when_already_live(self):
        # A live pid + present socket → report it, never stack a second ssh.
        vm.MCP_PIDFILE.write_text(str(os.getpid()))  # this test process: alive
        vm.MCP_SOCKET.touch()
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.Popen") as popen:
            r = vm.do_mcp_up()
        self.assertTrue(r["ok"])
        self.assertIn("already up", r["output"])
        popen.assert_not_called()

    def test_mcp_up_surfaces_a_dying_ssh_with_its_stderr(self):
        def fake_popen(argv, **kw):
            vm.MCP_LOG.write_bytes(b"Permission denied (publickey).")
            return mock.Mock(pid=4242, returncode=255,
                             poll=mock.Mock(return_value=255))
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            r = vm.do_mcp_up(wait=5)
        self.assertFalse(r["ok"])
        self.assertIn("Permission denied", r["output"])
        self.assertIsNone(vm._tunnel_pid())  # no stale pidfile left behind

    def test_mcp_down_is_idempotent(self):
        r = vm.do_mcp_down()
        self.assertTrue(r["ok"])
        self.assertIn("not running", r["output"])

    def test_mcp_status_reports_not_running_without_a_tunnel(self):
        r = vm.mcp_status()
        self.assertTrue(r["ok"])
        self.assertFalse(r["running"])
        self.assertIsNone(r["socket"])


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
