#!/usr/bin/env python3
"""Run one ordinary DeepSeek carrier prompt against a loopback provider."""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence


ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import deepseek_runtime  # noqa: E402


MODEL = "deepseek-ordinary-inference-canary"
PROVIDER = "deepseek-official"
SESSION_ID = "deepseek-" + "8" * 32
WORKER = Path(__file__).resolve().parent / "deepseek_carrier_worker.py"


class _MockProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    self.send_error(400)
                    return
                requests.append(payload)
                chunks = [
                    {
                        "choices": [{
                            "delta": {
                                "role": "assistant",
                                "content": "Hello from DeepSeek.",
                                "reasoning_content": "",
                            }
                        }]
                    },
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 8, "completion_tokens": 4},
                    },
                ]
                body = "".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                    for chunk in chunks
                ) + "data: [DONE]\n\n"
                encoded = body.encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_MockProvider":
        self.thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _CarrierWorker:
    def __init__(self, carrier_python: Path, worktree: Path, env: Mapping[str, str]):
        self.process = subprocess.Popen(
            [str(carrier_python), "-I", str(WORKER)],
            cwd=worktree,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._deferred: list[dict[str, Any]] = []
        self._stderr: list[str] = []
        self._next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._messages.put(
                    {"method": "worker/error", "params": {"detail": str(exc)}}
                )
                return
            if isinstance(message, dict):
                self._messages.put(message)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        remaining = 4096
        for line in self.process.stderr:
            if remaining <= 0:
                continue
            chunk = deepseek_runtime.sanitize_diagnostic(line, limit=remaining)
            self._stderr.append(chunk)
            remaining -= len(chunk)

    def _next(self, timeout: float = 15) -> dict[str, Any]:
        if self._deferred:
            return self._deferred.pop(0)
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            detail = "".join(self._stderr) or "carrier worker timed out"
            raise RuntimeError(detail) from exc

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {"id": request_id, "method": method, "params": dict(params)},
                separators=(",", ":"),
            )
            + "\n"
        )
        self.process.stdin.flush()
        deferred: list[dict[str, Any]] = []
        while True:
            message = self._next()
            if message.get("id") != request_id:
                deferred.append(message)
                continue
            self._deferred[0:0] = deferred
            error = message.get("error")
            if isinstance(error, dict):
                raise RuntimeError(str(error.get("detail") or error.get("code")))
            return message.get("result")

    def wait_for_completion(self) -> tuple[str, str]:
        assistant_text: list[str] = []
        while True:
            message = self._next()
            method = message.get("method")
            params = message.get("params")
            if method == "worker/error" and isinstance(params, dict):
                raise RuntimeError(str(params.get("detail") or "carrier failed"))
            if method == "native/request":
                raise RuntimeError("carrier requested unsupported interaction")
            if method != "native/notification" or not isinstance(params, dict):
                continue
            if params.get("method") != "session.event":
                continue
            payload = params.get("payload")
            if not isinstance(payload, dict) or payload.get("sessionId") != SESSION_ID:
                continue
            event = payload.get("event")
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if event.get("type") == "assistant/chunk" and isinstance(data, dict):
                chunk = data.get("chunk")
                if isinstance(chunk, dict) and chunk.get("type") == "text-delta":
                    assistant_text.append(str(chunk.get("text") or ""))
                continue
            if event.get("type") != "turn/end":
                continue
            reason = data.get("reason") if isinstance(data, dict) else None
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if kind != "completed":
                raise RuntimeError(f"carrier turn ended as {kind or 'unknown'}")
            return "run.completed", "".join(assistant_text)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.request("shutdown", {})
            self.process.wait(timeout=5)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self.process.terminate()
            self.process.wait(timeout=5)


def run_canary(carrier_python: Path) -> dict[str, object]:
    if not carrier_python.is_file():
        raise RuntimeError(f"carrier Python is missing: {carrier_python}")
    with tempfile.TemporaryDirectory(prefix="sc-deepseek-inference-canary-") as raw:
        root = Path(raw)
        worktree = root / "worktree"
        worktree.mkdir()
        layout = deepseek_runtime.conversation_layout(
            "cv_" + "8" * 32, state_root=root / "state"
        )
        with _MockProvider() as provider:
            child_env = deepseek_runtime.launch_environment(
                layout,
                worktree=worktree,
                system_prompt="Complete the user's ordinary request.",
                provider=PROVIDER,
                api_key="sk-loopback-canary",
                base_url=provider.url,
                base_env=os.environ,
            )
            child_env.update({
                "SC_DEEPSEEK_PROVIDER": PROVIDER,
                "SC_DEEPSEEK_MODEL": MODEL,
                "SC_DEEPSEEK_PROVIDER_OPTIONS": json.dumps(
                    {"thinking": "omit", "reasoningEffort": "omit"},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "SC_DEEPSEEK_PROVIDER_THINKING": "omit",
                "SC_DEEPSEEK_PROVIDER_REASONING_EFFORT": "omit",
            })
            worker = _CarrierWorker(carrier_python, worktree, child_env)
            try:
                started = worker.request("session/start", {"sessionId": SESSION_ID})
                if not isinstance(started, dict) or started.get("sessionId") != SESSION_ID:
                    raise RuntimeError("carrier did not preserve the exact session")
                prompted = worker.request(
                    "session/prompt",
                    {"sessionId": SESSION_ID, "message": "Offer a brief greeting."},
                )
                if not isinstance(prompted, dict) or not prompted.get("messageId"):
                    raise RuntimeError("carrier returned no message identity")
                terminal, assistant_text = worker.wait_for_completion()
            finally:
                worker.close()

        if len(provider.requests) != 1:
            raise AssertionError(
                f"ordinary inference made {len(provider.requests)} provider requests"
            )
        request = provider.requests[0]
        if request.get("model") != MODEL:
            raise AssertionError("ordinary inference changed the exact model")
        if "thinking" in request or "reasoning_effort" in request:
            raise AssertionError("reserved defaults reached the provider wire")
        if not assistant_text.strip():
            raise AssertionError("ordinary inference returned no assistant text")

    manifest = deepseek_runtime.load_runtime_manifest()
    return {
        "schema_version": 1,
        "contract": "deepseek-production-ordinary-inference-v1",
        "source_commit": manifest["source"]["commit"],
        "composition_sha256": manifest["composition"]["sha256"],
        "provider": PROVIDER,
        "model": MODEL,
        "provider_request_count": 1,
        "reserved_default_omitted": True,
        "assistant_response_nonempty": True,
        "terminal": terminal,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carrier-python", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    receipt = run_canary(args.carrier_python.absolute())
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt is not None:
        args.receipt.write_text(encoded)
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
