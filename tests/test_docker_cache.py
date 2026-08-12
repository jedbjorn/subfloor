"""Contract tests for explicit Docker build-cache garbage collection."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import docker_cache


class Runner:
    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, check, text):
        self.assertEqualProtocol(check, text)
        self.commands.append(tuple(command))
        return subprocess.CompletedProcess(command, self.status)

    @staticmethod
    def assertEqualProtocol(check, text) -> None:
        if check is not False or text is not True:
            raise AssertionError("runner protocol changed")


class DockerCacheGcTest(unittest.TestCase):
    def test_default_keeps_recent_cache(self):
        runner = Runner()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = docker_cache.main([], runner=runner)

        self.assertEqual(result, 0)
        self.assertEqual(
            runner.commands,
            [
                (
                    "docker",
                    "builder",
                    "prune",
                    "--all",
                    "--force",
                    "--filter",
                    "until=168h",
                )
            ],
        )
        self.assertIn("older than 168h", output.getvalue())

    def test_all_requires_explicit_flag_and_has_no_age_filter(self):
        runner = Runner()

        result = docker_cache.main(["--all"], runner=runner)

        self.assertEqual(result, 0)
        self.assertEqual(
            runner.commands,
            [("docker", "builder", "prune", "--all", "--force")],
        )

    def test_custom_age_is_bounded_to_duration_vocabulary(self):
        runner = Runner()
        self.assertEqual(docker_cache.main(["--until", "24h"], runner=runner), 0)
        self.assertIn("until=24h", runner.commands[0])
        with self.assertRaises(SystemExit):
            docker_cache.main(["--until", "yesterday"], runner=Runner())

    def test_docker_failure_is_returned(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = docker_cache.main([], runner=Runner(status=23))
        self.assertEqual(result, 23)
        self.assertIn("status 23", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
