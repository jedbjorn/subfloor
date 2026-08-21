#!/usr/bin/env python3
"""Run the real DeepSeek adapter skill-refresh canary against a mock provider."""
from __future__ import annotations

import argparse
import hashlib
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


BOOT_BYTES = "immutable DeepSeek skill canary boot bytes"
MODEL = "deepseek-v4-pro"
INITIAL_SKILLS = ("changed", "current", "revoked")
RESUMED_SKILLS = ("changed", "current", "new")
SESSION_ID = "deepseek-" + "7" * 32
WORKER = Path(__file__).resolve().parent / "deepseek_carrier_worker.py"


def _skill_body(name: str, description: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{body}\n"
    )


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_skill_body(name, description, body))


class _ProviderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "initial"
        self.requests: dict[str, list[dict[str, Any]]] = {
            "initial": [],
            "resumed": [],
        }

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def record(self, request: dict[str, Any]) -> tuple[str, int]:
        with self._lock:
            phase = self.phase
            bucket = self.requests[phase]
            bucket.append(request)
            return phase, len(bucket)


class _MockProvider:
    def __init__(self) -> None:
        self.state = _ProviderState()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    self.send_error(400)
                    return
                phase, request_number = state.record(payload)
                calls = (
                    ["changed"]
                    if phase == "initial"
                    else ["changed", "new", "revoked"]
                )
                chunks: list[dict[str, Any]] = [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_content": "",
                                }
                            }
                        ]
                    }
                ]
                if request_number == 1:
                    chunks.append(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": index,
                                                "id": f"{phase}-call-{index}",
                                                "type": "function",
                                                "function": {
                                                    "name": "skill",
                                                    "arguments": json.dumps(
                                                        {"name": name},
                                                        separators=(",", ":"),
                                                    ),
                                                },
                                            }
                                            for index, name in enumerate(calls)
                                        ]
                                    }
                                }
                            ]
                        }
                    )
                    chunks.append(
                        {
                            "choices": [
                                {"delta": {}, "finish_reason": "tool_calls"}
                            ],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                        }
                    )
                else:
                    chunks.extend(
                        [
                            {"choices": [{"delta": {"content": "done"}}]},
                            {
                                "choices": [
                                    {"delta": {}, "finish_reason": "stop"}
                                ],
                                "usage": {
                                    "prompt_tokens": 12,
                                    "completion_tokens": 1,
                                },
                            },
                        ]
                    )
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

    def wait_for_completion(self, session_id: str) -> str:
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
            if not isinstance(payload, dict) or payload.get("sessionId") != session_id:
                continue
            event = payload.get("event")
            if not isinstance(event, dict) or event.get("type") != "turn/end":
                continue
            data = event.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if kind != "completed":
                raise RuntimeError(f"carrier turn ended as {kind or 'unknown'}")
            return "run.completed"

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.request("shutdown", {})
            self.process.wait(timeout=5)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self.process.terminate()
            self.process.wait(timeout=5)


def _run_phase(
    carrier_python: Path,
    *,
    layout: deepseek_runtime.ConversationLayout,
    worktree: Path,
    provider_url: str,
    message: str,
) -> tuple[int, str, str]:
    child_env = deepseek_runtime.launch_environment(
        layout,
        worktree=worktree,
        system_prompt=BOOT_BYTES,
        provider="deepseek-official",
        api_key="sk-canary-never-persist",
        base_url=provider_url,
        base_env=os.environ,
    )
    child_env.update(
        {
            "SC_DEEPSEEK_PROVIDER": "deepseek-official",
            "SC_DEEPSEEK_MODEL": MODEL,
            "SC_DEEPSEEK_PROVIDER_OPTIONS": json.dumps(
                {"thinking": "omit", "reasoningEffort": "omit"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            "SC_DEEPSEEK_PROVIDER_THINKING": "omit",
            "SC_DEEPSEEK_PROVIDER_REASONING_EFFORT": "omit",
        }
    )
    worker = _CarrierWorker(carrier_python, worktree, child_env)
    try:
        started = worker.request("session/start", {"sessionId": SESSION_ID})
        if not isinstance(started, dict) or started.get("sessionId") != SESSION_ID:
            raise RuntimeError("carrier did not preserve the exact native session")
        prompted = worker.request(
            "session/prompt", {"sessionId": SESSION_ID, "message": message}
        )
        if not isinstance(prompted, dict) or not prompted.get("messageId"):
            raise RuntimeError("carrier returned no native message identity")
        terminal = worker.wait_for_completion(SESSION_ID)
        boot_digest = hashlib.sha256(child_env["DSH_SYSTEM_PROMPT"].encode()).hexdigest()
        return worker.process.pid, terminal, boot_digest
    finally:
        worker.close()


def _catalog_text(request: Mapping[str, Any]) -> str:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise AssertionError("provider request has no message list")
    catalogs = [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and "<available_skills>" in str(message.get("content") or "")
    ]
    if not catalogs:
        raise AssertionError("provider request has no model-visible skill catalog")
    return catalogs[-1]


def _tool_results(request: Mapping[str, Any]) -> list[str]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise AssertionError("provider request has no message list")
    return [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]


def _assert_catalog(text: str, present: Sequence[str], absent: Sequence[str]) -> None:
    for name in present:
        if f"`{name}`" not in text:
            raise AssertionError(f"skill catalog omitted {name}")
    for name in absent:
        if f"`{name}`" in text:
            raise AssertionError(f"skill catalog retained {name}")


def run_canary(carrier_python: Path) -> dict[str, object]:
    if not carrier_python.is_file():
        raise RuntimeError(f"carrier Python is missing: {carrier_python}")
    with tempfile.TemporaryDirectory(prefix="sc-deepseek-skill-canary-") as raw:
        root = Path(raw)
        worktree = root / "worktree"
        skill_root = worktree / ".agents" / "skills"
        worktree.mkdir()
        _write_skill(skill_root, "changed", "Changed skill", "version-one-body")
        _write_skill(skill_root, "current", "Current skill", "current-body")
        _write_skill(skill_root, "revoked", "Revoked skill", "revoked-body")
        state_root = root / "state"
        layout = deepseek_runtime.conversation_layout(
            "cv_" + "7" * 32, state_root=state_root
        )

        with _MockProvider() as provider:
            first_pid, first_terminal, first_boot_digest = _run_phase(
                carrier_python,
                layout=layout,
                worktree=worktree,
                provider_url=provider.url,
                message="Load the changed skill",
            )

            initial_requests = provider.state.requests["initial"]
            if len(initial_requests) != 2:
                raise AssertionError(
                    f"initial turn made {len(initial_requests)} provider requests"
                )
            _assert_catalog(_catalog_text(initial_requests[0]), INITIAL_SKILLS, ("new",))
            initial_results = _tool_results(initial_requests[1])
            if not any("version-one-body" in result for result in initial_results):
                raise AssertionError("initial skill body did not reach the real skill tool")

            _write_skill(skill_root, "changed", "Changed skill", "version-two-body")
            _write_skill(skill_root, "new", "New skill", "new-grant-body")
            (skill_root / "revoked" / "SKILL.md").unlink()
            provider.state.set_phase("resumed")
            resumed_pid, resumed_terminal, resumed_boot_digest = _run_phase(
                carrier_python,
                layout=layout,
                worktree=worktree,
                provider_url=provider.url,
                message="Load the current grants",
            )

            resumed_requests = provider.state.requests["resumed"]
            if len(resumed_requests) != 2:
                raise AssertionError(
                    f"resumed turn made {len(resumed_requests)} provider requests"
                )
            _assert_catalog(
                _catalog_text(resumed_requests[0]), RESUMED_SKILLS, ("revoked",)
            )
            resumed_results = _tool_results(resumed_requests[1])
            serialized_results = json.dumps(resumed_results, sort_keys=True)
            if "version-two-body" not in serialized_results:
                raise AssertionError("changed skill body was stale after exact resume")
            if "new-grant-body" not in serialized_results:
                raise AssertionError("new grant body was unavailable after exact resume")
            if "revoked-body" in serialized_results:
                raise AssertionError("revoked skill body remained loadable after exact resume")
            if not any(
                'skill "revoked" is unknown or no longer available' in result
                for result in resumed_results
            ):
                raise AssertionError(
                    "revoked skill call did not fail as unavailable: "
                    + serialized_results
                )
            expected_boot = hashlib.sha256(BOOT_BYTES.encode()).hexdigest()
            if {first_boot_digest, resumed_boot_digest} != {expected_boot}:
                raise AssertionError("immutable boot digest changed on resume")
            if first_pid == resumed_pid:
                raise AssertionError("resume reused the old carrier process")

        manifest = deepseek_runtime.load_runtime_manifest()
        return {
            "schema_version": 1,
            "contract": "deepseek-production-skill-resume-v1",
            "source_commit": manifest["source"]["commit"],
            "composition_sha256": manifest["composition"]["sha256"],
            "initial_catalog": list(INITIAL_SKILLS),
            "resumed_catalog": list(RESUMED_SKILLS),
            "changed_body_refreshed": True,
            "new_grant_loadable": True,
            "revoked_grant_absent": True,
            "boot_digest_preserved": True,
            "native_session_preserved": True,
            "fresh_carrier_process": True,
            "initial_terminal": first_terminal,
            "resumed_terminal": resumed_terminal,
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
