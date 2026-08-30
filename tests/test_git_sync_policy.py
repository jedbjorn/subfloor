#!/usr/bin/env python3
"""Regress the disposable shell-base sync policy in boot + Git skill."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
BOOT = ENGINE / "templates" / "boot.md"
ASSET = ENGINE / "assets" / "skills" / "git" / "SKILL.md"
RESEED = ENGINE / "migrations" / "0243_role_aware_boot_contract.sql"

sys.path.insert(0, str(ENGINE / "scripts"))
import seed_skills  # noqa: E402


class GitSyncPolicyTest(unittest.TestCase):
    def test_boot_grants_only_exact_shell_base_discard_authority(self):
        body = " ".join(BOOT.read_text().split())
        self.assertIn("treat `shell/<shortname>` as a disposable base", body.lower())
        self.assertIn("durable coordination lives in the control plane", body.lower())
        self.assertIn("code lives on a pushed branch with a pr", body.lower())
        self.assertIn("`git status --short` is empty", body)
        self.assertIn("`HEAD` equals `origin/main`", body)
        self.assertIn(
            "NEVER to a feature branch or open PR",
            body,
        )
        self.assertIn("surface a target/identity mismatch", body)

    def test_git_skill_makes_the_base_reset_executable_and_bounded(self):
        body = " ".join(ASSET.read_text().split())
        self.assertIn(
            "Compare `git rev-parse --show-toplevel` + "
            "`git branch --show-current` with ACTIVE SESSION",
            body,
        )
        self.assertIn(
            "`git reset --hard origin/main && git clean -fd`", body
        )
        self.assertIn("without asking", body)
        self.assertIn("a pushed remote branch with a PR", body)
        self.assertIn("NEVER reset or clean a feature branch / open PR", body)
        self.assertIn(
            "`git rev-parse HEAD` equals `git rev-parse origin/main`", body
        )

    def test_git_reseed_is_exact_idempotent_and_preserves_local_skills(self):
        with sqlite3.connect(":memory:") as con:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
                "CREATE TABLE flavor_skills ("
                "flavor TEXT, skill_id INTEGER, PRIMARY KEY(flavor,skill_id));"
                "INSERT INTO skills VALUES "
                "(1,'git','stale','stale','stale',1,'stale',1);"
                "INSERT INTO skills VALUES "
                "(2,'fork_only','local','fork',NULL,0,'bespoke body',0);"
            )
            migration = RESEED.read_text()
            con.executescript(migration)
            con.executescript(migration)

            parsed = seed_skills.parse_skill(ASSET)
            actual = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='git'"
            ).fetchone()
            self.assertEqual(
                (
                    parsed["description"],
                    parsed["category"],
                    parsed["command"],
                    parsed["common"],
                    parsed["content"],
                    0,
                ),
                actual,
            )
            self.assertEqual(
                ("fork", "bespoke body", 0),
                con.execute(
                    "SELECT category,content,is_deleted FROM skills "
                    "WHERE name='fork_only'"
                ).fetchone(),
            )


if __name__ == "__main__":
    unittest.main()
