#!/usr/bin/env python3
"""`./sc skill` — the explicit write surface for the skill catalogue (#237).

Skill grants used to live only as raw SQL blocks inside the
local_skill_management skill, executable solely through the `sc sql-rw`
escape hatch — and a grant whose skill name didn't resolve was a SILENT
no-op (`INSERT ... SELECT` over zero rows, #253). This surface makes the
lifecycle first-class and loud: unknown skill or shell names are hard
errors, engine skills refuse `rm` (the seed would just resurrect them),
and every write reminds you that `./sc snapshot` is the persistence step.

Catalogue rows themselves are authored as assets + `./sc seed-skills`
(engine + fork-local alike); this command manages what's GRANTED where,
and retires local skills.

Usage:
    ./sc skill list                        catalogue: origin, common, grants
    ./sc skill grant  <name> <shell>...    grant a skill to shell(s) (id or shortname)
    ./sc skill revoke <name> <shell>...    revoke a skill from shell(s)
    ./sc skill rm     <name>               soft-delete a LOCAL skill + revoke all grants
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_driver  # noqa: E402
import seed_skills  # noqa: E402 — seeded_skill_names is the engine/local line

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"


def connect():
    if not DB_PATH.exists() or not DB_PATH.stat().st_size:
        sys.exit("sc skill: no live DB — run `./sc rebuild` (or `./sc launch`) first.")
    return db_driver.connect(DB_PATH)


def resolve_shell(con, ref: str) -> tuple[int, str]:
    """A shell by id or shortname → (shell_id, label). Loud on a miss."""
    if ref.isdigit():
        row = con.execute(
            "SELECT shell_id, COALESCE(shortname, display_name, shell_id) FROM shells "
            "WHERE shell_id=? AND COALESCE(is_deleted,0)=0", (int(ref),)).fetchone()
    else:
        row = con.execute(
            "SELECT shell_id, shortname FROM shells "
            "WHERE shortname=? COLLATE NOCASE AND COALESCE(is_deleted,0)=0",
            (ref,)).fetchone()
    if row:
        return row[0], str(row[1])
    have = con.execute(
        "SELECT shell_id, COALESCE(shortname, display_name, '?') FROM shells "
        "WHERE COALESCE(is_deleted,0)=0 ORDER BY shell_id").fetchall()
    sys.exit(f"sc skill: no shell '{ref}' — have: "
             + ", ".join(f"{i} ({n})" for i, n in have))


def resolve_skill(con, name: str) -> int:
    """A live skill row by name. Loud on a miss — the silent-no-op killer."""
    row = con.execute(
        "SELECT skill_id FROM skills WHERE name=? AND is_deleted=0", (name,)).fetchone()
    if row:
        return row[0]
    sys.exit(f"sc skill: no skill '{name}' in the live DB — author "
             f".super-coder/assets/skills/{name}/SKILL.md then `./sc seed-skills`.")


def persist_note() -> None:
    print("→ persist: ./sc snapshot   (serializes to .sc-state/content.sql — commit it)")


def cmd_list(con) -> int:
    engine = set(seed_skills.seeded_skill_names())
    rows = con.execute(
        "SELECT s.skill_id, s.name, s.common, s.is_deleted, "
        "  (SELECT GROUP_CONCAT(COALESCE(sh.shortname, sh.shell_id), ', ') "
        "   FROM shell_skills ss JOIN shells sh ON sh.shell_id = ss.shell_id "
        "   WHERE ss.skill_id = s.skill_id AND COALESCE(sh.is_deleted,0)=0) "
        "FROM skills s ORDER BY s.is_deleted, s.name").fetchall()
    if not rows:
        print("(no skills)")
        return 0
    w = max(len(r[1]) for r in rows)
    for _, name, common, deleted, grants in rows:
        origin = "engine" if name in engine else "local "
        tag = "common" if common else "opt-in"
        dead = "  [deleted]" if deleted else ""
        print(f"{name:<{w}}  {origin}  {tag}  → {grants or '(ungranted)'}{dead}")
    return 0


def cmd_grant(con, name: str, shell_refs: list[str]) -> int:
    skill_id = resolve_skill(con, name)
    for ref in shell_refs:
        shell_id, label = resolve_shell(con, ref)
        cur = con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (shell_id, skill_id))
        print(f"grant: {name} → {label}"
              + ("" if cur.rowcount else "  (already granted)"))
    con.commit()
    persist_note()
    return 0


def cmd_revoke(con, name: str, shell_refs: list[str]) -> int:
    skill_id = resolve_skill(con, name)
    for ref in shell_refs:
        shell_id, label = resolve_shell(con, ref)
        cur = con.execute(
            "DELETE FROM shell_skills WHERE shell_id=? AND skill_id=?",
            (shell_id, skill_id))
        print(f"revoke: {name} ⇸ {label}"
              + ("" if cur.rowcount else "  (was not granted)"))
    con.commit()
    persist_note()
    return 0


def cmd_rm(con, name: str) -> int:
    skill_id = resolve_skill(con, name)
    if name in set(seed_skills.seeded_skill_names()):
        sys.exit(f"sc skill: '{name}' is an ENGINE skill — the seed re-inserts it "
                 "on every update/rebuild, so a local rm cannot stick. Retire it "
                 "upstream (or just `./sc skill revoke` it from your shells).")
    n = con.execute("DELETE FROM shell_skills WHERE skill_id=?", (skill_id,)).rowcount
    con.execute("UPDATE skills SET is_deleted=1 WHERE skill_id=?", (skill_id,))
    con.commit()
    print(f"rm: {name} soft-deleted, {n} grant(s) revoked.")
    asset = ENGINE / "assets" / "skills" / name
    if asset.exists():
        print(f"  note: {asset.relative_to(ENGINE.parent)} still exists — remove it "
              "or `./sc seed-skills` will re-insert the skill.")
    persist_note()
    return 0


def main(argv: list[str]) -> int:
    usage = ("usage: ./sc skill list | grant <name> <shell>... | "
             "revoke <name> <shell>... | rm <name>")
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage)
        return 0
    cmd, args = argv[0], argv[1:]
    con = connect()
    try:
        if cmd == "list" and not args:
            return cmd_list(con)
        if cmd == "grant" and len(args) >= 2:
            return cmd_grant(con, args[0], args[1:])
        if cmd == "revoke" and len(args) >= 2:
            return cmd_revoke(con, args[0], args[1:])
        if cmd == "rm" and len(args) == 1:
            return cmd_rm(con, args[0])
        sys.exit(usage)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
