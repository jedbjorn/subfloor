"""Authoritative retained-harness surface and launch contracts."""
from __future__ import annotations

import json
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
    def test_surface_roster_contains_only_retained_harnesses(self) -> None:
        commands = []

        def executable(command: str) -> str:
            commands.append(command)
            return f"/bin/{command}"

        projection = harness_surfaces.project(executable=executable)

        expected = ["claude", "codex", "kimi", "opencode", "vibe"]
        self.assertEqual(harness_surfaces.known_terminal_harnesses(), expected)
        self.assertEqual(harness_surfaces.known_runnable_harnesses(), expected)
        self.assertEqual(harness_surfaces.known_interactive_harnesses(), expected)
        for harness in ("claude", "codex", "kimi", "opencode"):
            with self.subTest(harness=harness):
                self.assertEqual(
                    projection[harness]["surfaces"],
                    {
                        "terminal": True,
                        "one_shot": True,
                        "browser": True,
                        "sprint": True,
                    },
                )
                self.assertTrue(projection[harness]["healthy"])
                self.assertIsNone(projection[harness]["unavailable_reason"])
        self.assertEqual(
            projection["vibe"]["surfaces"],
            {
                "terminal": True,
                "one_shot": False,
                "browser": False,
                "sprint": False,
            },
        )
        self.assertEqual(sorted(projection), expected)
        self.assertEqual(sorted(commands), expected)

    def test_missing_runtime_and_disablement_have_stable_distinct_reasons(self) -> None:
        missing = harness_surfaces.project(executable=lambda command: None)
        disabled = harness_surfaces.project(
            env={"SC_DISABLED_HARNESSES": " codex "},
            executable=lambda command: command,
        )

        self.assertFalse(missing["codex"]["installed"])
        self.assertEqual(
            missing["codex"]["unavailable_reason"], "HARNESS_UNAVAILABLE"
        )
        self.assertFalse(disabled["codex"]["enabled"])
        self.assertFalse(disabled["codex"]["healthy"])
        self.assertEqual(disabled["codex"]["unavailable_reason"], "HARNESS_DISABLED")

    def test_model_visibility_is_separate_from_terminal_launch_eligibility(self) -> None:
        manifests = {
            "one-shot-only": {
                "harness": "one-shot-only",
                "surfaces": {
                    "terminal": False,
                    "one_shot": True,
                    "browser": False,
                    "sprint": False,
                },
                "headless": {"launch": ["one-shot"]},
            },
            "browser-only": {
                "harness": "browser-only",
                "surfaces": {
                    "terminal": False,
                    "one_shot": False,
                    "browser": True,
                    "sprint": False,
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            adapters = Path(tmp)
            for harness, manifest in manifests.items():
                path = adapters / harness / "adapter.json"
                path.parent.mkdir()
                path.write_text(json.dumps(manifest))
            with (
                mock.patch.object(harness_surfaces, "ADAPTERS", adapters),
                mock.patch.object(
                    harness_surfaces,
                    "SUPPORTED_HARNESSES",
                    frozenset(manifests),
                ),
                mock.patch.object(
                    harness_surfaces,
                    "_browser_contract_proven",
                    side_effect=lambda harness: harness == "browser-only",
                ),
            ):
                visible = harness_surfaces.known_runnable_harnesses()
                defaults = harness_surfaces.known_interactive_harnesses()

        self.assertEqual(visible, ["browser-only", "one-shot-only"])
        self.assertEqual(defaults, [])

    def test_interactive_detection_includes_only_terminal_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapters = Path(tmp)
            terminal = adapters / "terminal" / "adapter.json"
            nonterminal = adapters / "nonterminal" / "adapter.json"
            terminal.parent.mkdir()
            nonterminal.parent.mkdir()
            terminal.write_text(
                '{"harness":"terminal","launch":["terminal"],'
                '"surfaces":{"terminal":true}}'
            )
            nonterminal.write_text(
                '{"harness":"nonterminal","surfaces":{"terminal":false}}'
            )
            with mock.patch.object(run, "ADAPTERS", adapters), mock.patch.object(
                run.shutil, "which", side_effect=lambda command: f"/bin/{command}"
            ):
                detected = run.detect_harnesses()

        self.assertEqual(detected, ["terminal"])

    def test_unshipped_selector_is_rejected_before_command_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            run, "ADAPTERS", Path(tmp)
        ), mock.patch.object(
            run.shutil,
            "which",
            side_effect=AssertionError("unshipped selector reached PATH lookup"),
        ) as lookup, self.assertRaisesRegex(
            ValueError, "harness selector is not shipped"
        ):
            run.load_adapter("unshipped")

        lookup.assert_not_called()

    def test_explicit_unsupported_surfaces_fail_early(self) -> None:
        adapter = {
            "harness": "nonterminal",
            "surfaces": {"terminal": False, "one_shot": False},
        }

        for surface, label in (("terminal", "terminal"), ("one_shot", "one-shot")):
            with self.subTest(surface=surface), self.assertRaisesRegex(
                ValueError,
                rf"harness 'nonterminal' does not support {label}",
            ):
                run.require_harness_surface(adapter, surface)


if __name__ == "__main__":
    unittest.main()
