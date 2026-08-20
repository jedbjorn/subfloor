#!/usr/bin/env python3
"""Pin the executable shape-notice sender and Cartographer contracts."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import compose  # noqa: E402
import map_notices  # noqa: E402


VALID = (
    "shape: API routes landed — paths: app/api/; ref: feature #8 / PR #123\n"
    "flags: 41=SC-041, 52=MAP-052\n"
    "curate; verify and close each flag; mark this notice read last."
)


class ShapeNoticeParserTest(unittest.TestCase):
    def test_parses_numeric_id_name_pairs(self):
        notice = map_notices.parse_shape_notice(VALID)

        self.assertEqual("API routes landed", notice.summary)
        self.assertEqual("app/api/", notice.paths)
        self.assertEqual("feature #8 / PR #123", notice.reference)
        self.assertEqual(
            ((41, "SC-041"), (52, "MAP-052")),
            tuple((flag.flag_id, flag.name) for flag in notice.flags),
        )

    def test_flags_none_is_explicit_and_valid(self):
        notice = map_notices.parse_shape_notice(
            VALID.replace("flags: 41=SC-041, 52=MAP-052", "flags: none")
        )
        self.assertEqual((), notice.flags)

    def test_malformed_or_incomplete_notices_fail_closed(self):
        cases = (
            (VALID.replace("flags: 41=SC-041, 52=MAP-052\n", ""), "three lines"),
            (VALID.replace("41=SC-041", "SC-041=41"), "malformed flag identity"),
            (VALID.replace("41=SC-041, 52=MAP-052", "41=SC-041, 41=SC-041"), "duplicate"),
            (VALID.replace("mark this notice read last", "mark read"), "mark-read-last"),
        )
        for body, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(map_notices.ShapeNoticeError, message):
                    map_notices.parse_shape_notice(body)


class InstructionContractTest(unittest.TestCase):
    def setUp(self):
        self.skill = (
            ENGINE / "assets" / "skills" / "cartographer" / "SKILL.md"
        ).read_text()

    def test_sender_contract_requires_flag_identity_or_none(self):
        for text in (
            "Open blocking map-quality flags before sending",
            "flags: <numeric_id>=<SC-name>",
            "Write `flags: none` when no flag",
        ):
            self.assertIn(text, self.skill)
        self.assertIn("Open any blocking map-quality flag first", compose.MAP_DISCREPANCY_BLOCK)
        self.assertIn("flags: <numeric_id>=<SC-name>", compose.MAP_DISCREPANCY_BLOCK)

    def test_cartographer_closes_exact_rows_before_marking_read(self):
        get_position = self.skill.index("sc mem get flags <numeric_id>")
        close_position = self.skill.index("sc mem flag close")
        read_position = self.skill.index("--message mark-read <message_id>")
        self.assertLess(get_position, close_position)
        self.assertLess(close_position, read_position)
        self.assertIn("ID/name mismatch", self.skill)
        self.assertIn("already-resolved row", self.skill)
        self.assertIn("Send no closure reply", self.skill)


if __name__ == "__main__":
    unittest.main(verbosity=2)
