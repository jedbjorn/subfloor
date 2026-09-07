import http.client
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))
import browser
import browser_proxy as proxy
import run


@pytest.fixture
def gates(tmp_path, monkeypatch):
    calls = []

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(msg)
            method = msg["method"]
            if method == "tools/call" and msg["params"]["name"] == "slow":
                gate.slow_started.set()
                time.sleep(0.3)
            result = (
                {"tools": [{"name": "browser_snapshot"}]}
                if method == "tools/list"
                else {"content": []}
            )
            data = (
                b"data: "
                + json.dumps(
                    {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
                ).encode()
                + b"\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "upstream-" + str(len(calls)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = {"armed": True, "browser_port": upstream.server_port}
    monkeypatch.setattr(browser, "private", lambda: tmp_path)
    monkeypatch.setattr(browser, "process_receipt", lambda name: {"start": "1"})
    gate = proxy.Gate(
        ("127.0.0.1", 0),
        config,
        state=lambda: config,
        window_check=lambda config: None,
        timeout=0.15,
    )
    gate.slow_started = threading.Event()
    threading.Thread(target=gate.serve_forever, daemon=True).start()
    yield gate, config, calls
    gate.shutdown()
    gate.server_close()
    upstream.shutdown()
    upstream.server_close()


def request(gate, shell, method, session=None, params=None):
    client = http.client.HTTPConnection("127.0.0.1", gate.server_port, timeout=2)
    headers = {"Content-Type": "application/json"}
    if session:
        headers["Mcp-Session-Id"] = session
    client.request(
        "POST",
        "/mcp/" + shell,
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ),
        headers,
    )
    response = client.getresponse()
    sid = response.getheader("Mcp-Session-Id")
    body = response.read()
    client.close()
    return sid, proxy.rpc_result(body), body


def test_shell_identity_sse_audit_and_cross_session_refusal(gates):
    gate, _config, calls = gates
    for shell in ["DEV5", "REV1"]:
        sid, _, _ = request(
            gate, shell, "initialize", params={"clientInfo": {"name": "spoof"}}
        )
        _, result, raw = request(gate, shell, "tools/list", sid)
        assert result["result"]["tools"][0]["name"] == "browser_snapshot"
        assert raw.startswith(b"data: ")
        assert request(gate, "OTHER", "tools/list", sid)[1]["error"]
        assert request(
            gate,
            shell,
            "tools/call",
            sid,
            {
                "name": "browser_navigate",
                "arguments": {"url": "https://example.test/path?secret=value"},
            },
        )[1]["result"]
    initialized = [m for m in calls if m["method"] == "initialize"]
    assert [m["params"]["clientInfo"]["name"] for m in initialized] == [
        "Subfloor DEV5",
        "Subfloor REV1",
    ]
    rows = [json.loads(x) for x in gate.audit_path.read_text().splitlines()]
    assert {r["shell"] for r in rows} == {"DEV5", "REV1"}
    assert all(
        r["target"] == "https://example.test/path" and r["result"] == "ok" for r in rows
    )
    assert gate.audit_path.stat().st_mode & 0o777 == 0o600


def test_disarmed_discovery_and_named_refusal(gates):
    gate, config, calls = gates
    config["armed"] = False
    sid, init, _ = request(gate, "DEV5", "initialize")
    assert "result" in init
    assert "tools" in request(gate, "DEV5", "tools/list", sid)[1]["result"]
    assert (
        "FnB"
        in request(gate, "DEV5", "tools/call", sid, {"name": "browser_click"})[1][
            "error"
        ]["message"]
    )
    assert not calls


def test_timeout_is_named_and_not_replayed(gates):
    gate, _config, calls = gates
    sid, _, _ = request(gate, "DEV5", "initialize")
    started = time.monotonic()
    _, result, _ = request(gate, "DEV5", "tools/call", sid, {"name": "slow"})
    assert "extension not connected" in result["error"]["message"]
    assert time.monotonic() - started < 0.5
    assert sum(m["method"] == "tools/call" for m in calls) == 1


def test_guard_refusal_does_not_reach_upstream(gates):
    gate, _config, calls = gates

    def refuse(config):
        raise ValueError("extension not connected")

    gate.window_check = refuse
    sid, _, _ = request(gate, "DEV5", "initialize")
    assert (
        "error"
        in request(gate, "DEV5", "tools/call", sid, {"name": "browser_snapshot"})[1]
    )
    assert not calls


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_both_managed_servers_and_container_exclusion(harness, monkeypatch):
    config = {"vm": {"domain": "windows"}, "browser": {"proxy_port": 8860}}
    monkeypatch.setattr(run.ports_mod, "resolve", lambda **kwargs: config)
    adapter = json.loads((Path(run.ADAPTERS) / harness / "adapter.json").read_text())
    first = run.managed_mcp_injection(adapter, "DEV5", sandbox=False)
    again = run.managed_mcp_injection(adapter, "DEV5", sandbox=False)
    assert first == again
    assert "127.0.0.1:8860/mcp/DEV5" in json.dumps(first)
    assert "127.0.0.1:18000/mcp" in json.dumps(first)
    container = run.managed_mcp_injection(adapter, "DEV5", sandbox=True)
    assert "8860" not in json.dumps(container)
    assert "18000" in json.dumps(container)


def test_concurrent_shells_and_no_same_session_queue(gates):
    gate, _, _ = gates
    a, _, _ = request(gate, "DEV5", "initialize")
    b, _, _ = request(gate, "REV1", "initialize")
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            request(gate, "DEV5", "tools/call", a, {"name": "slow"})[1]
        )
    )
    worker.start()
    assert gate.slow_started.wait(1)
    _, other, _ = request(gate, "REV1", "tools/call", b, {"name": "browser_snapshot"})
    assert "result" in other
    _, busy, _ = request(gate, "DEV5", "tools/call", a, {"name": "browser_click"})
    assert "not queued" in busy["error"]["message"]
    worker.join(timeout=1)
    assert results and "extension not connected" in results[0]["error"]["message"]
