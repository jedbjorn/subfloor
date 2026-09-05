"""Claude shells get their permission bypass from a launch flag, not a
settings merge — Claude Code 2.1.256 ignores a project-scoped
`permissions.defaultMode: bypassPermissions`, so the merge that used to carry
sandbox shells (and, by accident, host shells with a leftover file) is dead."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run

BYPASS = "--dangerously-skip-permissions"


def _claude() -> dict:
    return json.loads((run.ADAPTERS / "claude" / "adapter.json").read_text())


class ClaudePermissionFlagsTest(unittest.TestCase):
    def test_host_launch_carries_bypass_in_both_modes(self) -> None:
        adapter = _claude()
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SC_SANDBOX", None)
            self.assertEqual(run.launch_mode_flags(adapter, headless=False), [BYPASS])
            self.assertEqual(run.launch_mode_flags(adapter, headless=True), [BYPASS])

    def test_sandbox_adds_no_second_copy(self) -> None:
        adapter = _claude()
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}, clear=True):
            self.assertEqual(run.launch_mode_flags(adapter, headless=False), [BYPASS])
            self.assertEqual(run.launch_mode_flags(adapter, headless=True), [BYPASS])

    def test_headless_argv_ends_with_bypass_then_prompt(self) -> None:
        adapter = _claude()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(run, "linked_vm_configured", return_value=False):
            argv = run.headless_command(
                adapter, "first turn", None,
                run.launch_mode_flags(adapter, headless=True),
            )
        self.assertEqual(argv[-3:], [BYPASS, "-p", "first turn"])

    def test_no_project_settings_merge_claims_bypass(self) -> None:
        # The ignored knob must not linger in anything the adapter writes.
        adapter = _claude()
        merged = json.dumps(adapter.get("merge_json") or {})
        self.assertNotIn("bypassPermissions", merged)
        self.assertNotIn("skipDangerousModePermissionPrompt", merged)
        self.assertEqual(adapter["sandbox"], {"env": {"IS_SANDBOX": "1"}})

    def test_sandbox_only_flags_stay_sandbox_only(self) -> None:
        adapter = {"sandbox": {"launch_flags": ["--yolo"]}}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run.launch_mode_flags(adapter, headless=False), [])
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}, clear=True):
            self.assertEqual(run.launch_mode_flags(adapter, headless=False), ["--yolo"])
            self.assertEqual(run.launch_mode_flags(adapter, headless=True), [])


if __name__ == "__main__":
    unittest.main()
