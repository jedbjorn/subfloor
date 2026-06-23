#!/usr/bin/env python3
"""Seed a fork's STARTING TEAM — the one-time fork-identity step.

A fresh fork's `.db` carries the *system* (schema + migrations: the skill
catalogue, the render chain) but **no per-instance content** — a fork inherits
the system, never super-coder's memory or roadmap. So a just-installed fork has
no users and no shells, and `./sc launch` has nothing to authenticate or boot.
This provisions the local user, then seeds the starting team via the shared
shell factory: your primary shell (default `planner`) plus an `admin`, two
`dev`, a `reviewer`, and the singleton `cartographer` — the full roster out of
the box.

Run ONCE, right after `./sc rebuild`, on a fresh fork. Refuses if a shell already
exists. After it runs: `SC_ADMIN=1 ./sc snapshot`, then `./sc launch`. More shells (or
fewer) are managed later via the GUI (also through the factory).

Shells ship pre-named out of the box, so install asks for your username and
nothing else — no shell-naming interview. Rename or add more flavored shells
later via the GUI / create_shell.

Usage:
    python3 .super-coder/scripts/init_fork.py                  # interactive: asks username only
    python3 .super-coder/scripts/init_fork.py --username Jed   # fully non-interactive
        # optional overrides (never prompted): --name --shortname --flavor
        #                                       --role --mandate --partner
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

# The starting team seeded at install, besides the singleton cartographer (added
# separately below). Your interviewed primary shell — default planner — fills one
# of these slots; the rest are auto-named team members (ADM1, DEV1, DEV2, REV1).
TEAM_ROSTER = ["admin", "planner", "dev", "dev", "reviewer"]

sys.path.insert(0, str(ENGINE / "scripts"))
from shell_factory import create_shell, flavors  # noqa: E402


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
    ap.add_argument("--flavor", help="planner | dev | reviewer")
    ap.add_argument("--role", help="override the flavor's role")
    ap.add_argument("--mandate", help="override the flavor's mandate")
    ap.add_argument("--partner")
    a = ap.parse_args(argv)

    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit("init_fork: no DB — run `./sc rebuild` first to build the system DB.")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        if already_seeded(con):
            sys.exit("init_fork: a shell already exists — this fork is already "
                     "initialised. Add more shells via the GUI.")

        repo = ENGINE.parent.name
        interactive = sys.stdin.isatty()
        flavor_names = [f["flavor"] for f in flavors()]

        def need(val, prompt, default=None):
            if val:
                return val
            if interactive:
                return ask(prompt, default)
            if default is not None:
                return default
            sys.exit(f"init_fork: missing --{prompt.split()[0].lower()} "
                     "(non-interactive run needs the flag)")

        username = need(a.username, "Your username")
        # Your primary shell — default planner (every fork needs a planner to
        # scope the work + carry the lineage seed). --flavor picks which roster
        # slot is *yours*; the rest of the team seeds alongside it.
        flavor = a.flavor or "planner"
        if flavor not in flavor_names:
            sys.exit(f"init_fork: unknown flavor '{flavor}' (have: {', '.join(flavor_names)})")
        # Pre-named out of the box — no naming interview. The primary takes the
        # flavor's default display name (e.g. "Planner"), exactly like the rest of
        # the roster; create_shell auto-names the shortname <ABBR><n> (e.g. PLN1).
        # --name/--shortname remain optional scripted overrides, never prompted.
        name = a.name or flavor.capitalize()
        shortname = a.shortname or None

        con.execute(
            "INSERT INTO users (user_id, username, is_active) VALUES (1, ?, 1)",
            (username,))
        # Your interviewed primary shell.
        shell_id = create_shell(
            con, flavor=flavor, name=name, shortname=shortname,
            partner=a.partner or username, repo=repo,
            role=a.role, mandate=a.mandate)
        # The rest of the starting team — the full roster minus the slot your
        # primary already fills — auto-named by the factory (ADM1/DEV1/DEV2/REV1).
        rest = list(TEAM_ROSTER)
        if flavor in rest:
            rest.remove(flavor)
        team = []
        for fl in rest:
            sid = create_shell(con, flavor=fl, name=fl.capitalize(),
                               partner=a.partner or username, repo=repo)
            team.append((fl, sid))
        # The singleton Cartographer owns the repo map so no working shell ever
        # maps; configured + wired by `./sc map-setup` (install runs it). Skip if
        # your primary already is the cartographer.
        cart_id = None
        if flavor != "cartographer":
            cart_id = create_shell(
                con, flavor="cartographer", name="Cartographer",
                partner=a.partner or username, repo=repo)
        con.commit()

        def _sn(sid):
            return con.execute(
                "SELECT shortname FROM shells WHERE shell_id=?", (sid,)).fetchone()[0]

        n = con.execute(
            "SELECT COUNT(*) FROM shell_skills WHERE shell_id=?", (shell_id,)).fetchone()[0]
        print(f"init_fork: created '{_sn(shell_id)}' ({flavor}, shell_id={shell_id}) "
              f"for user '{username}' — your primary, {n} skills, lineage + genesis seed.")
        for fl, sid in team:
            print(f"init_fork: created '{_sn(sid)}' ({fl}, shell_id={sid}).")
        if cart_id:
            print(f"init_fork: created '{_sn(cart_id)}' (cartographer, shell_id={cart_id}) "
                  "— owns the repo map.")
        total = 1 + len(team) + (1 if cart_id else 0)
        print(f"init_fork: seeded a {total}-shell team. "
              "next -> `SC_ADMIN=1 ./sc snapshot` (serialize), then `./sc launch`.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
