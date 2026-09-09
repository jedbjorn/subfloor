"""Remote-seat skills, docs, and onboarding hints ship together (feature #80,
spec #230 tasks #832-#833): three opt-in engine skills as assets plus a trailing
reseed migration, the replacement runbook, the revised tailnet runbook, and
feature link hints that name the three setup commands."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SKILLS = ENGINE / "assets" / "skills"
SEED = ENGINE / "migrations" / "0001_seed_skills.sql"
RESEED = ENGINE / "migrations" / "0263_reseed_remote_seat_skills.sql"
README = ROOT / "docs" / "README.md"
REMOTE_SEATS_DOC = ENGINE / "docs" / "remote-seats.md"
TAILNET_DOC = ENGINE / "docs" / "tailscale-broker.md"
RETIRED_DOCS = (
    ENGINE / "docs" / "windows-test-vm.md",
    ENGINE / "docs" / "windows-vm-broker.md",
    ENGINE / "docs" / "skills",
)
NEW_SKILLS = ("remote_seats", "tailscale_diagnostics", "windows_testing")
RETIRED_GUIDANCE = re.compile(
    r"lease|forcecommand|sc vm test|acquire|release --force|baseline promote"
    r"|windows-test-client|windows_test_controller",
    re.IGNORECASE,
)

sys.path.insert(0, str(ENGINE / "scripts"))
import feature
import seed_skills
import ts

SKILL_SCHEMA = (
    "CREATE TABLE skills ("
    "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
    "category TEXT, command TEXT, common INTEGER, content TEXT, "
    "is_deleted INTEGER DEFAULT 0);"
)


def _flat(text: str) -> str:
    """Prose wraps at 80 columns; phrase checks compare on one line."""
    return " ".join(text.split())


def _specs() -> dict[str, dict]:
    return {
        name: seed_skills.parse_skill(SKILLS / name / "SKILL.md")
        for name in NEW_SKILLS
    }


class SkillAssetTests(unittest.TestCase):
    def test_three_skills_are_opt_in_substrate_assets(self) -> None:
        for name, spec in _specs().items():
            with self.subTest(skill=name):
                self.assertEqual(spec["name"], name)
                self.assertEqual(spec["common"], 0)
                self.assertEqual(spec["category"], "substrate")
                self.assertTrue(spec["description"])

    def test_windows_testing_is_the_windows_only_add_on(self) -> None:
        body = _specs()["windows_testing"]["content"]
        self.assertIsNone(RETIRED_GUIDANCE.search(body), RETIRED_GUIDANCE.search(body))
        for kept in ("remote_seats", "powershell", "./sc vm capture", "./sc vm mcp"):
            self.assertIn(kept, body.lower() if kept.islower() else body)

    def test_tailscale_diagnostics_carries_the_readonly_doctrine(self) -> None:
        body = _specs()["tailscale_diagnostics"]["content"]
        doctrine = body[body.index("## Read-only doctrine\n") + len("## Read-only doctrine\n"):]
        doctrine = doctrine[: doctrine.index("\n## ")].strip()
        self.assertEqual(doctrine.count("\n\n"), 0, "one paragraph")
        for phrase in (
            "never reaches a read-only host with `tailscale` or `ssh` directly",
            "on bare metal or otherwise",
            "observation means observation",
            "decision #354",
        ):
            self.assertIn(phrase, _flat(doctrine))
        self.assertIn("readonly_refused", body)
        self.assertIn("Stop.", body)

    def test_tailscale_skill_table_names_every_readonly_verb(self) -> None:
        body = _specs()["tailscale_diagnostics"]["content"]
        for verb in sorted({prefix[0] for prefix in ts.READONLY_COMMANDS}):
            with self.subTest(verb=verb):
                self.assertIn(f"`{verb}`", body)
        for character in ts.READONLY_FORBIDDEN_CHARACTERS:
            if character.strip():
                self.assertIn(character, body)
        self.assertIn("`sudo`", body)

    def test_remote_seats_names_setup_commands_and_sharing_rule(self) -> None:
        body = _specs()["remote_seats"]["content"]
        for phrase in (
            "./sc vm init", "./sc remote add", "./sc ts init", "reset --off",
            "decision #353", "decision #355",
        ):
            self.assertIn(phrase, _flat(body))
        self.assertIsNone(RETIRED_GUIDANCE.search(body), RETIRED_GUIDANCE.search(body))


class ReseedMigrationTests(unittest.TestCase):
    def test_generated_seed_lists_the_three_skills(self) -> None:
        seeded = set(seed_skills.seeded_skill_names())
        for name in NEW_SKILLS:
            self.assertIn(name, seeded)

    def test_trailing_reseed_converges_stale_rows_and_preserves_local(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            SKILL_SCHEMA
            + "INSERT INTO skills "
            "(skill_id,name,description,category,command,common,content,is_deleted) "
            "VALUES "
            "(40,'windows_testing','controller','substrate',NULL,1,"
            "'sc vm test acquire --owner ...',0),"
            "(41,'remote_seats','old','fork',NULL,1,'stale',1),"
            "(99,'fork_windows_lab','local','fork',NULL,0,'bespoke body',0);"
        )
        migration = RESEED.read_text()
        con.executescript(migration)
        con.executescript(migration)

        rows = {
            row[0]: row[1:]
            for row in con.execute(
                "SELECT name,description,category,command,common,content,is_deleted "
                "FROM skills"
            )
        }
        for name, spec in _specs().items():
            with self.subTest(skill=name):
                self.assertEqual(
                    rows[name],
                    (
                        spec["description"], spec["category"], spec["command"],
                        0, spec["content"], 0,
                    ),
                )
        self.assertEqual(rows["fork_windows_lab"][4], "bespoke body")
        self.assertEqual(
            con.execute("SELECT skill_id FROM skills WHERE name='windows_testing'")
            .fetchone()[0],
            40,
            "UPSERT keeps the row identity so grants survive",
        )

    def test_seed_and_reseed_agree_with_assets(self) -> None:
        for migration in (SEED, RESEED):
            con = sqlite3.connect(":memory:")
            con.executescript(SKILL_SCHEMA + migration.read_text())
            for name, spec in _specs().items():
                with self.subTest(migration=migration.name, skill=name):
                    row = con.execute(
                        "SELECT content, common FROM skills WHERE name=?", (name,)
                    ).fetchone()
                    self.assertEqual(row, (spec["content"], 0))
            con.close()


class DocumentationTests(unittest.TestCase):
    def test_retired_docs_and_fork_import_copy_are_gone(self) -> None:
        for path in RETIRED_DOCS:
            with self.subTest(path=path.name):
                self.assertFalse(path.exists())

    def test_remote_seats_runbook_replaces_the_two_retired_docs(self) -> None:
        body = REMOTE_SEATS_DOC.read_text()
        for needed in (
            "/remote/<name>/status", "/remote/<name>/exec",
            "/remote/<name>/push", "/remote/<name>/pull",
            "./sc vm init", "./sc remote add", "./sc ts init",
            "known_hosts_path", "decision #353", "decision #355",
            "tailscale-broker.md", "decision #356",
        ):
            self.assertIn(needed, _flat(body))
        self.assertIsNone(
            re.search(r"forcecommand|windows-test-client|acquire", body, re.IGNORECASE)
        )

    def test_tailnet_runbook_describes_the_readonly_tier(self) -> None:
        body = TAILNET_DOC.read_text()
        for needed in (
            "readonly_hosts", "readonly_refused", "./sc ts status",
            "./sc ts exec", "./sc ts init", "READONLY_COMMANDS",
            "remote-seats.md", "decision #354",
        ):
            self.assertIn(needed, _flat(body))
        self.assertNotIn("windows-vm-broker.md", body)

    def test_public_readme_links_the_replacement_runbook(self) -> None:
        readme = README.read_text()
        self.assertIn(".super-coder/docs/remote-seats.md", readme)
        for name in NEW_SKILLS:
            self.assertIn(f"`{name}`", readme)
        self.assertNotIn("windows-test-vm.md", readme)
        self.assertNotIn("windows-vm-broker.md", readme)


class OnboardingTests(unittest.TestCase):
    def test_feature_hints_name_the_setup_commands(self) -> None:
        windows = "\n".join(feature.FEATURES["windows"]["link"])
        tailnet = "\n".join(feature.FEATURES["tailnet"]["link"])
        self.assertIn("./sc vm init", windows)
        self.assertIn("./sc remote add", windows)
        self.assertIn("remote-seats.md", windows)
        self.assertIn("./sc ts init", tailnet)
        self.assertIn("--readonly-host", tailnet)
        self.assertNotIn("hand-fill", windows + tailnet)

    def test_vm_test_is_an_unknown_verb(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ENGINE / "scripts" / "vm.py"), "client", "test", "status"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("invalid choice: 'test'", completed.stderr)


if __name__ == "__main__":
    unittest.main()
