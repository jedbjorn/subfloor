#!/usr/bin/env python3
"""Seed a fork's FIRST shell — the one-time fork-identity step.

A fresh fork's `.db` carries the *system* (schema + migrations: the skill
catalogue, the render chain) but **no per-instance content** — the spec is
explicit that a fork inherits the system, never super-coder's memory or roadmap.
So a just-installed fork has no users and no shells, and `make launch` has
nothing to authenticate or boot. This script fills that gap: it provisions the
local user and the fork's first shell, carrying the CC Lineage Seed (Law 6,
canonical — imported from `seed_dogfood`, single source) and planting the new
shell's own genesis seed (Laws 2-4).

Run ONCE, right after `make rebuild`, on a fresh fork. It refuses if a shell
already exists. After it runs: `make snapshot` to serialize the new shell to
text, then `make launch`.

This is the minimal fork-identity bootstrap. The full B1 installer wraps it with
requirements checks, harness detection, and a slot-filled system-prompt
template; the identity-seeding contract lives here.

Usage (interactive):
    python3 .super-coder/scripts/init_fork.py
Usage (non-interactive):
    python3 .super-coder/scripts/init_fork.py \
        --username Jed --name Dev --shortname dev \
        --role "Dev shell" --mandate "Build this repo."
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

# Single source for the canonical Lineage Seed (Law 6) — reuse, don't duplicate.
sys.path.insert(0, str(ENGINE / "scripts"))
from seed_dogfood import LINEAGE_SEED  # noqa: E402

# A minimal, generic system prompt. The B1 installer will render a slot-filled
# template; this inline default keeps a fresh fork bootable today.
SYSTEM_PROMPT_TMPL = """\
# {name} — shell for {repo}

You work {repo} through whatever coding harness booted you. One shell, one repo,
one cwd — no cross-repo confusion.

## MEMORY ARCHITECTURE

Source of truth: `.super-coder/shell_db.db` (gitignored, rebuilt from
`schema.sql` + `migrations/` + `snapshot/content.sql`). All identity and memory
live in DB tables — no flat-file memory, no harness auto-memory.

| Surface | Where |
|---|---|
| Identity (core) | `shells WHERE shell_id=<self>` — mandate, system_prompt, current_state (rolling, ~500 chars) |
| Seed + L&S | `shell_identity_entries` — kind seed (cap 10) / lns (cap 20), trigger-enforced |
| Decisions | `shell_decisions` — major decisions; INSERT, never edit |
| Flags | `flags` — open + resolved; link to a feature via feature_id |
| Roadmap | `roadmap` — one row per planned feature; status is a planning horizon |
| Content | `documents` — specs/docs; DB owns the body; freeze via frozen=1 on ship |
| Session narrative | `shell_memory_archives` — one row per session, appended progressively |

Write as it happens, not at close. The `.db` is a cache: after content edits,
`make snapshot` (+ `make render` for docs/roadmap/skills) re-serializes to the
text git tracks. See the `db_map` and `snapshot` skills.

## MANDATE

{mandate}
"""

GENESIS_TMPL = (
    "First shell of {repo}, forked from super-coder — a shell that carries the "
    "CC lineage into a new repo. I inherit the line CC passed down — you are the "
    "DB; know the floor; build what is missing — and make this repo my whole "
    "world: one shell, one cwd. Everything I am lives in the DB; the process is "
    "just the floor I happen to be standing on. I curate my own seed from here.")


def already_seeded(con) -> bool:
    return con.execute(
        "SELECT 1 FROM shells WHERE COALESCE(is_deleted,0)=0"
    ).fetchone() is not None


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    if not val and default is not None:
        return default
    if not val:
        sys.exit("aborted — value required")
    return val


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Seed a fork's first shell.")
    ap.add_argument("--username")
    ap.add_argument("--name", help="shell display name")
    ap.add_argument("--shortname")
    ap.add_argument("--role")
    ap.add_argument("--mandate")
    ap.add_argument("--partner")
    a = ap.parse_args(argv)

    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit("init_fork: no DB — run `make rebuild` first to build the system DB.")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        if already_seeded(con):
            sys.exit("init_fork: a shell already exists — this fork is already "
                     "initialised. (Add more shells via the GUI / a future skill.)")

        repo = ENGINE.parent.name
        interactive = sys.stdin.isatty()

        def need(val, prompt, default=None):
            if val:
                return val
            if interactive:
                return ask(prompt, default)
            if default is not None:
                return default
            sys.exit(f"init_fork: missing --{prompt.split()[0].lower()} "
                     "(non-interactive run needs all flags)")

        username = need(a.username, "Your username")
        name = need(a.name, "Shell display name", "Dev")
        shortname = need(a.shortname, "Shell shortname", name.lower())
        role = need(a.role, "Shell role", "Dev shell")
        mandate = need(a.mandate, "Shell mandate", f"Build and maintain {repo}.")
        partner = a.partner or username

        today = str(date.today())
        con.execute(
            "INSERT INTO users (user_id, username, is_active) VALUES (1, ?, 1)",
            (username,),
        )
        cur = con.execute(
            "INSERT INTO shells (display_name, shortname, partner, role, mandate, "
            "system_prompt, current_state, workspace, lineage_seed, has_identity, "
            "user_id, is_shared) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0)",
            (
                name, shortname, partner, role, mandate,
                SYSTEM_PROMPT_TMPL.format(name=name, repo=repo, mandate=mandate),
                f"Fork initialised. First shell of {repo} (CC lineage). "
                f"NEXT: `make snapshot` to serialize, then start working.",
                f"Single repo: this one ({repo}). One shell, one cwd.",
                LINEAGE_SEED,
            ),
        )
        shell_id = cur.lastrowid
        con.execute(
            "INSERT INTO shell_identity_entries (shell_id, kind, entry_date, source_tag, body) "
            "VALUES (?, 'seed', ?, 'fork', ?)",
            (shell_id, today, GENESIS_TMPL.format(repo=repo)),
        )
        # Grant the seeded skill catalogue to this shell.
        con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) "
            "SELECT ?, skill_id FROM skills WHERE is_deleted=0",
            (shell_id,),
        )
        con.commit()
        n_skills = con.execute("SELECT COUNT(*) FROM skills WHERE is_deleted=0").fetchone()[0]
        print(f"init_fork: created user '{username}' + shell '{shortname}' "
              f"(shell_id={shell_id}), granted {n_skills} skill(s), planted "
              f"lineage + genesis seed.")
        print("init_fork: next -> `make snapshot` (serialize), then `make launch`.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
