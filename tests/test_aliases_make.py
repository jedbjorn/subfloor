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

# A description too long for one line wraps to the description column with no
# target of its own. Folding it back means a pin reads the WHOLE description,
# not just the clause that happened to fit on the first line.
CONTINUATION_LINE = re.compile(r"^ {32}(\S.*?)\s*$")

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

RETIRED_INTERFACE_TARGETS = (
    "dos-status",
    "dos-start",
    "dos-view",
    "dos-attach",
    "dos-take",
    "dos-take-control",
    "dos-stop",
    "dos-reconcile",
    "dos-recover",
)


def described_targets(help_text: str) -> dict[str, str]:
    """Map every target in `help_text` to its description, alias pairs split.

    Wrapped continuation lines fold into the entry above them; anything else
    ends the entry, so a continuation can never drift onto a later target."""
    described: dict[str, str] = {}
    current: list[str] = []
    for line in help_text.splitlines():
        match = DESCRIBED_LINE.match(line)
        if match:
            names, _args, description = match.groups()
            current = names.split(" / ")
            for name in current:
                described[name] = description
            continue
        wrapped = CONTINUATION_LINE.match(line) if current else None
        if wrapped:
            for name in current:
                described[name] += " " + wrapped.group(1)
            continue
        current = []
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
    def test_retired_dos_token_alias_stays_gone(self):
        """`dos-token` existed for one job: paste the sign-in credential into
        the browser by hand. The browser attaches its own now, so the alias is
        retired — and a retired alias needs a test, or the next person adding a
        maintenance target restores it from muscle memory. `./sc token` itself
        stays: it is still the recovery path when the automatic attach cannot
        run, so this pins the ALIAS being gone, never the capability."""
        result = make("-n", "dos-token")
        self.assertNotEqual(result.returncode, 0,
                            "dos-token still resolves as a make target")
        self.assertNotIn("dos-token", make("dos-help").stdout)
        self.assertNotIn("dos-token", make("dos-h").stdout)

    def test_retired_interface_aliases_stay_gone(self):
        help_text = make("dos-help").stdout
        for target in RETIRED_INTERFACE_TARGETS:
            with self.subTest(target=target):
                result = make("-n", target, "s=DEV1")
                self.assertNotEqual(
                    result.returncode, 0, f"{target} still resolves as a make target"
                )
                self.assertNotIn(target, help_text)
        self.assertNotIn("INTERFACE", help_text)

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
            (("dos-models", "ARGS=list codex"), "./sc models list codex"),
            (("dos-model-refresh",), "./sc models refresh"),
            (("dos-model-list", "h=codex"), "./sc models list codex"),
            (("dos-model-resolve", "h=codex", "m=gpt-5.6-sol", "s=DEV1"),
             "./sc models resolve codex gpt-5.6-sol --shell DEV1"),
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

    def test_model_resolve_requires_harness_and_model_before_dispatch(self):
        result = make("-n", "dos-model-resolve", "h=codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "dos-model-resolve: requires h=<harness> m=<model> "
            "[s=<shell-shortname>]",
            result.stderr,
        )

    def test_full_help_covers_operator_groups(self):
        result = make("dos-help")
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = result.stdout
        for heading in ("HOT", "MODELS + JOBS", "MAINTENANCE"):
            self.assertIn(heading, help_text)
        for target in (
            "dos-models",
            "dos-model-refresh",
            "dos-model-list",
            "dos-model-resolve",
            "dos-job",
            "dos-setup",
            "dos-url",
            "dos ARGS='<cmd>'",
        ):
            self.assertIn(target, help_text)
        self.assertNotIn("dos-sprint", help_text)
        self.assertNotIn("dos-watch", help_text)

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

    def test_branching_commands_help_describes_the_branch_not_the_headline(self):
        """Four of these commands take a path that does something OTHER than
        their headline, and help that states only the headline promises work
        the code will not do (SC-146/147/148):

        - rollback exits SUCCESS after restoring the DB ALONE when there is no
          .sc-state/engine.ref.prev — the pair-restore is intent, not guarantee;
        - deps skips venv/pip entirely for a host-managed .venv, verifying the
          declared pins instead of installing anything into it;
        - snapshot writes a SECOND artifact, the authored map layer, to local
          map/content.sql;
        - verify runs the fresh-fork init first when the rebuilt instance has
          no active user + shell.

        Each marker below is the condition or artifact the happy-path wording
        omitted; losing it is the wording regressing, not a rename.
        """
        result = make("dos-help")
        self.assertEqual(result.returncode, 0, result.stderr)
        described = described_targets(result.stdout)
        for target, marker in (
            ("dos-rollback", "engine.ref.prev"),
            ("dos-deps", "host-managed"),
            ("dos-snapshot", "map/content.sql"),
            ("dos-verify", "empty instance"),
        ):
            with self.subTest(target=target):
                self.assertIn(
                    marker,
                    described.get(target, ""),
                    f"{target}'s help no longer states the branch that does "
                    f"less than the headline (expected {marker!r}): "
                    f"{described.get(target)!r}",
                )

    def test_quick_chart_lists_the_url_recall_path(self):
        """dos-h is the chart an operator reaches for when the boot summary
        has scrolled away — the recall command has to be ON it."""
        result = make("dos-h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dos-url", result.stdout)


if __name__ == "__main__":
    unittest.main()
