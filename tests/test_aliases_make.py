#!/usr/bin/env python3
"""Contract tests for the supported Make operator surface.

Every target is a thin delegation to ./sc. These tests pin the public command
and argument shape so a Make-only behavior fork, a missing shell guard, or help
drift fails before it reaches an operator.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A described help line: an indented target (or `a / b` alias pair), optional
# argument hints, then TWO OR MORE spaces and the description. A bare name in a
# middot-separated list ("dos-build · dos-logs") is single-spaced and therefore
# never matches — which is the point: it is not a description.
DESCRIBED_LINE = re.compile(
    r"^ {4}(dos-[a-z-]+(?: / dos-[a-z-]+)?)"
    r"((?: \[?[a-z]=[a-z<>-]+\]?| ARGS='<cmd>')*)"
    r" {2,}(\S.*?)\s*$"
)

# Every target that lost its help description when dos-help was rewritten from
# a boxed table into grouped lists (decision #58). Each one still exists and
# still runs, so asserting the NAME appears proves nothing — assert the text.
COMMANDS_THAT_MUST_EXPLAIN_THEMSELVES = (
    "dos-build",
    "dos-deps",
    "dos-health",
    "dos-install",
    "dos-logs",
    "dos-map",
    "dos-ports",
    "dos-render",
    "dos-rollback",
    "dos-serve",
    "dos-snapshot",
    "dos-update-harnesses",
    "dos-verify",
)


def described_targets(help_text: str) -> dict[str, str]:
    """Map every target in `help_text` to its description, alias pairs split."""
    described: dict[str, str] = {}
    for line in help_text.splitlines():
        match = DESCRIBED_LINE.match(line)
        if not match:
            continue
        names, _args, description = match.groups()
        for name in names.split(" / "):
            described[name] = description
    return described


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

    def test_full_help_describes_every_maintenance_command(self):
        """A name is not documentation. `make dos-help` must say what each
        maintenance command DOES — the regression decision #58 records is a
        row of bare names that a name-level check reports as healthy."""
        result = make("dos-help")
        self.assertEqual(result.returncode, 0, result.stderr)
        described = described_targets(result.stdout)

        for target in COMMANDS_THAT_MUST_EXPLAIN_THEMSELVES:
            with self.subTest(target=target):
                self.assertIn(
                    target,
                    described,
                    f"`make dos-help` lists {target} without a description — "
                    "a bare name does not tell an operator what it does",
                )
                description = described[target]
                self.assertGreaterEqual(
                    len(description.split()), 3,
                    f"{target}'s description is too thin to be useful: "
                    f"{description!r}",
                )
                self.assertFalse(
                    description.startswith("dos-"),
                    f"{target}'s description is another target name, not "
                    f"prose: {description!r}",
                )

        # One description copy-pasted across the group would satisfy every
        # check above; nothing may stand in for another command's text.
        descriptions = [
            described[t]
            for t in COMMANDS_THAT_MUST_EXPLAIN_THEMSELVES
            if t in described
        ]
        self.assertEqual(
            len(set(descriptions)),
            len(descriptions),
            "two maintenance commands share one description",
        )

    def test_update_harnesses_help_names_every_harness_it_updates(self):
        """The pre-rewrite text said `claude + opencode + codex + vibe`; the
        engine also drives kimi, so restoring it verbatim would ship a doc that
        understates the command. Pin what it actually updates."""
        result = make("dos-help")
        self.assertEqual(result.returncode, 0, result.stderr)
        description = described_targets(result.stdout)["dos-update-harnesses"]
        for harness in ("claude", "opencode", "codex", "vibe", "kimi"):
            self.assertIn(harness, description)

    def test_quick_chart_lists_the_url_recall_path(self):
        """dos-h is the chart an operator reaches for when the boot summary
        has scrolled away — the recall command has to be ON it."""
        result = make("dos-h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dos-url", result.stdout)


if __name__ == "__main__":
    unittest.main()
