"""Codex launches carry a working execution policy on every seat.

Codex's own `read-only`/`workspace-write` sandbox shells out to a bundled
bubblewrap; on a host that denies unprivileged mount-propagation changes it
fails before the first command runs (`bwrap: Failed to make / slave`), which
kills the seat outright. Every launch therefore carries
`--sandbox danger-full-access` — the same policy the browser-chat lane sends
through the app-server — and never the YOLO bypass flag, which codex rejects
alongside `--ask-for-approval` and which defeats the branch-guard hook's
exit-2 deny.
"""
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

INTERACTIVE_FLAGS = [
    "--sandbox",
    "danger-full-access",
    "--ask-for-approval",
    "never",
]
HEADLESS_FLAGS = ["--sandbox", "danger-full-access"]
BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def _codex() -> dict:
    return json.loads((run.ADAPTERS / "codex" / "adapter.json").read_text())


class CodexPermissionFlagsTest(unittest.TestCase):
    def test_host_shell_gets_a_runnable_sandbox_policy_without_bypass_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            flags = run.launch_mode_flags(_codex(), headless=False)
        self.assertEqual(flags, INTERACTIVE_FLAGS)
        self.assertNotIn(BYPASS, flags)

    def test_no_adapter_declares_an_admin_only_permission_flag_layer(self) -> None:
        # Every seat gets the same always-on set: PR #1582 removed codex's
        # `host_admin` block when the direct-host Admin policy became identical
        # to the ordinary one, and no manifest has declared the key since. The
        # launcher no longer reads it, so re-introducing one here would be a
        # silently ignored elevation.
        for adapter_dir in sorted(run.ADAPTERS.iterdir()):
            manifest = adapter_dir / "adapter.json"
            if not manifest.is_file():
                continue
            with self.subTest(harness=adapter_dir.name):
                self.assertNotIn(
                    "host_admin", json.loads(manifest.read_text())
                )

    def test_headless_gets_the_policy_without_the_approval_flag(self) -> None:
        # `codex exec` has no --ask-for-approval; it never prompts.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                run.launch_mode_flags(_codex(), headless=True),
                HEADLESS_FLAGS,
            )

    def test_container_gets_the_same_policy_without_bypass_flag(self) -> None:
        # codex rejects the bypass flag alongside --ask-for-approval ("cannot
        # be used with '--dangerously-bypass-approvals-and-sandbox'"), so a
        # container launch that appended it never started; the always-on set
        # already grants the same policy and keeps the hook's exit-2 deny.
        for headless, expected in ((False, INTERACTIVE_FLAGS), (True, HEADLESS_FLAGS)):
            with self.subTest(headless=headless):
                with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}, clear=True):
                    flags = run.launch_mode_flags(_codex(), headless=headless)
                self.assertEqual(flags, expected)
                self.assertNotIn(BYPASS, flags)

    def test_no_launch_mode_leaves_the_bwrap_backed_sandbox_selected(self) -> None:
        # The failure this guards: an empty/`workspace-write` policy makes
        # codex wrap every command in bundled bwrap, which cannot start on a
        # host that denies unprivileged mount-propagation changes.
        for headless in (False, True):
            for env in ({}, {"SC_SANDBOX": "1"}):
                with self.subTest(headless=headless, sandbox=bool(env)):
                    with mock.patch.dict(os.environ, env, clear=True):
                        flags = run.launch_mode_flags(_codex(), headless=headless)
                    self.assertIn("danger-full-access", flags)
                    self.assertNotIn("workspace-write", flags)


if __name__ == "__main__":
    unittest.main()
