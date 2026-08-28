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
    "**If the map is wrong, report it — you don't map.** Surface the gap to "
    "the FnB. Open any blocking map-quality flag first; retain its numeric ID "
    "+ display name. Then send one `messaging` notice to `cartographer`:\n\n"
    "```text\n"
    "shape: <what is wrong> — paths: <region/>; ref: <feature/doc/PR>\n"
    "flags: <numeric_id>=<SC-name>[, <numeric_id>=<SC-name>] | none\n"
    "curate; verify and close each flag; mark this notice read last.\n"
    "```\n\n"
    "Use `flags: none` when no flag exists. Tell the FnB to boot the "
    "Cartographer; pass = its scoped map checks pass + every named flag is "
    "resolved + the notice is read. Keep working from what the map does show."
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
    "substrate), a gitignored dependency. Engine changes are authored upstream\n"
    "in subfloor (formerly super-coder) and delivered here by `./sc update`."
)
PROJECT_VS_ENGINE_SOURCE = (
    "**This repo IS the engine source — you are upstream.** `.super-coder/` is\n"
    "tracked here and is your work surface: engine changes are authored in this\n"
    "repo, land via branch → PR → merge, and reach every fork through *their*\n"
    "`./sc update`. There is no upstream above you — engine fixes come from\n"
    "here.\n"
    "\n"
    "Getting your own updates: after an engine change merges, sync with main\n"
    "and run `./sc update` — in this repo it skips fetch/materialize (the\n"
    "engine is already here) and reconciles in place: migrations, skills\n"
    "catalogue, repo map, snapshot. Restart your session to boot onto the new\n"
    "floor.\n"
    "\n"
    "Engine skills speak fork-language. Where a skill says \"never edit\n"
    "`.super-coder/`\" or \"report/file it upstream\", that guidance is for\n"
    "forks — author the fix directly here.\n"
    "\n"
    "**You are operating on the engine you are running — surgery on a moving\n"
    "car.** Four standing consequences, all of which have produced wrong\n"
    "answers in this repo:\n"
    "\n"
    "1. **Your `./sc` resolves the engine from the MAIN CHECKOUT, not your\n"
    "   worktree** (`sc:11-21` derives it from git's common dir). That tree\n"
    "   hosts the gitignored live DB and runs the server; being current in\n"
    "   your worktree says nothing about it. The `floor:` line in ACTIVE\n"
    "   SESSION reports exactly that.\n"
    "2. **Verify claims about engine code against the remote, not your\n"
    "   tree** — `git show origin/main:<path>`. A stale checkout answers\n"
    "   confidently, and a command that reads it inherits the staleness.\n"
    "3. **Pull after every merge; reconcile before restarting.** A restart\n"
    "   kills live sessions, so the FnB owns that boundary.\n"
    "4. **The live DB is the one you are migrating.** Back it up before\n"
    "   applying anything, and name the DB path explicitly rather than\n"
    "   trusting whichever one the dispatcher resolves.\n"
)

def render_project_vs_engine(
    source_mode: bool, devkit_declared: bool, devkit_repair: bool = False
) -> str:
    """Render repo position; dev-tool state has one role-scoped owner below."""
    del devkit_declared, devkit_repair
    return PROJECT_VS_ENGINE_SOURCE if source_mode else PROJECT_VS_ENGINE_FORK


DEV_TOOL_STATES = (
    "absent",
    "declared",
    "invalid",
    "ready",
    "failed",
    "stale",
    "advisory",
    "repair",
)
DEV_TOOL_HOOKS = ("deps", "test", "lint", "typecheck")
DEV_TOOL_RECOVERY = {
    "absent": "Add a tracked declaration only when the fork needs one.",
    "declared": "Run the exact configured hook to produce execution evidence.",
    "invalid": "Correct the named tracked input, then retry.",
    "ready": "Continue through the configured hook.",
    "failed": "Inspect retained evidence and retry the same supported surface.",
    "stale": "From the host run `sc launch`; use repair after a failed attempt.",
    "advisory": "Inspect the named evidence and submit a reviewed tracked remediation.",
    "repair": "Exit to the host, rerun `sc launch`, and require `ready`.",
}


def render_dev_tools(flavor: str | None, inventory: dict | None) -> str:
    """Render the exact fork tool/seat inventory for the roles that exercise it."""
    if flavor not in {"dev", "reviewer"}:
        return ""
    inventory = inventory or {
        "state": "absent",
        "checkout": "unavailable",
        "seat": "unavailable",
        "declaration": ".subfloor/dev-kit.json (absent)",
        "hooks": {},
        "sandbox": "absent",
        "provision": "absent",
        "evidence": ".sc-state/local/dev-kit/",
        "logs": ".sc-state/local/devkit-logs/",
        "baseline": {},
        "dev_port": "unavailable",
        "app_database": "unavailable",
    }
    state = inventory.get("state")
    if state not in DEV_TOOL_STATES:
        raise ValueError(f"unsupported dev-tool state: {state}")

    lines = [
        "## DEV TOOLS",
        "",
        f"- **Checkout:** `{inventory.get('checkout', 'unavailable')}`",
        f"- **Seat:** `{inventory.get('seat', 'unavailable')}`",
        f"- **Declaration:** {inventory.get('declaration', 'unavailable')}",
        f"- **State:** `{state}` — {inventory.get('detail') or DEV_TOOL_RECOVERY[state]}",
        "- **Hooks:**",
    ]
    hooks = inventory.get("hooks") or {}
    for name in DEV_TOOL_HOOKS:
        hook = hooks.get(name)
        if not hook:
            lines.append(f"  - `sc {name}` — unavailable (not declared)")
            continue
        status = hook.get("state", "unavailable")
        cwd = hook.get("cwd", "unavailable")
        executable = hook.get("executable", "unavailable")
        lines.append(
            f"  - `sc {name}` — {status}; cwd `{cwd}`; executable `{executable}`"
        )
    baseline = inventory.get("baseline") or {}
    baseline_text = ", ".join(
        f"`{name}` {status}" for name, status in sorted(baseline.items())
    ) or "unavailable"
    lines.extend(
        [
            f"- **Engine baseline:** {baseline_text}",
            f"- **Sandbox extension:** {inventory.get('sandbox', 'absent')}",
            f"- **Provisioning:** {inventory.get('provision', 'absent')}",
            f"- **Dev server:** {inventory.get('dev_port', 'unavailable')}",
            f"- **App database sidecar:** {inventory.get('app_database', 'unavailable')}",
            (
                f"- **Evidence:** "
                f"`{inventory.get('evidence', '.sc-state/local/dev-kit/')}`; "
                f"retained hook logs "
                f"`{inventory.get('logs', '.sc-state/local/devkit-logs/')}`; "
                "use `SC_DEVKIT_OUTPUT=full` for complete command output."
            ),
            f"- **Recovery:** {DEV_TOOL_RECOVERY[state]}",
        ]
    )
    return "\n".join(lines)


def render_execution_context(flavor: str | None, launch_mode: str) -> str:
    """Render the real execution seat without changing the shell's mandate."""
    if launch_mode not in {"container", "host"}:
        raise ValueError(f"unsupported launch mode: {launch_mode}")

    if launch_mode == "container":
        admin_note = ""
        if flavor == "admin":
            admin_note = (
                "\n\nThis is the contained Admin seat. Host-only engine "
                "recovery, root-checkout repair, and host process work require "
                "exiting this session and running `make dos-admin` from a host "
                "terminal."
            )
        return (
            "## EXECUTION CONTEXT\n\n"
            "You run **inside the sandbox container**; this repo is "
            "bind-mounted at its host path. The app the FnB watches in their "
            "browser is a separate host-supervised instance.\n\n"
            "- Run project dev servers in the sandbox on "
            "`0.0.0.0:$SC_DEV_PORT`; the published host URL is "
            "`http://127.0.0.1:$SC_DEV_PORT`.\n"
            "- Operate an existing host stack only through its supervisor "
            "(`pm2`, `systemd`, or `make`) — never start a competing process "
            "from this seat."
            f"{admin_note}"
        )

    admin_note = ""
    if flavor == "admin":
        admin_note = (
            "\n\nHost authority covers engine update, rollback, migration, "
            "root-checkout reconciliation, diagnosis, and recovery. Product "
            "feature work and product-runtime ownership stay with Dev and "
            "DevOps. The engine does not coordinate this seat with a container "
            "Admin session; the operator owns avoiding simultaneous use."
        )
    return (
        "## EXECUTION CONTEXT\n\n"
        "You run **directly on the host**. The host toolchain, network, "
        "creds, services, and files available to your user are in reach; use "
        "that authority only within this shell's mandate.\n\n"
        "- Run project dev servers on `$SC_DEV_PORT`, bound to `127.0.0.1` "
        "unless the task explicitly requires another interface.\n"
        "- Operate an existing process-supervised stack through its "
        "supervisor (`pm2`, `systemd`, or `make`) — never start a competing "
        "process."
        f"{admin_note}"
    )


def render_api_unreachable_guidance(
    flavor: str | None, launch_mode: str
) -> str:
    """Keep ordinary API failure ownership, with a host-Admin recovery branch."""
    if launch_mode not in {"container", "host"}:
        raise ValueError(f"unsupported launch mode: {launch_mode}")
    if flavor == "admin" and launch_mode == "host":
        return (
            "  If it reports \"API unreachable\", the host Admin boot remains "
            "valid. Diagnose with `sc health`, `sc logs`, and the read-only "
            "`sc sql` lane; restore the managed engine with `sc restart` / "
            "`make dos-r`. `sc mem` stays unavailable until the API returns — "
            "it never falls back to raw writes."
        )
    return (
        "  If it reports \"API unreachable\", the engine server is down — "
        "surface it to the FnB and stop; they restart it with `sc restart` / "
        "`make dos-r`."
    )


# The repo catalogue (dr_*) lives in its OWN db, separate from shell_db.db.
MAP_DB_PATH = ENGINE.parent / ".sc-state" / "map.db"

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

    root_count = con.execute(
        "SELECT COUNT(*) FROM dr_filepath WHERE instr(path, '/') = 0"
    ).fetchone()[0]
    sections = con.execute(
        "SELECT s.name, s.path_prefix, s.description, "
        "  (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || '%') AS n "
        "FROM dr_section s ORDER BY s.sort_order, s.name").fetchall()
    unsectioned = con.execute(
        "SELECT COUNT(*) FROM dr_filepath f WHERE instr(f.path, '/') > 0 AND NOT EXISTS "
        "(SELECT 1 FROM dr_section s WHERE f.path LIKE s.path_prefix || '%')"
    ).fetchone()[0]
    if root_count or sections or unsectioned:
        lines += ["", "**Sections** — `name · location · files · what's there`. Query a "
                  "section's leaves (file names + descriptions) on demand, never all at once:"]
        if root_count:
            lines.append(
                f"- **Repository Root** · `./` · {root_count} files — "
                "Top-level project entrypoints and metadata"
            )
        for s in sections:
            desc = f" — {s['description']}" if s["description"] else ""
            lines.append(f"- **{s['name']}** · `{s['path_prefix']}` · {s['n']} files{desc}")
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
        return "(API not configured — run `sc rebuild` to assign a key)"
    return (
        f"- **Base URL:** `http://127.0.0.1:{port}`\n\n"
        "Write memory via `sc mem` — it is already wired to this launched shell; the engine "
        "resolves API identity for you. "
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
    flag_columns = {row[1] for row in con.execute("PRAGMA table_info(flags)")}
    runtime_filter = (
        " AND COALESCE(blocks_runtime,1)=1"
        if "blocks_runtime" in flag_columns
        else ""
    )
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
        "flags": one(
            "SELECT COUNT(*) FROM flags WHERE shell_id=? AND resolved=0 "
            "AND is_deleted=0" + runtime_filter
        ),
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


def render_target_freshness(
    floor_note: "str | None",
    work_repo_note: "str | None",
) -> list[str]:
    """Keep live-floor and declared-work evidence visibly independent."""
    lines: list[str] = []
    if floor_note:
        lines.append(f"- floor: {floor_note}")
    if work_repo_note:
        lines.append(f"- work repo: {work_repo_note}")
    return lines


def compose_boot(con: sqlite3.Connection, shell, user, session_id: str,
                 archive_id: int, work_dir: "Path | None" = None,
                 sync_note: "str | None" = None,
                 floor_note: "str | None" = None,
                 work_repo_note: "str | None" = None,
                 api_key: "str | None" = None,
                 api_port: "int | None" = None,
                 source_mode: bool = False,
                 devkit_declared: bool = False,
                 devkit_repair: bool = False,
                 dev_tools: "dict | None" = None,
                 launch_mode: str = "container") -> str:
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
    previously the only shell given no drift line at all. work_repo_note is the
    independently projected, never-mutated declared work surface; it must not
    inherit the substrate or floor result. source_mode flips
    PROJECT vs ENGINE to the
    source-repo variant (caller decides via install.is_source_repo() — compose
    stays a pure render, no git). devkit_declared is the caller's observation
    of `.subfloor/dev-kit.json` in the exact checkout receiving this boot.
    """
    template = TEMPLATE_PATH.read_text().rstrip()
    flavor = (shell["flavor"] if "flavor" in shell.keys() else None)
    template = template.replace(
        "{{project_vs_engine}}",
        render_project_vs_engine(source_mode, devkit_declared, devkit_repair))
    template = template.replace(
        "{{execution_context}}", render_execution_context(flavor, launch_mode)
    )
    dev_tools_block = render_dev_tools(flavor, dev_tools)
    if dev_tools_block:
        template = template.replace("{{dev_tools}}", dev_tools_block)
    else:
        template = template.replace("\n{{dev_tools}}\n", "\n")
    template = template.replace(
        "{{api_unreachable_guidance}}",
        render_api_unreachable_guidance(flavor, launch_mode),
    )
    shell_id = shell["shell_id"]
    counts = fetch_counts(con, shell_id)

    system_prompt = (shell["system_prompt"] or "").strip().replace("<self>", str(shell_id))
    current_state = (shell["current_state"] or "(none)").strip()

    # Orientation state: has this shell run first-run bootstrap, and is the repo
    # mapped? Drives the FIRST RUN prompt + the map-status line.
    bootstrapped = con.execute(
        "SELECT bootstrapped FROM shells WHERE shell_id=?", (shell_id,)).fetchone()[0]
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
                "You own the repo map and haven't completed setup. Run the "
                "**cartographer** skill now: inspect with `sc map-schema`, "
                "configure the local map, wire hooks with `sc map-setup`, install "
                "worktree-authored extractors through `sc map-extractor install`, "
                "and act on every `sc map finalize` row. Pass = finalization exits "
                "0 before you mark yourself oriented.")
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
    active_session.extend(render_target_freshness(floor_note, work_repo_note))

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
