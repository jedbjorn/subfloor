#!/usr/bin/env python3
"""Smoke tests for the review-layer data-assembly functions (api/server.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and
the sibling tests. Each test builds a throwaway DB the way the engine ships it
(schema.sql + every migration in filename order), seeds REPRESENTATIVE data,
then calls each `get_*(con)` assembler and asserts it returns without raising.

Why this file exists: a `get_roadmap()` `KeyError: 'feature_id'` shipped
because nothing exercised the endpoints, and the bug was data-dependent — it
only fired once an open flag was linked to a feature. `./sc verify` does
rebuild→render→boot and never touches the API; `./sc test` had no endpoint
coverage. So the seed below deliberately includes the trigger combinations:
  - a flag that is open + linked to a feature   (the exact KeyError trigger)
  - a document linked to a feature
  - a roadmap feature with an owning shell
Any future SELECT that omits a column the code reads by key will raise here,
on a developer's machine, instead of as a cryptic 500 in front of the FnB.

Run:
    python3 tests/test_api_endpoints.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402  (server.py adds scripts/ to the path on import)
import models as routes_cli  # noqa: E402


def compatible_runtime(version: str = "2.22.0", *, harness: str = "vibe",
                       scope: dict | None = None) -> dict:
    ranges = {
        "claude": ("2.1.220", "2.2.0", "2.1.222"),
        "codex": ("0.145.0", "0.147.0", "0.145.0"),
        "kimi": ("0.30.0", "0.34.0", "0.33.0"),
        "opencode": ("1.18.9", "1.19.0", "1.18.9"),
        "vibe": ("2.22.0", "2.23.0", "2.22.0"),
    }
    minimum, maximum, verified = ranges[harness]
    scope = scope or routes_cli.model_catalog.harness_versions.runtime_scope()
    return {
        "harness": harness,
        **scope,
        "version": version,
        "compatibility": "verified" if version == verified else "supported",
        "minimum_version": minimum,
        "maximum_version_exclusive": maximum,
        "verified_version": verified,
        "error": None,
    }


def controlled_bundle(
    harness: str, selector: str, fingerprint: str | None, *,
    status: dict | None = None,
) -> dict:
    versions = {
        "claude": "2.1.222", "codex": "0.145.0",
        "kimi": "0.33.0", "opencode": "1.18.9",
    }
    status = status or compatible_runtime(versions[harness], harness=harness)
    scope = {
        "runtime": status["runtime"],
        "runtime_identity": status["runtime_identity"],
    }
    return {
        "runtime_status": status,
        "runtime_scope": scope,
        "source_fingerprint": fingerprint,
    }


def build_db() -> sqlite3.Connection:
    """Fresh in-memory DB: schema.sql + every migration, FK enforcement on."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for path in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(path.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def build_legacy_db(path: str = ":memory:") -> sqlite3.Connection:
    """The schema floor immediately before runtime-advisory migration 0210."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if migration.name.startswith("0210_"):
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed(con: sqlite3.Connection) -> dict:
    """Minimal but trigger-complete fixture. Returns the ids it created."""
    cur = con.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname) "
        "VALUES ('Dev', 'x', 'dev', 'dev')")
    sid = cur.lastrowid
    bespoke_sid = con.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname) "
        "VALUES ('Custom', 'x', NULL, 'custom')").lastrowid
    fid = con.execute(
        "INSERT INTO roadmap (title, roadmap_status, sort_order, owning_shell, summary) "
        "VALUES ('Feature A', 'next', 1, ?, 'a summary')", (sid,)).lastrowid
    con.execute(
        "INSERT INTO documents (feature_id, kind, seq, title, render_path) "
        "VALUES (?, 'spec', 1, 'Spec A', 'specs_sc/a.md')", (fid,))
    con.execute(
        "INSERT INTO documents (feature_id, kind, seq, title, render_path) "
        "VALUES (?, 'doc', 1, 'Doc A', 'docs_sc/a.md')", (fid,))
    # The exact KeyError trigger: an OPEN, non-deleted flag linked to a feature.
    con.execute(
        "INSERT INTO flags (display_name, description, resolved, is_deleted, "
        "feature_id, shell_id) VALUES ('CC-001', 'blocker', 0, 0, ?, ?)",
        (fid, sid))
    # A repo-local skill (name not under assets/skills/) + a grant, so the
    # Skills-tab assembler exercises both origins and the grant aggregation.
    kid = con.execute(
        "INSERT INTO skills (name, description, category, common, is_deleted) "
        "VALUES ('local_only_skill', 'fixture repo skill', 'craft', 0, 0)").lastrowid
    con.execute("INSERT INTO flavor_skills (flavor, skill_id) VALUES ('dev', ?)",
                (kid,))
    con.execute("INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
                (bespoke_sid, kid))
    con.commit()
    return {"shell_id": sid, "bespoke_shell_id": bespoke_sid,
            "feature_id": fid, "skill_id": kid}


class AssemblerSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.ids = seed(self.con)

    def tearDown(self) -> None:
        self.con.close()

    def create_sprint_chat(
        self,
        participant_id: int,
        *,
        conversation_id: str,
        harness: str,
        key: str,
    ) -> str:
        shell_id = int(
            self.con.execute(
                "SELECT shell_id FROM sprint_participants WHERE participant_id=?",
                (participant_id,),
            ).fetchone()[0]
        )
        active = self.con.execute(
            "SELECT chat_id FROM active_shell_chats WHERE shell_id=?", (shell_id,)
        ).fetchone()
        if active is not None:
            self.con.execute(
                "UPDATE conversations SET state='closed',closed_at=datetime('now') "
                "WHERE conversation_id=?",
                (active[0],),
            )
        self.con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,title,"
            "creation_idempotency_key,creation_request_hash,conversation_scope) "
            "VALUES (?,?,1,?,'/fixture','Sprint fixture',?,?,'sprint')",
            (conversation_id, shell_id, harness, key, f"hash:{key}"),
        )
        self.con.execute(
            "INSERT INTO sprint_participant_conversations "
            "(sprint_participant_id,conversation_id) VALUES (?,?)",
            (participant_id, conversation_id),
        )
        self.con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (?,?)",
            (shell_id, conversation_id),
        )
        return conversation_id

    def test_get_shells(self) -> None:
        out = server.get_shells(self.con)
        self.assertTrue(any(s["shell_id"] == self.ids["shell_id"] for s in out))

    def test_get_shells_projects_recipient_scoped_unread_message_counts(self) -> None:
        target = self.ids["shell_id"]
        sender = self.ids["bespoke_shell_id"]
        self.con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id,to_shell_id,kind,body) VALUES (?,?,'shell','first')",
            (sender, target),
        )
        self.con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id,to_shell_id,kind,body) VALUES (?,?,'task','second')",
            (sender, target),
        )
        self.con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id,to_shell_id,kind,body,read_at) "
            "VALUES (?,?,'result','already read',datetime('now'))",
            (sender, target),
        )
        self.con.commit()

        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertEqual(2, by_id[target]["unread_message_count"])
        self.assertEqual(0, by_id[sender]["unread_message_count"])

    def test_get_shells_projects_only_future_pending_wake_availability(self) -> None:
        target = self.ids["shell_id"]
        other = self.ids["bespoke_shell_id"]
        future_wake = self.con.execute(
            "INSERT INTO sprint_wake_outbox "
            "(receiver_shell_id,idempotency_key,available_at) "
            "VALUES (?,?,'2099-07-31 12:00:15')",
            (target, "future-pending-wake"),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_wake_outbox "
            "(receiver_shell_id,idempotency_key,available_at) "
            "VALUES (?,?,'2000-07-31 12:00:15')",
            (other, "past-pending-wake"),
        )
        self.con.commit()

        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertEqual(
            "2099-07-31 12:00:15",
            by_id[target]["pending_wake_available_at"],
        )
        self.assertIsNone(by_id[other]["pending_wake_available_at"])

        self.con.execute(
            "UPDATE sprint_wake_outbox SET state='delivered',"
            "delivered_at=datetime('now') WHERE wake_id=?",
            (future_wake,),
        )
        self.con.commit()
        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertIsNone(by_id[target]["pending_wake_available_at"])

    def test_get_shells_projects_only_live_current_sprint_conversation(self) -> None:
        shell_id = self.ids["shell_id"]
        self.con.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        self.con.execute(
            "UPDATE shells SET user_id=1 WHERE shell_id=?", (shell_id,)
        )
        sprint_id = self.con.execute(
            "INSERT INTO sprints "
            "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
            "VALUES (?,?,1)",
            (self.ids["feature_id"], shell_id),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,disposition) "
            "VALUES (?,?,'reviewer','codex','idle')",
            (sprint_id, self.ids["bespoke_shell_id"]),
        )
        self.con.execute(
            "UPDATE sprints SET conformance_reviewer_shell_id=?,"
            "conformance_owner_generation=1,lifecycle='armed' WHERE sprint_id=?",
            (self.ids["bespoke_shell_id"], sprint_id),
        )
        participant_id = self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,disposition) "
            "VALUES (?,?,'developer','codex','active')",
            (sprint_id, shell_id),
        ).lastrowid
        conversation_id = self.create_sprint_chat(
            int(participant_id),
            conversation_id="cv_fixture_live",
            harness="codex",
            key="fixture:sprint:participant:wake",
        )
        self.con.commit()

        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertEqual(
            {
                "sprint_id": int(sprint_id),
                "lifecycle": "armed",
                "role": "developer",
                "disposition": "active",
                "current_conversation_id": conversation_id,
            },
            by_id[shell_id]["sprint"],
        )

        self.con.execute(
            "UPDATE sprints SET lifecycle='completed',terminal_outcome='shipped' "
            "WHERE sprint_id=?",
            (sprint_id,),
        )
        self.con.commit()
        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertIsNone(by_id[shell_id]["sprint"])

    def test_get_shells_prioritizes_armed_then_latest_paused_sprint(self) -> None:
        shell_id = self.ids["shell_id"]
        self.con.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        self.con.execute(
            "UPDATE shells SET user_id=1 WHERE shell_id=?", (shell_id,)
        )

        paused_id = self.con.execute(
            "INSERT INTO sprints "
            "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
            "VALUES (?,?,1)",
            (self.ids["feature_id"], shell_id),
        ).lastrowid
        paused_participant_id = self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,disposition) "
            "VALUES (?,?,'developer','codex','active')",
            (paused_id, shell_id),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,disposition) "
            "VALUES (?,?,'reviewer','codex','idle')",
            (paused_id, self.ids["bespoke_shell_id"]),
        )
        self.create_sprint_chat(
            int(paused_participant_id),
            conversation_id="cv_fixture_paused",
            harness="codex",
            key="fixture:sprint:paused:participant:wake",
        )
        self.con.execute(
            "UPDATE sprints SET conformance_reviewer_shell_id=?,"
            "conformance_owner_generation=1,lifecycle='armed',"
            "armed_at='2026-07-31 08:00:00' "
            "WHERE sprint_id=?",
            (self.ids["bespoke_shell_id"], paused_id),
        )
        self.con.execute(
            "UPDATE sprints SET lifecycle='paused',paused_at='2026-07-31 10:00:00' "
            "WHERE sprint_id=?",
            (paused_id,),
        )

        armed_id = self.con.execute(
            "INSERT INTO sprints "
            "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
            "VALUES (?,?,1)",
            (self.ids["feature_id"], shell_id),
        ).lastrowid
        armed_participant_id = self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,disposition) "
            "VALUES (?,?,'reviewer','kimi','idle')",
            (armed_id, shell_id),
        ).lastrowid
        armed_conversation_id = self.create_sprint_chat(
            int(armed_participant_id),
            conversation_id="cv_fixture_armed",
            harness="kimi",
            key="fixture:sprint:armed:participant:wake",
        )
        self.con.execute(
            "UPDATE sprints SET conformance_reviewer_shell_id=?,"
            "conformance_owner_generation=1,lifecycle='armed',"
            "armed_at='2026-07-31 11:00:00' "
            "WHERE sprint_id=?",
            (shell_id, armed_id),
        )
        self.con.commit()

        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertEqual(
            {
                "sprint_id": int(armed_id),
                "lifecycle": "armed",
                "role": "reviewer",
                "disposition": "idle",
                "current_conversation_id": armed_conversation_id,
            },
            by_id[shell_id]["sprint"],
        )

        self.con.execute(
            "UPDATE sprints SET lifecycle='paused',paused_at='2026-07-31 09:00:00' "
            "WHERE sprint_id=?",
            (armed_id,),
        )
        self.con.commit()
        by_id = {row["shell_id"]: row for row in server.get_shells(self.con)}
        self.assertEqual(
            {
                "sprint_id": int(paused_id),
                "lifecycle": "paused",
                "role": "developer",
                "disposition": "active",
                "current_conversation_id": armed_conversation_id,
            },
            by_id[shell_id]["sprint"],
        )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participants WHERE shell_id=?",
                (shell_id,),
            ).fetchone()[0],
        )

    def test_get_shell(self) -> None:
        out = server.get_shell(self.con, self.ids["shell_id"])
        self.assertIsNotNone(out)
        for key in ("seed", "lns", "skills", "decisions"):
            self.assertIn(key, out)

    def test_get_shell_missing_returns_none(self) -> None:
        self.assertIsNone(server.get_shell(self.con, 999999))

    def test_health_exposes_local_artifact_capabilities(self) -> None:
        with mock.patch.object(server.ports_mod, "resolve",
                               return_value={"repo": "source", "port": 17171}), \
             mock.patch.object(server.artifact_policy, "mode", return_value="local"):
            out = server.health_payload()
        self.assertEqual(out["artifact_mode"], "local")
        self.assertFalse(out["git_publication"])
        self.assertEqual(out["repo"], "source")

    def test_health_never_offers_git_publication(self) -> None:
        with mock.patch.object(server.ports_mod, "resolve",
                               return_value={"repo": "fork", "port": 17172}), \
             mock.patch.object(server.artifact_policy, "mode", return_value="local"):
            out = server.health_payload()
        self.assertEqual(out["artifact_mode"], "local")
        self.assertFalse(out["git_publication"])

    def test_get_roadmap_with_linked_flag_and_doc(self) -> None:
        # The regression: this path raised KeyError('feature_id') when a flag
        # was linked to a feature. Assert it assembles and carries the links.
        out = server.get_roadmap(self.con)
        feats = [f for b in out["buckets"] for f in b["features"]]
        feat = next(f for f in feats if f["feature_id"] == self.ids["feature_id"])
        self.assertEqual(len(feat["open_flags"]), 1)
        self.assertTrue(len(feat["documents"]) >= 1)

    def test_get_docs(self) -> None:
        out = server.get_docs(self.con)
        self.assertTrue(any(d["feature_id"] == self.ids["feature_id"]
                            for d in out["docs"]))

    def test_get_flags(self) -> None:
        out = server.get_flags(self.con)
        self.assertTrue(out["flags"])
        self.assertTrue(any(f["feature_title"] == "Feature A"
                            for f in out["flags"]))

    def test_flag_and_roadmap_assemblers_tolerate_pre_advisory_schema(
        self,
    ) -> None:
        legacy = build_legacy_db()
        try:
            ids = seed(legacy)
            with mock.patch.object(
                server.runtime_flags, "reconcile_pending"
            ) as reconcile:
                flags = server.get_flags(legacy)["flags"]
            reconcile.assert_not_called()
            flag = next(
                row for row in flags if row["feature_id"] == ids["feature_id"]
            )
            self.assertEqual(flag["management_state"], "human")
            self.assertEqual(flag["severity"], "tracker")
            self.assertEqual(flag["blocking_scope"], "feature")
            self.assertEqual(flag["blocks_runtime"], 1)
            self.assertIsNone(flag["source_kind"])
            roadmap = server.get_roadmap(legacy)
            feature = next(
                row
                for bucket in roadmap["buckets"]
                for row in bucket["features"]
                if row["feature_id"] == ids["feature_id"]
            )
            self.assertEqual(
                [row["display_name"] for row in feature["open_flags"]],
                ["CC-001"],
            )
        finally:
            legacy.close()

    def test_mem_flag_reads_and_patch_tolerate_pre_advisory_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.db"
            legacy = build_legacy_db(str(db_path))
            ids = seed(legacy)
            flag_id = legacy.execute(
                "SELECT flag_id FROM flags WHERE feature_id=?",
                (ids["feature_id"],),
            ).fetchone()[0]
            legacy.close()
            with mock.patch.object(server, "DB_PATH", db_path), mock.patch.object(
                server.Handler,
                "_require_shell_auth",
                return_value=ids["shell_id"],
            ):
                status, _headers, raw = server.dispatch_http(
                    "GET", "/_sc/mem/flags", "", b""
                )
                self.assertEqual(status, 200)
                listed = json.loads(raw)["flags"]
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["management_state"], "human")
                self.assertEqual(listed[0]["blocks_runtime"], 1)

                status, _headers, raw = server.dispatch_http(
                    "GET", f"/_sc/mem/flags/{flag_id}", "", b""
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["flag"]["severity"], "tracker")

                body = json.dumps({"description": "updated legacy flag"}).encode()
                headers = (
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                )
                status, _headers, raw = server.dispatch_http(
                    "PATCH", f"/_sc/mem/flags/{flag_id}", headers, body
                )
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(raw)["ok"])
            check = sqlite3.connect(db_path)
            try:
                description = check.execute(
                    "SELECT description FROM flags WHERE flag_id=?", (flag_id,)
                ).fetchone()[0]
                self.assertEqual(description, "updated legacy flag")
            finally:
                check.close()

    def test_get_skills_origin_and_grants(self) -> None:
        out = server.get_skills(self.con)
        self.assertTrue(out["shells"])
        by_name = {s["name"]: s for s in out["skills"]}
        # the fixture skill has no assets/skills/ dir → repo origin, granted once
        fixture = by_name["local_only_skill"]
        self.assertEqual(fixture["origin"], "repo")
        self.assertEqual(fixture["granted_flavors"], ["dev"])
        self.assertEqual(
            fixture["granted_shells"], [self.ids["bespoke_shell_id"]])
        # an engine-seeded skill derives as engine
        self.assertEqual(by_name["db_map"]["origin"], "engine")

    def test_get_shell_skills_carry_origin(self) -> None:
        out = server.get_shell(self.con, self.ids["shell_id"])
        self.assertTrue(all("origin" in k and "category" in k for k in out["skills"]))

    def test_get_map_unmapped_degrades_to_empty(self) -> None:
        # get_map() reads the SEPARATE map.db via map_db.open_ro() — it takes no
        # args and ignores shell_db. When the fork isn't mapped, open_ro() returns
        # None and get_map must degrade to the empty shape, never crash.
        with mock.patch.object(server.map_db, "open_ro", return_value=None):
            out = server.get_map()
        self.assertEqual(out["total_files"], 0)
        self.assertIsNone(out["repo"])

    def test_get_roadmap_includes_blockers_key(self) -> None:
        # Every feature dict must carry a `blockers` list (empty when none),
        # so the UI can read f.blockers unconditionally.
        out = server.get_roadmap(self.con)
        feats = [f for b in out["buckets"] for f in b["features"]]
        self.assertTrue(all(isinstance(f.get("blockers"), list) for f in feats))


class FeatureBlockerTest(unittest.TestCase):
    """server.set_blockers — replace-set semantics + the validations that keep
    the blocker graph a DAG (self, unknown id, cycle)."""

    def setUp(self) -> None:
        self.con = build_db()
        # three features in real (sequencing) stages
        self.A = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) VALUES ('A','in_progress')").lastrowid
        self.B = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) VALUES ('B','next')").lastrowid
        self.C = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) VALUES ('C','near_term')").lastrowid
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()

    def _blockers_of(self, fid):
        out = server.get_roadmap(self.con)
        feats = {f["feature_id"]: f for b in out["buckets"] for f in b["features"]}
        return sorted(feats[fid]["blockers"])

    def test_replace_set(self) -> None:
        ok, err = server.set_blockers(self.con, self.B, [self.A])
        self.assertTrue(ok, err)
        self.assertEqual(self._blockers_of(self.B), [self.A])
        # replace (not append): C then A,C
        ok, _ = server.set_blockers(self.con, self.C, [self.A])
        self.assertTrue(ok)
        ok, _ = server.set_blockers(self.con, self.C, [self.A, self.B])
        self.assertTrue(ok)
        self.assertEqual(self._blockers_of(self.C), sorted([self.A, self.B]))
        # empty list clears
        ok, _ = server.set_blockers(self.con, self.C, [])
        self.assertTrue(ok)
        self.assertEqual(self._blockers_of(self.C), [])

    def test_dedup(self) -> None:
        ok, _ = server.set_blockers(self.con, self.C, [self.A, self.A, self.B])
        self.assertTrue(ok)
        self.assertEqual(self._blockers_of(self.C), sorted([self.A, self.B]))

    def test_self_block_rejected(self) -> None:
        ok, err = server.set_blockers(self.con, self.A, [self.A])
        self.assertFalse(ok)
        self.assertIn("itself", err)
        self.assertEqual(self._blockers_of(self.A), [])

    def test_unknown_id_rejected(self) -> None:
        ok, err = server.set_blockers(self.con, self.A, [999999])
        self.assertFalse(ok)
        self.assertIn("no such feature", err)
        self.assertEqual(self._blockers_of(self.A), [])

    def test_missing_feature_rejected(self) -> None:
        ok, err = server.set_blockers(self.con, 999999, [self.A])
        self.assertFalse(ok)
        self.assertEqual(err, "no such feature")

    def test_cycle_rejected_and_no_write(self) -> None:
        ok, _ = server.set_blockers(self.con, self.B, [self.A])   # B ← A
        self.assertTrue(ok)
        ok, err = server.set_blockers(self.con, self.A, [self.B])  # A ← B would cycle
        self.assertFalse(ok)
        self.assertIn("cycle", err)
        # the rejected set wrote nothing; the original edge stands
        self.assertEqual(self._blockers_of(self.A), [])
        self.assertEqual(self._blockers_of(self.B), [self.A])

    def test_transitive_cycle_rejected(self) -> None:
        self.assertTrue(server.set_blockers(self.con, self.B, [self.A])[0])  # B ← A
        self.assertTrue(server.set_blockers(self.con, self.C, [self.B])[0])  # C ← B
        ok, err = server.set_blockers(self.con, self.A, [self.C])  # A ← C closes A→B→C→A
        self.assertFalse(ok)
        self.assertIn("cycle", err)


class FlavorDefaultsTest(unittest.TestCase):
    """The Default Models matrix: get_flavor_defaults / set_flavor_default.

    flavor_defaults is migration-seeded launch config the GUI now edits — the
    contract is upsert-on-write (template flavors / harnesses may lack seeded
    rows), a transactional star (exactly one is_default per flavor after), and
    loud validation for unknown names."""

    def setUp(self) -> None:
        self.con = build_db()
        self.addCleanup(self.con.close)
        evidence = mock.patch.object(
            server.model_catalog,
            "controlled_route_evidence",
            side_effect=lambda harness, selector: controlled_bundle(
                harness, selector, "f" * 64,
            ),
        )
        evidence.start()
        self.addCleanup(evidence.stop)

    def _row(self, flavor, harness):
        return self.con.execute(
            "SELECT model,effort,is_default FROM flavor_defaults "
            "WHERE flavor=? AND harness=?", (flavor, harness)).fetchone()

    def _route(self, harness, selector, *, availability="available", stale=0,
               seen_at=None, efforts=("low", "high")):
        seen_at = seen_at or datetime.now(timezone.utc).isoformat()
        if harness != "vibe":
            supported = list(efforts)
            digests = {"low": "d" * 64, "high": "e" * 64}
            version = "2.1.222" if harness == "claude" else "0.145.0"
            self.con.execute(
                "INSERT OR IGNORE INTO model_catalog_generations ("
                "generation_id,payload_version,contract_version,started_at,"
                "completed_at,state,runtime,source_summary,harness_versions,"
                "source_fingerprints,error_summary,payload_digest"
                ") VALUES (?,?,?,?,?,'successful','host','[]','{}','{}',NULL,?)",
                ("a" * 32, 6, 2, seen_at, seen_at, "b" * 64),
            )
            self.con.execute(
                "INSERT INTO model_routes ("
                "harness,selector,source,availability,headless_supported,"
                "high_effort_supported,supported_efforts,cli_version,last_seen_at,"
                "stale,generation_id,evidence_kind,evidence_digest,"
                "source_fingerprint,harness_version,harness_compatibility,"
                "selector_binding,effort_metadata,adapter_metadata,default_effort"
                ") VALUES (?,?,?,?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    harness, selector, "test", availability,
                    json.dumps(supported),
                    f"{harness} {version}", seen_at, stale, "a" * 32,
                    {"claude": "claude-portable-manifest",
                     "codex": "codex-model-cache"}[harness],
                    "e" * 64, "f" * 64, version, "verified",
                    json.dumps({"kind": "exact-model", "selector": selector}),
                    json.dumps({
                        "supported": supported,
                        "default": "high" if "high" in supported else None,
                        "digests": {
                            effort: digests[effort] for effort in supported
                        },
                        "native_variant_ids": {},
                    }),
                    "{}", "high",
                ),
            )
            self.con.commit()
            return
        self.con.execute(
            "INSERT INTO model_routes "
            "(harness, selector, source, availability, last_seen_at, stale) "
            "VALUES (?, ?, 'test', ?, ?, ?)",
            (harness, selector, availability, seen_at, stale))
        self.con.commit()

    def test_matrix_includes_template_flavors_and_harnesses(self) -> None:
        got = server.get_flavor_defaults(self.con)
        self.assertIn("planner", got["flavors"])
        self.assertIn("admin", got["flavors"], "template flavors appear even unseeded")
        self.assertEqual(
            got["harnesses"], ["claude", "codex", "kimi", "opencode", "vibe"]
        )
        self.assertIn("deepseek", got["harness_status"])
        self.assertNotIn("deepseek", got["harnesses"])
        self.assertEqual(got["harness_status"]["deepseek"]["surfaces"], {
            "terminal": False,
            "one_shot": True,
            "browser": True,
            "sprint": False,
        })

    def test_historical_unknown_harness_stays_projected_without_launch_access(self) -> None:
        self.con.execute(
            "INSERT INTO users (user_id,username) VALUES (910,'historical-owner')"
        )
        self.con.execute(
            "INSERT INTO shells (shell_id,display_name,system_prompt,user_id) "
            "VALUES (910,'Historical','prompt',910)"
        )
        self.con.execute(
            "INSERT INTO conversations ("
            "conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash"
            ") VALUES ('cv_removed',?,?,'removed-harness','/tmp/removed',"
            "'removed-create','removed-hash')",
            (910, 910),
        )
        self.con.commit()

        got = server.get_flavor_defaults(self.con)

        self.assertEqual(
            got["harness_status"]["removed-harness"]["unavailable_reason"],
            "HARNESS_NOT_SHIPPED",
        )
        self.assertEqual(
            got["harness_status"]["removed-harness"]["surfaces"],
            {"terminal": False, "one_shot": False, "browser": False, "sprint": False},
        )
        self.assertNotIn("removed-harness", got["harnesses"])

    def test_set_model(self) -> None:
        self._route("claude", "opus")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": "opus"})
        self.assertTrue(ok, err)
        self.assertEqual(self._row("planner", "claude")["model"], "opus")
        self.assertEqual(self._row("planner", "claude")["effort"], "high")

    def test_effort_is_canonical_and_saved_with_the_complete_route(self) -> None:
        self._route("claude", "opus-next")
        ok, err = server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "claude",
             "model": "opus-next", "effort": " LOW "},
        )
        self.assertTrue(ok, err)
        row = self._row("planner", "claude")
        self.assertEqual((row["model"], row["effort"]), ("opus-next", "low"))

    def test_model_default_persists_for_empty_effort_list_route(self) -> None:
        # Spec #160: 'default' is admitted and persisted even when the route
        # advertises no named levels; re-saving without an effort keeps it,
        # and a named level is still refused with 'default' in the details.
        self._route("codex", "gpt-plain", efforts=())
        ok, err = server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "codex",
             "model": "gpt-plain", "effort": " DEFAULT "},
        )
        self.assertTrue(ok, err)
        row = self._row("planner", "codex")
        self.assertEqual((row["model"], row["effort"]), ("gpt-plain", "default"))
        projection = server.get_flavor_defaults(self.con)["flavors"]["planner"]
        by_harness = {r["harness"]: r for r in projection}
        self.assertEqual(by_harness["codex"]["effort_state"], "controlled")
        self.assertEqual(by_harness["codex"]["effective_effort"], "default")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "codex",
                       "is_default": True})
        self.assertTrue(ok, err)
        self.assertEqual(self._row("planner", "codex")["effort"], "default")
        ok, err = server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "codex",
             "model": "gpt-plain", "effort": "high"},
        )
        self.assertFalse(ok)
        self.assertEqual(err["code"], "unsupported_thinking_level")
        self.assertEqual(err["details"]["default_effort"], "default")
        self.assertEqual(self._row("planner", "codex")["effort"], "default")

    def test_omitted_effort_resolves_by_fallback_chain(self) -> None:
        # Decision #223: saving a model without an effort resolves high where
        # advertised, else the reserved Model default — no more hard 422 on
        # unadvertised omitted-high, so no-thinking models save and bind.
        self._route("claude", "opus")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "opus"})
        self.assertTrue(ok, err)
        self.assertEqual(self._row("planner", "claude")["effort"], "high")

        self._route("codex", "gpt-plain", efforts=())
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "codex",
                       "model": "gpt-plain"})
        self.assertTrue(ok, err)
        row = self._row("planner", "codex")
        self.assertEqual((row["model"], row["effort"]), ("gpt-plain", "default"))
        projection = server.get_flavor_defaults(self.con)["flavors"]["planner"]
        by_harness = {r["harness"]: r for r in projection}
        self.assertEqual(by_harness["codex"]["effort_state"], "controlled")
        self.assertEqual(by_harness["codex"]["effective_effort"], "default")

        # A no-high route with named levels also resolves to Model default.
        self._route("codex", "gpt-lowonly", efforts=("low",))
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "codex",
                       "model": "gpt-lowonly"})
        self.assertTrue(ok, err)
        self.assertEqual(self._row("planner", "codex")["effort"], "default")

    def test_unsupported_effort_writes_nothing(self) -> None:
        before = tuple(self._row("planner", "claude"))
        self._route("claude", "opus-next")
        ok, err = server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "claude",
             "model": "opus-next", "effort": "max"},
        )
        self.assertFalse(ok)
        self.assertEqual(err["code"], "unsupported_thinking_level")
        self.assertEqual(tuple(self._row("planner", "claude")), before)

    def test_uncontrolled_states_reject_effort_and_clear_both_fields(self) -> None:
        self._route("claude", "opus")
        self.assertTrue(server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "claude",
             "model": "opus", "effort": "low"},
        )[0])
        ok, err = server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "claude",
             "model": None, "effort": "low"},
        )
        self.assertFalse(ok)
        self.assertEqual(err["code"], "unsupported_thinking_level")
        self.assertEqual(
            (self._row("planner", "claude")["model"],
             self._row("planner", "claude")["effort"]),
            ("opus", "low"),
        )
        self.assertTrue(server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "claude",
             "model": None, "effort": None},
        )[0])
        row = self._row("planner", "claude")
        self.assertEqual((row["model"], row["effort"]), (None, None))

    def test_projection_covers_controlled_and_uncontrolled_states(self) -> None:
        self._route("claude", "legacy-opus")
        self.con.execute(
            "UPDATE flavor_defaults SET model='legacy-opus',effort=NULL "
            "WHERE flavor='planner' AND harness='claude'"
        )
        self.con.execute(
            "INSERT INTO flavor_defaults (flavor,harness,model,effort,is_default) "
            "VALUES ('planner','vibe',NULL,NULL,0)"
        )
        projection = server.get_flavor_defaults(self.con)["flavors"]["planner"]
        by_harness = {row["harness"]: row for row in projection}
        self.assertEqual(by_harness["claude"]["effort_state"], "legacy-default")
        self.assertEqual(by_harness["claude"]["effective_effort"], "high")
        self.assertEqual(by_harness["vibe"]["effort_state"], "unavailable")
        self.assertIsNone(by_harness["vibe"]["effective_effort"])

    def test_star_is_transactional_across_the_flavor(self) -> None:
        self.assertTrue(server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "codex",
                       "is_default": True})[0])
        rows = self.con.execute(
            "SELECT harness, is_default FROM flavor_defaults "
            "WHERE flavor='planner'").fetchall()
        stars = {r["harness"]: r["is_default"] for r in rows}
        self.assertEqual(sum(stars.values()), 1)
        self.assertEqual(stars["codex"], 1)

    def test_upsert_missing_cell(self) -> None:
        # 'vibe' has no seeded row for planner — a write must create it
        self.assertIsNone(self._row("planner", "vibe"))
        self._route("vibe", "devstral-latest")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "vibe",
                       "model": "devstral-latest", "is_default": True})
        self.assertTrue(ok, err)
        row = self._row("planner", "vibe")
        self.assertEqual(row["model"], "devstral-latest")
        self.assertEqual(row["is_default"], 1)

    def test_empty_model_clears_to_null(self) -> None:
        self._route("claude", "opus")
        server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": "opus"})
        server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": None})
        self.assertIsNone(self._row("planner", "claude")["model"])

    def test_empty_string_is_not_harness_default(self) -> None:
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": ""})
        self.assertFalse(ok)
        self.assertEqual(err["code"], "invalid_model_route")

    def test_invalid_model_does_not_create_missing_cell(self) -> None:
        self.assertIsNone(self._row("planner", "vibe"))
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "vibe",
                       "model": "not-local"})
        self.assertFalse(ok)
        self.assertEqual(err["code"], "invalid_model_route")
        self.assertIsNone(self._row("planner", "vibe"))

    def test_model_requires_exact_available_route_for_harness(self) -> None:
        self._route("codex", "gpt-5.6-sol")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "gpt-5.6-sol"})
        self.assertFalse(ok)
        self.assertEqual(err["code"], "invalid_model_route")
        self.assertNotEqual(self._row("planner", "claude")["model"],
                            "gpt-5.6-sol")

    def test_stale_route_is_not_settable(self) -> None:
        self._route("claude", "opus-next", stale=1)
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "opus-next"})
        self.assertFalse(ok)
        self.assertEqual(err["code"], "thinking_evidence_stale")

    def test_fingerprint_drift_stales_route_and_refuses_selection(self) -> None:
        self._route("codex", "gpt-drift")
        with mock.patch.object(
            server.model_catalog, "controlled_route_evidence",
            return_value=controlled_bundle(
                "codex", "gpt-drift", "changed-fingerprint"
            ),
        ):
            ok, err = server.set_flavor_default(
                self.con,
                {"flavor": "planner", "harness": "codex", "model": "gpt-drift"},
            )

        route = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE harness='codex' AND selector='gpt-drift'"
        ).fetchone()
        self.assertFalse(ok)
        self.assertEqual(err["code"], "thinking_evidence_stale")
        self.assertEqual(route["stale"], 1)
        self.assertEqual(
            route["last_error"],
            "thinking_evidence_stale: Installed route source changed after "
            "refresh; remediation: sc models refresh",
        )
        self.assertFalse(server.model_route_available(
            self.con, "codex", "gpt-drift"
        ))
        self.assertNotEqual(self._row("planner", "codex")["model"], "gpt-drift")

    def test_age_expiry_stales_route_and_refuses_selection(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=7, hours=1)).isoformat()
        self._route("claude", "old-opus", seen_at=old)

        ok, err = server.set_flavor_default(
            self.con,
            {"flavor": "planner", "harness": "claude", "model": "old-opus"},
        )

        route = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE harness='claude' AND selector='old-opus'"
        ).fetchone()
        self.assertFalse(ok)
        self.assertEqual(err["code"], "thinking_evidence_stale")
        self.assertEqual(route["stale"], 1)
        self.assertEqual(
            route["last_error"],
            "thinking_evidence_stale: Route evidence is older than 7 days; "
            "remediation: sc models refresh",
        )
        self.assertFalse(server.model_route_available(
            self.con, "claude", "old-opus"
        ))
        self.assertNotEqual(self._row("planner", "claude")["model"], "old-opus")

    def test_unknown_names_and_empty_writes_are_loud(self) -> None:
        self.assertFalse(server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "emacs", "model": "x"})[0])
        self.assertFalse(server.set_flavor_default(
            self.con, {"flavor": "nope", "harness": "claude", "model": "x"})[0])
        self.assertFalse(server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude"})[0])


class PatchShellTest(unittest.TestCase):
    """server.patch_shell — display_name rename + the strictly-guarded
    system_prompt H1 re-stamp (creation-time render only, never curation)."""

    def setUp(self) -> None:
        self.con = build_db()

    def tearDown(self) -> None:
        self.con.close()

    def _mk(self, name, prompt) -> int:
        sid = self.con.execute(
            "INSERT INTO shells (display_name, system_prompt) VALUES (?, ?)",
            (name, prompt)).lastrowid
        self.con.commit()
        return sid

    def _shell(self, sid):
        return self.con.execute(
            "SELECT display_name, system_prompt, current_state FROM shells "
            "WHERE shell_id=?", (sid,)).fetchone()

    def test_rename_restamps_pristine_h1(self) -> None:
        sid = self._mk("DEV1", "# DEV1 — dev shell, working repo\n\nfocus")
        ok, err = server.patch_shell(self.con, sid, {"display_name": "Forge"})
        self.assertTrue(ok, err)
        row = self._shell(sid)
        self.assertEqual(row["display_name"], "Forge")
        self.assertEqual(row["system_prompt"],
                         "# Forge — dev shell, working repo\n\nfocus")

    def test_rename_never_touches_curated_prompt(self) -> None:
        # H1 no longer carries the creation-time name → shell curation, no door
        sid = self._mk("DEV1", "# The Floorwright\n\nmy own words")
        ok, _ = server.patch_shell(self.con, sid, {"display_name": "Forge"})
        self.assertTrue(ok)
        row = self._shell(sid)
        self.assertEqual(row["display_name"], "Forge")
        self.assertEqual(row["system_prompt"], "# The Floorwright\n\nmy own words")

    def test_rename_trims_whitespace(self) -> None:
        sid = self._mk("DEV1", "x")
        ok, _ = server.patch_shell(self.con, sid, {"display_name": "  Forge  "})
        self.assertTrue(ok)
        self.assertEqual(self._shell(sid)["display_name"], "Forge")

    def test_empty_and_nonstring_names_rejected(self) -> None:
        sid = self._mk("DEV1", "x")
        for bad in ("", "   ", None, 7):
            ok, err = server.patch_shell(self.con, sid, {"display_name": bad})
            self.assertFalse(ok)
            self.assertIn("non-empty", err)
        self.assertEqual(self._shell(sid)["display_name"], "DEV1")

    def test_missing_shell_is_not_found(self) -> None:
        ok, err = server.patch_shell(self.con, 999999, {"display_name": "X"})
        self.assertFalse(ok)
        self.assertEqual(err, "not found")

    def test_current_state_path_unchanged(self) -> None:
        sid = self._mk("DEV1", "x")
        ok, _ = server.patch_shell(self.con, sid, {"current_state": "building"})
        self.assertTrue(ok)
        self.assertEqual(self._shell(sid)["current_state"], "building")

    def test_system_prompt_stays_doorless(self) -> None:
        sid = self._mk("DEV1", "x")
        ok, err = server.patch_shell(self.con, sid, {"system_prompt": "hijack"})
        self.assertFalse(ok)
        self.assertEqual(self._shell(sid)["system_prompt"], "x")


class AuthenticatedCliCatalogueRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "engine.db"
        source = build_db()
        ids = seed(source)
        source.execute(
            "UPDATE shells SET api_key='shell-token' WHERE shell_id=?",
            (ids["shell_id"],),
        )
        source.execute(
            "INSERT INTO model_catalog_generations (generation_id,payload_version,"
            "contract_version,started_at,completed_at,state,runtime,source_summary,"
            "harness_versions,source_fingerprints,error_summary,payload_digest) "
            "VALUES (? ,6,2,datetime('now'),datetime('now'),'successful','host',"
            "'[]','{}','{}',NULL,?)",
            ("a" * 32, "b" * 64),
        )
        source.execute(
            "INSERT INTO model_routes (harness,selector,source,availability,"
            "headless_supported,high_effort_supported,supported_efforts,"
            "provider_model,cli_version,last_seen_at,generation_id,"
            "source_fingerprint,harness_version,harness_compatibility,"
            "evidence_kind,evidence_digest,selector_binding,effort_metadata,"
            "adapter_metadata) VALUES "
            "('codex','api-model','api-source-v1','available',1,1,'[\"high\"]',"
            "'api-provider-model','codex 0.145.0',datetime('now'),?,?,"
            "'0.145.0','verified','codex-model-cache',?,?,?, '{}')",
            (
                "a" * 32, "current-fingerprint", "c" * 64,
                '{"kind":"exact-model","selector":"api-model"}',
                json.dumps({
                    "supported": ["high"], "default": "high",
                    "digests": {"high": "d" * 64},
                    "native_variant_ids": {},
                }, separators=(",", ":"), sort_keys=True),
            ),
        )
        source.commit()
        target = sqlite3.connect(self.path)
        source.backup(target)
        target.close()
        source.close()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def request(self, path: str, token: str | None = "shell-token", *,
                fingerprint="current-fingerprint", runtime_status=None):
        headers = "Host: 127.0.0.1"
        if token is not None:
            headers += f"\r\nAuthorization: Bearer {token}"
        probe = {"side_effect": fingerprint} if callable(fingerprint) else {
            "return_value": fingerprint
        }
        with (
            mock.patch.object(server, "db", side_effect=self.connect),
            mock.patch.object(
                server.model_catalog,
                "current_source_fingerprint",
                **probe,
            ),
            mock.patch.object(
                server.model_catalog,
                "harness_runtime_status",
                return_value=(
                    compatible_runtime()
                    if runtime_status is None else runtime_status
                ),
            ),
        ):
            status, _headers, body = server.dispatch_http("GET", path, headers, b"")
        return status, json.loads(body)

    def publish_successor(self) -> str:
        fingerprint = "successor-fingerprint"
        completed = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        with self.connect() as con:
            con.execute(
                "INSERT INTO model_catalog_generations (generation_id,"
                "payload_version,contract_version,started_at,completed_at,state,"
                "runtime,source_summary,harness_versions,source_fingerprints,"
                "error_summary,payload_digest) VALUES (?,6,2,?,?,"
                "'successful','host','[]','{}','{}',NULL,?)",
                ("f" * 32, completed, completed, "e" * 64),
            )
            con.execute(
                "UPDATE model_routes SET generation_id=?,source_fingerprint=?,"
                "cli_version='codex 0.145.0',harness_version='0.145.0',"
                "last_seen_at=?,stale=0,last_error=NULL "
                "WHERE harness='codex' AND selector='api-model'",
                ("f" * 32, fingerprint, completed),
            )
        return fingerprint

    def test_model_routes_require_shell_auth_and_apply_exact_filters(self) -> None:
        self.assertEqual(self.request("/_sc/model-routes", None)[0], 401)
        self.assertEqual(self.request("/_sc/model-routes", "wrong")[0], 401)

        status, body = self.request(
            "/_sc/model-routes?harness=codex&selector=api-model"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["routes"]), 1)
        self.assertEqual(body["routes"][0]["source"], "api-source-v1")
        self.assertNotIn("current_source_fingerprint", body["routes"][0])

        con = self.connect()
        con.execute(
            "UPDATE model_routes SET source='api-source-v2' "
            "WHERE harness='codex' AND selector='api-model'"
        )
        con.commit()
        con.close()
        status, current = self.request(
            "/_sc/model-routes?harness=codex&selector=api-model"
        )
        self.assertEqual(status, 200, current)
        self.assertEqual(current["routes"][0]["source"], "api-source-v2")

    def test_route_projection_does_not_claim_api_host_runtime_evidence(self) -> None:
        status, unavailable = self.request(
            "/_sc/model-routes?harness=claude",
            runtime_status={
                "version": None, "compatibility": None,
                "error": "HARNESS_UNAVAILABLE",
            },
        )

        self.assertEqual(status, 200, unavailable)
        self.assertEqual(unavailable["routes"], [])
        self.assertNotIn("runtime_status", unavailable)

    def test_route_projection_carries_latest_bounded_harness_failure(self) -> None:
        completed = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        con = self.connect()
        con.execute(
            "INSERT INTO model_catalog_generations (generation_id,"
            "payload_version,contract_version,started_at,completed_at,state,"
            "runtime,source_summary,harness_versions,source_fingerprints,"
            "error_summary,payload_digest) VALUES (?,8,2,?,?,"
            "'successful','sandbox','[]','{}','{}',?,?)",
            (
                "d" * 32,
                completed,
                completed,
                json.dumps({
                    "error": None,
                    "errors": [],
                    "harness_errors": {
                        "deepseek": "authenticated DeepSeek exact model is absent"
                    },
                }),
                "c" * 64,
            ),
        )
        con.commit()
        con.close()

        status, body = self.request(
            "/_sc/model-routes?harness=deepseek&selector="
            "ollama-cloud%2Fdeepseek-v4-pro%3A0813"
        )

        self.assertEqual(200, status)
        self.assertEqual([], body["routes"])
        self.assertEqual(
            "authenticated DeepSeek exact model is absent",
            body["harness_error"],
        )

    def test_real_advisory_vibe_catalogue_resolves_locally_and_via_api(self) -> None:
        vibe_status = compatible_runtime()
        run = mock.Mock()
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        def fetch(url, _headers=None):
            if url == server.model_catalog.MODELS_DEV_URL:
                return {"mistral": {"models": {
                    "devstral-latest": {
                        "name": "Devstral", "release_date": "2026-01-01",
                    },
                }}}
            raise AssertionError(f"unexpected catalogue request: {url}")

        con = self.connect()
        self.addCleanup(con.close)
        with mock.patch.object(
            server.model_catalog, "CACHE",
            Path(self.tmp.name) / "model_catalog.json",
        ):
            refreshed = server.model_catalog.catalog(
                refresh=True, fetch=fetch, env={}, run=run, con=con,
                opencode_provider=lambda: [],
                harness_probe=lambda: {"vibe": vibe_status},
            )
            route = dict(con.execute(
                "SELECT * FROM model_routes "
                "WHERE harness='vibe' AND selector='devstral-latest'"
            ).fetchone())
            with mock.patch.object(
                routes_cli.model_catalog, "harness_runtime_status",
                return_value=vibe_status,
            ):
                local = routes_cli.resolve(con, "vibe", "devstral-latest")

        def api(method, path):
            self.assertEqual(method, "GET")
            self.assertEqual(
                path,
                "/_sc/model-routes?harness=vibe&selector=devstral-latest",
            )
            api_status, body = self.request(
                path, runtime_status=vibe_status
            )
            self.assertEqual(api_status, 200, body)
            return body

        output = io.StringIO()
        with (
            mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
            mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
            mock.patch.object(routes_cli.mem, "_api", side_effect=api),
            mock.patch.object(
                routes_cli.model_catalog, "harness_runtime_status",
                return_value=vibe_status,
            ),
            mock.patch.object(
                routes_cli, "_open_db", side_effect=AssertionError("opened DB")
            ),
            contextlib.redirect_stdout(output),
        ):
            exit_code = routes_cli.main([
                "resolve", "vibe", "devstral-latest", "--json",
            ])
        authenticated = json.loads(output.getvalue())

        self.assertFalse(refreshed["stale"])
        self.assertEqual(route["availability"], "advisory")
        self.assertEqual(route["headless_supported"], 0)
        self.assertIsNone(route["evidence_kind"])
        self.assertIsNone(route["harness_version"])
        self.assertIsNone(route["harness_compatibility"])
        self.assertEqual(
            route["generation_id"], refreshed["catalogue_generation"]
        )
        self.assertTrue(local["ok"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(authenticated["ok"])
        self.assertEqual(local["binding"], authenticated["binding"])
        self.assertEqual(local["binding_digest"], authenticated["binding_digest"])
        self.assertIsNone(local["binding"]["requested_effort"])
        self.assertIsNone(local["binding"]["catalogue_generation"])
        self.assertNotIn("--effort", local["command"])
        self.assertNotIn("--effort", authenticated["command"])

    def test_skill_catalogue_requires_auth_and_includes_grant_scopes(self) -> None:
        self.assertEqual(self.request("/_sc/skills", None)[0], 401)
        status, body = self.request("/_sc/skills")
        self.assertEqual(status, 200, body)
        skill = next(
            row for row in body["skills"] if row["name"] == "local_only_skill"
        )
        self.assertEqual(
            skill["grant_scopes"], ["flavor:dev", "shell:custom"]
        )

    def test_exact_route_read_is_a_durable_projection_not_a_host_probe(self) -> None:
        with (
            mock.patch.object(server, "db", side_effect=self.connect),
            mock.patch.object(
                server.model_catalog, "current_source_fingerprint",
                side_effect=AssertionError("API host source probe ran"),
            ),
            mock.patch.object(
                server.model_catalog, "harness_runtime_status",
                side_effect=AssertionError("API host runtime probe ran"),
            ),
        ):
            status, _headers, raw = server.dispatch_http(
                "GET", "/_sc/model-routes?harness=codex&selector=api-model",
                "Host: 127.0.0.1\r\nAuthorization: Bearer shell-token", b"",
            )
        body = json.loads(raw)
        route = body["routes"][0]
        with self.connect() as con:
            stored = con.execute(
                "SELECT stale,last_error FROM model_routes "
                "WHERE harness='codex' AND selector='api-model'"
            ).fetchone()

        self.assertEqual(status, 200, body)
        self.assertEqual(route["stale"], 0)
        self.assertNotIn("current_source_fingerprint", route)
        self.assertNotIn("route_resolution_error", route)
        self.assertEqual(tuple(stored), (0, None))

    def test_authenticated_controlled_resolution_uses_shell_runtime_only(self) -> None:
        def api(method, path):
            self.assertEqual(method, "GET")
            status, body = self.request(path)
            self.assertEqual(status, 200, body)
            return body

        output = io.StringIO()
        missing_status = {
            "harness": "codex",
            **routes_cli.model_catalog.harness_versions.runtime_scope(),
            "version": None,
            "compatibility": None,
            "minimum_version": "0.145.0",
            "maximum_version_exclusive": "0.147.0",
            "verified_version": "0.145.0",
            "error": "HARNESS_UNAVAILABLE",
        }
        with (
            mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
            mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
            mock.patch.object(routes_cli.mem, "_api", side_effect=api),
            mock.patch.object(
                routes_cli, "_open_db", side_effect=AssertionError("opened DB")
            ),
            mock.patch.object(
                routes_cli.model_catalog, "controlled_route_evidence",
                return_value=controlled_bundle(
                    "codex", "api-model", "current-fingerprint",
                    status=missing_status,
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            exit_code = routes_cli.main([
                "resolve", "Codex", "api-model", "--json",
            ])
        result = json.loads(output.getvalue())
        with self.connect() as con:
            stored = con.execute(
                "SELECT stale,last_error "
                "FROM model_routes WHERE harness='codex' "
                "AND selector='api-model'"
            ).fetchone()

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["code"], "thinking_evidence_stale")
        self.assertNotIn("binding", result)
        self.assertNotIn("binding_digest", result)
        self.assertNotIn("command", result)
        self.assertEqual(tuple(stored), (0, None))

    def test_flavor_route_check_retains_pre_probe_identity(self) -> None:
        con = self.connect()
        self.addCleanup(con.close)
        before = con.execute(
            "SELECT model FROM flavor_defaults "
            "WHERE flavor='planner' AND harness='codex'"
        ).fetchone()[0]
        successor = None

        def publish_after_probe(*_args, **_kwargs):
            nonlocal successor
            successor = self.publish_successor()
            return controlled_bundle(
                "codex", "api-model", "current-fingerprint"
            )

        with (
            mock.patch.object(
                server.model_catalog, "controlled_route_evidence",
                side_effect=publish_after_probe,
            ),
        ):
            ok, err = server.set_flavor_default(con, {
                "flavor": "planner", "harness": "codex", "model": "api-model",
            })

        stored = con.execute(
            "SELECT generation_id,source_fingerprint,stale,last_error "
            "FROM model_routes WHERE harness='codex' AND selector='api-model'"
        ).fetchone()
        unchanged = con.execute(
            "SELECT model FROM flavor_defaults "
            "WHERE flavor='planner' AND harness='codex'"
        ).fetchone()[0]
        self.assertFalse(ok)
        self.assertEqual(err["code"], "thinking_evidence_stale")
        self.assertEqual(tuple(stored), ("f" * 32, successor, 0, None))
        self.assertEqual(unchanged, before)

        with (
            mock.patch.object(
                server.model_catalog, "controlled_route_evidence",
                return_value=controlled_bundle(
                    "codex", "api-model", successor
                ),
            ),
        ):
            retry_ok, retry_err = server.set_flavor_default(con, {
                "flavor": "planner", "harness": "codex", "model": "api-model",
            })
        selected = con.execute(
            "SELECT model FROM flavor_defaults "
            "WHERE flavor='planner' AND harness='codex'"
        ).fetchone()[0]
        self.assertTrue(retry_ok, retry_err)
        self.assertIsNone(retry_err)
        self.assertEqual(selected, "api-model")

    def test_catalogue_filters_reject_unknown_or_repeated_input(self) -> None:
        self.assertEqual(
            self.request("/_sc/model-routes?sort=source")[0], 400
        )
        self.assertEqual(
            self.request("/_sc/model-routes?harness=codex&harness=kimi")[0],
            400,
        )
        self.assertEqual(self.request("/_sc/skills?harness=codex")[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
