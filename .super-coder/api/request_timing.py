"""Bounded, process-local buffered request diagnostics (Feature #88 R1)."""
from __future__ import annotations

import math
import re
import threading
from collections import deque
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_OPAQUE_ID = re.compile(
    r"(?:[+-]?\d+|(?:[a-zA-Z]+[_-])?[0-9a-fA-F]{8,}|"
    r"(?:[a-zA-Z]+[_-])?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
    r"(?=[a-zA-Z0-9_-]{16,}$)(?=.*[0-9A-Z])[a-zA-Z0-9_-]+)"
)
_LITERAL_SEGMENT = re.compile(r"[a-zA-Z0-9_.-]{1,128}")
_METHOD = re.compile(r"[A-Z]{1,20}")


def route_template(method: str, path: str) -> str:
    """Drop query values and collapse numeric/opaque path segments."""
    try:
        parsed = urlsplit(path)
        route_path, query = parsed.path, parsed.query
    except ValueError:
        # A malformed absolute target must still receive its timed error.
        route_path, query = "/{id}", path.partition("?")[2]
    segments = []
    for segment in route_path.split("/"):
        decoded = unquote(segment)
        segments.append(
            "{id}" if decoded and (
                _OPAQUE_ID.fullmatch(decoded)
                or not _LITERAL_SEGMENT.fullmatch(decoded)
            ) else decoded
        )
    refresh = any(
        unquote(flag.split("=", 1)[0]) == "refresh"
        for flag in query.split("&")
    )
    method = method if _METHOD.fullmatch(method) else "OTHER"
    return f"{method} {'/'.join(segments)}" + ("?refresh" if refresh else "")


class RequestTimingRecorder:
    """Keep 500 samples per template; reserve one of 64 slots for overflow."""

    def __init__(self, *, started_at: str = _PROCESS_STARTED_AT):
        self.started_at = started_at
        self._samples: dict[str, deque] = {}
        self._lock = threading.Lock()

    def record(self, template: str, queue: float, app: float) -> str:
        with self._lock:
            if template not in self._samples:
                if len(self._samples) >= 63:
                    template = "other"
                self._samples.setdefault(template, deque(maxlen=500))
            self._samples[template].append((queue, app))
        return template

    def snapshot(self) -> dict:
        with self._lock:
            samples = {key: list(values) for key, values in self._samples.items()}
        templates = {}
        for template, values in samples.items():
            stats = {"count": len(values)}
            for index, name in enumerate(("queue", "app")):
                ordered = sorted(value[index] for value in values)
                # Nearest-rank percentiles, including the one-sample case.
                stats[name] = {
                    "p50": ordered[math.ceil(len(ordered) * 0.50) - 1],
                    "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
                    "max": ordered[-1],
                }
            templates[template] = stats
        return {"started_at": self.started_at, "templates": templates}


RECORDER = RequestTimingRecorder()


def handle(method: str, headers_raw: str, *, recorder=None) -> tuple:
    """Use the conversation API's exact loopback/operator admission checks."""
    import conversation_routes as routes

    try:
        headers = routes._parse_headers(headers_raw)
        if not routes._host_ok(headers):
            return routes._err(
                403, "HOST_NOT_ALLOWED",
                "conversation API serves 127.0.0.1/localhost only",
            )
        con = routes._db()
        try:
            routes._operator(con, headers)
            if method != "GET":
                return routes._err(405, "METHOD_NOT_ALLOWED", "use GET")
            recorder = recorder if recorder is not None else RECORDER
            return routes._json(200, recorder.snapshot())
        finally:
            con.close()
    except routes.ApiError as exc:
        return routes._api_error(exc)
    except Exception:  # noqa: BLE001 — endpoint errors must also be no-store
        return routes._err(
            500, "INTERNAL_ERROR", "request timing diagnostics failed",
        )
