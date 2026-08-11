# ruff: noqa: I001
"""Sentinel-managed ``.gitignore`` ownership and legacy adoption coverage."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"))
import install
import remove as remove_mod


ISSUE_1090_LEGACY_BLOCK = """# super-coder — rebuilt/derived; never commit
# The engine is a materialized, gitignored DEPENDENCY (B7) — fetched from
# upstream, refreshed by `./sc update`, never committed to the fork. Your project
# is everything ELSE in this repo. The one fork-owned artifact that must survive,
# the DB serialization, lives in the tracked .sc-state/ below.
/.super-coder/
# Boot artifacts + per-shell skill render — rebuilt at launch from the DB.
/CLAUDE.md
/AGENTS.md
/opencode.json
/.claude/skills/
# Engine-managed harness config re-emitted each launch (per-harness branch-guard
# hook); kept apart from a fork's own tracked config (claude settings.json /
# codex config.toml).
/.claude/settings.local.json
/.codex/hooks.json
# Shell worktrees — one per shell, linked inside the repo root.
/.sc-worktrees/
# .sc-state/ is TRACKED (content.sql + engine.ref). Only ephemeral/derived
# state is ignored: the pre-update pointer, map cache, and local DB backups.
/.sc-state/engine.ref.prev
# Opt-out artifact mode stores snapshots, map authorship, and flat renders here.
# Tracked mode remains the default for downstream forks.
/.sc-state/local/
# Map DB — derived cache of the repo (dr_*), rebuilt by `./sc map`. Its authored
# layer (sections) is tracked in .sc-state/map_content.sql.
/.sc-state/map.db
/.sc-state/map.db-wal
/.sc-state/map.db-shm
/.sc-state/db_backups/
"""

FIRST_INSTALL_LEGACY_BLOCK = """# super-coder — rebuilt/derived; never commit
/.super-coder/shell_db.db
/.super-coder/shell_db.db-wal
/.super-coder/shell_db.db-shm
/.super-coder/instance.json
/CLAUDE.md
/AGENTS.md
/.claude/skills/
"""


class EnsureGitignoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gitignore = self.root / ".gitignore"

    def test_fresh_install_writes_one_canonical_range(self) -> None:
        self.assertTrue(install.ensure_gitignore(self.root))
        self.assertEqual(self.gitignore.read_text(), install._GITIGNORE_BLOCK)
        self.assertEqual(self.gitignore.read_text().count(install._GITIGNORE_BEGIN), 1)
        self.assertEqual(self.gitignore.read_text().count(install._GITIGNORE_END), 1)
        self.assertFalse(install.ensure_gitignore(self.root))

    def test_existing_range_is_replaced_without_host_drift(self) -> None:
        prefix = "vendor/\r\n# host prefix\r\n"
        suffix = "# host suffix\r\n/.super-coder/\r\n"
        stale = (
            f"{install._GITIGNORE_BEGIN}\n/.super-coder/\n"
            f"{install._GITIGNORE_END}\n"
        )
        self.gitignore.write_bytes((prefix + stale + suffix).encode())

        self.assertTrue(install.ensure_gitignore(self.root))
        updated = self.gitignore.read_bytes().decode()
        self.assertEqual(updated, prefix + install._GITIGNORE_BLOCK + suffix)
        self.assertEqual(updated.count("/.super-coder/"), 2)
        self.assertFalse(install.ensure_gitignore(self.root))

    def test_current_pre_sentinel_block_and_topup_are_adopted(self) -> None:
        legacy = install._GITIGNORE_BLOCK.replace(
            install._GITIGNORE_BEGIN, install._LEGACY_GITIGNORE_MARKER, 1
        ).replace(f"{install._GITIGNORE_END}\n", "")
        legacy = legacy.replace("/.agents/skills/\n", "")
        before = "node_modules/\n"
        after = "# host suffix\n*.host\n"
        self.gitignore.write_text(
            before
            + legacy
            + install._LEGACY_GITIGNORE_TOPUP
            + "\n/.agents/skills/\n"
            + after
        )

        self.assertTrue(install.ensure_gitignore(self.root))
        updated = self.gitignore.read_text()
        self.assertEqual(updated, before + install._GITIGNORE_BLOCK + after)
        self.assertNotIn(install._LEGACY_GITIGNORE_MARKER, updated)
        self.assertNotIn(install._LEGACY_GITIGNORE_TOPUP, updated)

    def test_first_shipped_legacy_block_is_adopted(self) -> None:
        before = "node_modules/\n"
        after = "# host suffix\n*.host\n"
        self.gitignore.write_text(before + FIRST_INSTALL_LEGACY_BLOCK + after)

        self.assertTrue(install.ensure_gitignore(self.root))
        self.assertEqual(
            self.gitignore.read_text(),
            before + install._GITIGNORE_BLOCK + after,
        )
        for old_pattern in install._LEGACY_GITIGNORE_PATTERNS:
            self.assertNotIn(old_pattern, self.gitignore.read_text())

    def test_issue_1090_multiline_block_is_adopted_without_orphans(self) -> None:
        prefix = "*.host\n"
        suffix = "# host duplicate follows\n/.super-coder/\n"
        self.gitignore.write_text(prefix + ISSUE_1090_LEGACY_BLOCK + suffix)

        self.assertTrue(install.ensure_gitignore(self.root))
        updated = self.gitignore.read_text()
        self.assertEqual(updated, prefix + install._GITIGNORE_BLOCK + suffix)
        self.assertNotIn("# the DB serialization", updated)
        self.assertNotIn("# Tracked mode remains", updated)
        self.assertEqual(updated.count("/.super-coder/"), 2)

    def test_issue_1090_multiline_block_is_removed_without_orphans(self) -> None:
        prefix = "*.host\n"
        suffix = "# host duplicate follows\n/.super-coder/\n"
        self.gitignore.write_text(prefix + ISSUE_1090_LEGACY_BLOCK + suffix)

        self.assertTrue(remove_mod.cleanup_gitignore(self.root))
        removed = self.gitignore.read_text()
        self.assertEqual(
            removed,
            prefix
            + suffix
            + remove_mod.BACKUP_IGNORE_COMMENT
            + "\n"
            + remove_mod.BACKUP_IGNORE
            + "\n",
        )
        self.assertNotIn("# the DB serialization", removed)
        self.assertNotIn("# Tracked mode remains", removed)
        self.assertEqual(removed.count("/.super-coder/"), 1)

    def test_interleaved_host_rule_aborts_legacy_adoption_unchanged(self) -> None:
        original = (
            "host-prefix/\n"
            f"{install._LEGACY_GITIGNORE_MARKER}\n"
            "/.super-coder/\n"
            "# host inserted this\n"
            "private-cache/\n"
            f"{install._LEGACY_GITIGNORE_TOPUP}\n"
            "/.agents/skills/\n"
            "host-suffix/\n"
        )
        self.gitignore.write_text(original)

        with self.assertRaisesRegex(
            install.GitignoreError,
            r"^ambiguous legacy subfloor ignore range: line 5: private-cache/$",
        ):
            install.ensure_gitignore(self.root)
        self.assertEqual(self.gitignore.read_text(), original)

    def test_interleaved_host_rules_without_topup_are_all_reported(self) -> None:
        original = (
            f"{install._LEGACY_GITIGNORE_MARKER}\n"
            "/.super-coder/\n"
            "private-cache/\n"
            "# host comment\n"
            "secret.env\n"
            "/CLAUDE.md\n"
            "host-suffix/\n"
        )
        self.gitignore.write_text(original)

        with self.assertRaisesRegex(
            install.GitignoreError,
            (
                r"^ambiguous legacy subfloor ignore range: "
                r"line 3: private-cache/; line 5: secret.env$"
            ),
        ):
            install.ensure_gitignore(self.root)
        self.assertEqual(self.gitignore.read_text(), original)

    def test_malformed_sentinels_abort_unchanged_with_exact_lines(self) -> None:
        cases = (
            (
                f"host\n{install._GITIGNORE_BEGIN}\n/.super-coder/\n",
                (
                    "malformed subfloor managed ignore sentinels: "
                    "begin lines 2; end lines none; expected exactly one ordered pair"
                ),
            ),
            (
                (
                    f"{install._GITIGNORE_BEGIN}\n{install._GITIGNORE_BEGIN}\n"
                    f"{install._GITIGNORE_END}\n"
                ),
                (
                    "malformed subfloor managed ignore sentinels: "
                    "begin lines 1, 2; end lines 3; expected exactly one ordered pair"
                ),
            ),
            (
                (
                    f"{install._GITIGNORE_END}\n/.super-coder/\n"
                    f"{install._GITIGNORE_BEGIN}\n"
                ),
                (
                    "malformed subfloor managed ignore sentinels: "
                    "begin line 3 follows end line 1"
                ),
            ),
        )
        for original, message in cases:
            with self.subTest(message=message):
                self.gitignore.write_text(original)
                with self.assertRaisesRegex(install.GitignoreError, f"^{message}$"):
                    install.ensure_gitignore(self.root)
                self.assertEqual(self.gitignore.read_text(), original)

    def test_installer_rejects_malformed_range_before_lifecycle_work(self) -> None:
        original = f"{install._GITIGNORE_BEGIN}\n/.super-coder/\n"
        self.gitignore.write_text(original)

        with (
            mock.patch.object(install, "REPO_ROOT", self.root),
            mock.patch.object(install.platform, "system", return_value="Linux"),
            mock.patch.object(install, "is_source_repo", return_value=False),
            mock.patch.object(install, "already_installed", return_value=False),
            mock.patch.object(install, "report_docker") as report_docker,
            mock.patch.object(install, "ensure_harnesses") as ensure_harnesses,
            self.assertRaises(SystemExit) as raised,
        ):
            install.main([])

        self.assertEqual(
            str(raised.exception),
            "install: malformed subfloor managed ignore sentinels: "
            "begin lines 1; end lines none; expected exactly one ordered pair",
        )
        self.assertEqual(self.gitignore.read_text(), original)
        report_docker.assert_not_called()
        ensure_harnesses.assert_not_called()


if __name__ == "__main__":
    unittest.main()
