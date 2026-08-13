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
from typing import Any, Protocol

AGENT_TARGET = "/run/super-coder/ssh-agent"
AUTH_ARGUMENTS_MARKER = "SC_GITHUB_AUTH_ARGS"
_CLEARED_HOST_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK")
_NUMERIC_ID = re.compile(r"\A[0-9]+\Z")


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
    return ParsedDiscovery(transport, agent, token)


def launch_with_discovery(
    discovery: RuntimeSelection,
    command: list[str],
    *,
    rootless: bool,
    uid: int,
    gid: int,
    environ: Mapping[str, str],
    runner: Any = subprocess.run,
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
        )
    except (TypeError, ValueError) as exc:
        print(f"sandbox GitHub auth: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
