#!/usr/bin/env python3
"""Managed DeepSeek Browser adapter over the stock loopback Host API."""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Callable, Iterator, Mapping

import deepseek_host
import deepseek_web
import harness_versions
import route_transport

from .base import (
    AdapterError,
    ConversationAdapter,
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ProbeResult,
    ReconcileResult,
    SessionInspection,
    ensure_nonempty_message,
    load_manifest,
)


SESSION_REF = re.compile(r"^sc-[0-9a-f]{32}$")
RUN_REF_PREFIX = "deepseek-host-run-v1:"
MAX_UNKNOWN_EVENTS = 24
MANAGED_IDENTITY_WAIT_SECONDS = 5400.0
SENSITIVE_KEY = re.compile(
    r"(?:key|token|secret|password|credential|authorization)", re.I
)


# Compatibility exports for imports of the former carrier protocol.
DeepSeekTransport = deepseek_host.HostTransport
DeepSeekHostClient = deepseek_host.DeepSeekHostClient


def _adapter_error(exc: deepseek_host.DeepSeekHostError) -> AdapterError:
    return AdapterError(
        exc.code,
        exc.detail,
        retryable=exc.code in {
            "HARNESS_HOST_UNAVAILABLE",
            "HARNESS_HOST_STREAM_LOST",
            "HARNESS_HOST_STREAM_TIMEOUT",
        },
    )


def _bounded_native(value: Any) -> Any:
    def redact(item: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[TRUNCATED]"
        if isinstance(item, Mapping):
            return {
                str(key): (
                    "[REDACTED]"
                    if SENSITIVE_KEY.search(str(key))
                    else redact(child, depth + 1)
                )
                for key, child in list(item.items())[:128]
            }
        if isinstance(item, list):
            return [redact(child, depth + 1) for child in item[:128]]
        if isinstance(item, str):
            return deepseek_host._redact_text(item, limit=2_048)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return str(type(item).__name__)

    return redact(value)


def _run_ref(boundary: int) -> str:
    payload = json.dumps(
        {"from_seq": boundary, "nonce": uuid.uuid4().hex},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return RUN_REF_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _run_boundary(run_ref: str) -> int:
    if not isinstance(run_ref, str) or not run_ref.startswith(RUN_REF_PREFIX):
        raise AdapterError(
            "HARNESS_RUN_REF_INVALID", "DeepSeek run reference is malformed"
        )
    encoded = run_ref[len(RUN_REF_PREFIX):]
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "HARNESS_RUN_REF_INVALID", "DeepSeek run reference is malformed"
        ) from exc
    boundary = payload.get("from_seq") if isinstance(payload, dict) else None
    if not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0:
        raise AdapterError(
            "HARNESS_RUN_REF_INVALID", "DeepSeek run reference boundary is invalid"
        )
    return boundary


class DeepSeekAdapter(ConversationAdapter):
    harness = "deepseek"

    def __init__(
        self,
        manifest: Mapping[str, Any] | None = None,
        *,
        client_factory: Callable[[], deepseek_host.HostTransport] = (
            deepseek_host.DeepSeekHostClient
        ),
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.client_factory = client_factory
        self._shell_lease: deepseek_web.ShellIdentityLease | None = None
        self._reserved_session: str | None = None
        self._proof_authority: Mapping[str, Any] | None = None
        self._proof_context: ConversationContext | None = None
        self._proof_session_id: str | None = None
        self._proof_quiesced = True

    def _client(self) -> deepseek_host.HostTransport:
        try:
            return self.client_factory()
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc

    def _managed_client(
        self,
        context: ConversationContext,
        root_session_id: str,
        *,
        recovery: bool = False,
        bind_identity: bool = True,
    ) -> deepseek_host.HostTransport:
        # Every production managed turn is canonically prepared. A missing
        # immutable shell identity is a refusal, never a test/probe fallback.
        env = context.env
        wiring = (
            env.get("SC_API_TOKEN"),
            env.get("SC_API_BASE"),
            env.get("SC_SHELL_ID"),
            env.get("SC_SHELL_SHORTNAME"),
        )
        if not all(wiring):
            raise AdapterError(
                "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
                "DeepSeek conversation preparation omitted canonical shell identity",
            )
        try:
            self._shell_lease = deepseek_web.acquire_shell_identity(
                env=env,
                wait_seconds=MANAGED_IDENTITY_WAIT_SECONDS,
            )
            deepseek_web.ensure(
                context.checked_worktree(),
                env=env,
                identity_lease=self._shell_lease,
                register_workspace=not recovery,
            )
            if bind_identity:
                self._bind_execution_identity(context, root_session_id)
        except deepseek_web.DeepSeekWebError as exc:
            self.close()
            raise AdapterError(exc.code, exc.detail) from exc
        return self._client()

    def _bind_execution_identity(
        self, context: ConversationContext, root_session_id: str
    ) -> None:
        env = context.env
        proof_authority = deepseek_web.preflight_candidate_execution(
            env=env,
            root_session_id=root_session_id,
            conversation_id=self._conversation_id(context),
            lifecycle_epoch=context.lifecycle_epoch,
            worktree=context.checked_worktree(),
        )
        deepseek_web.bind_session_identity(
            env=env,
            root_session_id=root_session_id,
            conversation_id=self._conversation_id(context),
            lifecycle_epoch=context.lifecycle_epoch,
            worktree=context.checked_worktree(),
            candidate_preflight=proof_authority,
        )
        same_proof_root = (
            proof_authority is not None
            and self._proof_context is not None
            and self._proof_session_id == root_session_id
        )
        self._proof_authority = proof_authority
        if self._proof_authority is not None:
            self._proof_context = context
            self._proof_session_id = root_session_id
            if not same_proof_root:
                self._proof_quiesced = (
                    proof_authority.get("binding_record_generation") is None
                )
        if self._proof_authority is not None and self._shell_lease is not None:
            self._shell_lease.close()
            self._shell_lease = None

    def _revalidate_proof_authority(
        self, context: ConversationContext, root_session_id: str
    ) -> None:
        if self._proof_authority is None:
            return
        try:
            admitted = deepseek_web.admit_candidate_execution(
                env=context.env,
                root_session_id=root_session_id,
                conversation_id=self._conversation_id(context),
                lifecycle_epoch=context.lifecycle_epoch,
            )
        except deepseek_web.DeepSeekWebError as exc:
            raise AdapterError(exc.code, exc.detail) from exc
        expected = {
            key: self._proof_authority.get(key)
            for key in (
                "mode",
                "generation",
                "proof_run_id",
                "root_session_id",
                "plugin_contract_generation",
            )
        }
        if admitted != expected:
            raise AdapterError(
                "HARNESS_PROOF_CAPABILITY_STALE",
                "proof capability changed before the managed effect",
            )

    def _require_recovery_target(
        self,
        client: deepseek_host.HostTransport,
        session_ref: str,
        context: ConversationContext,
    ) -> None:
        """Prove the old exact session before recovery reads its history."""
        worktree = str(context.checked_worktree())
        try:
            value = client.call("workspace.list", {})
            rows = self._workspace_rows(value)
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        workspace = next((row for row in rows if row.get("path") == worktree), None)
        workspace_id = (
            workspace.get("workspaceId") if isinstance(workspace, Mapping) else None
        )
        if not isinstance(workspace_id, str) or not workspace_id:
            raise AdapterError(
                "HARNESS_WORKSPACE_BINDING_FAILED",
                "DeepSeek recovery cannot find the canonical managed workspace",
            )
        archived = value.get("archivedSessionIds") if isinstance(value, Mapping) else None
        if not isinstance(archived, list):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "DeepSeek Host returned invalid workspace archive state",
            )
        # Resolve the exact global row before classifying missing workspace
        # membership.  A foreign workspace is identity corruption, not a lost
        # history, and recovery must surface that distinction before history.
        self._require_resume_target(client, session_ref, worktree, workspace_id)
        active = workspace.get("sessionIds") if isinstance(workspace, Mapping) else None
        if not isinstance(active, list) or session_ref not in active:
            raise AdapterError(
                "HARNESS_SESSION_WORKSPACE_MISMATCH",
                "DeepSeek recovery session is not actively accounted under its workspace",
            )

    def _managed_session(self) -> tuple[str, str]:
        configured = self.manifest.get("conversation", {}).get("managed_session")
        if not isinstance(configured, Mapping):
            raise AdapterError(
                "HARNESS_MANIFEST_INVALID",
                "DeepSeek managed-session policy is missing",
            )
        agent_preset = configured.get("agent_preset")
        permission_preset = configured.get("permission_preset")
        if not isinstance(agent_preset, str) or not agent_preset:
            raise AdapterError(
                "HARNESS_MANIFEST_INVALID",
                "DeepSeek managed agent preset is missing",
            )
        if not isinstance(permission_preset, str) or not permission_preset:
            raise AdapterError(
                "HARNESS_MANIFEST_INVALID",
                "DeepSeek managed permission preset is missing",
            )
        return agent_preset, permission_preset

    def _prepare_managed_session(
        self,
        client: deepseek_host.HostTransport,
        session_ref: str,
        context: ConversationContext,
        *,
        resume: bool = False,
    ) -> str:
        agent_preset, permission_preset = self._managed_session()
        worktree = str(context.checked_worktree())
        if not resume:
            try:
                client.call(
                    "settings.update",
                    {
                        "ns": "permission",
                        "patch": {"defaultPreset": permission_preset},
                    },
                )
            except deepseek_host.DeepSeekHostError as exc:
                raise AdapterError(
                    "HARNESS_PERMISSION_POLICY_UNAVAILABLE",
                    exc.detail,
                ) from exc
        workspace_id = self._workspace_id(client, worktree)
        if resume:
            self._require_resume_target(client, session_ref, worktree, workspace_id)
        payload = {
            "workspaceId": workspace_id,
            "sessionId": session_ref,
            "agentPreset": agent_preset,
        }
        try:
            created = client.call("session.create", payload)
        except deepseek_host.HostRpcError as exc:
            # The stock Host can publish the exact session before reporting an
            # attach failure.  Retrying the same identity repairs that narrow
            # partial success; a new ID would silently replace history.
            if exc.code == "HARNESS_HOST_RPC_WORKSPACE_ATTACH_FAILED":
                try:
                    created = client.call("session.create", payload)
                except deepseek_host.DeepSeekHostError as retry_exc:
                    raise AdapterError(
                        "HARNESS_SESSION_WORKSPACE_MISMATCH", retry_exc.detail
                    ) from retry_exc
            elif resume:
                raise AdapterError("HARNESS_SESSION_LOST", exc.detail) from exc
            else:
                raise _adapter_error(exc) from exc
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        if (
            not isinstance(created, Mapping)
            or created.get("sessionId") != session_ref
            or created.get("agentPreset") != agent_preset
        ):
            raise AdapterError(
                "HARNESS_SESSION_MISMATCH",
                "DeepSeek Host did not preserve the managed session preset",
            )
        self._confirm_workspace_session(
            client, session_ref, worktree, workspace_id
        )
        if not resume:
            try:
                history = self._history(client, session_ref)
            except deepseek_host.DeepSeekHostError as exc:
                raise AdapterError(
                    "HARNESS_PERMISSION_POLICY_UNAVAILABLE",
                    exc.detail,
                ) from exc
            if not any(
                event.get("type") == "permission/preset"
                and event.get("data") == {"preset": permission_preset}
                for event in history
            ):
                raise AdapterError(
                    "HARNESS_PERMISSION_POLICY_UNAVAILABLE",
                    "DeepSeek Host did not pin the unattended permission preset",
                )
        return workspace_id

    @staticmethod
    def _workspace_id(
        client: deepseek_host.HostTransport, worktree: str
    ) -> str:
        try:
            value = client.call("workspace.create", {"path": worktree})
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        workspace = value.get("workspace") if isinstance(value, Mapping) else None
        workspace_id = (
            workspace.get("workspaceId") if isinstance(workspace, Mapping) else None
        )
        returned_path = workspace.get("path") if isinstance(workspace, Mapping) else None
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or not isinstance(returned_path, str)
            or returned_path != worktree
        ):
            raise AdapterError(
                "HARNESS_WORKSPACE_BINDING_FAILED",
                "DeepSeek Host did not resolve the canonical managed workspace",
            )
        return workspace_id

    @staticmethod
    def _workspace_rows(value: Any) -> list[Mapping[str, Any]]:
        rows = value.get("items") if isinstance(value, Mapping) else None
        if not isinstance(rows, list):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host returned invalid workspace list"
            )
        return [row for row in rows if isinstance(row, Mapping)]

    @staticmethod
    def _session_rows(value: Any) -> list[Mapping[str, Any]]:
        rows = value.get("items") if isinstance(value, Mapping) else None
        if not isinstance(rows, list):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host returned invalid session list"
            )
        return [row for row in rows if isinstance(row, Mapping)]

    def _workspace_snapshot(
        self,
        client: deepseek_host.HostTransport,
        workspace_id: str,
    ) -> tuple[Mapping[str, Any], list[Any]]:
        try:
            value = client.call("workspace.list", {})
            rows = self._workspace_rows(value)
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        archived = value.get("archivedSessionIds") if isinstance(value, Mapping) else None
        if not isinstance(archived, list):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "DeepSeek Host returned invalid workspace archive state",
            )
        row = next((item for item in rows if item.get("workspaceId") == workspace_id), None)
        if row is None:
            raise AdapterError(
                "HARNESS_WORKSPACE_BINDING_FAILED",
                "DeepSeek Host did not list the resolved managed workspace",
            )
        return row, archived

    def _require_resume_target(
        self,
        client: deepseek_host.HostTransport,
        session_ref: str,
        worktree: str,
        workspace_id: str,
    ) -> None:
        try:
            rows = self._session_rows(client.call("session.list", {}))
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        row = next((item for item in rows if item.get("sessionId") == session_ref), None)
        if row is None:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                "DeepSeek managed session is absent; refusing to create a replacement",
            )
        if row.get("cwd") != worktree:
            raise AdapterError(
                "HARNESS_SESSION_WORKSPACE_MISMATCH",
                "DeepSeek managed session belongs to another worktree",
            )
        _workspace, archived = self._workspace_snapshot(client, workspace_id)
        if session_ref in archived:
            raise AdapterError(
                "HARNESS_SESSION_ARCHIVED",
                "DeepSeek managed session is archived and cannot be resumed",
            )

    def _confirm_workspace_session(
        self,
        client: deepseek_host.HostTransport,
        session_ref: str,
        worktree: str,
        workspace_id: str,
    ) -> None:
        workspace, archived = self._workspace_snapshot(client, workspace_id)
        if workspace.get("path") != worktree:
            raise AdapterError(
                "HARNESS_WORKSPACE_BINDING_FAILED",
                "DeepSeek Host workspace path changed during managed setup",
            )
        sessions = workspace.get("sessionIds")
        if not isinstance(sessions, list) or session_ref not in sessions:
            raise AdapterError(
                "HARNESS_SESSION_WORKSPACE_MISMATCH",
                "DeepSeek managed session is not accounted under its workspace",
            )
        if session_ref in archived:
            raise AdapterError(
                "HARNESS_SESSION_ARCHIVED",
                "DeepSeek managed session is archived and cannot be prompted",
            )
        try:
            rows = self._session_rows(client.call("session.list", {}))
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        row = next((item for item in rows if item.get("sessionId") == session_ref), None)
        if row is None or row.get("cwd") != worktree:
            raise AdapterError(
                "HARNESS_SESSION_WORKSPACE_MISMATCH",
                "DeepSeek Host session list disagrees with the managed workspace",
            )

    def probe(self) -> ProbeResult:
        try:
            client = self._client()
            described = client.call("host.describe", {})
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        host_version = (
            described.get("version") if isinstance(described, Mapping) else None
        )
        if not isinstance(host_version, str) or not host_version:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "DeepSeek Host descriptor version is invalid",
            )
        conversation = self.manifest["conversation"]
        version = harness_versions.probe(self.harness)
        if version is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "DeepSeek CLI version is unavailable",
            )
        if version != conversation["verified_cli_version"]:
            raise AdapterError(
                "HARNESS_VERSION_UNSUPPORTED",
                "DeepSeek CLI version does not match the pinned official runtime",
            )
        try:
            roster = client.call("agentPreset.list", {})
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        agent_preset, _permission_preset = self._managed_session()
        presets = roster.get("presets") if isinstance(roster, Mapping) else None
        selected = next(
            (
                row for row in presets
                if isinstance(row, Mapping) and row.get("id") == agent_preset
            ),
            None,
        ) if isinstance(presets, list) else None
        if (
            selected is None
            or selected.get("trust") != "system"
            or selected.get("broken") is not None
        ):
            raise AdapterError(
                "HARNESS_AGENT_PRESET_UNAVAILABLE",
                "DeepSeek shipped managed agent preset is unavailable",
            )
        return ProbeResult(
            harness=self.harness,
            version=version,
            minimum_version=conversation["minimum_cli_version"],
            capabilities=self.capabilities,
            maximum_version_exclusive=conversation["maximum_cli_version_exclusive"],
            verified_version=conversation["verified_cli_version"],
            compatibility="verified",
        )

    @staticmethod
    def _conversation_id(context: ConversationContext) -> str:
        value = context.conversation_id
        if not isinstance(value, str) or not value:
            raise AdapterError(
                "HARNESS_IDENTITY_MISSING",
                "DeepSeek adapter requires the stable conversation identity",
            )
        return value

    @classmethod
    def _new_session_ref(cls, context: ConversationContext) -> str:
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL, cls._conversation_id(context)
        ).hex
        return f"sc-{identity}"

    @staticmethod
    def _session_ref(value: str) -> str:
        if not isinstance(value, str) or SESSION_REF.fullmatch(value) is None:
            raise AdapterError(
                "HARNESS_SESSION_MISMATCH", "DeepSeek session reference is malformed"
            )
        return value

    @staticmethod
    def _history(
        client: deepseek_host.HostTransport, session_ref: str
    ) -> list[dict]:
        value = client.call(
            "session.history", {"sessionId": session_ref, "maxMessages": 200}
        )
        entries = value.get("events") if isinstance(value, Mapping) else None
        if not isinstance(entries, list):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host returned invalid history"
            )
        events = []
        for entry in entries:
            event = entry.get("event") if isinstance(entry, Mapping) else None
            if not isinstance(event, dict):
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR", "DeepSeek Host history row is invalid"
                )
            events.append(event)
        return events

    @classmethod
    def _boundary(
        cls, client: deepseek_host.HostTransport, session_ref: str
    ) -> int:
        events = cls._history(client, session_ref)
        seqs = [
            event.get("seq")
            for event in events
            if isinstance(event.get("seq"), int)
            and not isinstance(event.get("seq"), bool)
        ]
        return max(seqs, default=-1) + 1

    @staticmethod
    def _route(
        client: deepseek_host.HostTransport,
        context: ConversationContext,
    ) -> deepseek_host.ConfiguredRoute:
        try:
            projection = route_transport.context_projection(context, "deepseek")
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(exc.code, exc.message) from exc
        if projection is None or not projection.model:
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek requires one immutable exact route"
            )
        binding = context.route_binding
        if not isinstance(binding, Mapping):
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek route binding is missing"
            )
        metadata = binding.get("adapter_metadata")
        if not isinstance(metadata, Mapping):
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek route metadata is missing"
            )
        try:
            route = deepseek_host.route_for(client, projection.model)
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        expected = route.binding_metadata(binding["requested_effort"])
        if dict(metadata) != expected:
            raise AdapterError(
                "HARNESS_ROUTE_STALE",
                "DeepSeek official configuration changed after exact route binding",
            )
        if context.provider is not None and context.provider != route.provider:
            raise AdapterError(
                "HARNESS_ROUTE_INVALID",
                "stored provider disagrees with the immutable DeepSeek route",
            )
        return route

    @staticmethod
    def _select(
        client: deepseek_host.HostTransport,
        session_ref: str,
        route: deepseek_host.ConfiguredRoute,
        effort: str,
    ) -> None:
        payload = {
            "sessionId": session_ref,
            "provider": route.provider,
            "model": route.model,
            **({} if effort == "default" else {"reasoningEffort": effort}),
        }
        selected = client.call("session.selectModel", payload)
        expected = {
            "provider": route.provider,
            "model": route.model,
            **({} if effort == "default" else {"reasoningEffort": effort}),
        }
        if not isinstance(selected, Mapping) or selected.get("selected") != expected:
            raise AdapterError(
                "HARNESS_ROUTE_MISMATCH",
                "DeepSeek Host did not select the exact bound provider/model route",
            )

    def _turn(
        self,
        client: deepseek_host.HostTransport,
        session_ref: str,
        context: ConversationContext,
        message: str,
        *,
        resumed: bool,
        route: deepseek_host.ConfiguredRoute | None = None,
        workspace_id: str,
    ) -> NativeTurn:
        route = route or self._route(client, context)
        boundary = self._boundary(client, session_ref)
        binding = context.route_binding
        effort = (
            binding.get("requested_effort")
            if isinstance(binding, Mapping)
            else None
        )
        self._select(client, session_ref, route, effort or "default")
        stream = client.open_events()
        try:
            # Native Web shares the Host and may change workspace/archive state.
            # Recheck the two authoritative baselines at the final admission
            # boundary after route/session lifecycle work and stream setup.
            self._confirm_workspace_session(
                client,
                session_ref,
                str(context.checked_worktree()),
                workspace_id,
            )
            if self._proof_authority is not None:
                self._proof_quiesced = False
                self._revalidate_proof_authority(context, session_ref)
            accepted = client.call(
                "session.prompt",
                {
                    "sessionId": session_ref,
                    "mode": "queue",
                    "content": [{"type": "text", "text": message}],
                },
            )
        except Exception:
            stream.close()
            raise
        if not isinstance(accepted, Mapping) or accepted.get("accepted") is not True:
            if self._proof_authority is not None:
                self._proof_quiesced = True
            stream.close()
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host did not accept the prompt"
            )
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=_run_ref(boundary),
            worktree=context.checked_worktree(),
            metadata={
                "from_event_seq": boundary,
                "seen_event_seq": set(),
                "resumed": resumed,
                "context": context,
                "route": route,
                "client": client,
                "stream": stream,
                "proof_authority": self._proof_authority,
            },
            opaque=stream,
        )

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        message = ensure_nonempty_message(message)
        try:
            session_ref = self._new_session_ref(context)
            client = self._managed_client(context, session_ref)
            route = self._route(client, context)
            self._reserve(session_ref)
            workspace_id = self._prepare_managed_session(
                client, session_ref, context
            )
            return self._turn(
                client,
                session_ref,
                context,
                message,
                resumed=False,
                route=route,
                workspace_id=workspace_id,
            )
        except Exception:
            self.close()
            raise

    def _reserve(self, session_ref: str) -> None:
        try:
            deepseek_web.reserve_managed_session(session_ref)
            self._reserved_session = session_ref
        except deepseek_web.DeepSeekWebError as exc:
            raise AdapterError(exc.code, exc.detail) from exc

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        session_ref = self._session_ref(session_ref)
        message = ensure_nonempty_message(message)
        try:
            client = self._managed_client(
                context, session_ref, bind_identity=False
            )
            self._reserve(session_ref)
            route = self._route(client, context)
            workspace_id = self._prepare_managed_session(
                client, session_ref, context, resume=True
            )
            self._bind_execution_identity(context, session_ref)
            return self._turn(
                client,
                session_ref,
                context,
                message,
                resumed=True,
                route=route,
                workspace_id=workspace_id,
            )
        except Exception:
            self.close()
            raise

    @staticmethod
    def _usage(raw: Mapping[str, Any]) -> dict[str, int | float]:
        return {
            target: raw[source]
            for source, target in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cacheReadTokens", "cache_read_tokens"),
                ("cacheWriteTokens", "cache_write_tokens"),
                ("reasoningTokens", "reasoning_tokens"),
            )
            if isinstance(raw.get(source), (int, float))
            and not isinstance(raw.get(source), bool)
        }

    @staticmethod
    def _terminal(
        turn: NativeTurn,
        event_type: str,
        payload: Mapping[str, Any],
        native_type: str,
        interrupt_evidence: str | None = None,
    ) -> NormalizedEvent | None:
        if turn.metadata.get("terminal"):
            return None
        turn.metadata["terminal"] = event_type
        return NormalizedEvent(
            event_type,
            {
                **dict(payload),
                "session_ref": turn.session_ref,
                "run_ref": turn.run_ref,
            },
            native_type,
            interrupt_evidence,
        )

    def _session_event(
        self, turn: NativeTurn, event: Mapping[str, Any]
    ) -> list[NormalizedEvent]:
        native_type = event.get("type")
        seq = event.get("seq")
        data = event.get("data")
        if not isinstance(native_type, str) or not isinstance(data, Mapping):
            return []
        if isinstance(seq, int) and not isinstance(seq, bool):
            boundary = int(turn.metadata.get("from_event_seq", 0))
            seen = turn.metadata.setdefault("seen_event_seq", set())
            if seq < boundary or seq in seen:
                return []
            seen.add(seq)
        native = _bounded_native(event)
        if native_type == "turn/start":
            return [NormalizedEvent(
                "run.started", {"status": "running", "native": native}, native_type
            )]
        if native_type == "assistant/chunk":
            chunk = data.get("chunk")
            if not isinstance(chunk, Mapping):
                return []
            kind = chunk.get("type")
            if kind in {"text-delta", "reasoning-delta"} and isinstance(
                chunk.get("text"), str
            ):
                return [NormalizedEvent(
                    "assistant.delta",
                    {
                        "text": chunk["text"],
                        "segment": (
                            "reasoning" if kind == "reasoning-delta" else "answer"
                        ),
                        "native": native,
                    },
                    f"{native_type}.{kind}",
                )]
            if kind == "usage" and isinstance(chunk.get("usage"), Mapping):
                usage = self._usage(chunk["usage"])
                if usage:
                    return [NormalizedEvent(
                        "usage",
                        {"tokens": usage, "native": native},
                        f"{native_type}.usage",
                    )]
            return []
        if native_type == "assistant/message" and isinstance(
            data.get("usage"), Mapping
        ):
            usage = self._usage(data["usage"])
            return [NormalizedEvent(
                "usage", {"tokens": usage, "native": native}, native_type
            )] if usage else []
        if native_type == "tool/call":
            return [NormalizedEvent(
                "tool.started",
                {
                    "tool_ref": data.get("callId"),
                    "name": data.get("name"),
                    "arguments": data.get("arguments"),
                    "native": native,
                },
                native_type,
            )]
        if native_type == "tool/result":
            message = data.get("message")
            tool_ref = (
                message.get("toolCallId") if isinstance(message, Mapping) else None
            )
            is_error = message.get("isError") if isinstance(message, Mapping) else None
            return [NormalizedEvent(
                "tool.completed",
                {
                    "tool_ref": tool_ref,
                    "status": "failed" if is_error else "completed",
                    "native": native,
                },
                native_type,
            )]
        if native_type == "turn/end":
            if self._proof_authority is not None:
                self._proof_quiesced = True
            reason = data.get("reason")
            kind = reason.get("kind") if isinstance(reason, Mapping) else None
            if kind == "completed":
                terminal = self._terminal(
                    turn,
                    "run.completed",
                    {"status": "completed", "native": native},
                    native_type,
                )
            elif kind in {"aborted", "cancelled", "interrupted"}:
                terminal = self._terminal(
                    turn,
                    "run.interrupted",
                    {"status": "cancelled", "native": native},
                    native_type,
                    "native",
                )
            else:
                terminal = self._terminal(
                    turn,
                    "run.failed",
                    {
                        "status": "failed",
                        "error": "HARNESS_NATIVE_RUN_FAILED",
                        "reason": kind or "unknown",
                        "detail": json.dumps(
                            _bounded_native(reason),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                    native_type,
                )
            return [terminal] if terminal is not None else []
        unknown = turn.metadata.setdefault("unknown_native_events", [])
        if len(unknown) < MAX_UNKNOWN_EVENTS:
            unknown.append(native)
        return []

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        stream = turn.metadata.get("stream")
        if stream is None:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host event stream is missing"
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
            },
            "session.create",
        )
        stream_error = "HARNESS_HOST_STREAM_LOST"
        try:
            for envelope in stream:
                payload = (
                    envelope.get("payload")
                    if isinstance(envelope, Mapping)
                    else None
                )
                if not isinstance(payload, Mapping):
                    continue
                frame_type = payload.get("type")
                if (
                    frame_type == "session/event"
                    and payload.get("sessionId") == turn.session_ref
                ):
                    event = payload.get("event")
                    if isinstance(event, Mapping):
                        for normalized in self._session_event(turn, event):
                            yield normalized
                            if normalized.type in {
                                "run.completed", "run.failed", "run.interrupted"
                            }:
                                return
                elif (
                    frame_type in {"approval/requested", "question/requested"}
                    and payload.get("sessionId") == turn.session_ref
                ):
                    self.interrupt(turn)
                    terminal = self._terminal(
                        turn,
                        "run.failed",
                        {
                            "status": "failed",
                            "error": "HARNESS_APPROVAL_UNSUPPORTED",
                        },
                        str(frame_type),
                    )
                    if terminal is not None:
                        yield terminal
                    return
                elif frame_type == "stream/error":
                    break
        except deepseek_host.DeepSeekHostError as exc:
            stream_error = exc.code
        finally:
            stream.close()
        if turn.metadata.get("terminal"):
            return
        result = self.reconcile(turn, turn.metadata["context"])
        if result.outcome == "running":
            cancelled = self.interrupt(turn)
            if not cancelled.acknowledged:
                raise AdapterError(
                    "HARNESS_HOST_STREAM_LOST",
                    "DeepSeek Host stream ended while the native turn remained "
                    "running, and cancellation was not acknowledged",
                )
            terminal = self._terminal(
                turn,
                "run.failed",
                {
                    "status": "failed",
                    "error": stream_error,
                    "detail": (
                        "DeepSeek Host stream ended while the native turn remained "
                        "running; session cancellation was acknowledged"
                    ),
                },
                "session.history",
            )
            if terminal is not None:
                yield terminal
            return
        terminal_type = {
            "cancelled": "run.interrupted",
            "failed": "run.failed",
            "succeeded": "run.completed",
        }.get(result.outcome, "run.failed")
        terminal = self._terminal(
            turn,
            terminal_type,
            {
                "status": result.outcome,
                **(
                    {"error": "HARNESS_RECONCILIATION_UNKNOWN"}
                    if not result.proven
                    else {}
                ),
            },
            "session.history",
            result.interrupt_evidence,
        )
        if terminal is not None:
            yield terminal

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        client = turn.metadata.get("client")
        if client is None:
            return InterruptResult(False, "DeepSeek Host client is unavailable")
        context = turn.metadata.get("context")
        if self._proof_authority is not None:
            if not isinstance(context, ConversationContext):
                raise AdapterError(
                    "HARNESS_IDENTITY_MISSING",
                    "candidate cancellation lacks its exact conversation context",
                )
            self._revalidate_proof_authority(context, turn.session_ref)
            self._proof_quiesced = False
        try:
            result = client.call("session.cancel", {"sessionId": turn.session_ref})
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        accepted = isinstance(result, Mapping) and result.get("accepted") is True
        return InterruptResult(
            accepted,
            None if accepted else "DeepSeek Host did not acknowledge cancellation",
        )

    @staticmethod
    def _history_outcome(
        events: list[dict], boundary: int
    ) -> tuple[str, bool, str | None]:
        relevant = [
            event
            for event in events
            if isinstance(event.get("seq"), int) and event["seq"] >= boundary
        ]
        terminal = next(
            (
                event
                for event in reversed(relevant)
                if event.get("type") == "turn/end"
            ),
            None,
        )
        if terminal is not None:
            data = terminal.get("data")
            reason = data.get("reason") if isinstance(data, Mapping) else None
            kind = reason.get("kind") if isinstance(reason, Mapping) else None
            if kind == "completed":
                return "succeeded", True, None
            if kind in {"aborted", "cancelled", "interrupted"}:
                return "cancelled", True, "native"
            return "failed", True, None
        if any(event.get("type") == "turn/start" for event in relevant):
            return "running", True, None
        return "unknown", False, None

    def inspect(
        self, session_ref: str, context: ConversationContext
    ) -> SessionInspection:
        session_ref = self._session_ref(session_ref)
        client = self._managed_client(
            context, session_ref, recovery=True, bind_identity=False
        )
        try:
            self._require_recovery_target(client, session_ref, context)
            self._bind_execution_identity(context, session_ref)
            listed = client.call("session.list", {})
            items = listed.get("items") if isinstance(listed, Mapping) else None
            if not isinstance(items, list):
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR",
                    "DeepSeek Host returned invalid session list",
                )
            row = next(
                (
                    item
                    for item in items
                    if isinstance(item, Mapping)
                    and item.get("sessionId") == session_ref
                ),
                None,
            )
            if row is None:
                return SessionInspection(session_ref, False, "missing")
            if row.get("cwd") != str(context.checked_worktree()):
                raise AdapterError(
                    "HARNESS_WORKTREE_MISMATCH",
                    "DeepSeek native session belongs to another worktree",
                )
            events = self._history(client, session_ref)
        except deepseek_host.HostRpcError:
            return SessionInspection(session_ref, False, "missing")
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        outcome, proven, _interrupt = self._history_outcome(events, 0)
        state = "running" if row.get("running") is True else outcome
        return SessionInspection(
            session_ref,
            True,
            state,
            context.checked_worktree(),
            {
                "last_seq": max(
                    (event.get("seq", -1) for event in events), default=-1
                ),
                "proven": proven,
            },
        )

    def reconcile(
        self, turn: NativeTurn, context: ConversationContext
    ) -> ReconcileResult:
        client = turn.metadata.get("client")
        if client is None:
            client = self._managed_client(
                context, turn.session_ref, recovery=True, bind_identity=False
            )
            # Recovery begins from a durable NativeTurn with no live transport.
            # Retain the newly authenticated client so every later reconcile and
            # pending interrupt reuse this adapter's one full-lifetime lease.
            turn.metadata["client"] = client
        try:
            self._require_recovery_target(client, turn.session_ref, context)
            self._bind_execution_identity(context, turn.session_ref)
            events = self._history(client, turn.session_ref)
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        boundary = _run_boundary(turn.run_ref)
        outcome, proven, interrupt = self._history_outcome(events, boundary)
        if self._proof_authority is not None:
            self._proof_quiesced = proven and outcome not in {"running", "unknown"}
        return ReconcileResult(
            outcome,
            proven,
            (
                f"DeepSeek Host history proves {outcome} from event {boundary}"
                if proven
                else (
                    "DeepSeek Host history has no terminal evidence "
                    f"from event {boundary}"
                )
            ),
            interrupt,
        )

    def close(self) -> None:
        close_error: AdapterError | None = None
        if (
            self._proof_authority is not None
            and self._proof_context is not None
            and self._proof_session_id is not None
        ):
            try:
                deepseek_web.retire_session_identity(
                    env=self._proof_context.env,
                    root_session_id=self._proof_session_id,
                    quiesced=self._proof_quiesced,
                )
            except deepseek_web.DeepSeekWebError as exc:
                close_error = AdapterError(exc.code, exc.detail)
        if self._reserved_session is not None:
            deepseek_web.release_managed_session(self._reserved_session)
            self._reserved_session = None
        if self._shell_lease is not None:
            self._shell_lease.close()
            self._shell_lease = None
        self._proof_authority = None
        self._proof_context = None
        self._proof_session_id = None
        self._proof_quiesced = True
        if close_error is not None:
            raise close_error
