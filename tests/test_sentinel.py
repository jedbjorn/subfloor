#!/usr/bin/env python3
"""Conductor Step 5 sentinel unit and synthetic-cycle coverage."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import activity_readers  # noqa: E402
import pr_poller  # noqa: E402
import sentinel  # noqa: E402


def build_db(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path if path else ":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed_floor(con: sqlite3.Connection, *, state="working") -> int:
    con.executescript(
        "INSERT INTO users (user_id,username,is_active) VALUES (1,'T',1);"
        "INSERT INTO shells "
        "(shell_id,display_name,shortname,flavor,system_prompt,user_id) VALUES "
        "(1,'Planner','plan1','planner','x',1),"
        "(2,'Dev','dev1','dev','x',1),"
        "(3,'Reviewer','rev1','reviewer','x',1);"
        "INSERT INTO documents "
        "(document_id,kind,title,body,frozen) "
        "VALUES (100,'doc','SPRINT: Sentinel','# sprint',0);"
        "INSERT INTO sprints "
        "(sprint_doc_id,state,legacy,planner_shell_id) "
        "VALUES (100,'active',1,1);"
    )
    unit_id = con.execute(
        "INSERT INTO sprint_units "
        "(sprint_doc_id,seq,unit_title,dev_shell_id,reviewer_shell_id,state,"
        " assigned_at,state_changed_at) "
        "VALUES (100,'U1','sentinel slice',2,3,?,"
        " '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
        (state,),
    ).lastrowid
    con.commit()
    return unit_id


class FakeReader:
    def __init__(self, evidence=None):
        self.evidence = evidence or activity_readers.Evidence()
        self.calls = []

    def read(self, shell, unit, now, role=None):
        self.calls.append((shell["shell_id"], unit["unit_id"], role))
        return self.evidence


class SentinelConfigTests(unittest.TestCase):
    def test_missing_and_malformed_config_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "instance.json"
            self.assertEqual(sentinel.load_config(path), sentinel.SentinelConfig())
            path.write_text('{"sentinel":{"enabled":"yes","interval_seconds":0}}')
            self.assertEqual(sentinel.load_config(path), sentinel.SentinelConfig())

    def test_valid_per_fork_config_sets_both_cadences(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "instance.json"
            path.write_text(json.dumps({
                "port": 8800,
                "sentinel": {
                    "enabled": True,
                    "interval_seconds": 15,
                    "activity_beat_seconds": 90,
                },
            }))
            self.assertEqual(
                sentinel.load_config(path),
                sentinel.SentinelConfig(True, 15, 90),
            )


class SentinelCycleTests(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.config = sentinel.SentinelConfig(
            enabled=True, interval_seconds=30, activity_beat_seconds=300
        )

    def tearDown(self):
        self.con.close()

    def run_cycle(
        self,
        *,
        now,
        reader=None,
        liveness=lambda claim: True,
        git_probe=None,
    ):
        return sentinel.cycle(
            self.con,
            config=self.config,
            now=now,
            activity_reader=reader or FakeReader(),
            liveness=liveness,
            git_probe=git_probe or (
                lambda worktree: {
                    "head_sha": None,
                    "committed_at": None,
                }
            ),
        )

    def test_no_live_sprint_is_a_write_free_noop(self):
        reader = FakeReader()
        result = self.run_cycle(
            now="2026-01-01T03:00:00Z", reader=reader
        )
        self.assertEqual(result["active_units"], 0)
        self.assertEqual(reader.calls, [])
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sentinel_events"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM directives").fetchone()[0],
            0,
        )

    def test_dead_launch_is_immediate_and_deduped_before_dwell(self):
        unit_id = seed_floor(self.con)
        self.con.execute(
            "INSERT INTO shell_launch_records "
            "(shell_id,pid,start_ticks,worktree,harness,launched_at) "
            "VALUES (2,4242,99,'/tmp/dev1','codex','2026-01-01T00:01:00Z')"
        )
        self.con.commit()

        first = self.run_cycle(
            now="2026-01-01T00:02:00Z", liveness=lambda claim: False
        )
        second = self.run_cycle(
            now="2026-01-01T00:03:00Z", liveness=lambda claim: False
        )

        self.assertEqual(first["dead_shells"], 1)
        self.assertEqual(first["stalls"], 0)
        self.assertEqual(second["dead_shells"], 0)
        directive = self.con.execute(
            "SELECT * FROM directives WHERE kind='dead-shell'"
        ).fetchone()
        self.assertEqual(directive["issuer_flavor"], "system")
        self.assertEqual(directive["target"], "conductor")
        self.assertEqual(directive["unit_id"], unit_id)
        event = self.con.execute(
            "SELECT directive_id,evidence FROM sentinel_events "
            "WHERE event_kind='dead-shell'"
        ).fetchone()
        self.assertEqual(event["directive_id"], directive["directive_id"])
        self.assertEqual(json.loads(event["evidence"])["process"]["pid"], 4242)

    def test_indeterminate_launch_verdict_never_becomes_dead(self):
        seed_floor(self.con)
        self.con.execute(
            "INSERT INTO shell_launch_records "
            "(shell_id,pid,start_ticks,worktree,harness,launched_at) "
            "VALUES (2,4242,99,'/tmp/dev1','codex','2026-01-01T00:01:00Z')"
        )
        self.con.commit()
        result = self.run_cycle(
            now="2026-01-01T00:02:00Z", liveness=lambda claim: None
        )
        self.assertEqual(result["dead_shells"], 0)
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM directives"
            ).fetchone()[0],
            0,
        )

    def test_disk_activity_emits_a_beat_and_resets_dwell(self):
        seed_floor(self.con)
        reader = FakeReader(activity_readers.Evidence(
            state_changed_at="2026-01-01T00:00:00Z",
            newest_mtime="2026-01-01T02:30:00Z",
            last_work_at="2026-01-01T02:30:00Z",
            process_present=True,
        ))
        result = self.run_cycle(
            now="2026-01-01T03:00:00Z", reader=reader
        )
        replay = self.run_cycle(
            now="2026-01-01T03:10:00Z", reader=reader
        )

        self.assertEqual(result["activity_beats"], 1)
        self.assertEqual(result["stalls"], 0)
        self.assertEqual(replay["activity_beats"], 0)
        event = self.con.execute(
            "SELECT evidence FROM sentinel_events "
            "WHERE event_kind='activity-beat'"
        ).fetchone()
        self.assertEqual(
            json.loads(event["evidence"])["activity_at"],
            "2026-01-01T02:30:00+00:00",
        )

    def test_recent_message_is_an_expected_signal(self):
        seed_floor(self.con)
        self.con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id,to_shell_id,body,kind,sprint_doc_id,created_at) "
            "VALUES (2,1,'still working','shell',100,'2026-01-01T02:30:00Z')"
        )
        self.con.commit()
        result = self.run_cycle(now="2026-01-01T03:00:00Z")
        self.assertEqual(result["stalls"], 0)

    def test_message_for_another_active_unit_does_not_reset_dwell(self):
        seed_floor(self.con)
        self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id,seq,unit_title,dev_shell_id,state,assigned_at,"
            " state_changed_at) "
            "VALUES (100,'U2','other',2,'working','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z')"
        )
        self.con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id,to_shell_id,body,kind,sprint_doc_id,created_at) "
            "VALUES (2,1,'U2 still working','shell',100,"
            "'2026-01-01T02:30:00Z')"
        )
        self.con.commit()
        result = self.run_cycle(now="2026-01-01T03:00:00Z")
        self.assertEqual(result["stalls"], 1)
        stalled = self.con.execute(
            "SELECT u.seq FROM directives d "
            "JOIN sprint_units u ON u.unit_id=d.unit_id "
            "WHERE d.kind='stall'"
        ).fetchall()
        self.assertEqual([row[0] for row in stalled], ["U1"])

    def test_recent_commit_is_an_expected_working_signal(self):
        seed_floor(self.con)
        result = self.run_cycle(
            now="2026-01-01T03:00:00Z",
            git_probe=lambda worktree: {
                "head_sha": "abc123",
                "committed_at": "2026-01-01T02:30:00Z",
            },
        )
        self.assertEqual(result["stalls"], 0)

    def test_recent_pr_observation_is_an_expected_working_signal(self):
        unit_id = seed_floor(self.con)
        watch_id = self.con.execute(
            "INSERT INTO watched_prs "
            "(repo,pr_number,shell_id,sprint_doc_id,unit_id,last_seen) "
            "VALUES ('o/r',7,2,100,?,?)",
            (
                unit_id,
                json.dumps({
                    "state": "OPEN",
                    "sha": "abc123",
                    "checks": "PENDING",
                    "reviews": 0,
                    "review_state": None,
                }),
            ),
        ).lastrowid
        self.con.execute(
            "INSERT INTO pr_poll_observations "
            "(watch_id,head_sha,fingerprint,transition,observed_at) "
            "VALUES (?,'abc123','{}','checks:PENDING',"
            "'2026-01-01T02:30:00Z')",
            (watch_id,),
        )
        self.con.commit()
        result = self.run_cycle(now="2026-01-01T03:00:00Z")
        self.assertEqual(result["stalls"], 0)

    def test_recent_review_is_an_expected_in_review_signal(self):
        unit_id = seed_floor(self.con, state="in_review")
        watch_id = self.con.execute(
            "INSERT INTO watched_prs "
            "(repo,pr_number,shell_id,sprint_doc_id,unit_id,last_seen) "
            "VALUES ('o/r',7,3,100,?,'{}')",
            (unit_id,),
        ).lastrowid
        self.con.execute(
            "INSERT INTO pr_poll_observations "
            "(watch_id,head_sha,fingerprint,transition,observed_at) "
            "VALUES (?,'abc123','{}','review:APPROVED',"
            "'2026-01-01T02:30:00Z')",
            (watch_id,),
        )
        self.con.commit()
        result = self.run_cycle(now="2026-01-01T03:00:00Z")
        self.assertEqual(result["stalls"], 0)

    def test_recent_kickoff_is_an_expected_pending_signal(self):
        unit_id = seed_floor(self.con, state="pending")
        self.con.execute(
            "INSERT INTO directives "
            "(issuer_shell_id,issuer_flavor,kind,target,sprint_doc_id,unit_id,"
            " created_at) "
            "VALUES (1,'planner','kickoff','conductor',100,?,"
            "'2026-01-01T02:30:00Z')",
            (unit_id,),
        )
        self.con.commit()
        result = self.run_cycle(now="2026-01-01T03:00:00Z")
        self.assertEqual(result["stalls"], 0)

    def test_recent_planner_directive_is_an_expected_blocked_signal(self):
        unit_id = seed_floor(self.con, state="blocked")
        reader = FakeReader()
        self.con.execute(
            "INSERT INTO directives "
            "(issuer_shell_id,issuer_flavor,kind,target,sprint_doc_id,unit_id,"
            " created_at) "
            "VALUES (1,'planner','hold','conductor',100,?,"
            "'2026-01-01T02:30:00Z')",
            (unit_id,),
        )
        self.con.commit()
        result = self.run_cycle(
            now="2026-01-01T03:00:00Z", reader=reader
        )
        self.assertEqual(result["stalls"], 0)
        self.assertEqual(reader.calls, [])

    def test_expired_dwell_writes_full_evidence_and_one_signal(self):
        unit_id = seed_floor(self.con)
        result = self.run_cycle(now="2026-01-01T03:00:00Z")
        replay = self.run_cycle(now="2026-01-01T03:01:00Z")

        self.assertEqual(result["stalls"], 1)
        self.assertEqual(replay["stalls"], 0)
        directive = self.con.execute(
            "SELECT payload FROM directives WHERE kind='stall'"
        ).fetchone()
        payload = json.loads(directive["payload"])
        self.assertEqual(payload["dwell_seconds"], 10800)
        self.assertEqual(payload["max_dwell_seconds"], 7200)
        self.assertEqual(payload["last_commit_sha"], None)
        self.assertEqual(payload["last_message_id"], None)
        self.assertEqual(payload["unit_seq"], "U1")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sentinel_events "
                "WHERE unit_id=? AND event_kind='stall'",
                (unit_id,),
            ).fetchone()[0],
            1,
        )

    def test_in_review_monitors_the_reviewer_not_the_developer(self):
        seed_floor(self.con, state="in_review")
        reader = FakeReader(activity_readers.Evidence(
            state_changed_at="2026-01-01T00:00:00Z",
            newest_mtime="2026-01-01T00:10:00Z",
        ))
        self.run_cycle(now="2026-01-01T00:20:00Z", reader=reader)
        self.assertEqual(reader.calls, [(3, 1, "reviewer")])


class SyntheticServiceCycleTests(unittest.TestCase):
    def test_enabled_service_runs_one_sentinel_cycle_without_a_pr_watch(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "shell_db.db"
            con = build_db(db_path)
            seed_floor(con)
            con.close()
            poller = pr_poller.Poller(
                db_path,
                interval=30,
                fetch=lambda query: self.fail("no watch means no GitHub read"),
                activity_reader=FakeReader(),
                sentinel_config=sentinel.SentinelConfig(True, 30, 300),
                sentinel_liveness=lambda claim: True,
                sentinel_git_probe=lambda worktree: {
                    "head_sha": None,
                    "committed_at": None,
                },
            )
            poller.start()
            time.sleep(0.3)
            poller.stop()
            poller.join(timeout=5)

            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM directives WHERE kind='stall'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT interval_s FROM daemon_heartbeats "
                        "WHERE name='sentinel'"
                    ).fetchone()[0],
                    30,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM daemon_heartbeats "
                        "WHERE name='reconcile'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
