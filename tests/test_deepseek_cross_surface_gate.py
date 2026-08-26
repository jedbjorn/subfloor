"""Provider-free production boundary proof for DeepSeek identity fencing.

This test intentionally starts the real review API, the shipped command
clients, the engine gateway, and the pinned stock ``dsh`` process.  It is not
a substitute Host: all assertions across the two disposable shell identities
observe the production HTTP/SQLite/loopback boundaries used by a launched
shell.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api")]

import deepseek_host  # noqa: E402
import deepseek_one_shot  # noqa: E402
import deepseek_web  # noqa: E402
import mem  # noqa: E402
import route_bindings  # noqa: E402
import server  # noqa: E402
import sprint_cli  # noqa: E402
from conversation_adapters.base import AdapterError, ConversationContext  # noqa: E402
from conversation_adapters.deepseek import DeepSeekAdapter  # noqa: E402
from conversation_broker import BrokerRun, ConversationBroker  # noqa: E402


ALICE_TOKEN = "deepseek-cross-surface-alice-token"
BOB_TOKEN = "deepseek-cross-surface-bob-token"
PROVIDER_TOKEN = "deepseek-cross-surface-provider-token"


class _ControlledProvider:
    """A local OpenAI-compatible SSE endpoint used by stock DSH unchanged."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hold = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                fixture.requests.append(payload)
                fixture.entered.set()
                if fixture.hold and not fixture.release.wait(timeout=15):
                    self.send_error(504, "controlled provider timed out")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in (
                    {"id": "gate", "object": "chat.completion.chunk", "created": 1,
                     "model": "fixture", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": "gate"},
                     "finish_reason": None}]},
                    {"id": "gate", "object": "chat.completion.chunk", "created": 1,
                     "model": "fixture", "choices": [{"index": 0, "delta": {},
                     "finish_reason": "stop"}]},
                ):
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _PhaseBlockingHost:
    """Observe one real Host boundary and pause before it proceeds.

    This delegates every RPC and event to the stock process.  It is a wire
    barrier for the public one-shot's lifetime, not a substitute Host.
    """

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.delegate = deepseek_host.DeepSeekHostClient()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _pause(self) -> None:
        if self._blocked:
            return
        self._blocked = True
        self.entered.set()
        assert self.release.wait(timeout=10), self.phase

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((method, payload))
        if method == self.phase:
            self._pause()
        return self.delegate.call(method, payload)

    def open_events(self):
        stream = self.delegate.open_events()
        if self.phase not in {"turn/end", "stream.close"}:
            return stream

        fixture = self

        class PausedStream:
            def __iter__(self):
                for envelope in stream:
                    payload = envelope.get("payload")
                    event = payload.get("event") if isinstance(payload, dict) else None
                    if isinstance(event, dict) and event.get("type") == "turn/end":
                        fixture._pause()
                    yield envelope

            def close(self) -> None:
                stream.close()
                if fixture.phase == "stream.close":
                    fixture._pause()

        return PausedStream()


class _CancellationBlockingHost(_PhaseBlockingHost):
    """Lose the provider stream, then pause inside real cancellation proof."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.cancelled = False

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((method, payload))
        value = self.delegate.call(method, payload)
        if method == "session.cancel":
            self.cancelled = True
            if self.phase == method:
                self._pause()
        elif method == "session.history" and self.cancelled and self.phase == method:
            self._pause()
        return value

    def open_events(self):
        stream = self.delegate.open_events()

        class InterruptedStream:
            def __iter__(self):
                for envelope in stream:
                    payload = envelope.get("payload")
                    event = payload.get("event") if isinstance(payload, dict) else None
                    if isinstance(event, dict) and event.get("type") == "turn/start":
                        raise deepseek_host.DeepSeekHostError(
                            "HARNESS_PROVIDER_STREAM_FAILED",
                            "controlled stream loss after prompt admission",
                        )
                    yield envelope

            def close(self) -> None:
                stream.close()

        return InterruptedStream()


def _build_db(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            con.executescript(migration.read_text())
        con.execute("INSERT INTO users (user_id,username,is_active) VALUES (1,'gate',1)")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,mandate,system_prompt,"
            "user_id,is_shared,has_identity,bootstrapped,api_key) "
            "VALUES (?,?,?,?,?,?,1,0,1,1,?)",
            (
                (41, "Alice", "ALICE", "dev", "gate", "gate", ALICE_TOKEN),
                (42, "Bob", "BOB", "dev", "gate", "gate", BOB_TOKEN),
            ),
        )
        feature_id = int(con.execute(
            "INSERT INTO roadmap (title,roadmap_status) VALUES ('gate','in_progress')"
        ).lastrowid)
        sprint_id = int(con.execute(
            "INSERT INTO sprints (feature_id,originating_planner_shell_id) VALUES (?,41)",
            (feature_id,),
        ).lastrowid)
        con.executemany(
            "INSERT INTO sprint_participants (sprint_id,shell_id,role,harness) "
            "VALUES (?,?,?,?)",
            ((sprint_id, 41, "developer", "deepseek"),
             (sprint_id, 42, "reviewer", "deepseek")),
        )
        con.commit()
        return sprint_id
    finally:
        con.close()


def _rows(path: Path, table: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY 1")]
    finally:
        con.close()


def _protected_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    # These are independent stores/projections.  A pre-prompt refusal may not
    # mutate any one of them, even indirectly through a wake delivery intent.
    return {
        table: _rows(path, table)
        for table in (
            "shells", "shell_messages", "sprints", "sprint_participants",
            "sprint_events", "wake_message",
            "sprint_wake_messages", "sprint_wake_outbox",
        )
    }


def _assert_database_secrets_absent(
    database: Path, *, secrets: tuple[str, ...], capabilities: tuple[str, ...]
) -> None:
    """Sweep every persisted cell, exempting only canonical shell API keys."""
    con = sqlite3.connect(database)
    try:
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            columns = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
            assert columns, table
            quoted = ", ".join(f'"{column}"' for column in columns)
            for row in con.execute(f'SELECT {quoted} FROM "{table}"'):
                for column, value in zip(columns, row):
                    if table == "shells" and column == "api_key":
                        continue
                    payload = (
                        value if isinstance(value, bytes) else str(value).encode()
                    )
                    assert not any(secret.encode() in payload for secret in secrets), (
                        table, column
                    )
                    assert not any(capability.encode() in payload for capability in capabilities), (
                        table, column
                    )
    finally:
        con.close()


def _host_rpc(port: int, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({
        "type": "client-request", "rpcId": uuid.uuid4().hex,
        "method": method, "payload": payload,
    }).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{method}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        envelope = json.loads(response.read())
    result = envelope["result"]
    assert result["ok"] is True, result
    return result["value"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _gateway_cookie(port: int, generation: str) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/?sc_generation={generation}"
    )
    with pytest.raises(urllib.error.HTTPError) as redirected:
        opener.open(request, timeout=5)
    response = redirected.value
    assert response.code == 302
    cookie = response.headers["Set-Cookie"]
    assert f"{deepseek_web.GENERATION_COOKIE}=" in cookie
    return cookie.split(";", 1)[0]


def _gateway_prompt(
    port: int, session_id: str, *, query_generation: str | None = None,
    cookie: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps({
        "type": "client-request", "rpcId": uuid.uuid4().hex,
        "method": "session.prompt",
        "payload": {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "text", "text": "gate prompt"}],
        },
    }).encode()
    suffix = f"?sc_generation={query_generation}" if query_generation else ""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/session.prompt{suffix}", data=body,
        headers={
            "Content-Type": "application/json",
            **({"Cookie": cookie} if cookie else {}),
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _identity(base: str, token: str, shell_id: int, shortname: str, host_port: int) -> dict[str, str]:
    return {
        "SC_API_BASE": base,
        "SC_API_TOKEN": token,
        "SC_SHELL_ID": str(shell_id),
        "SC_SHELL_SHORTNAME": shortname,
        "SC_DEEPSEEK_HOST_PORT": str(host_port),
    }


def _assert_sc_mem_which(env: dict[str, str], *, display_name: str) -> None:
    completed = subprocess.run(
        [str(ROOT / "sc"), "mem", "which"],
        cwd=ROOT,
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"engine API : {env['SC_API_BASE']}" in completed.stdout
    assert (
        f"shell      : {display_name} ({env['SC_SHELL_SHORTNAME']}) "
        f"#{env['SC_SHELL_ID']}"
    ) in completed.stdout
    assert "identity   : resolved by the engine" in completed.stdout


def _managed_context(
    worktree: Path, env: dict[str, str], *, conversation_id: str
) -> ConversationContext:
    """Build the immutable production route binding from the live stock Host."""
    client = deepseek_host.DeepSeekHostClient()
    route = deepseek_host.route_for(client, "deepseek-official/deepseek-v4-flash")
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "deepseek",
        "requested_model": route.selector,
        "provider_model": route.model,
        "requested_effort": "high",
        "effective_effort": "high",
        "native_variant_id": None,
        "transport": deepseek_host.TRANSPORT_CONTRACT,
        "catalogue_generation": "c" * 32,
        "evidence_digest": "d" * 64,
        "selector_binding": {
            "kind": "official-host-configured-model", "selector": route.selector,
        },
        "adapter_metadata": route.binding_metadata("high"),
    }
    route_bindings.validate_v2_binding(binding)
    return ConversationContext(
        worktree=worktree,
        provider=route.provider,
        model=route.selector,
        effort="high",
        route_binding=binding,
        binding_digest=route_bindings.digest_json(binding),
        conversation_id=conversation_id,
        env=env,
    )


def _free_gateway_port() -> int:
    """Reserve a test-local public port whose private DSH offset is free too."""
    for port in range(12_000, 50_000):
        candidates = (port, port + 1, port + 2, port + deepseek_web.ports.DEEPSEEK_RELAY_OFFSET)
        sockets: list[socket.socket] = []
        try:
            for candidate in candidates:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", candidate))
                sockets.append(listener)
            return port
        except OSError:
            continue
        finally:
            for listener in sockets:
                listener.close()
    raise AssertionError("no free DeepSeek Web public/private port pair")


def test_stock_two_shell_cross_surface_refusals_are_side_effect_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise real per-execution identity, Host/gateway and command boundaries."""
    dsh = shutil.which("dsh")
    if dsh is None:
        pytest.fail("pinned dsh 0.1.1-rc.2 is required for this production gate")
    assert subprocess.check_output([dsh, "--version"], text=True).strip() == "0.1.1-rc.2"

    database = tmp_path / "shell.db"
    sprint_id = _build_db(database)
    original_db = server.DB_PATH
    original_base, original_token = mem.SC_API_BASE, mem.SC_API_TOKEN
    server.DB_PATH = database
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    provider = _ControlledProvider()
    provider.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    mem.SC_API_BASE, mem.SC_API_TOKEN = base, ALICE_TOKEN

    root = tmp_path / "fork"
    alice_worktree = root / "worktrees" / "alice"
    bob_worktree = root / "worktrees" / "bob"
    alice_worktree.mkdir(parents=True)
    bob_worktree.mkdir(parents=True)
    body_file = tmp_path / "relay.txt"
    body_file.write_text("baseline relay")
    try:
        # Drive the real shell-facing clients first; the rows below give every
        # later negative assertion a non-empty count and exact content.
        assert mem.main(["state", "baseline state"]) == 0
        assert mem.main(["message", "send", "BOB", "baseline message"]) == 0
        with contextlib.redirect_stdout(io.StringIO()):
            assert sprint_cli.main([
                "send", "--sprint", str(sprint_id), "--to", "BOB",
                "--body-file", str(body_file), "--key", "cross-surface-baseline",
            ]) == 0
        baseline = _protected_snapshot(database)
        assert baseline["shell_messages"]
        assert baseline["sprints"] and baseline["sprint_participants"]
        assert baseline["wake_message"] and baseline["sprint_wake_outbox"]

        # Isolate only filesystem/port ownership.  The HTTP handler, whoami
        # authentication, stock dsh, spawned gateway and command clients stay
        # unmodified production code.
        monkeypatch.setattr(deepseek_web, "REPO_ROOT", root)
        monkeypatch.setattr(deepseek_web.ports, "REPO_ROOT", root)
        monkeypatch.setattr(deepseek_web.ports, "CONFIG", root / "instance.json")
        monkeypatch.setenv("SC_DEEPSEEK_WEB_STATE", str(root / "web-state.json"))
        monkeypatch.setenv("SC_DEEPSEEK_WEB_LOCK", str(root / "web.lock"))
        monkeypatch.setenv("SC_DEEPSEEK_WEB_LOG", str(root / "web.log"))
        monkeypatch.setenv("DSH_HOME", str(root / "dsh-home"))
        monkeypatch.delenv("SC_SANDBOX", raising=False)
        public_port = _free_gateway_port()
        (root / "instance.json").write_text(json.dumps({
            "port": public_port + 1,
            "dev_port": public_port + 2,
            "deepseek_host_port": public_port,
        }))
        config = deepseek_web.ports.resolve(persist=True)
        assert config["deepseek_host_port"] == public_port
        upstream_port = public_port + deepseek_web.ports.DEEPSEEK_RELAY_OFFSET
        alice = _identity(base, ALICE_TOKEN, 41, "ALICE", upstream_port)
        bob = _identity(base, BOB_TOKEN, 42, "BOB", upstream_port)
        provider_environment = {
            "DSH_HOME": str(root / "dsh-home"),
            "DEEPSEEK_BASE_URL": provider.url,
            "DEEPSEEK_API_KEY": PROVIDER_TOKEN,
            "DSH_TELEMETRY_MODE": "DISABLED",
            "SC_DEEPSEEK_WEB_STATE": str(root / "web-state.json"),
            "SC_DEEPSEEK_WEB_LOCK": str(root / "web.lock"),
            "SC_DEEPSEEK_WEB_LOG": str(root / "web.log"),
            # Launch preparation preserves the shell PATH.  Stock dsh is a
            # /usr/bin/env node script on CI, so the controlled identity must
            # retain that non-secret runtime dependency as well.
            "PATH": os.environ["PATH"],
        }
        alice.update(provider_environment)
        bob.update(provider_environment)

        # The clean-room identities are not test-only request headers.  Drive
        # the exact public command for each disposable shell so the receipt is
        # anchored to the same server-side identity resolution operators use.
        _assert_sc_mem_which(alice, display_name="Alice")
        _assert_sc_mem_which(bob, display_name="Bob")

        # Both canonical identities reuse one neutral Host without a global
        # shell lease. Each whoami call still hits the temporary API.
        try:
            first = deepseek_web.ensure(alice_worktree, env=alice)
        except deepseek_web.DeepSeekWebError as exc:
            log = (root / "web.log").read_text(errors="replace")[-4_000:]
            pytest.fail(f"stock dsh startup failed: {exc}\n{log}")
        reused = deepseek_web.ensure(alice_worktree, env=alice)
        assert reused["reused"] is True
        old_generation = first["url"].split("sc_generation=", 1)[1]
        handed = deepseek_web.ensure(bob_worktree, env=bob)
        generation = handed["url"].split("sc_generation=", 1)[1]
        assert handed["host_identity"] == "neutral"
        assert handed["reused"] is True
        assert generation == old_generation
        stale_generation = "0" * 64
        assert stale_generation != generation

        # The shell handoff did create exactly the selected Host workspace;
        # stale Web work below must not add a prompt to this real stock Host.
        workspaces = _host_rpc(upstream_port, "workspace.list", {})["items"]
        assert {row["path"] for row in workspaces} == {
            str(alice_worktree), str(bob_worktree)
        }
        bob_workspace = next(row for row in workspaces if row["path"] == str(bob_worktree))
        managed_id = f"sc-{uuid.uuid4().hex}"
        created = _host_rpc(upstream_port, "session.create", {
            "workspaceId": bob_workspace["workspaceId"], "sessionId": managed_id,
            "agentPreset": "standard",
        })
        assert created["sessionId"] == managed_id
        history_before = _host_rpc(upstream_port, "session.history", {"sessionId": managed_id})

        # A stale tab is rejected by the real generation boundary before the
        # upstream Host can observe a prompt.
        status, stale = _gateway_prompt(
            public_port, managed_id, query_generation=stale_generation
        )
        assert status == 409 and stale == {"error": "HARNESS_WEB_GENERATION_STALE"}
        assert _host_rpc(upstream_port, "session.history", {"sessionId": managed_id}) == history_before
        assert _protected_snapshot(database) == baseline

        # Current-generation native Web cannot become a second prompt owner of
        # the same managed session.  This is an actual gateway request, not a
        # fake relay handler, and the Host history remains exactly unchanged.
        cookie = _gateway_cookie(public_port, generation)
        deepseek_web.reserve_managed_session(managed_id)
        try:
            status, busy = _gateway_prompt(public_port, managed_id, cookie=cookie)
            assert status == 409 and busy == {"error": "HARNESS_WEB_SESSION_BUSY"}
        finally:
            deepseek_web.release_managed_session(managed_id)
        assert _host_rpc(upstream_port, "session.history", {"sessionId": managed_id}) == history_before
        assert _protected_snapshot(database) == baseline

        # A managed Browser/Sprint-shaped turn uses the real adapter and stock
        # Host, not a manually-created managed-looking chat.  Its immutable
        # binding, workspace membership, deterministic session ID, prompt,
        # stream terminal, and binding retirement all cross production boundaries.
        provider.requests.clear()
        for key, value in bob.items():
            monkeypatch.setenv(key, value)
        managed_adapter = DeepSeekAdapter()
        managed_context = _managed_context(
            bob_worktree, bob, conversation_id="cross-surface-managed-turn"
        )
        try:
            managed_turn = managed_adapter.start(managed_context, "managed gate prompt")
            managed_events = list(managed_adapter.stream(managed_turn))
        finally:
            managed_adapter.close()
        assert managed_events[-1].type == "run.completed"
        assert managed_turn.session_ref == DeepSeekAdapter._new_session_ref(managed_context)
        assert any("managed gate prompt" in json.dumps(request) for request in provider.requests)
        managed_workspace = next(
            row for row in _host_rpc(upstream_port, "workspace.list", {})["items"]
            if row["path"] == str(bob_worktree)
        )
        assert managed_turn.session_ref in managed_workspace["sessionIds"]

        # Recovery starts from the broker's persisted running shape after a
        # real Host process loss.  Its production recovery context rebuilds
        # Bob's authenticated identity; the broker's actual reconciliation
        # loop may read history only after the adapter repeats every binding.
        pre_restart = json.loads((root / "web-state.json").read_text())["web_pid"]
        deepseek_web.stop(env=bob)

        class RecoveryPreparer:
            def recovery(self, _run: BrokerRun) -> ConversationContext:
                return managed_context

        recovery_conversation = "cv_" + uuid.uuid4().hex
        con = sqlite3.connect(database)
        try:
            con.execute(
                "INSERT INTO conversations (conversation_id,shell_id,owner_user_id,"
                "harness,provider,model,effort,worktree,harness_session_ref,state,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,'running',?,?)",
                (
                    recovery_conversation, 42, 1, "deepseek",
                    managed_context.provider, managed_context.model,
                    managed_context.effort, str(bob_worktree),
                    managed_turn.session_ref, "cross-surface-recovery",
                    "cross-surface-recovery-hash",
                ),
            )
            recovery_message = int(con.execute(
                "INSERT INTO conversation_messages (conversation_id,sender_kind,"
                "sender_ref,message_kind,body,idempotency_key,request_hash,state) "
                "VALUES (?,'user','42','prompt','recovery never prompts',?,?, 'running')",
                (recovery_conversation, "cross-surface-recovery-message",
                 "cross-surface-recovery-message-hash"),
            ).lastrowid)
            recovery_run_id = int(con.execute(
                "INSERT INTO conversation_runs (conversation_id,shell_id,"
                "trigger_message_id,harness_session_before,harness_session_after,"
                "runner_ref,state,lease_owner,lease_expires_at,started_at) "
                "VALUES (?,?,?,?,?,?, 'running','lost-broker','1970-01-01 00:00:00',"
                "'1970-01-01 00:00:00')",
                (
                    recovery_conversation, 42, recovery_message,
                    managed_turn.session_ref, managed_turn.session_ref,
                    managed_turn.run_ref,
                ),
            ).lastrowid)
            con.commit()
        finally:
            con.close()
        broker = ConversationBroker(
            database,
            adapter_factory=lambda _harness: DeepSeekAdapter(),
            launch_preparer=RecoveryPreparer(),  # type: ignore[arg-type]
            recovery_seconds=0.01,
        )
        assert broker._recover(startup=True) == 1
        assert broker.wait_idle(timeout=10)
        con = sqlite3.connect(database)
        try:
            recovered = con.execute(
                "SELECT state,error_code FROM conversation_runs WHERE run_id=?",
                (recovery_run_id,),
            ).fetchone()
            recovery_events = con.execute(
                "SELECT event_type FROM conversation_events WHERE run_id=? ORDER BY sequence",
                (recovery_run_id,),
            ).fetchall()
        finally:
            con.close()
        assert recovered == ("succeeded", None)
        assert recovery_events == [("run.completed",)]
        post_restart = json.loads((root / "web-state.json").read_text())["web_pid"]
        assert post_restart != pre_restart
        assert _host_rpc(upstream_port, "session.history", {
            "sessionId": managed_turn.session_ref,
        })["events"]
        recovered_service = deepseek_web.ensure(bob_worktree, env=bob)
        generation = recovered_service["url"].split("sc_generation=", 1)[1]
        cookie = _gateway_cookie(public_port, generation)

        # Stock DSH must actually execute a native-Web prompt against the
        # controlled provider.  A distinct native session coexists with the
        # reservation above and produces a real Host history boundary.
        assert deepseek_web._read_activity(required=True) == {
            "admission": "open", "requests": {}
        }
        native_id = f"session-{uuid.uuid4()}"
        native_created = _host_rpc(upstream_port, "session.create", {
            "sessionId": native_id, "cwd": str(bob_worktree),
        })
        assert native_created["sessionId"] == native_id
        selected = _host_rpc(upstream_port, "session.selectModel", {
            "sessionId": native_id,
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "reasoningEffort": "high",
        })
        assert selected["selected"] == {
            "provider": "deepseek-official", "model": "deepseek-v4-flash",
            "reasoningEffort": "high",
        }
        provider.requests.clear()
        provider.entered.clear()
        provider.release.clear()
        provider.hold = True
        status, accepted = _gateway_prompt(public_port, native_id, cookie=cookie)
        assert status == 200, accepted
        assert accepted["result"]["ok"] is True
        assert provider.entered.wait(timeout=10)
        # The Host is identity-neutral, so a different shell may reuse it while
        # distinct native-Web work is live without rotating authority or the
        # gateway generation. The active-session mutation ledger still refuses
        # a second prompt owner for the same native session.
        concurrent_reuse = deepseek_web.ensure(alice_worktree, env=alice)
        assert concurrent_reuse["reused"] is True
        assert concurrent_reuse["url"].split("sc_generation=", 1)[1] == generation
        assert _gateway_prompt(public_port, native_id, cookie=cookie)[0] == 409
        provider.release.set()
        provider.hold = False
        deadline = threading.Event()
        for _ in range(100):
            history = _host_rpc(upstream_port, "session.history", {"sessionId": native_id})
            if any(event.get("event", {}).get("type") == "turn/end" for event in history["events"]):
                break
            deadline.wait(0.05)
        # Stock DSH makes one agent completion and one generated-title request;
        # both are bounded, local, and prove the prompt reached its unmodified
        # provider integration rather than a relay mock.
        assert len(provider.requests) == 2
        assert any("gate prompt" in json.dumps(request) for request in provider.requests)
        native_history = _host_rpc(upstream_port, "session.history", {"sessionId": native_id})
        assert any(
            event.get("event", {}).get("type") == "turn/end"
            for event in native_history["events"]
        )
        handed_after_web = deepseek_web.ensure(alice_worktree, env=alice)
        assert handed_after_web["host_identity"] == "neutral"
        assert handed_after_web["reused"] is True
        assert handed_after_web["url"].split("sc_generation=", 1)[1] == generation
        returned = deepseek_web.ensure(bob_worktree, env=bob)
        generation = returned["url"].split("sc_generation=", 1)[1]
        cookie = _gateway_cookie(public_port, generation)

        # A real public one-shot remains bound through a live provider stream
        # while the other canonical shell reuses the same neutral Host.
        for key, value in bob.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("SC_SHELL_WORKTREE", str(bob_worktree))
        provider.requests.clear()
        provider.entered.clear()
        provider.release.clear()
        provider.hold = True
        one_shot_result: dict[str, Any] = {}

        def capture_one_shot(result: dict[str, Any], prompt: str) -> None:
            try:
                result["status"] = deepseek_one_shot.run(
                    "deepseek-official/deepseek-v4-flash", "high", prompt
                )
            except deepseek_host.DeepSeekHostError as exc:
                result["error"] = exc

        one_shot = threading.Thread(
            target=capture_one_shot,
            args=(one_shot_result, "held one-shot"),
            daemon=True,
        )
        one_shot.start()
        assert provider.entered.wait(timeout=10), one_shot_result
        overlapping = deepseek_web.ensure(alice_worktree, env=alice)
        assert overlapping["reused"] is True
        assert overlapping["host_identity"] == "neutral"
        provider.release.set()
        one_shot.join(timeout=15)
        provider.hold = False
        assert not one_shot.is_alive()
        assert one_shot_result == {"status": 0}
        assert any("held one-shot" in json.dumps(request) for request in provider.requests)

        # Hold the real public entry at route resolution, exact-session
        # creation, prompt admission, terminal delivery, and after terminal
        # delivery while the provider stream is closing.  The observing
        # transport delegates to the stock Host; each pause is therefore a
        # production lifetime boundary, not a fabricated one-shot result.
        for phase in (
            "host.describe", "session.create", "session.prompt", "turn/end",
            "stream.close",
        ):
            provider.requests.clear()
            blocker = _PhaseBlockingHost(phase)
            phase_result: dict[str, Any] = {}
            with monkeypatch.context() as phase_patch:
                phase_patch.setattr(
                    deepseek_host,
                    "DeepSeekHostClient",
                    lambda blocker=blocker: blocker,
                )
                phase_thread = threading.Thread(
                    target=capture_one_shot,
                    args=(phase_result, f"one-shot {phase}"),
                    daemon=True,
                )
                phase_thread.start()
                assert blocker.entered.wait(timeout=10), phase
                overlapping = deepseek_web.ensure(alice_worktree, env=alice)
                assert overlapping["reused"] is True
                assert overlapping["host_identity"] == "neutral"
                blocker.release.set()
                phase_thread.join(timeout=15)
            assert not phase_thread.is_alive(), phase
            assert phase_result == {"status": 0}, phase_result
            assert any(f"one-shot {phase}" in json.dumps(request) for request in provider.requests)

        # Force a provider stream loss after the prompt reached stock DSH.
        # Bob's exact binding remains responsible for cancellation and terminal
        # proof while Alice continues to reuse the neutral Host independently.
        for phase in ("session.cancel", "session.history"):
            provider.requests.clear()
            blocker = _CancellationBlockingHost(phase)
            phase_result = {}
            with monkeypatch.context() as phase_patch:
                phase_patch.setattr(
                    deepseek_host,
                    "DeepSeekHostClient",
                    lambda blocker=blocker: blocker,
                )
                phase_thread = threading.Thread(
                    target=capture_one_shot,
                    args=(phase_result, f"one-shot {phase}"),
                    daemon=True,
                )
                phase_thread.start()
                assert blocker.entered.wait(timeout=10), phase
                overlapping = deepseek_web.ensure(alice_worktree, env=alice)
                assert overlapping["reused"] is True
                assert overlapping["host_identity"] == "neutral"
                blocker.release.set()
                phase_thread.join(timeout=15)
            assert not phase_thread.is_alive(), phase
            assert blocker.cancelled is True
            assert set(phase_result) == {"error"}
            error = phase_result["error"]
            assert isinstance(error, deepseek_host.DeepSeekHostError)
            assert error.code == "HARNESS_PROVIDER_STREAM_FAILED"
            prompts = [
                payload for method, payload in blocker.calls
                if method == "session.prompt"
            ]
            assert len(prompts) == 1
            assert prompts[0]["content"] == [{
                "type": "text", "text": f"one-shot {phase}",
            }]
            assert prompts[0]["sessionId"].startswith("sc-")

        # Recovery and public one-shot each use their production entry point.
        # A wrong authenticated durable identity fails before Host mutation.
        monkeypatch.setenv("SC_API_BASE", base)
        monkeypatch.setenv("SC_API_TOKEN", BOB_TOKEN)
        monkeypatch.setenv("SC_SHELL_ID", "999")
        monkeypatch.setenv("SC_SHELL_SHORTNAME", "BOB")
        monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", str(upstream_port))
        monkeypatch.setenv("SC_SHELL_WORKTREE", str(bob_worktree))
        with pytest.raises(deepseek_host.DeepSeekHostError, match="IDENTITY_MISMATCH"):
            deepseek_one_shot.run("missing-route", "default", "must not prompt")
        context = ConversationContext(
            worktree=bob_worktree, env={**bob, "SC_SHELL_ID": "999"}
        )
        with pytest.raises(AdapterError, match="IDENTITY_MISMATCH"):
            DeepSeekAdapter().inspect(managed_id, context)
        assert _protected_snapshot(database) == baseline

        # No global lease blocks another canonical one-shot. Model discovery
        # remains available and reports the exact missing route while the same
        # five protected stores stay untouched; a matching recovery check also
        # proves archived sessions never drive history inspection.
        monkeypatch.setenv("SC_SHELL_ID", "42")
        with pytest.raises(deepseek_host.DeepSeekHostError) as route_missing:
            deepseek_one_shot.run("missing-route", "default", "must not prompt")
        assert route_missing.value.code == "HARNESS_ROUTE_UNAVAILABLE"
        assert _protected_snapshot(database) == baseline
        # Stock DSH does not retain an archive marker for an empty session and
        # may retire an imported session during later Host handoffs.  Create a
        # current prompted managed session so the archive check exercises the
        # durable production boundary rather than an ephemeral probe.
        archive_context = _managed_context(
            bob_worktree,
            bob,
            conversation_id="cross-surface-archive-probe",
        )
        archive_source = DeepSeekAdapter()
        try:
            archive_turn = archive_source.start(
                archive_context,
                "managed archive probe",
            )
            archive_events = list(archive_source.stream(archive_turn))
        finally:
            archive_source.close()
        assert archive_events[-1].type == "run.completed"
        _host_rpc(
            upstream_port,
            "workspace.archiveSession",
            {"sessionId": archive_turn.session_ref},
        )
        archived = DeepSeekAdapter()
        try:
            with pytest.raises(AdapterError, match="SESSION_ARCHIVED"):
                archived.inspect(
                    archive_turn.session_ref,
                    archive_context,
                )
        finally:
            archived.close()
        missing = DeepSeekAdapter()
        try:
            with pytest.raises(AdapterError, match="SESSION_LOST"):
                missing.inspect(
                    f"sc-{uuid.uuid4().hex}",
                    ConversationContext(worktree=bob_worktree, env=bob),
                )
        finally:
            missing.close()
        other_worktree = root / "worktrees" / "other"
        other_worktree.mkdir()
        other_workspace = _host_rpc(upstream_port, "workspace.create", {
            "path": str(other_worktree),
        })["workspace"]
        foreign_id = f"sc-{uuid.uuid4().hex}"
        _host_rpc(upstream_port, "session.create", {
            "workspaceId": other_workspace["workspaceId"],
            "sessionId": foreign_id, "agentPreset": "standard",
        })
        mismatched = DeepSeekAdapter()
        try:
            with pytest.raises(AdapterError, match="SESSION_WORKSPACE_MISMATCH"):
                mismatched.inspect(
                    foreign_id, ConversationContext(worktree=bob_worktree, env=bob)
                )
        finally:
            mismatched.close()
        assert _protected_snapshot(database) == baseline

        # Reopen after a real stop: same durable shell reauthenticates and a
        # new generation is minted.  No secret reaches argv/state/log output.
        deepseek_web.stop(env=bob)
        reopened = deepseek_web.ensure(bob_worktree, env=bob)
        reopened_generation = reopened["url"].split("sc_generation=", 1)[1]
        assert reopened_generation != generation
        state = json.loads((root / "web-state.json").read_text())
        assert not (root / "deepseek-shell-api.json").exists()
        assert json.loads((root / "deepseek-web-generation.json").read_text())["generation"] == reopened_generation
        # The controlled provider key necessarily belongs to stock DSH's live
        # process environment.  Engine-owned shell credentials may exist only
        # in unique owner-only binding artifacts and must never cross into
        # either stock process or another durable surface.
        secrets = (ALICE_TOKEN, BOB_TOKEN)
        capabilities = (
            stale_generation, old_generation, generation, reopened_generation,
        )
        identity_registry = deepseek_web._identity_registry(bob)
        credential_paths = set(identity_registry.layout.credentials.glob("*.json"))
        assert credential_paths
        for credential_path in credential_paths:
            metadata = credential_path.lstat()
            assert stat.S_ISREG(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert not credential_path.is_symlink()
            credential = json.loads(credential_path.read_text())
            assert credential["contract"] == "sc-dsh-binding-credential-v1"
            assert credential["token"] in secrets
        owner_only = {
            root / "deepseek-web-generation.json",
            *credential_paths,
        }
        for pid_key in ("web_pid", "relay_pid"):
            command = "\0".join(deepseek_web._process_cmdline(state[pid_key]))
            assert not any(value in command for value in (*secrets, *capabilities))
            environment = Path(f"/proc/{state[pid_key]}/environ").read_bytes()
            assert not any(value.encode() in environment for value in (*secrets, *capabilities))
        for path in root.rglob("*"):
            if not path.is_file() or path in owner_only or path == database:
                continue
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            assert not any(value.encode() in payload for value in (*secrets, *capabilities)), path
        # The only durable token cells are their canonical shell records.  This
        # is a table-aware sweep, not an SQL dump shortcut: every other cell in
        # documents, metadata/events, transcripts, receipts, snapshots, and
        # future persisted tables is examined independently.
        con = sqlite3.connect(database)
        try:
            shell_keys = con.execute(
                "SELECT shell_id,api_key FROM shells ORDER BY shell_id"
            ).fetchall()
        finally:
            con.close()
        assert shell_keys == [(41, ALICE_TOKEN), (42, BOB_TOKEN)]
        _assert_database_secrets_absent(
            database,
            secrets=(ALICE_TOKEN, BOB_TOKEN, PROVIDER_TOKEN),
            capabilities=capabilities,
        )
        output = capsys.readouterr()
        assert not any(value in output.out + output.err for value in (*secrets, *capabilities))
    finally:
        # Stop whichever stock service is alive before the temporary API and
        # artifacts disappear; cleanup is deliberately independent of pass/fail.
        try:
            if "bob" in locals():
                deepseek_web.stop(env=bob)
        except Exception:
            pass
        httpd.shutdown()
        httpd.server_close()
        provider.close()
        server.DB_PATH = original_db
        mem.SC_API_BASE, mem.SC_API_TOKEN = original_base, original_token
