"""Regression coverage for model-facing VM exec, push, and capture clients."""

from __future__ import annotations

import base64
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import vm


class ExecClientTests(unittest.TestCase):
    def test_argument_command_preserves_complex_powershell_and_unicode(self):
        command = (
            '$name = "München 東京";\n'
            "$path = 'C:\\Program Files\\Demo App\\data.txt';\n"
            'Get-Content $path | ForEach-Object { `"$name :: $_`" }'
        )
        response = {
            "ok": True,
            "exit": 0,
            "stdout": "München 東京 :: ready\n",
            "stderr": "",
        }
        with (
            mock.patch.object(vm, "broker_call", return_value=response) as call,
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
        ):
            code = vm.client_main(["exec", "--json", "--", command])
        self.assertEqual(code, 0)
        call.assert_called_once_with(
            "POST", "/exec", {"command": command}, timeout=vm.EXEC_CLIENT_TIMEOUT
        )
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "schema_version": 1,
                "ok": True,
                "operation": "exec",
                "result": {
                    "exit_code": 0,
                    "stdout": "München 東京 :: ready\n",
                    "stderr": "",
                },
                "error": None,
            },
        )

    def test_broker_wire_payload_round_trips_complex_command_as_utf8_json(self):
        command = (
            '$name = "München 東京";\n'
            "$path = 'C:\\Program Files\\Demo App\\data.txt';\n"
            'Get-Content $path | ForEach-Object { `"$name :: $_`" }'
        )
        response_body = b'{"ok":true}'
        transport = mock.Mock()
        transport.recv.side_effect = [
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(response_body)).encode()
            + b"\r\n\r\n"
            + response_body,
            b"",
        ]
        with mock.patch.object(vm.socket, "socket", return_value=transport):
            response = vm.broker_call("POST", "/exec", {"command": command})
        sent = transport.sendall.call_args.args[0]
        raw_headers, separator, raw_body = sent.partition(b"\r\n\r\n")
        self.assertEqual(separator, b"\r\n\r\n")
        self.assertIn(f"Content-Length: {len(raw_body)}".encode(), raw_headers)
        self.assertEqual(json.loads(raw_body.decode()), {"command": command})
        self.assertEqual(response, {"ok": True})
        transport.connect.assert_called_once_with(str(vm.SOCKET))
        transport.close.assert_called_once_with()

    def test_utf8_command_file_preserves_multiline_content_and_trailing_newline(self):
        command = "$value = 'naïve';\nWrite-Output `\"$value 東京`\"\n"
        with tempfile.TemporaryDirectory() as tmp:
            command_file = Path(tmp) / "command.ps1"
            command_file.write_text(command, encoding="utf-8")
            response = {"ok": True, "exit": 0, "stdout": "ok\n", "stderr": ""}
            with (
                mock.patch.object(vm, "broker_call", return_value=response) as call,
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
            ):
                code = vm.client_main(
                    [
                        "exec",
                        "--command-file",
                        str(command_file),
                        "--json",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["result"],
            {
                "exit_code": 0,
                "stdout": "ok\n",
                "stderr": "",
            },
        )
        call.assert_called_once_with(
            "POST", "/exec", {"command": command}, timeout=vm.EXEC_CLIENT_TIMEOUT
        )

    def test_cli_rejects_command_file_plus_arguments_before_broker_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            command_file = Path(tmp) / "command.ps1"
            command_file.write_text("Write-Output file", encoding="utf-8")
            with (
                mock.patch.object(vm, "broker_call") as call,
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
            ):
                code = vm.client_main(
                    [
                        "exec",
                        "--command-file",
                        str(command_file),
                        "--json",
                        "--",
                        "Write-Output argv",
                    ]
                )
        self.assertEqual(code, 1)
        call.assert_not_called()
        self.assertEqual(
            json.loads(stdout.getvalue())["error"],
            {
                "code": "exec_arguments_invalid",
                "message": "use either arguments after -- or --command-file, not both",
                "details": {},
            },
        )

    def test_cli_rejects_non_utf8_command_file_before_broker_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            command_file = Path(tmp) / "command.ps1"
            command_file.write_bytes(b"Write-Output \xff")
            with (
                mock.patch.object(vm, "broker_call") as call,
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
            ):
                code = vm.client_main(
                    [
                        "exec",
                        "--command-file",
                        str(command_file),
                        "--json",
                    ]
                )
        self.assertEqual(code, 1)
        call.assert_not_called()
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"], "exec_command_file_invalid"
        )

    def test_failed_guest_command_does_not_copy_output_into_error_details(self):
        response = {
            "ok": False,
            "exit": 7,
            "stdout": "SECRET FILE CONTENT",
            "stderr": "SECRET STACK TRACE",
        }
        with mock.patch.object(vm, "broker_call", return_value=response):
            result = vm.run_operation("exec", command="exit 7")
        self.assertEqual(
            result["error"],
            {
                "code": "exec_failed",
                "message": "the guest command failed",
                "details": {
                    "exit_code": 7,
                    "stdout_bytes": len("SECRET FILE CONTENT"),
                    "stderr_bytes": len("SECRET STACK TRACE"),
                },
            },
        )
        self.assertNotIn("SECRET", json.dumps(result))


class PushClientTests(unittest.TestCase):
    def test_push_passes_unicode_paths_and_returns_structured_locations(self):
        response = {
            "ok": True,
            "output": "staged",
            "source": "/repo/artifacts/Δ build.zip",
            "destination": "/share/Builds/Δ build.zip",
        }
        with (
            mock.patch.object(vm, "broker_call", return_value=response) as call,
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
        ):
            code = vm.client_main(
                [
                    "push",
                    "artifacts/Δ build.zip",
                    "Builds/Δ build.zip",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        call.assert_called_once_with(
            "POST",
            "/push",
            {"src": "artifacts/Δ build.zip", "dest": "Builds/Δ build.zip"},
            timeout=vm.DEFAULT_CLIENT_TIMEOUT,
        )
        self.assertEqual(
            json.loads(stdout.getvalue())["result"],
            {
                "source": "/repo/artifacts/Δ build.zip",
                "destination": "/share/Builds/Δ build.zip",
            },
        )


class CaptureClientTests(unittest.TestCase):
    IMAGE = b"P6\n1 1\n255\n\x00\x7f\xff"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".sc-state" / "local"
        patcher = mock.patch.object(vm, "LOCAL_ARTIFACT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self) -> dict:
        return {
            "ok": True,
            "screenshot_b64": base64.b64encode(self.IMAGE).decode(),
            "screenshot_bytes": len(self.IMAGE),
            "screenshot_format": "ppm",
        }

    def test_default_capture_is_atomic_mode_0600_and_returns_view_metadata(self):
        with (
            mock.patch.object(vm.time, "time_ns", return_value=123456),
            mock.patch.object(vm, "broker_call", return_value=self.response()) as call,
        ):
            result = vm.run_operation("capture")
        target = self.root / "vm-captures" / "capture-123456.ppm"
        self.assertEqual(
            result,
            {
                "schema_version": 1,
                "ok": True,
                "operation": "capture",
                "result": {
                    "path": str(target),
                    "bytes": len(self.IMAGE),
                    "format": "ppm",
                    "mime_type": "image/x-portable-pixmap",
                },
                "error": None,
            },
        )
        self.assertEqual(target.read_bytes(), self.IMAGE)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])
        call.assert_called_once_with(
            "POST", "/capture", {}, timeout=vm.CAPTURE_CLIENT_TIMEOUT
        )

    def test_explicit_output_outside_local_artifacts_is_rejected_without_capture(self):
        outside = Path(self.tmp.name) / "outside.ppm"
        with mock.patch.object(vm, "broker_call") as call:
            result = vm.run_operation("capture", output=str(outside))
        self.assertEqual(
            result["error"],
            {
                "code": "capture_output_not_allowed",
                "message": "the capture output must stay inside the local artifact area",
                "details": {"allowed_root": str(self.root.resolve())},
            },
        )
        call.assert_not_called()
        self.assertFalse(outside.exists())

    def test_explicit_output_inside_local_artifacts_is_written_exactly_there(self):
        target = self.root / "review" / "frame.ppm"
        with mock.patch.object(vm, "broker_call", return_value=self.response()):
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(result["result"]["path"], str(target))
        self.assertEqual(target.read_bytes(), self.IMAGE)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_symlinked_output_outside_local_artifacts_is_rejected_without_capture(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.root.mkdir(parents=True)
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        target = self.root / "escape" / "frame.ppm"
        with mock.patch.object(vm, "broker_call") as call:
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(result["error"]["code"], "capture_output_not_allowed")
        call.assert_not_called()
        self.assertFalse((outside / "frame.ppm").exists())

    def test_invalid_base64_creates_neither_target_nor_partial_file(self):
        target = self.root / "vm-captures" / "invalid.ppm"
        response = self.response()
        response["screenshot_b64"] = "not base64!"
        with mock.patch.object(vm, "broker_call", return_value=response):
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(result["error"]["code"], "capture_response_invalid")
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_encoded_payload_over_limit_is_rejected_even_when_metadata_lies(self):
        target = self.root / "vm-captures" / "encoded-too-large.ppm"
        response = self.response()
        response["screenshot_b64"] = base64.b64encode(b"0123456789").decode()
        response["screenshot_bytes"] = 1
        with (
            mock.patch.object(vm, "MAX_CAPTURE_BYTES", 8),
            mock.patch.object(vm, "broker_call", return_value=response),
        ):
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(
            result["error"],
            {
                "code": "capture_too_large",
                "message": "the capture exceeds the 8-byte limit",
                "details": {"max_bytes": 8, "reported_bytes": 1},
            },
        )
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_byte_count_mismatch_creates_neither_target_nor_partial_file(self):
        target = self.root / "vm-captures" / "mismatch.ppm"
        response = self.response()
        response["screenshot_bytes"] -= 1
        with mock.patch.object(vm, "broker_call", return_value=response):
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(
            result["error"],
            {
                "code": "capture_response_invalid",
                "message": "the capture byte count did not match the broker metadata",
                "details": {
                    "reported_bytes": len(self.IMAGE) - 1,
                    "decoded_bytes": len(self.IMAGE),
                },
            },
        )
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_oversized_capture_preserves_existing_target_and_creates_no_partial(self):
        target = self.root / "vm-captures" / "existing.ppm"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        response = self.response()
        response["screenshot_bytes"] = vm.MAX_CAPTURE_BYTES + 1
        with mock.patch.object(vm, "broker_call", return_value=response):
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(
            result["error"],
            {
                "code": "capture_too_large",
                "message": f"the capture exceeds the {vm.MAX_CAPTURE_BYTES}-byte limit",
                "details": {
                    "max_bytes": vm.MAX_CAPTURE_BYTES,
                    "reported_bytes": vm.MAX_CAPTURE_BYTES + 1,
                },
            },
        )
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_failed_atomic_replace_removes_partial_and_preserves_existing_target(self):
        target = self.root / "vm-captures" / "existing.ppm"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        with (
            mock.patch.object(vm, "broker_call", return_value=self.response()),
            mock.patch.object(vm.os, "replace", side_effect=OSError("disk full")),
        ):
            result = vm.run_operation("capture", output=str(target))
        self.assertEqual(
            result["error"],
            {
                "code": "capture_write_failed",
                "message": "the capture artifact could not be saved",
                "details": {},
            },
        )
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_capture_help_documents_allowed_path_and_result_fields(self):
        with (
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output,
            self.assertRaises(SystemExit) as raised,
        ):
            vm.client_main(["capture", "--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        for expected in (
            ".sc-state/local/vm-captures",
            "--output",
            "path",
            "bytes",
            "format",
            "mime_type",
        ):
            self.assertIn(expected, help_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
