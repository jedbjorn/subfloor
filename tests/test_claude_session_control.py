#!/usr/bin/env python3
"""Hermetic Claude UUID, inbox-watcher, and dormant-resume fixtures."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ADAPTER = ENGINE / "adapters" / "claude"
sys.path.insert(0, str(ADAPTER))
sys.path.insert(0, str(ENGINE / "scripts"))

import run  # noqa: E402
import session_control as common_session_control  # noqa: E402
import watch  # noqa: E402
from claude_cli import probe_claude  # noqa: E402


def load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter_module = load_file(
    "claude_session_control_adapter", ADAPTER / "session_control.py"
)
launcher_module = load_file("claude_session_launcher", ADAPTER / "claude-session.py")

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)
NATIVE_ID = "12345678-1234-4234-8234-123456789abc"


def binding(
    *,
    native_id: str | None = NATIVE_ID,
    owner: bool = True,
    watcher: bool = True,
    permission: str = "auto",
) -> dict:
    capabilities = {
        "active_delivery": True,
        "deliver": True,
        "resume": True,
        "normal_steer": False,
        "settings": {
            "model": "claude-fable-5",
            "cwd": "/repo/.sc-worktrees/pln1",
            "effort": "high",
            "permission_mode": permission,
        },
    }
    return {
        "binding_id": 8,
        "native_session_id": native_id,
        "control_capabilities": json.dumps(capabilities),
        "archive_model": "claude-fable-5",
        "shortname": "PLN1",
        "flavor": "planner",
        "lease_pid": 41 if owner else None,
        "lease_start_ticks": 900 if owner else None,
        "active_channel_pid": 52 if watcher else None,
        "active_channel_start_ticks": 901 if watcher else None,
        "active_channel_heartbeat_at": "2026-07-22 03:59:30" if watcher else None,
    }


def completed(command: list[str], output: str, returncode: int = 0):
    return subprocess.CompletedProcess(command, returncode, output, "")


class ProbeTest(unittest.TestCase):
    @staticmethod
    def runner(version: str, help_text: str | None = None):
        flags = (
            "--session-id --resume --print --model --effort --permission-mode"
            if help_text is None
            else help_text
        )

        def run(command: list[str]):
            if command == ["claude", "--version"]:
                return completed(command, version)
            if command == ["claude", "--help"]:
                return completed(command, flags)
            raise AssertionError(command)

        return run

    def test_tested_cli_enables_uuid_launch_watcher_delivery_and_resume(self):
        probe = probe_claude(self.runner("2.1.216 (Claude Code)"))
        self.assertEqual(probe["cli_version"], "2.1.216")
        self.assertEqual(
            (probe["create"], probe["deliver"], probe["resume"]),
            (True, True, True),
        )

    def test_unknown_version_fails_active_closed_but_keeps_flag_tested_resume(self):
        probe = probe_claude(self.runner("2.2.0 (Claude Code)"))
        self.assertFalse(probe["create"])
        self.assertFalse(probe["active_delivery"])
        self.assertTrue(probe["resume"])


class LauncherTest(unittest.TestCase):
    def test_controlled_launcher_is_declared(self):
        adapter = run.load_adapter("claude")
        self.assertEqual(
            run.session_control_launch(adapter),
            ["python3", str(ADAPTER / "claude-session.py")],
        )

    def test_fresh_launch_supplies_uuid_and_applies_effective_route_and_effort(self):
        row = {
            "native_session_id": None,
            "display_name": "Plan-01",
            "control_capabilities": "{}",
        }
        native_id, command, capabilities = launcher_module.launch_plan(
            row,
            {"cli_version": "2.1.216", "create": True, "resume": True},
            cwd=Path("/repo/.sc-worktrees/pln1"),
            env={"SC_SESSION_MODEL": "claude-fable-5", "SC_SESSION_EFFORT": "high"},
            native_id_factory=lambda: uuid.UUID(NATIVE_ID),
        )
        self.assertEqual(native_id, NATIVE_ID)
        self.assertEqual(
            command,
            [
                "claude",
                "--session-id",
                NATIVE_ID,
                "--name",
                "Plan-01",
                "--model",
                "claude-fable-5",
                "--effort",
                "high",
                "--permission-mode",
                "auto",
            ],
        )
        self.assertEqual(
            capabilities["settings"],
            {
                "model": "claude-fable-5",
                "cwd": "/repo/.sc-worktrees/pln1",
                "effort": "high",
                "permission_mode": "auto",
            },
        )

    def test_sandbox_launch_records_and_applies_bypass_posture(self):
        native_id, command, capabilities = launcher_module.launch_plan(
            {
                "native_session_id": None,
                "display_name": None,
                "control_capabilities": "{}",
            },
            {"create": True, "resume": True},
            cwd=Path("/repo/.sc-worktrees/pln1"),
            env={"SC_SANDBOX": "1"},
            native_id_factory=lambda: uuid.UUID(NATIVE_ID),
        )
        self.assertEqual(native_id, NATIVE_ID)
        self.assertEqual(
            command,
            ["claude", "--session-id", NATIVE_ID, "--dangerously-skip-permissions"],
        )
        self.assertEqual(
            capabilities["settings"]["permission_mode"], "bypassPermissions"
        )

    def test_existing_binding_resumes_exact_uuid_with_stored_settings(self):
        row = binding()
        row["display_name"] = "Plan-01"
        native_id, command, _capabilities = launcher_module.launch_plan(
            row,
            {"create": True, "resume": True},
            cwd=Path("/repo/.sc-worktrees/pln1"),
            env={"SC_SESSION_MODEL": "wrong", "SC_SESSION_EFFORT": "low"},
        )
        self.assertEqual(native_id, NATIVE_ID)
        self.assertEqual(
            command,
            [
                "claude",
                "--resume",
                NATIVE_ID,
                "--name",
                "Plan-01",
                "--model",
                "claude-fable-5",
                "--effort",
                "high",
                "--permission-mode",
                "auto",
            ],
        )

    def test_main_registers_the_same_uuid_it_supplies_to_claude(self):
        class Connection:
            def execute(self, _query, _params):
                return self

            def fetchone(self):
                return {
                    "binding_id": 8,
                    "native_session_id": None,
                    "display_name": "Plan-01",
                    "control_capabilities": "{}",
                }

            def close(self):
                return None

        probe = {
            "cli_version": "2.1.216",
            "create": True,
            "deliver": True,
            "resume": True,
            "active_delivery": True,
        }
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "SC_SESSION_BINDING_ID": "8",
                    "SC_SESSION_MODEL": "claude-fable-5",
                },
                clear=False,
            ),
            mock.patch.object(
                launcher_module.db_driver, "connect", return_value=Connection()
            ),
            mock.patch.object(launcher_module, "probe_claude", return_value=probe),
            mock.patch.object(
                launcher_module.session_supervisor, "register_native_session"
            ) as register,
            mock.patch.object(
                launcher_module.os, "execvpe", side_effect=RuntimeError("exec stopped")
            ) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "exec stopped"):
                launcher_module.main()

        registered_id = register.call_args.args[2]
        command = execute.call_args.args[1]
        launch_env = execute.call_args.args[2]
        self.assertEqual(command[0:3], ["claude", "--session-id", registered_id])
        self.assertEqual(launch_env["SC_SESSION_ACTIVE_CHANNEL"], "claude-inbox")
        self.assertEqual(register.call_args.args[1], 8)


class AdapterTest(unittest.TestCase):
    def adapter(self, **kwargs):
        return adapter_module.ClaudeAdapter(now=lambda: NOW, **kwargs)

    def test_status_requires_id_owner_and_fresh_watcher(self):
        adapter = self.adapter()
        self.assertEqual(adapter.status(binding(native_id=None)), "starting")
        self.assertEqual(adapter.status(binding(owner=False, watcher=False)), "dormant")
        self.assertEqual(adapter.status(binding(watcher=False)), "active")
        self.assertEqual(adapter.status(binding()), "idle")

        stale = binding()
        stale["active_channel_heartbeat_at"] = "2026-07-22 03:57:00"
        self.assertEqual(adapter.status(stale), "active")

    def test_active_delivery_waits_for_read_at_acknowledgement(self):
        waited: list[int] = []
        adapter = self.adapter(
            unread=lambda _row: 1,
            ack_waiter=lambda row: waited.append(row["binding_id"]),
        )
        adapter.deliver(binding(), "ignored transport prompt")
        self.assertEqual(waited, [8])

    def test_default_ack_waiter_polls_until_running_messages_are_read(self):
        unread = iter([1, 0])
        sleeps: list[float] = []
        clocks = iter([0.0, 1.0])
        adapter_module._wait_for_ack(
            binding(),
            unread=lambda _row: next(unread),
            sleeper=sleeps.append,
            clock=lambda: next(clocks),
        )
        self.assertEqual(sleeps, [adapter_module.ACK_POLL_INTERVAL])

    def test_ack_waiter_aborts_when_owner_and_watcher_are_gone(self):
        clocks = iter([0.0, adapter_module.BINDING_RECHECK_INTERVAL])
        with self.assertRaisesRegex(RuntimeError, "owner and inbox watcher"):
            adapter_module._wait_for_ack(
                binding(),
                unread=lambda _row: 1,
                binding_reader=lambda _row: binding(owner=False, watcher=False),
                sleeper=lambda _seconds: self.fail("lost delivery slept again"),
                clock=lambda: next(clocks),
                now=lambda: NOW,
            )

    def test_ack_waiter_keeps_waiting_while_owner_or_watcher_is_live(self):
        for current_binding in (
            binding(owner=True, watcher=False),
            binding(owner=False, watcher=True),
        ):
            with self.subTest(current_binding=current_binding):
                unread = iter([1, 0])
                clocks = iter([0.0, adapter_module.BINDING_RECHECK_INTERVAL])
                sleeps: list[float] = []
                adapter_module._wait_for_ack(
                    binding(),
                    unread=lambda _row: next(unread),
                    binding_reader=lambda _row: current_binding,
                    sleeper=sleeps.append,
                    clock=lambda: next(clocks),
                    now=lambda: NOW,
                )
                self.assertEqual(sleeps, [adapter_module.ACK_POLL_INTERVAL])

    def test_missing_watcher_queues_without_assuming_foreground_delivery(self):
        adapter = self.adapter(unread=lambda _row: 1)
        with self.assertRaises(common_session_control.ProviderBusy):
            adapter.deliver(binding(watcher=False), "check inbox")

    def test_already_acknowledged_race_finishes_without_a_rearmed_watcher(self):
        waited: list[bool] = []
        adapter = self.adapter(
            unread=lambda _row: 0,
            ack_waiter=lambda _row: waited.append(True),
        )
        adapter.deliver(binding(watcher=False), "check inbox")
        self.assertEqual(waited, [])

    def test_manual_permission_posture_refuses_before_delivery_wait(self):
        waited: list[bool] = []
        adapter = self.adapter(
            unread=lambda _row: 1,
            ack_waiter=lambda _row: waited.append(True),
        )
        with self.assertRaisesRegex(RuntimeError, "managed wake requires"):
            adapter.deliver(binding(permission="manual"), "check inbox")
        self.assertEqual(waited, [])

    def test_dormant_resume_pins_uuid_route_effort_permission_and_worktree(self):
        calls: list[tuple[dict, list[str], Path]] = []
        adapter = self.adapter(
            resume_runner=lambda row, command, cwd: calls.append((row, command, cwd)),
            resume_probe=lambda: {"resume": True},
        )
        row = binding(owner=False, watcher=False)
        adapter.resume(row, "check inbox")
        self.assertEqual(
            calls,
            [
                (
                    row,
                    [
                        "claude",
                        "--resume",
                        NATIVE_ID,
                        "--model",
                        "claude-fable-5",
                        "--effort",
                        "high",
                        "--permission-mode",
                        "auto",
                        "-p",
                        "check inbox",
                    ],
                    Path("/repo/.sc-worktrees/pln1"),
                )
            ],
        )

    def test_resume_reprobes_before_spawning(self):
        calls: list[bool] = []
        adapter = self.adapter(
            resume_runner=lambda _row, _command, _cwd: calls.append(True),
            resume_probe=lambda: {"resume": False},
        )
        with self.assertRaisesRegex(RuntimeError, "failed the resume capability probe"):
            adapter.resume(binding(owner=False, watcher=False), "check inbox")
        self.assertEqual(calls, [])

    def test_fenced_runner_refuses_before_spawn_when_an_owner_is_live(self):
        class Connection:
            def close(self):
                return None

        spawned: list[bool] = []

        def supervise(_command, **kwargs):
            kwargs["on_pre_spawn"]()
            spawned.append(True)
            return 0

        with (
            mock.patch.object(
                adapter_module.db_driver, "connect", return_value=Connection()
            ),
            mock.patch.object(
                adapter_module.session_supervisor,
                "preflight_lease",
                side_effect=RuntimeError("validated owner is live"),
            ),
            mock.patch.object(
                adapter_module.session_supervisor, "supervise", side_effect=supervise
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "validated owner is live"):
                adapter_module._run_fenced_resume(
                    binding(owner=False, watcher=False),
                    ["claude", "--resume", NATIVE_ID, "-p", "check inbox"],
                    Path("/repo/.sc-worktrees/pln1"),
                )
        self.assertEqual(spawned, [])


class InboxWatcherTest(unittest.TestCase):
    def test_claude_watcher_registers_heartbeats_and_clears_exact_identity(self):
        calls: list[tuple[str, str, dict | None]] = []

        def register(method: str, path: str, payload: dict | None = None):
            calls.append((method, path, payload))
            return {"binding_id": 8, "pid": 77, "start_ticks": 901}

        def soft(method: str, path: str, payload: dict | None = None):
            calls.append((method, path, payload))
            if path == "/_sc/mem/messages":
                return {
                    "messages": [
                        {"message_id": 9, "kind": "result", "body": "unit ready"}
                    ]
                }
            return {"ok": True}

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "SC_SESSION_ACTIVE_CHANNEL": "claude-inbox",
                    "SC_SESSION_BINDING_ID": "8",
                },
                clear=False,
            ),
            mock.patch.object(watch, "_require_api"),
            mock.patch.object(watch, "_api", side_effect=register),
            mock.patch.object(watch, "_api_soft", side_effect=soft),
            mock.patch.object(watch.os, "getpid", return_value=77),
        ):
            result = watch.cmd_inbox(SimpleNamespace(timeout=1, interval=0))

        self.assertEqual(result, 0)
        actions = [
            payload.get("action")
            for _method, path, payload in calls
            if path == "/_sc/session-control/channel" and payload
        ]
        self.assertEqual(actions, ["register", "heartbeat", "clear"])
        for _method, path, payload in calls:
            if path == "/_sc/session-control/channel" and payload:
                self.assertEqual(payload["pid"], 77)


class ManagedPostureTest(unittest.TestCase):
    def test_claude_bypass_permission_vocabulary_is_approval_safe(self):
        common_session_control.validate_managed_wake_posture(
            {"settings": {"permission_mode": "bypassPermissions"}}
        )


if __name__ == "__main__":
    unittest.main()
