"""Focused host-side GitHub capability-discovery coverage."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import github_capabilities as github


class FakeRunner:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], list[Any]] = {}
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def add(
        self,
        command: tuple[str, ...],
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.responses.setdefault(command, []).append(
            subprocess.CompletedProcess(command, returncode, stdout, stderr)
        )

    def add_exception(self, command: tuple[str, ...], error: Exception) -> None:
        self.responses.setdefault(command, []).append(error)

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        key = tuple(command)
        self.calls.append((key, kwargs))
        queued = self.responses.get(key)
        if not queued:
            raise AssertionError(f"unexpected command: {key!r}")
        result = queued.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def assert_consumed(self, testcase: unittest.TestCase) -> None:
        leftovers = {key: len(value) for key, value in self.responses.items() if value}
        testcase.assertEqual({}, leftovers)


class GitHubCapabilityDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        self.runner = FakeRunner()
        self.now = datetime(2026, 8, 13, 18, 30, tzinfo=timezone.utc)

    @property
    def fetch_command(self) -> tuple[str, ...]:
        return (
            "git",
            "-C",
            str(self.repo),
            "remote",
            "get-url",
            "--all",
            "origin",
        )

    @property
    def push_command(self) -> tuple[str, ...]:
        return (
            "git",
            "-C",
            str(self.repo),
            "remote",
            "get-url",
            "--push",
            "--all",
            "origin",
        )

    @property
    def https_probe_command(self) -> tuple[str, ...]:
        return (
            "git",
            "-C",
            str(self.repo),
            "-c",
            "credential.helper=",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            "ls-remote",
            "--symref",
            "origin",
            "HEAD",
        )

    @property
    def ssh_probe_command(self) -> tuple[str, ...]:
        return (
            "git",
            "-C",
            str(self.repo),
            "ls-remote",
            "--symref",
            "origin",
            "HEAD",
        )

    def add_origin(self, fetch: str, push: str | None = None) -> None:
        self.runner.add(self.fetch_command, stdout=f"{fetch}\n")
        self.runner.add(self.push_command, stdout=f"{push or fetch}\n")

    def add_token_success(self) -> None:
        self.runner.add(("gh", "api", "user", "--jq", ".login"), stdout="octocat\n")
        self.runner.add(
            ("gh", "api", "repos/Owner/Repo", "--jq", ".full_name"),
            stdout="owner/repo\n",
        )

    def discover(
        self,
        environ: dict[str, str],
        *,
        socket_ok: bool = False,
    ) -> github.DiscoveryResult:
        return github.discover_github_capabilities(
            self.repo,
            environ={"PATH": "/usr/bin", "HOME": "/host/home", **environ},
            runner=self.runner,
            socket_checker=lambda value: socket_ok and value == "/run/agent.sock",
            now=self.now,
        )

    def test_ssh_transport_and_api_are_independent(self) -> None:
        self.add_origin("git@github.com:Owner/Repo.git")
        self.runner.add(
            ("gh", "auth", "token", "--hostname", "github.com"),
            returncode=1,
            stderr="not logged in",
        )
        self.runner.add(("ssh-add", "-L"), stdout="ssh-ed25519 AAAA test\n")
        self.runner.add(
            (
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "git@github.com",
            ),
            returncode=1,
            stderr="Hi operator! You've successfully authenticated, but GitHub does not provide shell access.\n",
        )
        self.runner.add(self.ssh_probe_command, stdout="ref: refs/heads/main\tHEAD\n")

        result = self.discover({"SSH_AUTH_SOCK": "/run/agent.sock"}, socket_ok=True)

        self.assertEqual("ready", result.git_transport.state)
        self.assertEqual("ssh_agent", result.git_transport.mechanism)
        self.assertTrue(result.git_transport.repository_read)
        self.assertEqual("unverified", result.git_transport.mutation_authority)
        self.assertEqual("unavailable", result.github_api.state)
        self.assertFalse(result.github_api.repository_read)
        self.assertEqual("/run/agent.sock", result.runtime.ssh_auth_sock)
        self.assertIsNone(result.runtime.gh_token)
        self.runner.assert_consumed(self)

    def test_api_can_be_ready_while_ssh_transport_is_unavailable(self) -> None:
        self.add_origin("ssh://git@github.com/Owner/Repo.git")
        self.add_token_success()

        result = self.discover({"SC_GH_TOKEN": "api-secret"})

        self.assertEqual("unavailable", result.git_transport.state)
        self.assertEqual("ssh_agent_missing", result.git_transport.reason)
        self.assertEqual("ready", result.github_api.state)
        self.assertEqual("sc_gh_token", result.github_api.source)
        self.assertEqual("api-secret", result.runtime.gh_token)
        self.assertIsNone(result.runtime.ssh_auth_sock)
        self.runner.assert_consumed(self)

    def test_origin_topology_rejects_multiple_fetch_urls_without_auth_probes(self) -> None:
        self.runner.add(
            self.fetch_command,
            stdout="https://github.com/Owner/Repo.git\nhttps://github.com/Other/Repo.git\n",
        )

        result = self.discover({"SC_GH_TOKEN": "must-not-be-used"})

        self.assertEqual("multiple_origin_fetch_urls", result.origin.reason)
        self.assertEqual("unavailable", result.git_transport.state)
        self.assertEqual("unavailable", result.github_api.state)
        self.assertEqual((), result.credential_attempts)
        self.assertEqual([self.fetch_command], [command for command, _ in self.runner.calls])
        self.runner.assert_consumed(self)

    def test_origin_topology_rejects_divergent_push_repository(self) -> None:
        self.add_origin(
            "https://github.com/Owner/Repo.git",
            "https://github.com/Owner/Elsewhere.git",
        )

        result = self.discover({"GH_TOKEN": "must-not-be-used"})

        self.assertEqual("divergent_origin_push", result.origin.reason)
        self.assertFalse(result.origin.applies_to_github)
        self.assertFalse(result.runtime.diagnostic_dict()["gh_token_selected"])
        self.runner.assert_consumed(self)

    def test_non_github_origin_skips_all_github_discovery(self) -> None:
        self.add_origin("git@gitlab.com:Owner/Repo.git")

        result = self.discover(
            {"SC_GH_TOKEN": "must-not-be-used", "SSH_AUTH_SOCK": "/run/agent.sock"},
            socket_ok=True,
        )

        self.assertEqual("non_github_origin", result.origin.reason)
        self.assertFalse(result.origin.applies_to_github)
        self.assertIsNone(result.runtime.gh_token)
        self.assertIsNone(result.runtime.ssh_auth_sock)
        self.assertEqual(
            [self.fetch_command, self.push_command],
            [command for command, _ in self.runner.calls],
        )
        self.runner.assert_consumed(self)

    def test_equivalent_standard_ssh_urls_are_one_topology(self) -> None:
        self.add_origin(
            "git@github.com:Owner/Repo.git",
            "ssh://git@github.com/owner/repo.git",
        )

        result = github.inspect_origin(
            self.repo,
            environ={"PATH": "/usr/bin"},
            runner=self.runner,
        )

        self.assertEqual("ready", result.state)
        self.assertTrue(result.applies_to_github)
        self.assertEqual("ssh", result.transport)
        self.assertEqual("Owner/Repo", result.repository)
        self.runner.assert_consumed(self)

    def test_invalid_explicit_token_falls_through_to_isolated_standard_token(self) -> None:
        self.add_origin("https://github.com/Owner/Repo.git")
        self.runner.add(
            ("gh", "api", "user", "--jq", ".login"),
            returncode=1,
            stderr="HTTP 401: Bad credentials",
        )
        self.add_token_success()
        self.runner.add(self.https_probe_command, stdout="ref: refs/heads/main\tHEAD\n")

        result = self.discover(
            {
                "SC_GH_TOKEN": "stale-explicit",
                "GH_TOKEN": "working-standard",
                "GITHUB_TOKEN": "lower-precedence",
            }
        )

        self.assertEqual("gh_token", result.github_api.source)
        self.assertEqual("working-standard", result.runtime.gh_token)
        self.assertEqual(
            [
                github.CredentialAttempt("sc_gh_token", "unavailable", "credential_rejected"),
                github.CredentialAttempt("gh_token", "ready", "repository_read_verified"),
            ],
            list(result.credential_attempts),
        )
        api_calls = [
            kwargs["env"]
            for command, kwargs in self.runner.calls
            if command[:3] == ("gh", "api", "user")
        ]
        self.assertEqual("stale-explicit", api_calls[0]["GH_TOKEN"])
        self.assertEqual("working-standard", api_calls[1]["GH_TOKEN"])
        for environment in api_calls:
            self.assertNotIn("SC_GH_TOKEN", environment)
            self.assertNotIn("GITHUB_TOKEN", environment)
        self.runner.assert_consumed(self)

    def test_repository_denial_falls_through_to_stored_oauth(self) -> None:
        self.add_origin("https://github.com/Owner/Repo.git")
        self.runner.add(("gh", "api", "user", "--jq", ".login"), stdout="first-user\n")
        self.runner.add(
            ("gh", "api", "repos/Owner/Repo", "--jq", ".full_name"),
            returncode=1,
            stderr="HTTP 404: Not Found",
        )
        self.runner.add(
            ("gh", "auth", "token", "--hostname", "github.com"),
            stdout="stored-oauth-secret\n",
        )
        self.add_token_success()
        self.runner.add(self.https_probe_command)

        result = self.discover({"SC_GH_TOKEN": "repo-denied-secret"})

        self.assertEqual("gh_oauth", result.github_api.source)
        self.assertEqual("stored-oauth-secret", result.runtime.gh_token)
        self.assertEqual("repository_unreachable", result.credential_attempts[0].reason)
        oauth_command, oauth_kwargs = next(
            (command, kwargs)
            for command, kwargs in self.runner.calls
            if command[:3] == ("gh", "auth", "token")
        )
        self.assertEqual(("gh", "auth", "token", "--hostname", "github.com"), oauth_command)
        for name in ("SC_GH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(name, oauth_kwargs["env"])
        self.runner.assert_consumed(self)

    def test_network_failure_is_unverified_not_unavailable(self) -> None:
        self.add_origin("https://github.com/Owner/Repo.git")
        self.runner.add(
            ("gh", "api", "user", "--jq", ".login"),
            returncode=1,
            stderr="dial tcp: network is unreachable; token=offline-secret",
        )
        self.runner.add(
            ("gh", "auth", "token", "--hostname", "github.com"),
            returncode=1,
            stderr="not logged in",
        )

        result = self.discover({"SC_GH_TOKEN": "offline-secret"})

        self.assertEqual("unverified", result.github_api.state)
        self.assertEqual("credential_validation_unverified", result.github_api.reason)
        self.assertEqual("unverified", result.git_transport.state)
        self.assertEqual("api_credential_not_ready", result.git_transport.reason)
        self.runner.assert_consumed(self)

    def test_rejected_credentials_are_unavailable_not_unverified(self) -> None:
        self.add_origin("https://github.com/Owner/Repo.git")
        self.runner.add(
            ("gh", "api", "user", "--jq", ".login"),
            returncode=1,
            stderr="HTTP 401: Bad credentials",
        )
        self.runner.add(
            ("gh", "auth", "token", "--hostname", "github.com"),
            returncode=1,
            stderr="not logged in",
        )

        result = self.discover({"SC_GH_TOKEN": "rejected-secret"})

        self.assertEqual("unavailable", result.github_api.state)
        self.assertEqual("unavailable", result.git_transport.state)
        self.assertFalse(result.github_api.repository_read)
        self.assertEqual("not_claimed", result.github_api.mutation_authority)
        self.runner.assert_consumed(self)

    def test_transport_probe_can_be_unverified_after_api_read_is_ready(self) -> None:
        self.add_origin("https://github.com/Owner/Repo.git")
        self.add_token_success()
        self.runner.add(
            self.https_probe_command,
            returncode=1,
            stderr="fatal: unable to access origin: Could not resolve host: github.com",
        )

        result = self.discover({"SC_GH_TOKEN": "valid-secret"})

        self.assertEqual("ready", result.github_api.state)
        self.assertEqual("unverified", result.git_transport.state)
        self.assertEqual("network_unavailable", result.git_transport.reason)
        self.assertEqual("valid-secret", result.runtime.gh_token)
        self.runner.assert_consumed(self)

    def test_diagnostic_schema_and_representations_are_secret_free(self) -> None:
        self.add_origin("git@github.com:Owner/Repo.git")
        self.add_token_success()
        self.runner.add(("ssh-add", "-L"), stdout="ssh-ed25519 VERY-SECRET-PUBLIC-MATERIAL\n")
        self.runner.add(
            (
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "git@github.com",
            ),
            returncode=1,
            stderr="Hi secret-user! You've successfully authenticated, but GitHub does not provide shell access.\n",
        )
        self.runner.add(self.ssh_probe_command)

        result = self.discover(
            {"SC_GH_TOKEN": "top-secret-token", "SSH_AUTH_SOCK": "/run/agent.sock"},
            socket_ok=True,
        )
        diagnostic = result.diagnostic_dict()
        serialized = json.dumps(diagnostic, sort_keys=True)
        representations = f"{result!r}\n{result.runtime!r}"

        self.assertEqual(
            {
                "schema_version": 1,
                "observed_at": "2026-08-13T18:30:00+00:00",
                "origin": {
                    "state": "ready",
                    "applies_to_github": True,
                    "transport": "ssh",
                    "repository": "Owner/Repo",
                    "reason": "supported_origin",
                },
                "capabilities": {
                    "git_transport": {
                        "state": "ready",
                        "mechanism": "ssh_agent",
                        "source": "ssh_auth_sock",
                        "reason": "repository_read_verified",
                        "repository_read": True,
                        "mutation_authority": "unverified",
                    },
                    "github_api": {
                        "state": "ready",
                        "mechanism": "token",
                        "source": "sc_gh_token",
                        "reason": "repository_read_verified",
                        "repository_read": True,
                        "mutation_authority": "unverified",
                    },
                },
                "credential_attempts": [
                    {
                        "source": "sc_gh_token",
                        "state": "ready",
                        "reason": "repository_read_verified",
                    }
                ],
                "runtime": {
                    "gh_token_selected": True,
                    "token_source": "sc_gh_token",
                    "ssh_agent_selected": True,
                },
            },
            diagnostic,
        )
        for secret in (
            "top-secret-token",
            "/run/agent.sock",
            "VERY-SECRET-PUBLIC-MATERIAL",
            "secret-user",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, representations)
        self.assertEqual("top-secret-token", result.runtime.gh_token)
        self.assertEqual("2026-08-13T18:30:00+00:00", result.observed_at)
        self.runner.assert_consumed(self)


if __name__ == "__main__":
    unittest.main()
