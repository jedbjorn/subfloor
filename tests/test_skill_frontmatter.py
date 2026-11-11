#!/usr/bin/env python3
"""Skill frontmatter scalars survive a standard YAML parser.

A rendered SKILL.md is read by each harness's YAML parser. A description
written bare lost everything after ` #` there (a `Spec #116` reference became
a comment), so the engine's one writer quotes a value whenever its bare form
would not read back as the stored string, and the engine's readers accept the
quoted forms beside the legacy bare one.

PyYAML is not an engine dependency. Where it is importable the round-trip is
checked against it; the literal expected renders and the engine's own reader
are checked everywhere.

Run:
    python3 tests/test_skill_frontmatter.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import seed_skills  # noqa: E402
import skill as skill_mod  # noqa: E402
import skill_frontmatter  # noqa: E402
import skill_projection  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the test environment
    yaml = None

# value → the exact text the writer must emit for it.
QUOTED = {
    "Run the promotion canary for Spec #116.":
        '"Run the promotion canary for Spec #116."',
    "Use when: the seat is cold": '"Use when: the seat is cold"',
    'Say "hello" to the seat: twice': '"Say \\"hello\\" to the seat: twice"',
    '"Quoted" opening word': '"\\"Quoted\\" opening word"',
    "'single' opening word": "\"'single' opening word\"",
    "[draft] bracketed opening": '"[draft] bracketed opening"',
    "{draft} braced opening": '"{draft} braced opening"',
    "*starred* opening": '"*starred* opening"',
    "&anchor opening": '"&anchor opening"',
    "!tagged opening": '"!tagged opening"',
    "@mention opening": '"@mention opening"',
    "`sc skill` opening": '"`sc skill` opening"',
    "%percent opening": '"%percent opening"',
    "|pipe opening": '"|pipe opening"',
    ">angle opening": '">angle opening"',
    "#hash opening": '"#hash opening"',
    "- dash item opening": '"- dash item opening"',
    "? question opening": '"? question opening"',
    "ends with a colon:": '"ends with a colon:"',
    "back\\slash with a # comment": '"back\\\\slash with a # comment"',
    "tab\tinside": '"tab\\tinside"',
    "true": '"true"',
    "null": '"null"',
    "116": '"116"',
    "2026-11-11": '"2026-11-11"',
    "": '""',
}

# Ordinary prose stays bare, so existing renders do not churn.
BARE = [
    "Fork-local probe",
    "Git event procedures beyond the boot rules; load on a GitHub auth failure",
    "Planner-only; use when a capability needs durable guidance.",
    "The fork's dev hooks and seats",
    'Say "hello" mid-sentence',
    "Search the web through the engine (`sc search`, Tavily)",
    "Issue#12 with no space before the hash",
    "ratio 3:1 and a URL http://127.0.0.1:8837/x",
    "em dash — arrow → and accents é",
    "-leading dash without a space",
    "Version 2 of the thing",
]


def yaml_value(rendered: str) -> object:
    return yaml.safe_load(f"description: {rendered}\n")["description"]


class EmitParseTest(unittest.TestCase):
    def test_risky_values_are_quoted_exactly(self) -> None:
        for value, expected in QUOTED.items():
            with self.subTest(value=value):
                self.assertEqual(skill_frontmatter.emit_scalar(value), expected)

    def test_ordinary_prose_is_emitted_bare(self) -> None:
        for value in BARE:
            with self.subTest(value=value):
                self.assertEqual(skill_frontmatter.emit_scalar(value), value)

    def test_engine_reader_round_trips_every_form(self) -> None:
        for value in [*QUOTED, *BARE]:
            with self.subTest(value=value):
                rendered = skill_frontmatter.emit_scalar(value)
                self.assertEqual(skill_frontmatter.parse_scalar(rendered), value)
                self.assertEqual(
                    skill_frontmatter.parse_scalar(f"  {rendered}  "), value)

    @unittest.skipIf(yaml is None, "PyYAML is not installed")
    def test_standard_yaml_parser_round_trips_every_form(self) -> None:
        for value in [*QUOTED, *BARE]:
            with self.subTest(value=value):
                rendered = skill_frontmatter.emit_scalar(value)
                self.assertEqual(yaml_value(rendered), value)

    def test_reader_accepts_legacy_bare_and_quoted_forms(self) -> None:
        cases = {
            # Legacy bare values are taken whole: that is how they were stored.
            "Run the canary for Spec #116.": "Run the canary for Spec #116.",
            "Use when: cold": "Use when: cold",
            # Only a well-formed quoted scalar is unquoted.
            '"Run the canary for Spec #116."': "Run the canary for Spec #116.",
            "'it''s a # quoted: value'": "it's a # quoted: value",
            '"a" and "b"': '"a" and "b"',
            "'a' and 'b'": "'a' and 'b'",
            '"dangling \\': '"dangling \\',
            '"bad \\q escape"': '"bad \\q escape"',
            '"\\u00e9 \\x41 \\U0001f600"': "é A \U0001f600",
            '"': '"',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(skill_frontmatter.parse_scalar(raw), expected)

    def test_engine_asset_descriptions_render_safely(self) -> None:
        """Shipped engine skills: bare where bare is already valid YAML (no
        render churn), quoted only where the bare form was misread."""
        specs = seed_skills.engine_skill_specs()
        self.assertTrue(specs)
        for spec in specs:
            with self.subTest(skill=spec["name"]):
                description = (spec["description"] or "").strip()
                rendered = skill_frontmatter.emit_scalar(description)
                self.assertEqual(
                    skill_frontmatter.parse_scalar(rendered), description)
                self.assertEqual(
                    skill_frontmatter.emit_scalar(spec["name"]), spec["name"])
                if yaml is None:
                    continue
                self.assertEqual(yaml_value(rendered), description)
                try:
                    bare_is_exact = yaml_value(description) == description
                except yaml.YAMLError:
                    bare_is_exact = False
                self.assertEqual(rendered == description, bare_is_exact)


def build_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def draft(name: str, description_line: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description_line}\n"
        "common: false\n"
        "---\n\n"
        "procedure body\n"
    )


class RenderedSkillTest(unittest.TestCase):
    """Draft → stored row → every managed mirror → read back, unchanged."""

    def setUp(self) -> None:
        self.con = build_db()
        self.shell_id = self.con.execute(
            "INSERT INTO shells (display_name, shortname, system_prompt, flavor) "
            "VALUES ('custom', 'CUSTOM', 'prompt', NULL)"
        ).lastrowid

    def tearDown(self) -> None:
        self.con.close()

    def render(self, name: str, description: str) -> list[str]:
        skill_id = self.con.execute(
            "INSERT INTO skills (name, description, content, common) "
            "VALUES (?, ?, 'procedure body', 0)",
            (name, description),
        ).lastrowid
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.shell_id, skill_id),
        )
        roots = [str(path) for path in skill_projection.managed_skill_dirs()]
        self.assertIn(".claude/skills", roots)
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            skill_projection.reconcile_shell(
                self.con, self.shell_id, checkout, ensure_dirs=roots)
            return [
                (checkout / root / name / "SKILL.md").read_text()
                for root in roots
            ]

    def test_hash_reference_survives_every_adapter_mirror(self) -> None:
        description = (
            "Run the exact-ref, disposable promotion canary "
            "for Spec #116."
        )
        rendered = self.render("canary_probe", description)
        expected = (
            "---\nname: canary_probe\n"
            f'description: "{description}"\n---\n\nprocedure body\n'
        )
        self.assertEqual(rendered, [expected] * len(rendered))
        if yaml is not None:
            front = yaml.safe_load(expected.split("---\n")[1])
            self.assertEqual(
                front, {"name": "canary_probe", "description": description})

    def test_put_round_trip_from_legacy_and_quoted_drafts(self) -> None:
        description = 'Probe: say "go" for Spec #116'
        for index, line in enumerate(
            (description, skill_frontmatter.emit_scalar(description))
        ):
            with self.subTest(line=line):
                name = f"round_trip_{index}"
                spec = skill_mod.parse_local_skill_spec(draft(name, line))
                self.assertEqual(spec["description"], description)
                rendered = self.render(name, spec["description"])[0]
                # The mirror is itself a valid draft that stores the same row.
                again = skill_mod.parse_local_skill_spec(rendered)
                self.assertEqual(again["description"], description)
                self.assertEqual(again["content"], "procedure body")
                if yaml is not None:
                    front = yaml.safe_load(rendered.split("---\n")[1])
                    self.assertEqual(front["description"], description)

    def test_asset_reader_accepts_both_forms(self) -> None:
        description = "Seeded: for Spec #116"
        with tempfile.TemporaryDirectory() as tmp:
            for index, line in enumerate(
                (description, skill_frontmatter.emit_scalar(description))
            ):
                path = Path(tmp) / f"asset_{index}.md"
                path.write_text(draft(f"asset_{index}", line))
                self.assertEqual(
                    seed_skills.parse_skill(path)["description"], description)

    def test_multi_line_description_is_refused(self) -> None:
        with self.assertRaises(skill_mod.DraftValidationError):
            skill_mod.parse_local_skill_spec(
                draft("multi_line", '"first\\nsecond"'))


if __name__ == "__main__":
    unittest.main()
