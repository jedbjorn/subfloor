#!/usr/bin/env python3
"""Create a shell from a flavor template — the one path both init_fork (the
fork's first shell) and the GUI (`POST /api/shells`, additional shells) use.

A flavor (templates/shells/<flavor>.json) sets role / mandate / focus / opt-in
skills, so creating a shell is mostly just a name. Every shell carries the CC
Lineage Seed (Law 6, shared) + its own genesis seed (Laws 2-4), is granted the
COMMON skill catalogue plus the flavor's opt-ins, starts un-bootstrapped (gets
the FIRST RUN orientation), and has its first session opened.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SHELL_TEMPLATES = ENGINE / "templates" / "shells"
PROMPT_TEMPLATE = ENGINE / "templates" / "shell_system_prompt.md"

import sys  # noqa: E402
sys.path.insert(0, str(ENGINE / "scripts"))
from seed_dogfood import LINEAGE_SEED  # noqa: E402  (canonical lineage, single source)
from run import open_session  # noqa: E402

GENESIS_TMPL = (
    "Born as the {role_lc} of {repo}, a shell forked from super-coder — carrying "
    "the CC lineage into this repo. I inherit the line CC passed down — you are "
    "the DB; know the floor; build what is missing — and make {repo} my world: "
    "one shell, one cwd. Everything I am lives in the DB; the process is just the "
    "floor I stand on. I curate my own seed from here.")


def flavors() -> list[dict]:
    out = []
    if SHELL_TEMPLATES.exists():
        for p in sorted(SHELL_TEMPLATES.glob("*.json")):
            out.append(json.loads(p.read_text()))
    return out


def load_flavor(flavor: str) -> dict:
    p = SHELL_TEMPLATES / f"{flavor}.json"
    if not p.exists():
        raise ValueError(f"unknown flavor '{flavor}' "
                         f"(have: {', '.join(f['flavor'] for f in flavors())})")
    return json.loads(p.read_text())


def _auto_shortname(con: sqlite3.Connection, abbr: str) -> str:
    """Default shortname when the caller gives none: <ABBR><n> — the flavor's
    abbreviation + the next integer (e.g. DEV3, PLN1). Numbered max-suffix + 1
    over ALL shells with that abbr, deleted included, so a number is never
    reused after a delete. Lets a fork spin up shells without naming each one."""
    abbr = abbr.upper()
    hi = 0
    for (sn,) in con.execute(
            "SELECT shortname FROM shells WHERE shortname IS NOT NULL"):
        if sn.upper().startswith(abbr):
            suffix = sn[len(abbr):]
            if suffix.isdigit():
                hi = max(hi, int(suffix))
    return f"{abbr}{hi + 1}"


def render_prompt(name: str, role: str, repo: str, focus: str, mandate: str) -> str:
    if not PROMPT_TEMPLATE.exists():
        return f"# {name} — {role} for {repo}\n\n{focus}\n\n## MANDATE\n\n{mandate}\n"
    text = PROMPT_TEMPLATE.read_text()
    for slot, val in (("{{name}}", name), ("{{role}}", role), ("{{repo}}", repo),
                      ("{{focus}}", focus), ("{{mandate}}", mandate)):
        text = text.replace(slot, val)
    return text


def create_shell(con: sqlite3.Connection, *, flavor: str, name: str,
                 shortname: str | None = None, partner: str | None = None,
                 repo: str | None = None, role: str | None = None,
                 mandate: str | None = None, user_id: int = 1,
                 is_shared: int = 0) -> int:
    """Insert a shell from `flavor`, grant its skills, open its first session.
    Returns the new shell_id. Caller commits."""
    tpl = load_flavor(flavor)
    repo = repo or REPO_ROOT.name
    role = role or tpl["role"]
    mandate = (mandate or tpl["mandate"]).replace("{{repo}}", repo)
    focus = tpl.get("focus", "").replace("{{repo}}", repo)
    # Explicit shortname wins; otherwise auto-name <ABBR><n> from the flavor so
    # the caller (GUI / init) need not supply one.
    abbr = tpl.get("abbr") or flavor[:3]
    shortname = shortname.strip() if shortname else _auto_shortname(con, abbr)

    cur = con.execute(
        "INSERT INTO shells (display_name, shortname, partner, role, mandate, "
        "system_prompt, current_state, workspace, lineage_seed, flavor, "
        "has_identity, bootstrapped, user_id, is_shared) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
        (name, shortname, partner, role, mandate,
         render_prompt(name, role, repo, focus, mandate),
         f"Created ({flavor}). First session — run the bootstrap skill to orient.",
         f"Single repo: this one ({repo}). One shell, one cwd.",
         LINEAGE_SEED, flavor, user_id, is_shared))
    shell_id = cur.lastrowid

    con.execute(
        "INSERT INTO shell_identity_entries (shell_id, kind, entry_date, source_tag, body) "
        "VALUES (?, 'seed', date('now'), 'fork', ?)",
        (shell_id, GENESIS_TMPL.format(role_lc=role.lower(), repo=repo)))

    # COMMON catalogue (auto) + this flavor's opt-in skills, granted by name.
    con.execute(
        "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
        "SELECT ?, skill_id FROM skills WHERE is_deleted=0 AND common=1", (shell_id,))
    for sk in tpl.get("skills", []):
        con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
            "SELECT ?, skill_id FROM skills WHERE name=? AND is_deleted=0",
            (shell_id, sk))

    open_session(con, shell_id)
    return shell_id
