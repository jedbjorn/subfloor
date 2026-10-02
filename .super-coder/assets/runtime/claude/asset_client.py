"""Bounded private controller ingress, never the public engine API."""
from __future__ import annotations

import json
import os
import socket
from typing import Any

MAX_FRAME_BYTES = 256 * 1024
CONTRACT = "f89-native-runtime-v1"


def call(payload: dict[str, Any], timeout: float = 1) -> dict[str, Any]:
    endpoint = os.environ["SC_F89_CONTROLLER_ENDPOINT"]
    generation = os.environ["SC_F89_GENERATION_ID"]
    timeout = min(max(timeout, 0.01), 5)
    data = json.dumps({"generation": generation, "contract": CONTRACT, "op": "asset",
                       "payload": payload, "timeout": timeout}, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("asset frame exceeds bound")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(endpoint)
        client.sendall(data)
        received = bytearray()
        while b"\n" not in received:
            chunk = client.recv(min(8192, MAX_FRAME_BYTES + 1 - len(received)))
            if not chunk or len(received) + len(chunk) > MAX_FRAME_BYTES:
                raise ValueError("asset response missing or oversized")
            received.extend(chunk)
    response = json.loads(received.split(b"\n", 1)[0])
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ValueError("controller rejected asset")
    result = response.get("result")
    if not isinstance(result, dict):
        raise TypeError("asset result must be an object")
    return result
