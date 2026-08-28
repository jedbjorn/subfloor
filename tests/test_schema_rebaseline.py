"""DSH-free schema rebaseline convergence proofs (spec #178 task #684)."""
from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import migrate  # noqa: E402
import test_dsh_removal_preparation as preparation  # noqa: E402

PURGE = ROOT / ".super-coder/migrations/0237_purge_dsh_owned_data.sql"
REBASELINE = ROOT / ".super-coder/migrations/0238_final_schema_rebaseline.sql"
PRUNED = {
    "0227_deepseek_controlled_route_binding.sql",
    "0230_deepseek_stock_host_route_binding.sql",
    "0235_live_native_route_binding_v3.sql",
    "0236_live_native_conversation_routes.sql",
}


def schema(con: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]


def forbidden_schema(con: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in con.execute(
            "SELECT type,name FROM sqlite_master "
            "WHERE instr(lower(COALESCE(sql,'')),'deepseek')>0 "
            "OR instr(lower(COALESCE(sql,'')),'dsh')>0 "
            "ORDER BY type,name"
        )
    ]


def typed_owner_counts(con: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        con.execute(query).fetchone()[0]
        for query in (
            "SELECT COUNT(*) FROM flavor_defaults WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM model_routes WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM conversations WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM sprint_participants WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM sprint_participant_route_bindings "
            "WHERE harness='deepseek'",
        )
    )


def stamp_frozen_ledger(con: sqlite3.Connection) -> None:
    manifest = preparation.load(preparation.MANIFEST_PATH)
    con.executemany(
        "INSERT OR IGNORE INTO schema_migrations (filename) VALUES (?)",
        [(row["filename"],) for row in manifest["immutable_migration_ledger"]],
    )
    con.commit()


class SchemaRebaselineTest(unittest.TestCase):
    def fresh_database(self, directory: Path) -> sqlite3.Connection:
        path = directory / "fresh.db"
        with closing(sqlite3.connect(path)) as con:
            con.executescript((ROOT / ".super-coder/schema.sql").read_text())
            con.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            migrate.migrate(str(path), fresh_build=True)
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con

    def upgraded_database(self) -> sqlite3.Connection:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        con = preparation.replay_database(fixture)
        stamp_frozen_ledger(con)
        migrate.apply(con, PURGE, dsh_purge_authorized=True)
        migrate.apply(con, REBASELINE, rebaseline_authorized=True)
        return con

    def test_fresh_and_upgraded_floors_converge_without_retired_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw, closing(
            self.fresh_database(Path(raw))
        ) as fresh, closing(self.upgraded_database()) as upgraded:
            self.assertEqual(schema(fresh), schema(upgraded))
            self.assertEqual([], forbidden_schema(fresh))
            self.assertEqual([], forbidden_schema(upgraded))
            self.assertEqual((0, 0, 0, 0, 0), typed_owner_counts(fresh))
            self.assertEqual(typed_owner_counts(fresh), typed_owner_counts(upgraded))
            self.assertEqual([], fresh.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual([], upgraded.execute("PRAGMA foreign_key_check").fetchall())

            fresh_ledger = {
                row[0] for row in fresh.execute("SELECT filename FROM schema_migrations")
            }
            upgraded_ledger = {
                row[0]
                for row in upgraded.execute("SELECT filename FROM schema_migrations")
            }
            self.assertIn(REBASELINE.name, fresh_ledger)
            self.assertIn(REBASELINE.name, upgraded_ledger)
            self.assertTrue(PRUNED.isdisjoint(fresh_ledger))
            self.assertTrue(PRUNED.issubset(upgraded_ledger))

    def test_rebaseline_preserves_retained_rows_and_rejects_retired_route(self) -> None:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        with closing(preparation.replay_database(fixture)) as con:
            migrate.apply(con, PURGE, dsh_purge_authorized=True)
            queries = {
                "defaults": (
                    "SELECT * FROM flavor_defaults WHERE harness='opencode' "
                    "ORDER BY flavor"
                ),
                "conversations": (
                    "SELECT * FROM conversations WHERE harness='opencode' "
                    "ORDER BY conversation_id"
                ),
                "bindings": (
                    "SELECT * FROM sprint_participant_route_bindings "
                    "WHERE harness='opencode' ORDER BY binding_id"
                ),
            }
            retained_before = {
                name: [tuple(row) for row in con.execute(query)]
                for name, query in queries.items()
            }
            self.assertEqual(
                {"defaults": 1, "conversations": 1, "bindings": 2},
                {name: len(rows) for name, rows in retained_before.items()},
            )
            option_binding_before = tuple(
                con.execute(
                    "SELECT requested_effort,effective_effort,native_option_id,"
                    "binding_json,binding_digest "
                    "FROM sprint_participant_route_bindings WHERE binding_id=9003"
                ).fetchone()
            )
            self.assertEqual(
                (
                    "high",
                    "high",
                    "high",
                    '{"contract_version":3,"control_state":"controlled",'
                    '"harness":"opencode","native_option_id":"high",'
                    '"provider_model":"deepseek-v4-pro",'
                    '"requested_model":"ollama-cloud/deepseek-v4-pro",'
                    '"transport":"opencode-route-agent"}',
                    "2a050759549e44f9f5bf8170834ed51f2733134662f265a9aae36859182c6f9d",
                ),
                option_binding_before,
            )

            migrate.apply(con, REBASELINE, rebaseline_authorized=True)

            retained_after = {
                name: [tuple(row) for row in con.execute(query)]
                for name, query in queries.items()
            }
            self.assertEqual(retained_before, retained_after)
            self.assertEqual(
                option_binding_before,
                tuple(
                    con.execute(
                        "SELECT requested_effort,effective_effort,native_option_id,"
                        "binding_json,binding_digest "
                        "FROM sprint_participant_route_bindings "
                        "WHERE binding_id=9003"
                    ).fetchone()
                ),
            )

            columns = [
                row[1]
                for row in con.execute(
                    "PRAGMA table_info(sprint_participant_route_bindings)"
                )
            ]
            select = ",".join(
                "?" if name == "binding_id" else
                "?" if name == "route_revision" else
                "?" if name == "harness" else
                f'"{name}"'
                for name in columns
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO sprint_participant_route_bindings ("
                    + ",".join(f'"{name}"' for name in columns)
                    + ") SELECT "
                    + select
                    + " FROM sprint_participant_route_bindings WHERE binding_id=?",
                    (9901, 99, "deepseek", retained_after["bindings"][0][0]),
                )
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM sprint_participant_route_bindings "
                    "WHERE binding_id=9901 OR harness='deepseek'"
                ).fetchone()[0],
            )

    def test_live_chain_prunes_the_four_frozen_reference_migrations(self) -> None:
        live = {
            path.name
            for path in (ROOT / ".super-coder/migrations").glob("*.sql")
        }
        historical = {
            path.name
            for path in preparation.FIXTURES.HISTORICAL_MIGRATIONS.glob("*.sql")
        }
        self.assertTrue(PRUNED.isdisjoint(live))
        self.assertEqual(PRUNED, historical)
        self.assertNotIn("deepseek", REBASELINE.read_text().lower())
        self.assertNotIn("dsh", REBASELINE.read_text().lower())


if __name__ == "__main__":
    unittest.main()
