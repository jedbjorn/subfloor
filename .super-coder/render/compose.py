#!/usr/bin/env python3
"""Compose the boot artifact from live DB state.

Pure render: reads the chosen shell's identity, memory, projects, and skills
out of the DB and assembles one markdown document. The launcher
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


def render_connections(con) -> str:
    """## CONNECTIONS — the single "where things live" surface (B5). Two layers,
    top to bottom: a derived header (facts, never authored) and the section index
    (`dr_section`, prefix-joined to live file counts). The shell sees *where to
    start* here, then queries one section's leaves on demand. (The old authored
    `shells.connections` free-text layer was retired — nothing prompted shells to
    fill it, so it sat empty; the map is the surface now.)"""
    repo = con.execute(
        "SELECT root, default_branch, mapped_at FROM dr_repo WHERE repo_id=1").fetchone()
    lines = ["**Need to find something? Look here first** — read the `dr_*` map via "
             "the `surface_catalogue` skill; don't grep the tree blind."]
    if repo and repo["root"]:
        branch = f" · `{repo['default_branch']}`" if repo["default_branch"] else ""
        mapped = f" · mapped {repo['mapped_at']}" if repo["mapped_at"] else ""
        lines.append(f"- Repo root: `{repo['root']}`{branch}{mapped}")
        lines.append(f"- Shared (scratch / handoff): `{repo['root']}/shared`")

    sections = con.execute(
        "SELECT s.name, s.path_prefix, s.description, "
        "  (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || '%') AS n "
        "FROM dr_section s ORDER BY s.sort_order, s.name").fetchall()
    if sections:
        lines += ["", "**Sections** — `name · location · files · what's there`. Query a "
                  "section's leaves (file names + descriptions) on demand, never all at once:"]
        for s in sections:
            desc = f" — {s['description']}" if s["description"] else ""
            lines.append(f"- **{s['name']}** · `{s['path_prefix']}` · {s['n']} files{desc}")
        unsectioned = con.execute(
            "SELECT COUNT(*) FROM dr_filepath f WHERE NOT EXISTS "
            "(SELECT 1 FROM dr_section s WHERE f.path LIKE s.path_prefix || '%')"
        ).fetchone()[0]
        if unsectioned:
            lines.append(f"- _other / unsectioned_ · {unsectioned} files — cartographer worklist")

    return "\n".join(lines)


def render_skills(con, shell_id: int) -> str:
    rows = con.execute(
        "SELECT s.name, s.description FROM skills s "
        "JOIN shell_skills ss ON ss.skill_id = s.skill_id "
        "WHERE ss.shell_id=? AND s.is_deleted=0 ORDER BY s.name",
        (shell_id,),
    ).fetchall()
    if not rows:
        return "(none)"
    # Substrate skills load from the DB, not a harness skill dir — so they work
    # on every harness (claude/codex/opencode/vibe), present and future, with no
    # per-harness bridging. These are SEPARATE from and ADDITIONAL to whatever
    # native skills your harness ships (codex's `.system` set, claude plugins,
    # …; vibe ships none). Below: name, one-line description, and the exact query
    # to load each skill's full procedure on demand.
    lines = [
        "Substrate skills granted to you — loaded from your memory DB, **in "
        "addition to** any native skills your harness provides. Load a skill's "
        "full procedure on demand with the query under it:",
        "",
    ]
    for r in rows:
        desc = (r["description"] or "").strip().splitlines()[0] if r["description"] else ""
        lines.append(f"- **{r['name']}** — {desc}")
        lines.append(
            "  - load: `sqlite3 .super-coder/shell_db.db \"SELECT content FROM "
            f"skills WHERE name='{r['name']}' AND is_deleted=0;\"`"
        )
    return "\n".join(lines)


def fetch_counts(con, shell_id: int) -> dict:
    def one(q):
        return con.execute(q, (shell_id,)).fetchone()[0]
    return {
        "seed": one("SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=? AND kind='seed' AND is_deleted=0 AND retired_at IS NULL"),
        "lns": one("SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL"),
        "flags": one("SELECT COUNT(*) FROM flags WHERE shell_id=? AND resolved=0 AND is_deleted=0"),
        "unread": one("SELECT COUNT(*) FROM shell_messages WHERE to_shell_id=? AND read_at IS NULL"),
    }


def compose_boot(con: sqlite3.Connection, shell, user, session_id: str,
                 archive_id: int, work_dir: "Path | None" = None) -> str:
    """Assemble the full boot markdown for `shell`, driven by `user`.

    work_dir, when set, is the shell's effective working directory (dev-shell
    worktree). Its path is surfaced in ACTIVE SESSION so the shell knows it
    operates from a worktree rather than the repo root.
    """
    template = TEMPLATE_PATH.read_text().rstrip()
    shell_id = shell["shell_id"]
    counts = fetch_counts(con, shell_id)

    system_prompt = (shell["system_prompt"] or "").strip().replace("<self>", str(shell_id))
    current_state = (shell["current_state"] or "(none)").strip()

    # Orientation state: has this shell run first-run bootstrap, and is the repo
    # mapped? Drives the FIRST RUN prompt + the map-status line.
    bootstrapped = con.execute(
        "SELECT bootstrapped FROM shells WHERE shell_id=?", (shell_id,)).fetchone()[0]
    flavor = (shell["flavor"] if "flavor" in shell.keys() else None)
    map_count = con.execute("SELECT COUNT(*) FROM dr_filepath").fetchone()[0]
    mapped_at_row = con.execute("SELECT mapped_at FROM dr_repo WHERE repo_id=1").fetchone()
    # Working shells never map — an unmapped repo is the cartographer's to fix.
    map_status = (f"{map_count} files, mapped {mapped_at_row[0]}"
                  if map_count and mapped_at_row and mapped_at_row[0]
                  else "not mapped — cartographer: `./sc map-setup`")

    # Ingest status: INGESTABLE host-repo docs vs how many are in the DB. The
    # denominator is narrowed (B5) so the ratio stops reading as a false backlog:
    # exclude the engine + embedded substrate assets (.super-coder/), .github
    # templates/workflows, and standard meta files (README/CHANGELOG/LICENSE/…)
    # that `onboard` would never ingest.
    repo_docs = con.execute(
        "SELECT COUNT(*) FROM dr_filepath WHERE role='doc' "
        "AND path NOT LIKE '.super-coder/%' "
        "AND path NOT LIKE '.github/%' "
        "AND lower(path) NOT LIKE '%readme.md' "
        "AND lower(path) NOT LIKE '%changelog%' "
        "AND lower(path) NOT LIKE '%license%' "
        "AND lower(path) NOT LIKE '%contributing.md' "
        "AND lower(path) NOT LIKE '%code_of_conduct.md' "
        "AND lower(path) NOT LIKE '%security.md'").fetchone()[0]
    ingested = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    docs_status = f"{ingested} ingested / {repo_docs} ingestable in repo"
    if repo_docs > ingested:
        docs_status += " — run the `onboard` skill"

    first_run = []
    if not bootstrapped:
        if flavor == "cartographer":
            prompt = (
                "You own the repo map and haven't set it up yet. Run the "
                "**cartographer** skill now — configure `map.config.json` for "
                "this repo, wire the auto-remap git hooks (`./sc map-setup`), and "
                "map. Do this before other work; the working shells rely on it.")
        else:
            prompt = (
                "You have not oriented in this repo yet. Run the **bootstrap** "
                "skill now — read the repo map + your identity, set your "
                "`current_state`, and mark yourself oriented. (You don't map the "
                "repo — the cartographer keeps the map fresh for you.) Do this "
                "before other work.")
        first_run = ["## FIRST RUN", "", prompt, "", "---", ""]

    active_session = [
        f"- shell_id: `{shell_id}`",
        f"- display_name: `{shell['display_name']}`",
        f"- shortname: `{shell['shortname']}`",
        f"- session_id: `{session_id}`",
        f"- archive_id: `{archive_id}`",
    ]
    if work_dir is not None:
        active_session.append(
            f"- worktree: `{work_dir}` (your cwd — branch and commit from here)")

    parts = [
        template,
        "",
        "## ACTIVE SESSION", "",
        *active_session,
        "", "---", "",
        *first_run,
        "## OPERATOR", "", render_operator(user),
        "", "---", "",
        "## IDENTITY", "", render_identity(shell),
        "", "---", "",
        "## SYSTEM PROMPT", "", system_prompt,
        "", "---", "",
        "## CONNECTIONS", "", render_connections(con),
        "", "---", "",
        "## CURRENT STATE", "", current_state,
        "", "---", "",
        "## SEED", "", render_seed(con, shell_id),
        "", "---", "",
        "## LESSONS & STANCES", "", render_lns(con, shell_id),
        "", "---", "",
        "## ACTIVE PROJECTS", "", render_projects(con, shell_id),
        "", "---", "",
        "## SKILLS", "", render_skills(con, shell_id),
        "", "---", "",
        "## STATUS", "",
        f"- **Session:** {session_id}",
        f"- **Seed:** {counts['seed']}",
        f"- **L&S:** {counts['lns']}",
        f"- **Flags:** {counts['flags']} open",
        f"- **Inbox:** {counts['unread']} unread — `--message check` to surface.",
        f"- **Repo map:** {map_status}",
        f"- **Docs:** {docs_status}",
        "",
    ]
    return "\n".join(parts)
