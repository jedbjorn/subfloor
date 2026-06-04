#!/usr/bin/env python3
"""Compose the boot artifact from live DB state.

Pure render: reads the chosen shell's identity, memory, projects, roadmap, and
skills out of the DB and assembles one markdown document. The launcher
(`scripts/run.py`) dual-writes the result to `CLAUDE.md` + `AGENTS.md` at the
repo root — one compose, two outputs, consumed natively by Claude Code and the
AGENTS.md-reading harnesses (OpenCode, Goose, Crush).

Nothing here touches the harness; nothing here writes the DB.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ENGINE / "templates" / "boot.md"


def _cell(v) -> str:
    s = (v or "").strip() if isinstance(v, str) else (v or "")
    return s if s else "—"


def render_identity(shell) -> str:
    return (
        "| | |\n"
        "|---|---|\n"
        f"| **Name** | {_cell(shell['display_name'])} |\n"
        f"| **Shortname** | {_cell(shell['shortname'])} |\n"
        f"| **Partner** | {_cell(shell['partner'])} |\n"
        f"| **Role** | {_cell(shell['role'])} |\n"
        f"| **Mandate** | {_cell(shell['mandate'])} |"
    )


def render_operator(user) -> str:
    return (
        "| | |\n"
        "|---|---|\n"
        f"| **user_id** | `{user['user_id']}` |\n"
        f"| **username** | {user['username']} |"
    )


def render_seed(con, shell_id: int) -> str:
    rows = con.execute(
        "SELECT entry_date, body FROM shell_identity_entries "
        "WHERE shell_id=? AND kind='seed' AND is_deleted=0 AND retired_at IS NULL "
        "ORDER BY entry_date, entry_id",
        (shell_id,),
    ).fetchall()
    if not rows:
        return "(none)"
    return "\n\n".join(f"### {r['entry_date']}\n{r['body']}" for r in rows)


def render_lns(con, shell_id: int) -> str:
    rows = con.execute(
        "SELECT body FROM shell_identity_entries "
        "WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL "
        "ORDER BY entry_date, entry_id",
        (shell_id,),
    ).fetchall()
    return "\n\n".join(r["body"] for r in rows) if rows else "(none)"


def render_projects(con, shell_id: int) -> str:
    rows = con.execute(
        "SELECT p.shortname, p.purpose, ps.role FROM projects p "
        "JOIN project_shells ps ON ps.project_id = p.project_id "
        "WHERE ps.shell_id=? AND ps.is_deleted=0 AND COALESCE(p.is_deleted,0)=0 "
        "ORDER BY p.shortname",
        (shell_id,),
    ).fetchall()
    if not rows:
        return "(none)"
    lines = []
    for r in rows:
        role = f" ({r['role']})" if r["role"] else ""
        lines.append(f"- {r['shortname']}{role}: {r['purpose'] or '(no purpose set)'}")
    return "\n".join(lines)


# Funnel order: idea inlet → most-active committed work → done (shipped, summarized).
_ROADMAP_ORDER = ["brainstorm", "in_progress", "next", "near_term", "long_term", "shipped"]
_ROADMAP_LABEL = {
    "brainstorm": "Brainstorm", "in_progress": "In Progress", "next": "Next",
    "near_term": "Near Term", "long_term": "Long Term", "shipped": "Shipped",
}


def render_roadmap(con, shell_id: int) -> str:
    """Compact roadmap index for the boot doc. Features owned by this shell are
    marked; the DB is the index, so the shell sees what's planned at a glance.
    Shipped features are summarized as a count, not enumerated."""
    rows = con.execute(
        "SELECT feature_id, title, roadmap_status, owning_shell, sort_order "
        "FROM roadmap ORDER BY sort_order, feature_id"
    ).fetchall()
    if not rows:
        return "(none)"
    buckets: dict[str, list] = {}
    shipped = 0
    for r in rows:
        if r["roadmap_status"] == "shipped":
            shipped += 1
            continue
        buckets.setdefault(r["roadmap_status"], []).append(r)
    parts = []
    for status in _ROADMAP_ORDER:
        if status not in buckets:
            continue
        parts.append(f"**{_ROADMAP_LABEL[status]}**")
        for r in buckets[status]:
            mine = " · *mine*" if r["owning_shell"] == shell_id else ""
            parts.append(f"- {r['title']}{mine}")
        parts.append("")
    if shipped:
        parts.append(f"_{shipped} shipped._")
    return "\n".join(parts).rstrip() or "(none)"


def render_skills(con, shell_id: int) -> str:
    rows = con.execute(
        "SELECT s.name, s.description FROM skills s "
        "JOIN shell_skills ss ON ss.skill_id = s.skill_id "
        "WHERE ss.shell_id=? AND s.is_deleted=0 ORDER BY s.name",
        (shell_id,),
    ).fetchall()
    if not rows:
        return "(none)"
    return "\n".join(
        f"- **{r['name']}** — {(r['description'] or '').strip().splitlines()[0] if r['description'] else ''}"
        for r in rows
    )


def fetch_counts(con, shell_id: int) -> dict:
    def one(q):
        return con.execute(q, (shell_id,)).fetchone()[0]
    return {
        "seed": one("SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=? AND kind='seed' AND is_deleted=0 AND retired_at IS NULL"),
        "lns": one("SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL"),
        "flags": one("SELECT COUNT(*) FROM flags WHERE shell_id=? AND resolved=0 AND is_deleted=0"),
    }


def compose_boot(con: sqlite3.Connection, shell, user, session_id: str,
                 archive_id: int) -> str:
    """Assemble the full boot markdown for `shell`, driven by `user`."""
    template = TEMPLATE_PATH.read_text().rstrip()
    shell_id = shell["shell_id"]
    counts = fetch_counts(con, shell_id)

    system_prompt = (shell["system_prompt"] or "").strip().replace("<self>", str(shell_id))
    current_state = (shell["current_state"] or "(none)").strip()
    workspace = (shell["workspace"] or "(none)").strip()

    # Orientation state: has this shell run first-run bootstrap, and is the repo
    # mapped? Drives the FIRST RUN prompt + the map-status line.
    bootstrapped = con.execute(
        "SELECT bootstrapped FROM shells WHERE shell_id=?", (shell_id,)).fetchone()[0]
    map_count = con.execute("SELECT COUNT(*) FROM dr_filepath").fetchone()[0]
    mapped_at_row = con.execute("SELECT mapped_at FROM dr_repo WHERE repo_id=1").fetchone()
    map_status = (f"{map_count} files, mapped {mapped_at_row[0]}"
                  if map_count and mapped_at_row and mapped_at_row[0]
                  else "not mapped — run `make map` (or the bootstrap skill)")

    first_run = []
    if not bootstrapped:
        first_run = [
            "## FIRST RUN", "",
            "You have not oriented in this repo yet. Run the **bootstrap** skill "
            "now — it maps the repo (if needed), reads the map + your identity, "
            "sets your `current_state`, and marks you oriented. Do this before "
            "other work.",
            "", "---", "",
        ]

    parts = [
        template,
        "",
        "## ACTIVE SESSION", "",
        f"- shell_id: `{shell_id}`",
        f"- display_name: `{shell['display_name']}`",
        f"- shortname: `{shell['shortname']}`",
        f"- session_id: `{session_id}`",
        f"- archive_id: `{archive_id}`",
        "", "---", "",
        *first_run,
        "## OPERATOR", "", render_operator(user),
        "", "---", "",
        "## IDENTITY", "", render_identity(shell),
        "", "---", "",
        "## SYSTEM PROMPT", "", system_prompt,
        "", "---", "",
        "## CURRENT STATE", "", current_state,
        "", "---", "",
        "## SEED", "", render_seed(con, shell_id),
        "", "---", "",
        "## LESSONS & STANCES", "", render_lns(con, shell_id),
        "", "---", "",
        "## ACTIVE PROJECTS", "", render_projects(con, shell_id),
        "", "---", "",
        "## WORKSPACE", "", workspace,
        "", "---", "",
        "## ROADMAP", "", render_roadmap(con, shell_id),
        "", "---", "",
        "## SKILLS", "", render_skills(con, shell_id),
        "", "---", "",
        "## STATUS", "",
        f"- **Session:** {session_id}",
        f"- **Seed:** {counts['seed']}",
        f"- **L&S:** {counts['lns']}",
        f"- **Flags:** {counts['flags']} open",
        f"- **Repo map:** {map_status}",
        "",
    ]
    return "\n".join(parts)
