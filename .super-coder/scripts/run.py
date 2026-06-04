#!/usr/bin/env python3
"""Launch a shell against this repo.

super-coder is forked into ONE repo, so a shell works the repo root — no
per-shell workdir, no cross-repo cwd confusion (that is the whole inversion).

Flow:
    1. username-only auth (v1: no password challenge — pick a name)
    2. pick a shell (arg shortname · --first · interactive picker)
    3. open a session archive row
    4. compose the boot artifact and dual-write CLAUDE.md + AGENTS.md at root
    5. exec the harness  (skipped when RENDER_ONLY=1 — used to verify headless)

Usage:
    python3 .super-coder/scripts/run.py [shortname] [--first]
    RENDER_ONLY=1 python3 .super-coder/scripts/run.py --first   # render, don't exec
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "render"))
from compose import compose_boot  # noqa: E402
import flat  # noqa: E402

# The launch command per harness. The adapters/ seam owns this for real in B1;
# inline here keeps the spine self-contained. HARNESS env overrides.
LAUNCH = {
    "claude": ["claude"],
    "opencode": ["opencode"],
}


def _configured_harness() -> str | None:
    cfg = ENGINE / "instance.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("harness")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def open_db() -> sqlite3.Connection:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(
            f"FATAL: no usable DB at {DB_PATH}.\n"
            f"  Rebuild it from text:  make rebuild"
        )
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("SELECT 1 FROM shells LIMIT 1")  # smoke
    return con


# ── Auth (username-only) ────────────────────────────────────────────────────

def authenticate(con: sqlite3.Connection) -> sqlite3.Row:
    username = input("Username: ").strip()
    if not username:
        sys.exit("aborted")
    row = con.execute(
        "SELECT user_id, username FROM users "
        "WHERE LOWER(username)=LOWER(?) AND is_active=1",
        (username,),
    ).fetchone()
    if row is None:
        sys.exit(f"no active user '{username}'")
    return row


# ── Shell selection ─────────────────────────────────────────────────────────

def list_shells(con: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT shell_id, display_name, shortname, mandate, is_shared FROM shells "
        "WHERE (user_id=? OR is_shared=1) AND COALESCE(is_deleted,0)=0 "
        "ORDER BY is_shared, shell_id",
        (user_id,),
    ).fetchall()


def pick_shell(shells: list[sqlite3.Row], requested: str | None,
               first: bool) -> sqlite3.Row:
    if not shells:
        sys.exit("FATAL: no shells available to this user.")
    if requested:
        chosen = next((s for s in shells if s["shortname"] == requested), None)
        if chosen is None:
            avail = ", ".join(s["shortname"] or "?" for s in shells)
            sys.exit(f"no shell '{requested}'. Available: {avail}")
        return chosen
    if first or not sys.stdin.isatty():
        return shells[0]
    # Interactive picker
    print(f"\n{'ID':>3}  {'Name':<16}{'Shortname':<14}Mandate")
    for s in shells:
        print(f"{s['shell_id']:>3}  {(s['display_name'] or ''):<16}"
              f"{(s['shortname'] or ''):<14}{s['mandate'] or ''}")
    valid = {s["shell_id"] for s in shells}
    while True:
        choice = input("\nPick (ID): ").strip()
        if choice.isdigit() and int(choice) in valid:
            return next(s for s in shells if s["shell_id"] == int(choice))
        print("  invalid id")


# ── Session archive ─────────────────────────────────────────────────────────

def open_session(con: sqlite3.Connection, shell_id: int) -> tuple[str, int]:
    last = con.execute(
        "SELECT MAX(CAST(session_id AS INTEGER)) FROM shell_memory_archives WHERE shell_id=?",
        (shell_id,),
    ).fetchone()[0]
    session_id = f"{(last or 0) + 1:04d}"
    today, now_hm = str(date.today()), datetime.now().strftime("%H:%M")
    narrative = (f"# {session_id} | {today} | session opened\n\n"
                 f"## Narrative\n\n[{now_hm}] Session start.\n")
    cur = con.execute(
        "INSERT INTO shell_memory_archives (shell_id, session_id, date, full_narrative) "
        "VALUES (?, ?, ?, ?)",
        (shell_id, session_id, today, narrative),
    )
    archive_id = cur.lastrowid
    con.execute("UPDATE shells SET active_archive_id=? WHERE shell_id=?",
                (archive_id, shell_id))
    con.commit()
    return session_id, archive_id


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    first = "--first" in args
    positional = [a for a in args if not a.startswith("-")]
    requested = positional[0] if positional else None

    con = open_db()
    user = authenticate(con)
    chosen = pick_shell(list_shells(con, user["user_id"]), requested, first)

    session_id, archive_id = open_session(con, chosen["shell_id"])

    full = con.execute(
        "SELECT shell_id, display_name, shortname, partner, role, mandate, "
        "current_state, system_prompt, workspace FROM shells WHERE shell_id=?",
        (chosen["shell_id"],),
    ).fetchone()
    content = compose_boot(con, full, user, session_id, archive_id)

    # Render this shell's granted skills to .claude/skills/<name>/SKILL.md —
    # harness-consumed, gitignored, rebuilt per boot (like the boot artifact).
    skills = flat.render_skill_md(con, full["shell_id"])
    con.close()

    # One compose, two outputs — Claude Code reads CLAUDE.md, the AGENTS.md
    # harnesses read AGENTS.md. Both at the repo root.
    for name in ("CLAUDE.md", "AGENTS.md"):
        atomic_write(REPO_ROOT / name, content)

    print(f"\n→ booted {full['display_name']} "
          f"(shell_id={full['shell_id']}, session={session_id})")
    print(f"→ wrote {REPO_ROOT/'CLAUDE.md'}")
    print(f"→ wrote {REPO_ROOT/'AGENTS.md'}")
    print(f"→ skills: {len(skills['written'])} written, "
          f"{len(skills['skipped'])} unchanged → .claude/skills/")

    if os.environ.get("RENDER_ONLY"):
        print("→ RENDER_ONLY set — not exec'ing the harness.")
        return

    # Harness: HARNESS env wins, else this fork's configured harness
    # (instance.json, set by the installer), else claude.
    harness = os.environ.get("HARNESS") or _configured_harness() or "claude"
    cmd = LAUNCH.get(harness)
    if not cmd:
        sys.exit(f"unknown harness '{harness}' (known: {', '.join(LAUNCH)})")
    os.chdir(REPO_ROOT)
    print(f"→ exec {' '.join(cmd)}\n")
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
