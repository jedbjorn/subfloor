"""Generation-capability handoff stays out of launcher/browser argv."""
from __future__ import annotations

import importlib
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
browser_handoff = importlib.import_module("browser_handoff")


def test_one_shot_handoff_consumes_owner_only_capability_without_ready_output() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        capability = root / "capability"
        ready = root / "ready"
        generation = "a" * 64
        capability.write_text(f"http://127.0.0.1:8942/?sc_generation={generation}\n")
        capability.chmod(0o600)
        result: list[int] = []
        thread = threading.Thread(
            target=lambda: result.append(browser_handoff.handoff(capability, ready, 2)),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 1
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        opened = ready.read_text().strip()
        assert opened.startswith("http://127.0.0.1:")
        assert "/handoff/" in opened
        assert generation not in opened
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, request, response, code, message, headers):
                return response

        request = urllib.request.Request(opened, method="GET")
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=1) as response:
            assert response.headers["Location"].endswith(generation)
        thread.join(timeout=1)
        assert result == [0]
        assert not capability.exists()


def test_handoff_rejects_unsafe_capability_artifacts() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        capability = root / "capability"
        capability.write_text("http://127.0.0.1:8942/?sc_generation=" + "b" * 64)
        capability.chmod(0o644)

        with pytest.raises(ValueError):
            browser_handoff.handoff(capability, root / "ready", 0.1)
