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

ENGINE skills can't be `rm`'d (the seed resurrects them on every update) —
they retire via the fork retire list instead (#238): `retire` writes the
name to `.sc-state/skills_retired.json` (tracked, fork-owned — commit it)
and flips the row to is_deleted=1, which every surface already filters on.
The list is re-applied after every seed sync/heal/rebuild, so it rides
`./sc update` the same way flavor overlays do. Grant rows stay in place
(inert) so `unretire` restores who-had-what.

Usage:
    ./sc skill list                        catalogue: origin, common, grants
    ./sc skill grant  <name> <shell>...    grant a skill to shell(s) (id or shortname)
    ./sc skill revoke <name> <shell>...    revoke a skill from shell(s)
    ./sc skill rm     <name>               soft-delete a LOCAL skill + revoke all grants
    ./sc skill retire   <name>             retire an ENGINE skill fork-wide (durable)
    ./sc skill unretire <name>             restore a retired engine skill (+ its grants)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_driver  # noqa: E402
import artifact_policy  # noqa: E402
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
        "SELECT skill_id, is_deleted FROM skills WHERE name=?", (name,)).fetchone()
    if row and not row[1]:
        return row[0]
    if row and name in seed_skills.retired_skill_names():
        sys.exit(f"sc skill: '{name}' is retired on this fork "
                 f"(.sc-state/skills_retired.json) — `./sc skill unretire {name}` "
                 "to restore it.")
    if row:
        sys.exit(f"sc skill: '{name}' is soft-deleted — re-author + `./sc seed-skills` "
                 "to restore it.")
    sys.exit(f"sc skill: no skill '{name}' in the live DB — author "
             f".super-coder/assets/skills/{name}/SKILL.md then `./sc seed-skills`.")


def persist_note() -> None:
    target = artifact_policy.content_path().relative_to(ENGINE.parent)
    suffix = " — commit it" if artifact_policy.tracks_local_artifacts() else " — local, ignored"
    print(f"→ persist: ./sc snapshot   (serializes to {target}{suffix})")


def cmd_list(con) -> int:
    engine = set(seed_skills.seeded_skill_names())
    retired = set(seed_skills.retired_skill_names())
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
        dead = ("  [retired]" if name in retired else "  [deleted]") if deleted else ""
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


def _write_retire_list(names: list[str]) -> None:
    artifact_policy.atomic_write_text(
        seed_skills.RETIRED_FILE,
        json.dumps(sorted(set(names)), indent=2) + "\n",
    )


def _display_retire_file() -> Path:
    try:
        return seed_skills.RETIRED_FILE.relative_to(ENGINE.parent)
    except ValueError:
        return seed_skills.RETIRED_FILE


def cmd_retire(con, name: str) -> int:
    if name not in set(seed_skills.seeded_skill_names()):
        if con.execute("SELECT 1 FROM skills WHERE name=?", (name,)).fetchone():
            sys.exit(f"sc skill: '{name}' is a LOCAL skill — `./sc skill rm {name}` "
                     "retires it (the retire list is for engine skills the seed "
                     "would resurrect).")
        sys.exit(f"sc skill: no engine skill '{name}' — `./sc skill list` shows the "
                 "catalogue.")
    names = seed_skills.retired_skill_names()
    already = name in names
    if not already:
        _write_retire_list(names + [name])
    seed_skills.apply_retired(con)
    dormant = con.execute(
        "SELECT COUNT(*) FROM shell_skills ss JOIN skills s ON s.skill_id=ss.skill_id "
        "WHERE s.name=?", (name,)).fetchone()[0]
    rel = _display_retire_file()
    print(f"retire: {name}" + ("  (already listed)" if already else "")
          + f" — retired fork-wide; {dormant} grant(s) kept dormant "
          "(restored on unretire).")
    action = "commit" if artifact_policy.tracks_local_artifacts() else "kept local at"
    print(f"→ {action} {rel} — the list rides `./sc update`.")
    return 0


def cmd_unretire(con, name: str) -> int:
    names = seed_skills.retired_skill_names()
    if name not in names:
        sys.exit(f"sc skill: '{name}' is not on the retire list "
                 f"({seed_skills.RETIRED_FILE}).")
    _write_retire_list([n for n in names if n != name])
    seed_skills.apply_retired(con)
    grants = con.execute(
        "SELECT COUNT(*) FROM shell_skills ss JOIN skills s ON s.skill_id=ss.skill_id "
        "WHERE s.name=?", (name,)).fetchone()[0]
    rel = _display_retire_file()
    print(f"unretire: {name} — restored with {grants} grant(s) live again.")
    action = "commit" if artifact_policy.tracks_local_artifacts() else "kept local at"
    print(f"→ {action} {rel}.")
    return 0


def cmd_rm(con, name: str) -> int:
    skill_id = resolve_skill(con, name)
    if name in set(seed_skills.seeded_skill_names()):
        sys.exit(f"sc skill: '{name}' is an ENGINE skill — the seed re-inserts it "
                 "on every update/rebuild, so a local rm cannot stick. "
                 f"`./sc skill retire {name}` retires it fork-wide (durable), or "
                 "`./sc skill revoke` removes it per shell.")
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
             "revoke <name> <shell>... | rm <name> | retire <name> | "
             "unretire <name>")
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
        if cmd == "retire" and len(args) == 1:
            return cmd_retire(con, args[0])
        if cmd == "unretire" and len(args) == 1:
            return cmd_unretire(con, args[0])
        sys.exit(usage)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
