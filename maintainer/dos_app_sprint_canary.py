#!/usr/bin/env python3
"""Run an exact-ref, disposable dos-app Sprint promotion canary.

This is source-maintainer tooling, not an ``sc`` verb.  It deliberately lives
outside the engine materialization set and controls a disposable installed fork
from the subfloor source checkout.

The command is intentionally conservative:

* every credential, capacity, dirty-state, active-Sprint, and remote collision
  check completes before a disposable install or remote ref is created;
* the candidate is resolved to one commit and that exact commit is installed;
* real browser conversations drive either the standard Codex/Kimi route or an
  explicit provider-neutral DeepSeek Sprint route;
* stage and whole-run deadlines fail closed;
* the durable receipt is allow-listed and recursively redacted; and
* cleanup runs after every partial failure, is fatal when incomplete, and can be
  safely repeated with the ``cleanup`` subcommand.

The live canary is intentionally not run in CI.  Hermetic tests inject the
backend at the controller boundary and prove orchestration and failure cleanup.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

RECEIPT_VERSION = 1
WORKSPACE_PREFIX = "subfloor-dos-app-canary-"
REMOTE_PREFIX = "subfloor-canary"
ENGINE_REMOTE = "super-coder"
MIN_GITHUB_REMAINING = 100
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
EXPLICIT_TEMP_ROOT = Path("/home")
FORK_PREPARATION_PATHS = {
    ".github/workflows/subfloor-visual-qa.yml",
    ".gitignore",
    ".sc-state/engine.ref",
    ".sc-state/visual-qa.example.json",
    "Makefile",
    "sc",
}
TERMINAL_LIFECYCLES = {"completed", "aborted"}
FAILURE_LIFECYCLES = {"paused", "aborted"}
PICKUP_INJECTION_PAUSE_REASON = "wake_pickup_evidence_invalid"
STANDARD_PROFILE = "standard"
DEEPSEEK_SPRINT_PROFILE = "deepseek-sprint"
PROFILES = {STANDARD_PROFILE, DEEPSEEK_SPRINT_PROFILE}
DEEPSEEK_MODEL = "ollama-cloud/deepseek-v4-pro:0813"
PROVIDER_CREDENTIAL_ENV = {
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MISTRAL_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
}
HEX_SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,47}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CONVERSATION_ID_RE = re.compile(r"^cv_[0-9a-f]{32}$")
DEEPSEEK_SESSION_ID_RE = re.compile(r"^deepseek-[0-9a-f]{32}$")
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\b(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "body",
    "credential",
    "credentials",
    "error_detail",
    "full_prompt",
    "message_body",
    "native_session_body",
    "prompt",
    "refresh_token",
    "reasoning",
    "secret",
    "stack_trace",
    "text",
    "token",
    "transcript",
}
QAQC_EVIDENCE_KEYS = frozenset(
    {"boot", "terminal", "record_qaqc", "action_receipt", "postcondition"}
)
QAQC_BOOT_KEYS = frozenset(
    {"role", "skill", "shell_tool", "candidate", "predeclaration"}
)
QAQC_COMMAND_KEYS = frozenset(
    {"observed", "invocation_count", "exit_class", "receipt", "identity"}
)
QAQC_ACTION_RECEIPT_KEYS = frozenset({"count", "identity"})
QAQC_ACTION_RECEIPT_IDENTITIES = frozenset(
    {
        "matched",
        "absent",
        "duplicate",
        "sprint_mismatch",
        "participant_mismatch",
        "shell_mismatch",
        "role_mismatch",
        "generation_mismatch",
        "conversation_mismatch",
        "session_mismatch",
        "run_mismatch",
        "candidate_mismatch",
        "spec_mismatch",
        "phase_mismatch",
        "approval_mismatch",
        "row_mismatch",
        "malformed",
    }
)
QAQC_TERMINAL_CLASSES = frozenset(
    {"succeeded", "failed", "cancelled", "unknown", "missing", "ambiguous"}
)
QAQC_EXIT_CLASSES = frozenset(
    {"success", "failure", "missing_completion", "not_invoked", "ambiguous"}
)
QAQC_POSTCONDITIONS = frozenset(
    {"approved", "absent", "reviewer_mismatch", "revision_mismatch", "verdict_mismatch", "ambiguous"}
)
PARTICIPANT_FAILURE_KEYS = frozenset(
    {
        "schema_version",
        "source",
        "phase",
        "category",
        "upstream_code",
        "http_status",
        "provider_request_observed",
        "provider_exact",
        "model_exact",
        "reserved_default_omitted",
        "shell_tool_declared",
        "purpose",
    }
)
PARTICIPANT_FAILURE_SOURCES = frozenset(
    {"provider", "carrier", "protocol", "tool-dispatch", "engine"}
)
PARTICIPANT_FAILURE_PHASES = frozenset(
    {
        "initialize",
        "request-serialize",
        "provider-request",
        "provider-response",
        "tool-call-decode",
        "tool-dispatch",
        "terminal",
    }
)
PARTICIPANT_FAILURE_CATEGORIES = frozenset(
    {
        "model-tools-unsupported",
        "tool-schema-rejected",
        "tool-call-malformed",
        "authentication",
        "quota-or-rate-limit",
        "provider-unavailable",
        "protocol-contract",
        "carrier-contract",
        "unknown",
    }
)
PARTICIPANT_FAILURE_PURPOSES = frozenset(
    {"conversation", "compaction", "session-title", "unknown"}
)
PARTICIPANT_UPSTREAM_CODES = frozenset(
    {
        "ABORTED",
        "AUTH",
        "CONTEXT_WINDOW_EXCEEDED",
        "EMPTY_RESPONSE",
        "INVALID_CREDENTIAL",
        "INVALID_REQUEST",
        "MALFORMED_RESPONSE",
        "MISSING_CREDENTIAL",
        "PI_AI_ERROR",
        "QUOTA",
        "RATE_LIMIT",
        "SERVER",
        "STREAM_CLOSED",
        "TIMEOUT",
        "TRANSPORT",
        "UNSUPPORTED_CONTENT",
    }
)
PARTICIPANT_HTTP_CODE = re.compile(r"^HTTP_[1-5][0-9]{2}$")
ROUTE_ADMISSION_KEYS = (
    "contract_version",
    "requested_provider",
    "requested_model",
    "admitted",
    "error_code",
    "category",
    "required_surface",
    "required_capability",
    "freshness",
    "authentication",
    "tool_capability",
    "exit_class",
)
ROUTE_ADMISSION_CATEGORIES = frozenset(
    {
        "harness-unavailable",
        "runtime-unavailable",
        "credential-or-authentication",
        "catalogue-unavailable",
        "catalogue-stale",
        "exact-model-absent",
        "exact-model-unavailable",
        "tool-capability-unsupported",
        "tool-capability-unproven",
        "route-evidence-invalid",
        "provider-option-drift",
        "unknown",
    }
)
ROUTE_ADMISSION_ERROR_CODES = {
    category: "ROUTE_ADMISSION_" + category.replace("-", "_").upper()
    for category in ROUTE_ADMISSION_CATEGORIES
}
ROUTE_ADMISSION_FRESHNESS = frozenset({"fresh", "stale", "missing", "unknown"})
ROUTE_ADMISSION_AUTHENTICATION = frozenset(
    {"verified", "failed", "unproven", "unknown"}
)
ROUTE_ADMISSION_TOOL_CAPABILITY = frozenset(
    {"supported", "unsupported", "unproven", "unknown"}
)
ROUTE_ADMISSION_EXIT_CLASSES = frozenset(
    {"success", "route-rejected", "malformed-response", "identity-mismatch", "command-failed"}
)
RESTART_REHEARSAL_PORT = 18991
RESTART_REHEARSAL_HELPER = (
    ".super-coder/scripts/deepseek_exact_restart_rehearsal.py"
)
RESTART_CATEGORIES = frozenset(
    {
        "command-contract",
        "api-or-auth-target",
        "session-reference-missing",
        "session-reference-mismatch",
        "persisted-root-missing",
        "persisted-root-mismatch",
        "boot-or-runtime-drift",
        "route-drift",
        "old-process-still-live",
        "process-collision",
        "broker-state-or-lease",
        "native-resume-rejected",
        "restart-timeout",
        "unknown",
    }
)
RESTART_PROBE_KEYS = frozenset(
    {
        "ok",
        "candidate_sha",
        "conversation_id",
        "native_session_id",
        "provider",
        "model",
        "runtime_version",
        "source_commit",
        "patch_sha256",
        "composition_sha256",
        "binding_digest",
        "boot_sha256",
        "persisted_root_id",
        "persisted_root_device",
        "persisted_root_inode",
        "persisted_root_present",
        "persisted_root_private",
        "run_id",
        "run_state",
        "process_pid",
        "process_start_ticks",
        "process_live",
        "lease_clear",
        "inspect_session_exact",
        "inspect_presence",
        "inspect_state",
        "reconcile_outcome",
        "reconcile_proven",
    }
)
RESTART_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "category",
        "command_exit_class",
        "command_exit_status",
        "candidate_sha",
        "conversation_id",
        "native_session_id",
        "provider",
        "model",
        "runtime_version",
        "source_commit",
        "patch_sha256",
        "composition_sha256",
        "binding_digest",
        "boot_sha256",
        "persisted_root_id",
        "persisted_root_device",
        "persisted_root_inode",
        "persisted_root_private",
        "old_process_pid",
        "old_process_start_ticks",
        "old_process_live",
        "resumed_process_pid",
        "resumed_process_start_ticks",
        "resumed_process_distinct",
        "same_candidate",
        "same_runtime",
        "same_boot",
        "same_route",
        "same_conversation",
        "same_native_session",
        "same_persisted_root",
        "inspect_session_exact",
        "inspect_presence",
        "inspect_state",
        "reconcile_outcome",
        "reconcile_proven",
        "broker_state",
        "lease_clear",
    }
)


class CanaryError(RuntimeError):
    """A stable, receipt-safe canary failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclasses.dataclass(frozen=True)
class CanaryConfig:
    source_repo: Path
    engine_ref: str
    dos_app_repo: Path
    dos_app_ref: str
    repository: str | None
    receipt_path: Path
    temp_parent: Path
    run_id: str
    temp_parent_explicit: bool = False
    profile: str = STANDARD_PROFILE
    credential_file: Path | None = None
    stage_timeout_s: float = 900.0
    whole_timeout_s: float = 3600.0
    poll_interval_s: float = 2.0


@dataclasses.dataclass(frozen=True)
class Preflight:
    candidate_sha: str
    base_sha: str
    repository: str
    remote_url: str
    workspace: Path
    base_branch: str
    head_branch: str
    container: str
    network: str
    api_port: int
    dev_port: int
    github_remaining: int


@dataclasses.dataclass
class ResourceLedger:
    workspace: str | None = None
    marker_written: bool = False
    candidate_sha: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    repository: str | None = None
    container: str | None = None
    network: str | None = None
    pull_request: int | None = None


@dataclasses.dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclasses.dataclass(frozen=True)
class ForceNewProbe:
    reviewer_id: str
    target_path: str
    proof_path: str


class Backend(Protocol):
    """Side-effect boundary used by the live backend and hermetic tests."""

    def preflight(self, config: CanaryConfig) -> Preflight: ...

    def create_disposable(
        self,
        config: CanaryConfig,
        facts: Preflight,
        ledger: ResourceLedger,
        checkpoint: Callable[[], None],
    ) -> None: ...

    def launch(
        self, config: CanaryConfig, facts: Preflight, ledger: ResourceLedger
    ) -> dict[str, Any]: ...

    def orchestrate(
        self,
        config: CanaryConfig,
        facts: Preflight,
        ledger: ResourceLedger,
        stage: Callable[[str], None],
        checkpoint: Callable[[], None],
    ) -> dict[str, Any]: ...

    def cleanup(
        self,
        config: CanaryConfig,
        facts: Preflight | None,
        ledger: ResourceLedger,
    ) -> list[dict[str, Any]]: ...


class Deadline:
    def __init__(
        self,
        whole_timeout_s: float,
        stage_timeout_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self.whole_started = clock()
        self.whole_deadline = self.whole_started + whole_timeout_s
        self.stage_timeout_s = stage_timeout_s
        self.stage_started = self.whole_started
        self.stage_deadline = self.stage_started + stage_timeout_s
        self.stage_name = "preflight"

    def enter(self, name: str) -> None:
        now = self.clock()
        self.stage_name = name
        self.stage_started = now
        # Cleanup gets its own bounded grace period.  A whole-run timeout is
        # exactly when cleanup is most necessary; making it inherit an already
        # expired whole deadline would strand every resource.
        self.stage_deadline = (
            now + self.stage_timeout_s
            if name == "cleanup"
            else min(self.whole_deadline, now + self.stage_timeout_s)
        )

    def remaining(self) -> float:
        now = self.clock()
        effective_deadline = (
            self.stage_deadline
            if self.stage_name == "cleanup"
            else min(self.stage_deadline, self.whole_deadline)
        )
        remaining = effective_deadline - now
        if remaining <= 0:
            boundary = (
                "whole-run"
                if self.stage_name != "cleanup"
                and self.whole_deadline <= self.stage_deadline
                else f"stage:{self.stage_name}"
            )
            raise CanaryError(
                "CANARY_DEADLINE_EXCEEDED",
                f"{boundary} deadline exceeded",
                details={"stage": self.stage_name},
            )
        return remaining

    def elapsed(self) -> dict[str, float]:
        now = self.clock()
        return {
            "whole_seconds": round(now - self.whole_started, 3),
            "stage_seconds": round(now - self.stage_started, 3),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def sanitize(value: Any, *, key: str | None = None) -> Any:
    """Return receipt-safe evidence without prompts, secrets, or transcripts."""
    if key and key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


class Receipt:
    def __init__(self, path: Path, config: CanaryConfig) -> None:
        self.path = path.resolve()
        self.data: dict[str, Any] = {
            "receipt_version": RECEIPT_VERSION,
            "run_id": config.run_id,
            "status": "initializing",
            "started_at": utc_now(),
            "finished_at": None,
            "engine_ref_requested": config.engine_ref,
            "profile": config.profile,
            "temp_parent_explicit": config.temp_parent_explicit,
            "dos_app_repo": str(config.dos_app_repo.resolve()),
            "candidate_sha": None,
            "base_sha": None,
            "repository": config.repository,
            "runtime": {},
            "routes": {},
            "sprint": {},
            "pull_request": {},
            "timeline": [],
            "durations": {},
            "failure": None,
            "next_action": None,
            "resources": dataclasses.asdict(ResourceLedger()),
            "cleanup": {"attempts": 0, "complete": False, "actions": []},
        }

    @classmethod
    def load(cls, path: Path) -> tuple[dict[str, Any], ResourceLedger]:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CanaryError(
                "RECEIPT_INVALID", f"cannot read receipt: {path}"
            ) from exc
        if data.get("receipt_version") != RECEIPT_VERSION:
            raise CanaryError("RECEIPT_INVALID", "unsupported receipt version")
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise CanaryError("RECEIPT_INVALID", "receipt run_id is invalid")
        resources = data.get("resources")
        if not isinstance(resources, dict):
            raise CanaryError("RECEIPT_INVALID", "receipt resources are missing")
        allowed = {field.name for field in dataclasses.fields(ResourceLedger)}
        ledger = ResourceLedger(**{k: v for k, v in resources.items() if k in allowed})
        return data, ledger

    def event(self, event: str, **fields: Any) -> None:
        self.data["timeline"].append(
            sanitize({"at": utc_now(), "event": event, **fields})
        )

    def resources(self, ledger: ResourceLedger) -> None:
        self.data["resources"] = sanitize(dataclasses.asdict(ledger))

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = sanitize(self.data)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(safe, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


class CommandRunner:
    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        label: str,
    ) -> CommandResult:
        timeout = max(0.1, self.deadline.remaining())
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CanaryError(
                "CANARY_COMMAND_TIMEOUT",
                f"{label} exceeded its deadline",
                details={"stage": self.deadline.stage_name},
            ) from exc
        result = CommandResult(
            completed.stdout or "", completed.stderr or "", completed.returncode
        )
        if check and result.returncode != 0:
            tail = "\n".join((result.stderr or result.stdout).splitlines()[-8:])
            raise CanaryError(
                "CANARY_COMMAND_FAILED",
                f"{label} failed with exit {result.returncode}",
                details={"label": label, "tail": redact_text(tail)},
            )
        return result


class JsonHttp:
    def __init__(self, base_url: str, deadline: Deadline) -> None:
        self.base_url = base_url.rstrip("/")
        self.deadline = deadline

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base_url + path, data=raw, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=max(0.1, self.deadline.remaining())
            ) as response:
                decoded = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode(errors="replace")
            raise CanaryError(
                "CANARY_API_FAILED",
                f"{method} {path} returned HTTP {exc.code}",
                details={"response": redact_text(payload[:1000])},
            ) from exc
        except (OSError, ValueError) as exc:
            raise CanaryError("CANARY_API_FAILED", f"{method} {path} failed") from exc
        if not isinstance(decoded, dict):
            raise CanaryError(
                "CANARY_API_INVALID", f"{method} {path} returned non-object JSON"
            )
        return decoded


def _parse_repository(remote_url: str) -> str | None:
    value = remote_url.strip().removesuffix(".git")
    match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+)$", value)
    return match.group(1) if match else None


def _json_output(result: CommandResult, *, label: str) -> Any:
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise CanaryError(
            "CANARY_COMMAND_INVALID", f"{label} returned invalid JSON"
        ) from exc


def _route_admission_fallback(exit_class: str) -> dict[str, Any]:
    provider, model = DEEPSEEK_MODEL.split("/", 1)
    return {
        "contract_version": 1,
        "requested_provider": provider,
        "requested_model": model,
        "admitted": False,
        "error_code": ROUTE_ADMISSION_ERROR_CODES["unknown"],
        "category": "unknown",
        "required_surface": "sprint",
        "required_capability": "reviewer-shell-tool-execution",
        "freshness": "unknown",
        "authentication": "unknown",
        "tool_capability": "unknown",
        "exit_class": exit_class,
    }


def _validated_route_admission(result: CommandResult) -> dict[str, Any]:
    """Consume only the fixed admission contract; never project raw output."""
    try:
        payload = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or tuple(payload) != ROUTE_ADMISSION_KEYS:
        raise CanaryError(
            "CANARY_ROUTE_ADMISSION_INVALID",
            "exact-route admission returned a malformed bounded contract",
            details={"route_admission": _route_admission_fallback("malformed-response")},
        )

    provider, model = DEEPSEEK_MODEL.split("/", 1)
    identity_matches = (
        payload.get("requested_provider") == provider
        and payload.get("requested_model") == model
        and payload.get("required_surface") == "sprint"
        and payload.get("required_capability")
        == "reviewer-shell-tool-execution"
    )
    if not identity_matches:
        raise CanaryError(
            "CANARY_ROUTE_ADMISSION_INVALID",
            "exact-route admission identity did not match the canary",
            details={"route_admission": _route_admission_fallback("identity-mismatch")},
        )

    admitted = payload.get("admitted")
    category = payload.get("category")
    error_code = payload.get("error_code")
    bounded = (
        payload.get("contract_version") == 1
        and type(admitted) is bool
        and payload.get("freshness") in ROUTE_ADMISSION_FRESHNESS
        and payload.get("authentication") in ROUTE_ADMISSION_AUTHENTICATION
        and payload.get("tool_capability") in ROUTE_ADMISSION_TOOL_CAPABILITY
        and payload.get("exit_class") in ROUTE_ADMISSION_EXIT_CLASSES
    )
    if admitted:
        bounded = bounded and (
            result.returncode == 0
            and category is None
            and error_code is None
            and payload.get("freshness") == "fresh"
            and payload.get("authentication") == "verified"
            and payload.get("tool_capability") == "supported"
            and payload.get("exit_class") == "success"
        )
    else:
        bounded = bounded and (
            result.returncode == 2
            and category in ROUTE_ADMISSION_CATEGORIES
            and error_code == ROUTE_ADMISSION_ERROR_CODES.get(category)
            and payload.get("exit_class") == "route-rejected"
        )
    if not bounded:
        exit_class = "command-failed" if result.returncode not in {0, 2} else "malformed-response"
        raise CanaryError(
            "CANARY_ROUTE_ADMISSION_INVALID",
            "exact-route admission violated its bounded contract",
            details={"route_admission": _route_admission_fallback(exit_class)},
        )
    return {key: payload[key] for key in ROUTE_ADMISSION_KEYS}


def _restart_command_category(result: CommandResult) -> str | None:
    """Classify restart failures ephemerally without retaining command output."""
    if result.returncode == 0:
        return None
    detail = f"{result.stdout}\n{result.stderr}".lower()
    if any(token in detail for token in ("restart aborted", "unknown argument", "usage:")):
        return "command-contract"
    if any(
        token in detail
        for token in ("unauthorized", "forbidden", "authentication", "api target")
    ):
        return "api-or-auth-target"
    if any(token in detail for token in ("broker", "lease", "engine unhealthy")):
        return "broker-state-or-lease"
    return "unknown"


def _validated_restart_probe(result: CommandResult) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        payload = None
    valid = (
        result.returncode == 0
        and isinstance(payload, dict)
        and set(payload) == RESTART_PROBE_KEYS
        and payload.get("ok") is True
        and isinstance(payload.get("candidate_sha"), str)
        and HEX_SHA.fullmatch(payload["candidate_sha"]) is not None
        and isinstance(payload.get("conversation_id"), str)
        and CONVERSATION_ID_RE.fullmatch(payload["conversation_id"]) is not None
        and payload.get("provider") == "ollama-cloud"
        and payload.get("model") == DEEPSEEK_MODEL
        and isinstance(payload.get("runtime_version"), str)
        and bool(payload["runtime_version"])
        and isinstance(payload.get("source_commit"), str)
        and HEX_SHA.fullmatch(payload["source_commit"]) is not None
        and all(
            isinstance(payload.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", payload[key]) is not None
            for key in (
                "patch_sha256",
                "composition_sha256",
                "binding_digest",
                "boot_sha256",
            )
        )
        and isinstance(payload.get("persisted_root_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", payload["persisted_root_id"]) is not None
        and all(
            payload.get(key) is None
            or (
                isinstance(payload.get(key), int)
                and not isinstance(payload.get(key), bool)
                and payload[key] > 0
            )
            for key in ("persisted_root_device", "persisted_root_inode")
        )
        and all(
            type(payload.get(key)) is bool
            for key in (
                "persisted_root_present",
                "persisted_root_private",
                "process_live",
                "lease_clear",
            )
        )
        and all(
            payload.get(key) is None or type(payload.get(key)) is bool
            for key in ("inspect_session_exact", "inspect_presence", "reconcile_proven")
        )
        and all(
            isinstance(payload.get(key), int)
            and not isinstance(payload.get(key), bool)
            and payload[key] > 0
            for key in ("run_id", "process_pid", "process_start_ticks")
        )
        and isinstance(payload.get("native_session_id"), str)
        and DEEPSEEK_SESSION_ID_RE.fullmatch(payload["native_session_id"]) is not None
        and payload.get("run_state") in {"succeeded", "failed", "cancelled"}
        and all(
            payload.get(key) is None
            or (
                isinstance(payload.get(key), str)
                and 1 <= len(payload[key]) <= 64
                and re.fullmatch(r"[a-z0-9._-]+", payload[key]) is not None
            )
            for key in ("inspect_state", "reconcile_outcome")
        )
    )
    if not valid:
        raise CanaryError(
            "CANARY_RESTART_RECOVERY_FAILED",
            "exact-session probe violated its bounded contract",
            details={"restart": {"schema_version": 1, "category": "unknown"}},
        )
    return {key: payload[key] for key in RESTART_PROBE_KEYS}


def _restart_result(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    command_exit_class: str,
    command_exit_status: int,
    category: str | None = None,
) -> dict[str, Any]:
    same_runtime = all(
        before.get(key) == after.get(key)
        for key in ("runtime_version", "source_commit", "patch_sha256", "composition_sha256")
    )
    same_route = all(
        before.get(key) == after.get(key)
        for key in ("provider", "model", "binding_digest")
    )
    distinct_process = (
        isinstance(before.get("process_pid"), int)
        and isinstance(before.get("process_start_ticks"), int)
        and isinstance(after.get("process_pid"), int)
        and isinstance(after.get("process_start_ticks"), int)
        and (before["process_pid"], before["process_start_ticks"])
        != (after["process_pid"], after["process_start_ticks"])
    )
    checks = {
        "same_candidate": before.get("candidate_sha") == after.get("candidate_sha"),
        "same_runtime": same_runtime,
        "same_boot": before.get("boot_sha256") == after.get("boot_sha256"),
        "same_route": same_route,
        "same_conversation": before.get("conversation_id") == after.get("conversation_id"),
        "same_native_session": before.get("native_session_id") == after.get("native_session_id"),
        "same_persisted_root": all(
            before.get(key) == after.get(key)
            for key in (
                "persisted_root_id",
                "persisted_root_device",
                "persisted_root_inode",
            )
        ),
    }
    if category is None:
        if not before.get("native_session_id") or not after.get("native_session_id"):
            category = "session-reference-missing"
        elif not checks["same_native_session"]:
            category = "session-reference-mismatch"
        elif not before.get("persisted_root_present") or not after.get("persisted_root_present"):
            category = "persisted-root-missing"
        elif not checks["same_persisted_root"]:
            category = "persisted-root-mismatch"
        elif not checks["same_candidate"] or not same_runtime or not checks["same_boot"]:
            category = "boot-or-runtime-drift"
        elif not same_route or not checks["same_conversation"]:
            category = "route-drift"
        elif before.get("process_live") is True:
            category = "old-process-still-live"
        elif not distinct_process:
            category = "process-collision"
        elif after.get("run_state") != "succeeded" or after.get("lease_clear") is not True:
            category = "broker-state-or-lease"
        elif (
            after.get("inspect_session_exact") is not True
            or after.get("inspect_presence") is not True
            or after.get("reconcile_proven") is not True
        ):
            category = "native-resume-rejected"
    result = {
        "schema_version": 1,
        "category": category,
        "command_exit_class": command_exit_class,
        "command_exit_status": command_exit_status,
        "candidate_sha": after.get("candidate_sha") or before.get("candidate_sha"),
        "conversation_id": after.get("conversation_id") or before.get("conversation_id"),
        "native_session_id": after.get("native_session_id") or before.get("native_session_id"),
        "provider": after.get("provider") or before.get("provider"),
        "model": after.get("model") or before.get("model"),
        "runtime_version": after.get("runtime_version") or before.get("runtime_version"),
        "source_commit": after.get("source_commit") or before.get("source_commit"),
        "patch_sha256": after.get("patch_sha256") or before.get("patch_sha256"),
        "composition_sha256": after.get("composition_sha256") or before.get("composition_sha256"),
        "binding_digest": after.get("binding_digest") or before.get("binding_digest"),
        "boot_sha256": after.get("boot_sha256") or before.get("boot_sha256"),
        "persisted_root_id": after.get("persisted_root_id") or before.get("persisted_root_id"),
        "persisted_root_device": after.get("persisted_root_device"),
        "persisted_root_inode": after.get("persisted_root_inode"),
        "persisted_root_private": after.get("persisted_root_private") is True,
        "old_process_pid": before.get("process_pid"),
        "old_process_start_ticks": before.get("process_start_ticks"),
        "old_process_live": before.get("process_live") is True,
        "resumed_process_pid": after.get("process_pid"),
        "resumed_process_start_ticks": after.get("process_start_ticks"),
        "resumed_process_distinct": distinct_process,
        **checks,
        "inspect_session_exact": after.get("inspect_session_exact"),
        "inspect_presence": after.get("inspect_presence"),
        "inspect_state": after.get("inspect_state"),
        "reconcile_outcome": after.get("reconcile_outcome"),
        "reconcile_proven": after.get("reconcile_proven"),
        "broker_state": after.get("run_state"),
        "lease_clear": after.get("lease_clear"),
    }
    return _validated_restart_result(result)


def _validated_restart_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    category = payload.get("category")
    native_session_id = payload.get("native_session_id")
    valid = (
        set(payload) == RESTART_RESULT_KEYS
        and payload.get("schema_version") == 1
        and (category is None or category in RESTART_CATEGORIES)
        and payload.get("command_exit_class") in {"success", "failure", "timeout"}
        and isinstance(payload.get("command_exit_status"), int)
        and not isinstance(payload.get("command_exit_status"), bool)
        and isinstance(payload.get("candidate_sha"), str)
        and HEX_SHA.fullmatch(payload["candidate_sha"]) is not None
        and isinstance(payload.get("conversation_id"), str)
        and CONVERSATION_ID_RE.fullmatch(payload["conversation_id"]) is not None
        and (
            (
                isinstance(native_session_id, str)
                and DEEPSEEK_SESSION_ID_RE.fullmatch(native_session_id) is not None
            )
            or (native_session_id is None and category == "session-reference-missing")
        )
        and payload.get("provider") == "ollama-cloud"
        and payload.get("model") == DEEPSEEK_MODEL
        and isinstance(payload.get("runtime_version"), str)
        and 1 <= len(payload["runtime_version"]) <= 64
        and isinstance(payload.get("source_commit"), str)
        and HEX_SHA.fullmatch(payload["source_commit"]) is not None
        and all(
            isinstance(payload.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", payload[key]) is not None
            for key in (
                "patch_sha256",
                "composition_sha256",
                "binding_digest",
                "boot_sha256",
            )
        )
        and isinstance(payload.get("persisted_root_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", payload["persisted_root_id"]) is not None
        and all(
            (
                isinstance(payload.get(key), int)
                and not isinstance(payload.get(key), bool)
                and payload[key] > 0
            )
            or (payload.get(key) is None and category == "persisted-root-missing")
            for key in ("persisted_root_device", "persisted_root_inode")
        )
        and all(
            payload.get(key) is None
            or (
                isinstance(payload.get(key), int)
                and not isinstance(payload.get(key), bool)
                and payload[key] > 0
            )
            for key in (
                "old_process_pid",
                "old_process_start_ticks",
                "resumed_process_pid",
                "resumed_process_start_ticks",
            )
        )
        and payload.get("broker_state") in {"succeeded", "failed", "cancelled"}
        and all(
            payload.get(key) is None
            or (
                isinstance(payload.get(key), str)
                and 1 <= len(payload[key]) <= 64
                and re.fullmatch(r"[a-z0-9._-]+", payload[key]) is not None
            )
            for key in ("inspect_state", "reconcile_outcome")
        )
        and all(
            payload.get(key) is None or type(payload.get(key)) is bool
            for key in ("inspect_session_exact", "inspect_presence", "reconcile_proven")
        )
        and all(type(payload.get(key)) is bool for key in (
            "persisted_root_private", "old_process_live", "resumed_process_distinct",
            "same_candidate", "same_runtime", "same_boot", "same_route",
            "same_conversation", "same_native_session", "same_persisted_root",
        ))
    )
    if not valid:
        raise CanaryError(
            "CANARY_RESTART_RECOVERY_FAILED",
            "restart evidence violated its bounded contract",
            details={"restart": {"schema_version": 1, "category": "unknown"}},
        )
    return {key: payload[key] for key in RESTART_RESULT_KEYS}


def _restart_passed(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("category") is None
        and payload.get("command_exit_class") == "success"
        and payload.get("command_exit_status") == 0
        and all(
            payload.get(key) is True
            for key in (
                "persisted_root_private",
                "resumed_process_distinct",
                "same_candidate",
                "same_runtime",
                "same_boot",
                "same_route",
                "same_conversation",
                "same_native_session",
                "same_persisted_root",
                "inspect_session_exact",
                "inspect_presence",
                "reconcile_proven",
                "lease_clear",
            )
        )
        and payload.get("old_process_live") is False
        and payload.get("broker_state") == "succeeded"
    )


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 2
    except OSError:
        return False


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _strictly_beneath(path: Path, parent: Path) -> bool:
    return path != parent and path.is_relative_to(parent)


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _validate_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise CanaryError(
                "CANARY_INPUT_INVALID",
                "explicit disposable parent is absent or unreadable",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CanaryError(
                "CANARY_INPUT_INVALID",
                "explicit disposable parent traverses a symlink",
            )


def _validated_explicit_parent(config: CanaryConfig) -> Path:
    raw_parent = config.temp_parent
    if not raw_parent.is_absolute() or ".." in raw_parent.parts:
        raise CanaryError(
            "CANARY_INPUT_INVALID",
            "explicit disposable parent must be an absolute path without traversal",
        )
    parent = _absolute_lexical(raw_parent)
    _validate_no_symlink_components(parent)
    try:
        metadata = parent.lstat()
        root = EXPLICIT_TEMP_ROOT.resolve(strict=True)
        root_metadata = root.stat()
    except OSError as exc:
        raise CanaryError(
            "CANARY_INPUT_INVALID",
            "explicit disposable parent or required filesystem root is unavailable",
        ) from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or mode != 0o700
        or parent.resolve(strict=True) != parent
        or not _strictly_beneath(parent, root)
        or metadata.st_dev != root_metadata.st_dev
    ):
        raise CanaryError(
            "CANARY_INPUT_INVALID",
            "explicit disposable parent failed path, ownership, mode, or filesystem checks",
        )
    return parent


def _validated_explicit_workspace(
    config: CanaryConfig,
    *,
    protected_paths: Sequence[Path] = (),
) -> tuple[Path, Path]:
    parent = _validated_explicit_parent(config)
    workspace = parent / f"{WORKSPACE_PREFIX}{config.run_id}"
    if workspace.parent != parent or not _strictly_beneath(workspace, parent):
        raise CanaryError(
            "CANARY_INPUT_INVALID",
            "disposable workspace is not an exact child of its parent",
        )
    if workspace.exists() or workspace.is_symlink():
        raise CanaryError("CANARY_COLLISION", "disposable workspace already exists")

    configured_protected = [
        config.source_repo,
        config.dos_app_repo,
        config.receipt_path,
        config.source_repo / ".sc-state",
        config.source_repo / ".super-coder",
        config.dos_app_repo / ".sc-state",
        config.dos_app_repo / ".super-coder",
    ]
    if config.credential_file is not None:
        configured_protected.extend(
            [config.credential_file, config.credential_file.parent]
        )
    for protected in (*configured_protected, *protected_paths):
        candidate = _absolute_lexical(protected)
        if _paths_overlap(workspace, candidate):
            raise CanaryError(
                "CANARY_INPUT_INVALID",
                "disposable workspace overlaps a protected path",
            )

    for sibling in parent.iterdir():
        if sibling.name.startswith(WORKSPACE_PREFIX) and _paths_overlap(
            workspace, _absolute_lexical(sibling)
        ):
            raise CanaryError(
                "CANARY_COLLISION", "disposable workspace overlaps another run"
            )
    return parent, workspace


def _require_disposable_capacity(parent: Path) -> int:
    try:
        free_bytes = shutil.disk_usage(parent).free
    except OSError as exc:
        raise CanaryError(
            "CANARY_CAPACITY_FAILED", "cannot inspect free disk"
        ) from exc
    if free_bytes < MIN_FREE_BYTES:
        raise CanaryError(
            "CANARY_CAPACITY_FAILED",
            "insufficient disk capacity",
            details={"free_bytes": free_bytes, "required_bytes": MIN_FREE_BYTES},
        )
    return free_bytes


class HostBackend:
    """Live host/Docker/GitHub implementation."""

    def __init__(
        self,
        deadline: Deadline,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.deadline = deadline
        self.runner = CommandRunner(deadline)
        self.sleep = sleep
        self._port_sockets: list[socket.socket] = []
        self._provider_key: str | None = None

    @staticmethod
    def _validate_credential_file(path: Path | None) -> Path:
        if path is None:
            raise CanaryError(
                "CANARY_CREDENTIAL_INVALID",
                "deepseek-sprint profile requires an authorized credential file",
            )
        candidate = path.resolve(strict=False)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise CanaryError(
                "CANARY_CREDENTIAL_INVALID",
                "authorized credential file is absent or unreadable",
            ) from exc
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 16 <= metadata.st_size <= 4096
            or candidate != path.absolute()
        ):
            raise CanaryError(
                "CANARY_CREDENTIAL_INVALID",
                "authorized credential file failed ownership, mode, size, or path checks",
            )
        return path

    def _read_provider_key(self, path: Path | None) -> str:
        source = self._validate_credential_file(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                value = stream.read(4097).strip()
        except (OSError, UnicodeError) as exc:
            raise CanaryError(
                "CANARY_CREDENTIAL_INVALID",
                "authorized credential could not be read safely",
            ) from exc
        if not 16 <= len(value) <= 4096 or any(char.isspace() for char in value):
            raise CanaryError(
                "CANARY_CREDENTIAL_INVALID",
                "authorized credential has an unsafe structure",
            )
        return value

    def _runtime_env(self, facts: Preflight) -> dict[str, str]:
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in PROVIDER_CREDENTIAL_ENV
        }
        env["SC_NET"] = facts.network
        if self._provider_key is not None:
            env["OLLAMA_API_KEY"] = self._provider_key
        return env

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        label: str,
    ) -> CommandResult:
        return self.runner.run(argv, cwd=cwd, env=env, check=check, label=label)

    def _git(
        self, repo: Path, *args: str, label: str, check: bool = True
    ) -> CommandResult:
        return self._run(["git", "-C", str(repo), *args], check=check, label=label)

    def _infra_git(self, repo: Path, *args: str, label: str) -> CommandResult:
        try:
            return self._git(repo, *args, label=label)
        except CanaryError as exc:
            raise CanaryError(
                "CANARY_INFRASTRUCTURE_FAILED",
                f"GitHub transport failed during {label}",
                details=exc.details,
            ) from exc

    def _infra_run(
        self,
        argv: Sequence[str],
        *,
        label: str,
        cwd: Path | None = None,
    ) -> CommandResult:
        try:
            return self._run(argv, cwd=cwd, label=label)
        except CanaryError as exc:
            raise CanaryError(
                "CANARY_INFRASTRUCTURE_FAILED",
                f"GitHub API failed during {label}",
                details=exc.details,
            ) from exc

    def _reserve_ports(self) -> tuple[int, int]:
        selected: list[int] = []
        for port in range(8800, 8900):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                sock.close()
                continue
            self._port_sockets.append(sock)
            selected.append(port)
            if len(selected) == 2:
                return selected[0], selected[1]
        self._release_ports()
        raise CanaryError(
            "CANARY_CAPACITY_FAILED", "two isolated ports are unavailable"
        )

    def _release_ports(self) -> None:
        for sock in self._port_sockets:
            with contextlib.suppress(OSError):
                sock.close()
        self._port_sockets.clear()

    def preflight(self, config: CanaryConfig) -> Preflight:
        if config.profile not in PROFILES:
            raise CanaryError("CANARY_INPUT_INVALID", "canary profile is invalid")
        if not RUN_ID_RE.fullmatch(config.run_id):
            raise CanaryError("CANARY_INPUT_INVALID", "run_id is invalid")
        source = config.source_repo.resolve()
        target = config.dos_app_repo.resolve()
        receipt = config.receipt_path.resolve()
        if config.temp_parent_explicit:
            temp_parent, workspace = _validated_explicit_workspace(config)
        else:
            temp_parent = config.temp_parent.resolve()
            workspace = temp_parent / f"{WORKSPACE_PREFIX}{config.run_id}"
            if workspace.exists():
                raise CanaryError(
                    "CANARY_COLLISION", f"workspace already exists: {workspace}"
                )
        if workspace == receipt or workspace in receipt.parents:
            raise CanaryError(
                "CANARY_INPUT_INVALID", "receipt must live outside disposable state"
            )
        free_bytes = _require_disposable_capacity(temp_parent)
        for executable in ("git", "gh", "docker", "python3"):
            if shutil.which(executable) is None:
                raise CanaryError(
                    "CANARY_PREFLIGHT_FAILED",
                    f"required executable is missing: {executable}",
                )
        if (
            not (source / ".git").exists()
            and not self._git(
                source,
                "rev-parse",
                "--git-dir",
                label="source repository probe",
                check=False,
            ).returncode
            == 0
        ):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED", "source_repo is not a Git repository"
            )
        if not target.is_dir():
            raise CanaryError("CANARY_PREFLIGHT_FAILED", "dos_app_repo is missing")
        if config.temp_parent_explicit:
            worktrees: list[Path] = []
            for repository_path in (source, target):
                listing = self._git(
                    repository_path,
                    "worktree",
                    "list",
                    "--porcelain",
                    label="protected Git worktree inventory",
                )
                worktrees.extend(
                    Path(line.removeprefix("worktree "))
                    for line in listing.stdout.splitlines()
                    if line.startswith("worktree ")
                )
            _validated_explicit_workspace(config, protected_paths=worktrees)
        candidate = self._git(
            source,
            "rev-parse",
            "--verify",
            f"{config.engine_ref}^{{commit}}",
            label="resolve exact engine ref",
        ).stdout.strip()
        if not HEX_SHA.fullmatch(candidate):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED", "engine ref did not resolve to a commit"
            )
        base_sha = self._git(
            target,
            "rev-parse",
            "--verify",
            f"{config.dos_app_ref}^{{commit}}",
            label="resolve dos-app base",
        ).stdout.strip()
        if not HEX_SHA.fullmatch(base_sha):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED", "dos-app ref did not resolve to a commit"
            )
        dirty = self._git(
            target, "status", "--porcelain", label="dos-app dirty-state preflight"
        ).stdout.strip()
        if dirty:
            raise CanaryError("CANARY_PREFLIGHT_FAILED", "dos-app checkout is dirty")
        remote_url = self._git(
            target, "remote", "get-url", "origin", label="resolve dos-app origin"
        ).stdout.strip()
        repository = config.repository or _parse_repository(remote_url)
        if repository is None or not REPO_RE.fullmatch(repository):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "GitHub repository could not be derived; pass --repository owner/name",
            )

        active = self._run(
            [
                str(target / "sc"),
                "sql",
                "-json",
                (
                    "SELECT sprint_id,lifecycle FROM sprints "
                    "WHERE lifecycle IN ('prepared','armed','paused') "
                    "ORDER BY sprint_id;"
                ),
            ],
            cwd=target,
            label="live dos-app Sprint preflight",
        )
        active_rows = _json_output(active, label="live dos-app Sprint preflight")
        if active_rows:
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "live dos-app has an active or paused Sprint",
                details={"sprints": active_rows},
            )

        self._run(["gh", "auth", "status"], label="GitHub authentication preflight")
        rate = _json_output(
            self._run(["gh", "api", "rate_limit"], label="GitHub capacity preflight"),
            label="GitHub capacity preflight",
        )
        try:
            remaining = int(rate["resources"]["core"]["remaining"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED", "GitHub rate-limit response is incomplete"
            ) from exc
        if remaining < MIN_GITHUB_REMAINING:
            raise CanaryError(
                "CANARY_CAPACITY_FAILED",
                "insufficient GitHub API capacity",
                details={"remaining": remaining, "required": MIN_GITHUB_REMAINING},
            )
        self._run(["docker", "info"], label="Docker capacity preflight")
        image = self._run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "sc.engine_ref" }}',
                "super-coder-sandbox",
            ],
            label="sandbox image preflight",
        ).stdout.strip()
        if image != candidate:
            raise CanaryError(
                "CANARY_EXACT_REF_MISMATCH",
                "sandbox image label does not match the candidate",
                details={"expected": candidate, "actual": image or None},
            )
        codex_auth = Path.home() / ".codex" / "auth.json"
        kimi_auth = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
        if not _nonempty_file(codex_auth) or (
            config.profile == STANDARD_PROFILE and not _nonempty_file(kimi_auth)
        ):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                (
                    "Codex host credentials must be present"
                    if config.profile == DEEPSEEK_SPRINT_PROFILE
                    else "Codex and Kimi host credentials must both be present"
                ),
            )
        if config.profile == DEEPSEEK_SPRINT_PROFILE:
            self._validate_credential_file(config.credential_file)

        base_branch = f"{REMOTE_PREFIX}/{config.run_id}/base"
        head_branch = f"{REMOTE_PREFIX}/{config.run_id}/head"
        for branch in (base_branch, head_branch):
            collision = self._git(
                target,
                "ls-remote",
                "--exit-code",
                "--heads",
                "origin",
                f"refs/heads/{branch}",
                label=f"remote branch collision preflight ({branch})",
                check=False,
            )
            if collision.returncode == 0:
                raise CanaryError("CANARY_COLLISION", f"remote branch exists: {branch}")
            if collision.returncode not in {2}:
                raise CanaryError(
                    "CANARY_PREFLIGHT_FAILED",
                    f"could not prove remote branch absence: {branch}",
                )
        pr_rows = _json_output(
            self._run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "all",
                    "--head",
                    head_branch,
                    "--json",
                    "number,state",
                ],
                label="pull-request collision preflight",
            ),
            label="pull-request collision preflight",
        )
        if pr_rows:
            raise CanaryError("CANARY_COLLISION", "canary pull request already exists")
        container = f"sc-{workspace.name}"
        network = f"sc-canary-{config.run_id}"
        for kind, argv in (
            (
                "container",
                ["docker", "container", "inspect", container],
            ),
            ("network", ["docker", "network", "inspect", network]),
        ):
            result = self._run(argv, label=f"{kind} collision preflight", check=False)
            if result.returncode == 0:
                raise CanaryError("CANARY_COLLISION", f"Docker {kind} exists")
        api_port, dev_port = self._reserve_ports()
        return Preflight(
            candidate_sha=candidate,
            base_sha=base_sha,
            repository=repository,
            remote_url=remote_url,
            workspace=workspace,
            base_branch=base_branch,
            head_branch=head_branch,
            container=container,
            network=network,
            api_port=api_port,
            dev_port=dev_port,
            github_remaining=remaining,
        )

    def create_disposable(
        self,
        config: CanaryConfig,
        facts: Preflight,
        ledger: ResourceLedger,
        checkpoint: Callable[[], None],
    ) -> None:
        self._run(
            [
                "git",
                "clone",
                "--no-local",
                "--no-checkout",
                str(config.dos_app_repo.resolve()),
                str(facts.workspace),
            ],
            label="clone disposable dos-app",
        )
        ledger.workspace = str(facts.workspace)
        marker = facts.workspace / ".git" / "subfloor-canary-marker.json"
        marker.write_text(
            json.dumps(
                {"run_id": config.run_id, "candidate_sha": facts.candidate_sha},
                sort_keys=True,
            )
            + "\n"
        )
        ledger.marker_written = True
        ledger.candidate_sha = facts.candidate_sha
        ledger.base_branch = facts.base_branch
        ledger.head_branch = facts.head_branch
        ledger.repository = facts.repository
        ledger.container = facts.container
        ledger.network = facts.network
        checkpoint()
        self._git(
            facts.workspace,
            "remote",
            "set-url",
            "origin",
            facts.remote_url,
            label="point disposable origin at GitHub",
        )
        self._git(
            facts.workspace,
            "checkout",
            "--detach",
            facts.base_sha,
            label="checkout exact dos-app base",
        )
        self._git(
            facts.workspace,
            "remote",
            "add",
            ENGINE_REMOTE,
            str(config.source_repo.resolve()),
            label="add exact engine source",
        )
        self._git(
            facts.workspace,
            "fetch",
            "--no-tags",
            ENGINE_REMOTE,
            f"{facts.candidate_sha}:refs/remotes/{ENGINE_REMOTE}/main",
            label="fetch exact candidate engine",
        )
        self._git(
            facts.workspace,
            "checkout",
            f"refs/remotes/{ENGINE_REMOTE}/main",
            "--",
            ".super-coder",
            "sc",
            label="materialize exact candidate engine",
        )
        self._run(
            [
                "python3",
                ".super-coder/scripts/install.py",
                "--force",
                "--skip-harness-install",
                "--username",
                "canary",
            ],
            cwd=facts.workspace,
            label="install disposable fork",
        )
        pin = (facts.workspace / ".sc-state" / "engine.ref").read_text().strip()
        if pin != facts.candidate_sha:
            raise CanaryError(
                "CANARY_EXACT_REF_MISMATCH",
                "installed engine pin does not match the candidate",
                details={"expected": facts.candidate_sha, "actual": pin},
            )
        callable_ref = self._run(
            [str(facts.workspace / "sc"), "engine-ref"],
            cwd=facts.workspace,
            label="verify callable exact engine ref",
        ).stdout.strip()
        if callable_ref != facts.candidate_sha:
            raise CanaryError(
                "CANARY_EXACT_REF_MISMATCH",
                "callable dispatcher does not match the candidate",
            )
        instance_path = facts.workspace / ".super-coder" / "instance.json"
        instance = json.loads(instance_path.read_text())
        instance.update(
            {
                "repo": facts.workspace.name,
                "port": facts.api_port,
                "dev_port": facts.dev_port,
                "harness": "codex",
            }
        )
        instance_path.write_text(json.dumps(instance, indent=2, sort_keys=True) + "\n")
        self._git(
            facts.workspace,
            "checkout",
            "-b",
            facts.base_branch,
            label="create local ephemeral base",
        )
        status = self._git(
            facts.workspace,
            "status",
            "--porcelain=v1",
            label="inspect ephemeral base preparation",
        ).stdout
        changed_paths: set[str] = set()
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].split(" -> ")[-1]
            changed_paths.add(path)
        unexpected = sorted(changed_paths - FORK_PREPARATION_PATHS)
        if unexpected:
            raise CanaryError(
                "CANARY_BASE_PREPARATION_INVALID",
                "install changed a non-engine-managed fork path",
                details={"paths": unexpected},
            )
        if changed_paths:
            ordered_paths = sorted(changed_paths)
            self._git(
                facts.workspace,
                "add",
                "--",
                *ordered_paths,
                label="stage ephemeral base preparation",
            )
            self._git(
                facts.workspace,
                "-c",
                "user.name=Subfloor Canary",
                "-c",
                "user.email=noreply@subfloor.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                f"chore: prepare exact engine {facts.candidate_sha[:12]}",
                label="commit ephemeral base preparation",
            )
        self._infra_git(
            facts.workspace,
            "push",
            "origin",
            f"HEAD:refs/heads/{facts.base_branch}",
            label="create ephemeral base branch",
        )
        checkpoint()

    def launch(
        self, config: CanaryConfig, facts: Preflight, ledger: ResourceLedger
    ) -> dict[str, Any]:
        self._release_ports()

        def start_runtime(env: Mapping[str, str], *, label: str) -> None:
            self._run(
                [str(facts.workspace / "sc"), "launch", "--no-build"],
                cwd=facts.workspace,
                env=env,
                label=label,
            )
            api = JsonHttp(f"http://127.0.0.1:{facts.api_port}", self.deadline)
            while True:
                try:
                    health = api.request("GET", "/api/health")
                    if health:
                        return
                except CanaryError:
                    pass
                self.deadline.remaining()
                self.sleep(min(config.poll_interval_s, self.deadline.remaining()))

        nonsecret_env = self._runtime_env(facts)
        start_runtime(nonsecret_env, label="launch non-secret route probe")
        self._run(
            ["docker", "exec", facts.container, "./sc", "models", "refresh"],
            label="refresh exact-image model routes",
        )
        codex_resolution = _json_output(
            self._run(
                [
                    "docker",
                    "exec",
                    facts.container,
                    "./sc",
                    "models",
                    "resolve",
                    "codex",
                    "gpt-5.6-terra",
                    "--json",
                ],
                label="resolve exact-image codex route",
            ),
            label="resolve exact-image codex route",
        )
        if (
            not isinstance(codex_resolution, dict)
            or codex_resolution.get("ok") is not True
        ):
            raise CanaryError(
                "CANARY_ROUTE_NOT_CANONICAL",
                "exact-image codex route did not resolve",
                details={"harness": "codex", "selector": "gpt-5.6-terra"},
            )
        self._run(
            [str(facts.workspace / "sc"), "down"],
            cwd=facts.workspace,
            env=nonsecret_env,
            label="stop non-secret route probe",
        )
        stopped = self._run(
            ["docker", "container", "inspect", facts.container],
            check=False,
            label="verify non-secret route probe stopped",
        )
        if stopped.returncode == 0:
            raise CanaryError(
                "CANARY_CLEANUP_FAILED",
                "non-secret route probe container remained after stop",
            )

        if config.profile == DEEPSEEK_SPRINT_PROFILE:
            self._provider_key = "sc-loopback-restart-rehearsal-only"
            rehearsal_env = self._runtime_env(facts)
            try:
                start_runtime(
                    rehearsal_env,
                    label="launch credential-free exact-session rehearsal",
                )
                restart_rehearsal = self._credential_free_restart_rehearsal(
                    JsonHttp(f"http://127.0.0.1:{facts.api_port}", self.deadline),
                    config,
                    facts,
                    rehearsal_env,
                )
                self._run(
                    [str(facts.workspace / "sc"), "down"],
                    cwd=facts.workspace,
                    env=rehearsal_env,
                    label="stop credential-free exact-session rehearsal",
                )
                stopped = self._run(
                    ["docker", "container", "inspect", facts.container],
                    check=False,
                    label="verify exact-session rehearsal stopped",
                )
                if stopped.returncode == 0:
                    raise CanaryError(
                        "CANARY_CLEANUP_FAILED",
                        "exact-session rehearsal container remained after stop",
                    )
            finally:
                self._provider_key = None
            self._provider_key = self._read_provider_key(config.credential_file)
        else:
            restart_rehearsal = None
        env = self._runtime_env(facts)
        start_runtime(env, label="launch isolated runtime")
        if config.profile == DEEPSEEK_SPRINT_PROFILE:
            self._run(
                ["docker", "exec", facts.container, "./sc", "models", "refresh"],
                check=False,
                label="refresh admitted deepseek route",
            )
            route_admission = _validated_route_admission(
                self._run(
                    [
                        "docker",
                        "exec",
                        facts.container,
                        "./sc",
                        "models",
                        "resolve",
                        "deepseek",
                        DEEPSEEK_MODEL,
                        "--sprint-admission-json",
                    ],
                    check=False,
                    label="resolve admitted deepseek route",
                )
            )
            if not route_admission["admitted"]:
                raise CanaryError(
                    "CANARY_ROUTE_NOT_ADMITTED",
                    "exact DeepSeek Sprint route was not admitted",
                    details={"route_admission": route_admission},
                )
        else:
            route_admission = None
        status = self._run(
            [str(facts.workspace / "sc"), "harness-status"],
            cwd=facts.workspace,
            env=env,
            label="inspect launched harness versions",
        ).stdout
        versions: dict[str, str] = {}
        for line in status.splitlines():
            match = re.match(
                r"\s*(claude|codex|deepseek|kimi|opencode)\s+(.+?)\s*$", line
            )
            if match:
                versions[match.group(1)] = redact_text(match.group(2))[:160]
        required = (
            {"codex", "deepseek"}
            if config.profile == DEEPSEEK_SPRINT_PROFILE
            else {"codex", "kimi"}
        )
        if not required.issubset(versions):
            raise CanaryError(
                "CANARY_RUNTIME_PROVENANCE_MISSING",
                "launched runtime did not report every profile harness version",
            )
        return {
            "versions": versions,
            "route_admission": route_admission,
            "restart_rehearsal": restart_rehearsal,
        }

    def _restart_helper(
        self,
        facts: Preflight,
        *args: str,
        label: str,
    ) -> CommandResult:
        return self._run(
            [
                "docker",
                "exec",
                facts.container,
                "python3",
                RESTART_REHEARSAL_HELPER,
                *args,
            ],
            check=False,
            label=label,
        )

    def _start_restart_provider(self, facts: Preflight) -> None:
        launched = self._run(
            [
                "docker",
                "exec",
                "--detach",
                facts.container,
                "python3",
                RESTART_REHEARSAL_HELPER,
                "provider",
                "--port",
                str(RESTART_REHEARSAL_PORT),
            ],
            check=False,
            label="start credential-free loopback provider",
        )
        if launched.returncode != 0:
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "credential-free loopback provider did not start",
                details={"restart": {"schema_version": 1, "category": "unknown"}},
            )
        for _attempt in range(30):
            probe = self._restart_helper(
                facts,
                "provider-probe",
                "--port",
                str(RESTART_REHEARSAL_PORT),
                label="probe credential-free loopback provider",
            )
            try:
                payload = json.loads(probe.stdout or "null")
            except json.JSONDecodeError:
                payload = None
            if probe.returncode == 0 and payload == {"ok": True}:
                return
            self.deadline.remaining()
            self.sleep(min(0.1, self.deadline.remaining()))
        raise CanaryError(
            "CANARY_RESTART_RECOVERY_FAILED",
            "credential-free loopback provider did not become ready",
            details={"restart": {"schema_version": 1, "category": "unknown"}},
        )

    def _restart_probe(
        self,
        facts: Preflight,
        conversation_id: str,
        *,
        native: bool,
    ) -> dict[str, Any]:
        args = ["probe", "--conversation", conversation_id]
        if native:
            args.append("--native")
        return _validated_restart_probe(
            self._restart_helper(facts, *args, label="read bounded restart evidence")
        )

    def _wait_health(self, api: JsonHttp, config: CanaryConfig) -> None:
        while True:
            try:
                if api.request("GET", "/api/health"):
                    return
            except CanaryError:
                pass
            try:
                self.deadline.remaining()
            except CanaryError as exc:
                raise CanaryError(
                    "CANARY_RESTART_RECOVERY_FAILED",
                    "restarted runtime did not become healthy before its deadline",
                    details={
                        "restart": {
                            "schema_version": 1,
                            "category": "restart-timeout",
                        }
                    },
                ) from exc
            self.sleep(min(config.poll_interval_s, self.deadline.remaining()))

    def _run_exact_restart(
        self,
        facts: Preflight,
        env: Mapping[str, str],
    ) -> None:
        try:
            result = self._run(
                [str(facts.workspace / "sc"), "restart", "--yes", "--no-build"],
                cwd=facts.workspace,
                env=env,
                check=False,
                label="restart exact DeepSeek canary runtime",
            )
        except CanaryError as exc:
            if exc.code != "CANARY_COMMAND_TIMEOUT":
                raise
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "exact-session restart exceeded its deadline",
                details={"restart": {"schema_version": 1, "category": "restart-timeout"}},
            ) from exc
        category = _restart_command_category(result)
        if category is not None:
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "exact-session restart command was rejected",
                details={"restart": {"schema_version": 1, "category": category}},
            )

    def _credential_free_restart_rehearsal(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        facts: Preflight,
        env: Mapping[str, str],
    ) -> dict[str, Any]:
        self._start_restart_provider(facts)
        prepared = self._restart_helper(
            facts,
            "prepare",
            "--shortname",
            "REV1",
            "--endpoint",
            f"http://127.0.0.1:{RESTART_REHEARSAL_PORT}/v1",
            label="prepare credential-free exact-session rehearsal",
        )
        try:
            payload = json.loads(prepared.stdout or "null")
        except json.JSONDecodeError:
            payload = None
        conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
        if (
            prepared.returncode != 0
            or not isinstance(payload, dict)
            or set(payload)
            != {"ok", "candidate_sha", "conversation_id", "provider", "model", "binding_digest"}
            or payload.get("ok") is not True
            or payload.get("candidate_sha") != facts.candidate_sha
            or payload.get("provider") != "ollama-cloud"
            or payload.get("model") != DEEPSEEK_MODEL
            or not isinstance(conversation_id, str)
            or CONVERSATION_ID_RE.fullmatch(conversation_id) is None
        ):
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "credential-free restart fixture violated its bounded contract",
                details={"restart": {"schema_version": 1, "category": "unknown"}},
            )
        try:
            self._message(
                api,
                conversation_id,
                "Reply with only restart-rehearsal-before.",
                f"{config.run_id}:restart-rehearsal:before",
            )
            self._wait_idle(api, conversation_id, config, facts)
            before = self._restart_probe(facts, conversation_id, native=False)
            self._run_exact_restart(facts, env)
            self._wait_health(api, config)
            self._start_restart_provider(facts)
            self._message(
                api,
                conversation_id,
                "Resume this exact native session; reply with only restart-rehearsal-after.",
                f"{config.run_id}:restart-rehearsal:after",
            )
            self._wait_idle(api, conversation_id, config, facts)
            after = self._restart_probe(facts, conversation_id, native=True)
            result = _restart_result(
                before,
                after,
                command_exit_class="success",
                command_exit_status=0,
            )
            if not _restart_passed(result):
                raise CanaryError(
                    "CANARY_RESTART_RECOVERY_FAILED",
                    "credential-free exact-session rehearsal did not preserve identity",
                    details={"restart": result},
                )
            return result
        finally:
            cleaned = self._restart_helper(
                facts,
                "cleanup",
                "--conversation",
                conversation_id,
                label="clean credential-free exact-session rehearsal",
            )
            try:
                cleanup_payload = json.loads(cleaned.stdout or "null")
            except json.JSONDecodeError:
                cleanup_payload = None
            if cleaned.returncode != 0 or cleanup_payload != {
                "conversation_removed": True,
                "ok": True,
                "root_removed": True,
            }:
                raise CanaryError(
                    "CANARY_CLEANUP_FAILED",
                    "credential-free exact-session rehearsal cleanup was incomplete",
                )

    def _wait_idle(
        self,
        api: JsonHttp,
        conversation_id: str,
        config: CanaryConfig,
        facts: Preflight,
    ) -> dict[str, Any]:
        while True:
            conversation = api.request("GET", f"/api/conversations/{conversation_id}")
            if conversation.get("state") == "idle":
                messages = api.request(
                    "GET", f"/api/conversations/{conversation_id}/messages?limit=1"
                )
                items = messages.get("items") or []
                if items and items[-1].get("state") == "completed":
                    return {
                        "conversation_id": conversation_id,
                        "state": "idle",
                        "version": conversation.get("version"),
                    }
            if conversation.get("state") in {"error", "closed"}:
                raise CanaryError(
                    "CANARY_PARTICIPANT_FAILED",
                    "participant conversation terminalized before completing",
                    details={
                        "conversation_id": conversation_id,
                        "state": conversation.get("state"),
                        "failure": self._conversation_failure_evidence(
                            facts, conversation_id
                        ),
                    },
                )
            self.deadline.remaining()
            self.sleep(min(config.poll_interval_s, self.deadline.remaining()))

    def _conversation_failure_evidence(
        self, facts: Preflight, conversation_id: str
    ) -> dict[str, Any]:
        if not CONVERSATION_ID_RE.fullmatch(conversation_id):
            return {"diagnostic": "invalid_conversation_id"}
        query = (
            "SELECT state,error_code,error_detail FROM conversation_runs "
            f"WHERE conversation_id='{conversation_id}' "
            "ORDER BY run_id DESC LIMIT 1;"
        )
        result = self._run(
            ["docker", "exec", facts.container, "./sc", "sql", "-json", query],
            check=False,
            label="read bounded participant failure",
        )
        if result.returncode != 0:
            return {"diagnostic": "unavailable"}
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return {"diagnostic": "invalid"}
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return {"diagnostic": "absent"}
        row = rows[0]
        error_code = str(row.get("error_code") or "")
        try:
            evidence = json.loads(str(row.get("error_detail") or ""))
        except json.JSONDecodeError:
            return {
                "run_state": row.get("state"),
                "error_code": error_code or None,
                "diagnostic": "structured_evidence_invalid",
            }
        valid = (
            isinstance(evidence, dict)
            and set(evidence) == PARTICIPANT_FAILURE_KEYS
            and evidence.get("schema_version") == 1
            and evidence.get("source") in PARTICIPANT_FAILURE_SOURCES
            and evidence.get("phase") in PARTICIPANT_FAILURE_PHASES
            and evidence.get("category") in PARTICIPANT_FAILURE_CATEGORIES
            and evidence.get("purpose") in PARTICIPANT_FAILURE_PURPOSES
            and (
                evidence.get("upstream_code") is None
                or evidence.get("upstream_code") in PARTICIPANT_UPSTREAM_CODES
                or (
                    isinstance(evidence.get("upstream_code"), str)
                    and PARTICIPANT_HTTP_CODE.fullmatch(
                        evidence["upstream_code"]
                    )
                    is not None
                )
            )
            and (
                evidence.get("http_status") is None
                or isinstance(evidence.get("http_status"), int)
                and not isinstance(evidence.get("http_status"), bool)
                and 100 <= evidence["http_status"] <= 599
            )
            and all(
                isinstance(evidence.get(key), bool)
                for key in (
                    "provider_request_observed",
                    "provider_exact",
                    "model_exact",
                    "reserved_default_omitted",
                    "shell_tool_declared",
                )
            )
        )
        if not valid:
            return {
                "run_state": row.get("state"),
                "error_code": error_code or None,
                "diagnostic": "structured_evidence_mismatch",
            }
        return {
            "run_state": row.get("state"),
            "error_code": error_code or None,
            **evidence,
        }

    @staticmethod
    def _shells(payload: dict[str, Any]) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in payload.get("shells") or []:
            shortname = row.get("shortname")
            shell_id = row.get("shell_id")
            if isinstance(shortname, str) and isinstance(shell_id, int):
                result[shortname.upper()] = shell_id
        return result

    @staticmethod
    def _feature(payload: dict[str, Any], title: str) -> dict[str, Any] | None:
        for bucket in payload.get("buckets") or []:
            for feature in bucket.get("features") or []:
                if feature.get("title") == title:
                    return feature
        return None

    def _approval(self, facts: Preflight, document_id: int) -> int:
        query = (
            "SELECT approval_id FROM sprint_spec_approvals "
            f"WHERE document_id={document_id} AND verdict='pass' "
            "ORDER BY approval_id DESC LIMIT 1;"
        )
        result = self._run(
            [
                "docker",
                "exec",
                facts.container,
                "./sc",
                "sql",
                "-json",
                query,
            ],
            label="read reviewed spec approval",
        )
        rows = _json_output(result, label="read reviewed spec approval")
        if not rows or not isinstance(rows[0].get("approval_id"), int):
            raise CanaryError("CANARY_QAQC_FAILED", "reviewer did not record approval")
        return int(rows[0]["approval_id"])

    @staticmethod
    def _qaqc_invocation(arguments: Any, document_id: int) -> bool:
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError:
                decoded = {"cmd": arguments}
        elif isinstance(arguments, dict):
            decoded = arguments
        else:
            return False
        command = decoded.get("cmd") or decoded.get("command")
        if not isinstance(command, str):
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return argv in (
            [
                "./sc",
                "sprint",
                "record-qaqc",
                "--document",
                str(document_id),
                "--verdict",
                "pass",
            ],
            [
                "sc",
                "sprint",
                "record-qaqc",
                "--document",
                str(document_id),
                "--verdict",
                "pass",
            ],
        )

    @staticmethod
    def _qaqc_receipt_values(value: Any) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []

        def visit(item: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(item, dict):
                if {
                    "approval_id",
                    "revision_sha256",
                    "verdict",
                    "created",
                }.issubset(item):
                    receipts.append(item)
                for child in item.values():
                    visit(child, depth + 1)
                return
            if isinstance(item, list):
                for child in item[:128]:
                    visit(child, depth + 1)
                return
            if not isinstance(item, str) or len(item.encode()) > 65536:
                return
            try:
                decoded = json.loads(item)
            except json.JSONDecodeError:
                return
            if decoded != item:
                visit(decoded, depth + 1)

        visit(value)
        return receipts

    @staticmethod
    def _validate_qaqc_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        if set(evidence) != QAQC_EVIDENCE_KEYS:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC evidence escaped its fixed top-level allowlist",
            )
        boot = evidence.get("boot")
        command = evidence.get("record_qaqc")
        action_receipt = evidence.get("action_receipt")
        if not isinstance(boot, dict) or set(boot) != QAQC_BOOT_KEYS:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC boot evidence escaped its fixed allowlist",
            )
        if not isinstance(command, dict) or set(command) != QAQC_COMMAND_KEYS:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC command evidence escaped its fixed allowlist",
            )
        if (
            not isinstance(action_receipt, dict)
            or set(action_receipt) != QAQC_ACTION_RECEIPT_KEYS
        ):
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC action receipt evidence escaped its fixed allowlist",
            )
        if evidence.get("terminal") not in QAQC_TERMINAL_CLASSES:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC terminal evidence is not a bounded category",
            )
        if command.get("exit_class") not in QAQC_EXIT_CLASSES:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC command exit evidence is not a bounded category",
            )
        if evidence.get("postcondition") not in QAQC_POSTCONDITIONS:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC postcondition evidence is not a bounded category",
            )
        if any(value not in {"resolved", "missing", "mismatch"} for value in boot.values()):
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC boot evidence is not a bounded category",
            )
        if not isinstance(command.get("observed"), bool):
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC observation evidence is not boolean",
            )
        count = command.get("invocation_count")
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 64:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC invocation count is outside its bound",
            )
        if not isinstance(command.get("receipt"), bool):
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC receipt evidence is not boolean",
            )
        if command.get("identity") not in {"matched", "mismatch", "absent"}:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC receipt identity is not a bounded category",
            )
        receipt_count = action_receipt.get("count")
        if (
            not isinstance(receipt_count, int)
            or isinstance(receipt_count, bool)
            or not 0 <= receipt_count <= 64
        ):
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC action receipt count is outside its bound",
            )
        if action_receipt.get("identity") not in QAQC_ACTION_RECEIPT_IDENTITIES:
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "QA/QC action receipt identity is not a bounded category",
            )
        return evidence

    @staticmethod
    def _qaqc_evidence_passed(evidence: Mapping[str, Any]) -> bool:
        boot = evidence.get("boot")
        action_receipt = evidence.get("action_receipt")
        return (
            isinstance(boot, dict)
            and set(boot) == QAQC_BOOT_KEYS
            and all(value == "resolved" for value in boot.values())
            and evidence.get("terminal") == "succeeded"
            and isinstance(action_receipt, dict)
            and action_receipt.get("count") == 1
            and action_receipt.get("identity") == "matched"
            and evidence.get("postcondition") == "approved"
        )

    def _qaqc_action_evidence(
        self,
        api: JsonHttp,
        facts: Preflight,
        reviewer_id: str,
        *,
        sprint_id: int,
        reviewer_shell_id: int,
        document_id: int,
    ) -> tuple[int | None, dict[str, Any]]:
        if not CONVERSATION_ID_RE.fullmatch(reviewer_id):
            raise CanaryError(
                "CANARY_QAQC_EVIDENCE_INVALID",
                "Reviewer conversation identity is invalid",
            )
        conversation_query = (
            "SELECT c.harness,c.state,c.shell_id,c.worktree,s.flavor,s.shortname,"
            "boot.content_sha256,boot.content_bytes,"
            "instr(boot.content,'sprint_rev')>0 has_sprint_rev,"
            "(SELECT r.state FROM conversation_runs r "
            " WHERE r.conversation_id=c.conversation_id "
            " ORDER BY r.run_id DESC LIMIT 1) terminal_state,"
            "(SELECT r.run_id FROM conversation_runs r "
            " WHERE r.conversation_id=c.conversation_id "
            " ORDER BY r.run_id DESC LIMIT 1) terminal_run_id,"
            "(SELECT archive.session_id FROM conversation_runs r "
            " JOIN shell_memory_archives archive ON archive.archive_id=r.archive_id "
            " WHERE r.conversation_id=c.conversation_id "
            " ORDER BY r.run_id DESC LIMIT 1) terminal_session_id "
            "FROM conversations c JOIN shells s ON s.shell_id=c.shell_id "
            "LEFT JOIN conversation_boot_snapshots boot "
            "ON boot.conversation_id=c.conversation_id "
            f"WHERE c.conversation_id='{reviewer_id}';"
        )
        conversation_rows = _json_output(
            self._run(
                [
                    "docker",
                    "exec",
                    facts.container,
                    "./sc",
                    "sql",
                    "-json",
                    conversation_query,
                ],
                label="read bounded QA/QC boot evidence",
            ),
            label="read bounded QA/QC boot evidence",
        )
        row = conversation_rows[0] if len(conversation_rows) == 1 else None
        if not isinstance(row, dict):
            row = {}

        terminal = str(row.get("terminal_state") or "missing")
        if len(conversation_rows) != 1:
            terminal = "ambiguous"
        elif terminal not in QAQC_TERMINAL_CLASSES:
            terminal = "unknown"

        skill_source = facts.workspace / ".super-coder" / "assets" / "skills" / "sprint_rev" / "SKILL.md"
        composition = facts.workspace / ".super-coder" / "assets" / "deepseek" / "cordis-ollama-cloud.yml"
        engine_ref = facts.workspace / ".sc-state" / "engine.ref"
        try:
            skill_body = skill_source.read_text()
        except (OSError, UnicodeError):
            skill_body = ""
        try:
            composition_body = composition.read_text()
        except (OSError, UnicodeError):
            composition_body = ""
        try:
            installed_ref = engine_ref.read_text().strip()
        except (OSError, UnicodeError):
            installed_ref = ""
        worktree = row.get("worktree")
        candidate_worktree = False
        if isinstance(worktree, str):
            try:
                Path(worktree).resolve().relative_to(facts.workspace.resolve())
            except (OSError, ValueError):
                pass
            else:
                candidate_worktree = True
        boot = {
            "role": (
                "resolved"
                if row.get("shell_id") == reviewer_shell_id
                and row.get("flavor") == "reviewer"
                and str(row.get("shortname") or "").upper() == "REV1"
                else "mismatch"
            ),
            "skill": (
                "resolved"
                if bool(row.get("has_sprint_rev"))
                and isinstance(row.get("content_sha256"), str)
                and len(str(row["content_sha256"])) == 64
                else "missing"
            ),
            "shell_tool": (
                "resolved"
                if row.get("harness") == "deepseek"
                and "- id: bash" in composition_body
                and "@deepseek-ai/dsh-bash-local" in composition_body
                else "missing"
            ),
            "candidate": (
                "resolved"
                if installed_ref == facts.candidate_sha
                and candidate_worktree
                else "mismatch"
            ),
            "predeclaration": (
                "resolved"
                if "pre-declaration QAQC" in skill_body
                and "sc sprint record-qaqc" in skill_body
                else "missing"
            ),
        }

        event_query = (
            "SELECT event_type,payload FROM conversation_events "
            f"WHERE conversation_id='{reviewer_id}' "
            "AND event_type IN ('tool.started','tool.completed') "
            "AND run_id=(SELECT run_id FROM conversation_runs "
            f"WHERE conversation_id='{reviewer_id}' "
            "ORDER BY run_id DESC LIMIT 1) "
            "ORDER BY sequence;"
        )
        event_rows = _json_output(
            self._run(
                [
                    "docker",
                    "exec",
                    facts.container,
                    "./sc",
                    "sql",
                    "-json",
                    event_query,
                ],
                label="inspect bounded QA/QC action events",
            ),
            label="inspect bounded QA/QC action events",
        )
        tool_started: list[tuple[str, dict[str, Any]]] = []
        completed: dict[str, list[dict[str, Any]]] = {}
        for event in event_rows if isinstance(event_rows, list) else []:
            if not isinstance(event, dict):
                continue
            try:
                payload = json.loads(str(event.get("payload") or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            tool_ref = payload.get("tool_ref")
            if not isinstance(tool_ref, str) or not tool_ref:
                continue
            if event.get("event_type") == "tool.started":
                tool_started.append((tool_ref, payload))
            elif event.get("event_type") == "tool.completed":
                completed.setdefault(tool_ref, []).append(payload)

        approval_query = (
            "SELECT a.approval_id,a.document_id,a.revision_sha256,"
            "a.reviewer_shell_id,a.verdict,d.body FROM documents d "
            "LEFT JOIN sprint_spec_approvals a ON a.document_id=d.document_id "
            f"WHERE d.document_id={document_id} ORDER BY a.approval_id;"
        )
        approval_rows = _json_output(
            self._run(
                [
                    "docker",
                    "exec",
                    facts.container,
                    "./sc",
                    "sql",
                    "-json",
                    approval_query,
                ],
                label="read bounded QA/QC durable postcondition",
            ),
            label="read bounded QA/QC durable postcondition",
        )
        document_rows = [row for row in approval_rows if isinstance(row, dict)]
        document_body = document_rows[0].get("body") if document_rows else None
        revision = (
            hashlib.sha256(document_body.encode()).hexdigest()
            if isinstance(document_body, str)
            else None
        )
        approvals = [row for row in document_rows if isinstance(row.get("approval_id"), int)]
        exact = [
            row
            for row in approvals
            if row.get("reviewer_shell_id") == reviewer_shell_id
            and row.get("revision_sha256") == revision
            and row.get("verdict") == "pass"
        ]
        if len(exact) == 1:
            postcondition = "approved"
            approval_id = int(exact[0]["approval_id"])
        elif len(exact) > 1:
            postcondition = "ambiguous"
            approval_id = None
        elif any(row.get("reviewer_shell_id") != reviewer_shell_id for row in approvals):
            postcondition = "reviewer_mismatch"
            approval_id = None
        elif any(row.get("revision_sha256") != revision for row in approvals):
            postcondition = "revision_mismatch"
            approval_id = None
        elif any(row.get("verdict") != "pass" for row in approvals):
            postcondition = "verdict_mismatch"
            approval_id = None
        else:
            postcondition = "absent"
            approval_id = None

        binding_query = (
            "SELECT sprint.conversation_generation,participant.participant_id "
            "FROM sprints sprint JOIN sprint_participants participant "
            "ON participant.sprint_id=sprint.sprint_id "
            f"WHERE sprint.sprint_id={sprint_id} "
            f"AND participant.shell_id={reviewer_shell_id} "
            "AND participant.role='reviewer';"
        )
        binding_rows = _json_output(
            self._run(
                [
                    "docker",
                    "exec",
                    facts.container,
                    "./sc",
                    "sql",
                    "-json",
                    binding_query,
                ],
                label="read bounded QA/QC participant binding",
            ),
            label="read bounded QA/QC participant binding",
        )
        binding = binding_rows[0] if len(binding_rows) == 1 else {}
        events_page = api.request(
            "GET", f"/api/sprints/{sprint_id}/events?limit=100"
        )
        receipt_events = [
            item
            for item in events_page.get("items") or []
            if isinstance(item, dict) and item.get("type") == "qaqc.action_recorded"
        ]
        receipt_count = min(len(receipt_events), 64)
        if not receipt_events:
            action_identity = "absent"
        elif len(receipt_events) != 1:
            action_identity = "duplicate"
        else:
            event = receipt_events[0]
            details = event.get("details")
            actor = event.get("actor")
            required_receipt_keys = {
                "action_kind",
                "sprint_id",
                "participant_id",
                "reviewer_shell_id",
                "role",
                "assignment_generation",
                "conversation_id",
                "session_id",
                "run_id",
                "candidate_sha",
                "document_id",
                "revision_sha256",
                "review_phase",
                "approval_id",
                "approval_created",
            }
            if (
                not isinstance(details, dict)
                or set(details) != required_receipt_keys
                or not isinstance(actor, dict)
                or details.get("action_kind") != "record-qaqc"
            ):
                action_identity = "malformed"
            elif details.get("sprint_id") != sprint_id:
                action_identity = "sprint_mismatch"
            elif details.get("participant_id") != binding.get("participant_id"):
                action_identity = "participant_mismatch"
            elif (
                details.get("reviewer_shell_id") != reviewer_shell_id
                or actor.get("shell_id") != reviewer_shell_id
                or actor.get("kind") != "participant"
            ):
                action_identity = "shell_mismatch"
            elif details.get("role") != "reviewer":
                action_identity = "role_mismatch"
            elif details.get("assignment_generation") != binding.get(
                "conversation_generation"
            ):
                action_identity = "generation_mismatch"
            elif details.get("conversation_id") != reviewer_id:
                action_identity = "conversation_mismatch"
            elif details.get("session_id") != row.get("terminal_session_id"):
                action_identity = "session_mismatch"
            elif details.get("run_id") != row.get("terminal_run_id"):
                action_identity = "run_mismatch"
            elif details.get("candidate_sha") != facts.candidate_sha:
                action_identity = "candidate_mismatch"
            elif (
                details.get("document_id") != document_id
                or details.get("revision_sha256") != revision
            ):
                action_identity = "spec_mismatch"
            elif details.get("review_phase") != "pre-arm-qaqc":
                action_identity = "phase_mismatch"
            elif approval_id is None or details.get("approval_id") != approval_id:
                action_identity = "approval_mismatch"
            elif details.get("approval_created") is not True:
                action_identity = "row_mismatch"
            else:
                action_identity = "matched"

        receipt_matches_by_ref: dict[str, list[dict[str, Any]]] = {}
        for tool_ref, _payload in tool_started:
            receipts = [
                receipt
                for completion in completed.get(tool_ref, [])
                for receipt in self._qaqc_receipt_values(completion)
                if approval_id is not None
                and receipt.get("approval_id") == approval_id
                and receipt.get("revision_sha256") == revision
                and receipt.get("verdict") == "pass"
                and isinstance(receipt.get("created"), bool)
            ]
            if receipts:
                receipt_matches_by_ref[tool_ref] = receipts
        started = [
            (tool_ref, payload)
            for tool_ref, payload in tool_started
            if self._qaqc_invocation(payload.get("arguments"), document_id)
            or tool_ref in receipt_matches_by_ref
        ]
        completions = [
            item
            for tool_ref, _payload in started
            for item in completed.get(tool_ref, [])
        ]
        statuses = {str(item.get("status") or "") for item in completions}
        if not started:
            exit_class = "not_invoked"
        elif any(len(completed.get(tool_ref, [])) != 1 for tool_ref, _payload in started):
            exit_class = "missing_completion" if not completions else "ambiguous"
        elif statuses == {"completed"}:
            exit_class = "success"
        elif statuses == {"failed"}:
            exit_class = "failure"
        else:
            exit_class = "ambiguous"

        returned_receipts = [
            receipt
            for completion in completions
            for receipt in self._qaqc_receipt_values(completion)
        ]
        matched_receipts = [
            receipt
            for tool_ref, _payload in started
            for receipt in receipt_matches_by_ref.get(tool_ref, [])
        ]
        identity = (
            "matched"
            if len(matched_receipts) == 1
            else "mismatch" if returned_receipts else "absent"
        )
        evidence = {
            "boot": boot,
            "terminal": terminal,
            "record_qaqc": {
                "observed": bool(started),
                "invocation_count": min(len(started), 64),
                "exit_class": exit_class,
                "receipt": bool(returned_receipts),
                "identity": identity,
            },
            "action_receipt": {
                "count": receipt_count,
                "identity": action_identity,
            },
            "postcondition": postcondition,
        }
        return approval_id, self._validate_qaqc_evidence(evidence)

    @staticmethod
    def _qaqc_reviewer_prompt(document_id: int) -> str:
        return (
            "Load sprint_rev and use its explicit pre-declaration QA/QC path; "
            "there is no Sprint id or Sprint inbox yet. "
            f"Review spec document #{document_id} as the canary QA/QC Reviewer. "
            "Confirm it is limited to a deterministic file, an ephemeral-base PR, real "
            "Sprint lifecycle actions, and no change to main. If sound, run exactly "
            f"./sc sprint record-qaqc --document {document_id} --verdict pass. "
            "Verify that command confirms the durable approval, retry the exact command "
            "if the write is failed or ambiguous, and stop only after confirmation."
        )

    def _create_conversation(
        self,
        api: JsonHttp,
        *,
        shell_id: int,
        harness: str,
        model: str | None = None,
        effort: str | None = None,
        key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"shell_id": shell_id, "harness": harness}
        if model is not None:
            body["model"] = model
        if effort is not None:
            body["effort"] = effort
        conversation = api.request(
            "POST",
            "/api/conversations",
            body=body,
            key=key,
        )
        projected_route = conversation.get("route") or {}
        route = {
            field: projected_route.get(field)
            for field in ("harness", "provider", "model", "effort")
        }
        if not all(isinstance(route[field], str) and route[field] for field in route):
            raise CanaryError(
                "CANARY_ROUTE_NOT_CANONICAL",
                "browser conversation route contains a nullable field",
                details={"harness": harness},
            )
        return conversation

    def _conversation_evidence(
        self,
        facts: Preflight,
        conversation_id: str,
    ) -> dict[str, Any]:
        if not CONVERSATION_ID_RE.fullmatch(conversation_id):
            raise CanaryError(
                "CANARY_CONVERSATION_INVALID", "conversation identity is invalid"
            )
        query = (
            "SELECT c.conversation_id,c.harness,c.state,c.harness_session_ref,"
            "boot.content_sha256 boot_sha256,boot.content_bytes,"
            "instr(boot.content,'sprint_dev')>0 has_sprint_dev,"
            "instr(boot.content,'sprint_rev')>0 has_sprint_rev,"
            "SUM(CASE WHEN event.event_type='tool.started' THEN 1 ELSE 0 END) "
            "tool_started,"
            "SUM(CASE WHEN event.event_type='tool.completed' THEN 1 ELSE 0 END) "
            "tool_completed,"
            "SUM(CASE WHEN event.event_type IN "
            "('run.completed','run.failed','run.interrupted') THEN 1 ELSE 0 END) "
            "terminal_events FROM conversations c "
            "LEFT JOIN conversation_boot_snapshots boot "
            "ON boot.conversation_id=c.conversation_id "
            "LEFT JOIN conversation_events event "
            "ON event.conversation_id=c.conversation_id "
            f"WHERE c.conversation_id='{conversation_id}' GROUP BY c.conversation_id;"
        )
        rows = _json_output(
            self._run(
                ["docker", "exec", facts.container, "./sc", "sql", "-json", query],
                label="read bounded conversation evidence",
            ),
            label="read bounded conversation evidence",
        )
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise CanaryError(
                "CANARY_CONVERSATION_INVALID",
                "conversation evidence is missing or ambiguous",
            )
        row = rows[0]
        session_ref = row.get("harness_session_ref")
        return {
            "conversation_id": conversation_id,
            "harness": row.get("harness"),
            "state": row.get("state"),
            "session_sha256": (
                hashlib.sha256(session_ref.encode()).hexdigest()
                if isinstance(session_ref, str) and session_ref
                else None
            ),
            "boot_sha256": row.get("boot_sha256"),
            "boot_bytes": row.get("content_bytes"),
            "has_sprint_dev": bool(row.get("has_sprint_dev")),
            "has_sprint_rev": bool(row.get("has_sprint_rev")),
            "tool_started": int(row.get("tool_started") or 0),
            "tool_completed": int(row.get("tool_completed") or 0),
            "terminal_events": int(row.get("terminal_events") or 0),
        }

    def _restart_exact_session(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        facts: Preflight,
        conversation_id: str,
    ) -> dict[str, Any]:
        before = self._restart_probe(facts, conversation_id, native=False)
        if before["native_session_id"] is None:
            result = _restart_result(
                before,
                before,
                command_exit_class="failure",
                command_exit_status=1,
                category="session-reference-missing",
            )
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "DeepSeek session evidence is absent before restart",
                details={"restart": result},
            )
        self._run_exact_restart(facts, self._runtime_env(facts))
        self._wait_health(api, config)
        try:
            self._message(
                api,
                conversation_id,
                (
                    "Resume this exact conversation after the engine restart. Use Bash "
                    "once to run `pwd`, then reply with only exact-session-recovered. "
                    "Pass when the tool completes and this turn becomes idle; do not "
                    "change files."
                ),
                f"{config.run_id}:deepseek:restart-resume",
            )
            self._wait_idle(api, conversation_id, config, facts)
            after = self._restart_probe(facts, conversation_id, native=True)
        except CanaryError as exc:
            if exc.code == "CANARY_RESTART_RECOVERY_FAILED":
                raise
            result = _restart_result(
                before,
                before,
                command_exit_class="success",
                command_exit_status=0,
                category="native-resume-rejected",
            )
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "native exact-session resume did not complete",
                details={"restart": result},
            ) from exc
        result = _restart_result(
            before,
            after,
            command_exit_class="success",
            command_exit_status=0,
        )
        if not _restart_passed(result):
            raise CanaryError(
                "CANARY_RESTART_RECOVERY_FAILED",
                "restart did not preserve the exact native session",
                details={"restart": result},
            )
        return result

    def _deepseek_participant_evidence(
        self,
        facts: Preflight,
        sprint_id: int,
        board: Mapping[str, Any],
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        for role, skill_key in (("developer", "has_sprint_dev"), ("reviewer", "has_sprint_rev")):
            participant = next(
                (
                    row
                    for row in board.get("participants") or []
                    if row.get("role") == role
                ),
                None,
            )
            if (
                not isinstance(participant, dict)
                or participant.get("harness") != "deepseek"
                or participant.get("model") != DEEPSEEK_MODEL
                or participant.get("effort") != "default"
                or not isinstance(participant.get("current_conversation_id"), str)
            ):
                raise CanaryError(
                    "CANARY_PARTICIPANT_EVIDENCE_FAILED",
                    f"{role} is not bound to the admitted DeepSeek route",
                )
            conversation = self._conversation_evidence(
                facts, str(participant["current_conversation_id"])
            )
            if (
                not conversation[skill_key]
                or not isinstance(conversation["boot_sha256"], str)
                or len(conversation["boot_sha256"]) != 64
                or conversation["tool_completed"] < 1
            ):
                raise CanaryError(
                    "CANARY_PARTICIPANT_EVIDENCE_FAILED",
                    f"{role} lacks role skill, boot, or completed-tool evidence",
                )
            evidence[role] = {
                "conversation_id": conversation["conversation_id"],
                "route": {
                    "harness": participant.get("harness"),
                    "provider": "ollama-cloud",
                    "model": participant.get("model"),
                    "effort": participant.get("effort"),
                },
                "boot_sha256": conversation["boot_sha256"],
                "boot_bytes": conversation["boot_bytes"],
                "role_skill_loaded": True,
                "tool_started": conversation["tool_started"],
                "tool_completed": conversation["tool_completed"],
            }
        query = (
            "SELECT COUNT(*) handoffs FROM wake_message message "
            "JOIN sprint_participants sender "
            "ON sender.participant_id=message.from_participant_id "
            f"WHERE message.sprint_id={sprint_id} AND message.intent='handoff' "
            "AND sender.role='developer';"
        )
        rows = _json_output(
            self._run(
                ["docker", "exec", facts.container, "./sc", "sql", "-json", query],
                label="read bounded Sprint handoff evidence",
            ),
            label="read bounded Sprint handoff evidence",
        )
        handoffs = int(rows[0].get("handoffs") or 0) if len(rows) == 1 else 0
        if handoffs != 1:
            raise CanaryError(
                "CANARY_PARTICIPANT_EVIDENCE_FAILED",
                "DeepSeek Developer did not produce exactly one durable handoff",
            )
        evidence["developer_handoffs"] = handoffs
        return evidence

    def _message(
        self,
        api: JsonHttp,
        conversation_id: str,
        body: str,
        key: str,
    ) -> None:
        api.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": body},
            key=key,
        )

    @staticmethod
    def _bounded_board(board: Mapping[str, Any]) -> dict[str, Any]:
        """Keep diagnostic identities/states while excluding message bodies."""
        sprint = board.get("sprint") or {}
        return sanitize(
            {
                "sprint": {
                    key: sprint.get(key)
                    for key in ("sprint_id", "lifecycle", "terminal_outcome")
                },
                "runtime": board.get("runtime"),
                "pickup": board.get("pickup"),
                "participants": [
                    {
                        key: row.get(key)
                        for key in (
                            "participant_id",
                            "shortname",
                            "role",
                            "harness",
                            "model",
                            "effort",
                            "disposition",
                            "current_conversation_id",
                        )
                    }
                    for row in board.get("participants") or []
                ],
                "work_units": [
                    {
                        "work_unit_id": row.get("work_unit_id"),
                        "disposition": row.get("disposition"),
                        "column": row.get("column"),
                        "pull_requests": row.get("pull_requests"),
                        "messages": [
                            {
                                key: message.get(key)
                                for key in (
                                    "message_id",
                                    "kind",
                                    "disposition",
                                    "read_at",
                                    "created_at",
                                )
                            }
                            for message in row.get("messages") or []
                        ],
                        "pickup": row.get("pickup"),
                        "delivery": row.get("delivery"),
                    }
                    for row in board.get("work_units") or []
                ],
            }
        )

    @staticmethod
    def _review_message(
        board: Mapping[str, Any], message_id: int | None = None
    ) -> dict[str, Any] | None:
        units = board.get("work_units") or []
        if len(units) != 1:
            raise CanaryError(
                "CANARY_FORCE_NEW_GATE_FAILED",
                "canary Sprint does not have exactly one work unit",
            )
        matches = [
            message
            for message in units[0].get("messages") or []
            if message.get("kind") == "review_request"
            and (message_id is None or message.get("message_id") == message_id)
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise CanaryError(
                "CANARY_FORCE_NEW_GATE_FAILED",
                "canary Sprint has ambiguous review-request messages",
            )
        return matches[0]

    @staticmethod
    def _validate_force_new_probe(
        payload: Any, *, sprint_id: int, message_id: int
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CanaryError(
                "CANARY_FORCE_NEW_GATE_FAILED",
                "REV1 Force-new gate proof is not a JSON object",
            )
        inbox_ids = payload.get("inbox_message_ids")
        inbox = payload.get("inbox")
        accept = payload.get("accept")
        decline = payload.get("decline")
        valid_identity = (
            payload.get("sprint_id") == sprint_id
            and payload.get("message_id") == message_id
        )
        valid_inbox = (
            isinstance(inbox, dict)
            and inbox.get("returncode") == 0
            and inbox.get("parsed") is True
            and isinstance(inbox_ids, list)
            and all(isinstance(item, int) for item in inbox_ids)
            and message_id not in inbox_ids
        )

        def rejected(result: Any) -> bool:
            return (
                isinstance(result, dict)
                and isinstance(result.get("returncode"), int)
                and result["returncode"] != 0
                and result.get("http_status") == 409
                and result.get("not_delivered") is True
            )

        if not valid_identity or not valid_inbox or not rejected(accept) or not rejected(
            decline
        ):
            raise CanaryError(
                "CANARY_FORCE_NEW_GATE_FAILED",
                "undelivered Force-new message did not reject inbox acceptance",
                details={
                    "sprint_id": sprint_id,
                    "message_id": message_id,
                    "identity_matched": valid_identity,
                    "inbox_absent": valid_inbox,
                    "accept_rejected": rejected(accept),
                    "decline_rejected": rejected(decline),
                },
            )
        return {
            "message_id": message_id,
            "inbox_absent": True,
            "accept_rejected": True,
            "accept_http_status": 409,
            "decline_rejected": True,
            "decline_http_status": 409,
        }

    @staticmethod
    def _event_after(
        payload: Mapping[str, Any],
        *,
        event_type: str,
        after_event_id: int,
        message_id: int | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        matches = []
        for event in payload.get("items") or []:
            event_id = event.get("event_id")
            details = event.get("details") or {}
            if (
                isinstance(event_id, int)
                and event_id > after_event_id
                and event.get("type") == event_type
                and (message_id is None or details.get("message_id") == message_id)
                and (
                    conversation_id is None
                    or details.get("conversation_id") == conversation_id
                )
            ):
                matches.append(event)
        if not matches:
            return None
        return min(matches, key=lambda event: int(event["event_id"]))

    @staticmethod
    def _observe_column(board: Mapping[str, Any], observed: list[str]) -> None:
        units = board.get("work_units") or []
        if len(units) != 1:
            return
        column = units[0].get("column")
        if isinstance(column, str) and (not observed or observed[-1] != column):
            observed.append(column)

    def _wait_running(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        conversation_id: str,
    ) -> None:
        while True:
            conversation = api.request("GET", f"/api/conversations/{conversation_id}")
            state = conversation.get("state")
            if state == "running":
                return
            if state in {"idle", "error", "closed"}:
                raise CanaryError(
                    "CANARY_FORCE_NEW_BARRIER_FAILED",
                    "Reviewer control turn terminalized before establishing the barrier",
                    details={"conversation_id": conversation_id, "state": state},
                )
            self.deadline.remaining()
            self.sleep(min(0.05, self.deadline.remaining()))

    def _start_force_new_barrier(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        *,
        reviewer_id: str,
    ) -> ForceNewProbe:
        prefix = f"/tmp/subfloor-canary-{config.run_id}-force-new"
        probe = ForceNewProbe(
            reviewer_id=reviewer_id,
            target_path=prefix + "-target.json",
            proof_path=prefix + "-proof.json",
        )
        program = f'''from pathlib import Path
import json
import re
import subprocess
import time

TARGET = Path({probe.target_path!r})
PROOF = Path({probe.proof_path!r})
DEADLINE = time.monotonic() + {max(1.0, config.whole_timeout_s)!r}

def wait_for(path):
    while not path.is_file():
        if time.monotonic() >= DEADLINE:
            raise TimeoutError(f"timed out waiting for {{path.name}}")
        time.sleep(0.05)

def run(*args):
    result = subprocess.run(
        ["./sc", "sprint", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    combined = result.stdout + "\\n" + result.stderr
    match = re.search(r"HTTP\\s+(\\d{{3}})", combined)
    return {{
        "returncode": result.returncode,
        "http_status": int(match.group(1)) if match else None,
        "not_delivered": "not been delivered" in combined.lower(),
        "stdout": result.stdout,
    }}

wait_for(TARGET)
target = json.loads(TARGET.read_text())
SPRINT_ID = target["sprint_id"]
MESSAGE_ID = target["message_id"]
if not isinstance(SPRINT_ID, int) or SPRINT_ID <= 0:
    raise ValueError("invalid Sprint id")
if not isinstance(MESSAGE_ID, int) or MESSAGE_ID <= 0:
    raise ValueError("invalid review message id")
inbox = run("inbox", "--sprint", str(SPRINT_ID))
inbox_parsed = False
try:
    inbox_payload = json.loads(inbox["stdout"]) if inbox["returncode"] == 0 else {{}}
    inbox_parsed = isinstance(inbox_payload.get("messages"), list)
except json.JSONDecodeError:
    inbox_payload = {{}}
accept = run("accept", "--sprint", str(SPRINT_ID), "--message", str(MESSAGE_ID))
decline = run(
    "decline", "--sprint", str(SPRINT_ID), "--message", str(MESSAGE_ID),
    "--reason", "canary pre-delivery rejection probe",
)
proof = {{
    "sprint_id": SPRINT_ID,
    "message_id": MESSAGE_ID,
    "inbox": {{"returncode": inbox["returncode"], "parsed": inbox_parsed}},
    "inbox_message_ids": [
        item.get("message_id") for item in inbox_payload.get("messages", [])
        if isinstance(item.get("message_id"), int)
    ],
    "accept": {{key: accept[key] for key in ("returncode", "http_status", "not_delivered")}},
    "decline": {{key: decline[key] for key in ("returncode", "http_status", "not_delivered")}},
}}
temporary = PROOF.with_suffix(".tmp")
temporary.write_text(json.dumps(proof, sort_keys=True) + "\\n")
temporary.replace(PROOF)
while time.monotonic() < DEADLINE:
    time.sleep(0.05)
raise TimeoutError("controller did not close the Force-new barrier")
'''
        self._message(
            api,
            reviewer_id,
            (
                "This is the exact-ref canary's pre-delivery control turn, not the "
                "pending review wake. Run the Python program below exactly once. It "
                "establishes a live barrier, waits for the controller's message identity, "
                "runs the public Sprint inbox/accept/decline probes, writes bounded proof, "
                "and stays live until the controller releases it.\n\n"
                "```python\n" + program + "```"
            ),
            f"{config.run_id}:reviewer:force-new-barrier",
        )
        self._wait_running(api, config, reviewer_id)
        return probe

    def _write_probe_file(
        self,
        facts: Preflight,
        path: str,
        content: str,
        *,
        label: str,
    ) -> None:
        self._run(
            [
                "docker",
                "exec",
                facts.container,
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "target=Path(sys.argv[1]); temporary=target.with_suffix('.tmp'); "
                    "temporary.write_text(sys.argv[2]); temporary.replace(target)"
                ),
                path,
                content,
            ],
            label=label,
        )

    def _target_force_new_probe(
        self,
        facts: Preflight,
        probe: ForceNewProbe,
        *,
        sprint_id: int,
        message_id: int,
    ) -> None:
        body = json.dumps(
            {"sprint_id": sprint_id, "message_id": message_id},
            separators=(",", ":"),
            sort_keys=True,
        )
        self._write_probe_file(
            facts,
            probe.target_path,
            body + "\n",
            label="target active Force-new gate probe",
        )

    def _collect_force_new_probe(
        self,
        facts: Preflight,
        probe: ForceNewProbe,
        *,
        sprint_id: int,
        message_id: int,
    ) -> dict[str, Any]:
        while True:
            result = self._run(
                [
                    "docker",
                    "exec",
                    facts.container,
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; import sys; path=Path(sys.argv[1]); "
                        "sys.stdout.write(path.read_text()) if path.is_file() "
                        "else sys.exit(3)"
                    ),
                    probe.proof_path,
                ],
                check=False,
                label="read bounded Force-new gate proof",
            )
            if result.returncode == 0:
                break
            if result.returncode != 3:
                raise CanaryError(
                    "CANARY_FORCE_NEW_BARRIER_FAILED",
                    "cannot read the active Reviewer gate proof",
                    details={"returncode": result.returncode},
                )
            self.deadline.remaining()
            self.sleep(min(0.05, self.deadline.remaining()))
        return self._validate_force_new_probe(
            _json_output(result, label="read bounded Force-new gate proof"),
            sprint_id=sprint_id,
            message_id=message_id,
        )

    def _close_force_new_barrier(
        self,
        api: JsonHttp,
        probe: ForceNewProbe,
    ) -> None:
        reviewer = api.request("GET", f"/api/conversations/{probe.reviewer_id}")
        if reviewer.get("state") != "running":
            raise CanaryError(
                "CANARY_FORCE_NEW_BARRIER_FAILED",
                "Reviewer control turn stopped before the barrier was closed",
                details={
                    "conversation_id": probe.reviewer_id,
                    "state": reviewer.get("state"),
                },
            )
        closed = api.request(
            "PATCH",
            f"/api/conversations/{probe.reviewer_id}",
            body={"version": reviewer["version"], "state": "closed"},
        )
        if closed.get("state") != "closed":
            raise CanaryError(
                "CANARY_FORCE_NEW_BARRIER_FAILED",
                "public close did not terminate the Reviewer control turn",
                details={"conversation_id": probe.reviewer_id},
            )

    def _start_isolation_peer(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        facts: Preflight,
        conversation_id: str,
    ) -> dict[str, Any]:
        before = self._conversation_evidence(facts, conversation_id)
        if before["harness"] != "deepseek" or before["session_sha256"] is None:
            raise CanaryError(
                "CANARY_ISOLATION_FAILED",
                "DeepSeek peer has no exact session identity",
            )
        self._message(
            api,
            conversation_id,
            (
                "Run one Bash tool with exactly `python3 -c 'import time; "
                "time.sleep(600)'`. Keep that tool call active until interrupted. "
                "Do not run another tool, change files, or finish early. Pass when the "
                "controller interrupts this turn."
            ),
            f"{config.run_id}:deepseek:isolation-peer",
        )
        while True:
            evidence = self._conversation_evidence(facts, conversation_id)
            if (
                evidence["state"] == "running"
                and evidence["tool_started"] > before["tool_started"]
            ):
                return evidence
            if evidence["state"] in {"error", "closed"}:
                raise CanaryError(
                    "CANARY_ISOLATION_FAILED",
                    "DeepSeek peer terminalized before its isolation barrier",
                )
            self.deadline.remaining()
            self.sleep(min(0.1, self.deadline.remaining()))

    def _assert_isolation_peer(
        self,
        facts: Preflight,
        conversation_id: str,
        baseline: Mapping[str, Any],
        *,
        boundary: str,
    ) -> dict[str, Any]:
        observed = self._conversation_evidence(facts, conversation_id)
        if (
            observed["state"] != "running"
            or observed["session_sha256"] != baseline["session_sha256"]
            or observed["boot_sha256"] != baseline["boot_sha256"]
            or observed["terminal_events"] != baseline["terminal_events"]
        ):
            raise CanaryError(
                "CANARY_ISOLATION_FAILED",
                f"DeepSeek peer changed across {boundary}",
            )
        return observed

    def _stop_isolation_peer(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        facts: Preflight,
        conversation_id: str,
        baseline: Mapping[str, Any],
    ) -> dict[str, Any]:
        interrupted = api.request(
            "POST",
            f"/api/conversations/{conversation_id}/interruptions",
            body={},
            key=f"{config.run_id}:deepseek:isolation-peer-stop",
        )
        if not isinstance(interrupted.get("run_id"), int):
            raise CanaryError(
                "CANARY_ISOLATION_FAILED",
                "DeepSeek peer stop returned no run identity",
            )
        while True:
            observed = self._conversation_evidence(facts, conversation_id)
            if observed["state"] == "idle":
                break
            if observed["state"] in {"error", "closed"}:
                raise CanaryError(
                    "CANARY_ISOLATION_FAILED",
                    "DeepSeek peer did not return to idle after scoped stop",
                )
            self.deadline.remaining()
            self.sleep(min(0.1, self.deadline.remaining()))
        if (
            observed["session_sha256"] != baseline["session_sha256"]
            or observed["boot_sha256"] != baseline["boot_sha256"]
            or observed["terminal_events"] != baseline["terminal_events"] + 1
        ):
            raise CanaryError(
                "CANARY_ISOLATION_FAILED",
                "DeepSeek peer stop changed identity or terminalized more than one run",
            )
        return {
            "conversation_id": conversation_id,
            "session_sha256": observed["session_sha256"],
            "boot_sha256": observed["boot_sha256"],
            "concurrent_running": True,
            "reviewer_close_scoped": True,
            "reviewer_pickup_interrupt_scoped": True,
            "peer_stop_scoped": True,
            "terminal_events_before": baseline["terminal_events"],
            "terminal_events_after": observed["terminal_events"],
        }

    def _exercise_review_delivery_gates(
        self,
        api: JsonHttp,
        config: CanaryConfig,
        facts: Preflight,
        *,
        sprint_id: int,
        probe: ForceNewProbe,
        stage: Callable[[str], None],
        require_deepseek_isolation: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        observed_columns: list[str] = []
        stage("force_new_pre_delivery")
        review_message: dict[str, Any] | None = None
        board: dict[str, Any] = {}
        while review_message is None:
            board = api.request("GET", f"/api/sprints/{sprint_id}")
            self._observe_column(board, observed_columns)
            lifecycle = (board.get("sprint") or {}).get("lifecycle")
            if lifecycle != "armed":
                raise CanaryError(
                    "CANARY_FORCE_NEW_GATE_FAILED",
                    "Sprint left armed before the Force-new review gate",
                    details={"lifecycle": lifecycle},
                )
            review_message = self._review_message(board)
            if review_message is None:
                self.deadline.remaining()
                self.sleep(min(0.1, self.deadline.remaining()))

        message_id = int(review_message["message_id"])
        reviewer = next(
            (
                row
                for row in board.get("participants") or []
                if row.get("role") == "reviewer"
            ),
            None,
        )
        if (
            not isinstance(reviewer, dict)
            or reviewer.get("current_conversation_id") != probe.reviewer_id
            or review_message.get("disposition") != "pending"
            or review_message.get("read_at") is not None
        ):
            raise CanaryError(
                "CANARY_FORCE_NEW_GATE_MISSED",
                "Force-new review rotated or became readable before the negative gate",
                details={"message_id": message_id},
            )
        self._target_force_new_probe(
            facts,
            probe,
            sprint_id=sprint_id,
            message_id=message_id,
        )
        force_new = self._collect_force_new_probe(
            facts,
            probe,
            sprint_id=sprint_id,
            message_id=message_id,
        )
        board = api.request("GET", f"/api/sprints/{sprint_id}")
        review_message = self._review_message(board, message_id)
        reviewer = next(
            (
                row
                for row in board.get("participants") or []
                if row.get("role") == "reviewer"
            ),
            None,
        )
        if (
            review_message is None
            or review_message.get("disposition") != "pending"
            or review_message.get("read_at") is not None
            or not isinstance(reviewer, dict)
            or reviewer.get("current_conversation_id") != probe.reviewer_id
        ):
            raise CanaryError(
                "CANARY_FORCE_NEW_GATE_FAILED",
                "negative probes changed the undelivered review request",
                details={"message_id": message_id},
            )

        isolation_peer_id: str | None = None
        isolation_baseline: dict[str, Any] | None = None
        if require_deepseek_isolation:
            stage("deepseek_ab_isolation")
            developer = next(
                (
                    row
                    for row in board.get("participants") or []
                    if row.get("role") == "developer"
                ),
                None,
            )
            if (
                not isinstance(developer, dict)
                or developer.get("harness") != "deepseek"
                or not isinstance(developer.get("current_conversation_id"), str)
            ):
                raise CanaryError(
                    "CANARY_ISOLATION_FAILED",
                    "DeepSeek Developer participant conversation is missing",
                )
            isolation_peer_id = str(developer["current_conversation_id"])
            isolation_baseline = self._start_isolation_peer(
                api, config, facts, isolation_peer_id
            )

        self._close_force_new_barrier(api, probe)
        if isolation_peer_id is not None and isolation_baseline is not None:
            self._assert_isolation_peer(
                facts,
                isolation_peer_id,
                isolation_baseline,
                boundary="Reviewer barrier close",
            )

        stage("force_new_delivery")
        delivery_id: str | None = None
        while delivery_id is None:
            board = api.request("GET", f"/api/sprints/{sprint_id}")
            self._observe_column(board, observed_columns)
            message = self._review_message(board, message_id)
            sprint = board.get("sprint") or {}
            if sprint.get("lifecycle") != "armed":
                raise CanaryError(
                    "CANARY_FORCE_NEW_DELIVERY_FAILED",
                    "Sprint left armed before Force-new delivery",
                    details={"lifecycle": sprint.get("lifecycle")},
                )
            if (
                message is None
                or message.get("disposition") != "pending"
                or message.get("read_at") is not None
            ):
                raise CanaryError(
                    "CANARY_FORCE_NEW_GATE_MISSED",
                    "review request was accepted before pickup injection",
                    details={"message_id": message_id},
                )
            current = next(
                (
                    row.get("current_conversation_id")
                    for row in board.get("participants") or []
                    if row.get("role") == "reviewer"
                ),
                None,
            )
            if current == probe.reviewer_id:
                raise CanaryError(
                    "CANARY_FORCE_NEW_DELIVERY_FAILED",
                    "Force-new delivery retained the pre-delivery Reviewer chat",
                    details={"conversation_id": current},
                )
            if isinstance(current, str) and current != probe.reviewer_id:
                delivery_id = current
                break
            self.deadline.remaining()
            self.sleep(min(0.1, self.deadline.remaining()))

        stage("pickup_failure")
        interruption: dict[str, Any] | None = None
        while interruption is None:
            conversation = api.request("GET", f"/api/conversations/{delivery_id}")
            state = conversation.get("state")
            if state == "running":
                board = api.request("GET", f"/api/sprints/{sprint_id}")
                message = self._review_message(board, message_id)
                if message is None or message.get("read_at") is not None:
                    raise CanaryError(
                        "CANARY_PICKUP_INJECTION_MISSED",
                        "review request was read before pickup interruption",
                        details={"message_id": message_id},
                    )
                events_before = api.request(
                    "GET", f"/api/sprints/{sprint_id}/events?limit=100"
                )
                before_event_id = max(
                    [
                        int(item["event_id"])
                        for item in events_before.get("items") or []
                        if isinstance(item.get("event_id"), int)
                    ],
                    default=0,
                )
                interruption = api.request(
                    "POST",
                    f"/api/conversations/{delivery_id}/interruptions",
                    body={},
                    key=f"{config.run_id}:reviewer:pickup-interrupt",
                )
                if isolation_peer_id is not None and isolation_baseline is not None:
                    self._assert_isolation_peer(
                        facts,
                        isolation_peer_id,
                        isolation_baseline,
                        boundary="Reviewer pickup interruption",
                    )
                break
            if state in {"idle", "error", "closed"}:
                raise CanaryError(
                    "CANARY_PICKUP_INJECTION_MISSED",
                    "Force-new pickup terminalized before interruption",
                    details={"conversation_id": delivery_id, "state": state},
                )
            self.deadline.remaining()
            self.sleep(min(0.05, self.deadline.remaining()))

        run_id = interruption.get("run_id")
        if not isinstance(run_id, int):
            raise CanaryError(
                "CANARY_PICKUP_INJECTION_FAILED",
                "public interruption returned no run identity",
            )

        exhausted_event: dict[str, Any] | None = None
        paused_board: dict[str, Any] | None = None
        while exhausted_event is None:
            board = api.request("GET", f"/api/sprints/{sprint_id}")
            self._observe_column(board, observed_columns)
            message = self._review_message(board, message_id)
            if message is None or message.get("read_at") is not None:
                raise CanaryError(
                    "CANARY_PICKUP_RECOVERY_FAILED",
                    "interrupted review request became read before recovery",
                    details={"message_id": message_id},
                )
            lifecycle = (board.get("sprint") or {}).get("lifecycle")
            if lifecycle == "paused":
                pickup = board.get("pickup") or {}
                if pickup.get("pause_reason") != PICKUP_INJECTION_PAUSE_REASON:
                    raise CanaryError(
                        "CANARY_PICKUP_RECOVERY_FAILED",
                        "pickup interruption paused for an unexpected reason",
                        details={"pause_reason": pickup.get("pause_reason")},
                    )
                events = api.request(
                    "GET", f"/api/sprints/{sprint_id}/events?limit=100"
                )
                exhausted_event = self._event_after(
                    events,
                    event_type="wake.pickup_exhausted",
                    after_event_id=before_event_id,
                    message_id=message_id,
                    conversation_id=delivery_id,
                )
                if exhausted_event is not None:
                    paused_board = board
                    break
            elif lifecycle != "armed":
                raise CanaryError(
                    "CANARY_PICKUP_RECOVERY_FAILED",
                    "Sprint reached an unexpected lifecycle after pickup interruption",
                    details={"lifecycle": lifecycle},
                )
            self.deadline.remaining()
            self.sleep(min(config.poll_interval_s, self.deadline.remaining()))

        assert paused_board is not None
        exhausted = exhausted_event.get("details") or {}
        if (
            exhausted.get("run_state") != "cancelled"
            or exhausted.get("error_code") != "WAKE_PICKUP_EVIDENCE_INVALID"
            or exhausted.get("failure_class") != "evidence_invalid"
            or exhausted.get("attempt_count") != 1
        ):
            raise CanaryError(
                "CANARY_PICKUP_RECOVERY_FAILED",
                "pickup pause receipt does not match the induced interruption",
                details={
                    key: exhausted.get(key)
                    for key in (
                        "run_state",
                        "error_code",
                        "failure_class",
                        "attempt_count",
                    )
                },
            )

        stage("pickup_recovery")
        resume = api.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={
                "lifecycle": "armed",
                "reason": "exact-ref canary pickup interruption repaired",
            },
        )
        if resume.get("changed") is not True or (
            resume.get("sprint") or {}
        ).get("lifecycle") != "armed":
            raise CanaryError(
                "CANARY_PICKUP_RECOVERY_FAILED",
                "public Sprint resume did not durably re-arm the canary",
            )

        replacement_event: dict[str, Any] | None = None
        recovery_id: str | None = None
        while recovery_id is None:
            board = api.request("GET", f"/api/sprints/{sprint_id}")
            self._observe_column(board, observed_columns)
            sprint = board.get("sprint") or {}
            if sprint.get("lifecycle") != "armed":
                raise CanaryError(
                    "CANARY_PICKUP_RECOVERY_FAILED",
                    "Sprint left armed during pickup recovery",
                    details={"lifecycle": sprint.get("lifecycle")},
                )
            message = self._review_message(board, message_id)
            current = next(
                (
                    row.get("current_conversation_id")
                    for row in board.get("participants") or []
                    if row.get("role") == "reviewer"
                ),
                None,
            )
            events = api.request(
                "GET", f"/api/sprints/{sprint_id}/events?limit=100"
            )
            if replacement_event is None:
                replacement_event = self._event_after(
                    events,
                    event_type="wake.requeued",
                    after_event_id=int(exhausted_event["event_id"]),
                )
            accepted = (
                message is not None
                and message.get("disposition") == "accepted"
                and message.get("read_at") is not None
            )
            if accepted:
                if not isinstance(current, str) or current in {
                    probe.reviewer_id,
                    delivery_id,
                }:
                    raise CanaryError(
                        "CANARY_PICKUP_RECOVERY_FAILED",
                        "recovered review was accepted outside a fresh chat",
                        details={"conversation_id": current},
                    )
                if replacement_event is None:
                    raise CanaryError(
                        "CANARY_PICKUP_RECOVERY_FAILED",
                        "recovered review was accepted without durable requeue evidence",
                        details={"message_id": message_id},
                    )
                recovery_id = current
                break
            self.deadline.remaining()
            self.sleep(min(config.poll_interval_s, self.deadline.remaining()))

        replacement = replacement_event.get("details") or {}
        replacement_wake_id = replacement.get("replacement_wake_id")
        if not isinstance(replacement_wake_id, int):
            raise CanaryError(
                "CANARY_PICKUP_RECOVERY_FAILED",
                "resume recovery event contains no replacement wake identity",
            )
        force_new.update(
            {
                "prior_conversation_id": probe.reviewer_id,
                "delivery_conversation_id": delivery_id,
                "fresh_chat": delivery_id != probe.reviewer_id,
            }
        )
        isolation = None
        if isolation_peer_id is not None and isolation_baseline is not None:
            isolation = self._stop_isolation_peer(
                api,
                config,
                facts,
                isolation_peer_id,
                isolation_baseline,
            )
        return (
            {
                "force_new": force_new,
                "deepseek_ab_isolation": isolation,
                "pickup_recovery": {
                    "induced": True,
                    "interrupted_run_id": run_id,
                    "interrupted_conversation_id": delivery_id,
                    "pause_reason": PICKUP_INJECTION_PAUSE_REASON,
                    "pause_event_id": int(exhausted_event["event_id"]),
                    "error_code": "WAKE_PICKUP_EVIDENCE_INVALID",
                    "failure_class": "evidence_invalid",
                    "attempt_count": 1,
                    "resume_changed": True,
                    "replacement_wake_id": replacement_wake_id,
                    "recovery_conversation_id": recovery_id,
                    "message_id": message_id,
                    "final_disposition": "accepted",
                },
            },
            observed_columns,
        )

    def orchestrate(
        self,
        config: CanaryConfig,
        facts: Preflight,
        ledger: ResourceLedger,
        stage: Callable[[str], None],
        checkpoint: Callable[[], None],
    ) -> dict[str, Any]:
        api = JsonHttp(f"http://127.0.0.1:{facts.api_port}", self.deadline)
        shells = self._shells(api.request("GET", "/api/shells"))
        required = {"PLN1", "DEV1", "REV1"}
        if not required.issubset(shells):
            raise CanaryError(
                "CANARY_ROSTER_INVALID",
                "disposable fork is missing the required canonical roster",
                details={"missing": sorted(required - set(shells))},
            )
        feature_title = f"Subfloor exact-ref Sprint canary {config.run_id}"
        spec_title = f"Canary contract {config.run_id}"
        deterministic_path = f"canary/{config.run_id}.txt"
        deterministic_content = f"subfloor sprint canary {facts.candidate_sha}"
        deepseek_profile = config.profile == DEEPSEEK_SPRINT_PROFILE

        stage("planner_prepare")
        planner = self._create_conversation(
            api,
            shell_id=shells["PLN1"],
            harness="codex",
            key=f"{config.run_id}:planner:create",
        )
        planner_id = str(planner["conversation_id"])
        planner_projection = planner.get("route") or {}
        planner_route = {
            key: planner_projection.get(key)
            for key in ("harness", "provider", "model", "effort")
        }
        self._message(
            api,
            planner_id,
            (
                "You are the Planner for an unattended source-maintainer canary. "
                f"Create one in_progress roadmap feature titled exactly {feature_title!r}, "
                f"one unfrozen spec titled exactly {spec_title!r}, and one pending task. "
                "The spec must require DEV1 to create exactly one deterministic file and a "
                "real GitHub PR against the named ephemeral base, with REV1 reviewing through "
                f"{'DeepSeek Harness' if deepseek_profile else 'Kimi'}. "
                "Do not declare or arm a Sprint yet. Use only sc mem public commands, confirm "
                "every durable write, and stop after the feature, spec, and task exist."
            ),
            f"{config.run_id}:planner:prepare",
        )
        self._wait_idle(api, planner_id, config, facts)
        feature = self._feature(api.request("GET", "/api/roadmap"), feature_title)
        if feature is None:
            raise CanaryError(
                "CANARY_PLAN_FAILED", "Planner did not create the canary feature"
            )
        documents = [
            item
            for item in feature.get("documents") or []
            if item.get("kind") == "spec" and item.get("title") == spec_title
        ]
        tasks = list(feature.get("tasks") or [])
        if len(documents) != 1 or len(tasks) != 1:
            raise CanaryError(
                "CANARY_PLAN_FAILED",
                "Planner did not create exactly one spec and one task",
            )
        feature_id = int(feature["feature_id"])
        document_id = int(documents[0]["document_id"])
        task_id = int(tasks[0]["task_id"])

        reviewer_harness = "deepseek" if deepseek_profile else "kimi"
        developer_harness = "deepseek" if deepseek_profile else "codex"
        participants = [
            {
                "shell_id": shells["PLN1"],
                "role": "planner",
                "harness": "codex",
                "model": None,
                "effort": None,
            },
            {
                "shell_id": shells["DEV1"],
                "role": "developer",
                "harness": developer_harness,
                "model": DEEPSEEK_MODEL if deepseek_profile else None,
                "effort": "default" if deepseek_profile else None,
            },
            {
                "shell_id": shells["REV1"],
                "role": "reviewer",
                "harness": reviewer_harness,
                "model": DEEPSEEK_MODEL if deepseek_profile else None,
                "effort": "default" if deepseek_profile else None,
            },
        ]
        stage("declare_prepared")
        self._message(
            api,
            planner_id,
            (
                f"Declare one merge-granted prepared Sprint for feature #{feature_id} "
                f"using spec document #{document_id} directly, with no QA/QC approval id, "
                "and the participant JSON below. Plan exactly one code unit assigning "
                f"task #{task_id} to DEV1 with REV1. Expected output: create "
                f"{deterministic_path!r} containing exactly {deterministic_content!r} plus "
                f"a newline; use head branch {facts.head_branch!r}, created from "
                f"origin/{facts.base_branch}; open the PR in {facts.repository!r} against "
                f"base {facts.base_branch!r}; never target main. Do not arm or dispatch the "
                "Sprint. Confirm every durable write and stop with the Sprint prepared. "
                "Participants JSON: "
                + json.dumps(participants, separators=(",", ":"))
            ),
            f"{config.run_id}:planner:declare-prepared",
        )
        self._wait_idle(api, planner_id, config, facts)
        sprint_list = api.request("GET", "/api/sprints?limit=100")
        matches = [
            row
            for row in sprint_list.get("items") or []
            if (row.get("feature") or {}).get("feature_id") == feature_id
            or row.get("feature_id") == feature_id
        ]
        if len(matches) != 1:
            raise CanaryError(
                "CANARY_DECLARATION_FAILED", "Planner did not declare one Sprint"
            )
        sprint_id = int(matches[0]["sprint_id"])
        prepared_board = api.request("GET", f"/api/sprints/{sprint_id}")
        if (prepared_board.get("sprint") or {}).get("lifecycle") != "prepared":
            raise CanaryError(
                "CANARY_DECLARATION_FAILED", "canary Sprint is not prepared"
            )

        stage("deepseek_qaqc" if deepseek_profile else "kimi_qaqc")
        reviewer = self._create_conversation(
            api,
            shell_id=shells["REV1"],
            harness=reviewer_harness,
            model=DEEPSEEK_MODEL if deepseek_profile else None,
            effort="default" if deepseek_profile else None,
            key=f"{config.run_id}:reviewer:create",
        )
        reviewer_id = str(reviewer["conversation_id"])
        reviewer_projection = reviewer.get("route") or {}
        reviewer_route = {
            key: reviewer_projection.get(key)
            for key in ("harness", "provider", "model", "effort")
        }
        self._message(
            api,
            reviewer_id,
            self._qaqc_reviewer_prompt(document_id),
            f"{config.run_id}:reviewer:qaqc",
        )
        self._wait_idle(api, reviewer_id, config, facts)
        if deepseek_profile:
            approval_id, qaqc_evidence = self._qaqc_action_evidence(
                api,
                facts,
                reviewer_id,
                sprint_id=sprint_id,
                reviewer_shell_id=shells["REV1"],
                document_id=document_id,
            )
            if approval_id is None or not self._qaqc_evidence_passed(qaqc_evidence):
                raise CanaryError(
                    "CANARY_QAQC_FAILED",
                    "reviewer did not record exact approval",
                    details={"qaqc_action": qaqc_evidence},
                )
        else:
            approval_id = self._approval(facts, document_id)
            qaqc_evidence = None

        restart_recovery = None
        if deepseek_profile:
            if (
                reviewer_route["model"] != DEEPSEEK_MODEL
                or reviewer_route["provider"] != "ollama-cloud"
                or reviewer_route["effort"] != "default"
            ):
                raise CanaryError(
                    "CANARY_ROUTE_NOT_CANONICAL",
                    "DeepSeek Reviewer did not bind the admitted Ollama route",
                    details={"route": reviewer_route},
                )
            stage("deepseek_exact_session_restart")
            restart_recovery = self._restart_exact_session(
                api, config, facts, reviewer_id
            )

        stage("force_new_barrier")
        force_new_probe = self._start_force_new_barrier(
            api,
            config,
            reviewer_id=reviewer_id,
        )

        stage("declare_and_arm")
        self._message(
            api,
            planner_id,
            (
                f"QA/QC approval #{approval_id} and its engine action receipt now correlate "
                f"to prepared Sprint #{sprint_id} and document #{document_id}. Arm and "
                "dispatch that existing Sprint without declaring or planning another one. "
                "The lane must register the PR, "
                f"reach green, request real Force-new {reviewer_harness} review, authorize and merge only "
                "through Sprint gates, and report the merge to you. After dispatch, handle "
                "your own informational Sprint inbox items and stop."
            ),
            f"{config.run_id}:planner:arm",
        )
        self._wait_idle(api, planner_id, config, facts)
        board = api.request("GET", f"/api/sprints/{sprint_id}")
        if (board.get("sprint") or {}).get("lifecycle") != "armed":
            raise CanaryError("CANARY_ARM_FAILED", "canary Sprint is not armed")
        planner_latest = api.request("GET", f"/api/conversations/{planner_id}")
        api.request(
            "PATCH",
            f"/api/conversations/{planner_id}",
            body={"version": planner_latest["version"], "state": "closed"},
        )

        gate_evidence, observed_columns = self._exercise_review_delivery_gates(
            api,
            config,
            facts,
            sprint_id=sprint_id,
            probe=force_new_probe,
            require_deepseek_isolation=deepseek_profile,
            stage=stage,
        )

        stage("sprint_execution")
        last_signature: tuple[Any, ...] | None = None
        final_board: dict[str, Any] | None = None
        while True:
            board = api.request("GET", f"/api/sprints/{sprint_id}")
            sprint = board.get("sprint") or {}
            lifecycle = sprint.get("lifecycle")
            units = board.get("work_units") or []
            unit = units[0] if len(units) == 1 else {}
            prs = unit.get("pull_requests") or []
            signature = (
                lifecycle,
                unit.get("column"),
                unit.get("disposition"),
                tuple((pr.get("pr_number"), pr.get("normalized_state")) for pr in prs),
            )
            if signature != last_signature:
                self.deadline.enter("sprint_execution")
                last_signature = signature
                column = unit.get("column")
                if isinstance(column, str) and (
                    not observed_columns or observed_columns[-1] != column
                ):
                    observed_columns.append(column)
            if prs and isinstance(prs[0].get("pr_number"), int):
                observed_pr = int(prs[0]["pr_number"])
                if ledger.pull_request != observed_pr:
                    ledger.pull_request = observed_pr
                    checkpoint()
            if lifecycle in FAILURE_LIFECYCLES:
                pickup = board.get("pickup") or {}
                raise CanaryError(
                    "CANARY_SPRINT_FAILED",
                    f"Sprint entered {lifecycle}",
                    details={
                        "sprint_id": sprint_id,
                        "pickup_action": pickup.get("action"),
                        "pause_reason": pickup.get("pause_reason"),
                        "evidence": self._bounded_board(board),
                    },
                )
            if lifecycle == "completed":
                final_board = board
                break
            try:
                self.deadline.remaining()
                self.sleep(min(config.poll_interval_s, self.deadline.remaining()))
            except CanaryError as exc:
                if exc.code != "CANARY_DEADLINE_EXCEEDED":
                    raise
                raise CanaryError(
                    exc.code,
                    exc.message,
                    details={
                        **exc.details,
                        "sprint_id": sprint_id,
                        "evidence": self._bounded_board(board),
                    },
                ) from exc
        assert final_board is not None
        unit = (final_board.get("work_units") or [None])[0]
        if not isinstance(unit, dict):
            raise CanaryError(
                "CANARY_SPRINT_FAILED", "completed Sprint has no work unit"
            )
        prs = unit.get("pull_requests") or []
        if len(prs) != 1 or prs[0].get("normalized_state") != "merged":
            raise CanaryError("CANARY_PR_FAILED", "Sprint did not merge exactly one PR")
        pr = prs[0]
        ledger.pull_request = int(pr["pr_number"])
        live_pr = _json_output(
            self._infra_run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(ledger.pull_request),
                    "--repo",
                    facts.repository,
                    "--json",
                    "number,state,baseRefName,headRefName,headRefOid,mergeCommit,url",
                ],
                label="verify merged canary PR",
            ),
            label="verify merged canary PR",
        )
        if (
            live_pr.get("state") != "MERGED"
            or live_pr.get("baseRefName") != facts.base_branch
            or live_pr.get("headRefName") != facts.head_branch
            or not HEX_SHA.fullmatch(str(live_pr.get("headRefOid") or ""))
        ):
            raise CanaryError(
                "CANARY_PR_FAILED",
                "live GitHub PR identity does not match the ephemeral canary refs",
            )
        self._infra_git(
            facts.workspace,
            "fetch",
            "origin",
            facts.base_branch,
            label="fetch merged ephemeral base",
        )
        content = self._git(
            facts.workspace,
            "show",
            f"FETCH_HEAD:{deterministic_path}",
            label="verify deterministic canary file",
        ).stdout
        if content != deterministic_content + "\n":
            raise CanaryError(
                "CANARY_OUTPUT_MISMATCH", "deterministic canary file is wrong"
            )
        self._infra_git(
            facts.workspace,
            "fetch",
            "origin",
            "main",
            label="refresh dos-app main isolation proof",
        )
        main_contains = self._git(
            facts.workspace,
            "merge-base",
            "--is-ancestor",
            str(live_pr["headRefOid"]),
            "origin/main",
            label="prove canary head is absent from main",
            check=False,
        )
        if main_contains.returncode == 0:
            raise CanaryError("CANARY_MAIN_MUTATED", "canary head reached dos-app main")
        if main_contains.returncode not in {1}:
            raise CanaryError(
                "CANARY_MAIN_PROOF_FAILED", "could not prove main isolation"
            )
        planner_participant = next(
            (
                row
                for row in final_board.get("participants") or []
                if row.get("role") == "planner"
            ),
            None,
        )
        if not isinstance(planner_participant, dict):
            raise CanaryError("CANARY_REENTRY_FAILED", "Planner participant is missing")
        reentry_id = planner_participant.get("current_conversation_id")
        if not isinstance(reentry_id, str) or reentry_id == planner_id:
            raise CanaryError(
                "CANARY_REENTRY_FAILED",
                "Planner did not receive a fresh Re-enter conversation",
            )
        reentry = api.request("GET", f"/api/conversations/{reentry_id}")
        reentry_projection = reentry.get("route") or {}
        reentry_route = {
            key: reentry_projection.get(key)
            for key in ("harness", "provider", "model", "effort")
        }
        if not all(
            isinstance(value, str) and value for value in reentry_route.values()
        ):
            raise CanaryError(
                "CANARY_REENTRY_FAILED", "Planner Re-enter route is not canonical"
            )
        reentry_messages = (
            api.request("GET", f"/api/conversations/{reentry_id}/messages?limit=1").get(
                "items"
            )
            or []
        )
        if not reentry_messages or reentry_messages[-1].get("state") != "completed":
            raise CanaryError(
                "CANARY_REENTRY_FAILED", "Planner Re-enter first run did not complete"
            )
        participant_evidence = (
            self._deepseek_participant_evidence(facts, sprint_id, final_board)
            if deepseek_profile
            else None
        )
        events = api.request("GET", f"/api/sprints/{sprint_id}/events?limit=100")
        return {
            "sprint": {
                "sprint_id": sprint_id,
                "lifecycle": "completed",
                "feature_id": feature_id,
                "document_id": document_id,
                "task_id": task_id,
                "observed_columns": observed_columns,
                "runtime": final_board.get("runtime"),
                "pickup": final_board.get("pickup"),
                "evidence": self._bounded_board(final_board),
                "events": sanitize(events.get("items") or []),
                **gate_evidence,
                "exact_session_restart": restart_recovery,
                "participant_evidence": participant_evidence,
                "qaqc_action": qaqc_evidence,
            },
            "routes": {
                "planner_initial": planner_route,
                "planner_reentry": reentry_route,
                "reviewer_qaqc": reviewer_route,
                "participants": [
                    {
                        key: row.get(key)
                        for key in ("shortname", "role", "harness", "model", "effort")
                    }
                    for row in final_board.get("participants") or []
                ],
            },
            "pull_request": {
                "number": ledger.pull_request,
                "url": pr.get("url"),
                "head_sha": live_pr.get("headRefOid"),
                "merge_sha": (live_pr.get("mergeCommit") or {}).get("oid"),
                "base_branch": facts.base_branch,
                "head_branch": facts.head_branch,
                "state": "merged",
            },
        }

    def cleanup(
        self,
        config: CanaryConfig,
        facts: Preflight | None,
        ledger: ResourceLedger,
    ) -> list[dict[str, Any]]:
        self._release_ports()
        actions: list[dict[str, Any]] = []

        def record(
            name: str, result: CommandResult | None = None, *, ok: bool = True
        ) -> None:
            actions.append(
                {
                    "action": name,
                    "ok": ok,
                    "returncode": result.returncode if result else 0,
                }
            )

        failures: list[str] = []
        repository = ledger.repository or (facts.repository if facts else None)
        if ledger.pull_request and repository and REPO_RE.fullmatch(repository):
            viewed = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(ledger.pull_request),
                    "--repo",
                    repository,
                    "--json",
                    "state,headRefName,baseRefName",
                ],
                label="cleanup inspect PR",
                check=False,
            )
            if viewed.returncode == 0:
                identity = _json_output(viewed, label="cleanup inspect PR") or {}
                state = identity.get("state")
                identity_matches = (
                    identity.get("headRefName") == ledger.head_branch
                    and identity.get("baseRefName") == ledger.base_branch
                )
                if not identity_matches:
                    record("inspect_pr_identity", viewed, ok=False)
                    failures.append("inspect_pr_identity")
                elif state == "OPEN":
                    closed = self._run(
                        [
                            "gh",
                            "pr",
                            "close",
                            str(ledger.pull_request),
                            "--repo",
                            repository,
                        ],
                        label="cleanup close PR",
                        check=False,
                    )
                    record("close_pr", closed, ok=closed.returncode == 0)
                    if closed.returncode != 0:
                        failures.append("close_pr")
                elif state in {"CLOSED", "MERGED"}:
                    record("close_pr_already_terminal")
                else:
                    record("inspect_pr_state", viewed, ok=False)
                    failures.append("inspect_pr_state")
            else:
                record("inspect_pr", viewed, ok=False)
                failures.append("inspect_pr")
        target = config.dos_app_repo.resolve()
        for label, branch in (
            ("delete_head_branch", ledger.head_branch),
            ("delete_base_branch", ledger.base_branch),
        ):
            expected = f"{REMOTE_PREFIX}/{config.run_id}/"
            if not branch:
                record(label + "_absent")
                continue
            if not branch.startswith(expected):
                failures.append(label + "_unsafe")
                record(label, ok=False)
                continue
            deleted = self._git(
                target,
                "push",
                "origin",
                "--delete",
                branch,
                label=label,
                check=False,
            )
            combined = (deleted.stdout + deleted.stderr).lower()
            ok = deleted.returncode == 0 or "remote ref does not exist" in combined
            record(label, deleted, ok=ok)
            if not ok:
                failures.append(label)
        expected_container = f"sc-{WORKSPACE_PREFIX}{config.run_id}"
        if ledger.container:
            if ledger.container != expected_container:
                failures.append("remove_container_unsafe")
                record("remove_container", ok=False)
            else:
                removed = self._run(
                    ["docker", "rm", "-f", ledger.container],
                    label="cleanup remove container",
                    check=False,
                )
                ok = (
                    removed.returncode == 0
                    or "no such container" in removed.stderr.lower()
                )
                record("remove_container", removed, ok=ok)
                if not ok:
                    failures.append("remove_container")
        expected_network = f"sc-canary-{config.run_id}"
        if ledger.network:
            if ledger.network != expected_network:
                failures.append("remove_network_unsafe")
                record("remove_network", ok=False)
            else:
                removed = self._run(
                    ["docker", "network", "rm", ledger.network],
                    label="cleanup remove network",
                    check=False,
                )
                ok = removed.returncode == 0 or "not found" in removed.stderr.lower()
                record("remove_network", removed, ok=ok)
                if not ok:
                    failures.append("remove_network")
        if ledger.workspace:
            if config.temp_parent_explicit:
                parent = _validated_explicit_parent(config)
                workspace = _absolute_lexical(Path(ledger.workspace))
                expected_workspace = parent / f"{WORKSPACE_PREFIX}{config.run_id}"
                workspace_identity_ok = (
                    workspace == expected_workspace
                    and workspace.parent == parent
                    and _strictly_beneath(workspace, parent)
                    and not workspace.is_symlink()
                )
            else:
                workspace = Path(ledger.workspace).resolve()
                expected_workspace = (
                    config.temp_parent.resolve()
                    / f"{WORKSPACE_PREFIX}{config.run_id}"
                )
                workspace_identity_ok = workspace == expected_workspace
            marker = workspace / ".git" / "subfloor-canary-marker.json"
            if workspace.exists() or workspace.is_symlink():
                marker_ok = False
                if workspace_identity_ok:
                    try:
                        marker_data = json.loads(marker.read_text())
                        marker_ok = (
                            marker_data.get("run_id") == config.run_id
                            and isinstance(ledger.candidate_sha, str)
                            and HEX_SHA.fullmatch(ledger.candidate_sha) is not None
                            and marker_data.get("candidate_sha")
                            == ledger.candidate_sha
                        )
                    except (OSError, json.JSONDecodeError):
                        marker_ok = False
                if not workspace_identity_ok or not marker_ok:
                    failures.append("remove_workspace_unsafe")
                    record("remove_workspace", ok=False)
                else:
                    try:
                        shutil.rmtree(workspace)
                    except OSError:
                        failures.append("remove_workspace")
                        record("remove_workspace", ok=False)
                    else:
                        record("remove_workspace")
            else:
                record("remove_workspace_absent")
        self._provider_key = None
        if failures:
            raise CanaryError(
                "CANARY_CLEANUP_FAILED",
                "cleanup did not complete",
                details={"failed_actions": failures, "actions": actions},
            )
        return actions


class CanaryController:
    def __init__(
        self,
        config: CanaryConfig,
        backend: Backend,
        deadline: Deadline,
        receipt: Receipt,
    ) -> None:
        self.config = config
        self.backend = backend
        self.deadline = deadline
        self.receipt = receipt
        self.ledger = ResourceLedger()
        self.facts: Preflight | None = None
        self._receipt_stage = "preflight"
        self._receipt_stage_started = self.deadline.clock()

    def _stage(self, name: str) -> None:
        now = self.deadline.clock()
        if self._receipt_stage != name:
            elapsed = round(now - self._receipt_stage_started, 3)
            self.receipt.data["durations"][self._receipt_stage] = elapsed
            self.receipt.event(
                "stage.completed", stage=self._receipt_stage, seconds=elapsed
            )
            self._receipt_stage = name
            self._receipt_stage_started = now
        self.deadline.enter(name)
        self.receipt.event("stage.started", stage=name)
        self.receipt.resources(self.ledger)
        self.receipt.write()

    def _checkpoint(self) -> None:
        self.receipt.resources(self.ledger)
        self.receipt.write()

    def run(self) -> dict[str, Any]:
        failure: CanaryError | None = None
        failure_stage: str | None = None
        try:
            # Preflight is read-only.  Do not even create the receipt until all
            # credential, capacity, dirty-state, active-Sprint, and collision
            # checks pass (or fail and need a failure receipt).
            self.deadline.enter("preflight")
            self.facts = self.backend.preflight(self.config)
            self.receipt.data.update(
                {
                    "status": "running",
                    "candidate_sha": self.facts.candidate_sha,
                    "base_sha": self.facts.base_sha,
                    "repository": self.facts.repository,
                }
            )
            self.receipt.event(
                "preflight.passed",
                github_remaining=self.facts.github_remaining,
                api_port=self.facts.api_port,
                dev_port=self.facts.dev_port,
            )
            self.receipt.write()

            self._stage("materialize")
            self.backend.create_disposable(
                self.config,
                self.facts,
                self.ledger,
                self._checkpoint,
            )
            self.receipt.resources(self.ledger)
            self.receipt.event(
                "materialization.verified", engine_ref=self.facts.candidate_sha
            )
            self.receipt.write()

            self._stage("route_admission")
            launch_result = self.backend.launch(
                self.config, self.facts, self.ledger
            )
            versions = launch_result["versions"]
            route_admission = launch_result.get("route_admission")
            restart_rehearsal = launch_result.get("restart_rehearsal")
            if route_admission is not None:
                self.receipt.data["routes"]["admission"] = route_admission

            self._stage("launch")
            self.receipt.data["runtime"] = {
                "namespace": self.facts.network,
                "container": self.facts.container,
                "harness_versions": versions,
            }
            if restart_rehearsal is not None:
                self.receipt.data["runtime"]["restart_rehearsal"] = restart_rehearsal
            self.receipt.event("runtime.launched", harnesses=sorted(versions))
            self.receipt.write()

            outcome = self.backend.orchestrate(
                self.config,
                self.facts,
                self.ledger,
                self._stage,
                self._checkpoint,
            )
            self.receipt.data["routes"] = {
                **self.receipt.data["routes"],
                **outcome["routes"],
            }
            self.receipt.data["sprint"] = outcome["sprint"]
            self.receipt.data["pull_request"] = outcome["pull_request"]
            self.receipt.event("sprint.completed", **outcome["sprint"])
            self.receipt.resources(self.ledger)
            self.receipt.write()
        except CanaryError as exc:
            failure = exc
            failure_stage = self.deadline.stage_name
        except Exception as exc:  # noqa: BLE001 - receipt every unexpected boundary
            failure = CanaryError(
                "CANARY_INTERNAL_FAILED",
                f"unexpected {type(exc).__name__} during {self.deadline.stage_name}",
            )
            failure_stage = self.deadline.stage_name

        cleanup_failure: CanaryError | None = None
        try:
            self._stage("cleanup")
            actions = self.backend.cleanup(self.config, self.facts, self.ledger)
            self.receipt.data["cleanup"]["attempts"] += 1
            self.receipt.data["cleanup"].update(
                {"complete": True, "actions": sanitize(actions)}
            )
            self.receipt.event("cleanup.completed")
        except CanaryError as exc:
            cleanup_failure = exc
            self.receipt.data["cleanup"]["attempts"] += 1
            self.receipt.data["cleanup"].update(
                {
                    "complete": False,
                    "actions": sanitize(exc.details.get("actions", [])),
                }
            )
            self.receipt.event("cleanup.failed", code=exc.code)

        final_failure = cleanup_failure or failure
        now = self.deadline.clock()
        self.receipt.data["durations"][self._receipt_stage] = round(
            now - self._receipt_stage_started, 3
        )
        self.receipt.data["durations"]["whole_seconds"] = round(
            now - self.deadline.whole_started, 3
        )
        self.receipt.data["finished_at"] = utc_now()
        self.receipt.resources(self.ledger)
        if final_failure is None:
            self.receipt.data["status"] = "passed"
            self.receipt.data["next_action"] = (
                "Candidate DeepSeek Sprint receipt is green; bind it to exact-head review."
                if self.config.profile == DEEPSEEK_SPRINT_PROFILE
                else "Candidate receipt is green; task #353 may update the real dos-app install."
            )
            self.receipt.data["failure"] = None
        else:
            self.receipt.data["status"] = (
                "cleanup_failed" if cleanup_failure is not None else "failed"
            )
            primary_payload = (
                {
                    "code": failure.code,
                    "message": failure.message,
                    "details": failure.details,
                    "stage": failure_stage,
                }
                if failure is not None
                else None
            )
            cleanup_payload = (
                {
                    "code": cleanup_failure.code,
                    "message": cleanup_failure.message,
                    "details": cleanup_failure.details,
                    "stage": "cleanup",
                }
                if cleanup_failure is not None
                else None
            )
            self.receipt.data["failure"] = sanitize(
                {
                    "primary": primary_payload,
                    "cleanup": cleanup_payload,
                }
            )
            self.receipt.data["next_action"] = (
                "Run this command's cleanup subcommand with the receipt, repair the "
                "reported stable failure, then rerun with a new run id."
            )
        self.receipt.write()
        if final_failure is not None:
            raise final_failure
        return self.receipt.data


def default_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    entropy = hashlib.sha256(os.urandom(16)).hexdigest()[:8]
    return f"{stamp}-{entropy}".lower()


def build_config(args: argparse.Namespace) -> CanaryConfig:
    source = Path(args.source_repo).resolve()
    run_id = args.run_id or default_run_id()
    temp_parent_explicit = args.temp_parent is not None
    temp_parent = (
        Path(args.temp_parent)
        if temp_parent_explicit
        else Path(tempfile.gettempdir()).resolve()
    )
    receipt = (
        Path(args.receipt).resolve()
        if args.receipt
        else (source / "shared" / "canary-receipts" / f"{run_id}.json")
    )
    return CanaryConfig(
        source_repo=source,
        engine_ref=args.engine_ref,
        dos_app_repo=Path(args.dos_app_repo).resolve(),
        dos_app_ref=args.dos_app_ref,
        repository=args.repository,
        receipt_path=receipt,
        temp_parent=temp_parent,
        run_id=run_id,
        temp_parent_explicit=temp_parent_explicit,
        profile=args.profile,
        credential_file=(
            Path(args.credential_file).absolute()
            if args.credential_file is not None
            else None
        ),
        stage_timeout_s=args.stage_timeout,
        whole_timeout_s=args.whole_timeout,
        poll_interval_s=args.poll_interval,
    )


def _cleanup_from_receipt(path: Path) -> int:
    data, ledger = Receipt.load(path.resolve())
    run_id = str(data["run_id"])
    resources = data.get("resources") or {}
    workspace_value = resources.get("workspace")
    if workspace_value:
        workspace = _absolute_lexical(Path(str(workspace_value)))
        temp_parent = workspace.parent
    else:
        temp_parent = Path(tempfile.gettempdir()).resolve()
    repository = resources.get("repository") or data.get("repository")
    dos_app_repo = data.get("dos_app_repo")
    if not isinstance(dos_app_repo, str):
        raise CanaryError(
            "RECEIPT_INVALID",
            "receipt lacks dos_app_repo needed for idempotent branch cleanup",
        )
    config = CanaryConfig(
        source_repo=Path.cwd(),
        engine_ref=str(data.get("engine_ref_requested") or "unknown"),
        dos_app_repo=Path(dos_app_repo),
        dos_app_ref="origin/main",
        repository=repository if isinstance(repository, str) else None,
        receipt_path=path.resolve(),
        temp_parent=temp_parent,
        run_id=run_id,
        temp_parent_explicit=bool(data.get("temp_parent_explicit", False)),
        profile=str(data.get("profile") or STANDARD_PROFILE),
        stage_timeout_s=300,
        whole_timeout_s=600,
    )
    deadline = Deadline(config.whole_timeout_s, config.stage_timeout_s)
    backend = HostBackend(deadline)
    cleanup = data.setdefault("cleanup", {})
    temporary_receipt = Receipt(path.resolve(), config)
    temporary_receipt.data = data
    try:
        actions = backend.cleanup(config, None, ledger)
    except CanaryError as exc:
        cleanup["attempts"] = int(cleanup.get("attempts") or 0) + 1
        cleanup["complete"] = False
        cleanup["actions"] = sanitize(exc.details.get("actions", []))
        temporary_receipt.event("cleanup.failed", rerun=True, code=exc.code)
        temporary_receipt.write()
        raise
    else:
        cleanup["attempts"] = int(cleanup.get("attempts") or 0) + 1
        cleanup["complete"] = True
        cleanup["actions"] = sanitize(actions)
        data["resources"] = sanitize(dataclasses.asdict(ledger))
        temporary_receipt.event("cleanup.completed", rerun=True)
        temporary_receipt.write()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Source-only exact-ref dos-app Sprint promotion canary"
    )
    subparsers = result.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run a new disposable canary")
    run.add_argument(
        "--engine-ref", required=True, help="exact ref resolved in source repo"
    )
    run.add_argument("--source-repo", default=str(Path(__file__).resolve().parents[1]))
    run.add_argument("--dos-app-repo", required=True)
    run.add_argument("--dos-app-ref", default="origin/main")
    run.add_argument(
        "--repository", help="GitHub owner/name; derived from origin when omitted"
    )
    run.add_argument("--receipt", help="durable JSON path outside disposable state")
    run.add_argument("--temp-parent")
    run.add_argument("--run-id")
    run.add_argument("--profile", choices=sorted(PROFILES), default=STANDARD_PROFILE)
    run.add_argument("--credential-file")
    run.add_argument("--stage-timeout", type=float, default=900.0)
    run.add_argument("--whole-timeout", type=float, default=3600.0)
    run.add_argument("--poll-interval", type=float, default=2.0)
    cleanup = subparsers.add_parser(
        "cleanup", help="idempotently clean a prior receipt"
    )
    cleanup.add_argument("--receipt", required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "cleanup":
            return _cleanup_from_receipt(Path(args.receipt))
        config = build_config(args)
        deadline = Deadline(config.whole_timeout_s, config.stage_timeout_s)
        receipt = Receipt(config.receipt_path, config)
        controller = CanaryController(config, HostBackend(deadline), deadline, receipt)
        controller.run()
        print(config.receipt_path)
        return 0
    except CanaryError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
