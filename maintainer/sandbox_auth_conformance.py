#!/usr/bin/env python3
"""Run the disposable sandbox GitHub-auth conformance canary.

This source-maintainer harness exercises the assembled Feature #48 contract
against real GitHub and Docker boundaries.  It never prints or persists a
credential value.  Every remote branch, pull request, container, image, agent,
and workspace is uniquely named, recorded before use, and cleaned on success
or failure.  The durable receipt contains allow-listed state only.

The live canary is intentionally not run in CI.  Hermetic tests validate its
matrix, redaction, command construction, receipts, and cleanup orchestration.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

RECEIPT_VERSION = 1
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,47}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TOKEN_PATTERN = re.compile(
    r"\b(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b",
    re.IGNORECASE,
)
AUTH_ASSIGNMENT = re.compile(
    r"(?i)((?:authorization|api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"
)
SSH_SUCCESS = re.compile(
    r"successfully authenticated.*does not provide shell access", re.IGNORECASE
)
GITHUB_KEYS = re.compile(
    r"'github\.com (ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) "
    r"([A-Za-z0-9+/=]+)'"
)
REQUIRED_MATRIX = frozenset(
    {
        "ssh_oauth",
        "https_oauth",
        "explicit_scoped_token",
        "stale_explicit_fallback",
        "empty_candidate_fallback",
        "insufficient_push_access",
        "offline",
        "strict_host_trust",
        "rootless_agent_access",
        "rootful_agent_access",
        "relaunch_refresh",
        "restart_refresh",
        "ssh_push_and_pr",
        "https_push_and_pr",
    }
)
TOKEN_VARIABLES = (
    "SC_GH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


class CanaryError(RuntimeError):
    """A stable, receipt-safe canary failure."""

    def __init__(self, code: str, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage


@dataclasses.dataclass(frozen=True)
class Config:
    source_repo: Path
    repository: str
    ssh_key: Path
    receipt: Path
    run_id: str
    timeout_seconds: float = 1800.0


@dataclasses.dataclass
class Ledger:
    image: str | None = None
    agent_pid: int | None = None
    agent_socket: str | None = None
    agent_acl_uid: int | None = None
    dind_container: str | None = None
    branches: list[str] = dataclasses.field(default_factory=list)
    pull_requests: list[int] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact(value: str) -> str:
    value = TOKEN_PATTERN.sub("[REDACTED]", value)
    return AUTH_ASSIGNMENT.sub(r"\1[REDACTED]", value)


def _safe_result(value: Any, *, key: str | None = None) -> Any:
    if key and key.casefold() in {
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    }:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_result(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_result(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class Receipt:
    def __init__(self, path: Path, config: Config) -> None:
        self.path = path.resolve()
        self.data: dict[str, Any] = {
            "receipt_version": RECEIPT_VERSION,
            "run_id": config.run_id,
            "candidate_sha": None,
            "repository": config.repository,
            "status": "initializing",
            "stage": "preflight",
            "started_at": utc_now(),
            "finished_at": None,
            "matrix": {},
            "resources": dataclasses.asdict(Ledger()),
            "cleanup": {"complete": False, "actions": []},
            "failure": None,
        }

    def checkpoint(self, ledger: Ledger) -> None:
        self.data["resources"] = dataclasses.asdict(ledger)
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = _safe_result(self.data)
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


class Runner:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_bytes: bytes | None = None,
        text: bool = True,
        stage: str,
    ) -> CommandResult:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CanaryError("CANARY_TIMEOUT", "whole-run deadline exceeded", stage=stage)
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                input=input_bytes if not text else None,
                capture_output=True,
                text=text,
                timeout=remaining,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CanaryError(
                "CANARY_COMMAND_UNAVAILABLE",
                f"{stage} command could not complete",
                stage=stage,
            ) from exc
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        result = CommandResult(completed.returncode, stdout, stderr)
        if check and result.returncode != 0:
            raise CanaryError(
                "CANARY_COMMAND_FAILED",
                f"{stage} failed with exit {result.returncode}",
                stage=stage,
            )
        return result


class Backend(Protocol):
    def execute(self, receipt: Receipt, ledger: Ledger) -> dict[str, Any]: ...

    def cleanup(self, ledger: Ledger) -> list[dict[str, Any]]: ...


def _clean_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    for name in TOKEN_VARIABLES:
        environment.pop(name, None)
    if extra:
        environment.update(extra)
    return environment


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CanaryError(
            "CANARY_SOURCE_INVALID", f"cannot load {path.name}", stage="preflight"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _validate_matrix(matrix: Mapping[str, Any]) -> None:
    missing = sorted(REQUIRED_MATRIX - set(matrix))
    extra = sorted(set(matrix) - REQUIRED_MATRIX)
    if missing or extra:
        raise CanaryError(
            "CANARY_MATRIX_INCOMPLETE",
            f"matrix mismatch: missing={missing}, extra={extra}",
            stage="validate",
        )
    failed = sorted(
        name
        for name, evidence in matrix.items()
        if not isinstance(evidence, Mapping) or evidence.get("status") != "passed"
    )
    if failed:
        raise CanaryError(
            "CANARY_MATRIX_FAILED",
            f"matrix cases did not pass: {failed}",
            stage="validate",
        )


def _container_auth_args(
    socket_path: str | None, *, user: str, token: bool
) -> tuple[str, ...]:
    arguments: list[str] = ["--user", user]
    if socket_path is not None:
        arguments.extend(
            (
                "--mount",
                f"type=bind,src={socket_path},dst=/run/super-coder/ssh-agent,readonly",
                "-e",
                "SSH_AUTH_SOCK=/run/super-coder/ssh-agent",
            )
        )
    if token:
        arguments.extend(("-e", "GH_TOKEN"))
    return tuple(arguments)


def _ssh_failure_category(result: CommandResult) -> str:
    output = f"{result.stdout}\n{result.stderr}".casefold()
    categories = (
        ("no user exists for uid", "container_user_missing"),
        ("host key verification failed", "host_trust_rejected"),
        ("error connecting to agent", "agent_unreachable"),
        ("permission denied", "permission_denied"),
        ("bind source path does not exist", "socket_mount_missing"),
        ("invalid mount config", "socket_mount_invalid"),
        ("network is unreachable", "network_unavailable"),
        ("could not resolve hostname", "network_unavailable"),
    )
    return next(
        (category for marker, category in categories if marker in output),
        f"unexpected_exit_{result.returncode}",
    )


def _mapped_host_uid(uid_map: str, container_uid: int) -> int:
    for raw_line in uid_map.splitlines():
        fields = raw_line.split()
        if len(fields) != 3:
            continue
        try:
            container_start, host_start, length = map(int, fields)
        except ValueError:
            continue
        if container_start <= container_uid < container_start + length:
            return host_start + (container_uid - container_start)
    raise CanaryError(
        "CANARY_CONTAINER_FAILED",
        "rootful test UID is absent from the outer daemon mapping",
        stage="rootful dind",
    )


class HostBackend:
    def __init__(self, config: Config, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace
        self.runner = Runner(time.monotonic() + config.timeout_seconds)
        scripts = config.source_repo / ".super-coder" / "scripts"
        self.github_auth = _load_module(scripts / "github_auth.py", "canary_github_auth")
        self.github_login: str | None = None

    def _run(
        self,
        argv: Sequence[str],
        *,
        stage: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        return self.runner.run(
            argv, cwd=cwd, env=env, check=check, stage=stage
        )

    def _git(self, repo: Path, *args: str, stage: str, check: bool = True) -> CommandResult:
        return self._run(
            ("git", "-C", str(repo), *args), stage=stage, check=check
        )

    def _docker(
        self,
        *args: str,
        stage: str,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        return self._run(("docker", *args), stage=stage, env=env, check=check)

    def _preflight(self, ledger: Ledger, receipt: Receipt) -> tuple[str, str]:
        config = self.config
        if not RUN_ID_RE.fullmatch(config.run_id):
            raise CanaryError("CANARY_INPUT_INVALID", "run_id is invalid", stage="preflight")
        if not REPOSITORY_RE.fullmatch(config.repository):
            raise CanaryError(
                "CANARY_INPUT_INVALID", "repository must be owner/name", stage="preflight"
            )
        for executable in (
            "docker",
            "gh",
            "git",
            "ssh",
            "ssh-add",
            "ssh-agent",
            "setfacl",
        ):
            if shutil.which(executable) is None:
                raise CanaryError(
                    "CANARY_PREFLIGHT_FAILED",
                    f"required executable is missing: {executable}",
                    stage="preflight",
                )
        if not config.ssh_key.is_file() or config.ssh_key.suffix == ".pub":
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "ssh_key must name an existing private-key path",
                stage="preflight",
            )
        candidate = self._git(
            config.source_repo, "rev-parse", "HEAD", stage="resolve candidate"
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", candidate):
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "source HEAD is not one full commit",
                stage="preflight",
            )
        dirty = self._git(
            config.source_repo, "status", "--porcelain", stage="source clean gate"
        ).stdout.strip()
        if dirty:
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "source checkout must be clean before the live canary",
                stage="preflight",
            )
        self.github_login = self._run(
            ("gh", "api", "user", "--jq", ".login"),
            stage="GitHub API preflight",
        ).stdout.strip()
        if not self.github_login:
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "GitHub identity probe returned no login",
                stage="preflight",
            )
        permissions = self._run(
            (
                "gh",
                "api",
                f"repos/{config.repository}",
                "--jq",
                ".permissions.push",
            ),
            stage="GitHub repository preflight",
        ).stdout.strip()
        if permissions != "true":
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "GitHub credential cannot push to the target repository",
                stage="preflight",
            )
        token = self._run(("gh", "auth", "token"), stage="stored OAuth preflight").stdout.strip()
        if not token:
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "stored GitHub OAuth credential is unavailable",
                stage="preflight",
            )
        mode = self._docker(
            "info", "--format", "{{json .SecurityOptions}}", stage="Docker preflight"
        ).stdout.casefold()
        if "rootless" not in mode:
            raise CanaryError(
                "CANARY_PREFLIGHT_FAILED",
                "the primary canary daemon must exercise the rootless path",
                stage="preflight",
            )
        image = f"subfloor-auth-canary:{config.run_id}"
        dind = f"subfloor-auth-dind-{config.run_id}"
        image_collision = self._docker(
            "image", "inspect", image, stage="image collision preflight", check=False
        )
        if image_collision.returncode == 0:
            raise CanaryError(
                "CANARY_COLLISION",
                "the disposable image tag already exists",
                stage="preflight",
            )
        dind_collision = self._docker(
            "container", "inspect", dind, stage="container collision preflight", check=False
        )
        if dind_collision.returncode == 0:
            raise CanaryError(
                "CANARY_COLLISION",
                "the disposable rootful container already exists",
                stage="preflight",
            )
        for transport in ("ssh", "https"):
            branch = f"subfloor-auth-canary/{config.run_id}-{transport}"
            if self._remote_branch_exists(branch, token=token):
                raise CanaryError(
                    "CANARY_COLLISION",
                    f"disposable remote branch already exists for {transport}",
                    stage="preflight",
                )
            pull_requests = self._run(
                (
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    config.repository,
                    "--state",
                    "all",
                    "--head",
                    branch,
                    "--json",
                    "number",
                ),
                env=_clean_environment({"GH_TOKEN": token}),
                stage="pull-request collision preflight",
            )
            if json.loads(pull_requests.stdout):
                raise CanaryError(
                    "CANARY_COLLISION",
                    f"disposable pull request already exists for {transport}",
                    stage="preflight",
                )
        receipt.data["candidate_sha"] = candidate
        receipt.data["docker"] = {"primary_daemon": "rootless"}
        receipt.checkpoint(ledger)
        return token, candidate

    def _build_image(self, ledger: Ledger, receipt: Receipt) -> None:
        source = (self.config.source_repo / ".super-coder" / "Dockerfile").read_text()
        keys = GITHUB_KEYS.findall(source)
        if {kind for kind, _key in keys} != {
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ssh-rsa",
        }:
            raise CanaryError(
                "CANARY_SOURCE_INVALID",
                "Dockerfile does not contain the exact three pinned GitHub key types",
                stage="build image",
            )
        context = self.workspace / "image"
        context.mkdir()
        key_arguments = " ".join(
            f"'github.com {kind} {key}'" for kind, key in keys
        )
        dockerfile = f"""FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git gh openssh-client ca-certificates && rm -rf /var/lib/apt/lists/*
RUN install -d -m 0755 /etc/ssh/ssh_config.d && printf '%s\\n' {key_arguments} > /etc/ssh/ssh_known_hosts && chmod 0644 /etc/ssh/ssh_known_hosts && printf '%s\\n' 'Host github.com' '    StrictHostKeyChecking yes' '    GlobalKnownHostsFile /etc/ssh/ssh_known_hosts' '    UserKnownHostsFile /dev/null' > /etc/ssh/ssh_config.d/99-super-coder-github.conf
RUN git config --system credential.helper '' && git config --system credential.https://github.com.helper '!gh auth git-credential'
RUN if ! getent group {os.getgid()} >/dev/null; then groupadd -g {os.getgid()} authcanary; fi && useradd -m -u {os.getuid()} -g {os.getgid()} -s /bin/sh authcanary
ENV GIT_TERMINAL_PROMPT=0
"""
        (context / "Dockerfile").write_text(dockerfile)
        image = f"subfloor-auth-canary:{self.config.run_id}"
        self._docker(
            "build",
            "--pull",
            "--tag",
            image,
            str(context),
            stage="build auth canary image",
        )
        ledger.image = image
        receipt.checkpoint(ledger)

    def _start_agent(self, ledger: Ledger, receipt: Receipt) -> None:
        socket_path = self.workspace / "agent.sock"
        output = self._run(
            ("ssh-agent", "-a", str(socket_path), "-s"), stage="start disposable agent"
        ).stdout
        match = re.search(r"SSH_AGENT_PID=([0-9]+);", output)
        if match is None:
            raise CanaryError(
                "CANARY_AGENT_FAILED", "ssh-agent returned no pid", stage="agent"
            )
        ledger.agent_pid = int(match.group(1))
        ledger.agent_socket = str(socket_path)
        receipt.checkpoint(ledger)
        environment = _clean_environment({"SSH_AUTH_SOCK": str(socket_path)})
        self._run(
            ("ssh-add", str(self.config.ssh_key)),
            env=environment,
            stage="load disposable agent",
        )
        probe = self._run(
            (
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "git@github.com",
            ),
            env=environment,
            stage="host SSH auth probe",
            check=False,
        )
        if probe.returncode not in {0, 1} or not SSH_SUCCESS.search(
            f"{probe.stdout}\n{probe.stderr}"
        ):
            raise CanaryError(
                "CANARY_AGENT_FAILED",
                "the disposable agent did not authenticate to GitHub",
                stage="agent",
            )

    def _repo(self, name: str, origin: str) -> Path:
        path = self.workspace / name
        path.mkdir()
        self._git(path, "init", stage=f"initialize {name}")
        self._git(path, "remote", "add", "origin", origin, stage=f"configure {name}")
        return path

    def _discovery(self, repo: Path, environment: Mapping[str, str]) -> Any:
        return self.github_auth.discover_github_capabilities(
            repo, environ=environment
        )

    @staticmethod
    def _capability_evidence(result: Any) -> dict[str, Any]:
        return {
            "status": "passed",
            "origin_transport": result.origin.transport,
            "git_transport": result.git_transport.state,
            "github_api": result.github_api.state,
            "selected_source": result.runtime.token_source,
            "agent_selected": result.runtime.ssh_auth_sock is not None,
        }

    def _container_api_identity(self, token: str, ledger: Ledger, *, stage: str) -> None:
        assert ledger.image and self.github_login
        environment = _clean_environment({"GH_TOKEN": token})
        result = self._docker(
            "run",
            "--rm",
            "-e",
            "GH_TOKEN",
            ledger.image,
            "gh",
            "api",
            "user",
            "--jq",
            ".login",
            env=environment,
            stage=stage,
        )
        if result.stdout.strip() != self.github_login:
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "replacement container resolved a different GitHub identity",
                stage=stage,
            )

    def _discovery_matrix(self, token: str, ledger: Ledger) -> dict[str, Any]:
        assert ledger.agent_socket is not None
        repository = self.config.repository
        https = self._repo("https-discovery", f"https://github.com/{repository}.git")
        ssh = self._repo("ssh-discovery", f"git@github.com:{repository}.git")
        base = _clean_environment({"SSH_AUTH_SOCK": ledger.agent_socket})

        https_oauth = self._discovery(https, base)
        ssh_oauth = self._discovery(ssh, base)
        explicit = self._discovery(https, {**base, "SC_GH_TOKEN": token})
        stale = self._discovery(
            https, {**base, "SC_GH_TOKEN": "github_pat_deliberately-invalid"}
        )
        empty = self._discovery(
            https,
            {**base, "SC_GH_TOKEN": " ", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        )
        expected = (
            (https_oauth, "https", "gh_oauth", False),
            (ssh_oauth, "ssh", "gh_oauth", True),
            (explicit, "https", "sc_gh_token", False),
            (stale, "https", "gh_oauth", False),
            (empty, "https", "gh_oauth", False),
        )
        for result, transport, source, agent in expected:
            if (
                result.git_transport.state != "ready"
                or result.github_api.state != "ready"
                or result.origin.transport != transport
                or result.runtime.token_source != source
                or (result.runtime.ssh_auth_sock is not None) != agent
            ):
                raise CanaryError(
                    "CANARY_DISCOVERY_FAILED",
                    f"discovery matrix did not select {transport}/{source}",
                    stage="discovery matrix",
                )
        stale_attempts = [
            (item.source, item.state, item.reason) for item in stale.credential_attempts
        ]
        if stale_attempts[:2] != [
            ("sc_gh_token", "unavailable", "credential_rejected"),
            ("gh_oauth", "ready", "repository_read_verified"),
        ]:
            raise CanaryError(
                "CANARY_DISCOVERY_FAILED",
                "stale explicit token did not fall through to stored OAuth",
                stage="discovery matrix",
            )
        for result, stage in (
            (explicit, "initial explicit-token launch"),
            (stale, "stale-token relaunch"),
            (empty, "empty-token restart"),
        ):
            selected = result.runtime.gh_token
            if selected is None:
                raise CanaryError(
                    "CANARY_DISCOVERY_FAILED",
                    "lifecycle discovery selected no runtime token",
                    stage="discovery matrix",
                )
            self._container_api_identity(selected, ledger, stage=stage)
        return {
            "https_oauth": self._capability_evidence(https_oauth),
            "ssh_oauth": self._capability_evidence(ssh_oauth),
            "explicit_scoped_token": self._capability_evidence(explicit),
            "stale_explicit_fallback": self._capability_evidence(stale),
            "empty_candidate_fallback": self._capability_evidence(empty),
            "relaunch_refresh": {
                "status": "passed",
                "before_source": explicit.runtime.token_source,
                "after_source": stale.runtime.token_source,
                "replacement_api": "passed",
            },
            "restart_refresh": {
                "status": "passed",
                "before_source": stale.runtime.token_source,
                "after_source": empty.runtime.token_source,
                "replacement_api": "passed",
            },
        }

    def _container_ssh(
        self,
        *,
        image: str,
        socket_path: str,
        user: str,
        docker_prefix: Sequence[str] = (),
    ) -> bool:
        args = [
            *docker_prefix,
            "run",
            "--rm",
            *_container_auth_args(socket_path, user=user, token=False),
            image,
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "git@github.com",
        ]
        result = self._run(
            args, stage=f"container SSH probe ({user})", check=False
        )
        return result.returncode in {0, 1} and bool(
            SSH_SUCCESS.search(f"{result.stdout}\n{result.stderr}")
        )

    def _strict_trust(self, ledger: Ledger) -> dict[str, Any]:
        assert ledger.image and ledger.agent_socket
        if not self._container_ssh(
            image=ledger.image, socket_path=ledger.agent_socket, user="0:0", docker_prefix=("docker",)
        ):
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "engine-pinned trust did not authenticate",
                stage="strict host trust",
            )
        args = (
            "docker",
            "run",
            "--rm",
            *_container_auth_args(ledger.agent_socket, user="0:0", token=False),
            ledger.image,
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "git@github.com",
        )
        mismatch = self._run(args, stage="strict trust rejection", check=False)
        if mismatch.returncode == 0 or SSH_SUCCESS.search(
            f"{mismatch.stdout}\n{mismatch.stderr}"
        ):
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "strict host trust accepted an empty trust store",
                stage="strict host trust",
            )
        return {"status": "passed", "known_host": "accepted", "empty_trust": "rejected"}

    def _offline(self, token: str, ledger: Ledger) -> dict[str, Any]:
        assert ledger.image
        repo = self._repo(
            "offline-discovery", f"https://github.com/{self.config.repository}.git"
        )
        scripts = self.config.source_repo / ".super-coder" / "scripts"
        environment = _clean_environment({"SC_GH_TOKEN": token})
        result = self._docker(
            "run",
            "--rm",
            "--network",
            "none",
            "-e",
            "SC_GH_TOKEN",
            "-v",
            f"{repo}:/work:ro",
            "-v",
            f"{scripts}:/scripts:ro",
            ledger.image,
            "python3",
            "/scripts/github_auth.py",
            "discover",
            "--repo-root",
            "/work",
            env=environment,
            stage="offline discovery",
        )
        try:
            discovery = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "offline discovery did not return JSON",
                stage="offline",
            ) from exc
        if (
            discovery.get("git_transport_state") != "unverified"
            or discovery.get("github_api_state") != "unverified"
            or discovery.get("validated_selected_token") is not None
        ):
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "offline discovery made an invalid readiness claim: "
                f"git={discovery.get('git_transport_state')}/"
                f"{discovery.get('git_transport_reason')}, "
                f"api={discovery.get('github_api_state')}/"
                f"{discovery.get('github_api_reason')}, "
                "token_selected="
                f"{discovery.get('validated_selected_token') is not None}",
                stage="offline",
            )
        return {"status": "passed", "git_transport": "unverified", "github_api": "unverified"}

    def _start_rootful_dind(self, ledger: Ledger, receipt: Receipt) -> None:
        assert ledger.agent_socket and ledger.image
        ledger.dind_container = f"subfloor-auth-dind-{self.config.run_id}"
        receipt.checkpoint(ledger)
        self._docker(
            "run",
            "--detach",
            "--privileged",
            "--name",
            ledger.dind_container,
            "--mount",
            f"type=bind,src={ledger.agent_socket},dst=/agent.sock,readonly",
            "--mount",
            f"type=bind,src={self.workspace / 'image'},dst=/context,readonly",
            "docker:29-dind",
            "--host=tcp://0.0.0.0:2375",
            "--tls=false",
            stage="start rootful dind",
        )
        for _attempt in range(60):
            probe = self._docker(
                "exec",
                ledger.dind_container,
                "docker",
                "info",
                "--format",
                "{{json .SecurityOptions}}",
                stage="rootful dind readiness",
                check=False,
            )
            if probe.returncode == 0:
                if "rootless" in probe.stdout.casefold():
                    raise CanaryError(
                        "CANARY_CONTAINER_FAILED",
                        "nested daemon unexpectedly reports rootless",
                        stage="rootful dind",
                    )
                break
            time.sleep(0.5)
        else:
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "nested rootful daemon did not become ready",
                stage="rootful dind",
            )
        uid_map = self._docker(
            "exec",
            ledger.dind_container,
            "cat",
            "/proc/self/uid_map",
            stage="rootful dind uid mapping",
        ).stdout
        mapped_uid = _mapped_host_uid(uid_map, os.getuid())
        self._run(
            (
                "setfacl",
                "--modify",
                f"u:{mapped_uid}:rw",
                ledger.agent_socket,
            ),
            stage="rootful agent ownership bridge",
        )
        ledger.agent_acl_uid = mapped_uid
        receipt.checkpoint(ledger)
        self._docker(
            "exec",
            ledger.dind_container,
            "docker",
            "build",
            "--tag",
            ledger.image,
            "/context",
            stage="build nested auth image",
        )

    def _rootful_agent(self, ledger: Ledger) -> dict[str, Any]:
        assert ledger.image and ledger.dind_container
        args = (
            "docker",
            "exec",
            ledger.dind_container,
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            "type=bind,src=/agent.sock,dst=/run/super-coder/ssh-agent,readonly",
            "-e",
            "SSH_AUTH_SOCK=/run/super-coder/ssh-agent",
            ledger.image,
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "git@github.com",
        )
        result = self._run(args, stage="rootful agent probe", check=False)
        if result.returncode not in {0, 1} or not SSH_SUCCESS.search(
            f"{result.stdout}\n{result.stderr}"
        ):
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "rootful daemon container user could not use the agent: "
                f"{_ssh_failure_category(result)}",
                stage="rootful agent",
            )
        return {"status": "passed", "daemon": "rootful_dind", "user": f"{os.getuid()}:{os.getgid()}"}

    def _container_command(
        self,
        ledger: Ledger,
        token: str,
        workspace: Path,
        command: Sequence[str],
        *,
        ssh: bool,
        stage: str,
        check: bool = True,
    ) -> CommandResult:
        assert ledger.image
        auth = _container_auth_args(
            ledger.agent_socket if ssh else None,
            user="0:0",
            token=True,
        )
        environment = _clean_environment({"GH_TOKEN": token})
        return self._docker(
            "run",
            "--rm",
            *auth,
            "-v",
            f"{workspace}:/workspace",
            "-w",
            "/workspace",
            ledger.image,
            *command,
            env=environment,
            stage=stage,
            check=check,
        )

    def _real_pr(
        self,
        token: str,
        ledger: Ledger,
        receipt: Receipt,
        *,
        transport: str,
    ) -> dict[str, Any]:
        ssh = transport == "ssh"
        branch = f"subfloor-auth-canary/{self.config.run_id}-{transport}"
        ledger.branches.append(branch)
        receipt.checkpoint(ledger)
        workspace = self.workspace / f"pr-{transport}"
        workspace.mkdir()
        remote = (
            f"git@github.com:{self.config.repository}.git"
            if ssh
            else f"https://github.com/{self.config.repository}.git"
        )
        self._container_command(
            ledger,
            token,
            workspace,
            ("git", "clone", "--depth", "1", remote, "repo"),
            ssh=ssh,
            stage=f"{transport} clone",
        )
        repo = workspace / "repo"
        self._container_command(
            ledger,
            token,
            workspace,
            (
                "git",
                "-C",
                "/workspace/repo",
                "checkout",
                "-b",
                branch,
            ),
            ssh=ssh,
            stage=f"{transport} branch",
        )
        proof = repo / "canary" / f"{self.config.run_id}-{transport}.txt"
        proof.parent.mkdir()
        proof.write_text(f"sandbox auth conformance {self.config.run_id} {transport}\n")
        for command, stage in (
            (("git", "-C", "/workspace/repo", "add", "canary"), f"{transport} add"),
            (
                (
                    "git",
                    "-C",
                    "/workspace/repo",
                    "-c",
                    "user.name=super-coder auth canary",
                    "-c",
                    "user.email=noreply@super-coder.local",
                    "commit",
                    "-m",
                    f"test: {transport} auth canary {self.config.run_id}",
                ),
                f"{transport} commit",
            ),
            (
                (
                    "git",
                    "-C",
                    "/workspace/repo",
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{branch}",
                ),
                f"{transport} push",
            ),
        ):
            self._container_command(
                ledger, token, workspace, command, ssh=ssh, stage=stage
            )
        origin_after = self._git(repo, "remote", "get-url", "origin", stage="origin preservation").stdout.strip()
        if origin_after != remote:
            raise CanaryError(
                "CANARY_GITHUB_FAILED",
                f"{transport} origin changed during the canary",
                stage=f"{transport} PR",
            )
        created = self._container_command(
            ledger,
            token,
            workspace,
            (
                "gh",
                "pr",
                "create",
                "--repo",
                self.config.repository,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                f"Auth canary {self.config.run_id} ({transport})",
                "--body",
                "Disposable sandbox authentication conformance probe; close without merge.",
            ),
            ssh=ssh,
            stage=f"{transport} PR create",
        )
        match = re.search(r"/pull/([0-9]+)", created.stdout)
        if match is None:
            raise CanaryError(
                "CANARY_GITHUB_FAILED",
                "gh pr create returned no pull-request number",
                stage=f"{transport} PR",
            )
        pr_number = int(match.group(1))
        ledger.pull_requests.append(pr_number)
        receipt.checkpoint(ledger)
        state = self._run(
            (
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                self.config.repository,
                "--json",
                "state,headRefName,baseRefName",
            ),
            env=_clean_environment({"GH_TOKEN": token}),
            stage=f"{transport} PR verify",
        )
        projection = json.loads(state.stdout)
        if projection != {"baseRefName": "main", "headRefName": branch, "state": "OPEN"}:
            raise CanaryError(
                "CANARY_GITHUB_FAILED",
                "created pull request did not match the disposable branch",
                stage=f"{transport} PR",
            )
        self._run(
            (
                "gh",
                "pr",
                "close",
                str(pr_number),
                "--repo",
                self.config.repository,
                "--delete-branch",
            ),
            env=_clean_environment({"GH_TOKEN": token}),
            stage=f"{transport} PR cleanup",
        )
        ledger.pull_requests.remove(pr_number)
        ledger.branches.remove(branch)
        receipt.checkpoint(ledger)
        return {"status": "passed", "pull_request": pr_number, "closed": True, "branch_deleted": True}

    def _insufficient_push(self, token: str, ledger: Ledger) -> dict[str, Any]:
        workspace = self.workspace / "insufficient"
        workspace.mkdir()
        repo = workspace / "repo"
        repo.mkdir()
        self._git(repo, "init", stage="insufficient repo init")
        self._git(
            repo,
            "remote",
            "add",
            "origin",
            "https://github.com/cli/cli.git",
            stage="insufficient origin",
        )
        (repo / "proof.txt").write_text("must not reach remote\n")
        self._git(repo, "add", "proof.txt", stage="insufficient add")
        self._git(
            repo,
            "-c",
            "user.name=super-coder auth canary",
            "-c",
            "user.email=noreply@super-coder.local",
            "commit",
            "-m",
            "test: denied push",
            stage="insufficient commit",
        )
        result = self._container_command(
            ledger,
            token,
            workspace,
            (
                "git",
                "-C",
                "/workspace/repo",
                "push",
                "origin",
                f"HEAD:refs/heads/subfloor-auth-denied-{self.config.run_id}",
            ),
            ssh=False,
            stage="insufficient push",
            check=False,
        )
        if result.returncode == 0:
            raise CanaryError(
                "CANARY_GITHUB_FAILED",
                "a push unexpectedly succeeded against an ungranted repository",
                stage="insufficient access",
            )
        return {"status": "passed", "readiness_claim": "read_only", "push": "rejected"}

    def execute(self, receipt: Receipt, ledger: Ledger) -> dict[str, Any]:
        token, _candidate = self._preflight(ledger, receipt)
        self._build_image(ledger, receipt)
        self._start_agent(ledger, receipt)
        matrix = self._discovery_matrix(token, ledger)
        assert ledger.image and ledger.agent_socket
        rootless = self._container_ssh(
            image=ledger.image,
            socket_path=ledger.agent_socket,
            user="0:0",
            docker_prefix=("docker",),
        )
        if not rootless:
            raise CanaryError(
                "CANARY_CONTAINER_FAILED",
                "rootless daemon container root could not use the agent",
                stage="rootless agent",
            )
        matrix["rootless_agent_access"] = {
            "status": "passed",
            "daemon": "rootless",
            "user": "0:0",
        }
        matrix["strict_host_trust"] = self._strict_trust(ledger)
        matrix["offline"] = self._offline(token, ledger)
        self._start_rootful_dind(ledger, receipt)
        matrix["rootful_agent_access"] = self._rootful_agent(ledger)
        matrix["insufficient_push_access"] = self._insufficient_push(token, ledger)
        matrix["ssh_push_and_pr"] = self._real_pr(
            token, ledger, receipt, transport="ssh"
        )
        matrix["https_push_and_pr"] = self._real_pr(
            token, ledger, receipt, transport="https"
        )
        return matrix

    def cleanup(self, ledger: Ledger) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        environment = _clean_environment()
        for pr_number in list(reversed(ledger.pull_requests)):
            result = self._run(
                (
                    "gh",
                    "pr",
                    "close",
                    str(pr_number),
                    "--repo",
                    self.config.repository,
                ),
                env=environment,
                stage="cleanup pull request",
                check=False,
            )
            removed = result.returncode == 0
            if not removed:
                observed = self._run(
                    (
                        "gh",
                        "pr",
                        "view",
                        str(pr_number),
                        "--repo",
                        self.config.repository,
                        "--json",
                        "state",
                        "--jq",
                        ".state",
                    ),
                    env=environment,
                    stage="verify pull-request cleanup",
                    check=False,
                )
                removed = observed.returncode == 0 and observed.stdout.strip() == "CLOSED"
            actions.append({"resource": f"pr:{pr_number}", "removed": removed})
            if removed:
                ledger.pull_requests.remove(pr_number)
        for branch in list(reversed(ledger.branches)):
            result = self._run(
                (
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{self.config.repository}/git/refs/heads/{branch}",
                ),
                env=environment,
                stage="cleanup branch",
                check=False,
            )
            removed = result.returncode == 0 or not self._remote_branch_exists(
                branch, token=None
            )
            actions.append({"resource": f"branch:{branch}", "removed": removed})
            if removed:
                ledger.branches.remove(branch)
        if ledger.dind_container:
            result = self._docker(
                "rm", "--force", ledger.dind_container, stage="cleanup dind", check=False
            )
            actions.append({"resource": "rootful_dind", "removed": result.returncode == 0})
            if result.returncode == 0:
                ledger.dind_container = None
        if ledger.image:
            result = self._docker(
                "image", "rm", "--force", ledger.image, stage="cleanup image", check=False
            )
            actions.append({"resource": "canary_image", "removed": result.returncode == 0})
            if result.returncode == 0:
                ledger.image = None
        if ledger.agent_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(ledger.agent_pid, 15)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(ledger.agent_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            removed = not Path(ledger.agent_socket or "").exists()
            actions.append({"resource": "ssh_agent", "removed": removed})
            if removed:
                ledger.agent_pid = None
                ledger.agent_socket = None
                ledger.agent_acl_uid = None
        return actions

    def _remote_branch_exists(self, branch: str, *, token: str | None) -> bool:
        environment = _clean_environment({"GH_TOKEN": token}) if token else _clean_environment()
        result = self._run(
            (
                "gh",
                "api",
                f"repos/{self.config.repository}/git/matching-refs/heads/{branch}",
                "--jq",
                "length",
            ),
            env=environment,
            stage="remote branch probe",
            check=False,
        )
        if result.returncode != 0:
            raise CanaryError(
                "CANARY_GITHUB_FAILED",
                "could not verify remote branch state",
                stage="remote branch probe",
            )
        try:
            return int(result.stdout.strip()) != 0
        except ValueError as exc:
            raise CanaryError(
                "CANARY_GITHUB_FAILED",
                "remote branch probe returned invalid evidence",
                stage="remote branch probe",
            ) from exc


def run(config: Config, *, backend: Backend | None = None) -> dict[str, Any]:
    ledger = Ledger()
    receipt = Receipt(config.receipt, config)
    receipt.write()
    owned_workspace: tempfile.TemporaryDirectory[str] | None = None
    if backend is None:
        owned_workspace = tempfile.TemporaryDirectory(
            prefix=f"subfloor-auth-canary-{config.run_id}-"
        )
        backend = HostBackend(config, Path(owned_workspace.name))
    failure: CanaryError | None = None
    try:
        receipt.data["stage"] = "execute"
        receipt.write()
        matrix = backend.execute(receipt, ledger)
        _validate_matrix(matrix)
        receipt.data["matrix"] = matrix
        receipt.data["status"] = "passed"
        receipt.data["stage"] = "cleanup"
    except CanaryError as exc:
        failure = exc
        receipt.data["status"] = "failed"
        receipt.data["stage"] = exc.stage
        receipt.data["failure"] = {"code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001 - secret-safe top-level boundary
        failure = CanaryError(
            "CANARY_INTERNAL_FAILED", "unexpected internal failure", stage="internal"
        )
        receipt.data["status"] = "failed"
        receipt.data["stage"] = "internal"
        receipt.data["failure"] = {
            "code": failure.code,
            "message": failure.message,
            "exception_type": type(exc).__name__,
        }
    finally:
        try:
            actions = backend.cleanup(ledger)
        except Exception:  # noqa: BLE001 - preserve primary failure and receipt
            actions = [{"resource": "cleanup", "removed": False}]
        cleanup_complete = (
            all(action.get("removed") is True for action in actions)
            and not ledger.branches
            and not ledger.pull_requests
            and ledger.dind_container is None
            and ledger.image is None
            and ledger.agent_pid is None
            and ledger.agent_acl_uid is None
        )
        receipt.data["resources"] = dataclasses.asdict(ledger)
        receipt.data["cleanup"] = {
            "complete": cleanup_complete,
            "actions": actions,
        }
        if not cleanup_complete:
            receipt.data["status"] = "failed"
            receipt.data["stage"] = "cleanup"
            receipt.data["failure"] = {
                "code": "CANARY_CLEANUP_FAILED",
                "message": "one or more disposable resources remain",
            }
            failure = CanaryError(
                "CANARY_CLEANUP_FAILED",
                "one or more disposable resources remain",
                stage="cleanup",
            )
        receipt.data["finished_at"] = utc_now()
        receipt.write()
        if owned_workspace is not None:
            owned_workspace.cleanup()
    if failure is not None:
        raise failure
    return receipt.data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the disposable sandbox GitHub-auth conformance canary"
    )
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = Config(
        source_repo=args.source_repo.resolve(),
        repository=args.repository,
        ssh_key=args.ssh_key.resolve(),
        receipt=args.receipt.resolve(),
        run_id=args.run_id,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        result = run(config)
    except CanaryError as exc:
        print(f"sandbox auth canary: {exc.code} at {exc.stage}; receipt: {config.receipt}", file=sys.stderr)
        return 1
    print(
        "sandbox auth canary: passed; "
        f"candidate={result['candidate_sha']} receipt={config.receipt}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
