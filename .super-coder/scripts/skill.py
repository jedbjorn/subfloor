#!/usr/bin/env python3
"""`./sc skill` — the explicit write surface for the skill catalogue (#237).

Skill grants used to live only as raw SQL blocks inside the
local_skill_management skill, executable solely through the `sc sql-rw`
escape hatch — and a grant whose skill name didn't resolve was a SILENT
no-op (`INSERT ... SELECT` over zero rows, #253). This surface makes the
lifecycle first-class and loud: unknown skill or shell names are hard
errors, engine skills refuse `rm` (the seed would just resurrect them),
and every write reminds you that `./sc snapshot` is the persistence step.
Naming a standard shell targets its shared flavor pack; naming a Bespoke shell
targets only that shell.

Catalogue rows themselves are authored as assets + `./sc seed-skills`
(engine + administrator-authored fork-local alike); this command manages
what's GRANTED where, and retires local skills.

ENGINE skills can't be `rm`'d (the seed resurrects them on every update) —
they retire via the fork retire list instead (#238): `retire` writes the
name to `.sc-state/skills_retired.json` (tracked, fork-owned — commit it)
and flips the row to is_deleted=1, which every surface already filters on.
The list is re-applied after every seed sync/heal/rebuild, so it rides
`./sc update` the same way flavor overlays do. Grant rows stay in place
(inert) so `unretire` restores who-had-what.

Usage:
    ./sc skill list                        catalogue: origin, common, grants
    ./sc skill grant  <name> <shell>...    grant via shell reference (flavor/Bespoke)
    ./sc skill revoke <name> <shell>...    revoke via shell reference
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
import mem  # noqa: E402
import seed_skills  # noqa: E402 — seeded_skill_names is the engine/local line
import skill_projection  # noqa: E402

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"


def connect():
    if not DB_PATH.exists() or not DB_PATH.stat().st_size:
        sys.exit("sc skill: no live DB — run `./sc rebuild` (or `./sc launch`) first.")
    return db_driver.connect(DB_PATH)


def _shell_api_enabled() -> bool:
    if not mem.SC_API_TOKEN:
        return False
    mem._PROG = "skill"
    mem._require_api()
    return True


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


def set_target_grant(con, shell_id: int, label: str, skill_id: int,
                     granted: bool) -> tuple[int, str]:
    """Mutate the owning pack for a shell and return (rowcount, scope label)."""
    flavor = con.execute(
        "SELECT flavor FROM shells WHERE shell_id=?", (shell_id,)).fetchone()[0]
    if flavor is not None:
        if granted:
            cur = con.execute(
                "INSERT OR IGNORE INTO flavor_skills (flavor, skill_id) "
                "VALUES (?, ?)", (flavor, skill_id))
        else:
            cur = con.execute(
                "DELETE FROM flavor_skills WHERE flavor=? AND skill_id=?",
                (flavor, skill_id))
        return cur.rowcount, f"{flavor} flavor"
    if granted:
        cur = con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (shell_id, skill_id))
    else:
        cur = con.execute(
            "DELETE FROM shell_skills WHERE shell_id=? AND skill_id=?",
            (shell_id, skill_id))
    return cur.rowcount, f"Bespoke {label}"


def grant_scopes(con, skill_id: int) -> list[str]:
    rows = con.execute(
        "SELECT 'flavor:' || flavor AS scope "
        "FROM flavor_skills WHERE skill_id=? "
        "UNION ALL "
        "SELECT 'shell:' || COALESCE(sh.shortname, sh.display_name, sh.shell_id) "
        "FROM shell_skills ss JOIN shells sh ON sh.shell_id=ss.shell_id "
        "WHERE ss.skill_id=? AND sh.flavor IS NULL "
        "AND COALESCE(sh.is_deleted,0)=0 ORDER BY scope",
        (skill_id, skill_id)).fetchall()
    return [r[0] for r in rows]


def grant_count(con, skill_id: int) -> int:
    return con.execute(
        "SELECT (SELECT COUNT(*) FROM flavor_skills WHERE skill_id=?) + "
        "(SELECT COUNT(*) FROM shell_skills ss "
        " JOIN shells sh ON sh.shell_id=ss.shell_id "
        " WHERE ss.skill_id=? AND sh.flavor IS NULL)",
        (skill_id, skill_id)).fetchone()[0]


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


def _reconcile_targets(con, shell_ids: list[int], action: str) -> None:
    try:
        skill_projection.reconcile_assignment_targets(con, shell_ids)
    except skill_projection.ProjectionError as exc:
        sys.exit(skill_projection.partial_failure_message(action, exc))


def _reconcile_all(con, action: str) -> None:
    try:
        skill_projection.reconcile_existing_checkouts(con)
    except skill_projection.ProjectionError as exc:
        sys.exit(skill_projection.partial_failure_message(action, exc))


def print_catalogue(rows: list[dict]) -> int:
    engine = set(seed_skills.seeded_skill_names())
    retired = set(seed_skills.retired_skill_names())
    if not rows:
        print("(no skills)")
        return 0
    w = max(len(row["name"]) for row in rows)
    for row in rows:
        name = row["name"]
        common = row["common"]
        deleted = row["is_deleted"]
        origin = "engine" if name in engine else "local "
        tag = "common" if common else "opt-in"
        dead = ("  [retired]" if name in retired else "  [deleted]") if deleted else ""
        scopes = ", ".join(row.get("grant_scopes") or []) or "(ungranted)"
        print(f"{name:<{w}}  {origin}  {tag}  → {scopes}{dead}")
    return 0


def cmd_list(con) -> int:
    rows = [dict(row) for row in con.execute(
        "SELECT s.skill_id, s.name, s.common, s.is_deleted "
        "FROM skills s ORDER BY s.is_deleted, s.name").fetchall()]
    for row in rows:
        row["grant_scopes"] = grant_scopes(con, row["skill_id"])
    return print_catalogue(rows)


def cmd_list_api() -> int:
    return print_catalogue(mem._api("GET", "/_sc/skills").get("skills") or [])


def cmd_grant(con, name: str, shell_refs: list[str]) -> int:
    skill_id = resolve_skill(con, name)
    targets: list[int] = []
    for ref in shell_refs:
        shell_id, label = resolve_shell(con, ref)
        targets.append(shell_id)
        changed, scope = set_target_grant(con, shell_id, label, skill_id, True)
        print(f"grant: {name} → {scope}"
              + ("" if changed else "  (already granted)"))
    con.commit()
    _reconcile_targets(con, targets, f"grant {name}")
    persist_note()
    return 0


def cmd_revoke(con, name: str, shell_refs: list[str]) -> int:
    skill_id = resolve_skill(con, name)
    targets: list[int] = []
    for ref in shell_refs:
        shell_id, label = resolve_shell(con, ref)
        targets.append(shell_id)
        changed, scope = set_target_grant(con, shell_id, label, skill_id, False)
        print(f"revoke: {name} ⇸ {scope}"
              + ("" if changed else "  (was not granted)"))
    con.commit()
    _reconcile_targets(con, targets, f"revoke {name}")
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
    _reconcile_all(con, f"retire {name}")
    dormant = grant_count(
        con, con.execute(
            "SELECT skill_id FROM skills WHERE name=?", (name,)).fetchone()[0])
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
    _reconcile_all(con, f"unretire {name}")
    grants = grant_count(
        con, con.execute(
            "SELECT skill_id FROM skills WHERE name=?", (name,)).fetchone()[0])
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
                 "`./sc skill revoke` removes it from a flavor or Bespoke shell.")
    n = con.execute("DELETE FROM flavor_skills WHERE skill_id=?", (skill_id,)).rowcount
    n += con.execute("DELETE FROM shell_skills WHERE skill_id=?", (skill_id,)).rowcount
    con.execute("UPDATE skills SET is_deleted=1 WHERE skill_id=?", (skill_id,))
    con.commit()
    _reconcile_all(con, f"rm {name}")
    print(f"rm: {name} soft-deleted, {n} grant(s) revoked.")
    asset = ENGINE / "assets" / "skills" / name
    if asset.exists():
        print(f"  note: {asset.relative_to(ENGINE.parent)} still exists — remove it "
              "or `./sc seed-skills` will re-insert the skill.")
    persist_note()
    return 0


def main(argv: list[str]) -> int:
    usage = ("usage: ./sc skill list | grant <name> <shell>... | "
             "revoke <name> <shell>... | rm <name> | "
             "retire <name> | unretire <name>")
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "list" and not args and _shell_api_enabled():
        return cmd_list_api()
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
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
