#!/usr/bin/env python3
"""Interface CLI — hermetic verb/routing proofs (spec #20, sprint 25 seq 6).

Covers `sc interface` + the `sc enter` routing decision WITHOUT a server,
a socket, or tmux: the module's one network seam (`_http`) is a fake
transport that records every urllib Request (so Idempotency-Key, the
operator bearer, method, path, and body are all asserted on the REAL api()
wrapper), and the WS loop (`run_stream`) is a mock — the verbs are tested
up to the point a socket would open.

The real `run_stream` is covered separately (RunStreamTest) against a
scripted FakeWS peer + a scripted stdin: ack-gating (one unacknowledged
input frame), the read-only flip on lease loss, and quiet control frames.

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
import queue
import sys
import tempfile
import threading
import time
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
         "flavor": "dev", "default_harness": "codex",
         "default_model": "openai/gpt-5.6-sol",
         "availability": "available", "session_id": None, "lifecycle": None,
         "harness": None, "composer": None, "alerts": 0,
         "wake_state": "disarmed"},
        {"shell_id": 2, "shortname": "S2", "display_name": "Two",
         "flavor": "dev", "default_harness": "codex",
         "default_model": "openai/gpt-5.6-sol",
         "availability": "occupied", "session_id": 7, "lifecycle": "idle",
         "harness": "claude", "composer": "clean", "alerts": 0,
         "wake_state": "disarmed"},
        {"shell_id": 3, "shortname": "S3", "display_name": "Three",
         "flavor": "reviewer", "default_harness": "claude",
         "default_model": "opus",
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

    def test_status_states_the_launch_route_as_the_launch_route(self):
        """flag #130 / decision #55 — `model_route` is only what the session
        was LAUNCHED with, so status reports it under `launched` and never
        under a bare `model` key that would read as the live model. Same rule
        as the browser rail, which is the parity the sprint unit requires."""
        self.http.add("GET", "/api/interface/sessions/7",
                      {**SESSION7, "model_route": "fable"})
        rc, out, _ = self.run_cli(["status", "s2"])
        self.assertEqual(rc, 0)
        self.assertIn("launched    fable", out)
        self.assertNotIn("model", out)

    def test_status_launch_route_falls_back_to_the_harness_default(self):
        self.http.add("GET", "/api/interface/sessions/7",
                      {**SESSION7, "model_route": None})
        rc, out, _ = self.run_cli(["status", "s2"])
        self.assertEqual(rc, 0)
        self.assertIn("launched    harness default", out)

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

    def test_start_names_wait_or_legal_cancel_action(self):
        self.http.add("POST", "/api/interface/sessions",
                      {"session_id": 11, "shell_id": 1, "generation": 1,
                       "occupancy": "reserved", "lifecycle": "starting",
                       "harness": "claude"})
        rc, out, _ = self.run_cli(["start", "s1"])
        self.assertEqual(rc, 0)
        self.assertIn("wait for it to become occupied", out)
        self.assertIn("stop S1", out)
        self.assertNotIn("interface attach", out)

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
                      http_error(409, "writer_held", "session 7 writer "
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
        self.assertEqual(
            len(self.http.find("GET", "/api/interface/shells")), 1,
            "direct selection must reuse the API rail response, not fetch a "
            "second potentially different snapshot",
        )
        self.assertEqual(self.http.find("POST", "/api/interface/sessions"), [])
        self.assertEqual(self.stream.call_args[0][1], "writer")

    def test_enter_occupied_lease_held_falls_back_readonly(self):
        self.http.add("POST", "/api/interface/writer-leases",
                      http_error(409, "writer_held", "writer held by web-1"))
        rc, _, err = self.run_cli(["enter", "s2"])
        self.assertEqual(rc, 0)
        self.assertIn("READ-ONLY", err)
        self.assertIn("take-control", err)
        ticket = self.http.find("POST", "/api/interface/stream-tickets")[0]
        self.assertEqual(ticket["body"]["role"], "viewer")
        self.assertEqual(self.stream.call_args[0][1], "viewer")

    def test_enter_starting_or_lost_refuses(self):
        starting = json.loads(json.dumps(SHELLS))
        starting["shells"][2].update(
            availability="starting", lifecycle="starting")
        self.http.add("GET", "/api/interface/shells", starting)
        rc, _, err = self.run_cli(["enter", "s3"])
        self.assertEqual(rc, 1)
        self.assertIn("stop S3", err)
        self.assertNotIn("interface view", err)
        self.assertEqual(self.http.find("POST", "/api/interface/stream-tickets"),
                         [])
        self.stream.assert_not_called()

        self.http.add("GET", "/api/interface/shells", SHELLS)
        rc, _, err = self.run_cli(["enter", "s3"])
        self.assertEqual(rc, 1)
        self.assertIn("lost", err)
        self.assertIn("reconcile", err)
        self.assertEqual(self.http.find("POST", "/api/interface/sessions"), [])
        self.stream.assert_not_called()

    def test_harness_default_comes_from_api_projection_without_db_read(self):
        args = mock.Mock(harness=None)
        with mock.patch.dict(ic.os.environ, {}, clear=True), \
                mock.patch.object(run_mod, "detect_harnesses",
                                  return_value=["codex"]), \
                mock.patch.object(run_mod.db_driver, "connect",
                                  side_effect=AssertionError(
                                      "CLI must not read Interface state "
                                      "from the DB")):
            picked = ic._pick_harness(SHELLS["shells"][0], args)
        self.assertEqual(picked, "codex")

    def test_grouped_picker_snapshot_and_selection_order(self):
        shells = [
            {"shell_id": 1, "shortname": "REV1",
             "display_name": "Reviewer", "flavor": "reviewer",
             "availability": "lost", "default_harness": "claude",
             "default_model": "opus"},
            {"shell_id": 2, "shortname": "DEV1",
             "display_name": "Builder", "flavor": "dev",
             "availability": "available", "default_harness": "codex",
             "default_model": "openai/gpt-5.6-sol"},
            {"shell_id": 3, "shortname": "CUSTOM-LONG-NAME",
             "display_name": "A bespoke shell with a long display name",
             "flavor": None, "availability": None,
             "default_harness": None, "default_model": None},
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(ic.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(ic.shutil, "get_terminal_size",
                                  return_value=ic.os.terminal_size((100, 24))), \
                mock.patch("builtins.input", return_value="2"):
            chosen = ic._pick_shell(shells, None)
        self.assertEqual(chosen["shortname"], "REV1")
        self.assertEqual(
            out.getvalue(),
            "\nShells\n"
            "  #  Name              Shortname    State          "
            "Default (harness · model)     \n"
            "\n"
            "dev\n"
            "  1  Builder           DEV1         available      "
            "codex · gpt-5.6-sol\n"
            "\n"
            "reviewer\n"
            "  2  Reviewer          REV1         lost           "
            "claude · opus\n"
            "\n"
            "(bespoke)\n"
            "  3  A bespoke shell … CUSTOM-LONG… unknown        \n",
        )

    def test_grouped_picker_is_readable_in_a_narrow_terminal(self):
        shell = {
            "shell_id": 1,
            "shortname": "EXTRAORDINARILY-LONG",
            "display_name": "A very long display name",
            "flavor": "dev",
            "availability": "starting",
            "default_harness": "codex",
            "default_model": "openai/gpt-5.6-sol",
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(ic.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(ic.shutil, "get_terminal_size",
                                  return_value=ic.os.terminal_size((36, 24))), \
                mock.patch("builtins.input", return_value="1"):
            chosen = ic._pick_shell([shell], None)
        self.assertEqual(chosen["shell_id"], 1)
        lines = out.getvalue().splitlines()
        self.assertLessEqual(max(map(len, lines)), 36)
        self.assertIn("EXTRAORDINARI…", out.getvalue())
        self.assertIn("starting", out.getvalue())
        self.assertIn("codex · gpt-5.6-sol", out.getvalue())

    def test_grouped_picker_can_be_cancelled_without_an_action(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(ic.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(ic.shutil, "get_terminal_size",
                                  return_value=ic.os.terminal_size((80, 24))), \
                mock.patch("builtins.input", return_value="q"):
            with self.assertRaises(SystemExit) as raised:
                ic._pick_shell([SHELLS["shells"][0]], None)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("selection cancelled", err.getvalue())


class FakeWS:
    """A scripted sc-term.v1 peer for the REAL run_stream: outbound frames
    are recorded in `sent`; inbound frames are fed by the test through a
    queue so the receive loop blocks exactly like a socket would."""

    _END = object()

    def __init__(self):
        self.sent = []
        self._q = queue.Queue()

    def send(self, data):
        self.sent.append(data)

    def feed(self, frame):
        self._q.put(frame)

    def end(self):
        self._q.put(self._END)

    def __iter__(self):
        while True:
            item = self._q.get()
            if item is self._END:
                return
            yield item

    def input_frames(self):
        return [f for f in self.sent if f[:1] == b"\x01"]


class Transcript:
    """One ordered buffer behind BOTH streams run_stream writes to: pane
    payloads (stdout.buffer, bytes) and notices (stderr, text).

    Capturing the two separately records what each stream said but never
    which came first — and for the GUI notice the order IS the feature, so
    a separate-capture test passes just as happily with the notice emitted
    before the redraw that paints over it. `text` is the terminal's own
    view: the bytes in the order the operator's terminal received them.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._pane = bytearray()
        self.buffer = _TranscriptBytes(self)     # the stdout.buffer stand-in

    def write(self, text: str) -> int:           # the stderr (text) side
        with self._lock:
            self._parts.append(text)
        return len(text)

    def _write_pane(self, data: bytes) -> int:
        with self._lock:
            self._pane += data
            self._parts.append(data.decode("utf-8", "replace"))
        return len(data)

    def flush(self):
        pass

    def text(self) -> str:
        """Both streams, interleaved in write order."""
        with self._lock:
            return "".join(self._parts)

    def pane(self) -> bytes:
        """Only the bytes that went to the pane."""
        with self._lock:
            return bytes(self._pane)


class _TranscriptBytes:
    """The binary half of a Transcript — stands in for sys.stdout.buffer."""

    def __init__(self, transcript: Transcript):
        self._t = transcript

    def write(self, data: bytes) -> int:
        return self._t._write_pane(data)

    def flush(self):
        pass


class StdinScript:
    """A scripted stdin for run_stream's input seams: the test feeds reads;
    `ready` blocks (like select) until one is pending."""

    def __init__(self):
        self._cv = threading.Condition()
        self._reads = []

    def feed(self, data: bytes):
        with self._cv:
            self._reads.append(data)
            self._cv.notify_all()

    def ready(self, timeout: float) -> bool:
        with self._cv:
            if not self._reads:
                self._cv.wait(timeout)
            return bool(self._reads)

    def read(self) -> bytes:
        with self._cv:
            return self._reads.pop(0)


def wait_for(cond, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


class RunStreamTest(unittest.TestCase):
    """The real run_stream against FakeWS + StdinScript: client-side broker
    protocol semantics (spec #20 Input Broker — review r1 M1)."""

    def run_stream(self, ws, stdin, role="writer", start_seq=5, err=None):
        """Drives run_stream on a thread; returns (thread, stderr sink).
        The caller feeds frames/keystrokes, then `ws.end()` and joins.

        `err` defaults to a private StringIO; pass a Transcript to capture
        stderr in the same buffer as the pane bytes."""
        err = io.StringIO() if err is None else err
        patches = [
            mock.patch.object(ic, "_stdin_ready", stdin.ready),
            mock.patch.object(ic, "_read_stdin", stdin.read),
            mock.patch.object(ic, "_ws_connect", return_value=ws),
            # run_stream installs its SIGWINCH handler unconditionally; off
            # the main thread that raises — the handler is untestable noise.
            mock.patch.object(ic.signal, "signal"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        redir = contextlib.redirect_stderr(err)
        redir.__enter__()
        self.addCleanup(redir.__exit__, None, None, None)
        t = threading.Thread(target=ic.run_stream,
                             args=("ws://x", role, start_seq), daemon=True)
        t.start()
        self.addCleanup(t.join, 10)
        return t, err

    @staticmethod
    def _seq(frame):
        return int.from_bytes(frame[1:9], "big")

    def test_input_is_ack_gated_one_unacked_frame(self):
        ws, stdin = FakeWS(), StdinScript()
        t, _ = self.run_stream(ws, stdin)
        # Resize goes out immediately (0x03); keystrokes wait on the gate.
        self.assertTrue(wait_for(lambda: any(f[:1] == b"\x03"
                                             for f in ws.sent)))
        stdin.feed(b"a")
        self.assertTrue(wait_for(lambda: len(ws.input_frames()) == 1))
        self.assertEqual(self._seq(ws.input_frames()[0]), 5)
        # A second keystroke with no ack must NOT hit the wire.
        stdin.feed(b"b")
        time.sleep(0.3)
        self.assertEqual(len(ws.input_frames()), 1,
                         "one unacknowledged frame — b must buffer locally")
        # The ack releases exactly the next buffered frame.
        ws.feed(json.dumps({"type": "input_ack", "seq": 5}))
        self.assertTrue(wait_for(lambda: len(ws.input_frames()) == 2))
        self.assertEqual(self._seq(ws.input_frames()[1]), 6)
        self.assertEqual(ws.input_frames()[1][9:], b"b")
        # A non-terminal reject also settles the inflight frame (loudly) —
        # the buffer keeps draining without an ack.
        ws.feed(json.dumps({"type": "input_reject", "seq": 6,
                            "reason": "delivery_unknown"}))
        stdin.feed(b"c")
        self.assertTrue(wait_for(lambda: len(ws.input_frames()) == 3))
        self.assertEqual(ws.input_frames()[2][9:], b"c")
        ws.end()
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_writer_revoked_reject_flips_readonly(self):
        ws, stdin = FakeWS(), StdinScript()
        t, err = self.run_stream(ws, stdin)
        stdin.feed(b"a")
        self.assertTrue(wait_for(lambda: len(ws.input_frames()) == 1))
        ws.feed(json.dumps({"type": "input_reject", "seq": 5,
                            "reason": "writer_revoked"}))
        self.assertTrue(wait_for(lambda: "READ-ONLY" in err.getvalue()))
        self.assertIn("take-control", err.getvalue())
        # The displaced writer types into the void no longer: input stops.
        stdin.feed(b"b")
        time.sleep(0.3)
        self.assertEqual(len(ws.input_frames()), 1,
                         "a revoked writer must stop sending input")
        ws.end()
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_writer_control_non_active_flips_readonly(self):
        ws, stdin = FakeWS(), StdinScript()
        t, err = self.run_stream(ws, stdin)
        ws.feed(json.dumps({"type": "writer", "state": "active"}))
        self.assertTrue(wait_for(lambda: "writer active" in err.getvalue()))
        # A takeover broadcast ("held" while we believe we're writer) is the
        # same signal as a revoke: read-only flip, input halted.
        ws.feed(json.dumps({"type": "writer", "state": "held"}))
        self.assertTrue(wait_for(lambda: "READ-ONLY" in err.getvalue()))
        stdin.feed(b"x")
        time.sleep(0.3)
        self.assertEqual(ws.input_frames(), [])
        ws.end()
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_routine_control_frames_are_silent(self):
        ws, stdin = FakeWS(), StdinScript()
        t, err = self.run_stream(ws, stdin, role="viewer")
        ws.feed(json.dumps({"type": "writer", "state": "held"}))
        ws.feed(json.dumps({"type": "writer", "state": "held"}))   # unchanged
        ws.feed(json.dumps({"type": "lifecycle", "lifecycle": "running",
                            "composer": "idle"}))
        ws.feed(json.dumps({"type": "lifecycle", "lifecycle": "running",
                            "composer": "idle"}))                    # unchanged
        ws.feed(json.dumps({"type": "heartbeat"}))                   # hb ack
        ws.feed(json.dumps({"type": "input_ack", "seq": 1}))
        ws.feed(json.dumps({"type": "error", "code": "boom"}))
        ws.feed(json.dumps({"type": "error", "code": "terminated"}))
        t.join(10)
        self.assertFalse(t.is_alive(), "terminated ends the stream")
        text = err.getvalue()
        self.assertEqual(text.count("writer held"), 1)
        self.assertEqual(text.count("lifecycle running"), 1)
        self.assertEqual(text.count("error: boom"), 1)
        self.assertNotIn("heartbeat", text)
        self.assertNotIn("input_ack", text)
        self.assertNotIn("\x1b[2m", text, "no dimmed per-frame echo in raw "
                                          "mode — transitions/errors only")

    def _capture_stdout(self, buffer=None):
        """run_stream writes pane payloads to sys.stdout.buffer — hand it a
        buffer so the test can read them instead of the terminal."""
        fake = mock.Mock(buffer=io.BytesIO() if buffer is None else buffer)
        patch = mock.patch.object(ic.sys, "stdout", fake)
        patch.start()
        self.addCleanup(patch.stop)
        return fake.buffer

    def test_attach_names_the_review_gui_once_riding_the_first_payload(self):
        """Decision #52. `./sc enter` hands the terminal straight to the
        harness, so the session view is the only surface left that can name
        the GUI — and the GUI is on a different port per fork.

        It has to ride the first pane payload: attach opens with a
        full-screen redraw, and a line printed before that redraw is painted
        straight over by it. It also has to fire exactly once — this sits in
        the output hot path, and a line per pane write would make the
        session unusable.

        Both streams land in ONE transcript, because the guarantee is an
        ordering across them: the notice must follow the redraw bytes into
        the terminal, and a test that reads stdout and stderr separately
        cannot tell that apart from the defect."""
        ws, stdin = FakeWS(), StdinScript()
        tr = Transcript()
        self._capture_stdout(tr.buffer)
        t, _ = self.run_stream(ws, stdin, role="viewer", err=tr)

        # Control frames before any pane bytes: still no link, or the redraw
        # that follows would erase it.
        ws.feed(json.dumps({"type": "lifecycle", "lifecycle": "running",
                            "composer": "idle"}))
        self.assertTrue(wait_for(lambda: "lifecycle running" in tr.text()))
        self.assertNotIn("Review GUI", tr.text())

        ws.feed(b"\x04REDRAW")
        self.assertTrue(wait_for(lambda: "Review GUI" in tr.text()))
        self.assertIn(ic.API_BASE, tr.text())
        self.assertIn("./sc url", tr.text(),
                      "the line names the durable recall path, not just a URL")

        ws.feed(b"\x00OUTPUT")
        ws.feed(b"\x00MORE")
        self.assertTrue(wait_for(lambda: tr.pane() == b"REDRAWOUTPUTMORE"))

        # The anti-overdraw guarantee itself: in the terminal's own byte
        # order the attach redraw is already painted when the link appears.
        text = tr.text()
        self.assertLess(text.index("REDRAW"), text.index("Review GUI"),
                        "the notice must land AFTER the attach redraw — "
                        "before it, the redraw paints straight over it")
        self.assertEqual(text.count("Review GUI"), 1,
                         "once per attach, not once per pane write")
        ws.end()
        t.join(10)
        self.assertFalse(t.is_alive())


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


# ── spec #30 req 12 / #518: lazy websockets ─────────────────────────────────
# The real seam, captured at import time before any test patches it — the
# stream-refusal test puts it back so the verb reaches the actual import.
_REAL_RUN_STREAM = ic.run_stream

# A host python without the package: any websockets import raises ImportError.
WS_BLOCKED = {"websockets": None, "websockets.sync": None,
              "websockets.sync.client": None}


class LazyWebsocketsTest(InterfaceCliTest):
    """Host python without `websockets` (spec #30 req 12, issue #518): the
    stream dependency is checked lazily, inside the verbs that stream —
    HTTP-only verbs (status/stop/reconcile) run on a stdlib python, and a
    stream verb refuses with the exact dependency action instead of the old
    dispatch-time `no python with websockets` gate."""

    def test_http_verbs_pass_without_the_package(self):
        self.http.add("POST", "/api/interface/termination-requests",
                      {"terminated": True})
        self.http.add("POST", "/api/interface/reconciliations",
                      {"session_id": 9, "verified": True,
                       "occupancy": "unreconciled", "actions": []})
        with mock.patch.dict(sys.modules, WS_BLOCKED):
            rc, _, _ = self.run_cli(["status"])
            self.assertEqual(rc, 0)
            rc, _, _ = self.run_cli(["stop", "s2", "--json"])
            self.assertEqual(rc, 0)
            rc, _, _ = self.run_cli(["reconcile", "s3", "--json"])
            self.assertEqual(rc, 0)

    def test_stream_verb_refuses_with_the_dependency_action(self):
        with mock.patch.dict(sys.modules, WS_BLOCKED), \
                mock.patch.object(ic, "run_stream", _REAL_RUN_STREAM):
            rc, _, err = self.run_cli(["view", "s2"])
        self.assertEqual(rc, ic.EXIT_API_DOWN)
        self.assertIn("websockets", err)
        self.assertIn("./sc deps", err)
        self.assertIn("status/start/stop/reconcile", err)


if __name__ == "__main__":
    unittest.main()
