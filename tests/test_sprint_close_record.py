#!/usr/bin/env python3
"""H-23/H-24 on the skill layer: the close skill treats the board as a RECORD.

Guards the text of `assets/skills/sprint_orchestration_close/SKILL.md` — the
asset, which is the source; `.claude/skills/` is a per-boot render (decision
#71) and is never read here.

Why text assertions earn their brittleness: sprint 84 U5 rewrites this exact
body wholesale through migration 0112 (the three-artifact reseed). A reseed
that drops the freeze rule or restores `status: CLOSED` ships a close procedure
that RELEASES NOTHING — under H-1 a sprint is live iff its doc row is unfrozen
with units, so the `status:` line is display prose no liveness reader consults.
That regression is silent at every other layer: the skill still reads correctly
to a human, the close still "succeeds", and the binding stays armed. These are
the assertions that turn red on it.

Each method pins ONE property. Every section lookup asserts it found a
non-empty section first, so a renamed heading fails loudly instead of passing
vacuously against an empty string.

Run:
    python3 tests/test_sprint_close_record.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SKILL = ENGINE / "assets" / "skills" / "sprint_orchestration_close" / "SKILL.md"

# every column H-23 requires the frozen report to carry
BOARD_COLUMNS = ("seq", "title", "dev", "reviewer", "terminal state", "PR",
                 "review head")


class CloseSkillRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = SKILL.read_text()

    def section(self, heading: str) -> str:
        """Text under `## <heading>`, up to the next `## ` heading.

        Fence-aware on purpose: the report skeleton is a fenced block whose
        lines are themselves `## …`, and a naive splitter ends the section at
        the first of them — truncating the very content under test.

        Raises rather than returns "" when the heading is gone: an absent
        section must fail the test that reads it, never satisfy an
        assertNotIn by vacuum.
        """
        collected, fenced, started = [], False, False
        for line in self.body.splitlines():
            if line.startswith("```"):
                fenced = not fenced
            elif not fenced and line.startswith("## "):
                if started:
                    break
                if line == f"## {heading}":
                    started = True
                    continue
            if started:
                collected.append(line)
        self.assertTrue(
            started,
            f"section '## {heading}' is gone — a reseed renamed or dropped "
            f"it, and every assertion below it would pass vacuously")
        text = "\n".join(collected).strip()
        self.assertTrue(text, f"section '## {heading}' is empty")
        return text

    def test_close_is_a_freeze(self):
        """The close mechanism is `doc freeze`, stated as the whole action."""
        revoke = self.section("Revoke sprint authority")
        self.assertIn("./sc mem doc freeze <doc-id>", revoke)
        self.assertIn("is** freezing its doc", revoke)

    def test_close_never_instructs_a_body_edit(self):
        """The no-op close must not come back — the U1 consequence.

        Two independent detectors, because the regression has two shapes: the
        COMMAND returning (`doc edit` on the sprint doc) and the INSTRUCTION
        returning (`status: CLOSED` written as a step rather than a
        prohibition).
        """
        revoke = self.section("Revoke sprint authority")
        with self.subTest("no doc-edit command anywhere in the skill"):
            self.assertNotIn("mem doc edit", self.body)
        with self.subTest("status: CLOSED survives only under a prohibition"):
            self.assertIn("NEVER close a sprint by editing its body", revoke)
            for line in revoke.splitlines():
                if "status: CLOSED" in line:
                    self.assertIn(
                        "NEVER", line,
                        "`status: CLOSED` appears outside a prohibition — as "
                        "an instruction it is a close that releases nothing")

    def test_close_says_why_the_status_line_is_not_the_mechanism(self):
        """Bare 'freeze instead' is followed the first time and reasoned around
        the second. The H-1 definition is what makes the rule stick."""
        revoke = self.section("Revoke sprint authority")
        for token in ("frozen = 0", "sprint_units", "display prose"):
            with self.subTest(token=token):
                self.assertIn(token, revoke)

    def test_report_skeleton_carries_the_final_board(self):
        """H-23: the frozen report is the board's only durable copy."""
        report = self.section("Write the sprint report")
        self.assertIn("## Final Board", report)

    def test_final_board_is_built_from_structured_rows(self):
        """H-23: read `sprint_units`, not a render — with every column named.

        The columns are the point. A 'Final Board' section carrying seq+title
        only is a table that satisfies the heading and loses the record.
        """
        report = self.section("Write the sprint report")
        self.assertIn("`sprint_units` rows", report)
        for column in BOARD_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, report)

    def test_final_board_forbids_copying_a_render(self):
        """H-24 at the point of use: the table is assembled from rows."""
        report = self.section("Write the sprint report")
        self.assertRegex(report, r"NEVER assemble that table from")

    def test_structural_read_rule_is_stated_once_up_front(self):
        """H-24: the rule a reseed is most likely to drop, because it directs
        no single command — it governs every read in the file."""
        preamble = self.body.split("## Confirm the close trigger")[0]
        self.assertIn("Take every fact from structure", preamble)
        self.assertIn("NEVER read a fact out of a persisted render", preamble)

    def test_live_board_is_named_as_a_live_read(self):
        """The rule must not read as 'never look at a board'. `sc sprint board`
        renders rows at call time and stays legal; without this the next author
        resolves the apparent contradiction by deleting the rule."""
        preamble = self.body.split("## Confirm the close trigger")[0]
        self.assertIn("sc sprint board", preamble)
        self.assertIn("render the rows at call time", preamble)


if __name__ == "__main__":
    unittest.main()
