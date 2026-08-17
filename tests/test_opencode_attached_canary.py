"""No-token compatibility canary for an attached OpenCode route turn."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPENCODE_VERSION = "1.18.9"


class _ProviderFixture:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                fixture.requests.append((self.path, payload))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = (
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ok"},
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }],
                    },
                )
                for chunk in chunks:
                    self.wfile.write(
                        b"data: " + json.dumps(chunk).encode() + b"\n\n"
                    )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="opencode-provider-fixture",
            daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return int(reserved.getsockname()[1])


def _request_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
) -> dict | list:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


class OpenCodeAttachedCanary(unittest.TestCase):
    """Exercise the exact attached CLI seam without provider-token spend."""

    def setUp(self) -> None:
        required = os.environ.get("SC_REQUIRE_OPENCODE_CANARY") == "1"
        executable = shutil.which("opencode")
        if executable is None:
            if required:
                self.fail("OpenCode is required for this compatibility gate")
            self.skipTest("OpenCode is not installed")
        version = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if version != OPENCODE_VERSION:
            if required:
                self.fail(
                    f"OpenCode {OPENCODE_VERSION} required; found {version}"
                )
            self.skipTest(
                f"OpenCode {OPENCODE_VERSION} required; found {version}"
            )
        self.opencode = executable
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.provider = _ProviderFixture()
        self.provider.start()
        self.addCleanup(self.provider.close)

    def _start_server(self) -> tuple[subprocess.Popen[str], str]:
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        environment = {
            **os.environ,
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
        }
        process = subprocess.Popen(
            [
                self.opencode,
                "serve",
                "--pure",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_server, process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    "OpenCode server exited during startup:\n"
                    f"stdout: {stdout}\nstderr: {stderr}"
                )
            try:
                health = _request_json("GET", f"{url}/global/health")
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
                continue
            if not isinstance(health, dict):
                self.fail(f"OpenCode health response was not an object: {health!r}")
            self.assertEqual(health.get("healthy"), True)
            return process, url
        self.fail("OpenCode server did not become healthy within 20 seconds")

    @staticmethod
    def _stop_server(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    def test_attached_turn_applies_one_exact_variant_overlay(self) -> None:
        digest = "a" * 64
        agent = f"sc-route-{digest}"
        config = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "fixture": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Fixture",
                    "options": {
                        "baseURL": self.provider.url,
                        "apiKey": "fixture-only",
                    },
                    "models": {
                        "fixture-model": {
                            "name": "Fixture model",
                            "variants": {
                                "high": {"reasoningEffort": "high"},
                            },
                        },
                    },
                },
            },
            "agent": {
                agent: {
                    "mode": "primary",
                    "model": "fixture/fixture-model",
                    "reasoningEffort": "high",
                },
            },
        }
        (self.root / "opencode.json").write_text(json.dumps(config))
        _process, server_url = self._start_server()
        session = _request_json(
            "POST",
            f"{server_url}/session?{urllib.parse.urlencode({'directory': self.root})}",
            body={"title": "Attached compatibility canary"},
        )
        if not isinstance(session, dict):
            self.fail(f"OpenCode session response was not an object: {session!r}")
        session_id = session["id"]
        prompt = "Return the fixture response once."

        completed = subprocess.run(
            [
                self.opencode,
                "run",
                "--attach",
                server_url,
                "--session",
                session_id,
                "--model",
                "fixture/fixture-model",
                "--agent",
                agent,
                "--variant",
                "high",
                "--format",
                "json",
                "--dir",
                str(self.root),
                prompt,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self.provider.requests), 1)
        path, request = self.provider.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(request["model"], "fixture-model")
        self.assertEqual(request["reasoning_effort"], "high")
        user_messages = [
            message
            for message in request["messages"]
            if message.get("role") == "user"
        ]
        self.assertEqual(len(user_messages), 1)
        self.assertEqual(user_messages[0]["content"], json.dumps(prompt))
        messages = _request_json(
            "GET",
            f"{server_url}/session/{session_id}/message?"
            f"{urllib.parse.urlencode({'directory': self.root})}",
        )
        if not isinstance(messages, list):
            self.fail(f"OpenCode messages response was not a list: {messages!r}")
        self.assertEqual(len(messages), 2)
        user, assistant = messages
        self.assertEqual(user["info"]["agent"], agent)
        self.assertEqual(user["info"]["model"], {
            "providerID": "fixture",
            "modelID": "fixture-model",
            "variant": "high",
        })
        self.assertEqual(
            [(part["type"], part.get("text")) for part in user["parts"]],
            [("text", json.dumps(prompt))],
        )
        self.assertEqual(assistant["info"]["parentID"], user["info"]["id"])
        self.assertEqual(assistant["info"]["providerID"], "fixture")
        self.assertEqual(assistant["info"]["modelID"], "fixture-model")
        self.assertEqual(assistant["info"]["agent"], agent)
        self.assertEqual(assistant["info"]["mode"], agent)
        self.assertEqual(assistant["info"]["variant"], "high")
        self.assertEqual(assistant["info"]["finish"], "stop")
        self.assertEqual(
            [part["type"] for part in assistant["parts"]],
            ["step-start", "text", "step-finish"],
        )
        self.assertEqual(
            [
                part["text"]
                for part in assistant["parts"]
                if part["type"] == "text"
            ],
            ["ok"],
        )


if __name__ == "__main__":
    unittest.main()
