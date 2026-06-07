#!/usr/bin/env python3
"""Seed a fork's FIRST shell — the one-time fork-identity step.

A fresh fork's `.db` carries the *system* (schema + migrations: the skill
catalogue, the render chain) but **no per-instance content** — a fork inherits
the system, never super-coder's memory or roadmap. So a just-installed fork has
no users and no shells, and `./sc launch` has nothing to authenticate or boot.
This provisions the local user, then creates the first shell via the shared
shell factory (a flavor template — default `dev`).

Run ONCE, right after `./sc rebuild`, on a fresh fork. Refuses if a shell already
exists. After it runs: `./sc snapshot`, then `./sc launch`. Additional shells are
created later via the GUI (also through the factory).

Usage:
    python3 .super-coder/scripts/init_fork.py            # interactive
    python3 .super-coder/scripts/init_fork.py \
        --username Jed --name Dev --shortname dev --flavor dev
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

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
        # The first shell is a PLANNER shell — every fork needs a planner to
        # scope the work. (--flavor overrides; the GUI creates other flavors.)
        flavor = a.flavor or "planner"
        if flavor not in flavor_names:
            sys.exit(f"init_fork: unknown flavor '{flavor}' (have: {', '.join(flavor_names)})")
        name = need(a.name, "Shell display name", flavor.capitalize())
        # shortname is optional — omit it and create_shell auto-names <ABBR><n>
        # from the flavor (e.g. PLN1). --shortname still overrides.
        shortname = a.shortname or None

        con.execute(
            "INSERT INTO users (user_id, username, is_active) VALUES (1, ?, 1)",
            (username,))
        shell_id = create_shell(
            con, flavor=flavor, name=name, shortname=shortname,
            partner=a.partner or username, repo=repo,
            role=a.role, mandate=a.mandate)
        # Every fork also gets a dedicated Cartographer: it owns the repo map so
        # no working shell ever maps. Configured + wired by `./sc map-setup`
        # (install runs it); re-bootable to heal the map automation later.
        cart_id = create_shell(
            con, flavor="cartographer", name="Cartographer",
            partner=a.partner or username, repo=repo)
        con.commit()
        shortname = con.execute(
            "SELECT shortname FROM shells WHERE shell_id=?", (shell_id,)).fetchone()[0]
        cart_sn = con.execute(
            "SELECT shortname FROM shells WHERE shell_id=?", (cart_id,)).fetchone()[0]

        n = con.execute(
            "SELECT COUNT(*) FROM shell_skills WHERE shell_id=?", (shell_id,)).fetchone()[0]
        print(f"init_fork: created '{shortname}' ({flavor}, shell_id={shell_id}) "
              f"for user '{username}' — {n} skills, lineage + genesis seed, session opened.")
        print(f"init_fork: created '{cart_sn}' (cartographer, shell_id={cart_id}) "
              "— owns the repo map.")
        print("init_fork: next -> `./sc snapshot` (serialize), then `./sc launch`.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
