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

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402  (server.py adds scripts/ to the path on import)


def build_db() -> sqlite3.Connection:
    """Fresh in-memory DB: schema.sql + every migration, FK enforcement on."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for path in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(path.read_text())
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

    def _row(self, flavor, harness):
        return self.con.execute(
            "SELECT model, is_default FROM flavor_defaults "
            "WHERE flavor=? AND harness=?", (flavor, harness)).fetchone()

    def _route(self, harness, selector, *, availability="available", stale=0):
        self.con.execute(
            "INSERT INTO model_routes "
            "(harness, selector, source, availability, last_seen_at, stale) "
            "VALUES (?, ?, 'test', ?, datetime('now'), ?)",
            (harness, selector, availability, stale))
        self.con.commit()

    def test_matrix_includes_template_flavors_and_harnesses(self) -> None:
        got = server.get_flavor_defaults(self.con)
        self.assertIn("planner", got["flavors"])
        self.assertIn("admin", got["flavors"], "template flavors appear even unseeded")
        for h in ("claude", "codex", "opencode", "vibe"):
            self.assertIn(h, got["harnesses"])

    def test_set_model(self) -> None:
        self._route("claude", "opus")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": "opus"})
        self.assertTrue(ok, err)
        self.assertEqual(self._row("planner", "claude")["model"], "opus")

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
        self.assertIn("invalid_model_route", err)

    def test_invalid_model_does_not_create_missing_cell(self) -> None:
        self.assertIsNone(self._row("planner", "vibe"))
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "vibe",
                       "model": "not-local"})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)
        self.assertIsNone(self._row("planner", "vibe"))

    def test_model_requires_exact_available_route_for_harness(self) -> None:
        self._route("codex", "gpt-5.6-sol")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "gpt-5.6-sol"})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)
        self.assertNotEqual(self._row("planner", "claude")["model"],
                            "gpt-5.6-sol")

    def test_stale_route_is_not_settable(self) -> None:
        self._route("claude", "opus-next", stale=1)
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "opus-next"})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)

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
            "INSERT INTO model_routes (harness,selector,source,availability,"
            "headless_supported,high_effort_supported,supported_efforts,"
            "last_seen_at) VALUES "
            "('codex','api-model','api-source-v1','available',1,1,'[\"high\"]',"
            "datetime('now'))"
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

    def request(self, path: str, token: str | None = "shell-token"):
        headers = "Host: 127.0.0.1"
        if token is not None:
            headers += f"\r\nAuthorization: Bearer {token}"
        with mock.patch.object(server, "db", side_effect=self.connect):
            status, _headers, body = server.dispatch_http("GET", path, headers, b"")
        return status, json.loads(body)

    def test_model_routes_require_shell_auth_and_apply_exact_filters(self) -> None:
        self.assertEqual(self.request("/_sc/model-routes", None)[0], 401)
        self.assertEqual(self.request("/_sc/model-routes", "wrong")[0], 401)

        status, body = self.request(
            "/_sc/model-routes?harness=codex&selector=api-model"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["routes"]), 1)
        self.assertEqual(body["routes"][0]["source"], "api-source-v1")

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
