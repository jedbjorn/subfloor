#!/usr/bin/env python3
"""The Scripts page's Services panel (scripts/services.py + /api/services).

Stdlib unittest, no pytest. Probes (sockets, systemctl, docker) and the
dispatcher are mocked: these tests pin the contract — the five services in
display order, fail-closed `up` on an unconfigured service, sandbox refusal
with the host command, the fixed dispatcher verb per (key, action), and the
sidecar's enable-and-start — never the host's live state.

Run:
    python3 tests/test_services_panel.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402
import services  # noqa: E402


class _Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class ServicesStatusTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("SC_SANDBOX", None)
        self.addCleanup(self.env.stop)

    def test_five_services_in_display_order_with_contract_fields(self):
        with mock.patch.object(services.ports, "resolve", return_value={"port": 8800}), \
             mock.patch.object(services, "_broker_running", return_value=False), \
             mock.patch.object(services, "_systemd_state", return_value=(False, False)), \
             mock.patch.object(services, "_sidecar_running", return_value=None):
            data = services.list_services()
        self.assertTrue(data["host_lifecycle"])
        rows = data["services"]
        self.assertEqual([r["key"] for r in rows], ["ts", "vm", "pm2", "db", "pg"])
        for r in rows:
            self.assertFalse(r["configured"])
            self.assertTrue(r["onboarding"])
            self.assertTrue(r["host_command"].startswith("./sc "))
        self.assertEqual(rows[0]["actions"], ["up", "down", "install", "uninstall"])
        self.assertEqual(rows[4]["actions"], ["up", "down"])
        self.assertIsNone(rows[4]["running"], "no docker here → unknown, never False")
        self.assertEqual(rows[0]["host_command"], "./sc ts-broker-up")

    def test_configured_running_and_supervisor_projection(self):
        cfg = {"ts": {"allowed_hosts": ["a"]}, "pg": {}}
        with mock.patch.object(services.ports, "resolve", return_value=cfg), \
             mock.patch.object(services, "_broker_running", return_value=True), \
             mock.patch.object(services, "_systemd_state", return_value=(True, True)), \
             mock.patch.object(services, "_sidecar_running", return_value=True):
            ts = services.status("ts")
            pg = services.status("pg")
        self.assertTrue(ts["configured"] and ts["running"] and ts["persistent"])
        self.assertEqual(ts["supervisor"], "systemd")
        self.assertTrue(pg["configured"] and pg["running"])
        self.assertIsNone(pg["persistent"])
        self.assertEqual(pg["supervisor"], "pidfile")

    def test_broker_probe_treats_unreachable_socket_as_stopped(self):
        with mock.patch.object(services.ts, "broker_call", side_effect=ConnectionError("no sock")):
            self.assertFalse(services._broker_running(services.SERVICES["ts"]))
        with mock.patch.object(services.ts, "broker_call", return_value={"ok": True}):
            self.assertTrue(services._broker_running(services.SERVICES["ts"]))

    def test_sandbox_reports_status_but_not_lifecycle(self):
        os.environ["SC_SANDBOX"] = "1"
        with mock.patch.object(services.ports, "resolve", return_value={}), \
             mock.patch.object(services, "_broker_running", return_value=False), \
             mock.patch.object(services, "_systemd_state", return_value=(None, False)), \
             mock.patch.object(services, "_sidecar_running", return_value=None):
            self.assertFalse(services.list_services()["host_lifecycle"])


class ServicesLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("SC_SANDBOX", None)
        self.addCleanup(self.env.stop)
        self.calls: list[list[str]] = []

        def fake_run(argv, **kw):
            self.calls.append(argv)
            return _Done(0, f"→ {argv[-1]} ok")
        self.run_patch = mock.patch.object(services.subprocess, "run", side_effect=fake_run)
        self.run_patch.start(); self.addCleanup(self.run_patch.stop)
        self.status_patch = mock.patch.object(services, "_broker_running", return_value=False)
        self.status_patch.start(); self.addCleanup(self.status_patch.stop)
        mock.patch.object(services, "_systemd_state", return_value=(False, False)).start()
        mock.patch.object(services, "_sidecar_running", return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def _cfg(self, cfg):
        p = mock.patch.object(services.ports, "resolve", return_value=cfg)
        p.start(); self.addCleanup(p.stop)

    def _verbs(self):
        return [argv[-1] for argv in self.calls if argv[0] == "sh"]

    def test_unknown_key_or_action_is_none(self):
        self._cfg({"ts": {}})
        self.assertIsNone(services.run("nope", "up"))
        self.assertIsNone(services.run("ts", "restart"))
        self.assertIsNone(services.run("pg", "install"), "sidecar has no systemd unit")
        self.assertEqual(self.calls, [])

    def test_up_on_unconfigured_broker_returns_onboarding_without_running(self):
        self._cfg({})
        r = services.run("ts", "up")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "not_configured")
        self.assertEqual(r["output"], services.SERVICES["ts"]["onboarding"])
        self.assertFalse(r["service"]["configured"])
        self.assertEqual(self._verbs(), [])

    def test_actions_map_to_fixed_dispatcher_verbs(self):
        self._cfg({"ts": {"allowed_hosts": ["a"]}, "pm2": {"processes": ["x"]}})
        for key, action, verb in [("ts", "up", "ts-broker-up"), ("ts", "down", "ts-broker-down"),
                                  ("pm2", "install", "pm2-broker-install"),
                                  ("pm2", "uninstall", "pm2-broker-uninstall")]:
            self.calls.clear()
            r = services.run(key, action)
            self.assertTrue(r["ok"], r)
            self.assertEqual(self._verbs(), [verb])
            argv = self.calls[0]
            self.assertEqual(argv[:2], ["sh", str(services.DISPATCH)])
            self.assertIn("service", r)

    def test_dispatch_runs_from_repo_root_as_the_caller(self):
        self._cfg({"ts": {}})
        captured = {}

        def fake_run(argv, **kw):
            captured.update(kw); return _Done(0, "ok")
        with mock.patch.object(services.subprocess, "run", side_effect=fake_run):
            services.run("ts", "up")
        self.assertEqual(captured["cwd"], str(services.REPO_ROOT))
        self.assertEqual(captured["env"]["SC_CALLER_ROOT"], str(services.REPO_ROOT))

    def test_down_is_allowed_even_when_unconfigured(self):
        # A block removed while the broker still runs must remain stoppable.
        self._cfg({})
        r = services.run("ts", "down")
        self.assertTrue(r["ok"])
        self.assertEqual(self._verbs(), ["ts-broker-down"])

    def test_sidecar_enable_and_start_runs_init_then_up(self):
        self._cfg({})
        self.assertEqual(services.run("pg", "up")["code"], "not_configured")
        self.calls.clear()
        r = services.run("pg", "up", init=True)
        self.assertTrue(r["ok"])
        self.assertEqual(self._verbs(), ["pg-init", "pg-up"])
        self.assertIn("pg-init ok", r["output"])
        self.assertIn("pg-up ok", r["output"])

    def test_sidecar_init_failure_stops_before_up(self):
        self._cfg({})

        def fail_init(argv, **kw):
            self.calls.append(argv)
            return _Done(1, "", "✗ init failed") if argv[-1] == "pg-init" else _Done(0, "up")
        with mock.patch.object(services.subprocess, "run", side_effect=fail_init):
            r = services.run("pg", "up", init=True)
        self.assertFalse(r["ok"])
        self.assertEqual(self._verbs(), ["pg-init"])
        self.assertIn("init failed", r["output"])

    def test_configured_sidecar_up_skips_init(self):
        self._cfg({"pg": {}})
        services.run("pg", "up", init=True)
        self.assertEqual(self._verbs(), ["pg-up"])

    def test_sandbox_refuses_lifecycle_with_host_command(self):
        self._cfg({"ts": {}})
        os.environ["SC_SANDBOX"] = "1"
        r = services.run("ts", "up")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "host_required")
        self.assertIn("./sc ts-broker-up", r["output"])
        self.assertEqual(self.calls, [])

    def test_dispatch_timeout_is_a_failure_result(self):
        self._cfg({"ts": {}})
        with mock.patch.object(services.subprocess, "run",
                               side_effect=services.subprocess.TimeoutExpired("sh", 120)):
            r = services.run("ts", "up")
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["output"])


class ServerWiringTest(unittest.TestCase):
    def test_server_exposes_services_module_and_routes(self):
        self.assertIs(server.services_mod, services)
        src = Path(server.__file__).read_text()
        self.assertIn('if path == "/api/services":', src)
        self.assertIn('if path.startswith("/api/services/"):', src)
        # Status codes the UI keys off: sandbox → 503, unconfigured → 409.
        self.assertIn('if r["code"] == "host_required":\n                    return self._send(503, r)', src)
        self.assertIn('if r["code"] == "not_configured":\n                    return self._send(409, r)', src)


if __name__ == "__main__":
    unittest.main()
