#!/usr/bin/env python3
"""Interface CLI — hermetic verb/routing proofs (spec #20, sprint 25 seq 6).

Covers `sc interface` + the `sc enter` routing decision WITHOUT a server,
a socket, or tmux: the module's one network seam (`_http`) is a fake
transport that records every urllib Request (so Idempotency-Key, the
operator bearer, method, path, and body are all asserted on the REAL api()
wrapper), and the WS loop (`run_stream`) is a mock — the verbs are tested
up to the point a socket would open.

Also covers the run.py raw-launch refusal (spec #20 Tmux Runtime): the
public interactive entry refuses before an archive exists; the escape
hatch, headless (`sc run`), and RENDER_ONLY paths pass the gate.

Run:
    python3 tests/test_interface_cli.py
"""
from __future__ import annotations

import contextlib
import email.message
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import interface_cli as ic  # noqa: E402
import run as run_mod  # noqa: E402

SHELLS = {
    "shells": [
        {"shell_id": 1, "shortname": "S1", "display_name": "One",
         "availability": "available", "session_id": None, "lifecycle": None,
         "harness": None, "composer": None, "alerts": 0,
         "wake_state": "disarmed"},
        {"shell_id": 2, "shortname": "S2", "display_name": "Two",
         "availability": "occupied", "session_id": 7, "lifecycle": "idle",
         "harness": "claude", "composer": "clean", "alerts": 0,
         "wake_state": "disarmed"},
        {"shell_id": 3, "shortname": "S3", "display_name": "Three",
         "availability": "lost", "session_id": 9, "lifecycle": "lost",
         "harness": "claude", "composer": "unknown", "alerts": 1,
         "wake_state": "disarmed"},
    ]
}

SESSION7 = {
    "session_id": 7, "shell_id": 2, "generation": 1, "archive_id": 20,
    "harness": "claude", "model_route": None, "worktree": "/x/s2",
    "occupancy": "occupied", "lifecycle": "idle", "composer": "clean",
    "delivery": "idle", "forwarded_seq": 4, "last_human_input_at": None,
    "writer": {"held": True, "client_id": "web-1"},
    "wake_state": "disarmed", "clients": 1, "alerts": 0,
    "created_at": "t", "occupied_at": "t", "ended_at": None,
    "end_reason": None, "error_detail": None,
}


def http_error(status: int, code: str, message: str,
               details=None) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message,
                                 "details": details or {}}}).encode()
    return urllib.error.HTTPError("http://x", status, message,
                                  email.message.Message(),
                                  io.BytesIO(body))


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeHTTP:
    """The `_http` seam: routes (method, path) → payload or exception."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def add(self, method, path, payload):
        self.routes[(method, path)] = payload

    def __call__(self, req):
        path = req.full_url.replace(ic.API_BASE, "")
        self.calls.append({
            "method": req.method,
            "path": path,
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": json.loads(req.data) if req.data else None,
        })
        outcome = self.routes[(req.method, path)]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResp(outcome)

    def find(self, method, path):
        return [c for c in self.calls
                if c["method"] == method and c["path"] == path]


class InterfaceCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        token = Path(self.tmp.name) / "operator.token"
        token.write_text("optok")
        self.http = FakeHTTP()
        self.stream = mock.Mock(return_value=0)
        patches = [
            mock.patch.object(ic, "OPERATOR_TOKEN_PATH", token),
            mock.patch.object(ic, "_http", self.http),
            mock.patch.object(ic, "run_stream", self.stream),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Default routes: the shell rail + the occupied session's detail.
        self.http.add("GET", "/api/interface/shells", SHELLS)
        self.http.add("GET", "/api/interface/sessions/7", SESSION7)
        self.http.add("POST", "/api/interface/stream-tickets",
                      {"ticket": "tk-1", "expires_in": 60})
        self.http.add("POST", "/api/interface/writer-leases",
                      {"lease_id": 5, "lease_token": "lt-1",
                       "next_input_seq": 5})

    # -- helpers -------------------------------------------------------------

    def run_cli(self, argv):
        """Returns (exit_code, stdout, stderr); SystemExit becomes its code."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                rc = ic.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    # -- envelope: bearer + idempotency on the real api() wrapper -------------

    def test_operator_bearer_and_idempotency_key(self):
        self.run_cli(["status"])
        call = self.http.find("GET", "/api/interface/shells")[0]
        self.assertEqual(call["headers"].get("authorization"),
                         "Bearer optok")
        self.assertNotIn("idempotency-key", call["headers"])
        self.http.add("POST", "/api/interface/reconciliations",
                      {"session_id": 7, "verified": True,
                       "occupancy": "occupied", "actions": []})
        self.run_cli(["reconcile", "s2"])
        call = self.http.find("POST", "/api/interface/reconciliations")[0]
        key = call["headers"].get("idempotency-key")
        self.assertTrue(key, "every mutation sends an Idempotency-Key")
        self.assertEqual(len(key), 36, "uuid4")

    # -- status ---------------------------------------------------------------

    def test_status_rail_json(self):
        rc, out, _ = self.run_cli(["status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual([s["shortname"] for s in payload["shells"]],
                         ["S1", "S2", "S3"])
        self.assertEqual(payload["shells"][1]["availability"], "occupied")

    def test_status_named_shell_fetches_session(self):
        rc, out, _ = self.run_cli(["status", "s2", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.http.find("GET", "/api/interface/sessions/7"),
                         self.http.find("GET", "/api/interface/sessions/7"))
        payload = json.loads(out)
        self.assertEqual(payload["shell"]["session_id"], 7)
        self.assertEqual(payload["session"]["occupancy"], "occupied")
        self.assertEqual(payload["session"]["writer"]["client_id"], "web-1")
        # Human mode names the writer holder.
        rc, out, _ = self.run_cli(["status", "s2"])
        self.assertIn("held by web-1", out)

    def test_status_available_shell_has_no_session_fetch(self):
        rc, out, _ = self.run_cli(["status", "s1", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertIsNone(payload["session"])
        self.assertEqual(self.http.find("GET", "/api/interface/sessions/7"),
                         [])

    # -- start (New chat) -------------------------------------------------------

    def test_start_posts_session_with_route_hints(self):
        self.http.add("POST", "/api/interface/sessions",
                      {"session_id": 11, "shell_id": 1, "generation": 1,
                       "occupancy": "reserved", "lifecycle": "starting",
                       "harness": "claude"})
        rc, out, _ = self.run_cli(
            ["start", "s1", "--harness", "claude", "--model", "m1",
             "--effort", "high", "--json"])
        self.assertEqual(rc, 0)
        call = self.http.find("POST", "/api/interface/sessions")[0]
        self.assertEqual(call["body"]["shell_id"], 1)
        self.assertEqual(call["body"]["harness"], "claude")
        self.assertEqual(call["body"]["model"], "m1")
        self.assertEqual(call["body"]["effort"], "high")
        self.assertIn("rows", call["body"])
        self.assertIn("cols", call["body"])
        self.assertEqual(json.loads(out)["session_id"], 11)

    def test_start_occupied_race_reports_existing_session(self):
        self.http.add("POST", "/api/interface/sessions",
                      http_error(409, "shell_occupied", "a live generation "
                                 "already owns this shell",
                                 {"session_id": 7, "occupancy": "occupied"}))
        rc, _, err = self.run_cli(["start", "s2"])
        self.assertEqual(rc, 1)
        self.assertIn("session 7", err)
        self.assertIn("attach", err)

    # -- attach verbs ---------------------------------------------------------

    def test_view_mints_viewer_ticket_without_lease(self):
        rc, _, _ = self.run_cli(["view", "s2"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.http.find("POST", "/api/interface/writer-leases"),
                         [])
        call = self.http.find("POST", "/api/interface/stream-tickets")[0]
        self.assertEqual(call["body"]["role"], "viewer")
        self.assertEqual(call["body"]["session_id"], 7)
        self.assertNotIn("lease_token", call["body"])
        args = self.stream.call_args[0]
        self.assertTrue(args[0].startswith("ws://127.0.0.1:"))
        self.assertIn("/api/interface/session-streams/7?ticket=tk-1", args[0])
        self.assertEqual(args[1], "viewer")

    def test_attach_acquires_lease_then_writer_ticket(self):
        rc, _, _ = self.run_cli(["attach", "s2"])
        self.assertEqual(rc, 0)
        lease = self.http.find("POST", "/api/interface/writer-leases")[0]
        self.assertFalse(lease["body"]["takeover"])
        ticket = self.http.find("POST", "/api/interface/stream-tickets")[0]
        self.assertEqual(ticket["body"]["role"], "writer")
        self.assertEqual(ticket["body"]["lease_token"], "lt-1")
        args = self.stream.call_args[0]
        self.assertEqual(args[1], "writer")
        # The input seq continues the SESSION's sequence (next_input_seq),
        # never reset to 1 — a reset would wedge the broker's gap detection.
        self.assertEqual(args[2], 5)

    def test_attach_held_lease_refuses_without_takeover(self):
        self.http.add("POST", "/api/interface/writer-leases",
                      http_error(409, "lease_refused", "session 7 writer "
                                 "held by web-1 — explicit takeover required"))
        rc, _, err = self.run_cli(["attach", "s2"])
        self.assertEqual(rc, 1)
        self.assertIn("take-control", err)
        self.assertIn("web-1", err)
        self.assertEqual(self.http.find("POST", "/api/interface/stream-tickets"),
                         [], "a refused lease must not mint a ticket")
        self.stream.assert_not_called()

    def test_take_control_passes_takeover(self):
        rc, _, _ = self.run_cli(["take-control", "s2"])
        self.assertEqual(rc, 0)
        lease = self.http.find("POST", "/api/interface/writer-leases")[0]
        self.assertTrue(lease["body"]["takeover"])
        self.assertEqual(self.stream.call_args[0][1], "writer")

    def test_view_without_session_refuses(self):
        rc, _, err = self.run_cli(["view", "s1"])
        self.assertEqual(rc, 1)
        self.assertIn("no live Interface session", err)

    # -- stop / reconcile -----------------------------------------------------

    def test_stop_graceful_then_json(self):
        self.http.add("POST", "/api/interface/termination-requests",
                      {"terminated": True})
        rc, out, _ = self.run_cli(["stop", "s2", "--json"])
        self.assertEqual(rc, 0)
        call = self.http.find("POST", "/api/interface/termination-requests")[0]
        self.assertEqual(call["body"],
                         {"session_id": 7, "force": False})
        self.assertTrue(json.loads(out)["terminated"])

    def test_stop_graceful_timeout_names_force_followup(self):
        self.http.add("POST", "/api/interface/termination-requests",
                      {"terminated": False, "reason": "graceful_timeout",
                       "pid": 4321, "generation": 1})
        rc, out, _ = self.run_cli(["stop", "s2"])
        self.assertEqual(rc, 1, "not-terminated is a non-zero exit")
        self.assertIn("--force", out)
        self.assertIn("4321", out)

    def test_stop_force_gate_error_explains(self):
        self.http.add("POST", "/api/interface/termination-requests",
                      http_error(409, "force_requires_graceful_timeout",
                                 "force is available only after a graceful "
                                 "termination timed out"))
        rc, _, err = self.run_cli(["stop", "s2", "--force"])
        self.assertEqual(rc, 1)
        self.assertIn("graceful", err)
        call = self.http.find("POST", "/api/interface/termination-requests")[0]
        self.assertTrue(call["body"]["force"])

    def test_reconcile_verify_and_close(self):
        self.http.add("POST", "/api/interface/reconciliations",
                      {"session_id": 9, "verified": False,
                       "occupancy": "unreconciled",
                       "actions": ["identity could not be verified"]})
        rc, out, _ = self.run_cli(["reconcile", "s3", "--json"])
        self.assertEqual(rc, 0)
        call = self.http.find("POST", "/api/interface/reconciliations")[0]
        self.assertEqual(call["body"], {"session_id": 9, "action": "verify"})
        self.assertFalse(json.loads(out)["verified"])
        rc, _, _ = self.run_cli(["reconcile", "s3", "--close"])
        self.assertEqual(rc, 0)
        call = self.http.find("POST", "/api/interface/reconciliations")[-1]
        self.assertEqual(call["body"]["action"], "close")

    # -- API outage ---------------------------------------------------------------

    def test_api_unreachable_reports_supervised_remediation(self):
        def boom(req):
            raise urllib.error.URLError("connection refused")
        with mock.patch.object(ic, "_http", boom):
            rc, _, err = self.run_cli(["status"])
        self.assertEqual(rc, 3)
        self.assertIn("unreachable", err)
        self.assertIn("supervised", err)
        self.assertIn("./sc restart", err)
        self.assertIn("no direct-DB or tmux fallback", err)

    def test_missing_operator_capability_is_api_down(self):
        with mock.patch.object(ic, "OPERATOR_TOKEN_PATH",
                               Path(self.tmp.name) / "nope"):
            rc, _, err = self.run_cli(["status"])
        self.assertEqual(rc, 3)
        self.assertIn("operator capability", err)

    # -- enter routing ---------------------------------------------------------

    def test_enter_available_picks_starts_attaches_writer(self):
        self.http.add("POST", "/api/interface/sessions",
                      {"session_id": 11, "shell_id": 1, "generation": 1,
                       "occupancy": "reserved", "lifecycle": "starting",
                       "harness": "claude"})
        with mock.patch.object(ic, "_pick_harness", return_value="claude"), \
                mock.patch.object(ic, "_wait_occupied",
                                  return_value={"occupancy": "occupied"}):
            rc, out, _ = self.run_cli(["enter", "s1"])
        self.assertEqual(rc, 0)
        # New chat through the reservation BEFORE the writer attach.
        self.assertEqual(len(self.http.find("POST", "/api/interface/sessions")),
                         1)
        lease = self.http.find("POST", "/api/interface/writer-leases")[0]
        self.assertEqual(lease["body"]["session_id"], 11)
        self.assertFalse(lease["body"]["takeover"])
        self.assertEqual(self.stream.call_args[0][1], "writer")

    def test_enter_occupied_lease_free_attaches_writer(self):
        self.http.add("POST", "/api/interface/writer-leases",
                      {"lease_id": 6, "lease_token": "lt-2",
                       "next_input_seq": 5})
        rc, _, _ = self.run_cli(["enter", "s2"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.http.find("POST", "/api/interface/sessions"), [])
        self.assertEqual(self.stream.call_args[0][1], "writer")

    def test_enter_occupied_lease_held_falls_back_readonly(self):
        self.http.add("POST", "/api/interface/writer-leases",
                      http_error(409, "lease_refused", "writer held by web-1"))
        rc, _, err = self.run_cli(["enter", "s2"])
        self.assertEqual(rc, 0)
        self.assertIn("READ-ONLY", err)
        self.assertIn("take-control", err)
        ticket = self.http.find("POST", "/api/interface/stream-tickets")[0]
        self.assertEqual(ticket["body"]["role"], "viewer")
        self.assertEqual(self.stream.call_args[0][1], "viewer")

    def test_enter_starting_or_lost_refuses(self):
        rc, _, err = self.run_cli(["enter", "s3"])
        self.assertEqual(rc, 1)
        self.assertIn("lost", err)
        self.assertIn("reconcile", err)
        self.assertEqual(self.http.find("POST", "/api/interface/sessions"), [])
        self.stream.assert_not_called()


class RawLaunchRefusalTest(unittest.TestCase):
    """run.py's public interactive entry refuses without the reservation
    capability — before open_db/open_session can create an archive."""

    def _main(self, argv, env):
        with mock.patch.dict(run_mod.os.environ, env, clear=True), \
                mock.patch.object(run_mod.sys, "argv", argv), \
                mock.patch.object(run_mod, "open_db",
                                  side_effect=KeyError("reached boot")):
            run_mod.main()

    def test_interactive_boot_refuses_before_archive(self):
        with self.assertRaises(SystemExit) as cm:
            self._main(["run.py", "dev3"], {})
        self.assertIn("./sc enter", str(cm.exception.code))
        self.assertIn("Interface", str(cm.exception.code))

    def test_escape_hatch_passes_the_gate(self):
        # SC_RAW_BOOT is tooling's explicit opt-in (like SC_NO_AUTOPRUNE);
        # reaching open_db proves the gate did not fire.
        with self.assertRaises(KeyError):
            self._main(["run.py", "dev3"], {"SC_RAW_BOOT": "1"})

    def test_headless_sc_run_passes_the_gate(self):
        with self.assertRaises(KeyError):
            self._main(["run.py", "--headless", "dev3"], {})

    def test_render_only_passes_the_gate(self):
        with self.assertRaises(KeyError):
            self._main(["run.py", "--first"], {"RENDER_ONLY": "1"})


if __name__ == "__main__":
    unittest.main()
