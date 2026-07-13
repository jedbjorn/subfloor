#!/usr/bin/env python3
"""Smoke tests for the pm2 broker (api/pm2_broker.py + scripts/pm2.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and the
sibling tests (test_vm_broker.py, test_ts_broker.py). The broker drives a real
pm2 daemon, which no CI box has; so we mock at the subprocess seam
(`pm2._run` / `subprocess.run`) and exercise the parts that DO run everywhere:
the verb dispatch + fail-closed scoping + the lifecycle gate, the JSON shapes
the `pm2` skill depends on, and the real unix-socket HTTP transport end to end
(a live broker on a temp socket, driven by the same `pm2.broker_call` client
the in-sandbox server proxies through).

Run:
    python3 tests/test_pm2_broker.py
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

import pm2  # noqa: E402
import pm2_broker  # noqa: E402

SAVED = {
    "processes": ["myapp-api", "myapp-ui"],
    "health_url": "http://127.0.0.1:8000/health",
    "pm2_bin": "pm2",
}

# A minimal `pm2 jlist` payload (with the daemon-boot chatter pm2 sometimes
# prefixes, so the parser's find-the-array behavior is exercised too).
JLIST = "[PM2] Spawning PM2 daemon\n" + json.dumps([
    {"name": "myapp-api", "pid": 4242,
     "pm2_env": {"status": "online", "pm_uptime": 1, "restart_time": 3,
                 "pm_out_log_path": "/tmp/api-out.log",
                 "pm_err_log_path": "/tmp/api-err.log"},
     "monit": {"cpu": 0.5, "memory": 52428800}},
    {"name": "myapp-ui", "pid": 4243,
     "pm2_env": {"status": "stopped", "pm_uptime": 1, "restart_time": 0},
     "monit": {"cpu": 0, "memory": 0}},
    {"name": "unrelated-host-proc", "pid": 9999,
     "pm2_env": {"status": "online", "pm_uptime": 1, "restart_time": 0},
     "monit": {"cpu": 1, "memory": 1024}},
])


class VerbDispatchTests(unittest.TestCase):
    """The verbs operate on the SAVED block + a named process and shape results."""

    def test_status_scopes_to_declared_processes_only(self):
        # The host supervises an undeclared process — it must not leak through.
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch.object(pm2, "_run", return_value=(True, JLIST)):
            r = pm2.do_status()
        self.assertTrue(r["ok"])
        names = sorted(p["name"] for p in r["processes"])
        self.assertEqual(names, ["myapp-api", "myapp-ui"])
        self.assertEqual(r["missing"], [])
        api = next(p for p in r["processes"] if p["name"] == "myapp-api")
        self.assertEqual(api["status"], "online")
        self.assertEqual(api["restarts"], 3)
        ui = next(p for p in r["processes"] if p["name"] == "myapp-ui")
        self.assertIsNone(ui["uptime_s"])  # not online → no uptime

    def test_status_surfaces_declared_but_unsupervised_names(self):
        cfg = dict(SAVED, processes=["myapp-api", "ghost"])
        with mock.patch.object(pm2, "read", return_value=cfg), \
             mock.patch.object(pm2, "_run", return_value=(True, JLIST)):
            r = pm2.do_status()
        self.assertTrue(r["ok"])
        self.assertEqual(r["missing"], ["ghost"])

    def test_status_with_no_processes_denies(self):
        with mock.patch.object(pm2, "read", return_value={"health_url": "x"}):
            r = pm2.do_status()
        self.assertFalse(r["ok"])
        self.assertIn("no processes", r["output"])

    def test_restart_targets_pm2_and_respects_the_allowlist(self):
        fake = mock.Mock(returncode=0, stdout="restarted\n", stderr="")
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            r = pm2.do_lifecycle("restart", "myapp-api")
        self.assertEqual(r, {"ok": True, "exit": 0, "stdout": "restarted\n", "stderr": ""})
        self.assertEqual(run.call_args[0][0], ["pm2", "restart", "myapp-api"])

    def test_restart_denies_an_undeclared_process(self):
        # Fail-closed scoping: a proc not in `processes` is rejected pre-pm2.
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch("subprocess.run") as run:
            r = pm2.do_lifecycle("restart", "unrelated-host-proc")
        self.assertFalse(r["ok"])
        self.assertIn("not in processes", r["stderr"])
        run.assert_not_called()

    def test_stop_is_gated_behind_allow_lifecycle(self):
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch("subprocess.run") as run:
            r = pm2.do_lifecycle("stop", "myapp-api")
        self.assertFalse(r["ok"])
        self.assertIn("allow_lifecycle", r["stderr"])
        run.assert_not_called()
        fake = mock.Mock(returncode=0, stdout="stopped\n", stderr="")
        with mock.patch.object(pm2, "read",
                               return_value=dict(SAVED, allow_lifecycle=True)), \
             mock.patch("subprocess.run", return_value=fake) as run:
            r = pm2.do_lifecycle("stop", "myapp-api")
        self.assertTrue(r["ok"])
        self.assertEqual(run.call_args[0][0], ["pm2", "stop", "myapp-api"])

    def test_unknown_action_is_a_clean_error(self):
        with mock.patch.object(pm2, "read", return_value=SAVED):
            r = pm2.do_lifecycle("delete", "myapp-api")  # deliberately not a verb
        self.assertFalse(r["ok"])
        self.assertIn("unknown action", r["stderr"])

    def test_logs_tails_the_jlist_paths_for_a_declared_proc(self):
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch.object(pm2, "_run", return_value=(True, JLIST)), \
             mock.patch.object(pm2, "_tail", side_effect=["out-tail", "err-tail"]) as tail:
            r = pm2.do_logs("myapp-api", 50)
        self.assertTrue(r["ok"])
        self.assertEqual((r["out"], r["err"]), ("out-tail", "err-tail"))
        tail.assert_any_call("/tmp/api-out.log", 50)
        tail.assert_any_call("/tmp/api-err.log", 50)

    def test_logs_denies_an_undeclared_process(self):
        with mock.patch.object(pm2, "read", return_value=SAVED):
            r = pm2.do_logs("unrelated-host-proc")
        self.assertFalse(r["ok"])
        self.assertIn("not in processes", r["output"])

    def test_tail_is_bounded_not_a_whole_file_read(self):
        # #308: pm2 never rotates by default — a multi-GB log slurped via
        # readlines() hung the verb into an empty reply. _tail must return the
        # last N lines while reading only a bounded window from the end.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            for i in range(10_000):
                f.write(f"line-{i}\n")
            path = f.name
        try:
            out = pm2._tail(path, 3)
            self.assertEqual(out.splitlines(), ["line-9997", "line-9998", "line-9999"])
            # reads stay bounded even when the file has no newlines at all
            with open(path, "w") as f:
                f.write("x" * (9 * 1024 * 1024))
            capped = pm2._tail(path, 5)
            self.assertLessEqual(len(capped), 9 * 1024 * 1024)
            self.assertTrue(capped)  # still returns the tail window, no hang
        finally:
            Path(path).unlink()

    def test_tail_missing_file_reports_unreadable(self):
        self.assertIn("unreadable", pm2._tail("/nonexistent/x.log", 5))
        self.assertEqual(pm2._tail(None, 5), "")

    def test_health_curls_the_saved_url_host_side(self):
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch.object(pm2, "_fetch",
                               return_value={"ok": True, "code": 200, "body": "ok"}) as f:
            r = pm2.do_health()
        self.assertTrue(r["ok"])
        f.assert_called_once_with("http://127.0.0.1:8000/health")

    def test_health_without_a_url_is_a_clean_error(self):
        with mock.patch.object(pm2, "read", return_value={"processes": ["x"]}):
            r = pm2.do_health()
        self.assertFalse(r["ok"])
        self.assertIn("no health_url", r["error"])

    def test_configured_cli_reflects_a_linked_stack(self):
        # `./sc pm2-broker-up` calls `pm2.py configured` to self-skip when unlinked.
        with mock.patch.object(pm2, "read", return_value=SAVED):
            self.assertEqual(pm2.main(["configured"]), 0)
        with mock.patch.object(pm2, "read", return_value=None):
            self.assertEqual(pm2.main(["configured"]), 1)


class CheckTests(unittest.TestCase):
    """validate() runs one live check against a CANDIDATE block."""

    def test_daemon_passes_when_jlist_parses(self):
        with mock.patch.object(pm2, "_run", return_value=(True, JLIST)):
            r = pm2.validate("daemon", SAVED)
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "daemon")

    def test_daemon_fails_when_pm2_is_missing(self):
        with mock.patch.object(pm2, "_run",
                               return_value=(False, "command not found: pm2")):
            r = pm2.validate("daemon", SAVED)
        self.assertFalse(r["ok"])

    def test_procs_flags_a_missing_declared_process(self):
        cfg = dict(SAVED, processes=["myapp-api", "ghost"])
        with mock.patch.object(pm2, "_run", return_value=(True, JLIST)):
            r = pm2.validate("procs", cfg)
        self.assertFalse(r["ok"])
        self.assertIn("ghost", r["output"])

    def test_health_check_probes_the_candidate_url(self):
        with mock.patch.object(pm2, "_fetch",
                               return_value={"ok": True, "code": 200, "body": "ok"}):
            r = pm2.validate("health", SAVED)
        self.assertTrue(r["ok"])

    def test_unknown_check_is_none(self):
        self.assertIsNone(pm2.validate("nope", SAVED))


class SocketTransportTests(unittest.TestCase):
    """A live broker on a temp socket, driven by the real broker_call client —
    proves the unix-socket HTTP transport the container relies on actually works."""

    def setUp(self):
        self.sock = Path(__file__).resolve().parent / "_test_pm2_broker.sock"
        self._orig_socket = pm2.SOCKET
        pm2.SOCKET = self.sock  # both server (pm2_broker path) + client read this
        self.srv = pm2_broker.UnixHTTPServer(str(self.sock), pm2_broker.Handler)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        pm2.SOCKET = self._orig_socket
        self.sock.unlink(missing_ok=True)

    def test_health(self):
        r = pm2.broker_call("GET", "/health")
        self.assertEqual(r, {"ok": True, "service": "pm2-broker"})

    def test_unknown_route_is_404_shaped(self):
        r = pm2.broker_call("GET", "/nope")
        self.assertFalse(r["ok"])

    def test_status_round_trips_over_the_socket(self):
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch.object(pm2, "_run", return_value=(True, JLIST)):
            r = pm2.broker_call("GET", "/status")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["processes"]), 2)

    def test_restart_round_trips_over_the_socket(self):
        fake = mock.Mock(returncode=2, stdout="out", stderr="err")
        with mock.patch.object(pm2, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake):
            r = pm2.broker_call("POST", "/restart", {"proc": "myapp-api"})
        self.assertEqual(r["exit"], 2)
        self.assertEqual(r["stdout"], "out")

    def test_stop_gate_holds_over_the_socket(self):
        with mock.patch.object(pm2, "read", return_value=SAVED):
            r = pm2.broker_call("POST", "/stop", {"proc": "myapp-api"})
        self.assertFalse(r["ok"])
        self.assertIn("allow_lifecycle", r["stderr"])

    def test_validate_proxies_the_candidate_cfg_in_the_body(self):
        # The in-sandbox server proxies validate through exactly this path.
        with mock.patch.object(pm2, "_run", return_value=(True, JLIST)):
            r = pm2.broker_call("POST", "/validate/daemon", {"pm2": SAVED})
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "daemon")

    def test_broker_call_raises_when_nothing_listens(self):
        pm2.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            pm2.broker_call("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
