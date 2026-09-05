#!/usr/bin/env python3
"""Task context projection — `sc context --task|--work-unit` (spec doc #187).

The contract under test:
  • six sections, from direct relations only: linked tasks, governing document
    identity + hash (current outside a Sprint, immutable bound revision inside),
    active linked decisions once, feature-level flags labeled as such, direct
    dependencies, unit-scoped blockers by pointer;
  • access: `--task` for any authenticated shell, `--work-unit` only for the
    assigned Developer or an Admin, bounded refusal otherwise; unknown ids 404;
  • boundaries render usable absolute paths and known walls, never shorthand or
    invented permissions; unavailable facts are absent, not fabricated;
  • resources describe the map + declared hooks without preloading rows;
  • nothing is written; no body, rationale, or index is embedded;
  • the CLI renders the payload, passes launcher runtime facts, and is
    registered in the dispatcher; guidance and the assignment wake carry the
    exact command.

Run:
    python3 tests/test_task_context.py
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import compose
import map_db
import mem
import seed_skills
import server
import sprint_domain
import task_context as tc

DEV_TOKEN, REV_TOKEN, PLN_TOKEN, ADM_TOKEN, OTHER_TOKEN = (
    "tok-dev", "tok-rev", "tok-pln", "tok-adm", "tok-other")
SPEC_BODY = "# Governing spec\n\nThe complete body that must never be embedded."


def build_engine_db(path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for p in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
    con.executemany(
        "INSERT INTO shells (shell_id,display_name,shortname,flavor,system_prompt,"
        "user_id,api_key) VALUES (?,?,?,?,'p',1,?)",
        (
            (1, "Developer", "DEV1", "dev", DEV_TOKEN),
            (2, "Reviewer", "REV1", "reviewer", REV_TOKEN),
            (3, "Planner", "PLN1", "planner", PLN_TOKEN),
            (4, "Admin", "ADM", "admin", ADM_TOKEN),
            (5, "Other dev", "DEV2", "dev", OTHER_TOKEN),
        ),
    )
    con.commit()
    return con


def seed_feature(con, *, decisions=True, flags=True, frozen=False) -> dict:
    fid = con.execute(
        "INSERT INTO roadmap (title,roadmap_status,summary) "
        "VALUES ('Feature','in_progress','Feature summary')").lastrowid
    other = con.execute(
        "INSERT INTO roadmap (title,roadmap_status) VALUES ('Other','next')").lastrowid
    did = con.execute(
        "INSERT INTO documents (feature_id,kind,seq,title,body,frozen) "
        "VALUES (?,'spec',1,'Spec',?,?)", (fid, SPEC_BODY, 1 if frozen else 0)).lastrowid
    tasks = [con.execute(
        "INSERT INTO spec_tasks (feature_id,document_id,seq,title,description,status) "
        "VALUES (?,?,?,?,?,?)", (fid, did, seq, title, desc, status)).lastrowid
        for seq, title, desc, status in (
            (0, "Preparation", "Read the code paths", "done"),
            (1, "Build", "Build the projector", "pending"),
            (2, "Verification", "", "pending"),
        )]
    ids = {"feature": fid, "other": other, "doc": did, "tasks": tasks}
    if decisions:
        old = con.execute(
            "INSERT INTO shell_decisions (shell_id,decision,rationale,priority,"
            "decision_date,feature_id) VALUES (3,'old rule','why','M','2026-01-01',?)",
            (fid,)).lastrowid
        ids["superseding"] = con.execute(
            "INSERT INTO shell_decisions (shell_id,decision,rationale,priority,"
            "decision_date,feature_id,parent_decision_id) "
            "VALUES (3,'new rule by feature','secret rationale','M','2026-01-02',?,?)",
            (fid, old)).lastrowid
        ids["superseded"] = old
        ids["doc_decision"] = con.execute(
            "INSERT INTO shell_decisions (shell_id,decision,rationale,priority,"
            "decision_date,document_id) VALUES (3,?,'why','M','2026-01-03',?)",
            ("doc rule " + "x" * 300, did)).lastrowid
        ids["unrelated_decision"] = con.execute(
            "INSERT INTO shell_decisions (shell_id,decision,rationale,priority,"
            "decision_date,feature_id) VALUES (3,'unrelated','why','M','2026-01-04',?)",
            (other,)).lastrowid
        ids["deleted_decision"] = con.execute(
            "INSERT INTO shell_decisions (shell_id,decision,rationale,priority,"
            "decision_date,feature_id,is_deleted) "
            "VALUES (3,'deleted','why','M','2026-01-05',?,1)", (fid,)).lastrowid
    if flags:
        ids["open_flag"] = con.execute(
            "INSERT INTO flags (display_name,priority,description,feature_id,shell_id) "
            "VALUES ('SC-1','High',?,?,3)", ("open flag " + "y" * 300, fid)).lastrowid
        ids["resolved_flag"] = con.execute(
            "INSERT INTO flags (display_name,priority,description,feature_id,shell_id,"
            "resolved) VALUES ('SC-2','Low','resolved flag',?,3,1)", (fid,)).lastrowid
        ids["unlinked_flag"] = con.execute(
            "INSERT INTO flags (display_name,priority,description,shell_id) "
            "VALUES ('SC-3','Medium','fleet flag',3)").lastrowid
        ids["other_flag"] = con.execute(
            "INSERT INTO flags (display_name,priority,description,feature_id,shell_id) "
            "VALUES ('SC-4','Medium','other feature flag',?,3)", (other,)).lastrowid
    con.commit()
    return ids


def seed_sprint(con, ids, *, lifecycle="armed", disposition="active",
                output_kind="code", pending_assignment=True, blocker=True) -> dict:
    fid, did = ids["feature"], ids["doc"]
    revision = hashlib.sha256(SPEC_BODY.encode()).hexdigest()
    approval = con.execute(
        "INSERT INTO sprint_spec_approvals (document_id,revision_sha256,"
        "reviewer_shell_id,verdict) VALUES (?,?,2,'pass')", (did, revision)).lastrowid
    sid = con.execute(
        "INSERT INTO sprints (feature_id,originating_planner_shell_id,"
        "merge_grant_enabled) VALUES (?,3,1)", (fid,)).lastrowid
    con.execute(
        "INSERT INTO sprint_specs (sprint_id,document_id,bound_revision_sha256,"
        "approval_id,bound_revision_body,bound_revision_legacy) VALUES (?,?,?,?,?,0)",
        (sid, did, revision, approval, SPEC_BODY))
    for gen in (1, 2):
        con.execute(
            "INSERT INTO sprint_spec_revision_history (sprint_id,document_id,generation,"
            "bound_revision_sha256,bound_revision_body,bound_revision_legacy,actor_kind,"
            "reason) VALUES (?,?,?,?,?,0,'system','bind')",
            (sid, did, gen, revision, SPEC_BODY))
    participants = {}
    for shell_id, role in ((3, "planner"), (1, "developer"), (2, "reviewer")):
        participants[role] = con.execute(
            "INSERT INTO sprint_participants (sprint_id,shell_id,role,harness) "
            "VALUES (?,?,?,'codex')", (sid, shell_id, role)).lastrowid
    for step in {"armed": ("armed",), "paused": ("armed", "paused")}.get(lifecycle, ()):
        con.execute("UPDATE sprints SET conformance_reviewer_shell_id=2,"
                    "conformance_owner_generation=1,lifecycle=?,"
                    "armed_at=COALESCE(armed_at,datetime('now')),"
                    "paused_at=CASE WHEN ?='paused' THEN datetime('now') END "
                    "WHERE sprint_id=?", (step, step, sid))
    dep = con.execute(
        "INSERT INTO sprint_work_units (sprint_id,assigned_shell_id,reviewer_shell_id,"
        "title,expected_output,disposition,planned_wave) "
        "VALUES (?,5,2,'Foundation','Ship the foundation','completed',1)", (sid,)).lastrowid
    unit = con.execute(
        "INSERT INTO sprint_work_units (sprint_id,assigned_shell_id,reviewer_shell_id,"
        "title,expected_output,disposition,planned_wave,output_kind) "
        "VALUES (?,1,2,'Projector lane','Deliver the projector with tests',?,2,?)",
        (sid, disposition, output_kind)).lastrowid
    con.execute(
        "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
        (sid, unit, ids["tasks"][1]))
    con.execute(
        "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
        (sid, dep, ids["tasks"][0]))
    con.execute(
        "INSERT INTO sprint_work_unit_dependencies (sprint_id,work_unit_id,"
        "depends_on_work_unit_id) VALUES (?,?,?)", (sid, unit, dep))
    out = {"sprint": sid, "unit": unit, "dep": dep, "revision": revision}
    if pending_assignment:
        out["assignment"] = con.execute(
            "INSERT INTO wake_message (sprint_id,sender_shell_id,receiver_shell_id,"
            "from_participant_id,to_participant_id,work_unit_id,message_kind,body,"
            "declared_type,actionable,disposition,idempotency_key) "
            "VALUES (?,3,1,?,?,?,'work_assignment',?,'new',1,'pending','assign-1')",
            (sid, participants["planner"], participants["developer"], unit,
             sprint_domain.assignment_body(
                 {"title": "Projector lane",
                  "expected_output": "Deliver the projector with tests",
                  "work_unit_id": unit}))).lastrowid
    if blocker:
        out["blocker"] = con.execute(
            "INSERT INTO wake_message (sprint_id,sender_shell_id,receiver_shell_id,"
            "from_participant_id,to_participant_id,work_unit_id,message_kind,body,"
            "declared_type,actionable,disposition,idempotency_key,intent,requires_reply) "
            "VALUES (?,1,3,?,?,?,'notification','SECRET blocker body','new',1,'pending',"
            "'block-1','blocker',1)",
            (sid, participants["developer"], participants["planner"], unit)).lastrowid
        answered = con.execute(
            "INSERT INTO wake_message (sprint_id,sender_shell_id,receiver_shell_id,"
            "from_participant_id,to_participant_id,work_unit_id,message_kind,body,"
            "declared_type,actionable,disposition,idempotency_key,intent,requires_reply) "
            "VALUES (?,1,3,?,?,?,'notification','answered blocker','new',1,'pending',"
            "'block-2','blocker',1)",
            (sid, participants["developer"], participants["planner"], unit)).lastrowid
        con.execute(
            "INSERT INTO wake_message (sprint_id,sender_shell_id,receiver_shell_id,"
            "from_participant_id,to_participant_id,work_unit_id,message_kind,body,"
            "declared_type,actionable,disposition,idempotency_key,intent,"
            "reply_to_message_id) VALUES (?,3,1,?,?,?,'notification','the answer','new',"
            "0,NULL,'reply-2','information',?)",
            (sid, participants["planner"], participants["developer"], unit, answered))
    con.commit()
    return out


def build_map_db(root="/mapped/host/repo") -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "map_schema.sql").read_text())
    con.execute(
        "INSERT INTO dr_repo (repo_id,name,root,default_branch,file_count,mapped_at) "
        "VALUES (1,'repo',?,'main',42,'2026-09-01T10:00:00')", (root,))
    con.execute("INSERT INTO dr_endpoint (method,path,handler) VALUES ('GET','/x','f:1')")
    con.commit()
    return con


class ProjectorTest(unittest.TestCase):
    def setUp(self):
        self.con = build_engine_db(":memory:")
        self.addCleanup(self.con.close)
        self.runtime = {"worktree": "/abs/worktrees/dev1", "seat": "host",
                        "branch": "feat/projection"}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)          # a real, empty checkout: no dev-kit

    def project(self, **kw):
        kw.setdefault("caller_shell_id", 1)
        kw.setdefault("repo_root", self.root)
        kw.setdefault("runtime", self.runtime)
        return tc.project(self.con, **kw)

    # ── task selector ────────────────────────────────────────────────────
    def test_task_projection_is_six_sections_from_direct_relations(self):
        ids = seed_feature(self.con)
        p = self.project(task_id=ids["tasks"][1])
        self.assertEqual(tuple(p), tc.SECTIONS)

        a = p["assignment"]
        self.assertEqual(a["selector"], "task")
        self.assertEqual([t["task_id"] for t in a["tasks"]], [ids["tasks"][1]])
        self.assertEqual(a["feature"]["feature_id"], ids["feature"])
        self.assertEqual(a["feature"]["roadmap_status"], "in_progress")
        self.assertEqual(p["goal"]["primary"], "Build the projector")
        self.assertNotIn("insufficient", p["goal"])

        au = p["authority"]
        doc, = au["documents"]
        self.assertEqual(doc["revision"], "current")
        self.assertEqual(doc["sha256"], hashlib.sha256(SPEC_BODY.encode()).hexdigest())
        self.assertEqual(doc["read"], f"sc mem get documents --doc {ids['doc']}")
        self.assertEqual([d["decision_id"] for d in au["decisions"]],
                         [ids["superseding"], ids["doc_decision"]])
        self.assertEqual([d["linked_by"] for d in au["decisions"]], ["feature", "document"])
        for d in au["decisions"]:
            self.assertEqual(d["provenance"], "current decision")
            self.assertLessEqual(len(d["statement"]), tc.ONE_LINE)
            self.assertEqual(d["read"], f"sc mem get decisions {d['decision_id']}")
        self.assertFalse(au["body_included"])

        flags = p["blockers"]["feature_flags"]
        self.assertEqual([f["flag_id"] for f in flags], [ids["open_flag"]])
        self.assertEqual(flags[0]["scope"], "feature-level")
        self.assertLessEqual(len(flags[0]["description"]), tc.ONE_LINE)
        self.assertNotIn("dependencies", p["blockers"])

        dumped = json.dumps(p)
        self.assertNotIn(SPEC_BODY, dumped)
        self.assertNotIn("secret rationale", dumped)
        for absent in ("unrelated", "deleted", "old rule", "resolved flag", "fleet flag",
                       "other feature flag"):
            self.assertNotIn(absent, dumped)

    def test_task_boundaries_carry_runtime_and_walls(self):
        ids = seed_feature(self.con)
        bo = self.project(task_id=ids["tasks"][1])["boundaries"]
        self.assertEqual(bo["locations"], {
            "worktree": "/abs/worktrees/dev1", "repo_root": str(self.root),
            "shared": f"{self.root}/shared"})
        self.assertEqual(bo["seat"], "host")
        self.assertEqual(bo["role"], "dev")
        self.assertEqual(bo["git"]["branch"], "feat/projection")
        self.assertEqual(bo["git"]["base"], "origin/main")
        self.assertIn(f"sc mem task start {ids['tasks'][1]}", " ".join(bo["walls"]))
        self.assertIn("merging a PR: the FnB's gate", bo["reserved"])
        self.assertEqual(bo["actions"], [])

        done = self.project(task_id=ids["tasks"][0])["boundaries"]
        self.assertTrue(any(w.startswith("already done") for w in done["walls"]))

    def test_frozen_document_is_a_wall(self):
        ids = seed_feature(self.con, frozen=True)
        p = self.project(task_id=ids["tasks"][1])
        self.assertTrue(p["authority"]["documents"][0]["frozen"])
        self.assertTrue(any("frozen" in w for w in p["boundaries"]["walls"]))

    def test_no_decisions_no_flags_renders_none(self):
        ids = seed_feature(self.con, decisions=False, flags=False)
        p = self.project(task_id=ids["tasks"][1])
        self.assertEqual(p["authority"]["decisions"], [])
        self.assertEqual(p["blockers"]["feature_flags"], [])
        text = tc.render(p)
        self.assertIn("active decisions: none linked", text)
        self.assertIn("none known", text)

    def test_empty_goal_names_the_exact_spec_read(self):
        ids = seed_feature(self.con)
        p = self.project(task_id=ids["tasks"][2])
        self.assertIn(f"sc mem get documents --doc {ids['doc']}", p["goal"]["insufficient"])
        self.assertIn("no authored goal", tc.render(p))

    def test_selector_errors(self):
        ids = seed_feature(self.con)
        with self.assertRaises(tc.ContextError) as ctx:
            self.project(task_id=999)
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(tc.ContextError) as ctx:
            self.project(work_unit_id=999)
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(tc.ContextError) as ctx:
            self.project()
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(tc.ContextError) as ctx:
            self.project(task_id=ids["tasks"][1], work_unit_id=1)
        self.assertEqual(ctx.exception.status, 400)

    # ── work-unit selector ───────────────────────────────────────────────
    def test_work_unit_projection_uses_bound_revision_and_linked_rows_only(self):
        ids = seed_feature(self.con)
        sp = seed_sprint(self.con, ids)
        p = self.project(work_unit_id=sp["unit"])

        a = p["assignment"]
        self.assertEqual(a["selector"], "work_unit")
        self.assertEqual(a["sprint_id"], sp["sprint"])
        self.assertEqual(a["work_unit"]["assigned"], "DEV1")
        self.assertEqual([t["task_id"] for t in a["tasks"]], [ids["tasks"][1]])

        g = p["goal"]
        self.assertEqual(g["primary"], "Deliver the projector with tests")
        self.assertEqual(g["acceptance_slices"],
                         [{"task_id": ids["tasks"][1], "description": "Build the projector"}])

        doc, = p["authority"]["documents"]
        self.assertEqual(doc["revision"], "immutable Sprint revision")
        self.assertEqual(doc["sha256"], sp["revision"])
        self.assertEqual(doc["generation"], 2)
        self.assertFalse(doc["legacy"])
        self.assertEqual(doc["read"],
                         f"sc sprint spec-revision --sprint {sp['sprint']} --document {ids['doc']}")
        self.assertIn("do not silently amend", p["authority"]["note"])
        self.assertEqual([d["decision_id"] for d in p["authority"]["decisions"]],
                         [ids["superseding"], ids["doc_decision"]])

        b = p["blockers"]
        self.assertEqual(b["dependencies"], [{
            "work_unit_id": sp["dep"], "title": "Foundation", "disposition": "completed",
            "relation": "direct dependency"}])
        self.assertEqual([m["message_id"] for m in b["unit_blockers"]], [sp["blocker"]])
        self.assertEqual(b["unit_blockers"][0]["state"], "awaiting reply")
        self.assertEqual(b["unit_blockers"][0]["read"], f"sc sprint inbox --sprint {sp['sprint']}")
        self.assertEqual([f["flag_id"] for f in b["feature_flags"]], [ids["open_flag"]])

        dumped = json.dumps(p)
        self.assertNotIn(SPEC_BODY, dumped)
        self.assertNotIn("SECRET blocker body", dumped)
        self.assertNotIn("Read the code paths", dumped)   # unlinked sibling task

    def test_work_unit_boundaries_state_lane_walls_and_actions(self):
        ids = seed_feature(self.con)
        sp = seed_sprint(self.con, ids)
        bo = self.project(work_unit_id=sp["unit"])["boundaries"]
        self.assertEqual(bo["role"], f"dev · Sprint {sp['sprint']} developer")
        self.assertEqual(bo["ownership"]["assigned"], "DEV1")
        self.assertEqual(bo["ownership"]["reviewer"], "REV1")
        self.assertEqual(bo["ownership"]["planner"], "PLN1")
        walls = " | ".join(bo["walls"])
        self.assertIn("Sprint armed", walls)
        self.assertIn(f"unit #{sp['unit']} is active", walls)
        self.assertNotIn("direct dependency", walls)        # completed deps are not walls
        self.assertEqual(bo["actions"][0],
                         f"sc sprint accept --sprint {sp['sprint']} --message {sp['assignment']}")
        self.assertTrue(any("register-pr" in a for a in bo["actions"]))
        self.assertEqual(bo["actions"][-1], f"sc sprint inbox --sprint {sp['sprint']}")
        self.assertEqual(list(bo["reserved"]), list(tc.RESERVED_TO_OTHERS))

    def test_paused_sprint_and_report_unit_walls(self):
        ids = seed_feature(self.con)
        sp = seed_sprint(self.con, ids, lifecycle="paused", output_kind="report_only",
                         pending_assignment=False, blocker=False)
        self.con.execute("UPDATE sprint_work_units SET disposition='planned' "
                         "WHERE work_unit_id=?", (sp["dep"],))
        self.con.commit()
        bo = self.project(work_unit_id=sp["unit"])["boundaries"]
        walls = " | ".join(bo["walls"])
        self.assertIn("Sprint paused", walls)
        self.assertIn("output kind report_only", walls)
        self.assertIn(f"direct dependency #{sp['dep']} is planned", walls)
        self.assertEqual(bo["actions"], [f"sc sprint inbox --sprint {sp['sprint']}"])

    def test_work_unit_access(self):
        ids = seed_feature(self.con)
        sp = seed_sprint(self.con, ids)
        for shell_id in (2, 3, 5):
            with self.assertRaises(tc.ContextError) as ctx:
                self.project(work_unit_id=sp["unit"], caller_shell_id=shell_id)
            self.assertEqual(ctx.exception.status, 403)
            self.assertNotIn("Projector lane", ctx.exception.message)
        admin = self.project(work_unit_id=sp["unit"], caller_shell_id=4)
        self.assertEqual(admin["boundaries"]["role"], "admin · FnB recovery read (not a participant)")
        self.assertEqual(admin["assignment"]["work_unit"]["title"], "Projector lane")

    def test_task_linked_to_a_unit_points_at_the_work_unit_selector(self):
        ids = seed_feature(self.con)
        sp = seed_sprint(self.con, ids)
        p = self.project(task_id=ids["tasks"][1])
        self.assertEqual(p["assignment"]["tasks"][0]["sprint_work_unit_id"], sp["unit"])
        self.assertEqual(p["authority"]["documents"][0]["revision"], "current")
        self.assertIn(f"sc context --work-unit {sp['unit']}", " ".join(p["boundaries"]["walls"]))

    # ── boundaries: absent facts stay absent ─────────────────────────────
    def test_unavailable_boundary_data_is_absent_not_invented(self):
        ids = seed_feature(self.con, decisions=False, flags=False)
        p = self.project(task_id=ids["tasks"][1], runtime={}, repo_root=None, map_con=None)
        bo = p["boundaries"]
        self.assertEqual(bo["locations"], {"worktree": None, "repo_root": None, "shared": None})
        self.assertIsNone(bo["seat"])
        self.assertIsNone(bo["git"]["branch"])
        text = tc.render(p)
        self.assertIn("worktree: unavailable", text)
        self.assertIn("shared: unavailable", text)
        self.assertNotIn("`/shared`", text)
        self.assertIn("repo map: not mapped yet", text)
        self.assertIn("dev hooks: unavailable", text)

    def test_worktree_falls_back_to_launch_record_then_convention(self):
        ids = seed_feature(self.con, decisions=False, flags=False)
        tid = ids["tasks"][1]
        conv = self.project(task_id=tid, runtime={"seat": "host"})["boundaries"]["locations"]
        self.assertEqual(conv["worktree"], f"{self.root}/.sc-worktrees/dev1")
        self.con.execute(
            "INSERT INTO shell_launch_records (shell_id,pid,start_ticks,worktree) "
            "VALUES (1,1,1,'/recorded/dev1')")
        self.con.commit()
        rec = self.project(task_id=tid, runtime={})["boundaries"]["locations"]
        self.assertEqual(rec["worktree"], "/recorded/dev1")
        admin = self.project(task_id=tid, caller_shell_id=4, runtime={})["boundaries"]
        self.assertEqual(admin["locations"]["worktree"], str(self.root))
        self.assertTrue(all(Path(v).is_absolute() for v in admin["locations"].values()))

    # ── resources ────────────────────────────────────────────────────────
    def test_map_resource_prefers_live_root_and_lists_table_purposes(self):
        ids = seed_feature(self.con, decisions=False, flags=False)
        m = build_map_db()
        self.addCleanup(m.close)
        p = self.project(task_id=ids["tasks"][1], map_con=m)
        r = p["resources"]["map"]
        self.assertTrue(r["mapped"])
        self.assertEqual(r["mapped_at"], "2026-09-01T10:00:00")
        self.assertEqual(r["default_branch"], "main")
        self.assertEqual(r["file_count"], 42)
        names = {t["name"] for t in r["tables"]}
        self.assertIn("dr_filepath", names)
        self.assertTrue(all(t["purpose"] for t in r["tables"]))
        self.assertNotIn("dr_endpoint", r["semantic_empty"])
        self.assertIn("dr_route", r["semantic_empty"])
        # The map recorded another host's path; Boundaries use the live root.
        self.assertEqual(p["boundaries"]["locations"]["repo_root"], str(self.root))
        self.assertEqual(p["boundaries"]["locations"]["shared"], f"{self.root}/shared")
        text = tc.render(p)
        self.assertIn("repo map: /mapped/host/repo · main · mapped 2026-09-01T10:00:00", text)
        self.assertIn("not a navigation mandate", text)
        self.assertNotIn("'/x'", text)            # no map rows are dumped
        self.assertNotIn("GET", text)

    def test_declared_dev_hooks_are_referenced_compactly(self):
        ids = seed_feature(self.con, decisions=False, flags=False)
        p = self.project(task_id=ids["tasks"][1], repo_root=ROOT)
        self.assertEqual(p["resources"]["dev_hooks"],
                         {"state": "declared", "hooks": ["deps", "test", "lint", "typecheck"]})
        text = tc.render(p)
        self.assertIn("dev hooks: sc deps, sc test, sc lint, sc typecheck", text)
        absent = self.project(task_id=ids["tasks"][1])
        self.assertEqual(absent["resources"]["dev_hooks"], {"state": "absent", "hooks": []})

    def test_projection_is_compact_and_read_only(self):
        ids = seed_feature(self.con)
        sp = seed_sprint(self.con, ids)
        before = list(self.con.iterdump())
        text = tc.render(self.project(work_unit_id=sp["unit"]))
        self.assertEqual(list(self.con.iterdump()), before)
        self.assertLess(len(text), 4000, text)
        for section in ("## Assignment", "## Goal", "## Authority", "## Blockers",
                        "## Boundaries", "## Resources"):
            self.assertIn(section, text)
        self.assertIn("(spec body not included)", text)


class RuntimeFactsTest(unittest.TestCase):
    def test_launcher_env_and_one_local_branch_read(self):
        def run(argv, **kw):
            self.assertEqual(argv, ["git", "-C", "/w/dev1", "branch", "--show-current"])
            return subprocess.CompletedProcess(argv, 0, stdout="feat/x\n", stderr="")
        facts = tc.runtime_from_environment({"SC_SHELL_WORKTREE": "/w/dev1"}, run=run)
        self.assertEqual(facts, {"worktree": "/w/dev1", "seat": "host", "branch": "feat/x"})

    def test_container_seat_and_no_worktree_means_no_branch_probe(self):
        def run(argv, **kw):
            raise AssertionError("must not probe git without an exported worktree")
        facts = tc.runtime_from_environment({"SC_SANDBOX": "1"}, run=run)
        self.assertEqual(facts, {"seat": "container"})

    def test_git_failure_omits_branch(self):
        def run(argv, **kw):
            raise OSError("no git")
        facts = tc.runtime_from_environment({"SC_SHELL_WORKTREE": "/w/dev1"}, run=run)
        self.assertEqual(facts, {"worktree": "/w/dev1", "seat": "host"})


class ApiRouteTest(unittest.TestCase):
    """GET /_sc/context through server.dispatch_http against a file DB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "engine.db"
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        con = build_engine_db(self.db_path)
        self.ids = seed_feature(con)
        self.sp = seed_sprint(con, self.ids)
        con.close()

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def request(self, path, *, token=None, map_con=None):
        lines = ["Host: 127.0.0.1:8800", "Content-Length: 0"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        patches = [mock.patch.object(server, "db", side_effect=self.connect),
                   mock.patch.object(map_db, "open_ro", return_value=map_con),
                   mock.patch.object(server, "REPO_ROOT", self.root)]
        for p in patches:
            p.start()
        try:
            status, _headers, out = server.dispatch_http("GET", path, "\r\n".join(lines), b"")
        finally:
            for p in patches:
                p.stop()
        return status, json.loads(out)

    def test_requires_shell_token(self):
        status, _ = self.request(f"/_sc/context?task={self.ids['tasks'][1]}")
        self.assertEqual(status, 401)
        status, _ = self.request(f"/_sc/context?task={self.ids['tasks'][1]}", token="nope")
        self.assertEqual(status, 401)

    def test_task_read_is_shared_and_carries_runtime_facts(self):
        tid = self.ids["tasks"][1]
        for token in (DEV_TOKEN, REV_TOKEN, PLN_TOKEN, OTHER_TOKEN):
            status, body = self.request(
                f"/_sc/context?task={tid}&worktree=/w/x&seat=container&branch=feat/y",
                token=token)
            self.assertEqual(status, 200, body)
            self.assertEqual(tuple(body), tc.SECTIONS)
            self.assertEqual(body["boundaries"]["locations"],
                             {"worktree": "/w/x", "repo_root": str(self.root),
                              "shared": f"{self.root}/shared"})
            self.assertEqual(body["boundaries"]["seat"], "container")
            self.assertEqual(body["boundaries"]["git"]["branch"], "feat/y")

    def test_work_unit_read_enforces_owner_or_admin(self):
        uid = self.sp["unit"]
        status, owner = self.request(f"/_sc/context?work_unit={uid}", token=DEV_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(owner["authority"]["documents"][0]["revision"],
                         "immutable Sprint revision")
        status, _ = self.request(f"/_sc/context?work_unit={uid}", token=ADM_TOKEN)
        self.assertEqual(status, 200)
        for token in (OTHER_TOKEN, REV_TOKEN, PLN_TOKEN):
            status, refusal = self.request(f"/_sc/context?work_unit={uid}", token=token)
            self.assertEqual(status, 403)
            self.assertEqual(refusal["code"], "work_unit_not_owned")
            self.assertNotIn("Projector lane", json.dumps(refusal))

    def test_bad_and_unknown_selectors(self):
        for path, expected in (("/_sc/context", 400), ("/_sc/context?task=abc", 400),
                               ("/_sc/context?task=1&work_unit=1", 400),
                               ("/_sc/context?task=999999", 404),
                               ("/_sc/context?work_unit=999999", 404)):
            status, _ = self.request(path, token=DEV_TOKEN)
            self.assertEqual(status, expected, path)

    def test_read_writes_nothing(self):
        before = self.db_path.read_bytes()
        con = self.connect()
        dump_before = list(con.iterdump())
        con.close()
        self.request(f"/_sc/context?work_unit={self.sp['unit']}", token=DEV_TOKEN)
        self.request(f"/_sc/context?task={self.ids['tasks'][1]}", token=REV_TOKEN)
        con = self.connect()
        self.assertEqual(list(con.iterdump()), dump_before)
        con.close()
        self.assertEqual(self.db_path.read_bytes(), before)


class CliTest(unittest.TestCase):
    def run_cli(self, argv, env=None, payload=None):
        calls = []

        def fake_api(method, path, *a, **kw):
            calls.append((method, path))
            return payload or {"assignment": {}}
        out = io.StringIO()
        with mock.patch.object(mem, "_require_api"), \
                mock.patch.object(mem, "_api", side_effect=fake_api), \
                mock.patch.object(tc, "runtime_from_environment",
                                  return_value=env or {}), \
                mock.patch.object(tc, "render", return_value="RENDERED"), \
                redirect_stdout(out):
            code = tc.main(argv)
        return code, out.getvalue(), calls

    def test_task_selector_renders_and_passes_runtime_facts(self):
        code, out, calls = self.run_cli(
            ["--task", "7"], env={"worktree": "/w/dev1", "seat": "host", "branch": "feat/x"})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "RENDERED")
        expected = "/_sc/context?task=7&worktree=%2Fw%2Fdev1&seat=host&branch=feat%2Fx"
        self.assertEqual(calls, [("GET", expected)])

    def test_work_unit_selector_and_json(self):
        code, out, calls = self.run_cli(["--work-unit", "3", "--json"],
                                        payload={"assignment": {"selector": "work_unit"}})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["assignment"]["selector"], "work_unit")
        self.assertTrue(calls[0][1].startswith("/_sc/context?work_unit=3"))

    def test_exactly_one_selector(self):
        for argv in ([], ["--task", "1", "--work-unit", "2"]):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                tc.build_parser().parse_args(argv)

    def test_dispatcher_registers_the_verb(self):
        text = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertIn('context)      exec "$PY" "$S/task_context.py" "$@" ;;', text)
        gate = next(line for line in text.splitlines()
                    if line.strip().startswith("install|ensure-harness|doctor|"))
        self.assertIn("|context|", gate)
        self.assertIn("./sc context --task <id> | --work-unit <id> [--json]", text)


class GuidanceTest(unittest.TestCase):
    """Boot, role skills, the assignment wake, and the reseed carry the exact
    command and drop the map-first / anti-grep mandate."""

    def skill(self, name):
        return (ENGINE / "assets" / "skills" / name / "SKILL.md").read_text()

    def test_boot_orientation_loads_context_first_without_map_mandate(self):
        boot = (ENGINE / "templates" / "boot.md").read_text()
        self.assertIn("`sc context --task <id>`", boot)
        self.assertIn("`sc context --work-unit <id>`", boot)
        self.assertIn("resource, not a mandate", boot)
        self.assertNotIn("Map first, grep second", boot)
        header = compose.render_connections(None)
        self.assertNotIn("grep the tree blind", header)
        self.assertIn("abbreviated source documentation", header)

    def test_developer_prompt_and_role_skills_carry_the_command(self):
        dev = json.loads((ENGINE / "templates" / "shells" / "dev.json").read_text())
        self.assertIn("sc context --task <id>", dev["focus"])
        self.assertNotIn("don't grep blind", dev["focus"])
        spec = self.skill("spec")
        self.assertIn("sc context --task <task_id>", spec)
        self.assertLess(spec.index("sc context --task <task_id>"),
                        spec.index("sc mem get documents --doc <doc_id>"))
        self.assertIn("only for an unresolved need", spec)
        dev_skill = self.skill("sprint_dev")
        self.assertIn("sc context --work-unit <id>", dev_skill)
        self.assertIn("default planning context", dev_skill)

    def test_surface_catalogue_is_a_resource_not_a_mandate(self):
        text = self.skill("surface_catalogue")
        for gone in ("Map first, grep second", "NEVER `grep -r`", "BEFORE grepping",
                     "Query it first"):
            self.assertNotIn(gone, text)
        self.assertIn("not a mandate", text)
        self.assertIn("sc map-schema", text)
        self.assertIn("sc map-sql", text)
        self.assertIn("keep\n  working with another method", text)

    def test_cartographer_description_standard_is_behavioral_and_incremental(self):
        text = " ".join(self.skill("cartographer").split())
        for needed in ("responsibility the file owns", "mechanism it uses",
                       "principal input", "observable output", "200-character",
                       "| Test | the contract, boundary, or failure mode it proves |",
                       "| Migration |", "| Entrypoint |", "need no bulk rewrite",
                       "never merely repeat the filename"):
            self.assertIn(needed, text)
        self.assertNotIn("<=100 chars", text)

    def test_assignment_wake_carries_the_literal_command_only(self):
        body = sprint_domain.assignment_body(
            {"title": "T", "expected_output": "E", "work_unit_id": 9})
        self.assertEqual(body, "T\n\nE\n\nsc context --work-unit 9")
        self.assertNotIn("## Assignment", body)

    def test_reseed_migration_matches_the_assets(self):
        path = ENGINE / "migrations" / "0253_reseed_task_context_projection.sql"
        con = sqlite3.connect(":memory:")
        con.executescript(
            "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "description TEXT, category TEXT, command TEXT, common INTEGER, "
            "content TEXT, is_deleted INTEGER DEFAULT 0);")
        con.executescript(path.read_text())
        con.executescript(path.read_text())          # idempotent
        for name in ("cartographer", "spec", "sprint_dev", "surface_catalogue"):
            parsed = seed_skills.parse_skill(ENGINE / "assets" / "skills" / name / "SKILL.md")
            row = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name=?", (name,)).fetchone()
            self.assertEqual(tuple(row), (parsed["description"], parsed["category"],
                                          parsed["command"], parsed["common"],
                                          parsed["content"], 0), name)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM skills").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
