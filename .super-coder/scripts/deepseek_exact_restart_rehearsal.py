#!/usr/bin/env python3
"""Credential-free exact-session restart rehearsal for an installed fork.

The source maintainer canary invokes this helper only inside its disposable
foreign checkout.  It fixtures one controlled loopback route in that
checkout's real engine database, drives turns through the running browser API
and broker, and projects only bounded identity evidence.  Provider request
bodies, prompts, transcripts, command arguments, and environment values never
leave the process.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence


ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api")]

import db_driver  # noqa: E402
import deepseek_runtime  # noqa: E402
import route_bindings  # noqa: E402
from conversation_adapters import ConversationContext, NativeTurn  # noqa: E402
from conversation_adapters.deepseek import (  # noqa: E402
    EXACT_RESTART_REHEARSAL_DISCOVERY,
    EXACT_RESTART_REHEARSAL_ENDPOINT,
    EXACT_RESTART_REHEARSAL_WIRE,
    DeepSeekAdapter,
)


CONVERSATION_ID = re.compile(r"^cv_[0-9a-f]{32}$")
HEX_SHA = re.compile(r"^[0-9a-f]{40}$")
REHEARSAL_KEY = "deepseek-exact-restart-rehearsal-v1"
SELECTOR = "ollama-cloud/deepseek-v4-pro:0813"
PROVIDER = "ollama-cloud"
PROVIDER_MODEL = "deepseek-v4-pro:0813"
FAILURE_CATEGORIES = {
    "broker-state-or-lease",
    "old-process-still-live",
    "persisted-root-mismatch",
    "unknown",
}


class RehearsalFailure(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _candidate() -> str:
    value = (REPO_ROOT / ".sc-state" / "engine.ref").read_text().strip()
    if HEX_SHA.fullmatch(value) is None:
        raise ValueError("candidate identity is missing")
    return value


def _binding(endpoint: str) -> dict[str, Any]:
    if endpoint != EXACT_RESTART_REHEARSAL_ENDPOINT:
        raise ValueError("loopback endpoint identity is invalid")
    manifest = deepseek_runtime.load_runtime_manifest()
    adapter = deepseek_runtime.provider_adapter(PROVIDER)
    registry = ENGINE / "assets" / "deepseek" / "provider-adapters.json"
    metadata = {
        "provider_route": PROVIDER,
        "provider_adapter_id": adapter["adapter_id"],
        "provider_adapter_digest": route_bindings.digest_json(adapter),
        "provider_registry_sha256": deepseek_runtime._sha256(registry),
        "credential_kind": adapter["credential_kind"],
        "endpoint_identity": endpoint,
        "discovery_evidence_digest": EXACT_RESTART_REHEARSAL_DISCOVERY,
        "transport_contract": route_bindings.DEEPSEEK_TRANSPORT_CONTRACT,
        "wire_evidence_digest": EXACT_RESTART_REHEARSAL_WIRE,
        "runtime_version": manifest["runtime"]["version"],
        "source_commit": manifest["source"]["commit"],
        "patch_sha256": manifest["patch"]["sha256"],
        "composition_sha256": adapter["composition_sha256"],
        "provider_options": {
            "omit": ["thinking", "reasoning_effort"],
            "set": {},
        },
    }
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "deepseek",
        "requested_model": SELECTOR,
        "provider_model": PROVIDER_MODEL,
        "requested_effort": "default",
        "effective_effort": "default",
        "native_variant_id": None,
        "transport": route_bindings.TRANSPORTS["deepseek"],
        "catalogue_generation": "e" * 32,
        "evidence_digest": None,
        "selector_binding": {
            "kind": "authenticated-provider-model",
            "selector": SELECTOR,
        },
        "adapter_metadata": metadata,
    }
    route_bindings.validate_v2_binding(binding)
    return binding


def _prepare(shortname: str, endpoint: str) -> dict[str, Any]:
    candidate = _candidate()
    binding = _binding(endpoint)
    con = db_driver.connect(DB_PATH)
    try:
        shell = con.execute(
            "SELECT shell_id,shortname,flavor FROM shells "
            "WHERE upper(shortname)=upper(?) AND COALESCE(is_deleted,0)=0",
            (shortname,),
        ).fetchone()
        if shell is None or shell["flavor"] == "admin":
            raise ValueError("rehearsal shell is unavailable")
        active = con.execute(
            "SELECT chat_id FROM active_shell_chats WHERE shell_id=?",
            (int(shell["shell_id"]),),
        ).fetchone()
        if active is not None:
            raise ValueError("rehearsal shell already owns an active chat")
        conversation_id = "cv_" + uuid.uuid4().hex
        worktree = REPO_ROOT / ".sc-worktrees" / str(shell["shortname"]).lower()
        binding_text = route_bindings.canonical_json(binding)
        binding_digest = route_bindings.digest_json(binding)
        with db_driver.write_transaction(con, "deepseek.restart_rehearsal.prepare"):
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
                "effort,worktree,title,creation_idempotency_key,"
                "creation_request_hash,route_contract_version,route_binding) "
                "VALUES (?,?,(SELECT user_id FROM shells WHERE shell_id=?),"
                "'deepseek',?,?, 'default',?,'Exact restart rehearsal',?,?,2,?)",
                (
                    conversation_id,
                    int(shell["shell_id"]),
                    int(shell["shell_id"]),
                    PROVIDER,
                    SELECTOR,
                    str(worktree),
                    REHEARSAL_KEY,
                    binding_digest,
                    binding_text,
                ),
            )
            con.execute(
                "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (?,?)",
                (int(shell["shell_id"]), conversation_id),
            )
    finally:
        con.close()
    return {
        "ok": True,
        "candidate_sha": candidate,
        "conversation_id": conversation_id,
        "provider": PROVIDER,
        "model": SELECTOR,
        "binding_digest": binding_digest,
    }


def _process_live(pid: Any, start_ticks: Any) -> bool:
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(start_ticks, int)
        or isinstance(start_ticks, bool)
    ):
        return False
    try:
        return deepseek_runtime.process_start_ticks(pid) == start_ticks
    except deepseek_runtime.DeepSeekRuntimeError:
        return False


def _probe(conversation_id: str, *, native: bool) -> dict[str, Any]:
    if CONVERSATION_ID.fullmatch(conversation_id) is None:
        raise ValueError("conversation identity is invalid")
    con = db_driver.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT c.conversation_id,c.harness,c.provider,c.model,c.effort,"
            "c.worktree,c.harness_session_ref,c.route_binding,"
            "boot.content,boot.content_sha256,"
            "r.run_id,r.runner_ref,r.state run_state,r.process_pid,"
            "r.process_start_ticks "
            "FROM conversations c "
            "LEFT JOIN conversation_boot_snapshots boot "
            "ON boot.conversation_id=c.conversation_id "
            "LEFT JOIN conversation_runs r ON r.run_id=("
            " SELECT latest.run_id FROM conversation_runs latest "
            " WHERE latest.conversation_id=c.conversation_id "
            " ORDER BY latest.run_id DESC LIMIT 1) "
            "WHERE c.conversation_id=?",
            (conversation_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError("conversation evidence is missing")
    binding = json.loads(str(row["route_binding"] or "null"))
    if not isinstance(binding, dict):
        raise ValueError("route binding is missing")
    route_bindings.validate_v2_binding(binding)
    metadata = binding["adapter_metadata"]
    session_ref = row["harness_session_ref"]
    layout = deepseek_runtime.conversation_layout(conversation_id)
    root_present = all(
        path.is_dir()
        for path in (layout.root, layout.home, layout.session_root, layout.diagnostics)
    )
    root_private = root_present and all(
        (path.stat().st_mode & 0o077) == 0
        for path in (layout.root, layout.home, layout.session_root, layout.diagnostics)
    )
    root_stat = layout.root.stat() if root_present else None
    evidence: dict[str, Any] = {
        "ok": True,
        "candidate_sha": _candidate(),
        "conversation_id": conversation_id,
        "native_session_id": session_ref if isinstance(session_ref, str) else None,
        "provider": row["provider"],
        "model": row["model"],
        "runtime_version": metadata.get("runtime_version"),
        "source_commit": metadata.get("source_commit"),
        "patch_sha256": metadata.get("patch_sha256"),
        "composition_sha256": metadata.get("composition_sha256"),
        "binding_digest": route_bindings.digest_json(binding),
        "boot_sha256": row["content_sha256"],
        "persisted_root_id": layout.conversation_key,
        "persisted_root_device": root_stat.st_dev if root_stat is not None else None,
        "persisted_root_inode": root_stat.st_ino if root_stat is not None else None,
        "persisted_root_present": root_present,
        "persisted_root_private": root_private,
        "run_id": row["run_id"],
        "run_state": row["run_state"],
        "process_pid": row["process_pid"],
        "process_start_ticks": row["process_start_ticks"],
        "process_live": _process_live(
            row["process_pid"], row["process_start_ticks"]
        ),
        "lease_clear": row["run_state"]
        not in {"leased", "starting", "running"},
        "inspect_session_exact": None,
        "inspect_presence": None,
        "inspect_state": None,
        "reconcile_outcome": None,
        "reconcile_proven": None,
    }
    if not native:
        return evidence
    if not isinstance(session_ref, str) or not session_ref:
        raise ValueError("native session identity is missing")
    boot = row["content"]
    if not isinstance(boot, str) or not boot:
        raise ValueError("boot snapshot is missing")
    context = ConversationContext(
        worktree=Path(str(row["worktree"])).resolve(),
        provider=str(row["provider"]),
        model=str(row["model"]),
        effort=str(row["effort"]),
        permission_mode="unrestricted",
        title="Exact restart rehearsal",
        env=dict(os.environ),
        route_binding=binding,
        binding_digest=route_bindings.digest_json(binding),
        conversation_id=conversation_id,
        boot_content=boot,
    )
    adapter = DeepSeekAdapter()
    try:
        inspected = adapter.inspect(session_ref, context)
        reconciled = adapter.reconcile(
            NativeTurn(
                harness="deepseek",
                session_ref=session_ref,
                run_ref=str(row["runner_ref"] or "missing-run-ref"),
                worktree=context.worktree,
                metadata={"from_event_seq": 0},
            ),
            context,
        )
    finally:
        adapter.close()
    evidence.update(
        {
            "inspect_session_exact": inspected.session_ref == session_ref,
            "inspect_presence": inspected.exists,
            "inspect_state": inspected.state,
            "reconcile_outcome": reconciled.outcome,
            "reconcile_proven": reconciled.proven,
        }
    )
    return evidence


def _cleanup(conversation_id: str) -> dict[str, Any]:
    if CONVERSATION_ID.fullmatch(conversation_id) is None:
        raise ValueError("conversation identity is invalid")
    con = db_driver.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT creation_idempotency_key FROM conversations "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return {"ok": True, "conversation_removed": True, "root_removed": True}
        if row["creation_idempotency_key"] != REHEARSAL_KEY:
            raise ValueError("conversation is not rehearsal-owned")
        latest = con.execute(
            "SELECT state,process_pid,process_start_ticks FROM conversation_runs "
            "WHERE conversation_id=? ORDER BY run_id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if latest is not None and (
            latest["state"] in {"leased", "starting", "running"}
            or _process_live(latest["process_pid"], latest["process_start_ticks"])
        ):
            raise RehearsalFailure("old-process-still-live")
        try:
            with db_driver.write_transaction(con, "deepseek.restart_rehearsal.cleanup"):
                con.execute(
                    "DELETE FROM active_shell_chats WHERE chat_id=?", (conversation_id,)
                )
                con.execute(
                    "DELETE FROM sprint_wake_attempts WHERE target_conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM sprint_participant_conversations "
                    "WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversation_events WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversation_outbox WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversation_runs WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversation_messages WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversation_git_targets WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversation_boot_snapshots WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "DELETE FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                )
        except sqlite3.Error as exc:
            raise RehearsalFailure("broker-state-or-lease") from exc
    finally:
        con.close()
    layout = deepseek_runtime.conversation_layout(conversation_id)
    try:
        if layout.root.exists():
            shutil.rmtree(layout.root)
    except OSError as exc:
        raise RehearsalFailure("persisted-root-mismatch") from exc
    return {
        "ok": True,
        "conversation_removed": True,
        "root_removed": not layout.root.exists(),
    }


class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._send_json(200, {"ok": True})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("request bound")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or payload.get("model") != PROVIDER_MODEL:
                raise ValueError("model identity")
        except (OSError, ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": {"code": "INVALID_REQUEST"}})
            return
        chunks = (
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "restart-rehearsal-ok"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )
        body = "".join(
            "data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"
            for chunk in chunks
        ) + "data: [DONE]\n\n"
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _serve_provider(port: int) -> int:
    if not 1024 <= port <= 65535:
        raise ValueError("provider port is outside the bounded range")
    server = ThreadingHTTPServer(("127.0.0.1", port), _ProviderHandler)
    with contextlib.closing(server):
        server.serve_forever()
    return 0


def _probe_provider(port: int) -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
        payload = json.loads(response.read())
    return {"ok": response.status == 200 and payload == {"ok": True}}


def _failure_category(exc: BaseException) -> str:
    category = getattr(exc, "category", None)
    if category in FAILURE_CATEGORIES:
        return str(category)
    code = str(getattr(exc, "code", ""))
    if code in {"HARNESS_SESSION_LOST", "HARNESS_SESSION_IDENTITY_INVALID"}:
        return "session-reference-missing"
    if code in {"HARNESS_SESSION_MISMATCH", "HARNESS_SESSION_IDENTITY_MISMATCH"}:
        return "session-reference-mismatch"
    if code in {"HARNESS_WORKTREE_MISMATCH", "HARNESS_BOOT_SNAPSHOT_MISMATCH"}:
        return "boot-or-runtime-drift"
    if code in {"HARNESS_PROCESS_ALREADY_RUNNING", "HARNESS_PROCESS_IDENTITY_MISMATCH"}:
        return "old-process-still-live"
    if code.startswith("HARNESS_ROUTE") or code.startswith("HARNESS_PROVIDER"):
        return "route-drift"
    return "unknown"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--shortname", required=True)
    prepare.add_argument("--endpoint", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--conversation", required=True)
    probe.add_argument("--native", action="store_true")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--conversation", required=True)
    provider = commands.add_parser("provider")
    provider.add_argument("--port", required=True, type=int)
    provider_probe = commands.add_parser("provider-probe")
    provider_probe.add_argument("--port", required=True, type=int)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "provider":
            return _serve_provider(args.port)
        if args.command == "provider-probe":
            payload = _probe_provider(args.port)
        elif args.command == "prepare":
            payload = _prepare(args.shortname, args.endpoint)
        elif args.command == "probe":
            payload = _probe(args.conversation, native=args.native)
        else:
            payload = _cleanup(args.conversation)
    except BaseException as exc:  # bounded executable surface
        print(
            json.dumps(
                {"ok": False, "category": _failure_category(exc)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
