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
RESEED = ENGINE / "migrations" / "0264_reseed_winbox_adoption_skills.sql"
README = ROOT / "docs" / "README.md"
REMOTE_SEATS_DOC = ENGINE / "docs" / "remote-seats.md"
TAILNET_DOC = ENGINE / "docs" / "tailscale-broker.md"
RETIRED_DOCS = (
    ENGINE / "docs" / "windows-test-vm.md",
    ENGINE / "docs" / "windows-vm-broker.md",
    ENGINE / "docs" / "skills",
)
NEW_SKILLS = ("remote_seats", "tailscale_diagnostics", "windows_testing")
# The trailing reseed carries only the skills that spec #232 rewrote;
# tailscale_diagnostics was last reseeded by 0263 and is unchanged.
RESEED_SKILLS = ("remote_seats", "windows_testing")
RETIRED_GUIDANCE = re.compile(
    r"lease|forcecommand|sc vm test|acquire|release --force|baseline promote"
    r"|windows-test-client|windows_test_controller",
    re.IGNORECASE,
)

sys.path.insert(0, str(ENGINE / "scripts"))
import feature
import seed_skills
import services
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

    def test_remote_seats_carries_the_adoption_verbs_and_error_codes(self) -> None:
        """Spec #232: the posture-1 verb list and structured errors grew."""
        body = _flat(_specs()["remote_seats"]["content"])
        for phrase in (
            "./sc vm adopt", "bake", "./sc vm pull", "--running",
            "snapshot_live_unsupported", "remote_path_not_allowed",
            "adopt_guest_not_found", "adopt_ssh_timeout",
            "adopt_key_install_failed", "adopt_provision_failed",
            "adopt_verify_failed", "adopt_sandboxed",
            "decision #372", "decision #101",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_windows_testing_carries_adoption_authority_and_mcp_recovery(self) -> None:
        """Spec #232: two-command adoption, winbox.json, authority, MCP repair."""
        body = _flat(_specs()["windows_testing"]["content"])
        for phrase in (
            "./sc vm adopt", "bootstrap.ps1", ".subfloor/winbox.json",
            "winget_manifest", "mcp_port", "C:\\SubfloorTest",
            "## Authority", "decision #372", "decision #353",
            "windows-mcp-server",
            "windows-mcp install --transport streamable-http",
            "./sc vm bake", "--command-file",
            "[Convert]::ToBase64String(...)",
            # The pin lives in .sc-state (callable_floor.read_engine_ref).
            "`.sc-state/engine.ref`",
            # A LOGIN task: no desktop session means no listener, and that is
            # a warning on a fully adopted guest, not a broken one.
            "LOGIN task",
            # Guest paths take either separator; the key never reaches a shell.
            "C:/SubfloorTest/out.bin",
            "generated on the host and never handed to a shell",
            # A declared check is a cmd.exe command line.
            "must not contain a double quote",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)
        for retired in (
            "the engine installs nothing",
            "never install or reconfigure a guest",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, body.lower())


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
        specs = _specs()
        for name in RESEED_SKILLS:
            spec = specs[name]
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
        specs = _specs()
        for migration, names in ((SEED, NEW_SKILLS), (RESEED, RESEED_SKILLS)):
            con = sqlite3.connect(":memory:")
            con.executescript(SKILL_SCHEMA + migration.read_text())
            for name in names:
                spec = specs[name]
                with self.subTest(migration=migration.name, skill=name):
                    row = con.execute(
                        "SELECT content, common FROM skills WHERE name=?", (name,)
                    ).fetchone()
                    self.assertEqual(row, (spec["content"], 0))
            con.close()


# Every code the doc and the skill table must both carry. A code documented in
# one place and not the other is how a shell ends up guessing.
ADOPT_AND_TRANSFER_CODES = (
    "adopt_sandboxed", "adopt_guest_not_found", "adopt_ssh_timeout",
    "adopt_key_install_failed", "adopt_harden_failed", "adopt_host_key_changed",
    "adopt_provision_failed", "adopt_verify_failed", "adopt_config_invalid",
    "adopt_block_write_failed", "adopt_broker_failed", "adopt_baseline_failed",
    "push_failed", "pull_failed", "pull_source_invalid",
    "pull_destination_invalid", "push_source_invalid", "scp_unsupported",
    "bake_config_write_failed", "<operation>_timeout",
)


class ErrorVocabularyTests(unittest.TestCase):
    """Spec #232: the runbook and the shell-facing skill agree, and every code
    they name is one the engine actually emits."""

    def test_runbook_and_skill_document_the_same_codes(self) -> None:
        doc = _flat(REMOTE_SEATS_DOC.read_text())
        skill = _flat(_specs()["remote_seats"]["content"])
        for code in ADOPT_AND_TRANSFER_CODES:
            with self.subTest(code=code):
                self.assertIn(code, doc, "missing from docs/remote-seats.md")
                self.assertIn(code, skill, "missing from the remote_seats skill")

    def test_every_documented_code_is_emitted_by_the_engine(self) -> None:
        sources = "\n".join(
            (ENGINE / "scripts" / name).read_text()
            for name in ("vm.py", "vm_adopt.py")
        )
        for code in ADOPT_AND_TRANSFER_CODES:
            if code == "<operation>_timeout":
                # Generated per verb, so it is a format string in the source.
                self.assertIn('f"{operation}_timeout"', sources)
                continue
            with self.subTest(code=code):
                self.assertIn(f'"{code}"', sources)

    def test_the_broker_resume_line_says_the_block_is_written(self) -> None:
        """A shell that reads `adopt_broker_failed` must not re-run adoption
        believing nothing was saved."""
        for body in (
            _flat(REMOTE_SEATS_DOC.read_text()),
            _flat(_specs()["remote_seats"]["content"]),
        ):
            self.assertIn("./sc vm-broker-up", body)
            self.assertRegex(body, r"block IS written")


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

    def test_remote_seats_runbook_documents_two_command_adoption(self) -> None:
        """Spec #232: onboarding, routes, block fields, limits, error codes."""
        body = _flat(REMOTE_SEATS_DOC.read_text())
        for needed in (
            "./sc vm adopt", "bootstrap.ps1", "--bootstrap-url",
            # The engine pin lives in .sc-state, not under .super-coder:
            # callable_floor.read_engine_ref is the authority.
            "`.sc-state/engine.ref`", "`main`",
            "`/bake` `{name?}`", "`/pull` `{src, dest}`",
            "`/push` `{src, dest?}`", "`{snapshot?, running}`",
            "`/snapshot/create` `{name}`", "`/snapshot/delete` `{name}`",
            "`/validate/<check>` `{vm}`",
            "`workspace`", "`known_hosts_path`", "`ssh_port`", "`mcp_port`",
            "./sc vm-bake", "snapshot_live_unsupported",
            "remote_path_not_allowed", "adopt_guest_not_found",
            "adopt_ssh_timeout", "adopt_key_install_failed",
            "adopt_provision_failed", "adopt_verify_failed", "adopt_sandboxed",
            "booted, licensed Windows 10 or 11 guest",
            # Every flag `vm adopt` accepts, on the client-verb line.
            "--libvirt-uri", "--wait SECONDS", "--json",
            # The host floor push/pull depend on.
            "OpenSSH client 8.7 or newer",
        ):
            with self.subTest(needed=needed):
                self.assertIn(needed, body)
        self.assertNotIn("the engine installs nothing", body)
        # The one surviving mention is the retirement note on `vm init`.
        self.assertEqual(body.count("--transfer-dir"), 1)
        # `vm-bake` is the host-direct escape hatch, not a plain alias.
        self.assertIn("HOST-DIRECT", body)
        self.assertNotIn("stays as a dispatcher alias", body)

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
        self.assertIn("./sc vm adopt", windows)
        self.assertIn("./sc vm init", windows)
        self.assertIn("./sc remote add", windows)
        self.assertNotIn("--transfer-dir", windows)
        self.assertIn("remote-seats.md", windows)
        self.assertIn("./sc ts init", tailnet)
        self.assertIn("--readonly-host", tailnet)
        self.assertNotIn("hand-fill", windows + tailnet)

    def test_vm_broker_onboarding_names_two_command_adoption(self) -> None:
        onboarding = services.SERVICES["vm"]["onboarding"]
        self.assertIn("./sc vm adopt --domain <domain> --ssh-user <account>",
                      onboarding)
        self.assertIn("remote-seats.md", onboarding)
        self.assertNotIn("--transfer-dir", onboarding)

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
