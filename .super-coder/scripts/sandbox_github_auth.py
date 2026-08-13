"""Translate validated GitHub discovery into secret-safe Docker launch inputs."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

AGENT_TARGET = "/run/super-coder/ssh-agent"
AUTH_ARGUMENTS_MARKER = "SC_GITHUB_AUTH_ARGS"
_CLEARED_HOST_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK")
_NUMERIC_ID = re.compile(r"\A[0-9]+\Z")
_SAFE_DIAGNOSTIC = re.compile(r"\A[a-z0-9_]+\Z")
_CAPABILITY_STATES = {"ready", "unavailable", "unverified"}


class RuntimeSelection(Protocol):
    """The stable flat result emitted by ``github_auth.py discover``."""

    @property
    def origin_transport(self) -> str | None: ...

    @property
    def validated_agent_socket(self) -> str | None: ...

    @property
    def validated_selected_token(self) -> str | None: ...


@dataclass(frozen=True)
class RuntimeArguments:
    """Docker argv plus a separately held host environment.

    ``docker_args`` contains only ``-e GH_TOKEN``. The value stays in the
    Docker client's environment and therefore cannot enter argv or ordinary
    command logging. ``repr`` and ``diagnostic_dict`` never expose it.
    """

    docker_args: tuple[str, ...]
    container_user: str
    agent_forwarded: bool
    agent_reason: str
    token_injected: bool
    _environment: tuple[tuple[str, str], ...] = field(repr=False)

    def host_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        environment = dict(base)
        for name in _CLEARED_HOST_VARIABLES:
            environment.pop(name, None)
        environment.update(self._environment)
        return environment

    def diagnostic_dict(self) -> dict[str, object]:
        return {
            "container_user": self.container_user,
            "agent_forwarded": self.agent_forwarded,
            "agent_reason": self.agent_reason,
            "token_injected": self.token_injected,
        }


def _live_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _container_user(*, rootless: bool, uid: int, gid: int) -> str:
    uid_text = str(uid)
    gid_text = str(gid)
    if not _NUMERIC_ID.fullmatch(uid_text) or not _NUMERIC_ID.fullmatch(gid_text):
        raise ValueError("uid and gid must be non-negative integers")
    return "0:0" if rootless else f"{uid_text}:{gid_text}"


def build_runtime_arguments(
    discovery: RuntimeSelection,
    *,
    rootless: bool,
    uid: int,
    gid: int,
) -> RuntimeArguments:
    """Build the minimal Docker auth segment from one validated result.

    Discovery owns identity/repository probing. This boundary revalidates only
    the launch-sensitive facts: selected tokens must still be non-empty and a
    selected agent path must still resolve to a live Unix socket. A vanished or
    Docker-``--mount``-unsafe socket degrades SSH forwarding without failing the
    whole sandbox launch.
    """

    docker_args: list[str] = [
        "--user",
        _container_user(rootless=rootless, uid=uid, gid=gid),
    ]
    environment: list[tuple[str, str]] = []

    token = discovery.validated_selected_token
    selected_token = token if token is not None and token.strip() else None
    token_injected = selected_token is not None
    if selected_token is not None:
        environment.append(("GH_TOKEN", selected_token))
        docker_args.extend(("-e", "GH_TOKEN"))

    agent_forwarded = False
    agent_reason = "not_selected"
    selected_socket = discovery.validated_agent_socket
    if discovery.origin_transport != "ssh":
        agent_reason = "origin_transport_not_ssh"
    elif selected_socket:
        candidate = Path(selected_socket)
        if not candidate.is_absolute():
            agent_reason = "socket_path_not_absolute"
        else:
            host_path = str(candidate)
            if "," in host_path:
                agent_reason = "socket_path_unsupported"
            elif not _live_socket(host_path):
                agent_reason = "socket_not_live"
            else:
                docker_args.extend(
                    (
                        "--mount",
                        f"type=bind,src={host_path},dst={AGENT_TARGET},readonly",
                        "-e",
                        f"SSH_AUTH_SOCK={AGENT_TARGET}",
                    )
                )
                agent_forwarded = True
                agent_reason = "forwarded"

    return RuntimeArguments(
        docker_args=tuple(docker_args),
        container_user=docker_args[1],
        agent_forwarded=agent_forwarded,
        agent_reason=agent_reason,
        token_injected=token_injected,
        _environment=tuple(environment),
    )


@dataclass(frozen=True)
class ParsedDiscovery:
    origin_transport: str | None
    validated_agent_socket: str | None
    validated_selected_token: str | None = field(repr=False)
    origin_state: str | None = None
    origin_reason: str | None = None
    git_transport_state: str | None = None
    git_transport_reason: str | None = None
    github_api_state: str | None = None
    github_api_reason: str | None = None
    selected_token_source: str | None = None
    credential_attempts: tuple[tuple[str, str], ...] = ()


def _optional_state(value: Mapping[str, object], name: str) -> str | None:
    state = value.get(name)
    if state is None:
        return None
    if not isinstance(state, str) or state not in _CAPABILITY_STATES:
        raise ValueError(f"{name} must be ready, unavailable, or unverified")
    return state


def _optional_diagnostic(value: Mapping[str, object], name: str) -> str | None:
    diagnostic = value.get(name)
    if diagnostic is None:
        return None
    if not isinstance(diagnostic, str) or not _SAFE_DIAGNOSTIC.fullmatch(diagnostic):
        raise ValueError(f"{name} must be a safe diagnostic identifier")
    return diagnostic


def _credential_attempts(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw_attempts = value.get("credential_attempts")
    if not isinstance(raw_attempts, list):
        return ()
    attempts: list[tuple[str, str]] = []
    for raw_attempt in raw_attempts:
        if not isinstance(raw_attempt, Mapping):
            continue
        state = raw_attempt.get("state")
        reason = raw_attempt.get("reason")
        if (
            isinstance(state, str)
            and state in _CAPABILITY_STATES
            and isinstance(reason, str)
            and _SAFE_DIAGNOSTIC.fullmatch(reason)
        ):
            attempts.append((state, reason))
    return tuple(attempts)


def parse_discovery(raw: str) -> ParsedDiscovery:
    """Validate the three stable v1 integration fields from discovery JSON."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("discovery output is not one JSON object") from exc
    if not isinstance(value, dict):
        raise TypeError("discovery output must be one JSON object")
    required = {
        "origin_transport",
        "validated_agent_socket",
        "validated_selected_token",
    }
    if not required.issubset(value):
        raise ValueError("discovery output is missing stable v1 fields")

    transport = value.get("origin_transport")
    if transport not in {"ssh", "https", None}:
        raise ValueError("origin_transport must be ssh, https, or null")
    agent = value.get("validated_agent_socket")
    if agent is not None and not isinstance(agent, str):
        raise ValueError("validated_agent_socket must be a string or null")
    token = value.get("validated_selected_token")
    if token is not None and not isinstance(token, str):
        raise ValueError("validated_selected_token must be a string or null")
    return ParsedDiscovery(
        transport,
        agent,
        token,
        origin_state=_optional_state(value, "origin_state"),
        origin_reason=_optional_diagnostic(value, "origin_reason"),
        git_transport_state=_optional_state(value, "git_transport_state"),
        git_transport_reason=_optional_diagnostic(value, "git_transport_reason"),
        github_api_state=_optional_state(value, "github_api_state"),
        github_api_reason=_optional_diagnostic(value, "github_api_reason"),
        selected_token_source=_optional_diagnostic(value, "selected_token_source"),
        credential_attempts=_credential_attempts(value),
    )


def _display_reason(reason: str | None) -> str:
    return (reason or "reason unavailable").replace("_", " ")


def _source_label(source: str | None) -> str:
    return {
        "sc_gh_token": "SC_GH_TOKEN",
        "gh_token": "host GH_TOKEN",
        "github_token": "host GITHUB_TOKEN",
        "gh_oauth": "host gh OAuth",
    }.get(source, "validated host credential")


def _capability_line(
    label: str,
    state: str | None,
    reason: str | None,
    *,
    ready_detail: str,
) -> str:
    if state == "ready":
        return f"  {label}: ready — {ready_detail}"
    claim = state or "unverified"
    return (
        f"  {label}: {claim} — {_display_reason(reason)}; "
        "no readiness claim"
    )


def render_capability_summary(
    discovery: RuntimeSelection,
    runtime: RuntimeArguments,
) -> str:
    """Render only safe, current-lifecycle capability evidence and remedies."""
    origin_reason = getattr(discovery, "origin_reason", None)
    if origin_reason == "non_github_origin":
        return "\n".join(
            (
                "→ GitHub capabilities refreshed for this launch",
                "  skipped — origin is not a supported github.com repository; "
                "no GitHub token or SSH agent was forwarded",
            )
        )

    git_state = getattr(discovery, "git_transport_state", None)
    git_reason = getattr(discovery, "git_transport_reason", None)
    api_state = getattr(discovery, "github_api_state", None)
    api_reason = getattr(discovery, "github_api_reason", None)
    transport = discovery.origin_transport

    if transport == "ssh" and git_state == "ready" and not runtime.agent_forwarded:
        git_state = "unavailable"
        git_reason = f"agent_{runtime.agent_reason}"
    if transport == "https" and git_state == "ready" and not runtime.token_injected:
        git_state = "unavailable"
        git_reason = "selected_token_empty_at_launch"
    if api_state == "ready" and not runtime.token_injected:
        api_state = "unavailable"
        api_reason = "selected_token_empty_at_launch"

    if transport == "ssh":
        git_ready = "SSH agent verified repository read; push authority unproven"
    else:
        git_ready = "HTTPS credential verified repository read; mutation authority unproven"
    api_ready = (
        f"{_source_label(getattr(discovery, 'selected_token_source', None))} "
        "verified repository read; mutation scope unproven"
    )
    lines = [
        "→ GitHub capabilities refreshed for this launch",
        _capability_line(
            "Git transport", git_state, git_reason, ready_detail=git_ready
        ),
        _capability_line(
            "GitHub API", api_state, api_reason, ready_detail=api_ready
        ),
    ]
    if runtime.agent_forwarded:
        lines.append(
            "  SSH agent: forwarded — every sandbox process can use it for this "
            "container lifetime"
        )

    unavailable_reasons = {git_reason, api_reason, origin_reason}
    unverified_attempt_reasons = {
        reason
        for state, reason in getattr(discovery, "credential_attempts", ())
        if state == "unverified"
    }
    credential_probe_blocked_by_network = (
        bool(unverified_attempt_reasons)
        and unverified_attempt_reasons == {"network_unavailable"}
    )
    if (
        "network_unavailable" in unavailable_reasons
        or credential_probe_blocked_by_network
    ):
        lines.append(
            "  host remedy: restore GitHub/network access, then run ./sc launch "
            "or ./sc restart"
        )
    elif origin_reason == "origin_inspection_timed_out":
        lines.append(
            "  host remedy: repair or retry host Git origin inspection, then run "
            "./sc launch or ./sc restart"
        )
    elif git_reason == "ssh_agent_unverified":
        lines.append(
            "  host remedy (Git): restart or repair the host ssh-agent, then run "
            "./sc launch or ./sc restart"
        )
    elif origin_reason in {
        "origin_missing",
        "multiple_origin_fetch_urls",
        "multiple_origin_push_urls",
        "origin_push_config_unavailable",
        "unsupported_origin_topology",
        "divergent_origin_push",
    }:
        lines.append(
            "  host remedy: configure one matching standard github.com origin "
            "fetch/push URL, then run ./sc launch or ./sc restart"
        )
    else:
        ssh_reasons = {
            "ssh_agent_missing",
            "ssh_agent_no_identities",
            "ssh_agent_socket_invalid",
            "ssh_agent_unreachable",
            "ssh_identity_rejected",
            "repository_unreachable",
        }
        if (
            transport == "ssh"
            and git_state != "ready"
            and git_reason in ssh_reasons
        ):
            lines.append(
                "  host remedy (Git): start a live ssh-agent with a GitHub identity "
                "that can read origin, then run ./sc launch or ./sc restart"
            )
        elif git_state != "ready" and git_reason == "ssh_host_trust_rejected":
            lines.append(
                "  host remedy (Git): update the engine-pinned GitHub host keys and "
                "rebuild with ./sc launch; --no-build retains current trust"
            )
        elif git_state != "ready" and git_reason in {
            "git_unavailable",
            "ssh_add_unavailable",
            "ssh_client_unavailable",
        }:
            lines.append(
                "  host remedy (Git): install Git and OpenSSH client tools, then run "
                "./sc launch or ./sc restart"
            )
        if api_state != "ready":
            lines.append(
                "  host remedy (API): set a working scoped SC_GH_TOKEN or repair the "
                "host gh OAuth login, then run ./sc launch or ./sc restart"
            )
    if git_state != "ready" or api_state != "ready":
        lines.append(
            "  running sandbox auth remains unchanged until that launch or restart"
        )
    return "\n".join(lines)


def launch_with_discovery(
    discovery: RuntimeSelection,
    command: list[str],
    *,
    rootless: bool,
    uid: int,
    gid: int,
    environ: Mapping[str, str],
    runner: Any = subprocess.run,
    summary_writer: Callable[[str], None] | None = None,
) -> int:
    """Run the launch command with auth inserted only at its explicit marker."""
    if command.count(AUTH_ARGUMENTS_MARKER) != 1:
        raise ValueError("launch command must contain one auth argument marker")
    runtime = build_runtime_arguments(
        discovery,
        rootless=rootless,
        uid=uid,
        gid=gid,
    )
    if summary_writer is not None:
        summary_writer(render_capability_summary(discovery, runtime))
    marker = command.index(AUTH_ARGUMENTS_MARKER)
    argv = command[:marker] + list(runtime.docker_args) + command[marker + 1 :]
    completed = runner(argv, env=runtime.host_environment(environ), check=False)
    return int(completed.returncode)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply validated GitHub discovery to one sandbox launch"
    )
    parser.add_argument("--rootless", action="store_true")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        _parser().error("a launch command is required after --")
    try:
        discovery = parse_discovery(sys.stdin.read())
        return launch_with_discovery(
            discovery,
            command,
            rootless=args.rootless,
            uid=args.uid,
            gid=args.gid,
            environ=os.environ,
            summary_writer=print,
        )
    except (TypeError, ValueError) as exc:
        print(f"sandbox GitHub auth: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
