"""Regression coverage for broker readiness and the model-facing VM clients."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from typing import ClassVar
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
                 "unverified": False,
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
            "mcp_tunnel_listening": False,
            "mcp_tunnel_unverified": False,
        })
        run.assert_not_called()

    def test_status_reports_a_live_unverified_tunnel_without_mutation(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(vm, "_ssh_ready", return_value=(True, None)), \
             mock.patch.object(vm, "mcp_status", return_value={
                 "ok": True,
                 "running": False,
                 "pid": None,
                 "socket": str(vm.MCP_SOCKET),
                 "listening": True,
                 "unverified": True,
             }), \
             mock.patch.object(vm, "_run") as run:
            result = vm.do_status()
        self.assertEqual(result, {
            "ok": True,
            "domain": "win-test",
            "domain_state": "running",
            "ssh_ready": True,
            "ssh_error": None,
            "mcp_tunnel_running": False,
            "mcp_tunnel_listening": True,
            "mcp_tunnel_unverified": True,
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
            "mcp_tunnel_listening": False,
            "mcp_tunnel_unverified": False,
        }
        snapshot = {
            "tunnel": {
                "running": False,
                "listening": False,
                "unverified": False,
            },
            "relay": {
                "running": False,
                "listening": False,
                "unverified": False,
                "port": 18000,
            },
            "endpoint": {
                "url": "http://127.0.0.1:18000/mcp",
                "ready": False,
                "http_status": None,
                "error": "tunnel and relay are not both verified",
            },
            "adapter": {
                "harness": "codex",
                "state": "supported",
                "supported": True,
                "reason": None,
                "server_name": "windows-mcp",
            },
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call, \
             mock.patch.object(vm, "_mcp_snapshot", return_value=snapshot) as status:
            result = vm.run_operation("status")
        self.assertEqual(result, GOLDEN["status_success"])
        call.assert_called_once_with(
            "GET", "/status", None, timeout=vm.DEFAULT_CLIENT_TIMEOUT
        )
        status.assert_called_once_with({
            "running": False,
            "listening": False,
            "unverified": False,
        })

    def test_status_preserves_unverified_tunnel_state(self):
        response = {
            "ok": True,
            "domain": "win-test",
            "domain_state": "powered_off",
            "ssh_ready": True,
            "ssh_error": None,
            "mcp_tunnel_running": False,
            "mcp_tunnel_listening": True,
            "mcp_tunnel_unverified": True,
        }
        snapshot = {
            "tunnel": {
                "running": False,
                "listening": True,
                "unverified": True,
            },
            "relay": {
                "running": False,
                "listening": False,
                "unverified": False,
                "port": 18000,
            },
            "endpoint": {
                "url": "http://127.0.0.1:18000/mcp",
                "ready": False,
                "http_status": None,
                "error": "tunnel and relay are not both verified",
            },
            "adapter": {
                "harness": "codex",
                "state": "supported",
                "supported": True,
                "reason": None,
                "server_name": "windows-mcp",
            },
        }
        with mock.patch.object(vm, "broker_call", return_value=response), \
             mock.patch.object(vm, "_mcp_snapshot", return_value=snapshot):
            result = vm.run_operation("status")
        self.assertEqual(result["result"]["mcp"], {
            "tunnel_running": False,
            "tunnel_listening": True,
            "unverified": True,
        })
        self.assertEqual(result["error"], None)

    def test_status_human_output_names_an_unverified_tunnel(self):
        value = {
            "schema_version": 1,
            "ok": True,
            "operation": "status",
            "result": {
                "domain": {"name": "win-test", "state": "powered_off"},
                "ssh": {"ready": True, "last_error": None},
                "mcp": {
                    "tunnel_running": False,
                    "tunnel_listening": True,
                    "unverified": True,
                },
                "relay": {
                    "running": False,
                    "listening": False,
                    "unverified": False,
                    "port": 18000,
                },
                "endpoint": {
                    "url": "http://127.0.0.1:18000/mcp",
                    "ready": False,
                    "http_status": None,
                    "error": "tunnel and relay are not both verified",
                },
                "adapter": {
                    "harness": "codex",
                    "state": "supported",
                    "supported": True,
                    "reason": None,
                    "server_name": "windows-mcp",
                },
            },
            "error": None,
        }
        self.assertEqual(
            vm._human_result(value),
            "VM powered_off · SSH ready · broker ready · MCP tunnel unverified · "
            "relay stopped · endpoint unavailable · codex adapter supported",
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
                "mcp.tunnel_listening",
                "mcp.unverified",
                "relay.running",
                "relay.listening",
                "relay.unverified",
                "endpoint.ready",
                "endpoint.http_status",
                "adapter.state",
                "adapter.supported",
            ),
            "start": ("started", "ssh.attempts", "ssh.last_error"),
            "push": ("source", "destination"),
            "exec": ("--command-file", "exit_code", "stdout", "stderr"),
            "capture": (
                "--output",
                ".sc-state/local/vm-captures",
                "path",
                "bytes",
                "format",
                "mime_type",
            ),
            "reset": ("domain.state", "snapshot", "--off"),
        }
        for command, expected in cases.items():
            with self.subTest(command=command), \
                 mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output, \
                 self.assertRaises(SystemExit) as raised:
                vm.client_main([command, "--help"])
            self.assertEqual(raised.exception.code, 0)
            help_text = " ".join(output.getvalue().split())
            for field in expected:
                self.assertIn(field, help_text)

    def test_mcp_up_help_documents_total_endpoint_wait(self):
        with mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output, \
             self.assertRaises(SystemExit) as raised:
            vm.client_main(["mcp", "up", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("pace endpoint probes for up to 15s", output.getvalue())


class PublicMcpClientTests(unittest.TestCase):
    SUPPORTED: ClassVar[dict[str, object]] = {
        "harness": "codex",
        "state": "supported",
        "supported": True,
        "reason": None,
        "server_name": "windows-mcp",
    }

    def test_active_adapter_is_unknown_without_launched_harness_identity(self):
        with mock.patch.dict(vm.os.environ, {}, clear=True):
            result = vm.active_mcp_adapter()

        self.assertEqual(result, {
            "harness": None,
            "state": "unknown",
            "supported": False,
            "reason": "SC_HARNESS is not set",
            "server_name": None,
        })

    def test_endpoint_probe_performs_mcp_initialize_and_closes_session(self):
        initialize = mock.Mock(status=200)
        initialize.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Windows-MCP", "version": "1"},
            },
        }).encode()
        initialize.getheader.side_effect = lambda name: {
            "Content-Type": "application/json",
            "Mcp-Session-Id": "probe-session",
        }.get(name)
        closed = mock.Mock(status=204)
        closed.read.return_value = b""
        connections = [mock.Mock(), mock.Mock()]
        connections[0].getresponse.return_value = initialize
        connections[1].getresponse.return_value = closed

        with mock.patch.object(
            vm.http.client, "HTTPConnection", side_effect=connections
        ):
            result = vm.mcp_endpoint_status()

        self.assertEqual(result, {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": True,
            "http_status": 200,
            "error": None,
            "session_cleanup": {
                "attempted": True,
                "confirmed": True,
                "http_status": 204,
                "error": None,
            },
        })
        method, path = connections[0].request.call_args.args[:2]
        self.assertEqual((method, path), ("POST", "/mcp"))
        payload = json.loads(connections[0].request.call_args.kwargs["body"])
        self.assertEqual(payload["method"], "initialize")
        self.assertEqual(
            payload["params"]["protocolVersion"], vm.MCP_PROTOCOL_VERSION
        )
        connections[1].request.assert_called_once_with(
            "DELETE",
            "/mcp",
            headers={
                "Mcp-Session-Id": "probe-session",
                "MCP-Protocol-Version": "2025-06-18",
                "Connection": "close",
            },
        )
        for connection in connections:
            connection.close.assert_called_once_with()

    def test_endpoint_probe_keeps_initialize_ready_when_delete_resets(self):
        initialize = mock.Mock(status=200)
        initialize.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"protocolVersion": "2025-06-18"},
        }).encode()
        initialize.getheader.side_effect = lambda name: {
            "Content-Type": "application/json",
            "Mcp-Session-Id": "probe-session",
        }.get(name)
        connections = [mock.Mock(), mock.Mock()]
        connections[0].getresponse.return_value = initialize
        connections[1].request.side_effect = ConnectionResetError("peer closed")

        with mock.patch.object(
            vm.http.client, "HTTPConnection", side_effect=connections
        ):
            result = vm.mcp_endpoint_status()

        self.assertTrue(result["ready"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["session_cleanup"], {
            "attempted": True,
            "confirmed": False,
            "http_status": None,
            "error": "peer closed",
        })

    def test_endpoint_probe_accepts_sse_initialize_result(self):
        response = mock.Mock(status=200)
        response.read.return_value = (
            b"event: message\n"
            b'data: {"jsonrpc":"2.0","id":1,"result":'
            b'{"protocolVersion":"2025-06-18"}}\n\n'
        )
        response.getheader.side_effect = lambda name: (
            "text/event-stream; charset=utf-8" if name == "Content-Type" else None
        )
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch.object(
            vm.http.client, "HTTPConnection", return_value=connection
        ):
            result = vm.mcp_endpoint_status()

        self.assertTrue(result["ready"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["session_cleanup"], {
            "attempted": False,
            "confirmed": True,
            "http_status": None,
            "error": None,
        })

    def test_endpoint_probe_rejects_an_http_only_response(self):
        response = mock.Mock(status=200)
        response.read.return_value = b"not json-rpc"
        response.getheader.return_value = "text/plain"
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch.object(
            vm.http.client, "HTTPConnection", return_value=connection
        ):
            result = vm.mcp_endpoint_status()

        self.assertFalse(result["ready"])
        self.assertEqual(
            result["error"],
            "response did not contain an MCP initialization result",
        )

    def test_endpoint_wait_retries_with_pacing_until_ready(self):
        unavailable = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": False,
            "http_status": None,
            "error": "connection refused",
        }
        ready = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": True,
            "http_status": 200,
            "error": None,
        }
        clock = iter((0.0, 0.1, 0.2, 0.3, 0.4))
        sleeps = []
        with mock.patch.object(
            vm,
            "mcp_endpoint_status",
            side_effect=[unavailable, ready],
        ) as probe:
            result = vm.wait_for_mcp_endpoint(
                timeout=1.0,
                interval=0.25,
                clock=lambda: next(clock),
                sleep=sleeps.append,
            )

        self.assertEqual(result, {
            **ready,
            "attempts": 2,
            "timeout_seconds": 1.0,
        })
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(sleeps, [0.25])

    def test_endpoint_wait_returns_last_failure_at_total_timeout(self):
        unavailable = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": False,
            "http_status": None,
            "error": "connection refused",
        }
        clock = iter((0.0, 0.1, 0.2, 0.3, 1.0))
        sleeps = []
        with mock.patch.object(
            vm,
            "mcp_endpoint_status",
            return_value=unavailable,
        ) as probe:
            result = vm.wait_for_mcp_endpoint(
                timeout=1.0,
                interval=0.5,
                clock=lambda: next(clock),
                sleep=sleeps.append,
            )

        self.assertEqual(result, {
            **unavailable,
            "attempts": 2,
            "timeout_seconds": 1.0,
        })
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.5)

    def test_up_refuses_unsupported_adapter_without_transport_mutation(self):
        unsupported = {
            "harness": "kimi",
            "state": "unsupported",
            "supported": False,
            "reason": "no managed injection",
            "server_name": None,
        }
        with mock.patch.object(vm, "active_mcp_adapter", return_value=unsupported), \
             mock.patch.object(vm, "_mcp_broker_call") as broker, \
             mock.patch.object(vm, "_relay_module") as relay:
            result = vm.run_mcp_operation("up")

        self.assertEqual(result["error"], {
            "code": "mcp_adapter_unsupported",
            "message": "no managed injection",
            "details": {"harness": "kimi", "adapter_state": "unsupported"},
        })
        broker.assert_not_called()
        relay.assert_not_called()

    def test_up_verifies_tunnel_relay_and_endpoint(self):
        tunnel = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
        }
        relay_result = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
            "port": 18000,
        }
        endpoint = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": True,
            "http_status": 200,
            "error": None,
        }
        relay = mock.Mock()
        relay.up.return_value = relay_result
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(vm, "_mcp_broker_call", return_value=(tunnel, None)) as broker, \
             mock.patch.object(vm, "_relay_module", return_value=relay), \
             mock.patch.object(vm, "wait_for_mcp_endpoint", return_value=endpoint):
            result = vm.run_mcp_operation("up")

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], {
            "adapter": self.SUPPORTED,
            "tunnel": {
                "running": True,
                "listening": True,
                "unverified": False,
            },
            "relay": {
                "running": True,
                "listening": True,
                "unverified": False,
                "port": 18000,
            },
            "endpoint": endpoint,
        })
        broker.assert_called_once_with("POST", "/mcp/up")
        relay.up.assert_called_once_with(18000)
        relay.down.assert_not_called()

    def test_failed_endpoint_probe_records_relay_and_tunnel_cleanup(self):
        tunnel_up = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
        }
        tunnel_down = {"ok": True, "output": "tunnel stopped"}
        relay = mock.Mock()
        relay.up.return_value = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
            "port": 18000,
        }
        relay.down.return_value = {"ok": True, "output": "relay stopped"}
        endpoint = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": False,
            "http_status": 404,
            "error": "unexpected HTTP status 404",
        }
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(
                 vm,
                 "_mcp_broker_call",
                 side_effect=[(tunnel_up, None), (tunnel_down, None)],
             ) as broker, \
             mock.patch.object(vm, "_relay_module", return_value=relay), \
             mock.patch.object(vm, "wait_for_mcp_endpoint", return_value=endpoint):
            result = vm.run_mcp_operation("up")

        self.assertEqual(result["error"]["code"], "mcp_endpoint_unavailable")
        self.assertEqual(result["error"]["details"], {
            "endpoint": endpoint,
            "relay_cleanup": {"ok": True, "output": "relay stopped"},
            "tunnel_cleanup": {
                "ok": True,
                "uncertain": False,
                "result": {"ok": True, "output": "tunnel stopped"},
                "error": None,
            },
        })
        self.assertEqual(broker.call_args_list, [
            mock.call("POST", "/mcp/up"),
            mock.call("POST", "/mcp/down"),
        ])
        relay.down.assert_called_once_with(18000)

    def test_endpoint_failure_preserves_uncertain_tunnel_rollback(self):
        tunnel_up = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
        }
        cleanup_error = vm.operation_error(
            "mcp", "mcp_timeout", "the MCP broker operation timed out"
        )
        relay = mock.Mock()
        relay.up.return_value = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
            "port": 18000,
        }
        relay.down.return_value = {"ok": True, "output": "relay stopped"}
        endpoint = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": False,
            "http_status": None,
            "error": "connection refused",
        }
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(
                 vm,
                 "_mcp_broker_call",
                 side_effect=[(tunnel_up, None), (None, cleanup_error)],
             ), \
             mock.patch.object(vm, "_relay_module", return_value=relay), \
             mock.patch.object(vm, "wait_for_mcp_endpoint", return_value=endpoint):
            result = vm.run_mcp_operation("up")

        self.assertEqual(
            result["error"]["details"]["tunnel_cleanup"],
            {
                "ok": False,
                "uncertain": True,
                "result": None,
                "error": {
                    "code": "mcp_timeout",
                    "message": "the MCP broker operation timed out",
                    "details": {},
                },
            },
        )

    def test_relay_failure_preserves_uncertain_tunnel_rollback(self):
        tunnel_up = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
        }
        cleanup_error = vm.operation_error(
            "mcp", "broker_unreachable", "the VM broker is not reachable"
        )
        relay = mock.Mock()
        relay.up.return_value = {"ok": False, "output": "relay bind failed"}
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(
                 vm,
                 "_mcp_broker_call",
                 side_effect=[(tunnel_up, None), (None, cleanup_error)],
             ), \
             mock.patch.object(vm, "_relay_module", return_value=relay):
            result = vm.run_mcp_operation("up")

        self.assertEqual(result["error"]["code"], "mcp_relay_failed")
        self.assertEqual(
            result["error"]["details"]["tunnel_cleanup"]["error"]["code"],
            "broker_unreachable",
        )
        self.assertTrue(
            result["error"]["details"]["tunnel_cleanup"]["uncertain"]
        )

    def test_status_probes_endpoint_only_for_verified_tunnel_and_relay(self):
        tunnel = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
        }
        relay = mock.Mock()
        relay.status.return_value = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
            "port": 18000,
        }
        endpoint = {
            "url": "http://127.0.0.1:18000/mcp",
            "ready": True,
            "http_status": 200,
            "error": None,
        }
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(vm, "_mcp_broker_call", return_value=(tunnel, None)), \
             mock.patch.object(vm, "_relay_module", return_value=relay), \
             mock.patch.object(
                 vm, "mcp_endpoint_status", return_value=endpoint
             ) as probe:
            result = vm.run_mcp_operation("status")

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["endpoint"], endpoint)
        probe.assert_called_once_with(18000)

    def test_status_does_not_probe_an_unverified_tunnel(self):
        tunnel = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": True,
        }
        relay = mock.Mock()
        relay.status.return_value = {
            "ok": True,
            "running": True,
            "listening": True,
            "unverified": False,
            "port": 18000,
        }
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(vm, "_mcp_broker_call", return_value=(tunnel, None)), \
             mock.patch.object(vm, "_relay_module", return_value=relay), \
             mock.patch.object(vm, "mcp_endpoint_status") as probe:
            result = vm.run_mcp_operation("status")

        self.assertFalse(result["result"]["endpoint"]["ready"])
        self.assertEqual(
            result["result"]["endpoint"]["error"],
            "tunnel and relay are not both verified",
        )
        probe.assert_not_called()

    def test_down_reports_both_cleanup_results(self):
        relay = mock.Mock()
        relay.down.return_value = {"ok": True, "output": "relay stopped"}
        tunnel = {"ok": True, "output": "tunnel stopped"}
        with mock.patch.object(vm, "active_mcp_adapter", return_value=self.SUPPORTED), \
             mock.patch.object(vm, "_mcp_broker_call", return_value=(tunnel, None)) as broker, \
             mock.patch.object(vm, "_relay_module", return_value=relay):
            result = vm.run_mcp_operation("down")

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["relay_cleanup"], relay.down.return_value)
        self.assertEqual(result["result"]["tunnel_cleanup"], tunnel)
        relay.down.assert_called_once_with(18000)
        broker.assert_called_once_with("POST", "/mcp/down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
