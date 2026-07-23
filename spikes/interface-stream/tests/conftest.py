"""Session fixture: spike server on a free port in a background thread, with
its own asyncio loop; private tmux server per test session; all state in /tmp."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import Api, WSClient, tmux  # noqa: E402

# The engine API (.super-coder/api/server.py) is also imported as plain
# `server` by the tests/ suite in the same pytest process. Importing the
# spike's server under that top-level name poisons sys.modules for every
# later `import server` (sprint 25 seq 11: with the Interface stack baked,
# this conftest loads and 65 engine tests errored on
# `module 'server' has no attribute 'Handler'`). Load it namespaced.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "spike_server",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "server.py"))
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Server = _mod.Server


@pytest.fixture(scope="session")
def spike():
    server = Server(0, "spike")
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(30), "server failed to start"

    ns = SimpleNamespace(
        server=server, loop=loop, api=Api(server.port, "spike"),
        port=server.port, token="spike",
        sock=server.broker.sock, run_dir=server.broker.run_dir,
        created=[])
    yield ns

    for sid in list(ns.created):
        try:
            asyncio.run_coroutine_threadsafe(
                server.broker.terminate_session(sid), loop).result(10)
        except Exception:
            pass
    fut = asyncio.run_coroutine_threadsafe(server.stop(), loop)
    try:
        fut.result(30)
    except Exception:
        pass
    loop.call_soon_threadsafe(loop.stop)
    thread.join(10)
    shutil.rmtree(ns.run_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _terminate_sessions(spike):
    yield
    for sid in list(spike.created):
        try:
            asyncio.run_coroutine_threadsafe(
                spike.server.broker.terminate_session(sid), spike.loop).result(10)
        except Exception:
            pass
        finally:
            spike.created.remove(sid)


@pytest.fixture()
def make_session(spike):
    def _make(harness="bash", command=None, worktree="/tmp", rows=24, cols=80,
              wake_prompt="WAKEPROMPT\n", quiet_ms=3000, idle_quiet_ms=1000):
        st, body = spike.api("POST", "/api/interface/sessions", {
            "harness": harness, "worktree": worktree, "command": command,
            "rows": rows, "cols": cols, "wake_prompt": wake_prompt,
            "quiet_ms": quiet_ms, "idle_quiet_ms": idle_quiet_ms})
        assert st == 201, f"session create failed: {st} {body}"
        spike.created.append(body["session_id"])
        return body
    return _make


@pytest.fixture()
def ws_factory(spike):
    def _connect(sid: str, role: str = "viewer", lease_token: str | None = None) -> WSClient:
        return WSClient(spike.api, sid, role=role, lease_token=lease_token)
    return _connect


@pytest.fixture()
def writer(spike, ws_factory):
    def _make(sid: str, takeover: bool = False):
        st, body = spike.api("POST", "/api/interface/writer-leases",
                             {"session_id": sid, "takeover": takeover})
        assert st == 201, f"lease failed: {st} {body}"
        client = ws_factory(sid, role="writer", lease_token=body["lease_token"])
        return client, body
    return _make
