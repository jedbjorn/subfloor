"""Host-side GitHub capability discovery for sandbox launches.

Git transport and GitHub API access are intentionally independent.  This
module observes only the literal ``origin`` remote, validates credential
candidates in isolated environments, and returns a secret-free diagnostic
projection plus a separately redacted runtime selection for launch code.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
PROBE_TIMEOUT_SECONDS = 15.0

READY = "ready"
UNAVAILABLE = "unavailable"
UNVERIFIED = "unverified"

SSH = "ssh"
HTTPS = "https"

_TOKEN_VARIABLES = (
    "SC_GH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_NETWORK_MARKERS = (
    "could not resolve host",
    "connection refused",
    "connection reset",
    "connection timed out",
    "failed to connect",
    "network is unreachable",
    "no route to host",
    "operation timed out",
    "temporary failure in name resolution",
    "tls handshake timeout",
)
_HOST_TRUST_MARKERS = (
    "host key verification failed",
    "remote host identification has changed",
)

Runner = Callable[..., Any]
SocketChecker = Callable[[str], bool]


@dataclass(frozen=True)
class OriginResult:
    """Secret-free classification of the effective ``origin`` topology."""

    state: str
    applies_to_github: bool
    transport: str | None
    repository: str | None
    reason: str


@dataclass(frozen=True)
class CapabilityResult:
    """One bounded capability claim."""

    state: str
    mechanism: str | None
    source: str | None
    reason: str
    repository_read: bool
    mutation_authority: str


@dataclass(frozen=True)
class CredentialAttempt:
    """A candidate outcome containing class and category, never its value."""

    source: str
    state: str
    reason: str


class RuntimeSelection:
    """Selected launch inputs whose representation never contains values."""

    __slots__ = ("_gh_token", "_ssh_auth_sock", "token_source")

    def __init__(
        self,
        *,
        gh_token: str | None = None,
        token_source: str | None = None,
        ssh_auth_sock: str | None = None,
    ) -> None:
        self._gh_token = gh_token
        self.token_source = token_source
        self._ssh_auth_sock = ssh_auth_sock

    @property
    def gh_token(self) -> str | None:
        return self._gh_token

    @property
    def ssh_auth_sock(self) -> str | None:
        return self._ssh_auth_sock

    def diagnostic_dict(self) -> dict[str, object]:
        return {
            "gh_token_selected": self._gh_token is not None,
            "token_source": self.token_source,
            "ssh_agent_selected": self._ssh_auth_sock is not None,
        }

    def __repr__(self) -> str:
        token = "<selected>" if self._gh_token is not None else "<none>"
        agent = "<selected>" if self._ssh_auth_sock is not None else "<none>"
        return (
            "RuntimeSelection(gh_token="
            f"{token}, token_source={self.token_source!r}, ssh_auth_sock={agent})"
        )


@dataclass(frozen=True)
class DiscoveryResult:
    """Stable result schema consumed by later sandbox-launch integration."""

    observed_at: str
    origin: OriginResult
    git_transport: CapabilityResult
    github_api: CapabilityResult
    credential_attempts: tuple[CredentialAttempt, ...]
    runtime: RuntimeSelection = field(repr=False, compare=False)
    schema_version: int = SCHEMA_VERSION

    def diagnostic_dict(self) -> dict[str, object]:
        """Return the complete persistable/loggable, secret-free projection."""
        return {
            "schema_version": self.schema_version,
            "observed_at": self.observed_at,
            "origin": asdict(self.origin),
            "capabilities": {
                "git_transport": asdict(self.git_transport),
                "github_api": asdict(self.github_api),
            },
            "credential_attempts": [
                asdict(attempt) for attempt in self.credential_attempts
            ],
            "runtime": self.runtime.diagnostic_dict(),
        }

    def output_dict(self) -> dict[str, object]:
        """Return the confidential, ephemeral machine schema for launch.

        ``validated_selected_token`` is a live secret.  Consumers must capture
        stdout without echoing it, use the value only to construct the current
        replacement process environment, and never persist this object or the
        token in a state file, receipt, diagnostic, argv, or log.
        """
        return {
            "schema_version": self.schema_version,
            "observed_at": self.observed_at,
            "origin_transport": self.origin.transport,
            "validated_agent_socket": self.runtime.ssh_auth_sock,
            "validated_selected_token": self.runtime.gh_token,
            "origin_repository": self.origin.repository,
            "origin_state": self.origin.state,
            "origin_reason": self.origin.reason,
            "git_transport_state": self.git_transport.state,
            "git_transport_reason": self.git_transport.reason,
            "github_api_state": self.github_api.state,
            "github_api_reason": self.github_api.reason,
            "selected_token_source": self.runtime.token_source,
            "credential_attempts": [
                asdict(attempt) for attempt in self.credential_attempts
            ],
        }


@dataclass(frozen=True)
class _CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    failure: str | None = None


@dataclass(frozen=True)
class _ParsedRemote:
    github: bool
    transport: str | None
    repository: str | None
    reason: str


def _timestamp(now: datetime | None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean_environment(environ: Mapping[str, str]) -> dict[str, str]:
    clean = {
        name: value
        for name, value in environ.items()
        if not name.startswith("GIT_CONFIG_")
    }
    for name in _TOKEN_VARIABLES:
        clean.pop(name, None)
    clean.pop("GH_HOST", None)
    clean.pop("GITHUB_HOST", None)
    clean["GH_PROMPT_DISABLED"] = "1"
    clean["GIT_TERMINAL_PROMPT"] = "0"
    clean["GIT_CONFIG_GLOBAL"] = os.devnull
    clean["GIT_CONFIG_SYSTEM"] = os.devnull
    return clean


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    runner: Runner,
) -> _CommandResult:
    try:
        completed = runner(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        return _CommandResult(None, "", "", "timeout")
    except OSError:
        return _CommandResult(None, "", "", "tool_unavailable")
    return _CommandResult(
        int(completed.returncode),
        str(completed.stdout or ""),
        str(completed.stderr or ""),
    )


def _failure_text(result: _CommandResult) -> str:
    return f"{result.stderr}\n{result.stdout}".lower()


def _is_network_failure(result: _CommandResult) -> bool:
    if result.failure == "timeout":
        return True
    text = _failure_text(result)
    return any(marker in text for marker in _NETWORK_MARKERS)


def _is_host_trust_failure(result: _CommandResult) -> bool:
    text = _failure_text(result)
    return any(marker in text for marker in _HOST_TRUST_MARKERS)


def _repository_path(path: str) -> str | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        return None
    owner, name = parts
    name = name.removesuffix(".git")
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def _parse_remote(url: str) -> _ParsedRemote:
    value = url.strip()
    if value.startswith("git@github.com:"):
        repository = _repository_path(value.removeprefix("git@github.com:"))
        return _ParsedRemote(
            github=True,
            transport=SSH if repository else None,
            repository=repository,
            reason="supported_origin" if repository else "unsupported_github_url",
        )
    if "://" not in value and ":" in value:
        authority, _separator, _path = value.partition(":")
        _user, at, host = authority.partition("@")
        if at and host.casefold() == "github.com":
            return _ParsedRemote(True, None, None, "unsupported_github_url")

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return _ParsedRemote(True, None, None, "unsupported_github_url")
    if host != "github.com":
        return _ParsedRemote(False, None, None, "non_github_origin")
    try:
        port = parsed.port
    except ValueError:
        return _ParsedRemote(True, None, None, "unsupported_github_url")
    if parsed.query or parsed.fragment or port is not None:
        return _ParsedRemote(True, None, None, "unsupported_github_url")
    repository = _repository_path(parsed.path)
    if repository is None:
        return _ParsedRemote(True, None, None, "unsupported_github_url")
    if parsed.scheme == HTTPS and parsed.username is None and parsed.password is None:
        return _ParsedRemote(True, HTTPS, repository, "supported_origin")
    if parsed.scheme == SSH and parsed.username == "git" and parsed.password is None:
        return _ParsedRemote(True, SSH, repository, "supported_origin")
    return _ParsedRemote(True, None, None, "unsupported_github_url")


def _configured_urls(
    repo: Path,
    *,
    key: str,
    environ: Mapping[str, str],
    runner: Runner,
) -> _CommandResult:
    command = ["git", "-C", str(repo), "config", "--get-all", key]
    return _run(command, cwd=repo, env=_clean_environment(environ), runner=runner)


def _inspect_origin(
    root: Path,
    *,
    environ: Mapping[str, str],
    runner: Runner,
) -> tuple[OriginResult, str | None]:
    fetch = _configured_urls(
        root,
        key="remote.origin.url",
        environ=environ,
        runner=runner,
    )
    if fetch.failure == "timeout":
        return (
            OriginResult(
                UNVERIFIED, False, None, None, "origin_inspection_timed_out"
            ),
            None,
        )
    if fetch.failure == "tool_unavailable":
        return OriginResult(UNAVAILABLE, False, None, None, "git_unavailable"), None
    if fetch.returncode != 0:
        return OriginResult(UNAVAILABLE, False, None, None, "origin_missing"), None
    fetch_urls = [line.strip() for line in fetch.stdout.splitlines() if line.strip()]
    if len(fetch_urls) != 1:
        return (
            OriginResult(
                UNAVAILABLE, False, None, None, "multiple_origin_fetch_urls"
            ),
            None,
        )

    push = _configured_urls(
        root,
        key="remote.origin.pushurl",
        environ=environ,
        runner=runner,
    )
    if push.failure == "timeout":
        return (
            OriginResult(
                UNVERIFIED, False, None, None, "origin_inspection_timed_out"
            ),
            None,
        )
    if push.failure == "tool_unavailable":
        return OriginResult(UNAVAILABLE, False, None, None, "git_unavailable"), None
    if push.returncode == 1 and not push.stdout.strip():
        push_urls = fetch_urls
    elif push.returncode != 0:
        return (
            OriginResult(
                UNAVAILABLE, False, None, None, "origin_push_config_unavailable"
            ),
            None,
        )
    else:
        push_urls = [line.strip() for line in push.stdout.splitlines() if line.strip()]
    if len(push_urls) != 1:
        return (
            OriginResult(
                UNAVAILABLE, False, None, None, "multiple_origin_push_urls"
            ),
            None,
        )

    fetched = _parse_remote(fetch_urls[0])
    pushed = _parse_remote(push_urls[0])
    if not fetched.github and not pushed.github:
        return (
            OriginResult(UNAVAILABLE, False, None, None, "non_github_origin"),
            None,
        )
    if (
        fetched.transport is None
        or pushed.transport is None
        or fetched.repository is None
        or pushed.repository is None
    ):
        return (
            OriginResult(
                UNAVAILABLE, False, None, None, "unsupported_origin_topology"
            ),
            None,
        )
    if (
        fetched.transport != pushed.transport
        or fetched.repository.casefold() != pushed.repository.casefold()
    ):
        return (
            OriginResult(UNAVAILABLE, False, None, None, "divergent_origin_push"),
            None,
        )
    return (
        OriginResult(
            READY,
            True,
            fetched.transport,
            fetched.repository,
            "supported_origin",
        ),
        fetch_urls[0],
    )


def inspect_origin(
    repo: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> OriginResult:
    """Classify only the literal origin's configured fetch/push topology."""
    root = Path(repo).resolve()
    env = os.environ if environ is None else environ
    result, _fetch_url = _inspect_origin(root, environ=env, runner=runner)
    return result


def _capability(
    state: str,
    *,
    mechanism: str | None = None,
    source: str | None = None,
    reason: str,
) -> CapabilityResult:
    ready = state == READY
    return CapabilityResult(
        state=state,
        mechanism=mechanism,
        source=source,
        reason=reason,
        repository_read=ready,
        mutation_authority="unverified" if ready else "not_claimed",
    )


def _probe_token(
    root: Path,
    repository: str,
    source: str,
    token: str,
    *,
    environ: Mapping[str, str],
    runner: Runner,
) -> CredentialAttempt:
    probe_env = _clean_environment(environ)
    probe_env["GH_TOKEN"] = token
    identity = _run(
        ("gh", "api", "user", "--jq", ".login"),
        cwd=root,
        env=probe_env,
        runner=runner,
    )
    if identity.returncode != 0 or not identity.stdout.strip():
        if _is_network_failure(identity):
            return CredentialAttempt(source, UNVERIFIED, "network_unavailable")
        if identity.failure == "tool_unavailable":
            return CredentialAttempt(source, UNAVAILABLE, "gh_cli_unavailable")
        return CredentialAttempt(source, UNAVAILABLE, "credential_rejected")

    reach = _run(
        ("gh", "api", f"repos/{repository}", "--jq", ".full_name"),
        cwd=root,
        env=probe_env,
        runner=runner,
    )
    if reach.returncode != 0:
        if _is_network_failure(reach):
            return CredentialAttempt(source, UNVERIFIED, "network_unavailable")
        return CredentialAttempt(source, UNAVAILABLE, "repository_unreachable")
    if reach.stdout.strip().casefold() != repository.casefold():
        return CredentialAttempt(source, UNAVAILABLE, "repository_identity_mismatch")
    return CredentialAttempt(source, READY, "repository_read_verified")


def _stored_oauth(
    root: Path,
    *,
    environ: Mapping[str, str],
    runner: Runner,
) -> tuple[str | None, CredentialAttempt | None]:
    result = _run(
        ("gh", "auth", "token", "--hostname", "github.com"),
        cwd=root,
        env=_clean_environment(environ),
        runner=runner,
    )
    token = result.stdout.strip() if result.returncode == 0 else ""
    if token:
        return token, None
    reason = "gh_cli_unavailable" if result.failure == "tool_unavailable" else "stored_oauth_unavailable"
    return None, CredentialAttempt("gh_oauth", UNAVAILABLE, reason)


def _select_api_token(
    root: Path,
    repository: str,
    *,
    environ: Mapping[str, str],
    runner: Runner,
) -> tuple[CapabilityResult, tuple[CredentialAttempt, ...], str | None, str | None]:
    candidates = [
        ("sc_gh_token", environ.get("SC_GH_TOKEN", "")),
        ("gh_token", environ.get("GH_TOKEN", "")),
        ("github_token", environ.get("GITHUB_TOKEN", "")),
    ]
    attempts: list[CredentialAttempt] = []
    probed_candidate = False
    for source, token in candidates:
        if not token.strip():
            continue
        probed_candidate = True
        attempt = _probe_token(
            root,
            repository,
            source,
            token,
            environ=environ,
            runner=runner,
        )
        attempts.append(attempt)
        if attempt.state == READY:
            return (
                _capability(
                    READY,
                    mechanism="token",
                    source=source,
                    reason="repository_read_verified",
                ),
                tuple(attempts),
                token,
                source,
            )

    oauth_token, oauth_failure = _stored_oauth(root, environ=environ, runner=runner)
    if oauth_failure is not None:
        attempts.append(oauth_failure)
    elif oauth_token is not None:
        probed_candidate = True
        oauth_attempt = _probe_token(
            root,
            repository,
            "gh_oauth",
            oauth_token,
            environ=environ,
            runner=runner,
        )
        attempts.append(oauth_attempt)
        if oauth_attempt.state == READY:
            return (
                _capability(
                    READY,
                    mechanism="token",
                    source="gh_oauth",
                    reason="repository_read_verified",
                ),
                tuple(attempts),
                oauth_token,
                "gh_oauth",
            )

    state = UNVERIFIED if any(item.state == UNVERIFIED for item in attempts) else UNAVAILABLE
    reason = (
        "credential_validation_unverified"
        if state == UNVERIFIED
        else (
            "no_candidate_reached_repository"
            if probed_candidate
            else "no_credential_candidate"
        )
    )
    return _capability(state, reason=reason), tuple(attempts), None, None


def _probe_https_transport(
    root: Path,
    remote_url: str,
    api: CapabilityResult,
    token: str | None,
    source: str | None,
    *,
    environ: Mapping[str, str],
    runner: Runner,
) -> CapabilityResult:
    if token is None:
        state = UNVERIFIED if api.state == UNVERIFIED else UNAVAILABLE
        return _capability(state, reason="api_credential_not_ready")
    env = _clean_environment(environ)
    env["GH_TOKEN"] = token
    result = _run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "credential.helper=",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            "ls-remote",
            "--symref",
            remote_url,
            "HEAD",
        ),
        cwd=root,
        env=env,
        runner=runner,
    )
    if result.returncode == 0:
        return _capability(
            READY,
            mechanism="https_credential_helper",
            source=source,
            reason="repository_read_verified",
        )
    if _is_network_failure(result):
        return _capability(UNVERIFIED, reason="network_unavailable")
    if result.failure == "tool_unavailable":
        return _capability(UNAVAILABLE, reason="git_unavailable")
    return _capability(UNAVAILABLE, reason="repository_unreachable")


def _is_unix_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _probe_ssh_transport(
    root: Path,
    remote_url: str,
    *,
    environ: Mapping[str, str],
    runner: Runner,
    socket_checker: SocketChecker,
) -> tuple[CapabilityResult, str | None]:
    agent_socket = environ.get("SSH_AUTH_SOCK", "")
    if not agent_socket.strip():
        return _capability(UNAVAILABLE, reason="ssh_agent_missing"), None
    if not Path(agent_socket).is_absolute():
        return _capability(UNAVAILABLE, reason="ssh_agent_socket_invalid"), None
    if not socket_checker(agent_socket):
        return _capability(UNAVAILABLE, reason="ssh_agent_socket_invalid"), None

    env = _clean_environment(environ)
    env["SSH_AUTH_SOCK"] = agent_socket
    identities = _run(("ssh-add", "-L"), cwd=root, env=env, runner=runner)
    if identities.failure == "timeout":
        return _capability(UNVERIFIED, reason="ssh_agent_unverified"), None
    if identities.failure == "tool_unavailable":
        return _capability(UNAVAILABLE, reason="ssh_add_unavailable"), None
    if identities.returncode != 0:
        if "agent has no identities" in _failure_text(identities):
            return _capability(UNAVAILABLE, reason="ssh_agent_no_identities"), None
        return _capability(UNAVAILABLE, reason="ssh_agent_unreachable"), None
    if not identities.stdout.strip():
        return _capability(UNAVAILABLE, reason="ssh_agent_no_identities"), None

    identity = _run(
        (
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "git@github.com",
        ),
        cwd=root,
        env=env,
        runner=runner,
    )
    identity_text = _failure_text(identity)
    authenticated = identity.returncode == 0 or (
        identity.returncode == 1
        and "successfully authenticated" in identity_text
        and "does not provide shell access" in identity_text
    )
    if not authenticated:
        if _is_network_failure(identity):
            return _capability(UNVERIFIED, reason="network_unavailable"), None
        if _is_host_trust_failure(identity):
            return _capability(UNAVAILABLE, reason="ssh_host_trust_rejected"), None
        if identity.failure == "tool_unavailable":
            return _capability(UNAVAILABLE, reason="ssh_client_unavailable"), None
        return _capability(UNAVAILABLE, reason="ssh_identity_rejected"), None

    git_env = dict(env)
    git_env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    reach = _run(
        ("git", "-C", str(root), "ls-remote", "--symref", remote_url, "HEAD"),
        cwd=root,
        env=git_env,
        runner=runner,
    )
    if reach.returncode == 0:
        return (
            _capability(
                READY,
                mechanism="ssh_agent",
                source="ssh_auth_sock",
                reason="repository_read_verified",
            ),
            agent_socket,
        )
    if _is_network_failure(reach):
        return _capability(UNVERIFIED, reason="network_unavailable"), None
    if _is_host_trust_failure(reach):
        return _capability(UNAVAILABLE, reason="ssh_host_trust_rejected"), None
    if reach.failure == "tool_unavailable":
        return _capability(UNAVAILABLE, reason="git_unavailable"), None
    return _capability(UNAVAILABLE, reason="repository_unreachable"), None


def discover_github_capabilities(
    repo: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    socket_checker: SocketChecker = _is_unix_socket,
    now: datetime | None = None,
) -> DiscoveryResult:
    """Resolve current host capabilities without mutating Git or auth state."""
    root = Path(repo).resolve()
    env = dict(os.environ if environ is None else environ)
    origin, remote_url = _inspect_origin(root, environ=env, runner=runner)
    if origin.state != READY:
        git_result = _capability(origin.state, reason=origin.reason)
        api_state = UNVERIFIED if origin.state == UNVERIFIED else UNAVAILABLE
        api_result = _capability(api_state, reason="github_discovery_skipped")
        return DiscoveryResult(
            observed_at=_timestamp(now),
            origin=origin,
            git_transport=git_result,
            github_api=api_result,
            credential_attempts=(),
            runtime=RuntimeSelection(),
        )

    assert origin.repository is not None
    assert remote_url is not None
    api_result, attempts, token, token_source = _select_api_token(
        root,
        origin.repository,
        environ=env,
        runner=runner,
    )
    ssh_socket = None
    if origin.transport == HTTPS:
        git_result = _probe_https_transport(
            root,
            remote_url,
            api_result,
            token,
            token_source,
            environ=env,
            runner=runner,
        )
    else:
        git_result, ssh_socket = _probe_ssh_transport(
            root,
            remote_url,
            environ=env,
            runner=runner,
            socket_checker=socket_checker,
        )

    return DiscoveryResult(
        observed_at=_timestamp(now),
        origin=origin,
        git_transport=git_result,
        github_api=api_result,
        credential_attempts=attempts,
        runtime=RuntimeSelection(
            gh_token=token,
            token_source=token_source,
            ssh_auth_sock=ssh_socket,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github_auth.py",
        description="Discover host GitHub capabilities for one repository origin.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--repo-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "discover":
        return 2
    try:
        result = discover_github_capabilities(args.repo_root)
    except Exception:  # noqa: BLE001 - CLI boundary emits no exception secrets
        print("github auth discovery: internal fault", file=sys.stderr)
        return 1
    json.dump(result.output_dict(), sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
