"""System-managed runtime advisory persistence and projection contracts."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import runtime_flags
import server


def build_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for path in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(path.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def advisory(detail: str = "apt candidate failed") -> dict:
    return {
        "checkout_identity": "0" * 64,
        "source_commit": "a" * 40,
        "source_tracked_clean": True,
        "declaration_digest": "b" * 64,
        "package_digest": "c" * 64,
        "failing_atoms": ["curl=8.0"],
        "classification": "native_package_candidate",
        "detail": detail,
        "image_ids": {
            "parent": "sha256:" + "1" * 64,
            "engine_base": "sha256:" + "2" * 64,
        },
        "evidence_path": ".sc-state/local/dev-kit/status.json",
        "core_runtime": "ready",
        "native_packages": "advisory",
        "fork_readiness": "degraded",
        "selected_runtime": "engine_baseline",
        "cutover": "baseline_fallback",
        "remedy": runtime_flags.REMEDY,
    }


def open_request(generation: int, evidence: dict | None = None) -> dict:
    value = advisory() if evidence is None else evidence
    return {
        "state": "open",
        "source_kind": runtime_flags.SOURCE_KIND,
        "generation": generation,
        "evidence_digest": runtime_flags.canonical_digest(value),
        "advisory": value,
    }


def clearance(failed_generation: int) -> dict:
    return {
        "clearance_kind": "current_contract",
        "source_commit": "d" * 40,
        "source_tracked_clean": True,
        "failed_generation": failed_generation,
        "old_declaration_digest": "b" * 64,
        "current_declaration_digest": "e" * 64,
        "baseline_id": "sha256:" + "2" * 64,
        "extension_id": "none",
        "package_layer_id": "sha256:" + "3" * 64,
        "requested": ["curl=8.0"],
        "observed": [
            {
                "name": "curl",
                "architecture": "amd64",
                "version": "8.0",
                "status": "install ok installed",
            }
        ],
        "proof_digest": "f" * 64,
        "package_receipt": ".sc-state/local/dev-kit/ready.json",
        "evidence": ".sc-state/local/dev-kit/status.json",
        "cutover_owed": False,
    }


def resolved_request(generation: int, failed_generation: int) -> dict:
    value = clearance(failed_generation)
    return {
        "state": "resolved",
        "source_kind": runtime_flags.SOURCE_KIND,
        "generation": generation,
        "evidence_digest": runtime_flags.canonical_digest(value),
        "clearance": value,
    }


class RuntimeFlagStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.source = "9" * 64

    def tearDown(self) -> None:
        self.con.close()

    def test_generation_lifecycle_is_idempotent_ordered_and_tombstoned(self):
        status, created = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(1)
        )
        self.assertEqual(status, 201)
        flag_id = created["flag_id"]
        self.assertTrue(created["created"])

        status, repeated = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(1)
        )
        self.assertEqual((status, repeated["flag_id"], repeated["idempotent"]), (200, flag_id, True))

        changed = advisory("pin unavailable in configured repositories")
        status, refreshed = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(2, changed)
        )
        self.assertEqual((status, refreshed["flag_id"], refreshed["created"]), (200, flag_id, False))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM flags").fetchone()[0], 1
        )

        status, stale = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(1)
        )
        self.assertEqual((status, stale["error"]["code"]), (409, "stale_generation"))
        conflict = advisory("different evidence at generation two")
        status, collision = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(2, conflict)
        )
        self.assertEqual((status, collision["error"]["code"]), (409, "generation_conflict"))

        status, resolved = runtime_flags.put_runtime_flag(
            self.con, self.source, resolved_request(3, 2)
        )
        self.assertEqual((status, resolved["state"]), (200, "resolved"))
        row = self.con.execute(
            "SELECT resolved,source_generation FROM flags WHERE flag_id=?", (flag_id,)
        ).fetchone()
        self.assertEqual(tuple(row), (1, 3))

        status, stale_reopen = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(2, changed)
        )
        self.assertEqual((status, stale_reopen["error"]["code"]), (409, "stale_generation"))

        later = advisory("a later independent failure")
        status, reopened = runtime_flags.put_runtime_flag(
            self.con, self.source, open_request(4, later)
        )
        self.assertEqual((status, reopened["created"]), (201, True))
        self.assertNotEqual(reopened["flag_id"], flag_id)
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM flags WHERE source_key=? AND resolved=0",
                (self.source,),
            ).fetchone()[0],
            1,
        )

    def test_clearance_requires_the_current_failure_and_complete_proof(self):
        runtime_flags.put_runtime_flag(self.con, self.source, open_request(5))
        status, mismatch = runtime_flags.put_runtime_flag(
            self.con, self.source, resolved_request(6, 4)
        )
        self.assertEqual(
            (status, mismatch["error"]["code"]),
            (409, "clearance_generation_mismatch"),
        )

        incomplete = clearance(5)
        incomplete["proof_digest"] = "none"
        request = {
            "state": "resolved",
            "source_kind": runtime_flags.SOURCE_KIND,
            "generation": 6,
            "evidence_digest": runtime_flags.canonical_digest(incomplete),
            "clearance": incomplete,
        }
        status, invalid = runtime_flags.put_runtime_flag(self.con, self.source, request)
        self.assertEqual((status, invalid["error"]["code"]), (422, "validation_error"))
        self.assertEqual(
            self.con.execute("SELECT resolved FROM flags").fetchone()[0], 0
        )

    def test_no_longer_applicable_clearance_requires_explicit_package_none(self):
        runtime_flags.put_runtime_flag(self.con, self.source, open_request(1))
        value = clearance(1)
        value.update(
            {
                "clearance_kind": "packages_removed",
                "package_layer_id": "none",
                "requested": [],
                "observed": [],
                "proof_digest": "none",
                "package_receipt": "none",
            }
        )
        request = {
            "state": "resolved",
            "source_kind": runtime_flags.SOURCE_KIND,
            "generation": 2,
            "evidence_digest": runtime_flags.canonical_digest(value),
            "clearance": value,
        }
        status, result = runtime_flags.put_runtime_flag(self.con, self.source, request)
        self.assertEqual((status, result["state"]), (200, "resolved"))

    def test_unknown_fields_and_human_mutation_cannot_create_managed_rows(self):
        request = open_request(1)
        request["surprise"] = True
        status, invalid = runtime_flags.put_runtime_flag(self.con, self.source, request)
        self.assertEqual((status, invalid["error"]["code"]), (422, "validation_error"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM flags").fetchone()[0], 0)

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO flags (display_name,management_state) VALUES ('bad','system')"
            )

    def test_flags_projection_exposes_advisory_without_blocker_semantics(self):
        feature_id = self.con.execute(
            "INSERT INTO roadmap (title,roadmap_status,sort_order) VALUES ('F','next',1)"
        ).lastrowid
        self.con.execute(
            "INSERT INTO flags (display_name,feature_id,blocks_runtime) VALUES ('visible',?,1)",
            (feature_id,),
        )
        self.con.execute(
            "INSERT INTO flags (display_name,feature_id,blocks_runtime,blocking_scope) "
            "VALUES ('advisory-like',?,0,'none')",
            (feature_id,),
        )
        runtime_flags.put_runtime_flag(self.con, self.source, open_request(1))

        roadmap = server.get_roadmap(self.con)
        feature = next(
            feature
            for bucket in roadmap["buckets"]
            for feature in bucket["features"]
            if feature["feature_id"] == feature_id
        )
        self.assertEqual([flag["display_name"] for flag in feature["open_flags"]], ["visible"])

        with mock.patch.object(server.runtime_flags, "reconcile_pending", return_value=[]):
            projection = server.get_flags(self.con)
        managed = next(
            flag for flag in projection["flags"] if flag["management_state"] == "system"
        )
        self.assertEqual(managed["severity"], "advisory")
        self.assertEqual(managed["blocking_scope"], "none")
        self.assertEqual(managed["blocks_runtime"], 0)
        self.assertEqual(managed["evidence"]["native_packages"], "advisory")


if __name__ == "__main__":
    unittest.main()
