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
import server  # noqa: E402
import sprint_cli  # noqa: E402
from conversation_adapters.base import AdapterError, ConversationContext  # noqa: E402
from conversation_adapters.deepseek import DeepSeekAdapter  # noqa: E402


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
    """Exercise real identity, lease, Host/gateway and command boundaries."""
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
        }
        alice.update(provider_environment)
        bob.update(provider_environment)

        # Same identity reuse, an overlapping different identity refusal, then
        # a real handoff and restart.  Each whoami call hits the temporary API.
        alice_lease = deepseek_web.acquire_shell_identity(env=alice)
        first = deepseek_web.ensure(alice_worktree, env=alice, identity_lease=alice_lease)
        reused = deepseek_web.ensure(alice_worktree, env=alice, identity_lease=alice_lease)
        assert reused["reused"] is True
        old_generation = first["url"].split("sc_generation=", 1)[1]
        with pytest.raises(deepseek_web.DeepSeekWebError, match="IDENTITY_BUSY"):
            deepseek_web.ensure(bob_worktree, env=bob)
        alice_lease.close()
        handed = deepseek_web.ensure(bob_worktree, env=bob)
        generation = handed["url"].split("sc_generation=", 1)[1]
        assert handed["credential_shell"] == "BOB"
        assert handed["credential_shell_id"] == 42
        assert generation != old_generation

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
            public_port, managed_id, query_generation=old_generation
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
        status, accepted = _gateway_prompt(public_port, native_id, cookie=cookie)
        assert status == 200, accepted
        assert accepted["result"]["ok"] is True
        deadline = threading.Event()
        for _ in range(100):
            if provider.requests:
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

        # A real public one-shot remains the owner through a live provider
        # stream.  Arrival from the other canonical shell is refused until the
        # stock Host has produced its terminal event, rather than merely while
        # route/session setup is running.
        for key, value in bob.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("SC_SHELL_WORKTREE", str(bob_worktree))
        provider.requests.clear()
        provider.entered.clear()
        provider.release.clear()
        provider.hold = True
        one_shot_result: dict[str, Any] = {}

        def run_one_shot() -> None:
            try:
                one_shot_result["status"] = deepseek_one_shot.run(
                    "deepseek-official/deepseek-v4-flash", "high", "held one-shot"
                )
            except BaseException as exc:  # asserted below in the parent thread
                one_shot_result["error"] = exc

        one_shot = threading.Thread(target=run_one_shot, daemon=True)
        one_shot.start()
        assert provider.entered.wait(timeout=10), one_shot_result
        with pytest.raises(deepseek_web.DeepSeekWebError, match="IDENTITY_BUSY"):
            deepseek_web.ensure(alice_worktree, env=alice)
        provider.release.set()
        one_shot.join(timeout=15)
        provider.hold = False
        assert not one_shot.is_alive()
        assert one_shot_result == {"status": 0}
        assert any("held one-shot" in json.dumps(request) for request in provider.requests)

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

        # A true one-shot overlap holds the live identity lease.  It refuses
        # before route/session/prompt work and leaves the same five stores
        # untouched; a matching recovery check also proves archived sessions
        # never drive history inspection.
        monkeypatch.setenv("SC_SHELL_ID", "42")
        active = deepseek_web.acquire_shell_identity(env=bob)
        try:
            with pytest.raises(deepseek_host.DeepSeekHostError, match="IDENTITY_BUSY"):
                deepseek_one_shot.run("missing-route", "default", "must not prompt")
        finally:
            active.close()
        _host_rpc(upstream_port, "workspace.archiveSession", {"sessionId": managed_id})
        archived = DeepSeekAdapter()
        try:
            with pytest.raises(AdapterError, match="SESSION_ARCHIVED"):
                archived.inspect(managed_id, ConversationContext(worktree=bob_worktree, env=bob))
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
            with pytest.raises(AdapterError, match="SESSION_LOST"):
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
        owner_only = {
            root / "deepseek-shell-api.json", root / "deepseek-web-generation.json",
        }
        assert json.loads((root / "deepseek-shell-api.json").read_text())["token"] == BOB_TOKEN
        assert json.loads((root / "deepseek-web-generation.json").read_text())["generation"] == reopened_generation
        # The controlled provider key necessarily belongs to stock DSH's live
        # process environment.  The engine-owned shell credentials below must
        # never cross into either stock process or any durable surface.
        secrets = (ALICE_TOKEN, BOB_TOKEN)
        capabilities = (old_generation, generation, reopened_generation)
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
        # The only durable token rows are their canonical shell records.  The
        # complete SQL image covers documents, metadata/events, transcripts,
        # receipts, snapshots, and every other table without treating the
        # legitimate authenticated API-key rows as a leak.
        con = sqlite3.connect(database)
        try:
            shell_keys = con.execute(
                "SELECT shell_id,api_key FROM shells ORDER BY shell_id"
            ).fetchall()
            dump = "\n".join(con.iterdump())
        finally:
            con.close()
        assert shell_keys == [(41, ALICE_TOKEN), (42, BOB_TOKEN)]
        assert PROVIDER_TOKEN not in dump
        assert not any(value in dump for value in capabilities)
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
