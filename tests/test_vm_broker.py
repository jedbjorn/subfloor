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

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import vm  # noqa: E402
import vm_broker  # noqa: E402

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
        with mock.patch.object(vm, "read", return_value=SAVED):
            r = vm.do_push("/no/such/artifact.msi")
        self.assertFalse(r["ok"])
        self.assertIn("source not found", r["output"])

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

    def test_broker_call_raises_when_nothing_listens(self):
        vm.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            vm.broker_call("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
