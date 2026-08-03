"""Sprint routing and tracked Planner coordinate-mode contracts."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import active_chat_registry
import sprint_domain
import sprint_message_delivery


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintCoordinateModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        apply_schema(self.con)
        self.con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
            ),
        )
        feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Feature','in_progress')"
            ).lastrowid
        )
        self.sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) VALUES (?,?,?,?,?,?)",
            (
                (self.sprint_id, 3, "planner", "codex", "gpt-test", "high"),
                (self.sprint_id, 1, "developer", "codex", "gpt-test", "high"),
                (self.sprint_id, 2, "reviewer", "codex", "gpt-test", "high"),
            ),
        )
        participants = {
            str(row["role"]): int(row["participant_id"])
            for row in self.con.execute(
                "SELECT role,participant_id FROM sprint_participants WHERE sprint_id=?",
                (self.sprint_id,),
            )
        }
        self.planner_id = participants["planner"]
        self.developer_id = participants["developer"]
        self.con.commit()
        self.messages = sprint_message_delivery.SprintMessageStore(self.con)
        self.lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )

    def open_planner_chat(self, key: str) -> str:
        conversation_id = f"cv_{hashlib.sha256(key.encode()).hexdigest()[:32]}"
        self.con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,title,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (?,3,1,'codex','/tmp','Planner chat',?,?)",
            (conversation_id, key, hashlib.sha256(key.encode()).hexdigest()),
        )
        active_chat_registry.register(self.con, 3, conversation_id)
        self.con.commit()
        return conversation_id

    def planner_message(self, key: str):
        return self.messages.send(
            self.sprint_id,
            from_participant_id=self.developer_id,
            to_participant_id=self.planner_id,
            message_kind="notification",
            body=f"Planner report {key}",
            declared_type="re-enter",
            idempotency_key=key,
        )

    def set_coordinate_mode(self) -> None:
        self.con.execute(
            "UPDATE sprints SET coordinate_mode=1 WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()

    def coordinate_mode(self) -> int:
        return int(
            self.con.execute(
                "SELECT coordinate_mode FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )

    def test_coordinate_mode_rotates_an_idle_planner_chat(self) -> None:
        old_chat = self.open_planner_chat("idle-coordinate")
        self.set_coordinate_mode()
        sent = self.planner_message("idle-coordinate-message")
        observed: list[tuple[str, str]] = []

        outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "coordinate-idle-worker",
            lambda conversation, prompt, _key: (
                observed.append((conversation, prompt)) or "native-run"
            ),
        )

        self.assertEqual(outcome.wake_id, sent.wake_id)
        self.assertNotEqual(observed[0][0], old_chat)
        self.assertIn("(declared New)", observed[0][1])
        self.assertEqual(
            "closed",
            self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (old_chat,),
            ).fetchone()[0],
        )
        self.assertEqual(
            "re-enter",
            self.con.execute(
                "SELECT declared_type FROM wake_message WHERE message_id=?",
                (sent.message_id,),
            ).fetchone()[0],
            "coordinate mode is tracked separately from the sender's route literal",
        )

    def test_coordinate_mode_new_reenters_when_planner_is_mid_turn(self) -> None:
        planner_chat = self.open_planner_chat("busy-coordinate")
        pid, start_ticks = active_chat_registry.process_identity(str(os.getpid()))
        self.assertEqual(pid, os.getpid())
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=3",
            (pid, start_ticks),
        )
        self.con.commit()
        self.set_coordinate_mode()
        sent = self.planner_message("busy-coordinate-message")
        observed: list[tuple[str, str]] = []

        outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "coordinate-busy-worker",
            lambda conversation, prompt, _key: (
                observed.append((conversation, prompt)) or "native-run"
            ),
        )

        self.assertEqual(outcome.wake_id, sent.wake_id)
        self.assertEqual(observed[0][0], planner_chat)
        self.assertIn("(declared New)", observed[0][1])
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversations WHERE shell_id=3"
            ).fetchone()[0],
        )

    def test_automatic_pause_preserves_mode_until_fnb_confirms_pause(self) -> None:
        self.set_coordinate_mode()
        automatic = self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("system"),
            reason="wake_delivery_exhausted",
        )
        self.assertTrue(automatic.changed)
        self.assertEqual(self.coordinate_mode(), 1)

        confirmed = self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("fnb"),
            reason="FnB switches back to supervise mode",
        )
        self.assertTrue(confirmed.changed)
        self.assertEqual(self.coordinate_mode(), 0)

    def test_only_fnb_cancel_clears_coordinate_mode(self) -> None:
        self.set_coordinate_mode()
        receipt = self.lifecycle.abort(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="Planner cancellation",
            terminal_outcome="cancelled",
        )
        self.assertTrue(receipt.changed)
        self.assertEqual(self.coordinate_mode(), 1)

    def test_fnb_cancel_clears_coordinate_mode(self) -> None:
        self.set_coordinate_mode()
        receipt = self.lifecycle.abort(
            self.sprint_id,
            sprint_domain.LifecycleActor("fnb"),
            reason="FnB cancellation",
            terminal_outcome="cancelled",
        )
        self.assertTrue(receipt.changed)
        self.assertEqual(self.coordinate_mode(), 0)

    def test_fnb_close_clears_coordinate_mode(self) -> None:
        self.set_coordinate_mode()
        changed = self.lifecycle.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("fnb"),
            reason="Sprint accepted",
            terminal_outcome="accepted",
        )
        self.assertTrue(changed)
        self.assertEqual(self.coordinate_mode(), 0)


if __name__ == "__main__":
    unittest.main()
