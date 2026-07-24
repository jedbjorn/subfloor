#!/usr/bin/env python3
"""Contract tests for the supported Make operator surface.

Every target is a thin delegation to ./sc. These tests pin the public command
and argument shape so a Make-only behavior fork, a missing shell guard, or help
drift fails before it reaches an operator.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


@unittest.skipUnless(shutil.which("make"), "GNU Make is not installed")
class MakeAliasContractTest(unittest.TestCase):
    def test_documented_targets_delegate_exactly_to_sc(self):
        cases = [
            (("dos-enter",), "./sc enter"),
            (("dos-e", "s=DEV1"), "./sc enter-DEV1"),
            (("dos-launch", "ARGS=--no-build"), "./sc launch --no-build"),
            (("dos-l",), "./sc launch"),
            (("dos-restart", "ARGS=--yes --no-build"),
             "./sc restart --yes --no-build"),
            (("dos-r", "ARGS=--yes"), "./sc restart --yes"),
            (("dos-down",), "./sc down"),
            (("dos-d",), "./sc down"),
            (("dos-update", "ARGS=--no-fetch"), "./sc update --no-fetch"),
            (("dos-u",), "./sc update"),
            (("dos-test", "ARGS=tests/test_aliases_make.py"),
             "./sc test tests/test_aliases_make.py"),
            (("dos-t",), "./sc test"),
            (("dos-url",), "./sc url"),
            (("dos-status", "s=DEV1", "ARGS=--json"),
             "./sc interface status DEV1 --json"),
            (("dos-start", "s=DEV1", "ARGS=--harness codex"),
             "./sc interface start DEV1 --harness codex"),
            (("dos-view", "s=DEV1"), "./sc interface view DEV1"),
            (("dos-attach", "s=DEV1"), "./sc interface attach DEV1"),
            (("dos-take", "s=DEV1"),
             "./sc interface take-control DEV1"),
            (("dos-take-control", "s=DEV1"),
             "./sc interface take-control DEV1"),
            (("dos-stop", "s=DEV1", "ARGS=--force"),
             "./sc interface stop DEV1 --force"),
            (("dos-reconcile", "s=DEV1", "ARGS=--close"),
             "./sc interface reconcile DEV1 --close"),
            (("dos-recover", "s=DEV1", "ARGS=--force --yes"),
             "./sc interface recover DEV1 --force --yes"),
            (("dos-models", "ARGS=list codex"), "./sc models list codex"),
            (("dos-model-refresh",), "./sc models refresh"),
            (("dos-model-list", "h=codex"), "./sc models list codex"),
            (("dos-model-resolve", "h=codex", "m=gpt-5.6-sol", "s=DEV1"),
             "./sc models resolve codex gpt-5.6-sol --shell DEV1"),
            (("dos-sprint", "ARGS=status --all"), "./sc sprint status --all"),
            (("dos-watch", "ARGS=list --all"), "./sc watch list --all"),
            (("dos-job", "ARGS=status 7"), "./sc job status 7"),
            (("dos-build",), "./sc build"),
            (("dos-logs",), "./sc logs"),
            (("dos-serve", "ARGS=--port 9900"), "./sc serve --port 9900"),
            (("dos-health",), "./sc health"),
            (("dos-ports",), "./sc ports"),
            (("dos-verify",), "./sc verify"),
            (("dos-map", "ARGS=--help"), "./sc map --help"),
            (("dos-render", "ARGS=flat"), "./sc render flat"),
            (("dos-snapshot",), "./sc snapshot"),
            (("dos-deps", "ARGS=--help"), "./sc deps --help"),
            (("dos-install",), "./sc install"),
            (("dos-setup",), "./sc install"),
            (("dos-rollback",), "./sc rollback"),
            (("dos-token",), "./sc token"),
            (("dos-update-harnesses",), "./sc update-harnesses"),
            (("dos-feature", "ARGS=enable pg"), "./sc feature enable pg"),
            (("dos-feat",), "./sc feature"),
            (("dos-eject",), "./sc eject"),
            (("dos", "ARGS=doctor"), "./sc doctor"),
        ]
        for args, expected in cases:
            with self.subTest(target=args[0]):
                result = make("-n", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_shell_actions_fail_before_dispatch_when_s_is_missing(self):
        for target in (
            "dos-start",
            "dos-view",
            "dos-attach",
            "dos-take",
            "dos-take-control",
            "dos-stop",
            "dos-reconcile",
            "dos-recover",
        ):
            with self.subTest(target=target):
                result = make("-n", target)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(
                    f"{target}: requires s=<shell-shortname>", result.stderr
                )

    def test_model_resolve_requires_harness_and_model_before_dispatch(self):
        result = make("-n", "dos-model-resolve", "h=codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "dos-model-resolve: requires h=<harness> m=<model> "
            "[s=<shell-shortname>]",
            result.stderr,
        )

    def test_full_help_covers_token_dispatch_and_operator_groups(self):
        result = make("dos-help")
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = result.stdout
        self.assertEqual(
            help_text.count(
                "dos-token                   print the browser sign-in token "
                "(stdout only)"
            ),
            1,
        )
        for heading in ("HOT", "INTERFACE", "MODELS + SPRINT", "MAINTENANCE"):
            self.assertIn(heading, help_text)
        for target in (
            "dos-status",
            "dos-start",
            "dos-view",
            "dos-attach",
            "dos-take-control",
            "dos-stop",
            "dos-reconcile",
            "dos-recover",
            "dos-models",
            "dos-model-refresh",
            "dos-model-list",
            "dos-model-resolve",
            "dos-sprint",
            "dos-watch",
            "dos-job",
            "dos-setup",
            "dos-token",
            "dos-url",
            "dos ARGS='<cmd>'",
        ):
            self.assertIn(target, help_text)

    def test_quick_chart_lists_the_url_recall_path(self):
        """dos-h is the chart an operator reaches for when the boot summary
        has scrolled away — the recall command has to be ON it."""
        result = make("dos-h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dos-url", result.stdout)


if __name__ == "__main__":
    unittest.main()
