"""Authoritative harness surface/status projection contracts."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import harness_surfaces
import run


class HarnessSurfaceProjectionTest(unittest.TestCase):
    def test_existing_surface_roster_and_deepseek_gates_are_explicit(self) -> None:
        commands = []

        def executable(command: str) -> str:
            commands.append(command)
            return f"/bin/{command}"

        projection = harness_surfaces.project(
            executable=executable,
        )

        self.assertEqual(
            harness_surfaces.known_terminal_harnesses(),
            ["claude", "codex", "kimi", "opencode", "vibe"],
        )
        for harness in ("claude", "codex", "kimi", "opencode"):
            with self.subTest(harness=harness):
                self.assertEqual(projection[harness]["surfaces"], {
                    "terminal": True,
                    "one_shot": True,
                    "browser": True,
                    "sprint": True,
                })
                self.assertTrue(projection[harness]["healthy"])
                self.assertIsNone(projection[harness]["unavailable_reason"])
        self.assertEqual(projection["vibe"]["surfaces"], {
            "terminal": True,
            "one_shot": False,
            "browser": False,
            "sprint": False,
        })
        self.assertEqual(projection["deepseek"], {
            "shipped": True,
            "installed": True,
            "enabled": True,
            "healthy": True,
            "compatibility": "declared",
            "surfaces": {
                "terminal": False,
                "one_shot": True,
                "browser": True,
                "sprint": False,
            },
            "unavailable_reason": None,
        })
        self.assertIn("dsh", commands)
        self.assertNotIn("deepseek-harness", commands)

    def test_missing_runtime_and_disablement_have_stable_distinct_reasons(self) -> None:
        missing = harness_surfaces.project(
            executable=lambda command: None,
        )
        disabled = harness_surfaces.project(
            env={"SC_DISABLED_HARNESSES": " codex, DEEPSEEK "},
            executable=lambda command: command,
        )

        self.assertFalse(missing["deepseek"]["installed"])
        self.assertEqual(
            missing["deepseek"]["unavailable_reason"], "HARNESS_UNAVAILABLE"
        )
        self.assertFalse(disabled["codex"]["enabled"])
        self.assertFalse(disabled["codex"]["healthy"])
        self.assertEqual(disabled["codex"]["unavailable_reason"], "HARNESS_DISABLED")
        self.assertFalse(disabled["deepseek"]["enabled"])
        self.assertEqual(
            disabled["deepseek"]["unavailable_reason"], "HARNESS_DISABLED"
        )

    def test_unknown_historical_harness_is_readable_but_never_runnable(self) -> None:
        projection = harness_surfaces.project(
            ["retired-harness"], executable=lambda command: command
        )

        self.assertEqual(projection["retired-harness"], {
            "shipped": False,
            "installed": False,
            "enabled": False,
            "healthy": False,
            "compatibility": "unknown",
            "surfaces": {
                "terminal": False,
                "one_shot": False,
                "browser": False,
                "sprint": False,
            },
            "unavailable_reason": "HARNESS_NOT_SHIPPED",
        })
        self.assertNotIn(
            "retired-harness", harness_surfaces.known_terminal_harnesses()
        )

    def test_terminal_detection_excludes_shipped_non_terminal_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapters = Path(tmp)
            terminal = adapters / "terminal" / "adapter.json"
            browser_only = adapters / "browser-only" / "adapter.json"
            terminal.parent.mkdir()
            browser_only.parent.mkdir()
            terminal.write_text(
                '{"harness":"terminal","launch":["terminal"],'
                '"surfaces":{"terminal":true}}'
            )
            browser_only.write_text(
                '{"harness":"browser-only","runtime":{"command":"browser"},'
                '"surfaces":{"terminal":false}}'
            )
            with mock.patch.object(run, "ADAPTERS", adapters), mock.patch.object(
                run.shutil, "which", side_effect=lambda command: f"/bin/{command}"
            ):
                detected = run.detect_harnesses()

        self.assertEqual(detected, ["terminal"])
        self.assertNotIn("browser-only", detected)

    def test_explicit_unsupported_terminal_and_one_shot_requests_fail_early(self) -> None:
        adapter = {
            "harness": "deepseek",
            "surfaces": {"terminal": False, "one_shot": False},
        }

        for surface, label in (("terminal", "terminal"), ("one_shot", "one-shot")):
            with self.subTest(surface=surface), self.assertRaisesRegex(
                ValueError,
                rf"harness 'deepseek' does not support {label}",
            ):
                run.require_harness_surface(adapter, surface)

    def test_local_web_entry_requires_an_explicit_interactive_contract(self) -> None:
        run.require_local_web_surface({
            "harness": "deepseek",
            "interactive": {"kind": "local_web"},
        })
        with self.assertRaisesRegex(ValueError, "does not support local Web entry"):
            run.require_local_web_surface({"harness": "codex"})


if __name__ == "__main__":
    unittest.main()
