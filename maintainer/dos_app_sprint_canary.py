#!/usr/bin/env python3
"""Run an exact-ref, disposable dos-app Sprint promotion canary.

This is source-maintainer tooling, not an ``sc`` verb.  It deliberately lives
outside the engine materialization set and controls a disposable installed fork
from the subfloor source checkout.

The command is intentionally conservative:

* every credential, capacity, dirty-state, active-Sprint, and remote collision
  check completes before a disposable install or remote ref is created;
* the candidate is resolved to one commit and that exact commit is installed;
* real browser conversations drive a Codex Planner/Developer and Kimi Reviewer;
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
import shutil
import socket
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
MIN_GITHUB_REMAINING = 100
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
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
HEX_SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,47}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
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
    ) -> dict[str, str]: ...

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


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 2
    except OSError:
        return False


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
        if not RUN_ID_RE.fullmatch(config.run_id):
            raise CanaryError("CANARY_INPUT_INVALID", "run_id is invalid")
        source = config.source_repo.resolve()
        target = config.dos_app_repo.resolve()
        receipt = config.receipt_path.resolve()
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
        self._run(
            ["docker", "image", "inspect", "super-coder-sandbox"],
            label="sandbox image preflight",
        )
        codex_auth = Path.home() / ".codex" / "auth.json"
        kimi_auth = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
        if not _nonempty_file(codex_auth) or not _nonempty_file(kimi_auth):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "Codex and Kimi host credentials must both be present",
            )
        try:
            free_bytes = shutil.disk_usage(temp_parent).free
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
            "subfloor-canary",
            str(config.source_repo.resolve()),
            label="add exact engine source",
        )
        self._git(
            facts.workspace,
            "fetch",
            "--no-tags",
            "subfloor-canary",
            f"{facts.candidate_sha}:refs/remotes/subfloor-canary/main",
            label="fetch exact candidate engine",
        )
        self._git(
            facts.workspace,
            "checkout",
            "refs/remotes/subfloor-canary/main",
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
    ) -> dict[str, str]:
        self._release_ports()
        env = {**os.environ, "SC_NET": facts.network}
        self._run(
            [str(facts.workspace / "sc"), "launch", "--no-build"],
            cwd=facts.workspace,
            env=env,
            label="launch isolated runtime",
        )
        api = JsonHttp(f"http://127.0.0.1:{facts.api_port}", self.deadline)
        while True:
            try:
                health = api.request("GET", "/api/health")
                if health:
                    break
            except CanaryError:
                pass
            self.deadline.remaining()
            self.sleep(min(config.poll_interval_s, self.deadline.remaining()))
        status = self._run(
            [str(facts.workspace / "sc"), "harness-status"],
            cwd=facts.workspace,
            env=env,
            label="inspect launched harness versions",
        ).stdout
        versions: dict[str, str] = {}
        for line in status.splitlines():
            match = re.match(r"\s*(claude|codex|kimi|opencode)\s+(.+?)\s*$", line)
            if match:
                versions[match.group(1)] = redact_text(match.group(2))[:160]
        if "codex" not in versions or "kimi" not in versions:
            raise CanaryError(
                "CANARY_RUNTIME_PROVENANCE_MISSING",
                "launched runtime did not report Codex and Kimi versions",
            )
        return versions

    def _wait_idle(
        self,
        api: JsonHttp,
        conversation_id: str,
        config: CanaryConfig,
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
                    },
                )
            self.deadline.remaining()
            self.sleep(min(config.poll_interval_s, self.deadline.remaining()))

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

    def _create_conversation(
        self,
        api: JsonHttp,
        *,
        shell_id: int,
        harness: str,
        key: str,
    ) -> dict[str, Any]:
        conversation = api.request(
            "POST",
            "/api/conversations",
            body={"shell_id": shell_id, "harness": harness},
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
                "real GitHub PR against the named ephemeral base, with REV1 reviewing via Kimi. "
                "Do not declare or arm a Sprint yet. Use only sc mem public commands, confirm "
                "every durable write, and stop after the feature, spec, and task exist."
            ),
            f"{config.run_id}:planner:prepare",
        )
        self._wait_idle(api, planner_id, config)
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

        stage("kimi_qaqc")
        reviewer = self._create_conversation(
            api,
            shell_id=shells["REV1"],
            harness="kimi",
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
            (
                f"Review spec document #{document_id} as the canary QA/QC Reviewer. "
                "Confirm it is limited to a deterministic file, an ephemeral-base PR, real "
                "Sprint lifecycle actions, and no change to main. If sound, run "
                f"sc sprint record-qaqc --document {document_id} --verdict pass. "
                "Confirm the durable approval and stop."
            ),
            f"{config.run_id}:reviewer:qaqc",
        )
        self._wait_idle(api, reviewer_id, config)
        approval_id = self._approval(facts, document_id)
        reviewer_latest = api.request("GET", f"/api/conversations/{reviewer_id}")
        api.request(
            "PATCH",
            f"/api/conversations/{reviewer_id}",
            body={"version": reviewer_latest["version"], "state": "closed"},
        )

        stage("declare_and_arm")
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
                "harness": "codex",
                "model": planner_route["model"],
                "effort": planner_route["effort"],
            },
            {
                "shell_id": shells["REV1"],
                "role": "reviewer",
                "harness": "kimi",
                "model": reviewer_route["model"],
                "effort": reviewer_route["effort"],
            },
        ]
        self._message(
            api,
            planner_id,
            (
                f"QA/QC approval #{approval_id} now covers document #{document_id}. "
                "Declare one merge-granted Sprint using the participant JSON below, plan one "
                f"code unit assigning task #{task_id} to DEV1 with REV1, then arm and dispatch. "
                f"Expected output: create {deterministic_path!r} containing exactly "
                f"{deterministic_content!r} plus a newline; use head branch "
                f"{facts.head_branch!r}, created from origin/{facts.base_branch}; "
                f"open the PR in {facts.repository!r} against base "
                f"{facts.base_branch!r}; never target main. The lane must register the PR, "
                "reach green, request real Force-new Kimi review, authorize and merge only "
                "through Sprint gates, and report the merge to you. After dispatch, handle "
                "your own informational Sprint inbox items and stop. Participants JSON: "
                + json.dumps(participants, separators=(",", ":"))
            ),
            f"{config.run_id}:planner:arm",
        )
        self._wait_idle(api, planner_id, config)
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
        board = api.request("GET", f"/api/sprints/{sprint_id}")
        if (board.get("sprint") or {}).get("lifecycle") != "armed":
            raise CanaryError("CANARY_ARM_FAILED", "canary Sprint is not armed")
        planner_latest = api.request("GET", f"/api/conversations/{planner_id}")
        api.request(
            "PATCH",
            f"/api/conversations/{planner_id}",
            body={"version": planner_latest["version"], "state": "closed"},
        )

        stage("sprint_execution")
        last_signature: tuple[Any, ...] | None = None
        observed_columns: list[str] = []
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
                if isinstance(column, str):
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
            workspace = Path(ledger.workspace).resolve()
            expected_workspace = (
                config.temp_parent.resolve() / f"{WORKSPACE_PREFIX}{config.run_id}"
            )
            marker = workspace / ".git" / "subfloor-canary-marker.json"
            if workspace.exists():
                marker_ok = False
                try:
                    marker_data = json.loads(marker.read_text())
                    marker_ok = (
                        marker_data.get("run_id") == config.run_id
                        and isinstance(ledger.candidate_sha, str)
                        and HEX_SHA.fullmatch(ledger.candidate_sha) is not None
                        and marker_data.get("candidate_sha") == ledger.candidate_sha
                    )
                except (OSError, json.JSONDecodeError):
                    marker_ok = False
                if workspace != expected_workspace or not marker_ok:
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

            self._stage("launch")
            versions = self.backend.launch(self.config, self.facts, self.ledger)
            self.receipt.data["runtime"] = {
                "namespace": self.facts.network,
                "container": self.facts.container,
                "harness_versions": versions,
            }
            self.receipt.event("runtime.launched", harnesses=sorted(versions))
            self.receipt.write()

            outcome = self.backend.orchestrate(
                self.config,
                self.facts,
                self.ledger,
                self._stage,
                self._checkpoint,
            )
            self.receipt.data["routes"] = outcome["routes"]
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
                "Candidate receipt is green; task #353 may update the real dos-app install."
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
        temp_parent=Path(args.temp_parent).resolve(),
        run_id=run_id,
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
        workspace = Path(str(workspace_value)).resolve()
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
    run.add_argument("--temp-parent", default=tempfile.gettempdir())
    run.add_argument("--run-id")
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
