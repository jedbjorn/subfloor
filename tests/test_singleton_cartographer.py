#!/usr/bin/env python3
"""Tests for the cartographer singleton guard.

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and
test_shell_messaging.py. Each test builds a throwaway DB the way the engine
ships it (schema.sql + every migration in filename order), then exercises the
guard: the DB-layer trigger (trg_singleton_cartographer) and the friendly
shell_factory pre-check.

Run:
    python3 tests/test_singleton_cartographer.py
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

INSERT_SHELL = (
    "INSERT INTO shells (display_name, system_prompt, flavor) VALUES (?, 'x', ?)"
)


def build_db() -> sqlite3.Connection:
    """Fresh in-memory DB: schema.sql + every migration, FK enforcement on."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for path in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(path.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


class SingletonCartographerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()

    def tearDown(self) -> None:
        self.con.close()

    # ── trigger shape ─────────────────────────────────────────────────────────
    def test_trigger_exists(self) -> None:
        t = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_singleton_cartographer'"
        ).fetchone()
        self.assertIsNotNone(t, "singleton trigger missing after schema+migrations")

    # ── first ok, second blocked ──────────────────────────────────────────────
    def test_second_cartographer_rejected(self) -> None:
        self.con.execute(INSERT_SHELL, ("Cartographer", "cartographer"))
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(INSERT_SHELL, ("Cartographer 2", "cartographer"))

    # ── a deleted cartographer frees the slot ─────────────────────────────────
    def test_deleted_cartographer_frees_slot(self) -> None:
        self.con.execute(INSERT_SHELL, ("Cartographer", "cartographer"))
        self.con.commit()
        self.con.execute(
            "UPDATE shells SET is_deleted=1 WHERE flavor='cartographer'")
        self.con.commit()
        # No longer counts → a replacement is allowed.
        self.con.execute(INSERT_SHELL, ("Cartographer 2", "cartographer"))
        self.con.commit()
        n = self.con.execute(
            "SELECT COUNT(*) FROM shells WHERE flavor='cartographer' AND is_deleted=0"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    # ── other flavors are unconstrained ───────────────────────────────────────
    def test_other_flavors_unconstrained(self) -> None:
        for n in range(3):
            self.con.execute(INSERT_SHELL, (f"Dev {n}", "dev"))
            self.con.execute(INSERT_SHELL, (f"Review {n}", "review"))
        self.con.commit()
        devs = self.con.execute(
            "SELECT COUNT(*) FROM shells WHERE flavor='dev'").fetchone()[0]
        self.assertEqual(devs, 3)

    # ── factory pre-check raises a friendly ValueError ────────────────────────
    def test_factory_pre_check(self) -> None:
        import sys
        sys.path.insert(0, str(ENGINE / "scripts"))
        import shell_factory  # noqa: E402

        # Seed the incumbent directly (avoids running the full first-creation).
        self.con.execute(INSERT_SHELL, ("Cartographer", "cartographer"))
        self.con.commit()
        with self.assertRaises(ValueError):
            shell_factory.create_shell(
                self.con, flavor="cartographer", name="Cartographer 2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
