#!/usr/bin/env python3
"""Smoke tests for the tailnet broker (api/ts_broker.py + scripts/ts.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and the
sibling tests (test_vm_broker.py). The broker drives a real tailnet via the
tailscale CLI, which no CI box has; so we mock at the subprocess seam
(`ts._run` / `subprocess.run`) and exercise the parts that DO run everywhere: the
verb dispatch + scoping, the JSON shapes the `tailscale` skill depends on, and
the real unix-socket HTTP transport end to end (a live broker on a temp socket,
driven by the same `ts.broker_call` client the in-sandbox server proxies through).

Run:
    python3 tests/test_ts_broker.py
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

import ts  # noqa: E402
import ts_broker  # noqa: E402

SAVED = {
    "ssh_user": "tester",
    "allowed_hosts": ["build-box", "deploy-target"],
    "tailscale_bin": "tailscale",
}

# A minimal `tailscale status --json` payload.
STATUS_JSON = json.dumps({
    "BackendState": "Running",
    "Self": {"HostName": "cachy", "DNSName": "cachy.tail0.ts.net.",
             "TailscaleIPs": ["100.64.0.1"]},
    "Peer": {
        "k1": {"HostName": "build-box", "DNSName": "build-box.tail0.ts.net.",
               "TailscaleIPs": ["100.64.0.2"], "Online": True},
        "k2": {"HostName": "deploy-target", "DNSName": "deploy-target.tail0.ts.net.",
               "TailscaleIPs": ["100.64.0.3"], "Online": False},
    },
})


class VerbDispatchTests(unittest.TestCase):
    """The verbs operate on the SAVED block + a named host and shape results."""

    def test_exec_returns_exit_stdout_stderr_and_targets_tailscale_ssh(self):
        fake = mock.Mock(returncode=0, stdout="hello\n", stderr="")
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            r = ts.do_exec("build-box", "echo hello")
        self.assertEqual(r, {"ok": True, "exit": 0, "stdout": "hello\n", "stderr": ""})
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["tailscale", "ssh"])
        self.assertIn("tester@build-box", argv)

    def test_exec_denies_a_host_outside_allowed_hosts(self):
        # Fail-closed scoping: a host not in allowed_hosts is rejected pre-ssh.
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run") as run:
            r = ts.do_exec("rogue-host", "whoami")
        self.assertFalse(r["ok"])
        self.assertIn("not in allowed_hosts", r["stderr"])
        run.assert_not_called()

    def test_exec_with_no_allowed_hosts_denies_everything(self):
        with mock.patch.object(ts, "read", return_value={"ssh_user": "tester"}), \
             mock.patch("subprocess.run") as run:
            r = ts.do_exec("build-box", "whoami")
        self.assertFalse(r["ok"])
        self.assertIn("no allowed_hosts", r["stderr"])
        run.assert_not_called()

    def test_exec_empty_command_is_a_clean_error(self):
        with mock.patch.object(ts, "read", return_value=SAVED):
            r = ts.do_exec("build-box", "   ")
        self.assertFalse(r["ok"])
        self.assertIn("empty command", r["stderr"])

    def test_status_summarizes_self_and_peers(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.do_status()
        self.assertTrue(r["ok"])
        self.assertEqual(r["backend"], "Running")
        self.assertEqual(r["self"]["host"], "cachy")
        self.assertEqual(r["self"]["dns"], "cachy.tail0.ts.net")  # trailing dot stripped
        hosts = sorted(p["host"] for p in r["peers"])
        self.assertEqual(hosts, ["build-box", "deploy-target"])

    def test_configured_cli_reflects_a_linked_tailnet(self):
        # `./sc ts-broker-up` calls `ts.py configured` to self-skip when unlinked.
        with mock.patch.object(ts, "read", return_value=SAVED):
            self.assertEqual(ts.main(["configured"]), 0)
        with mock.patch.object(ts, "read", return_value=None):
            self.assertEqual(ts.main(["configured"]), 1)


class CheckTests(unittest.TestCase):
    """validate() runs one live check against a CANDIDATE block."""

    def test_auth_passes_when_backend_running(self):
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.validate("auth", SAVED)
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "auth")

    def test_auth_fails_when_not_logged_in(self):
        stopped = json.dumps({"BackendState": "NeedsLogin", "Self": {}, "Peer": {}})
        with mock.patch.object(ts, "_run", return_value=(True, stopped)):
            r = ts.validate("auth", SAVED)
        self.assertFalse(r["ok"])
        self.assertIn("tailscale up", r["output"])

    def test_peer_flags_a_missing_allowed_host(self):
        cfg = dict(SAVED, allowed_hosts=["build-box", "ghost"])
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.validate("peer", cfg)
        self.assertFalse(r["ok"])
        self.assertIn("ghost", r["output"])

    def test_unknown_check_is_none(self):
        self.assertIsNone(ts.validate("nope", SAVED))


class SocketTransportTests(unittest.TestCase):
    """A live broker on a temp socket, driven by the real broker_call client —
    proves the unix-socket HTTP transport the container relies on actually works."""

    def setUp(self):
        self.sock = Path(__file__).resolve().parent / "_test_ts_broker.sock"
        self._orig_socket = ts.SOCKET
        ts.SOCKET = self.sock  # both server (ts_broker path) + client read this
        self.srv = ts_broker.UnixHTTPServer(str(self.sock), ts_broker.Handler)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        ts.SOCKET = self._orig_socket
        self.sock.unlink(missing_ok=True)

    def test_health(self):
        r = ts.broker_call("GET", "/health")
        self.assertEqual(r, {"ok": True, "service": "ts-broker"})

    def test_unknown_route_is_404_shaped(self):
        r = ts.broker_call("GET", "/nope")
        self.assertFalse(r["ok"])

    def test_validate_proxies_the_candidate_cfg_in_the_body(self):
        # The in-sandbox server proxies validate through exactly this path.
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.broker_call("POST", "/validate/auth", {"ts": SAVED})
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "auth")

    def test_exec_round_trips_over_the_socket(self):
        fake = mock.Mock(returncode=2, stdout="out", stderr="err")
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake):
            r = ts.broker_call("POST", "/exec",
                               {"host": "build-box", "command": "exit 2"})
        self.assertEqual(r["exit"], 2)
        self.assertEqual(r["stdout"], "out")

    def test_status_round_trips_over_the_socket(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.broker_call("GET", "/status")
        self.assertTrue(r["ok"])
        self.assertEqual(r["backend"], "Running")

    def test_broker_call_raises_when_nothing_listens(self):
        ts.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            ts.broker_call("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
