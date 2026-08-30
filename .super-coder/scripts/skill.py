#!/usr/bin/env python3
"""`./sc skill` — the explicit write surface for the skill catalogue (#237).

Skill grants used to live only as raw SQL blocks, where a grant whose skill
name didn't resolve was a SILENT no-op (`INSERT ... SELECT` over zero rows,
#253). This surface makes the
lifecycle first-class and loud: unknown skill or shell names are hard
errors, engine skills refuse `rm` (the seed would just resurrect them),
and every supported mutation persists the local snapshot and projections.
Naming a standard shell targets its shared flavor pack; naming a Bespoke shell
targets only that shell.

Engine catalogue rows are authored as assets + `./sc seed-skills`. Fork-local
rows are DB-canonical and enter through `put --file`; the input file remains a
draft and is never copied into managed engine assets.

ENGINE skills can't be `rm`'d (the seed resurrects them on every update) —
they retire via the fork retire list instead (#238): `retire` writes the
name to `.sc-state/skills_retired.json` (tracked, fork-owned — commit it)
and flips the row to is_deleted=1, which every surface already filters on.
The list is re-applied after every seed sync/heal/rebuild, so it rides
`./sc update` the same way flavor overlays do. Grant rows stay in place
(inert) so `unretire` restores who-had-what.

Usage:
    ./sc skill list                        catalogue: origin, common, grants
    ./sc skill put --file <SKILL.md>       create/update a DB-canonical LOCAL skill
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
import artifact_policy
import db_driver
import instance_state
import mem
import render as render_mod
import seed_skills
import skill_projection
import snapshot

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = instance_state.active_database_path(ENGINE)
MAX_SKILL_FILE_BYTES = 128 * 1024
LOCAL_FRONTMATTER_FIELDS = {"name", "description", "category", "command", "common"}


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


def require_planner(con) -> int:
    """Resolve the launched shell token locally and require Planner flavor."""
    if not mem.SC_API_TOKEN:
        sys.exit(
            "sc skill: `put` requires a launched Planner shell; no shell token "
            "is present"
        )
    row = con.execute(
        "SELECT shell_id, shortname, flavor FROM shells WHERE api_key=? "
        "AND COALESCE(is_deleted,0)=0",
        (mem.SC_API_TOKEN,),
    ).fetchone()
    if row is None:
        sys.exit("sc skill: `put` shell token does not resolve to an active shell")
    if row[2] != "planner":
        label = row[1] or row[0]
        sys.exit(
            f"sc skill: `put` is Planner-owned; shell {label} has flavor "
            f"{row[2] or 'bespoke'}"
        )
    return int(row[0])


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
        sys.exit(
            f"sc skill: '{name}' is soft-deleted — restore it with "
            f"`./sc skill put --file <{name}-SKILL.md>`"
        )
    sys.exit(
        f"sc skill: no skill '{name}' in the live DB — create a fork-local skill "
        "with `./sc skill put --file <SKILL.md>`"
    )


def _persist_snapshot(con) -> None:
    snapshot.persist_instance(con)


def _persist_render(con) -> None:
    render_mod.persist_visibility(con)


def _persist_mutation(con, action: str, reconcile) -> None:
    """Persist each post-commit layer and report exact partial durability."""
    try:
        with artifact_policy.content_write_lock():
            try:
                _persist_snapshot(con)
            except Exception as exc:  # noqa: BLE001 — report failed layer
                sys.exit(
                    f"sc skill: {action} committed in the DB, but snapshot "
                    f"persistence failed: {exc}. Flat render and skill projection "
                    "were not attempted; fix the named failure, then retry the "
                    "same `sc skill` command."
                )

            try:
                _persist_render(con)
            except Exception as exc:  # noqa: BLE001 — report failed layer
                sys.exit(
                    f"sc skill: {action} committed in the DB and snapshot, but flat "
                    f"render persistence failed: {exc}. Skill projection was not "
                    "attempted; fix the named failure, then retry the same `sc skill` "
                    "command."
                )
    except Exception as exc:  # noqa: BLE001 — lock/filesystem implementations vary
        sys.exit(
            f"sc skill: {action} committed in the DB, but the snapshot/render "
            f"serialization lock failed before persistence: {exc}. Snapshot, flat "
            "render, and skill projection were not attempted; fix the named failure, "
            "then retry the same `sc skill` command."
        )

    try:
        reconcile()
    except Exception as exc:  # noqa: BLE001 — projection adapters vary by harness
        sys.exit(
            f"sc skill: {action} committed in the DB, snapshot, and flat render, "
            f"but skill projection failed: {exc}. Fix the named path, then retry "
            "the same `sc skill` command."
        )
    print("persist: DB + snapshot + flat render + skill projections reconciled")


def parse_local_skill_file(path: Path) -> dict:
    """Validate the bounded flat frontmatter accepted by `skill put`."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        sys.exit(f"sc skill: cannot read draft {path}: {exc}")
    if len(raw) > MAX_SKILL_FILE_BYTES:
        sys.exit(
            f"sc skill: draft {path} is {len(raw)} bytes; maximum is "
            f"{MAX_SKILL_FILE_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        sys.exit(f"sc skill: draft {path} must be UTF-8: {exc}")

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        sys.exit(f"sc skill: draft {path} must start with YAML frontmatter")
    try:
        boundary = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        sys.exit(f"sc skill: draft {path} has no closing frontmatter delimiter")

    meta: dict[str, str] = {}
    for line in lines[1:boundary]:
        if not line.strip():
            continue
        if ":" not in line:
            sys.exit(f"sc skill: invalid frontmatter line in {path}: {line!r}")
        key, value = (part.strip() for part in line.split(":", 1))
        if key not in LOCAL_FRONTMATTER_FIELDS:
            sys.exit(f"sc skill: unsupported frontmatter field {key!r} in {path}")
        if key in meta:
            sys.exit(f"sc skill: duplicate frontmatter field {key!r} in {path}")
        meta[key] = value

    name = meta.get("name", "")
    description = meta.get("description", "")
    if not name or not seed_skills.SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
        sys.exit(
            "sc skill: frontmatter `name` must be 1-64 lowercase letters, "
            "digits, or underscores and start with a letter"
        )
    if not description:
        sys.exit("sc skill: frontmatter `description` must be a non-empty line")

    common_value = meta.get("common", "false").lower()
    if common_value not in {"false", "0", "no"}:
        if common_value in {"true", "1", "yes"}:
            sys.exit(
                "sc skill: fork-local skills must use `common: false`; assign "
                "them explicitly with `sc skill grant`"
            )
        sys.exit("sc skill: frontmatter `common` must be true or false")

    body = "\n".join(lines[boundary + 1:]).strip()
    if not body:
        sys.exit(f"sc skill: draft {path} must include a non-empty procedure body")
    return {
        "name": name,
        "description": description,
        "category": meta.get("category") or None,
        "command": meta.get("command") or None,
        "common": 0,
        "content": body,
    }


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
    _persist_mutation(
        con,
        f"grant {name}",
        lambda: skill_projection.reconcile_assignment_targets(con, targets),
    )
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
    _persist_mutation(
        con,
        f"revoke {name}",
        lambda: skill_projection.reconcile_assignment_targets(con, targets),
    )
    return 0


def cmd_put(con, path: Path) -> int:
    require_planner(con)
    spec = parse_local_skill_file(path)
    name = spec["name"]
    reserved = set(seed_skills.seeded_skill_names()) | set(
        seed_skills.tombstoned_skill_names()
    )
    if name in reserved:
        sys.exit(
            f"sc skill: '{name}' is ENGINE-owned and cannot be overwritten by "
            "the fork-local lane; change it in the upstream engine skill workflow"
        )

    existing = con.execute(
        "SELECT skill_id, is_deleted FROM skills WHERE name=?", (name,)
    ).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO skills (name, description, category, command, common, "
            "content, is_deleted) VALUES (?, ?, ?, ?, 0, ?, 0)",
            (
                name,
                spec["description"],
                spec["category"],
                spec["command"],
                spec["content"],
            ),
        )
        verb = "created"
    else:
        con.execute(
            "UPDATE skills SET description=?, category=?, command=?, common=0, "
            "content=?, is_deleted=0 WHERE skill_id=?",
            (
                spec["description"],
                spec["category"],
                spec["command"],
                spec["content"],
                existing[0],
            ),
        )
        verb = "restored" if existing[1] else "updated"
    con.commit()
    _persist_mutation(
        con,
        f"put {name}",
        lambda: skill_projection.reconcile_existing_checkouts(con),
    )
    print(f"put: {name} {verb} in the DB; grants unchanged")
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
    try:
        skill_projection.reconcile_existing_checkouts(con)
    except skill_projection.ProjectionError as exc:
        sys.exit(skill_projection.partial_failure_message(f"retire {name}", exc))
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
    try:
        skill_projection.reconcile_existing_checkouts(con)
    except skill_projection.ProjectionError as exc:
        sys.exit(skill_projection.partial_failure_message(f"unretire {name}", exc))
    grants = grant_count(
        con, con.execute(
            "SELECT skill_id FROM skills WHERE name=?", (name,)).fetchone()[0])
    rel = _display_retire_file()
    print(f"unretire: {name} — restored with {grants} grant(s) live again.")
    action = "commit" if artifact_policy.tracks_local_artifacts() else "kept local at"
    print(f"→ {action} {rel}.")
    return 0


def cmd_rm(con, name: str) -> int:
    engine_owned = set(seed_skills.seeded_skill_names()) | set(
        seed_skills.tombstoned_skill_names()
    )
    if name in engine_owned:
        sys.exit(f"sc skill: '{name}' is an ENGINE skill — the seed re-inserts it "
                 "on every update/rebuild, so a local rm cannot stick. "
                 f"`./sc skill retire {name}` retires it fork-wide (durable), or "
                 "`./sc skill revoke` removes it from a flavor or Bespoke shell.")
    row = con.execute(
        "SELECT skill_id, is_deleted FROM skills WHERE name=?", (name,)
    ).fetchone()
    if row is None:
        sys.exit(f"sc skill: no local skill '{name}' in the live DB")
    skill_id, already_deleted = row
    n = con.execute("DELETE FROM flavor_skills WHERE skill_id=?", (skill_id,)).rowcount
    n += con.execute("DELETE FROM shell_skills WHERE skill_id=?", (skill_id,)).rowcount
    con.execute("UPDATE skills SET is_deleted=1 WHERE skill_id=?", (skill_id,))
    con.commit()
    _persist_mutation(
        con,
        f"rm {name}",
        lambda: skill_projection.reconcile_existing_checkouts(con),
    )
    suffix = " (already removed; persistence reconciled)" if already_deleted else ""
    print(f"rm: {name} soft-deleted, {n} grant(s) revoked.{suffix}")
    return 0


def main(argv: list[str]) -> int:
    usage = ("usage: ./sc skill list | put --file <SKILL.md> | "
             "grant <name> <shell>... | "
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
        if cmd == "put" and len(args) == 2 and args[0] == "--file":
            return cmd_put(con, Path(args[1]))
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
