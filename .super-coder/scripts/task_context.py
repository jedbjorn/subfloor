#!/usr/bin/env python3
"""Task context projection — `./sc context --task|--work-unit` (spec doc #187,
feature #41, decision #306).

One read-only projector over relations the engine already holds, rendered as
six short sections: Assignment, Goal, Authority, Blockers, Boundaries, and
Resources. Nothing is stored, ranked, summarized, or inferred: every line is a
direct row, a computed hash over a direct row, a launcher-exported runtime fact,
or a static description of an engine surface. Where the engine does not hold a
fact it says so or omits the line — it never goes looking.

  project   The pure projector. Reads one engine-DB connection, the optional
            read-only map DB, the repo root, the fork's declared dev hooks, and
            the runtime facts the launched shell exported. Raises ContextError
            with the HTTP status the API should return.
  render    The six labeled sections as text for model consumption.
  main      `./sc context --task <id> | --work-unit <id> [--json]`, wired
            through the same API lane as `sc mem` (GET /_sc/context).

Selectors:
  --task       any authenticated shell (the shared planning-read posture).
  --work-unit  only the unit's assigned Developer or an Admin (FnB recovery)
               shell; anyone else gets a bounded refusal without lane details.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]

ONE_LINE = 240            # the same bound `sc mem get flags` renders with
SECTIONS = ("assignment", "goal", "authority", "blockers", "boundaries", "resources")
DEV_TOOL_HOOKS = ("deps", "test", "lint", "typecheck")
GIT_TIMEOUT = 5.0

# Compact purposes for the catalogue tables (the `surface_catalogue` skill has
# the columns). Tables absent from the map DB are not listed; unknown extra
# tables are listed without a purpose rather than guessed at.
MAP_TABLE_PURPOSES = {
    "dr_repo": "the repo: root, remote, default_branch, file_count, mapped_at",
    "dr_section": "authored navigation index: name, path_prefix, description",
    "dr_filepath": "one row per file: path, lang, role, lines, desc (one-line behavior)",
    "dr_dependency": "manifest dependencies: manager, name, version, source_file",
    "dr_env": "env-var names from .env example files",
    "dr_endpoint": "HTTP routes: method, path, handler (semantic; extractor-fed)",
    "dr_db_table": "product DB tables/views (semantic; extractor-fed)",
    "dr_db_column": "product DB columns per table (semantic; extractor-fed)",
    "dr_route": "UI routes: path, kind (semantic; extractor-fed)",
    "dr_component": "UI components: name, path (semantic; extractor-fed)",
    "dr_managed_host": "managed hosts the fork declares",
    "dr_managed_service": "managed services the fork declares",
    "dr_managed_reference": "references between managed hosts/services",
}
SEMANTIC_TABLES = ("dr_endpoint", "dr_db_table", "dr_db_column", "dr_route", "dr_component")

# Developer-lane walls by work-unit disposition. Every command is guidance —
# the Sprint endpoint revalidates authority and state on the actual call.
LANE_WALLS = {
    "planned": "not dispatched — the Planner releases it; no lane action yet",
    "ready": "assignment delivered, not accepted — accept before editing",
    "active": "editing lane is yours — build, register the PR, request review",
    "blocked": "blocked — resolve or report through the Sprint inbox; no handoff",
    "in_review": "under review — the Reviewer records the verdict; wait",
    "fixing": "review asked for fixes — fix, push, request review again",
    "merge_ready": "merge only after a live merge authorization",
    "completed": "closed lane — no further edits under this unit",
    "cancelled": "cancelled lane — no further edits under this unit",
}
LANE_ACTIONS = {
    "active": (
        "sc sprint register-pr --sprint {sprint} --repository <owner/name> --pr <n> --work-unit {unit}",
        "sc sprint request-review --sprint {sprint} --work-unit {unit} ...",
        "sc sprint complete-unit --sprint {sprint} --work-unit {unit} --result-file <path>  (report/no-code units only)",
    ),
    "fixing": (
        "sc sprint request-review --sprint {sprint} --work-unit {unit} ...",
    ),
    "blocked": (
        "sc sprint send --sprint {sprint} --to <planner> --body-file <path> --intent blocker --requires-reply --work-unit {unit} --key <stable-key>",
    ),
}
SPRINT_WALLS = {
    "prepared": "Sprint not armed — nothing is dispatched",
    "armed": "Sprint armed — assignments and handoffs flow",
    "paused": "Sprint paused — fix red PRs only; no new handoffs until resumed",
    "completed": "Sprint completed — lanes are closed",
    "aborted": "Sprint aborted — lanes are closed",
}
RESERVED_TO_OTHERS = (
    "merge authorization: Planner/FnB (`sc sprint authorize-merge`)",
    "review verdicts: Reviewer (`sc sprint record-review`)",
    "dispatch, replan, recall, pause/resume: Planner/FnB",
)
TASK_WALLS = {
    "pending": "not started — `sc mem task start {task}` before building it",
    "in_progress": "in progress — `sc mem task done {task}` when verified",
    "done": "already done — a further change belongs to a new task",
    "cancelled": "cancelled — do not build it under this task",
}


class ContextError(Exception):
    """A refusal carrying the HTTP status the API should return."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def one_line(text, limit: int = ONE_LINE) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _sha256(text) -> str:
    return hashlib.sha256(str(text or "").encode()).hexdigest()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# -- direct relations ---------------------------------------------------------

def _shell(con, shell_id: int) -> dict:
    row = con.execute(
        "SELECT shell_id, shortname, display_name, flavor FROM shells "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0", (shell_id,)).fetchone()
    if row is None:
        raise ContextError(401, "unknown_shell", "caller shell is unknown")
    return dict(row)


def _active_decisions(con, feature_id, document_ids) -> list[dict]:
    """The engine's active-row rule: not deleted, and no non-deleted child
    supersedes it. Linked by governing document or by feature; deduplicated by
    id through the single query; ordered by id."""
    doc_ids = [int(d) for d in document_ids if d is not None]
    marks = ",".join("?" for _ in doc_ids) or "NULL"
    rows = con.execute(
        "SELECT d.decision_id, d.decision, d.priority, d.decision_date, "
        "d.feature_id, d.document_id, "
        "(SELECT s.shortname FROM shells s WHERE s.shell_id=d.shell_id) AS shortname "
        "FROM shell_decisions d "
        "WHERE COALESCE(d.is_deleted,0)=0 "
        "AND NOT EXISTS (SELECT 1 FROM shell_decisions c "
        " WHERE c.parent_decision_id=d.decision_id AND COALESCE(c.is_deleted,0)=0) "
        f"AND (d.feature_id=? OR d.document_id IN ({marks})) "
        "ORDER BY d.decision_id",
        (feature_id, *doc_ids)).fetchall()
    return [{
        "decision_id": r["decision_id"],
        "priority": r["priority"] or "M",
        "decision_date": r["decision_date"],
        "shortname": r["shortname"],
        "linked_by": "document" if r["document_id"] in doc_ids else "feature",
        "statement": one_line(r["decision"]),
        "provenance": "current decision",
        "read": f"sc mem get decisions {r['decision_id']}",
    } for r in rows]


def _feature_flags(con, feature_id) -> list[dict]:
    rows = con.execute(
        "SELECT f.flag_id, f.display_name, f.priority, f.description, "
        "(SELECT s.shortname FROM shells s WHERE s.shell_id=f.shell_id) AS owner "
        "FROM flags f WHERE f.feature_id=? AND COALESCE(f.resolved,0)=0 "
        "AND COALESCE(f.is_deleted,0)=0 ORDER BY f.flag_id", (feature_id,)).fetchall()
    return [{
        "flag_id": r["flag_id"],
        "display_name": r["display_name"],
        "priority": r["priority"] or "Medium",
        "owner": r["owner"],
        "scope": "feature-level",
        "description": one_line(r["description"]),
        "read": f"sc mem get flags {r['flag_id']}",
    } for r in rows]


def _document(con, document_id) -> dict | None:
    row = con.execute(
        "SELECT document_id, feature_id, kind, seq, title, frozen, body "
        "FROM documents WHERE document_id=?", (document_id,)).fetchone()
    if row is None:
        return None
    return {
        "document_id": row["document_id"],
        "title": row["title"],
        "kind": row["kind"],
        "seq": row["seq"],
        "frozen": bool(row["frozen"]),
        "revision": "current",
        "sha256": _sha256(row["body"]),
        "read": f"sc mem get documents --doc {row['document_id']}",
    }


def _task_row(con, task_id: int):
    return con.execute(
        "SELECT t.task_id, t.feature_id, t.document_id, t.seq, t.title, "
        "t.description, t.status, r.title AS feature_title, r.summary AS feature_summary, "
        "r.roadmap_status FROM spec_tasks t JOIN roadmap r ON r.feature_id=t.feature_id "
        "WHERE t.task_id=?", (task_id,)).fetchone()


def _unit_link(con, task_id: int):
    return con.execute(
        "SELECT sprint_id, work_unit_id FROM sprint_work_unit_tasks WHERE task_id=?",
        (task_id,)).fetchone()


# -- selectors ----------------------------------------------------------------

def _select_task(con, task_id: int) -> dict:
    t = _task_row(con, task_id)
    if t is None:
        raise ContextError(404, "unknown_task", f"no such task: {task_id}")
    link = _unit_link(con, task_id)
    task = {
        "task_id": t["task_id"], "seq": t["seq"], "title": t["title"],
        "description": t["description"] or "", "status": t["status"],
        "document_id": t["document_id"],
    }
    if link is not None:
        task["sprint_work_unit_id"] = int(link["work_unit_id"])
        task["sprint_id"] = int(link["sprint_id"])
    document = _document(con, t["document_id"])
    return {
        "selector": "task",
        "feature": {
            "feature_id": t["feature_id"], "title": t["feature_title"],
            "summary": t["feature_summary"] or "", "roadmap_status": t["roadmap_status"],
        },
        "tasks": [task],
        "documents": [document] if document else [],
        "sprint": None,
        "unit": None,
    }


def _select_work_unit(con, work_unit_id: int, caller: dict) -> dict:
    u = con.execute(
        "SELECT u.work_unit_id, u.sprint_id, u.title, u.expected_output, "
        "u.output_kind, u.disposition, u.planned_wave, u.assigned_shell_id, "
        "u.reviewer_shell_id, sp.lifecycle, sp.feature_id, "
        "sp.originating_planner_shell_id, r.title AS feature_title, "
        "r.summary AS feature_summary, r.roadmap_status, "
        "a.shortname AS assigned_shortname, rv.shortname AS reviewer_shortname, "
        "pl.shortname AS planner_shortname "
        "FROM sprint_work_units u JOIN sprints sp ON sp.sprint_id=u.sprint_id "
        "JOIN roadmap r ON r.feature_id=sp.feature_id "
        "JOIN shells a ON a.shell_id=u.assigned_shell_id "
        "JOIN shells rv ON rv.shell_id=u.reviewer_shell_id "
        "JOIN shells pl ON pl.shell_id=sp.originating_planner_shell_id "
        "WHERE u.work_unit_id=?", (work_unit_id,)).fetchone()
    if u is None:
        raise ContextError(404, "unknown_work_unit", f"no such work unit: {work_unit_id}")
    if int(u["assigned_shell_id"]) != int(caller["shell_id"]) and caller["flavor"] != "admin":
        raise ContextError(403, "work_unit_not_owned",
                           f"work unit {work_unit_id} is not assigned to this shell")
    sprint_id = int(u["sprint_id"])
    tasks = _rows(con.execute(
        "SELECT t.task_id, t.seq, t.title, t.description, t.status, t.document_id "
        "FROM sprint_work_unit_tasks l JOIN spec_tasks t ON t.task_id=l.task_id "
        "WHERE l.sprint_id=? AND l.work_unit_id=? ORDER BY t.seq, t.task_id",
        (sprint_id, work_unit_id)))
    for t in tasks:
        t["description"] = t["description"] or ""
    governing = sorted({t["document_id"] for t in tasks if t["document_id"] is not None})
    marks = ",".join("?" for _ in governing) or "NULL"
    revisions = con.execute(
        "SELECT ss.document_id, d.title, ss.bound_revision_sha256, "
        "ss.bound_revision_legacy, "
        "(SELECT MAX(h.generation) FROM sprint_spec_revision_history h "
        " WHERE h.sprint_id=ss.sprint_id AND h.document_id=ss.document_id) AS generation "
        "FROM sprint_specs ss JOIN documents d ON d.document_id=ss.document_id "
        f"WHERE ss.sprint_id=? AND ss.document_id IN ({marks}) ORDER BY ss.document_id",
        (sprint_id, *governing)).fetchall()
    documents = [{
        "document_id": r["document_id"],
        "title": r["title"],
        "revision": "immutable Sprint revision",
        "sha256": r["bound_revision_sha256"],
        "generation": r["generation"],
        "legacy": bool(r["bound_revision_legacy"]),
        "read": f"sc sprint spec-revision --sprint {sprint_id} --document {r['document_id']}",
    } for r in revisions]
    dependencies = _rows(con.execute(
        "SELECT d.depends_on_work_unit_id AS work_unit_id, u.title, u.disposition "
        "FROM sprint_work_unit_dependencies d JOIN sprint_work_units u "
        "ON u.sprint_id=d.sprint_id AND u.work_unit_id=d.depends_on_work_unit_id "
        "WHERE d.sprint_id=? AND d.work_unit_id=? ORDER BY d.depends_on_work_unit_id",
        (sprint_id, work_unit_id)))
    for d in dependencies:
        d["relation"] = "direct dependency"
    blockers = _rows(con.execute(
        "SELECT m.message_id, m.created_at, "
        "(SELECT s.shortname FROM shells s WHERE s.shell_id=m.sender_shell_id) AS sender "
        "FROM wake_message m WHERE m.sprint_id=? AND m.work_unit_id=? "
        "AND m.intent='blocker' AND COALESCE(m.requires_reply,0)=1 "
        "AND NOT EXISTS (SELECT 1 FROM wake_message r WHERE r.reply_to_message_id=m.message_id) "
        "ORDER BY m.message_id", (sprint_id, work_unit_id)))
    for b in blockers:
        b["state"] = "awaiting reply"
        b["read"] = f"sc sprint inbox --sprint {sprint_id}"
    pending = con.execute(
        "SELECT message_id FROM wake_message WHERE sprint_id=? AND work_unit_id=? "
        "AND message_kind='work_assignment' AND disposition='pending' "
        "ORDER BY message_id DESC LIMIT 1", (sprint_id, work_unit_id)).fetchone()
    participant = con.execute(
        "SELECT role FROM sprint_participants WHERE sprint_id=? AND shell_id=?",
        (sprint_id, caller["shell_id"])).fetchone()
    return {
        "selector": "work_unit",
        "feature": {
            "feature_id": u["feature_id"], "title": u["feature_title"],
            "summary": u["feature_summary"] or "", "roadmap_status": u["roadmap_status"],
        },
        "tasks": tasks,
        "documents": documents,
        "sprint": {
            "sprint_id": sprint_id, "lifecycle": u["lifecycle"],
            "planner": u["planner_shortname"],
            "caller_role": participant["role"] if participant else None,
        },
        "unit": {
            "work_unit_id": work_unit_id, "title": u["title"],
            "expected_output": u["expected_output"], "output_kind": u["output_kind"],
            "disposition": u["disposition"], "planned_wave": u["planned_wave"],
            "assigned": u["assigned_shortname"], "reviewer": u["reviewer_shortname"],
            "assigned_shell_id": int(u["assigned_shell_id"]),
            "dependencies": dependencies,
            "unit_blockers": blockers,
            "pending_assignment_message_id": int(pending["message_id"]) if pending else None,
        },
    }


# -- runtime and resources ----------------------------------------------------

def _worktree(con, caller: dict, runtime: dict, repo_root: Path | None) -> str | None:
    """The launcher's exported worktree first; then the engine's own launch
    record (headless shells); then the one worktree rule every boot path
    shares (`.sc-worktrees/<shortname>`, admin at the repo root)."""
    if runtime.get("worktree"):
        return str(runtime["worktree"])
    row = con.execute(
        "SELECT worktree FROM shell_launch_records WHERE shell_id=?",
        (caller["shell_id"],)).fetchone()
    if row is not None and row["worktree"]:
        return str(row["worktree"])
    if repo_root is None:
        return None
    if caller["flavor"] == "admin" or not caller.get("shortname"):
        return str(repo_root)
    return str(Path(repo_root) / ".sc-worktrees" / caller["shortname"].lower())


def _map_resource(map_con) -> dict:
    if map_con is None:
        return {"mapped": False}
    repo = map_con.execute(
        "SELECT root, default_branch, mapped_at, file_count FROM dr_repo WHERE repo_id=1"
    ).fetchone()
    present = [r[0] for r in map_con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dr_%' ORDER BY name")]
    semantic_empty = []
    for name in SEMANTIC_TABLES:
        if name in present and map_con.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is None:
            semantic_empty.append(name)
    return {
        "mapped": bool(repo and repo["root"]),
        "root": repo["root"] if repo else None,
        "default_branch": repo["default_branch"] if repo else None,
        "mapped_at": repo["mapped_at"] if repo else None,
        "file_count": repo["file_count"] if repo else None,
        "tables": [{"name": n, "purpose": MAP_TABLE_PURPOSES.get(n)} for n in present],
        "semantic_empty": semantic_empty,
    }


def _declared_hooks(repo_root: Path | None) -> dict:
    if repo_root is None:
        return {"state": "unavailable", "hooks": []}
    import devkit
    try:
        declaration = devkit.load_declaration(Path(repo_root))
    except devkit.DevkitConfigError as exc:
        return {"state": "invalid", "detail": str(exc), "hooks": []}
    if declaration is None:
        return {"state": "absent", "hooks": []}
    return {"state": "declared",
            "hooks": [n for n in DEV_TOOL_HOOKS if n in declaration.hooks]}


def runtime_from_environment(env=None, run=None) -> dict:
    """Runtime facts the launcher exported into this shell — no discovery
    beyond one local read of the exported worktree's checked-out branch."""
    env = os.environ if env is None else env
    facts: dict = {}
    worktree = env.get("SC_SHELL_WORKTREE") or ""
    if worktree:
        facts["worktree"] = worktree
    facts["seat"] = "container" if env.get("SC_SANDBOX") else "host"
    if worktree:
        run = run or subprocess.run
        try:
            out = run(["git", "-C", worktree, "branch", "--show-current"],
                      capture_output=True, text=True, timeout=GIT_TIMEOUT)
            if out.returncode == 0 and out.stdout.strip():
                facts["branch"] = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return facts


# -- the projector ------------------------------------------------------------

def project(con, *, task_id: int | None = None, work_unit_id: int | None = None,
            caller_shell_id: int, map_con=None, repo_root: Path | str | None = None,
            runtime: dict | None = None) -> dict:
    """Six sections from direct relations. Exactly one selector; no writes."""
    if (task_id is None) == (work_unit_id is None):
        raise ContextError(400, "one_selector",
                           "exactly one of --task <id> / --work-unit <id> is required")
    runtime = dict(runtime or {})
    repo_root = Path(repo_root) if repo_root else None
    caller = _shell(con, caller_shell_id)
    sel = (_select_task(con, int(task_id)) if task_id is not None
           else _select_work_unit(con, int(work_unit_id), caller))
    feature, tasks, documents = sel["feature"], sel["tasks"], sel["documents"]
    sprint, unit = sel["sprint"], sel["unit"]
    doc_ids = [d["document_id"] for d in documents]

    # Assignment
    assignment = {"selector": sel["selector"], "feature": feature, "tasks": tasks}
    if unit is not None:
        assignment["work_unit"] = {k: unit[k] for k in (
            "work_unit_id", "title", "output_kind", "disposition", "planned_wave",
            "assigned", "reviewer")}
        assignment["sprint_id"] = sprint["sprint_id"]

    # Goal — authored text as authored; a missing goal names the exact read.
    if unit is not None:
        primary = unit["expected_output"]
        slices = [{"task_id": t["task_id"], "description": t["description"]} for t in tasks]
    else:
        primary = tasks[0]["description"]
        slices = []
    goal = {"primary": primary, "acceptance_slices": slices,
            "feature_summary_is_context": True}
    if not (primary or "").strip() and not any(s["description"].strip() for s in slices):
        goal["insufficient"] = ("no authored goal — read the governing spec: "
                                + (documents[0]["read"] if documents else "(no governing document)"))

    # Authority
    authority = {
        "documents": documents,
        "decisions": _active_decisions(con, feature["feature_id"], doc_ids),
        "body_included": False,
    }
    if unit is not None:
        authority["note"] = ("bound revisions are immutable acceptance; decisions are "
                             "current at projection time and do not silently amend them — "
                             "raise a material conflict through the Sprint inbox")

    # Blockers
    blockers = {"feature_flags": _feature_flags(con, feature["feature_id"])}
    if unit is not None:
        blockers["dependencies"] = unit["dependencies"]
        blockers["unit_blockers"] = unit["unit_blockers"]

    # Boundaries — only what is known through the selection, caller, runtime,
    # and map header. An unknown fact is absent, never invented.
    map_res = _map_resource(map_con)
    # The live engine root wins: a map built on another host records that
    # host's path, and Boundaries must render a path that resolves here.
    root = str(repo_root) if repo_root else map_res.get("root")
    locations = {"worktree": _worktree(con, caller, runtime, repo_root),
                 "repo_root": root,
                 "shared": f"{root}/shared" if root else None}
    boundaries = {
        "locations": locations,
        "seat": runtime.get("seat"),
        "role": caller["flavor"],
        "shell": caller["shortname"],
        "git": {"branch": runtime.get("branch"), "base": "origin/main",
                "rule": "branch before the first edit; never commit on the default "
                        "branch or the shell base; merging is a separate authorization"},
        "walls": [],
        "actions": [],
        "reserved": [],
        "qualifications": ["feature-level", "direct dependency", "current decision",
                           "immutable Sprint revision"],
        "note": "omission is not a grant; every command is guidance — the action "
                "endpoint revalidates",
    }
    if unit is None:
        t = tasks[0]
        boundaries["walls"].append(
            TASK_WALLS.get(t["status"], f"status {t['status']}").format(task=t["task_id"]))
        boundaries["walls"].append(f"feature #{feature['feature_id']} is {feature['roadmap_status']}")
        for d in documents:
            if d.get("frozen"):
                boundaries["walls"].append(
                    f"document #{d['document_id']} is frozen — immutable; revise through a new spec")
        if t.get("sprint_work_unit_id"):
            boundaries["walls"].append(
                f"task is linked to Sprint {t['sprint_id']} work unit #{t['sprint_work_unit_id']} — "
                f"the bound revision and lane walls come from "
                f"`sc context --work-unit {t['sprint_work_unit_id']}`")
        boundaries["reserved"].append("merging a PR: the FnB's gate")
    else:
        sid, uid = sprint["sprint_id"], unit["work_unit_id"]
        if sprint["caller_role"]:
            boundaries["role"] = f"{caller['flavor']} · Sprint {sid} {sprint['caller_role']}"
        elif caller["flavor"] == "admin":
            boundaries["role"] = "admin · FnB recovery read (not a participant)"
        boundaries["ownership"] = {
            "assigned": unit["assigned"], "reviewer": unit["reviewer"],
            "planner": sprint["planner"],
            "rule": "one active lane; edit only your own worktree",
        }
        boundaries["walls"].append(SPRINT_WALLS.get(sprint["lifecycle"], sprint["lifecycle"]))
        boundaries["walls"].append(
            f"unit #{uid} is {unit['disposition']}: "
            + LANE_WALLS.get(unit["disposition"], unit["disposition"]))
        if unit["output_kind"] != "code":
            boundaries["walls"].append(
                f"output kind {unit['output_kind']} — may complete without a PR")
        for d in unit["dependencies"]:
            if d["disposition"] not in ("completed",):
                boundaries["walls"].append(
                    f"direct dependency #{d['work_unit_id']} is {d['disposition']}")
        if unit["pending_assignment_message_id"] is not None:
            boundaries["actions"].append(
                f"sc sprint accept --sprint {sid} --message {unit['pending_assignment_message_id']}")
        if sprint["lifecycle"] == "armed":
            boundaries["actions"].extend(
                a.format(sprint=sid, unit=uid) for a in LANE_ACTIONS.get(unit["disposition"], ()))
        boundaries["actions"].append(f"sc sprint inbox --sprint {sid}")
        boundaries["reserved"].extend(RESERVED_TO_OTHERS)

    # Resources — the catalogue as abbreviated documentation, never a mandate.
    resources = {
        "map": map_res,
        "map_commands": ['sc map-schema [dr_table]', 'sc map-sql "<read-only query>"'],
        "map_note": "a resource, not a navigation mandate — grep, direct reads, repository "
                    "docs, or harness-native search are equally valid; empty semantic "
                    "tables mean no extractor is wired, not an empty surface",
        "dev_hooks": _declared_hooks(repo_root),
        "dev_hooks_note": "seat and readiness: boot DEV TOOLS; run as `sc <hook>`",
    }
    return {
        "assignment": assignment, "goal": goal, "authority": authority,
        "blockers": blockers, "boundaries": boundaries, "resources": resources,
    }


# -- render -------------------------------------------------------------------

def _fmt_task(t: dict) -> str:
    return f"  task #{t['task_id']} seq {t['seq']} [{t['status']}] {t['title']}"


def render(p: dict) -> str:
    a, g, au, b, bo, r = (p[k] for k in SECTIONS)
    out: list[str] = []
    f = a["feature"]
    out.append("## Assignment")
    if a["selector"] == "work_unit":
        u = a["work_unit"]
        out.append(f"  Sprint {a['sprint_id']} work unit #{u['work_unit_id']} [{u['disposition']}] "
                   f"{u['title']} · {u['output_kind']} · wave {u['planned_wave']} · "
                   f"assigned {u['assigned']} · reviewer {u['reviewer']}")
    out.append(f"  feature #{f['feature_id']} [{f['roadmap_status']}] {f['title']}")
    if f["summary"]:
        out.append(f"    {one_line(f['summary'])}")
    out.extend(_fmt_task(t) for t in a["tasks"])
    for t in a["tasks"]:
        if t.get("sprint_work_unit_id"):
            out.append(f"    linked: Sprint {t['sprint_id']} work unit #{t['sprint_work_unit_id']}")

    out.append("## Goal")
    if g.get("insufficient"):
        out.append(f"  {g['insufficient']}")
    elif a["selector"] == "work_unit":
        out.append(f"  expected output: {g['primary']}")
        for s in g["acceptance_slices"]:
            out.append(f"  task #{s['task_id']}: {s['description']}")
    else:
        out.append(f"  {g['primary']}")
    out.append("  (feature summary is context, not the goal)")

    out.append("## Authority")
    for d in au["documents"]:
        gen = f" · generation {d['generation']}" if d.get("generation") is not None else ""
        legacy = " · legacy binding" if d.get("legacy") else ""
        frozen = " · frozen" if d.get("frozen") else ""
        out.append(f"  doc #{d['document_id']} {d['title']} — {d['revision']}{gen}{legacy}{frozen}")
        out.append(f"    sha256 {d['sha256']}")
        out.append(f"    read: {d['read']}")
    if not au["documents"]:
        out.append("  (no governing document)")
    if au["decisions"]:
        out.append("  active decisions (current):")
        for d in au["decisions"]:
            out.append(f"    #{d['decision_id']} [{d['priority']}] by {d['linked_by']}: {d['statement']}")
        out.append("    rationale: sc mem get decisions <id>")
    else:
        out.append("  active decisions: none linked")
    if au.get("note"):
        out.append(f"  {au['note']}")
    out.append("  (spec body not included)")

    out.append("## Blockers")
    for d in b.get("dependencies", []):
        out.append(f"  direct dependency #{d['work_unit_id']} [{d['disposition']}] {d['title']}")
    for m in b.get("unit_blockers", []):
        out.append(f"  unit blocker message #{m['message_id']} from {m['sender']} — {m['state']}; "
                   f"body + reply: {m['read']}")
    for fl in b["feature_flags"]:
        nm = f"[{fl['display_name']}] " if fl.get("display_name") else ""
        out.append(f"  feature-level flag #{fl['flag_id']} {nm}({fl['priority']}): {fl['description']}")
    if not (b.get("dependencies") or b.get("unit_blockers") or b["feature_flags"]):
        out.append("  none known (no direct dependencies, unit blockers, or open feature flags)")
    elif b["feature_flags"]:
        out.append("  (feature-level = linked to the feature; task relevance is not asserted; "
                   "detail: sc mem get flags <id>)")

    out.append("## Boundaries")
    loc = bo["locations"]
    for label, key in (("worktree", "worktree"), ("repo root", "repo_root"), ("shared", "shared")):
        out.append(f"  {label}: {loc[key] or 'unavailable'}")
    out.append(f"  seat: {bo['seat'] or 'unavailable'} · role: {bo['role']} · shell: {bo['shell']}")
    git = bo["git"]
    out.append(f"  branch: {git['branch'] or 'unavailable'} · base: {git['base']} — {git['rule']}")
    if bo.get("ownership"):
        o = bo["ownership"]
        out.append(f"  ownership: assigned {o['assigned']} · reviewer {o['reviewer']} · "
                   f"planner {o['planner']} — {o['rule']}")
    for w in bo["walls"]:
        out.append(f"  wall: {w}")
    for act in bo["actions"]:
        out.append(f"  action: {act}")
    for res in bo["reserved"]:
        out.append(f"  reserved: {res}")
    out.append(f"  ({bo['note']})")

    out.append("## Resources")
    m = r["map"]
    if m.get("mapped"):
        out.append(f"  repo map: {m['root']} · {m['default_branch'] or '?'} · "
                   f"mapped {m['mapped_at'] or '?'} · {m['file_count'] or '?'} files")
        out.append("  tables: " + "; ".join(
            f"{t['name']} — {t['purpose']}" if t["purpose"] else t["name"] for t in m["tables"]))
        if m["semantic_empty"]:
            out.append("  empty semantic tables: " + ", ".join(m["semantic_empty"]))
    else:
        out.append("  repo map: not mapped yet (the cartographer maps it)")
    out.append("  " + " · ".join(r["map_commands"]) + f" — {r['map_note']}")
    hooks = r["dev_hooks"]
    if hooks["hooks"]:
        out.append("  dev hooks: " + ", ".join(f"sc {h}" for h in hooks["hooks"])
                   + f" — {r['dev_hooks_note']}")
    else:
        out.append(f"  dev hooks: {hooks['state']}"
                   + (f" ({hooks['detail']})" if hooks.get("detail") else ""))
    return "\n".join(out)


# -- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sc context",
        description="Project one task or Sprint work unit: Assignment, Goal, Authority, "
                    "Blockers, Boundaries, Resources — read-only, from existing relations.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--task", type=int, metavar="ID", help="a spec_tasks id")
    sel.add_argument("--work-unit", type=int, metavar="ID",
                     help="a Sprint work unit id (assigned Developer or FnB only)")
    p.add_argument("--json", action="store_true", help="print the raw payload")
    return p


def main(argv: list[str]) -> int:
    import urllib.parse

    import mem
    args = build_parser().parse_args(argv)
    mem._PROG = "context"
    mem._require_api()
    query = {"task": args.task} if args.task is not None else {"work_unit": args.work_unit}
    query.update(runtime_from_environment())
    payload = mem._api("GET", "/_sc/context?" + urllib.parse.urlencode(query))
    print(json.dumps(payload, indent=2, default=str) if args.json else render(payload))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
