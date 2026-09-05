"""Sandbox memory policy and operator configuration coverage."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sandbox_resources


class DockerInfo:
    def __init__(self, memory: int = 20 * 1024**3) -> None:
        self.memory = memory
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, check, text, capture_output=False):
        self.assertEqualProtocol(check, text, capture_output)
        command = tuple(command)
        self.commands.append(command)
        if command != ("docker", "info", "--format", "{{.MemTotal}}"):
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, f"{self.memory}\n", "")

    @staticmethod
    def assertEqualProtocol(check, text, capture_output) -> None:
        if check is not False or text is not True or capture_output is not True:
            raise AssertionError("runner protocol changed")


class SandboxResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Path(self.temporary.name) / "instance.json"
        self.config.write_text(
            '{"instance_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","port":8800}\n'
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_block(self, value) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "instance_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "sandbox_resources": value,
                }
            )
            + "\n"
        )

    def test_default_targets_twelve_gib_and_disables_swap(self):
        docker = DockerInfo()

        arguments, policy = sandbox_resources.docker_arguments(
            self.config, runner=docker
        )

        expected = 12 * 1024**3
        self.assertEqual(policy.bytes, expected)
        self.assertIsNone(policy.configured)
        self.assertEqual(
            arguments,
            ("--memory", str(expected), "--memory-swap", str(expected)),
        )

    def test_default_clamps_to_eighty_percent_of_small_daemon(self):
        policy = sandbox_resources.resolve_memory(
            self.config,
            runner=DockerInfo(5 * 1024**3),
        )

        self.assertEqual(policy.bytes, 4 * 1024**3)

    def test_configured_override_may_use_safe_maximum(self):
        self.write_block({"memory": "16G"})

        policy = sandbox_resources.resolve_memory(self.config, runner=DockerInfo())

        self.assertEqual(policy.bytes, 16 * 1024**3)
        self.assertEqual(policy.configured, "16G")

    def test_configured_override_above_safety_boundary_refuses(self):
        self.write_block({"memory": "17g"})

        with self.assertRaisesRegex(
            sandbox_resources.SandboxResourceError,
            "exceeds the safe maximum 16 GiB",
        ):
            sandbox_resources.resolve_memory(self.config, runner=DockerInfo())

    def test_invalid_or_too_small_values_refuse(self):
        for value, message in (
            ("max", "positive integer"),
            ("128m", "at least 512 MiB"),
        ):
            with self.subTest(value=value):
                self.write_block({"memory": value})
                with self.assertRaisesRegex(
                    sandbox_resources.SandboxResourceError,
                    message,
                ):
                    sandbox_resources.resolve_memory(self.config, runner=DockerInfo())

    def test_unknown_resource_key_refuses(self):
        self.write_block({"memory": "12g", "cpu": "2"})

        with self.assertRaisesRegex(
            sandbox_resources.SandboxResourceError,
            "unknown key 'cpu'",
        ):
            sandbox_resources.resolve_memory(self.config, runner=DockerInfo())

    def test_cli_override_and_default_preserve_unrelated_instance_state(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = sandbox_resources.main(
                ["16g"], config_path=self.config, runner=DockerInfo()
            )

        self.assertEqual(result, 0)
        payload = json.loads(self.config.read_text())
        self.assertEqual(payload["instance_id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(payload["port"], 8800)
        self.assertEqual(payload["sandbox_resources"], {"memory": "16g"})
        self.assertIn("takes effect", stdout.getvalue())

        with redirect_stdout(io.StringIO()):
            result = sandbox_resources.main(
                ["default"], config_path=self.config, runner=DockerInfo()
            )
        self.assertEqual(result, 0)
        payload = json.loads(self.config.read_text())
        self.assertNotIn("sandbox_resources", payload)
        self.assertEqual(payload["port"], 8800)

    def test_cli_rejects_override_above_live_daemon_capacity(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = sandbox_resources.main(
                ["17g"], config_path=self.config, runner=DockerInfo()
            )

        self.assertEqual(result, 2)
        self.assertIn("exceeds the safe maximum", stderr.getvalue())
        self.assertNotIn("sandbox_resources", json.loads(self.config.read_text()))

    def test_oom_watcher_suppresses_baseline_then_reports_increment(self):
        events = Path(self.temporary.name) / "memory.events.local"
        memory_max = Path(self.temporary.name) / "memory.max"
        events.write_text("low 0\noom 2\noom_kill 2\n")
        memory_max.write_text(f"{12 * 1024**3}\n")
        messages: list[str] = []
        watcher = sandbox_resources.OomKillWatcher(
            events_path=events,
            memory_max_path=memory_max,
            emit=messages.append,
        )

        watcher.poll()
        self.assertEqual(messages, [])
        events.write_text("low 0\noom 4\noom_kill 4\n")
        watcher.poll()

        self.assertEqual(len(messages), 1)
        self.assertIn("kernel killed 2 processes", messages[0])
        self.assertIn("12 GiB hard limit", messages[0])
        self.assertIn("oom_kill=4", messages[0])

    def test_oom_watcher_reports_unavailable_seam_once(self):
        messages: list[str] = []
        missing = Path(self.temporary.name) / "missing"
        watcher = sandbox_resources.OomKillWatcher(
            events_path=missing,
            memory_max_path=missing,
            emit=messages.append,
        )

        watcher.poll()
        watcher.poll()

        self.assertEqual(len(messages), 1)
        self.assertIn("diagnostic unavailable", messages[0])

    def test_oom_watcher_thread_stops_cleanly(self):
        events = Path(self.temporary.name) / "memory.events.local"
        memory_max = Path(self.temporary.name) / "memory.max"
        events.write_text("oom_kill 0\n")
        memory_max.write_text(f"{12 * 1024**3}\n")
        watcher = sandbox_resources.OomKillWatcher(
            events_path=events,
            memory_max_path=memory_max,
            interval=0.01,
            emit=lambda _message: None,
        )

        watcher.start()
        time.sleep(0.02)
        watcher.stop()

        self.assertFalse(watcher.is_alive())


if __name__ == "__main__":
    unittest.main()
