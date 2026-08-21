#!/usr/bin/env python3
"""Isolated DeepSeek SDK worker controlled by the Python 3.9 engine.

The worker runs only under the pinned carrier interpreter. Its stdout is a
small JSON-line protocol owned by super-coder; the official SDK owns the
nested runtime process and its native JSON-RPC protocol.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

# ``python -I worker.py`` deliberately removes the script directory from the
# import path. Restore only this engine-owned directory so the standard
# entrypoint wrapper remains available without exposing ambient Python paths.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deepseek_harness import HarnessClient, HarnessConfig

MAX_LINE_BYTES = 1024 * 1024
MAX_DETAIL_CHARS = 4096
SECRET_TEXT = (
    re.compile(r"(?i)((?:DEEPSEEK|OLLAMA)_API_KEY\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{8,}"),
)


def _detail(exc: BaseException) -> str:
    value = str(exc)
    for pattern in SECRET_TEXT:
        value = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "")
            + "[REDACTED]",
            value,
        )
    return value[:MAX_DETAIL_CHARS]


def _json_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        return legacy_dict()
    return value


class Writer:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def send(self, value: Mapping[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_LINE_BYTES:
            encoded = json.dumps(
                {
                    "method": "worker/error",
                    "params": {
                        "code": "HARNESS_EVENT_TOO_LARGE",
                        "detail": "carrier event exceeded the 1 MiB protocol bound",
                    },
                },
                separators=(",", ":"),
            )
        with self._lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()


def _notification_loop(client: HarnessClient, writer: Writer) -> None:
    try:
        while True:
            notification = client.next_notification()
            writer.send(
                {
                    "method": "native/notification",
                    "params": {
                        "method": notification.method,
                        "payload": notification.payload,
                    },
                }
            )
    except BaseException as exc:
        writer.send(
            {
                "method": "worker/error",
                "params": {
                    "code": "HARNESS_UNAVAILABLE",
                    "detail": _detail(exc),
                },
            }
        )


def _request_loop(client: HarnessClient, writer: Writer) -> None:
    """Reject every unexpected server-to-client interaction fail closed."""
    try:
        while True:
            request = client.next_request()
            writer.send(
                {
                    "method": "native/request",
                    "params": {
                        "requestId": request.id,
                        "method": request.method,
                        "payload": request.payload,
                    },
                }
            )
            client.respond_error(
                request.id,
                code=-32001,
                message="super-coder unattended conversations cannot answer native interaction requests",
            )
    except BaseException as exc:
        writer.send(
            {
                "method": "worker/error",
                "params": {
                    "code": "HARNESS_UNAVAILABLE",
                    "detail": _detail(exc),
                },
            }
        )


def _dispatch(client: HarnessClient, method: str, params: Mapping[str, Any]) -> Any:
    session_id = params.get("sessionId")
    if method != "shutdown" and (not isinstance(session_id, str) or not session_id):
        raise ValueError("carrier lifecycle request requires sessionId")
    if method == "session/start":
        return _json_value(client.session_start(session_id))
    if method == "session/prompt":
        message = params.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("carrier prompt must be non-empty")
        return {
            "messageId": client.session_prompt(
                session_id,
                [{"type": "text", "text": message}],
            )
        }
    if method == "session/cancel":
        return _json_value(client.session_cancel(session_id))
    if method == "session/inspect":
        return _json_value(client.session_inspect(session_id))
    if method == "session/reconcile":
        boundary = params.get("fromEventSeq")
        if not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0:
            raise ValueError("fromEventSeq must be a non-negative integer")
        return _json_value(
            client.session_reconcile(session_id, from_event_seq=boundary)
        )
    if method == "shutdown":
        client.close()
        return {}
    raise ValueError(f"unsupported carrier worker method: {method}")


def main() -> int:
    writer = Writer()
    options = json.loads(os.environ["SC_DEEPSEEK_PROVIDER_OPTIONS"])
    config = HarnessConfig(
        cwd=os.environ["DSH_CWD"],
        env=dict(os.environ),
        request_timeout_seconds=30,
        shutdown_timeout_seconds=2,
    )
    client = HarnessClient(config)
    try:
        client.start()
        client.initialize(
            cwd=os.environ["DSH_CWD"],
            provider=os.environ["SC_DEEPSEEK_PROVIDER"],
            model=os.environ["SC_DEEPSEEK_MODEL"],
            provider_request_options=options,
        )
        threading.Thread(
            target=_notification_loop,
            args=(client, writer),
            name="deepseek-native-notifications",
            daemon=True,
        ).start()
        threading.Thread(
            target=_request_loop,
            args=(client, writer),
            name="deepseek-native-requests",
            daemon=True,
        ).start()
        for line in sys.stdin:
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                raise ValueError("carrier control request exceeds 1 MiB")
            message = json.loads(line)
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            if request_id is None or not isinstance(method, str) or not isinstance(params, dict):
                continue
            try:
                result = _dispatch(client, method, params)
                writer.send({"id": request_id, "result": result})
                if method == "shutdown":
                    return 0
            except BaseException as exc:
                writer.send(
                    {
                        "id": request_id,
                        "error": {
                            "code": getattr(exc, "code", "HARNESS_PROTOCOL_ERROR"),
                            "detail": _detail(exc),
                        },
                    }
                )
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
