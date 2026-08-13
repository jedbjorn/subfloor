"""Translate validated GitHub discovery into secret-safe Docker launch inputs."""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

AGENT_TARGET = "/run/super-coder/ssh-agent.sock"
_CLEARED_HOST_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK")
_NUMERIC_ID = re.compile(r"\A[0-9]+\Z")


class RuntimeSelection(Protocol):
    """The secret-bearing subset of github_capabilities.DiscoveryResult."""

    @property
    def gh_token(self) -> str | None: ...

    @property
    def ssh_auth_sock(self) -> str | None: ...


class DiscoveryResult(Protocol):
    runtime: RuntimeSelection


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
    discovery: DiscoveryResult,
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

    runtime = discovery.runtime
    docker_args: list[str] = [
        "--user",
        _container_user(rootless=rootless, uid=uid, gid=gid),
    ]
    environment: list[tuple[str, str]] = []

    token = runtime.gh_token
    selected_token = token.strip() if token is not None else ""
    token_injected = bool(selected_token)
    if selected_token:
        environment.append(("GH_TOKEN", selected_token))
        docker_args.extend(("-e", "GH_TOKEN"))

    agent_forwarded = False
    agent_reason = "not_selected"
    selected_socket = runtime.ssh_auth_sock
    if selected_socket:
        candidate = Path(selected_socket)
        if not candidate.is_absolute():
            agent_reason = "socket_path_not_absolute"
        else:
            resolved = os.path.realpath(candidate)
            if "," in resolved:
                agent_reason = "socket_path_unsupported"
            elif not _live_socket(resolved):
                agent_reason = "socket_not_live"
            else:
                docker_args.extend(
                    (
                        "--mount",
                        f"type=bind,src={resolved},dst={AGENT_TARGET},readonly",
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
