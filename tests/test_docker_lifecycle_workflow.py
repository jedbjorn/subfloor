"""Contract coverage for the disposable default-sandbox CI gate."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def job_block(job: str) -> str:
    text = WORKFLOW.read_text()
    match = re.search(
        rf"(?ms)^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        text,
    )
    if match is None:
        raise AssertionError(f"workflow job {job!r} is missing")
    return match.group(0)


class DockerLifecycleWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = job_block("docker-sandbox-lifecycle")

    def test_job_has_stable_disposable_runtime_contract(self) -> None:
        self.assertIn("name: Docker sandbox lifecycle", self.job)
        self.assertIn("runs-on: ubuntu-24.04", self.job)
        self.assertRegex(self.job, r"(?m)^    timeout-minutes: [1-9][0-9]*$")
        self.assertIn("python-version: '3.14'", self.job)
        self.assertIn("persist-credentials: false", self.job)
        self.assertIn("SC_CI_CONTAINER: sc-${{ github.event.repository.name }}", self.job)
        self.assertIn("SC_CI_SIDECAR: sc-pg-${{ github.event.repository.name }}", self.job)

    def test_job_uses_supported_bootstrap_and_real_launch(self) -> None:
        bootstrap = self.job.index("run: ./sc verify")
        launch = self.job.index("run: ./sc launch")

        self.assertLess(bootstrap, launch)
        self.assertNotIn("./sc launch --no-build", self.job)
        self.assertIn('["./sc", "health"]', self.job)
        self.assertIn("time.monotonic() + 90", self.job)
        self.assertIn('response.status == 200 and direct_payload.get("ok") is True', self.job)

    def test_runtime_proof_targets_the_live_api_server(self) -> None:
        self.assertIn('b".super-coder/api/server.py"', self.job)
        self.assertIn('os.readlink(servers[0] / "exe")', self.job)
        self.assertIn("server_executable != selected_executable", self.job)
        self.assertIn("sys.version_info[:2] != (3, 14)", self.job)

    def test_failure_evidence_and_unconditional_cleanup_are_required(self) -> None:
        self.assertRegex(
            self.job,
            r"(?s)- name: Capture Docker failure evidence\n        if: failure\(\).*?"
            r"docker ps -a --no-trunc.*?docker logs.*?docker image inspect.*?"
            r"docker image ls --all --no-trunc",
        )
        self.assertRegex(
            self.job,
            r"(?s)- name: Stop the managed sandbox\n        if: always\(\)\n"
            r"        run: \./sc down",
        )
        self.assertRegex(
            self.job,
            r"(?s)- name: Assert no managed container residue\n        if: always\(\).*?"
            r'for container in "\$SC_CI_CONTAINER" "\$SC_CI_SIDECAR".*?'
            r'test "\$residue" -eq 0',
        )

    def test_job_has_no_secret_or_browser_execution_surface(self) -> None:
        lowered = self.job.lower()
        self.assertNotIn("secrets.", lowered)
        self.assertNotIn("playwright", lowered)
        self.assertNotIn("chromium", lowered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
