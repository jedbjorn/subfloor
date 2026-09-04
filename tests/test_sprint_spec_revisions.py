#!/usr/bin/env python3
"""Immutable governing revision persistence, reads, and edit evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
REVISION_MIGRATION = MIGRATIONS / "0204_sprint_governing_revision_evidence.sql"
sys.path[:0] = [str(ENGINE / "api"), str(ENGINE / "scripts")]

import migrate
import server
import sprint_domain
import sprint_message_delivery


def apply_schema(con: sqlite3.Connection, *, through: str | None = None) -> None:
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if through is not None and migration.name > through:
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class GoverningRevisionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        apply_schema(self.con)
        self.con.execute(
            "INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)"
        )
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Planner", "PLN1", "planner", "prompt"),
                (2, "Developer", "DEV1", "dev", "prompt"),
                (3, "Reviewer", "REV1", "reviewer", "prompt"),
                (4, "Outsider", "DEV2", "dev", "prompt"),
            ),
        )
        self.feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Revision evidence','in_progress')"
            ).lastrowid
        )
        self.original = "# Governing bytes\n\nExact revision.\n"
        self.document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Governing spec',?)",
                (self.feature_id, self.original),
            ).lastrowid
        )
        revision = hashlib.sha256(self.original.encode()).hexdigest()
        self.approval_id = int(
            self.con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,3,'pass')",
                (self.document_id, revision),
            ).lastrowid
        )
        self.sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,1,1)",
                (self.feature_id,),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,bound_revision_body,"
            "bound_revision_legacy,approval_id) VALUES (?,?,?,?,0,?)",
            (
                self.sprint_id,
                self.document_id,
                revision,
                self.original,
                self.approval_id,
            ),
        )
        self.con.execute(
            "INSERT INTO sprint_spec_revision_history "
            "(sprint_id,document_id,generation,bound_revision_sha256,"
            "bound_revision_body,bound_revision_legacy,approval_id,actor_kind,"
            "reason) VALUES (?,?,1,?,?,0,?,'system','test binding')",
            (
                self.sprint_id,
                self.document_id,
                revision,
                self.original,
                self.approval_id,
            ),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants (sprint_id,shell_id,role,harness) "
            "VALUES (?,?,?,'codex')",
            (
                (self.sprint_id, 1, "planner"),
                (self.sprint_id, 2, "developer"),
                (self.sprint_id, 3, "reviewer"),
            ),
        )
        self.con.execute(
            "UPDATE sprints SET conformance_reviewer_shell_id=3,"
            "conformance_owner_generation=1,lifecycle='armed',"
            "armed_at=datetime('now') WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()

    def test_paused_rebind_preserves_history_and_exact_retry_is_idempotent(self) -> None:
        replacement = "# Revised governing bytes\n\nApproved correction.\n"
        old_revision = hashlib.sha256(self.original.encode()).hexdigest()
        new_revision = hashlib.sha256(replacement.encode()).hexdigest()
        self.con.execute(
            "UPDATE sprints SET lifecycle='paused',paused_at=datetime('now') "
            "WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.execute(
            "UPDATE documents SET body=? WHERE document_id=?",
            (replacement, self.document_id),
        )
        self.con.commit()
        actor = sprint_domain.LifecycleActor("planner", 1)
        store = sprint_domain.SprintSpecRevisionStore(self.con)

        receipt = store.rebind(
            self.sprint_id,
            self.document_id,
            actor,
            expected_revision_sha256=old_revision,
            reason="Reviewer decision message 77",
        )

        self.assertTrue(receipt.changed)
        self.assertEqual(old_revision, receipt.old_revision_sha256)
        self.assertEqual(new_revision, receipt.new_revision_sha256)
        self.assertEqual(2, receipt.generation)
        self.assertEqual(
            (new_revision, replacement, 0, None),
            tuple(
                self.con.execute(
                    "SELECT bound_revision_sha256,bound_revision_body,"
                    "bound_revision_legacy,approval_id FROM sprint_specs "
                    "WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )
        history = [
            tuple(row)
            for row in self.con.execute(
                "SELECT generation,bound_revision_sha256,bound_revision_body,"
                "approval_id,actor_kind,actor_shell_id,reason "
                "FROM sprint_spec_revision_history WHERE sprint_id=? "
                "ORDER BY generation",
                (self.sprint_id,),
            )
        ]
        self.assertEqual(
            [
                (
                    1,
                    old_revision,
                    self.original,
                    self.approval_id,
                    "system",
                    None,
                    "test binding",
                ),
                (
                    2,
                    new_revision,
                    replacement,
                    None,
                    "planner",
                    1,
                    "Reviewer decision message 77",
                ),
            ],
            history,
        )
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE event_type='spec.rebound'"
        ).fetchone()
        self.assertEqual(
            {
                "document_id": self.document_id,
                "generation": 2,
                "new_revision_sha256": new_revision,
                "old_revision_sha256": old_revision,
                "reason": "Reviewer decision message 77",
                "revision_id": receipt.revision_id,
            },
            json.loads(event[0]),
        )
        self.assertNotIn(replacement, event[0])
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM governing_revision_rebind_permits"
            ).fetchone()[0],
        )

        replay = store.rebind(
            self.sprint_id,
            self.document_id,
            actor,
            expected_revision_sha256=old_revision,
            reason="Reviewer decision message 77",
        )
        self.assertFalse(replay.changed)
        self.assertEqual(receipt.revision_id, replay.revision_id)
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_spec_revision_history "
                "WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE event_type='spec.rebound'"
            ).fetchone()[0],
        )

    def test_rebind_rejects_stale_expected_revision_without_effects(self) -> None:
        self.con.execute(
            "UPDATE sprints SET lifecycle='paused' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.execute(
            "UPDATE documents SET body='new contract' WHERE document_id=?",
            (self.document_id,),
        )
        self.con.commit()
        with self.assertRaisesRegex(
            sprint_domain.SprintConflictError, "binding changed"
        ) as raised:
            sprint_domain.SprintSpecRevisionStore(self.con).rebind(
                self.sprint_id,
                self.document_id,
                sprint_domain.LifecycleActor("planner", 1),
                expected_revision_sha256="0" * 64,
                reason="stale caller",
            )
        self.assertEqual("spec_revision_changed", raised.exception.details["code"])
        self.assertEqual(
            (hashlib.sha256(self.original.encode()).hexdigest(), self.original),
            tuple(
                self.con.execute(
                    "SELECT bound_revision_sha256,bound_revision_body "
                    "FROM sprint_specs WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            (1, 0),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_spec_revision_history"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events WHERE event_type='spec.rebound'"
                ).fetchone()[0],
            ),
        )

    def test_rebind_requires_paused_lifecycle_and_owner_authority(self) -> None:
        old_revision = hashlib.sha256(self.original.encode()).hexdigest()
        self.con.execute(
            "UPDATE documents SET body='new contract' WHERE document_id=?",
            (self.document_id,),
        )
        self.con.commit()
        store = sprint_domain.SprintSpecRevisionStore(self.con)
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "only while the Sprint is paused"
        ):
            store.rebind(
                self.sprint_id,
                self.document_id,
                sprint_domain.LifecycleActor("planner", 1),
                expected_revision_sha256=old_revision,
                reason="too early",
            )
        self.con.execute(
            "UPDATE sprints SET lifecycle='paused' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "originating Planner or FnB"
        ):
            store.rebind(
                self.sprint_id,
                self.document_id,
                sprint_domain.LifecycleActor("participant", 2),
                expected_revision_sha256=old_revision,
                reason="wrong actor",
            )
        self.assertEqual(
            (1, 0),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_spec_revision_history"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events WHERE event_type='spec.rebound'"
                ).fetchone()[0],
            ),
        )

    def test_rebind_history_and_active_projection_reject_untracked_rewrites(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.con.execute(
                "UPDATE sprint_specs SET bound_revision_body='invented' "
                "WHERE sprint_id=?",
                (self.sprint_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.con.execute(
                "UPDATE sprint_spec_revision_history SET reason='invented' "
                "WHERE sprint_id=?",
                (self.sprint_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.con.execute(
                "DELETE FROM sprint_spec_revision_history WHERE sprint_id=?",
                (self.sprint_id,),
            )
        self.assertEqual(
            (self.original, "test binding"),
            tuple(
                self.con.execute(
                    "SELECT ss.bound_revision_body,h.reason FROM sprint_specs ss "
                    "JOIN sprint_spec_revision_history h USING (sprint_id,document_id) "
                    "WHERE ss.sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )

    def test_exact_bound_body_survives_drift_and_is_participant_scoped(self) -> None:
        self.con.execute(
            "UPDATE documents SET body='current drift' WHERE document_id=?",
            (self.document_id,),
        )
        result = sprint_domain.SprintSpecRevisionStore(self.con).read(
            self.sprint_id, self.document_id, caller_shell_id=3
        )
        self.assertEqual(self.original, result["body"])
        self.assertEqual(
            hashlib.sha256(self.original.encode()).hexdigest(),
            result["bound_revision_sha256"],
        )
        self.assertEqual("available", result["availability"])
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            sprint_domain.SprintSpecRevisionStore(self.con).read(
                self.sprint_id, self.document_id, caller_shell_id=4
            )

    def test_legacy_mismatch_is_explicit_and_never_returns_current_text(self) -> None:
        legacy_document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',2,'Legacy governing spec','untrusted current text')",
                (self.feature_id,),
            ).lastrowid
        )
        revision = hashlib.sha256(self.original.encode()).hexdigest()
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,bound_revision_body,"
            "bound_revision_legacy) VALUES (?,?,?,NULL,1)",
            (self.sprint_id, legacy_document_id, revision),
        )
        self.con.execute(
            "INSERT INTO sprint_spec_revision_history "
            "(sprint_id,document_id,generation,bound_revision_sha256,"
            "bound_revision_body,bound_revision_legacy,actor_kind,reason) "
            "VALUES (?,?,1,?,NULL,1,'system','legacy test binding')",
            (self.sprint_id, legacy_document_id, revision),
        )
        with self.assertRaises(sprint_domain.BoundRevisionUnavailable) as raised:
            sprint_domain.SprintSpecRevisionStore(self.con).read(
                self.sprint_id, legacy_document_id, caller_shell_id=2
            )
        self.assertEqual("bound_revision_unavailable", raised.exception.details["code"])
        self.assertEqual(
            "unavailable_legacy_drift", raised.exception.details["availability"]
        )
        self.assertNotIn("body", raised.exception.details)

    def test_outside_authority_edit_is_atomic_ordered_and_retry_safe(self) -> None:
        changed = "# Changed by Developer\n"
        self.assertEqual(
            (True, None),
            server.patch_document(
                self.con,
                self.document_id,
                {"body": changed},
                editor_surface="shell_api",
                editor_shell_id=2,
            ),
        )
        self.assertEqual(
            (True, None),
            server.patch_document(
                self.con,
                self.document_id,
                {"body": changed},
                editor_surface="shell_api",
                editor_shell_id=2,
            ),
        )
        events = self.con.execute(
            "SELECT event_id,payload FROM sprint_events "
            "WHERE event_type='spec.body_edited' ORDER BY event_id"
        ).fetchall()
        self.assertEqual(1, len(events))
        payload = json.loads(events[0]["payload"])
        self.assertEqual("outside_authority", payload["authority"])
        self.assertEqual("shell_api", payload["editor_surface"])
        self.assertEqual(2, payload["editor_shell_id"])
        self.assertEqual("DEV1", payload["editor_shortname"])
        self.assertEqual("queued", payload["notification_state"])
        notices = self.con.execute(
            "SELECT receiver_shell_id,body,declared_type,actionable "
            "FROM wake_message WHERE message_kind='notification'"
        ).fetchall()
        self.assertEqual(1, len(notices))
        self.assertEqual((1, "re-enter", 0), (notices[0][0], notices[0][2], notices[0][3]))
        self.assertNotIn(self.original, notices[0]["body"])
        self.assertNotIn(changed, notices[0]["body"])
        bound = self.con.execute(
            "SELECT bound_revision_body FROM sprint_specs WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        self.assertEqual(self.original, bound)

        server.patch_document(
            self.con,
            self.document_id,
            {"body": self.original},
            editor_surface="shell_api",
            editor_shell_id=1,
        )
        ordered = [
            json.loads(row[0])
            for row in self.con.execute(
                "SELECT payload FROM sprint_events "
                "WHERE event_type='spec.body_edited' ORDER BY event_id"
            )
        ]
        self.assertEqual(2, len(ordered))
        self.assertEqual("planner", ordered[1]["authority"])
        self.assertEqual(ordered[0]["after_sha256"], ordered[1]["before_sha256"])
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0])

    def test_metadata_only_edit_creates_no_sprint_evidence(self) -> None:
        self.assertEqual(
            (True, None),
            server.patch_document(
                self.con,
                self.document_id,
                {"title": "Renamed"},
                editor_surface="shell_api",
                editor_shell_id=2,
            ),
        )
        self.assertEqual("Renamed", self.con.execute("SELECT title FROM documents").fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM sprint_events").fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0])

    def test_frozen_document_accepts_only_render_path(self) -> None:
        # A frozen row's content is immutable, but its render_path is a
        # location: it must stay movable so a frozen doc that collides in the
        # render layer can be cleared without unfreezing (#629).
        self.con.execute(
            "UPDATE documents SET frozen=1 WHERE document_id=?", (self.document_id,)
        )
        self.con.commit()
        for payload in (
            {"body": "# rewritten\n"},
            {"title": "Renamed"},
            {"title": "Renamed", "render_path": "specs_sc/moved.md"},
        ):
            ok, err = server.patch_document(
                self.con,
                self.document_id,
                payload,
                editor_surface="shell_api",
                editor_shell_id=2,
            )
            self.assertFalse(ok, payload)
            self.assertIn("document is frozen", err)
            self.assertIn("only render_path", err)
        self.assertEqual(
            (True, None),
            server.patch_document(
                self.con,
                self.document_id,
                {"render_path": "specs_sc/moved.md"},
                editor_surface="shell_api",
                editor_shell_id=2,
            ),
        )
        row = self.con.execute(
            "SELECT title,body,render_path,frozen FROM documents WHERE document_id=?",
            (self.document_id,),
        ).fetchone()
        self.assertEqual(
            ("Governing spec", self.original, "specs_sc/moved.md", 1), tuple(row)
        )
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM sprint_events").fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0])

    def test_relay_failure_rolls_back_document_event_and_wake(self) -> None:
        with mock.patch.object(
            sprint_message_delivery.SprintMessageStore,
            "send_in_transaction",
            side_effect=RuntimeError("relay failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "relay failed"):
                server.patch_document(
                    self.con,
                    self.document_id,
                    {"body": "must roll back"},
                    editor_surface="shell_api",
                    editor_shell_id=2,
                )
        self.assertEqual(self.original, self.con.execute("SELECT body FROM documents").fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM sprint_events").fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0])


class GoverningRevisionMigrationTest(unittest.TestCase):
    def test_migration_backfills_only_matching_legacy_body_and_guards_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "engine.db"
            con = sqlite3.connect(path)
            self.addCleanup(con.close)
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0203_sprint_cleanup_recovery.sql")
            con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (1,'Planner','PLN1','planner','prompt',1)"
            )
            feature = int(con.execute("INSERT INTO roadmap (title) VALUES ('Feature')").lastrowid)
            bodies = ("matching legacy body", "current body after drift")
            document_ids = [
                int(
                    con.execute(
                        "INSERT INTO documents (feature_id,kind,seq,title,body) "
                        "VALUES (?,'spec',?,?,?)",
                        (feature, seq, f"Spec {seq}", body),
                    ).lastrowid
                )
                for seq, body in enumerate(bodies, 1)
            ]
            sprint_ids = [
                int(
                    con.execute(
                        "INSERT INTO sprints "
                        "(feature_id,originating_planner_shell_id) VALUES (?,1)",
                        (feature,),
                    ).lastrowid
                )
                for _ in bodies
            ]
            revisions = (
                hashlib.sha256(bodies[0].encode()).hexdigest(),
                hashlib.sha256(b"historical unavailable body").hexdigest(),
            )
            con.executemany(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256) VALUES (?,?,?)",
                zip(sprint_ids, document_ids, revisions, strict=True),
            )
            con.commit()

            migrate.apply(con, REVISION_MIGRATION)

            stored = con.execute(
                "SELECT bound_revision_body,bound_revision_legacy "
                "FROM sprint_specs ORDER BY sprint_id"
            ).fetchall()
            self.assertEqual(bodies[0], stored[0][0])
            self.assertIsNone(stored[1][0])
            self.assertEqual((1, 1), (stored[0][1], stored[1][1]))
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "require an immutable governing body"
            ):
                con.execute(
                    "INSERT INTO sprint_specs "
                    "(sprint_id,document_id,bound_revision_sha256,"
                    "bound_revision_legacy) VALUES (?,?,?,0)",
                    (sprint_ids[0], document_ids[1], revisions[1]),
                )
            con.execute(
                "DELETE FROM sprint_specs WHERE sprint_id=?",
                (sprint_ids[1],),
            )
            con.execute(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256,"
                "bound_revision_body,bound_revision_legacy) "
                "VALUES (?,?,?,NULL,1)",
                (sprint_ids[1], document_ids[1], revisions[1]),
            )
            self.assertEqual(
                (None, 1),
                tuple(
                    con.execute(
                        "SELECT bound_revision_body,bound_revision_legacy "
                        "FROM sprint_specs WHERE sprint_id=?",
                        (sprint_ids[1],),
                    ).fetchone()
                ),
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                con.execute(
                    "UPDATE sprint_specs SET bound_revision_body='invented' "
                    "WHERE sprint_id=?",
                    (sprint_ids[1],),
                )


if __name__ == "__main__":
    unittest.main()
