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

# Rendered into ORIENTATION for every shell EXCEPT the cartographer (who owns the
# map and heals discrepancies directly — telling it to report them to itself is
# nonsense). Mirrors the cartographer skill's "shape:" notice contract, but for a
# map that is *wrong* rather than newly-grown. Substituted into the
# `{{map_discrepancy}}` slot in boot.md; stripped clean for the cartographer.
MAP_DISCREPANCY_BLOCK = (
    "**If the map is wrong, you report it — you never fix it (you don't map).** A "
    "discrepancy is the map being stale, missing a file, mis-sectioning an area, or "
    "contradicting the repo. On finding one: (1) surface the gap to the FnB; (2) "
    "message the cartographer naming what's off and where — `--message send "
    "cartographer \"map gap: <what's off> — paths: <region/>. heal.\"`; (3) tell the "
    "FnB to **boot the cartographer shell** to update the map. Then keep working "
    "from what the map *does* show — healing it is the cartographer's job, on its "
    "next boot."
)
# PROJECT vs ENGINE renders by repo position (`{{project_vs_engine}}` slot).
# A fork consumes the engine as a gitignored dependency; the SOURCE repo
# (origin basename in install.SOURCE_REPO_NAMES — the dogfood repo whose
# shells build subfloor itself) IS the engine and has no upstream. Same
# pipeline, described from each end — so source shells stop hunting for an
# upstream that is them, and fork shells keep the never-edit-engine rule.
PROJECT_VS_ENGINE_FORK = (
    "**Your project is this repo** — everything except `.super-coder/`.\n"
    "`.super-coder/` is the **engine** you run on (your memory + identity\n"
    "substrate), a gitignored dependency — do not treat it as the project or edit\n"
    "it. Engine changes are authored upstream in subfloor (formerly super-coder)\n"
    "and delivered here by `./sc update` — never authored here."
)
PROJECT_VS_ENGINE_SOURCE = (
    "**This repo IS the engine source — you are upstream.** `.super-coder/` is\n"
    "tracked here and is your work surface: engine changes are authored in this\n"
    "repo, land via branch → PR → merge, and reach every fork through *their*\n"
    "`./sc update`. There is no upstream above you to receive fixes from or file\n"
    "issues to — you are where engine fixes come from.\n"
    "\n"
    "Getting your own updates: after an engine change merges, sync with main and\n"
    "run `./sc update` — in this repo it skips fetch/materialize (the engine is\n"
    "already here) and reconciles in place: migrations, skills catalogue, repo\n"
    "map, snapshot. Restart your session to boot onto the new floor.\n"
    "\n"
    "Engine skills speak fork-language. Where a skill says \"never edit\n"
    "`.super-coder/`\" or \"report/file it upstream\", that guidance is for forks —\n"
    "here you author the fix directly instead.\n"
    "\n"
    "**You are operating on the engine you are running — surgery on a moving\n"
    "car.** Four standing consequences, all of which have produced wrong answers\n"
    "in this repo:\n"
    "\n"
    "1. **Your `./sc` resolves the engine from the MAIN CHECKOUT, not your\n"
    "   worktree** (`sc:11-21` derives it from git's common dir). That tree also\n"
    "   hosts the gitignored live DB and runs the server. Being current in your\n"
    "   own worktree says nothing about it — see the `floor:` line in ACTIVE\n"
    "   SESSION, which reports exactly that.\n"
    "2. **Verify claims about engine code against the remote, not your tree** —\n"
    "   `git show origin/main:<path>` — because a stale checkout answers\n"
    "   confidently and a command that reads it inherits the staleness.\n"
    "3. **Pull after every merge; reconcile and restart at sprint boundaries.**\n"
    "   Pulling is cheap and safe. `./sc update` refuses on live Interface state\n"
    "   by design and a restart kills live sessions — never mid-sprint, and the\n"
    "   restart is the FnB's.\n"
    "4. **The live DB is the one you are migrating.** Back it up before applying\n"
    "   anything, and name the DB path explicitly rather than trusting whichever\n"
    "   one the dispatcher resolves.\n"
    "\n"
    "Procedure for the heavier moves — three-artifact engine-skill commits,\n"
    "migrating the live DB, reconcile-vs-restart sequencing — is the\n"
    "`engine_surgery` skill."
)
# The repo catalogue (dr_*) lives in its OWN db, separate from shell_db.db.
MAP_DB_PATH = ENGINE.parent / ".sc-state" / "map.db"

# ── The sprint directive (spec doc 58, unit U9) ──────────────────────────────
# A shell holding sprint work is told so BY THE BOOT RENDER, resolved from the
# record. The directive used to live in a task row the planner hand-wrote; an
# audit found it in every kickoff row (written from a template) and absent from
# all six improvised afterwards. A worker booted without it behaved exactly as a
# shell should in a normal interactive session — it finished a full review pass,
# filed its flags, then ended its turn on a question. Nothing reads a headless
# run's stdout, so the planner read the silence as death and re-booted it over
# eleven minutes of completed work.
#
# So the remedy is not a better sentence. The boot doc is the one artifact the
# harness is guaranteed to read, and this section is a function of `sprint_units`
# and the planner binding: a task row cannot grant it, and a missing task row
# cannot withhold it. The planner is removed from the path.
TERMINAL_UNIT_STATES = ("merged", "cancelled")

# Stated inline rather than referenced, because the failure mode is precisely a
# skill body that never loads. This renders as prose in a document every harness
# reads, so it survives a harness with no skill mechanism at all.
PARTICIPANT_RULES = (
    "A headless turn communicates through durable artifacts:\n"
    "\n"
    "1. File findings, flags, verdicts, reports, PRs, and commits within your "
    "assigned authority.\n"
    "2. Send rulings and transitions to the planner as a scoped row: "
    "`./sc mem message send <planner> \"…\" --kind result --sprint <doc-id>`.\n"
    "3. When work remains, send a partial naming completed work, evidence, and "
    "the next uncompleted action before ending the turn."
)


def resolve_sprint_roles(con, shell_id: int) -> list:
    """Every sprint role `shell_id` holds right now, read fresh from the record
    — never cached, so a role changed since the last boot resolves correctly.

    Returns a list of `{doc_id, doc_title, role, units}` — planner entries
    first, then the dev/reviewer ones in `unit_id` order (so whichever role's
    lowest unit_id comes first, NOT dev-before-reviewer); `units` within an
    entry are in `unit_id` order too, which is the order the planner wrote the
    board. Empty means no section renders. A shell with roles in two sprints
    gets an entry per (sprint, role) — every one is named, because silently
    choosing one is the failure this unit removes.

    THE PLANNER IS RESOLVED FROM THE LATEST BINDING, ARMED OR RELEASED. The
    spec said "the armed binding", and that predicate is anti-correlated with
    the need: `_arm_binding` refuses unless the planner already has an
    `occupied` Interface session, so the session being booted cannot have a
    binding yet, and the previous one is generation-bound and released when
    that generation ends (`interface_recovery`). Under the armed reading the
    directive renders only when recovery lags — a race, not a record. The
    latest binding names who the sprint's planner IS; the LIVE predicate below
    is what retires it. Same definition as `interface_routes._board_writer`
    minus the armed filter, so the two cannot disagree about whose sprint it
    is. (Reported to the planner as an ambiguity call before build; adopted in
    PLN1 messages #2257 / #2263.)

    LIVENESS IS STRUCTURED, NEVER THE BODY'S PROSE — `documents.frozen` plus
    `sprint_units` rows, never the `status: ACTIVE` line. Regex-matching
    liveness out of prose is part of the structural gap this feature exists to
    close (flag_id 213 removed exactly that from the reconciler's trigger), and
    reintroducing it here would have the boot render and the reconciler
    disagreeing the moment someone reformats a line.

    BUT THE TWO ROLES DO NOT SHARE ONE PREDICATE, and collapsing them was
    flag_id 231 (my first cut did, on the planner's own instruction; the
    planner withdrew it):

    - dev / reviewer: `frozen = 0` AND a NON-TERMINAL row of their own. Same
      pair as the reconciler's trigger, which likewise should not watch a
      sprint with no live units.
    - planner: `frozen = 0` AND ANY unit row at all. Every step of close-out —
      the pre-freeze conformance pass, `status: CLOSED`, the freeze, the
      participant messages, the watch-retirement sweep, the sprint report, the
      flag and roadmap bookkeeping — happens AFTER the last unit reaches
      `merged` and BEFORE the doc is frozen. A non-terminal gate would delete
      the planner's directive at the exact moment its longest and most
      procedure-heavy phase begins. So the planner retires on the freeze alone,
      which is the participant skills' revocation predicate — the two surfaces
      cannot disagree about when a sprint ends.

    Consequence, decided rather than discovered: at a sprint's very first boot
    no unit rows exist yet, so the planner gets no directive — correct, since
    the planner is the shell CREATING those rows.

    Returns `[]` when `sprint_units` DOES NOT EXIST — a floor that has not
    applied migration 0098 has no such table, which was the state of this
    repo's own running floor as U9 was written, and compose reads the live db at
    every boot of every shell. That degrade is gated on the table's ACTUAL
    ABSENCE and nothing else (flag_id 233): the missing table is temporary, a
    bare `except OperationalError` would be permanent, and once 0098 is applied
    the only thing such a catch could still swallow is a genuine fault — a
    renamed column, a typo — silently reverting the whole fleet to pre-U9
    behaviour on a section the render itself labels MANDATORY. Faults raise.
    """
    entries = []
    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='sprint_units'").fetchone():
        return []
    # `sprint_planner_bindings` needs no check of its own: it arrived in
    # migration 0078 and `sprint_units` in 0098, so units-present implies
    # bindings-present on any floor that can reach this line.
    placeholders = ",".join("?" * len(TERMINAL_UNIT_STATES))
    planner_docs = con.execute(
        "SELECT b.sprint_doc_id, d.title FROM sprint_planner_bindings b "
        "JOIN documents d ON d.document_id = b.sprint_doc_id "
        "WHERE b.binding_id = (SELECT MAX(b2.binding_id) "
        "                      FROM sprint_planner_bindings b2 "
        "                      WHERE b2.sprint_doc_id = b.sprint_doc_id) "
        "  AND b.planner_shell_id = ? AND d.frozen = 0 "
        # ANY row, not a non-terminal one — see the predicate split above.
        "  AND EXISTS (SELECT 1 FROM sprint_units u "
        "              WHERE u.sprint_doc_id = b.sprint_doc_id) "
        "ORDER BY b.sprint_doc_id",
        (shell_id,)).fetchall()
    for doc_id, title in planner_docs:
        entries.append({"doc_id": doc_id, "doc_title": title,
                        "role": "planner", "units": []})

    rows = con.execute(
        "SELECT u.sprint_doc_id, d.title, u.seq, u.unit_title, "
        "       u.dev_shell_id, u.reviewer_shell_id "
        "FROM sprint_units u "
        "JOIN documents d ON d.document_id = u.sprint_doc_id "
        "WHERE (u.dev_shell_id = ? OR u.reviewer_shell_id = ?) "
        f"  AND u.state NOT IN ({placeholders}) AND d.frozen = 0 "
        "ORDER BY u.sprint_doc_id, u.unit_id",
        (shell_id, shell_id, *TERMINAL_UNIT_STATES)).fetchall()

    # (doc, role) -> entry, so a shell holding four review units in one sprint
    # gets one line naming all four rather than four sections.
    by_role = {}
    for doc_id, title, seq, unit_title, dev_id, reviewer_id in rows:
        for role, holder in (("dev", dev_id), ("reviewer", reviewer_id)):
            if holder != shell_id:
                continue
            entry = by_role.get((doc_id, role))
            if entry is None:
                entry = {"doc_id": doc_id, "doc_title": title, "role": role,
                         "units": []}
                by_role[(doc_id, role)] = entry
                entries.append(entry)
            entry["units"].append((seq, unit_title))
    return entries


def render_sprint_directive(con, shell_id: int) -> str:
    """The `## SPRINT DIRECTIVE` body, or "" when this shell holds no sprint
    role — no ceremony for a shell with no sprint work."""
    entries = resolve_sprint_roles(con, shell_id)
    if not entries:
        return ""
    lines = [
        "You hold sprint work. This section is resolved from the RECORD at "
        "every boot — the board's `sprint_units` rows and the planner binding "
        "define the standing role independently of message wording.",
        "",
    ]
    for e in entries:
        sprint = f"**Sprint doc {e['doc_id']}**"
        if e["doc_title"]:
            sprint += f" — {e['doc_title']}"
        if e["role"] == "planner":
            lines.append(f"- {sprint}: you are the **PLANNER**. Invoke the "
                         "`sprint_orchestration` skill.")
            continue
        units = ", ".join(f"`{seq}` ({title})" for seq, title in e["units"])
        slot = "dev" if e["role"] == "dev" else "reviewer"
        skill = "sprint_dev" if e["role"] == "dev" else "sprint_review"
        lines.append(f"- {sprint}: you are the **{slot.upper()}** for {units}. "
                     f"Invoke the `{skill}` skill.")
    if len(entries) > 1:
        lines.append("")
        lines.append("**Act on every role listed above.**")
    return "\n".join(lines + ["", PARTICIPANT_RULES])


def open_map_ro() -> "sqlite3.Connection | None":
    """Read-only handle to the map DB, or None if the repo isn't mapped yet
    (callers degrade to an empty CONNECTIONS block / 'not mapped' status)."""
    if not MAP_DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{MAP_DB_PATH}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError:
        return None


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
    fill it, so it sat empty; the map is the surface now.)

    `con` is the MAP DB (.sc-state/map.db), or None when the repo isn't mapped
    yet — then only the standing pointer renders."""
    lines = ["**Need to find something? Look here first** — read the `dr_*` map via "
             "the `surface_catalogue` skill; don't grep the tree blind."]
    if con is None:
        return "\n".join(lines + ["", "_Repo not mapped yet — the cartographer maps it._"])
    repo = con.execute(
        "SELECT root, default_branch, mapped_at FROM dr_repo WHERE repo_id=1").fetchone()
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
        "JOIN resolved_shell_skills ss ON ss.skill_id = s.skill_id "
        "WHERE ss.shell_id=? AND s.is_deleted=0 ORDER BY s.name",
        (shell_id,),
    ).fetchall()
    if not rows:
        return "(none)"
    # Each granted skill's full procedure is rendered to a flat file at
    # `.claude/skills/<slug>/SKILL.md` (see flat.render_skill_md), rebuilt at
    # every boot for whichever shell launches. These are SEPARATE from and
    # ADDITIONAL to whatever native skills your harness ships (codex's `.system`
    # set, claude plugins, …; vibe ships none). Below: name, one-line
    # description, and the on-disk path to each skill's full procedure — a file
    # read, never a DB query.
    lines = [
        "Substrate skills granted to you — **in addition to** any native skills "
        "your harness provides. Each skill's full procedure is on disk at the "
        "path under it; read that file to load the procedure — never query the "
        "DB directly:",
        "",
    ]
    for r in rows:
        desc = (r["description"] or "").strip().splitlines()[0] if r["description"] else ""
        slug = r["name"].strip().lower().replace(" ", "-")
        lines.append(f"- **{r['name']}** — {desc}")
        lines.append(f"  - full procedure: `.claude/skills/{slug}/SKILL.md`")
    return "\n".join(lines)


def render_api(port: "int | None", api_key: "str | None") -> str:
    if port is None or not api_key:
        return "(API not configured — run `./sc rebuild` to assign a key)"
    return (
        f"- **Base URL:** `http://127.0.0.1:{port}`\n"
        "- **Token:** available as `$SC_API_TOKEN` in your environment (set at launch).\n\n"
        "Write memory via `./sc mem` (proxied here automatically when `SC_API_TOKEN` is set). "
        "Read messages: `GET /_sc/mem/messages`. Command reference: the `memory` and `db_map` skills."
    )


# L&S writes since the last curation sweep that trip the STATUS advisory.
# Five, replayed against a real shell's write order, catches both of its large
# clusters at or before full formation. Three fires twice as often and sees
# clusters at two members, where a merge is usually wrong — two statements of a
# rule are often legitimately two rules; three is where a pattern becomes
# distinguishable from coincidence.
LNS_CURATION_DUE = 5


def fetch_counts(con, shell_id: int) -> dict:
    def one(q, params=None):
        return con.execute(q, params if params is not None else (shell_id,)).fetchone()[0]
    return {
        "seed": one("SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=? AND kind='seed' AND is_deleted=0 AND retired_at IS NULL"),
        "lns": one("SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL"),
        # Display-only: the cost of the section is in CHARACTERS, and the count
        # cap is structurally blind to it. Not a threshold — the per-entry
        # length cap bounds the total by construction (20 x 500), so this is
        # cheap awareness, nothing to watch.
        "lns_chars": one(
            "SELECT COALESCE(SUM(LENGTH(body)),0) FROM shell_identity_entries "
            "WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL"),
        # The one threshold. Counts writes, not retirements: curating AT the cap
        # net-added chars at constant count, so any signal that resets on "a
        # retirement happened" is gameable by the behaviour already observed.
        # A NULL stamp (never swept) makes every entry count — correct, that
        # shell is genuinely uncurated. Strictly `>`, at the whole-second
        # granularity both sides share: the merged entries a sweep writes just
        # before stamping must NOT count against the next interval, or a sweep
        # would re-arm its own advisory and never converge.
        "lns_since_curation": one(
            "SELECT COUNT(*) FROM shell_identity_entries "
            "WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL "
            "  AND created_at > COALESCE("
            "        (SELECT lns_curated_at FROM shells WHERE shell_id=?), '')",
            (shell_id, shell_id)),
        "flags": one("SELECT COUNT(*) FROM flags WHERE shell_id=? AND resolved=0 AND is_deleted=0"),
        "unread": one("SELECT COUNT(*) FROM shell_messages WHERE to_shell_id=? AND read_at IS NULL"),
    }


def render_lns_status(counts: dict) -> str:
    """The L&S STATUS line — count, size, and the curation advisory.

    Advisory, never an ABORT: hard rejection is right for a single write with
    an obvious remedy (shorten it), wrong for "go do a curation pass," which is
    work, not a correction. Same shape as the Inbox line — a cheap DB-derived
    count plus a conditional that fires a skill only when evidence says so.
    """
    kchars = f"{counts['lns_chars'] / 1000:.1f}k chars"
    line = f"{counts['lns']}/20 · {kchars}"
    since = counts["lns_since_curation"]
    if since >= LNS_CURATION_DUE:
        return f"{line} · {since} since curation — curation due (`curate` skill)"
    return line


def compose_boot(con: sqlite3.Connection, shell, user, session_id: str,
                 archive_id: int, work_dir: "Path | None" = None,
                 sync_note: "str | None" = None,
                 floor_note: "str | None" = None,
                 api_key: "str | None" = None,
                 api_port: "int | None" = None,
                 source_mode: bool = False) -> str:
    """Assemble the full boot markdown for `shell`, driven by `user`.

    work_dir, when set, is the shell's effective working directory (dev-shell
    worktree). Its path is surfaced in ACTIVE SESSION so the shell knows it
    operates from a worktree rather than the repo root. sync_note is the
    launcher's worktree-drift status (run.sync_worktree) — surfaced alongside
    so a stale or divergent worktree is ambient knowledge, not something the
    shell must remember to check. floor_note is run.main_checkout_note — the
    MAIN CHECKOUT's drift, which is a different tree from the shell's worktree
    and the one every `./sc` resolves the engine from; it renders for EVERY
    shell including admin at the repo root, because admin maintains main and was
    previously the only shell given no drift line at all. source_mode flips
    PROJECT vs ENGINE to the
    source-repo variant (caller decides via install.is_source_repo() — compose
    stays a pure render, no git).
    """
    template = TEMPLATE_PATH.read_text().rstrip()
    template = template.replace(
        "{{project_vs_engine}}",
        PROJECT_VS_ENGINE_SOURCE if source_mode else PROJECT_VS_ENGINE_FORK)
    shell_id = shell["shell_id"]
    counts = fetch_counts(con, shell_id)

    system_prompt = (shell["system_prompt"] or "").strip().replace("<self>", str(shell_id))
    current_state = (shell["current_state"] or "(none)").strip()

    # Orientation state: has this shell run first-run bootstrap, and is the repo
    # mapped? Drives the FIRST RUN prompt + the map-status line.
    bootstrapped = con.execute(
        "SELECT bootstrapped FROM shells WHERE shell_id=?", (shell_id,)).fetchone()[0]
    flavor = (shell["flavor"] if "flavor" in shell.keys() else None)
    # Map-discrepancy protocol: every working shell reports a wrong map (it never
    # maps); the cartographer owns the fix, so the block is stripped from its boot.
    if flavor == "cartographer":
        template = template.replace("\n{{map_discrepancy}}\n", "")
    else:
        template = template.replace("{{map_discrepancy}}", MAP_DISCREPANCY_BLOCK)
    # The repo catalogue lives in its own db now; open it read-only for the
    # CONNECTIONS block + map status. None when the repo isn't mapped yet.
    map_con = open_map_ro()
    map_count = map_con.execute("SELECT COUNT(*) FROM dr_filepath").fetchone()[0] if map_con else 0
    mapped_at_row = (map_con.execute("SELECT mapped_at FROM dr_repo WHERE repo_id=1").fetchone()
                     if map_con else None)
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
        if sync_note:
            active_session.append(f"- sync: {sync_note}")
    elif shell["flavor"] == "admin":
        active_session.append(
            "- working dir: repo root, branch `main` — you maintain main "
            "directly (the only shell that does; every other shell works "
            "from a worktree and lands changes via PRs)")
    # The engine floor is a DIFFERENT tree from the shell's cwd, so it is
    # reported unconditionally — a worktree can be current while the tree its
    # ./sc resolves from is stale, and admin (repo root) got no drift line here
    # at all before this.
    if floor_note:
        active_session.append(f"- floor: {floor_note}")

    # Directly after ACTIVE SESSION and ahead of everything else the render
    # appends: it is this session's standing orders, and the whole unit exists
    # because a worker missed the context it was operating in. Empty for a
    # shell with no sprint role — the section is mandatory, never unconditional.
    sprint_directive = render_sprint_directive(con, shell_id)
    sprint_block = (["## SPRINT DIRECTIVE — MANDATORY", "", sprint_directive,
                     "", "---", ""] if sprint_directive else [])

    parts = [
        template,
        "",
        "## ACTIVE SESSION", "",
        *active_session,
        "", "---", "",
        *sprint_block,
        *first_run,
        "## OPERATOR", "", render_operator(user),
        "", "---", "",
        "## IDENTITY", "", render_identity(shell),
        "", "---", "",
        "## SYSTEM PROMPT", "", system_prompt,
        "", "---", "",
        "## CONNECTIONS", "", render_connections(map_con),
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
        "## API", "", render_api(api_port, api_key),
        "", "---", "",
        "## STATUS", "",
        f"- **Session:** {session_id}",
        f"- **Seed:** {counts['seed']}",
        f"- **L&S:** {render_lns_status(counts)}",
        f"- **Flags:** {counts['flags']} open",
        f"- **Inbox:** {counts['unread']} unread — `--message check` to surface.",
        f"- **Repo map:** {map_status}",
        f"- **Docs:** {docs_status}",
        "",
    ]
    if map_con is not None:
        map_con.close()
    return "\n".join(parts)
