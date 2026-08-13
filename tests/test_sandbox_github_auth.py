"""Focused image/runtime source contract for sandbox GitHub authentication."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / ".super-coder" / "Dockerfile"
DISPATCH = ROOT / ".super-coder" / "scripts" / "dispatch.sh"
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

from sandbox_github_auth import (
    AGENT_TARGET,
    AUTH_ARGUMENTS_MARKER,
    build_runtime_arguments,
    launch_with_discovery,
    parse_discovery,
    render_capability_summary,
)


@dataclass(frozen=True)
class FakeRuntimeSelection:
    origin_transport: str | None = None
    validated_agent_socket: str | None = None
    validated_selected_token: str | None = None


class SandboxGitHubImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = DOCKERFILE.read_text()

    def test_image_installs_ssh_without_private_key_material(self) -> None:
        install = re.search(
            r"apt-get install -y --no-install-recommends(?P<body>.*?)rm -rf",
            self.dockerfile,
            re.DOTALL,
        )
        self.assertIsNotNone(install)
        packages = install.group("body").split()
        self.assertIn("openssh-client", packages)
        self.assertNotIn("openssh-server", packages)
        self.assertNotRegex(self.dockerfile, r"(?i)(COPY|ADD).*?(id_rsa|id_ed25519|\.ssh)")

    def test_pinned_github_keys_match_published_fingerprints(self) -> None:
        expected = {
            "ssh-ed25519": "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU=",
            "ecdsa-sha2-nistp256": "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM=",
            "ssh-rsa": "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s=",
        }
        lines = re.findall(
            r"'github\.com (ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) ([A-Za-z0-9+/=]+)'",
            self.dockerfile,
        )
        self.assertEqual({kind for kind, _ in lines}, set(expected))
        actual = {
            kind: "SHA256:"
            + base64.b64encode(hashlib.sha256(base64.b64decode(key)).digest())
            .decode()
            .rstrip("=")
            + "="
            for kind, key in lines
        }
        self.assertEqual(actual, expected)

    def test_github_trust_is_strict_and_image_owned(self) -> None:
        self.assertIn("StrictHostKeyChecking yes", self.dockerfile)
        self.assertIn(
            "GlobalKnownHostsFile /etc/ssh/ssh_known_hosts", self.dockerfile
        )
        self.assertIn("UserKnownHostsFile /dev/null", self.dockerfile)
        self.assertNotRegex(
            self.dockerfile,
            r"StrictHostKeyChecking(?:=|\s+)(?:no|accept-new)",
        )

    def test_https_helper_is_prompt_free_without_transport_rewrite(self) -> None:
        self.assertIn("ENV GIT_TERMINAL_PROMPT=0", self.dockerfile)
        self.assertIn('!gh auth git-credential', self.dockerfile)
        self.assertNotIn("insteadOf", self.dockerfile)


class SandboxGitHubLaunchSourceTest(unittest.TestCase):
    def test_launch_never_mounts_private_keys_or_the_ssh_tree(self) -> None:
        dispatch = DISPATCH.read_text()
        self.assertNotRegex(dispatch, r'-v\s+"?\$HOME/\.ssh')
        self.assertNotRegex(dispatch, r"(?i)(id_rsa|id_ed25519)")
        self.assertNotIn("git remote set-url", dispatch)

    def test_every_launch_path_discovers_immediately_before_runtime_creation(self) -> None:
        dispatch = DISPATCH.read_text()
        launch = dispatch.split("  launch)", 1)[1].split("  enter)", 1)[0]

        self.assertEqual(
            launch.count(
                '"$S/github_auth.py" discover --repo-root "$CALLER_ROOT"'
            ),
            1,
        )
        self.assertLess(
            launch.index('if [ -n "$no_build" ]'),
            launch.index('"$S/github_auth.py" discover'),
        )
        self.assertIn('"$S/sandbox_github_auth.py" $github_auth_rootless', launch)
        self.assertEqual(launch.count(AUTH_ARGUMENTS_MARKER), 1)
        self.assertNotIn('GH_TOKEN="$gh_token"', launch)
        self.assertNotRegex(launch, r"-e\s+GH_TOKEN=")

    def test_restart_reuses_the_launch_refresh_without_a_stale_side_path(self) -> None:
        dispatch = DISPATCH.read_text()
        restart = dispatch.split("  restart)", 1)[1].split("  build)", 1)[0]

        self.assertEqual(restart.count('"$0" launch --no-build'), 1)
        self.assertNotIn("github_auth.py", restart)
        self.assertIn(
            "--no-build  reuse the image but still refresh host GitHub capabilities",
            restart,
        )


class SandboxGitHubRuntimeArgumentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.socket_path = Path(self.temporary.name) / "agent socket"
        self.agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.agent.close)
        self.agent.bind(str(self.socket_path))

    def discovery(
        self,
        *,
        token: str | None = None,
        agent: str | None = None,
        transport: str | None = None,
    ) -> FakeRuntimeSelection:
        if transport is None:
            transport = "ssh" if agent else "https"
        return FakeRuntimeSelection(transport, agent, token)

    def test_rootful_forwards_only_the_validated_socket_and_token_name(self) -> None:
        secret = "github_pat_test-secret"
        result = build_runtime_arguments(
            self.discovery(token=secret, agent=str(self.socket_path)),
            rootless=False,
            uid=1234,
            gid=5678,
        )

        self.assertEqual(result.container_user, "1234:5678")
        self.assertEqual(result.docker_args[:2], ("--user", "1234:5678"))
        self.assertIn(
            f"type=bind,src={self.socket_path},dst={AGENT_TARGET},readonly",
            result.docker_args,
        )
        self.assertIn(f"SSH_AUTH_SOCK={AGENT_TARGET}", result.docker_args)
        self.assertIn(("-e", "GH_TOKEN"), tuple(zip(result.docker_args, result.docker_args[1:])))
        self.assertNotIn(secret, " ".join(result.docker_args))
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, repr(result.diagnostic_dict()))
        self.assertEqual(result.host_environment({})["GH_TOKEN"], secret)

    def test_selected_token_value_is_injected_byte_for_byte(self) -> None:
        token = "  exact-selected-token  "
        result = build_runtime_arguments(
            self.discovery(token=token),
            rootless=False,
            uid=1000,
            gid=1000,
        )

        self.assertEqual(result.host_environment({})["GH_TOKEN"], token)
        self.assertNotIn(token, result.docker_args)

    def test_rootless_uses_container_root_with_the_same_narrow_mount(self) -> None:
        result = build_runtime_arguments(
            self.discovery(agent=str(self.socket_path)),
            rootless=True,
            uid=os.getuid(),
            gid=os.getgid(),
        )

        self.assertEqual(result.container_user, "0:0")
        self.assertTrue(result.agent_forwarded)
        self.assertEqual(result.agent_reason, "forwarded")
        self.assertEqual(
            [item for item in result.docker_args if ".ssh" in item],
            [],
        )

    def test_no_secret_or_agent_omits_both_and_clears_stale_host_values(self) -> None:
        result = build_runtime_arguments(
            self.discovery(token="   ", agent=None),
            rootless=False,
            uid=1000,
            gid=1000,
        )

        self.assertEqual(result.docker_args, ("--user", "1000:1000"))
        self.assertFalse(result.token_injected)
        self.assertFalse(result.agent_forwarded)
        self.assertEqual(result.agent_reason, "origin_transport_not_ssh")
        self.assertEqual(
            result.host_environment(
                {
                    "KEEP": "yes",
                    "GH_TOKEN": "stale",
                    "GITHUB_TOKEN": "stale-too",
                    "SSH_AUTH_SOCK": "/stale/agent",
                }
            ),
            {"KEEP": "yes"},
        )

    def test_vanished_socket_is_not_forwarded(self) -> None:
        vanished = Path(self.temporary.name) / "vanished.sock"
        result = build_runtime_arguments(
            self.discovery(agent=str(vanished)),
            rootless=False,
            uid=1000,
            gid=1000,
        )

        self.assertFalse(result.agent_forwarded)
        self.assertEqual(result.agent_reason, "socket_not_live")
        self.assertNotIn("--mount", result.docker_args)

    def test_https_ignores_even_a_non_null_agent_contract_violation(self) -> None:
        result = build_runtime_arguments(
            FakeRuntimeSelection("https", str(self.socket_path), None),
            rootless=False,
            uid=1000,
            gid=1000,
        )

        self.assertFalse(result.agent_forwarded)
        self.assertEqual(result.agent_reason, "origin_transport_not_ssh")
        self.assertNotIn("--mount", result.docker_args)

    def test_flat_json_contract_parses_without_nesting_or_renaming(self) -> None:
        parsed = parse_discovery(
            '{"origin_transport":"ssh","validated_agent_socket":"/agent",'
            '"validated_selected_token":"token","sanitized_reason":"ready"}'
        )

        self.assertEqual(parsed.origin_transport, "ssh")
        self.assertEqual(parsed.validated_agent_socket, "/agent")
        self.assertEqual(parsed.validated_selected_token, "token")

    def test_launch_inserts_auth_at_marker_without_secret_in_argv(self) -> None:
        secret = "github_pat_never-in-argv"
        observed: dict[str, object] = {}

        def run(argv, *, env, check):
            observed["argv"] = argv
            observed["env"] = env
            observed["check"] = check
            return subprocess.CompletedProcess(argv, 23)

        status = launch_with_discovery(
            self.discovery(token=secret, agent=str(self.socket_path)),
            ["launcher", "before", AUTH_ARGUMENTS_MARKER, "after"],
            rootless=False,
            uid=1234,
            gid=5678,
            environ={"GH_TOKEN": "stale", "GITHUB_TOKEN": "also-stale"},
            runner=run,
        )

        self.assertEqual(status, 23)
        self.assertNotIn(secret, observed["argv"])
        self.assertEqual(observed["env"].get("GH_TOKEN"), secret)
        self.assertNotIn("GITHUB_TOKEN", observed["env"])
        self.assertFalse(observed["check"])

    def test_each_lifecycle_call_replaces_stale_candidates_with_current_selection(self) -> None:
        observed: list[dict[str, str]] = []

        def run(argv, *, env, check):
            observed.append(env)
            return subprocess.CompletedProcess(argv, 0)

        command = ["launcher", AUTH_ARGUMENTS_MARKER, "after"]
        first = FakeRuntimeSelection("https", None, "first-selected-token")
        second = FakeRuntimeSelection("https", None, "fallback-oauth-token")
        base = {
            "GH_TOKEN": "stale-standard-token",
            "GITHUB_TOKEN": "stale-lower-precedence-token",
        }

        launch_with_discovery(
            first,
            command,
            rootless=False,
            uid=1234,
            gid=5678,
            environ=base,
            runner=run,
        )
        launch_with_discovery(
            second,
            command,
            rootless=False,
            uid=1234,
            gid=5678,
            environ=base,
            runner=run,
        )

        self.assertEqual(
            [environment.get("GH_TOKEN") for environment in observed],
            ["first-selected-token", "fallback-oauth-token"],
        )
        self.assertTrue(
            all("GITHUB_TOKEN" not in environment for environment in observed)
        )
        self.assertNotIn("first-selected-token", observed[1].values())


class SandboxGitHubCapabilitySummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.socket_path = Path(self.temporary.name) / "agent.sock"
        self.agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.agent.close)
        self.agent.bind(str(self.socket_path))

    def parsed(self, **overrides: object):
        value: dict[str, object] = {
            "origin_transport": "https",
            "validated_agent_socket": None,
            "validated_selected_token": "selected-token",
            "origin_state": "ready",
            "origin_reason": "supported_origin",
            "git_transport_state": "ready",
            "git_transport_reason": "repository_read_verified",
            "github_api_state": "ready",
            "github_api_reason": "repository_read_verified",
            "selected_token_source": "gh_oauth",
        }
        value.update(overrides)
        return parse_discovery(json.dumps(value))

    def summary(self, discovery) -> str:
        runtime = build_runtime_arguments(
            discovery,
            rootless=False,
            uid=1000,
            gid=1000,
        )
        return render_capability_summary(discovery, runtime)

    def test_ready_summary_names_oauth_fallback_and_unproven_mutation(self) -> None:
        summary = self.summary(self.parsed())

        self.assertIn("Git transport: ready", summary)
        self.assertIn("GitHub API: ready — host gh OAuth", summary)
        self.assertIn("mutation scope unproven", summary)
        self.assertNotIn("selected-token", summary)

    def test_partial_summary_keeps_ssh_ready_and_reports_api_host_remedy(self) -> None:
        summary = self.summary(
            self.parsed(
                origin_transport="ssh",
                validated_agent_socket=str(self.socket_path),
                validated_selected_token=None,
                selected_token_source=None,
                github_api_state="unavailable",
                github_api_reason="no_credential_candidate",
            )
        )

        self.assertIn("Git transport: ready", summary)
        self.assertIn("GitHub API: unavailable", summary)
        self.assertIn("host remedy (API)", summary)
        self.assertIn("every sandbox process can use it", summary)
        self.assertIn("running sandbox auth remains unchanged", summary)

    def test_offline_summary_never_calls_unverified_capabilities_ready(self) -> None:
        summary = self.summary(
            self.parsed(
                validated_selected_token=None,
                selected_token_source=None,
                git_transport_state="unverified",
                git_transport_reason="network_unavailable",
                github_api_state="unverified",
                github_api_reason="network_unavailable",
            )
        )

        self.assertEqual(summary.count(": unverified"), 2)
        self.assertNotIn(": ready", summary)
        self.assertIn("restore GitHub/network access", summary)

    def test_vanished_agent_downgrades_the_current_launch_summary(self) -> None:
        vanished = Path(self.temporary.name) / "vanished.sock"
        summary = self.summary(
            self.parsed(
                origin_transport="ssh",
                validated_agent_socket=str(vanished),
                validated_selected_token=None,
                selected_token_source=None,
                github_api_state="unavailable",
                github_api_reason="no_credential_candidate",
            )
        )

        self.assertIn("Git transport: unavailable", summary)
        self.assertIn("agent socket not live", summary)
        self.assertNotIn("Git transport: ready", summary)

    def test_non_github_origin_reports_skip_without_forwarding_claim(self) -> None:
        summary = self.summary(
            self.parsed(
                origin_transport=None,
                validated_selected_token=None,
                origin_state="unavailable",
                origin_reason="non_github_origin",
                git_transport_state="unavailable",
                git_transport_reason="non_github_origin",
                github_api_state="unavailable",
                github_api_reason="github_discovery_skipped",
                selected_token_source=None,
            )
        )

        self.assertIn("skipped", summary)
        self.assertIn("no GitHub token or SSH agent was forwarded", summary)
        self.assertNotIn(": ready", summary)


if __name__ == "__main__":
    unittest.main()
