"""Regression coverage for broker readiness and the model-facing VM clients."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import vm
import vm_broker

SAVED = {
    "domain": "win-test",
    "ssh_host": "127.0.0.1",
    "ssh_port": 22,
    "ssh_user": "tester",
    "ssh_key_path": "~/.ssh/sc_win_test",
    "transfer_dir": "/tmp",
    "snapshot": "clean",
}
GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "vm_client_results.json").read_text()
)


class BrokerReadinessTests(unittest.TestCase):
    def test_domain_state_uses_stdout_and_ignores_benign_stderr(self):
        process = mock.Mock(
            returncode=0,
            stdout="shut off\n",
            stderr="libvirt: error : Failed to find user record for uid\n",
        )
        with mock.patch.object(vm.subprocess, "run", return_value=process) as run:
            result = vm._domain_state(SAVED)
        self.assertEqual(result, (True, "powered_off"))
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_domain_state_normalizes_unrecognized_stdout_to_unknown(self):
        process = mock.Mock(returncode=0, stdout="future state\n", stderr="")
        with mock.patch.object(vm.subprocess, "run", return_value=process):
            result = vm._domain_state(SAVED)
        self.assertEqual(result, (True, "unknown"))

    def test_status_observes_domain_ssh_and_tunnel_without_mutation(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(vm, "_ssh_ready", return_value=(False, "not ready")), \
             mock.patch.object(vm, "mcp_status", return_value={
                 "ok": True, "running": False, "pid": None, "socket": None,
             }), \
             mock.patch.object(vm, "_run") as run:
            result = vm.do_status()
        self.assertEqual(result, {
            "ok": True,
            "domain": "win-test",
            "domain_state": "running",
            "ssh_ready": False,
            "ssh_error": "not ready",
            "mcp_tunnel_running": False,
        })
        run.assert_not_called()

    def test_start_keeps_running_domain_and_waits_for_ssh(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(vm, "_wait_for_ssh", return_value=(True, 3, None)), \
             mock.patch.object(vm, "_run") as run:
            result = vm.do_start(wait=30)
        self.assertEqual(result, {
            "ok": True,
            "output": "SSH ready after 3 attempt(s)",
            "domain": "win-test",
            "domain_state": "running",
            "started": False,
            "attempts": 3,
            "last_readiness_error": None,
        })
        run.assert_not_called()

    def test_start_powers_on_off_domain_once_then_waits(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "powered_off")), \
             mock.patch.object(vm, "_run", return_value=(True, "started")) as run, \
             mock.patch.object(vm, "_wait_for_ssh", return_value=(True, 2, None)):
            result = vm.do_start(wait=30)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["started"], True)
        self.assertEqual(result["attempts"], 2)
        run.assert_called_once_with(["virsh", "start", "win-test"], timeout=30)

    def test_start_timeout_preserves_attempts_and_last_error(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(
                 vm, "_wait_for_ssh", return_value=(False, 4, "No route to host")
             ):
            result = vm.do_start(wait=8)
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(result["last_readiness_error"], "No route to host")
        self.assertEqual(result["domain_state"], "running")

    def test_powered_off_reset_confirms_observed_final_state(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "reverted")) as run, \
             mock.patch.object(vm, "_domain_state", return_value=(True, "powered_off")):
            result = vm.do_reset(running=False)
        self.assertEqual(result, {
            "ok": True,
            "output": "reverted",
            "domain": "win-test",
            "snapshot": "clean",
            "domain_state": "powered_off",
            "reset_outcome": "confirmed",
        })
        run.assert_called_once_with(
            ["virsh", "snapshot-revert", "win-test", "--snapshotname", "clean"],
            timeout=vm.RESET_COMMAND_TIMEOUT,
        )

    def test_powered_off_reset_fails_when_final_state_is_not_confirmed(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "reverted")), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")):
            result = vm.do_reset(running=False)
        self.assertEqual(result, {
            "ok": False,
            "output": (
                "snapshot reset did not reach the expected domain state; "
                "observed running, expected powered_off"
            ),
            "domain": "win-test",
            "snapshot": "clean",
            "domain_state": "running",
            "reset_outcome": "state_mismatch",
        })

    def test_reset_timeout_is_unknown_even_when_final_state_matches(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(
                 vm, "_run", return_value=(False, "timed out (>60s)")
             ), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "powered_off")):
            result = vm.do_reset(running=False)
        self.assertEqual(result, {
            "ok": False,
            "output": (
                "the snapshot reset could not be confirmed before the 60s "
                "command timeout"
            ),
            "domain": "win-test",
            "snapshot": "clean",
            "domain_state": "powered_off",
            "reset_outcome": "unknown",
        })

    def test_reset_with_unobservable_final_state_is_unknown(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "reverted")), \
             mock.patch.object(
                 vm, "_domain_state", return_value=(False, "timed out (>15s)")
             ):
            result = vm.do_reset(running=False)
        self.assertEqual(result, {
            "ok": False,
            "output": (
                "the snapshot reset result could not be confirmed because "
                "the final domain state could not be observed"
            ),
            "domain": "win-test",
            "snapshot": "clean",
            "domain_state": "unknown",
            "reset_outcome": "unknown",
        })

    def test_reset_rejection_remains_a_confirmed_failure(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(False, "snapshot not found")), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "powered_off")):
            result = vm.do_reset(running=False)
        self.assertEqual(result, {
            "ok": False,
            "output": "snapshot not found",
            "domain": "win-test",
            "snapshot": "clean",
            "domain_state": "powered_off",
            "reset_outcome": "rejected",
        })


class SingleResponseTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(vm_broker.Handler)
        handler._response_started = False
        handler._request_id = "request123"
        handler._request_started = 0.0
        handler.command = "POST"
        handler.path = "/reset"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        return handler

    def test_disconnected_client_gets_one_response_attempt_and_bounded_log(self):
        handler = self._handler()
        handler.wfile.write.side_effect = BrokenPipeError
        with mock.patch.object(vm_broker.time, "monotonic", return_value=0.1), \
             mock.patch.object(vm_broker.sys, "stderr", new_callable=io.StringIO) as log:
            handler._send(200, {"ok": True, "secret": "must-not-be-logged"})
            handler._send(500, {"ok": False, "error": "second write"})
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.write.call_count, 1)
        self.assertIn("event=response_lost", log.getvalue())
        self.assertIn("event=response_suppressed", log.getvalue())
        self.assertNotIn("must-not-be-logged", log.getvalue())
        self.assertNotIn("second write", log.getvalue())

    def test_handler_exception_returns_sanitized_error_without_payload_log(self):
        handler = self._handler()
        handler.path = "/exec"
        handler._body = mock.Mock(side_effect=ValueError("guest command SECRET"))
        handler._send = mock.Mock()
        with mock.patch.object(vm_broker.sys, "stderr", new_callable=io.StringIO) as log:
            handler.do_POST()
        handler._send.assert_called_once_with(
            500, {"ok": False, "error": "broker request failed"}
        )
        self.assertIn("error=ValueError", log.getvalue())
        self.assertNotIn("guest command SECRET", log.getvalue())

    def test_get_exception_returns_sanitized_error_without_payload_log(self):
        handler = self._handler()
        handler.command = "GET"
        handler.path = "/status"
        handler._send = mock.Mock()
        with mock.patch.object(vm, "do_status", side_effect=ValueError("SECRET")), \
             mock.patch.object(vm_broker.sys, "stderr", new_callable=io.StringIO) as log:
            handler.do_GET()
        handler._send.assert_called_once_with(
            500, {"ok": False, "error": "broker request failed"}
        )
        self.assertIn("error=ValueError", log.getvalue())
        self.assertNotIn("SECRET", log.getvalue())

    def test_put_exception_returns_sanitized_error_without_payload_log(self):
        handler = self._handler()
        handler.command = "PUT"
        handler.path = "/vm"
        handler._body = mock.Mock(side_effect=ValueError("SECRET"))
        handler._send = mock.Mock()
        with mock.patch.object(vm_broker.sys, "stderr", new_callable=io.StringIO) as log:
            handler.do_PUT()
        handler._send.assert_called_once_with(
            500, {"ok": False, "error": "broker request failed"}
        )
        self.assertIn("error=ValueError", log.getvalue())
        self.assertNotIn("SECRET", log.getvalue())

    def test_malformed_reset_body_cannot_trigger_the_default_running_reset(self):
        handler = self._handler()
        handler.headers = {"Content-Length": "1"}
        handler.rfile = io.BytesIO(b"{")
        handler._send = mock.Mock()
        with mock.patch.object(vm, "do_reset") as reset, \
             mock.patch.object(vm_broker.sys, "stderr", new_callable=io.StringIO):
            handler.do_POST()
        reset.assert_not_called()
        handler._send.assert_called_once_with(
            500, {"ok": False, "error": "broker request failed"}
        )

    def test_guest_mutating_routes_hold_the_shared_lock(self):
        cases = (
            ("/exec", "do_exec", {"command": "echo ok"}),
            ("/start", "do_start", {}),
            ("/reset", "do_reset", {"running": False}),
            ("/push", "do_push", {"src": "artifact", "dest": "staged"}),
            ("/capture", "do_capture", {"command": None}),
        )
        for path, verb_name, body in cases:
            with self.subTest(path=path):
                events = []
                lock = mock.MagicMock()
                lock.acquire.side_effect = lambda **kwargs: (
                    events.append(("lock_acquire", kwargs)) or True
                )
                lock.release.side_effect = lambda: events.append(("lock_release", {}))
                handler = self._handler()
                handler.path = path
                handler.server = mock.Mock(vm_mutation_lock=lock)
                handler._body = mock.Mock(return_value=body)
                handler._send = mock.Mock()
                with mock.patch.object(
                    vm,
                    verb_name,
                    side_effect=lambda *args, **kwargs: (
                        events.append("verb") or {"ok": True}
                    ),
                ):
                    handler.do_POST()
                self.assertEqual(events, [
                    ("lock_acquire", {"timeout": 5}),
                    "verb",
                    ("lock_release", {}),
                ])
                handler._send.assert_called_once_with(200, {"ok": True})

    def test_busy_reset_returns_before_the_reset_is_attempted(self):
        handler = self._handler()
        handler.path = "/reset"
        handler.server = mock.Mock()
        handler.server.vm_mutation_lock.acquire.return_value = False
        handler._body = mock.Mock(return_value={"running": False})
        handler._send = mock.Mock()
        with mock.patch.object(vm, "do_reset") as reset:
            handler.do_POST()
        reset.assert_not_called()
        handler.server.vm_mutation_lock.acquire.assert_called_once_with(timeout=5)
        handler.server.vm_mutation_lock.release.assert_not_called()
        handler._send.assert_called_once_with(409, {
            "ok": False,
            "error": "vm_busy",
            "output": "another VM mutation is still running",
            "wait_seconds": 5,
        })


class PublicClientTests(unittest.TestCase):
    def test_status_json_matches_golden_shape(self):
        response = {
            "ok": True,
            "domain": "win-test",
            "domain_state": "running",
            "ssh_ready": True,
            "ssh_error": None,
            "mcp_tunnel_running": False,
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call:
            result = vm.run_operation("status")
        self.assertEqual(result, GOLDEN["status_success"])
        call.assert_called_once_with(
            "GET", "/status", None, timeout=vm.DEFAULT_CLIENT_TIMEOUT
        )

    def test_start_json_matches_golden_shape_and_uses_longer_budget(self):
        response = {
            "ok": True,
            "domain": "win-test",
            "domain_state": "running",
            "started": False,
            "attempts": 1,
            "last_readiness_error": None,
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call:
            result = vm.run_operation("start")
        self.assertEqual(result, GOLDEN["start_success"])
        call.assert_called_once_with(
            "POST", "/start", None, timeout=155
        )
        self.assertEqual(vm.START_BROKER_BUDGET, 140)
        self.assertEqual(vm.START_CLIENT_TIMEOUT, 155)

    def test_reset_json_matches_golden_shape_and_exceeds_broker_budget(self):
        response = {
            "ok": True,
            "domain": "win-test",
            "domain_state": "powered_off",
            "snapshot": "clean",
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call:
            result = vm.run_operation("reset")
        self.assertEqual(result, GOLDEN["reset_success"])
        call.assert_called_once_with(
            "POST", "/reset", {"running": False}, timeout=130
        )
        self.assertEqual(vm.RESET_CLIENT_TIMEOUT, 130)
        self.assertEqual(vm.RESET_BROKER_BUDGET, 80)
        self.assertEqual(vm.RESET_COMMAND_TIMEOUT, 60)

    def test_busy_reset_is_distinct_and_known_not_to_have_run(self):
        response = {
            "ok": False,
            "error": "vm_busy",
            "output": "another VM mutation is still running",
            "wait_seconds": 5,
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call:
            result = vm.run_operation("reset")
        self.assertEqual(result, {
            "schema_version": 1,
            "ok": False,
            "operation": "reset",
            "result": None,
            "error": {
                "code": "vm_busy",
                "message": "another VM mutation is still running",
                "details": {"wait_seconds": 5},
            },
        })
        self.assertEqual(call.call_count, 1)

    def test_broker_reset_timeout_is_unknown_and_never_retried(self):
        response = {
            "ok": False,
            "output": (
                "the snapshot reset could not be confirmed before the 60s "
                "command timeout"
            ),
            "domain_state": "powered_off",
            "reset_outcome": "unknown",
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call:
            result = vm.run_operation("reset")
        self.assertEqual(result, {
            "schema_version": 1,
            "ok": False,
            "operation": "reset",
            "result": None,
            "error": {
                "code": "reset_result_unknown",
                "message": response["output"],
                "details": {"domain_state": "powered_off"},
            },
        })
        self.assertEqual(call.call_count, 1)

    def test_rejected_reset_remains_reset_failed(self):
        response = {
            "ok": False,
            "output": "snapshot not found",
            "domain_state": "powered_off",
            "reset_outcome": "rejected",
        }
        with mock.patch.object(vm, "broker_call", return_value=response):
            result = vm.run_operation("reset")
        self.assertEqual(result["error"], {
            "code": "reset_failed",
            "message": "snapshot not found",
            "details": {"domain_state": "powered_off"},
        })

    def test_reset_malformed_response_is_unknown_and_never_retried(self):
        with mock.patch.object(
            vm, "broker_call", side_effect=vm.BrokerResponseError("empty")
        ) as call:
            result = vm.run_operation("reset")
        self.assertEqual(result, GOLDEN["reset_unknown"])
        self.assertEqual(call.call_count, 1)

    def test_reset_transport_timeout_is_unknown_and_never_retried(self):
        with mock.patch.object(
            vm,
            "broker_call",
            side_effect=vm.BrokerTimeoutError("slow", request_sent=True),
        ) as call:
            result = vm.run_operation("reset")
        self.assertEqual(result, GOLDEN["reset_unknown"])
        self.assertEqual(call.call_count, 1)

    def test_start_transport_timeout_has_distinct_code_and_deadline(self):
        with mock.patch.object(
            vm,
            "broker_call",
            side_effect=vm.BrokerTimeoutError("slow", request_sent=True),
        ):
            result = vm.run_operation("start")
        self.assertEqual(result, {
            "schema_version": 1,
            "ok": False,
            "operation": "start",
            "result": None,
            "error": {
                "code": "start_timeout",
                "message": "the VM start operation timed out after 155s",
                "details": {"timeout_seconds": 155},
            },
        })

    def test_reset_success_missing_required_fields_is_unknown(self):
        with mock.patch.object(vm, "broker_call", return_value={"ok": True}):
            result = vm.run_operation("reset")
        self.assertEqual(result, GOLDEN["reset_unknown"])

    def test_reset_connection_failure_before_send_is_not_uncertain(self):
        with mock.patch.object(
            vm,
            "broker_call",
            side_effect=vm.BrokerConnectionError("absent", request_sent=False),
        ):
            result = vm.run_operation("reset")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "broker_unreachable")
        self.assertEqual(result["error"]["details"], {})

    def test_reset_timeout_before_send_is_not_uncertain(self):
        with mock.patch.object(
            vm,
            "broker_call",
            side_effect=vm.BrokerTimeoutError("slow", request_sent=False),
        ):
            result = vm.run_operation("reset")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "broker_unreachable")
        self.assertEqual(result["error"]["details"], {})

    def test_start_timeout_preserves_bounded_readiness_evidence(self):
        response = {
            "ok": False,
            "output": "SSH was not ready within 90s",
            "domain_state": "running",
            "attempts": 7,
            "last_readiness_error": "No route to host",
        }
        with mock.patch.object(vm, "broker_call", return_value=response):
            result = vm.run_operation("start")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "start_failed")
        self.assertEqual(result["error"]["details"], {
            "domain_state": "running",
            "attempts": 7,
            "last_readiness_error": "No route to host",
        })

    def test_json_cli_prints_one_object_and_failure_is_nonzero(self):
        with mock.patch.object(vm, "run_operation", return_value=GOLDEN["reset_unknown"]), \
             mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout:
            code = vm.client_main(["reset", "--off", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), GOLDEN["reset_unknown"])
        self.assertEqual(stdout.getvalue().count("\n"), 1)

    def test_reset_requires_explicit_off_flag_before_any_broker_call(self):
        with mock.patch.object(vm, "run_operation") as run, \
             mock.patch.object(sys, "stderr", new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as raised:
            vm.client_main(["reset", "--json"])
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_incomplete_content_length_is_rejected(self):
        transport = mock.Mock()
        transport.recv.side_effect = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}",
            b"",
        ]
        with mock.patch.object(vm.socket, "socket", return_value=transport), \
             self.assertRaisesRegex(vm.BrokerResponseError, "incomplete"):
            vm.broker_call("POST", "/reset", {"running": False})
        transport.connect.assert_called_once_with(str(vm.SOCKET))
        self.assertEqual(transport.sendall.call_count, 1)
        transport.close.assert_called_once_with()

    def test_broker_call_timeout_occurs_after_one_request_send(self):
        transport = mock.Mock()
        transport.recv.side_effect = TimeoutError
        with mock.patch.object(vm.socket, "socket", return_value=transport), \
             self.assertRaisesRegex(
                 vm.BrokerTimeoutError, "timed out after 130s"
             ) as raised:
            vm.broker_call("POST", "/reset", {"running": False})
        self.assertIsInstance(raised.exception, ConnectionError)
        self.assertEqual(raised.exception.request_sent, True)
        transport.connect.assert_called_once_with(str(vm.SOCKET))
        self.assertEqual(transport.sendall.call_count, 1)
        transport.close.assert_called_once_with()

    def test_all_broker_transport_failures_share_connection_error_hierarchy(self):
        self.assertTrue(issubclass(vm.BrokerConnectionError, ConnectionError))
        self.assertTrue(issubclass(vm.BrokerTimeoutError, ConnectionError))
        self.assertTrue(issubclass(vm.BrokerResponseError, ConnectionError))

    def test_subcommand_help_documents_json_result_fields(self):
        cases = {
            "status": (
                "broker.ready",
                "ssh.last_error",
                "mcp.tunnel_running",
                "relay.state",
                "endpoint.state",
                "adapter.state",
                "work unit 7",
                "work unit 10",
            ),
            "start": ("started", "ssh.attempts", "ssh.last_error"),
            "reset": ("domain.state", "snapshot", "--off"),
        }
        for command, expected in cases.items():
            with self.subTest(command=command), \
                 mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output, \
                 self.assertRaises(SystemExit) as raised:
                vm.client_main([command, "--help"])
            self.assertEqual(raised.exception.code, 0)
            for field in expected:
                self.assertIn(field, output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
