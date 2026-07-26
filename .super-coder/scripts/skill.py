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

`add` authors a LOCAL skill straight into the DB. It exists for the `curate`
skill's promote pass: a cluster of L&S entries that keeps recurring across
sessions is a *process*, not a lesson, and relocating it to a skill is the
pressure valve that makes a hard L&S budget survivable — knowledge moves to a
lazy surface instead of being deleted. Local means "name absent from the engine
seed", which is already the engine/local boundary every heal path respects
(seed_skills._engine_specs / stale_engine_skills / sync_engine_skills), so an
added skill survives migrate, sync, and rebuild with no new column or marker.
It writes NO asset file, deliberately: `./sc seed-skills` upserts every asset
under assets/skills/, so a file would put a local skill back on the seed path.

Usage:
    ./sc skill list                        catalogue: origin, common, grants
    ./sc skill add <name> --file <path>    author a LOCAL skill (DB-only) + grant it
                  [--desc "…"] [--category …] [--for <shell>] [--opt-in]
    ./sc skill grant  <name> <shell>...    grant a skill to shell(s) (id or shortname)
    ./sc skill revoke <name> <shell>...    revoke a skill from shell(s)
    ./sc skill rm     <name>               soft-delete a LOCAL skill + revoke all grants
    ./sc skill retire   <name>             retire an ENGINE skill fork-wide (durable)
    ./sc skill unretire <name>             restore a retired engine skill (+ its grants)
"""
from __future__ import annotations

import argparse
import json
import os
import re
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


def resolve_author(con, ref: "str | None") -> tuple[int, str]:
    """Who is authoring this skill. `--for` when given, else the shell whose
    api_key is the SC_API_TOKEN run.py injected at boot — the same identity the
    memory API resolves, so a shell never names itself. A host seat with neither
    is told to say who it means rather than silently authoring an orphan."""
    if ref:
        return resolve_shell(con, ref)
    token = os.environ.get("SC_API_TOKEN", "")
    if token:
        row = con.execute(
            "SELECT shell_id, COALESCE(shortname, display_name, shell_id) FROM shells "
            "WHERE api_key=? AND COALESCE(is_deleted,0)=0", (token,)).fetchone()
        if row:
            return row[0], str(row[1])
    sys.exit("sc skill add: can't tell who is authoring — no SC_API_TOKEN "
             "resolves to a shell. Pass --for <shell>.")


def cmd_add(con, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sc skill add", add_help=False)
    ap.add_argument("name")
    ap.add_argument("--file", required=True, help="markdown body → skills.content")
    ap.add_argument("--desc", help="one-line summary (boot SKILLS block + catalogue)")
    ap.add_argument("--category")
    ap.add_argument("--command")
    ap.add_argument("--for", dest="author", help="author/grantee shell (default: your token)")
    ap.add_argument("--opt-in", action="store_true",
                    help="not a common skill (default: common, like the engine catalogue)")
    args = ap.parse_args(argv)
    name = args.name.strip()

    author_id, author = resolve_author(con, args.author)

    # Guard 1 — never shadow an engine name. The seed UPSERTs BY NAME, so
    # authoring over one would silently overwrite the upstream skill here and
    # then be silently overwritten back on the next update.
    if name in set(seed_skills.seeded_skill_names()):
        sys.exit(f"sc skill add: '{name}' is an ENGINE skill — the seed owns that "
                 "name and upserts it on every update. Pick a different one "
                 f"(e.g. {author}_{name}).")
    # Guard 2 — namespace by shortname. A future upstream release claiming a
    # bare name would clobber this shell's content on the next sync; a
    # shortname prefix is a name upstream will never mint.
    if not re.match(rf"^{re.escape(author)}_[a-z0-9_]+$", name):
        sys.exit(f"sc skill add: local skills are namespaced — name it "
                 f"'{author}_<topic>' (lowercase, underscores). A bare name is "
                 "one an upstream release could claim, and the sync would then "
                 "overwrite your content.")
    # Guard 3 — a name on the fork retire list is re-asserted is_deleted=1 by
    # every heal, so the skill would vanish the first time anything synced.
    if name in set(seed_skills.retired_skill_names()):
        sys.exit(f"sc skill add: '{name}' is on the fork retire list "
                 f"({_display_retire_file()}) — every heal re-asserts is_deleted "
                 f"on it. `./sc skill unretire {name}` first, or pick another name.")
    # Guard 4 — DB-only. `./sc seed-skills` passes ALL asset specs, so an asset
    # file under assets/skills/<name>/ would put this local skill back on the
    # seed path. Refuse rather than write a row that a later seed can rewrite.
    asset = ENGINE / "assets" / "skills" / name
    if asset.exists():
        sys.exit(f"sc skill add: {asset.relative_to(ENGINE.parent)} exists — an "
                 "asset file puts the skill on the seed path. `sc skill add` is "
                 "DB-only; remove the asset dir or pick another name.")

    src = Path(args.file).expanduser()
    if not src.is_file():
        sys.exit(f"sc skill add: no such file '{args.file}'")
    content = src.read_text().strip()
    if not content:
        sys.exit(f"sc skill add: {args.file} is empty — a skill with no procedure "
                 "is a row nobody can read.")

    row = con.execute("SELECT is_deleted FROM skills WHERE name=?", (name,)).fetchone()
    con.execute(
        "INSERT INTO skills (name, description, category, command, common, content, "
        "is_deleted) VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
        "category=excluded.category, command=excluded.command, "
        "common=excluded.common, content=excluded.content, is_deleted=0",
        (name, args.desc, args.category, args.command,
         0 if args.opt_in else 1, content))
    skill_id = con.execute("SELECT skill_id FROM skills WHERE name=?", (name,)).fetchone()[0]
    # Guard 5 — auto-grant to the author, or the promotion produces a skill
    # nobody can read.
    con.execute("INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
                (author_id, skill_id))
    con.commit()
    verb = "updated" if row else "added"
    print(f"add: {name} {verb} (local, DB-only, {len(content)} chars) → granted to {author}")
    if not args.desc:
        print("  ⚠ no --desc — the boot SKILLS block lists the name with an empty "
              "summary; re-run with --desc to make it findable.")
    persist_note()
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
    usage = ("usage: ./sc skill list | add <name> --file <path> [--desc …] | "
             "grant <name> <shell>... | revoke <name> <shell>... | rm <name> | "
             "retire <name> | unretire <name>")
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage)
        return 0
    cmd, args = argv[0], argv[1:]
    con = connect()
    try:
        if cmd == "list" and not args:
            return cmd_list(con)
        if cmd == "add" and args:
            return cmd_add(con, args)
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
