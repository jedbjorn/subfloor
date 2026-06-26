#!/usr/bin/env python3
"""Generate the skills seed migration from `assets/skills/`.

Skills under `.super-coder/assets/skills/` are SYSTEM content: the catalogue
propagates to every fork. Project/fork-local skills are different; they live in
the fork DB and are serialized by snapshot.py. The engine seed must therefore
add/update authored engine skills without retiring local skills.

Source of truth for a skill = `assets/skills/<name>/SKILL.md`:

    ---
    name: db_map
    description: one-line summary (used in the boot SKILLS block + catalogue)
    category: substrate          # optional
    command: ./sc snapshot       # optional
    common: true                 # optional (default true)
    ---
    <markdown body → skills.content>

Output: `migrations/0001_seed_skills.sql` — idempotent UPSERTs for the authored
engine catalogue. It deliberately does not delete/retire names absent from
assets/skills; those may be project-local skills. Re-running regenerates it
byte-for-byte. The migration is keyed by filename in the ledger, so it applies
once per fork; `./sc update` also syncs the generated seed against the live DB.

Usage:
    python3 .super-coder/scripts/seed_skills.py
    ./sc seed-skills
"""
from __future__ import annotations

import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKILLS_DIR = ENGINE / "assets" / "skills"
OUT = ENGINE / "migrations" / "0001_seed_skills.sql"


def sql_str(v) -> str:
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def parse_skill(path: Path) -> dict:
    """Parse a SKILL.md: YAML-ish frontmatter (flat key: value) + body."""
    text = path.read_text()
    if not text.startswith("---"):
        sys.exit(f"seed_skills: {path} has no frontmatter")
    _, fm, body = text.split("---", 2)
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    if "name" not in meta:
        sys.exit(f"seed_skills: {path} frontmatter missing `name`")
    common = meta.get("common", "true").lower() not in ("false", "0", "no")
    return {
        "name": meta["name"],
        "description": meta.get("description"),
        "category": meta.get("category"),
        "command": meta.get("command"),
        "common": 1 if common else 0,
        "content": body.strip(),
    }


# Fields the seed UPSERT owns (seed_skills `main`'s ON CONFLICT DO UPDATE SET).
# A live row that differs from its asset on any of these is stale.
SEED_FIELDS = ("description", "category", "command", "common", "content")


def engine_skill_specs() -> list[dict]:
    """Parse every engine skill under assets/skills/ — the seed's source of
    truth and, by name, the line between engine and project-local skills (the
    same set snapshot.dump_local_skills uses to decide what is fork-local)."""
    if not SKILLS_DIR.exists():
        return []
    return [parse_skill(d / "SKILL.md")
            for d in sorted(SKILLS_DIR.iterdir())
            if (d / "SKILL.md").exists()]


def stale_engine_skills(con) -> list[str]:
    """Engine skills whose live-DB row lags assets/skills/. Names only.

    This is the drift the in-place `0001` regen can strand: a DB built before
    `./sc seed-skills` ran carries the OLD body, and the migrate ledger marks
    `0001` applied so `./sc migrate` never re-seeds it (currency is a per-update
    sync — see update.sync_skills — or a `./sc rebuild`). Rendering the flat
    mirror from such a DB writes STALE, possibly content-deleting, files.

    Scoped to engine skills BY NAME: a project-local skill (name absent from
    assets/skills/) has no upstream to lag, is never inspected here, and so can
    never trip this guard — admin-authored repo-local skills are safe.
    """
    stale: list[str] = []
    for want in engine_skill_specs():
        row = con.execute(
            "SELECT description, category, command, common, content "
            "FROM skills WHERE name=?", (want["name"],),
        ).fetchone()
        if row is None:                       # engine skill missing from the DB
            stale.append(want["name"])
            continue
        if any(row[i] != want[f] for i, f in enumerate(SEED_FIELDS)):
            stale.append(want["name"])
    return stale


def sync_engine_skills(con) -> list[str]:
    """Self-heal: bring the live DB's engine skills current with assets/skills/.

    Idempotent UPSERT BY NAME of exactly the engine skills that lag (per
    stale_engine_skills); returns the names healed, empty when already fresh.
    This is the cure the tripwire used to only point at — a DB stranded by an
    in-place `0001` regen repairs itself instead of needing a manual rebuild.

    Project-local skills (name absent from assets/skills/) are never touched:
    they aren't in the stale set, have no upstream to heal from, and stay
    durable via content.sql. The caller owns the transaction boundary intent;
    we commit our own writes."""
    stale = stale_engine_skills(con)
    if not stale:
        return []
    specs = {s["name"]: s for s in engine_skill_specs()}
    for name in stale:                       # stale ⊆ asset names, so always hits
        s = specs[name]
        con.execute(
            "INSERT INTO skills (name, description, category, command, common, "
            "content, is_deleted) VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(name) DO UPDATE SET "
            "description=excluded.description, category=excluded.category, "
            "command=excluded.command, common=excluded.common, "
            "content=excluded.content, is_deleted=0",
            (s["name"], s["description"], s["category"], s["command"],
             s["common"], s["content"]),
        )
    con.commit()
    return stale


def main() -> int:
    if not SKILLS_DIR.exists():
        sys.exit(f"seed_skills: no {SKILLS_DIR}")
    skills = [parse_skill(d / "SKILL.md")
              for d in sorted(SKILLS_DIR.iterdir())
              if (d / "SKILL.md").exists()]
    if not skills:
        sys.exit("seed_skills: no skills found under assets/skills/")

    lines = [
        "-- super-coder skills seed — GENERATED by scripts/seed_skills.py.",
        "-- System content: the engine catalogue propagates to every fork. Idempotent",
        "-- and ID-STABLE: UPSERTs each authored engine skill by name, but never",
        "-- retires names absent from assets/skills because those may be project-local",
        "-- skills serialized by .sc-state/content.sql. Do not hand-edit; author",
        "-- assets/skills/<name>/SKILL.md then `./sc seed-skills`.",
        "",
        "BEGIN;",
        "",
    ]
    for s in skills:
        lines.append(
            "INSERT INTO skills (name, description, category, command, common, content, is_deleted) "
            f"VALUES (\n  {sql_str(s['name'])},\n  {sql_str(s['description'])},\n  "
            f"{sql_str(s['category'])},\n  {sql_str(s['command'])},\n  {s['common']},\n  "
            f"{sql_str(s['content'])},\n  0\n)\n"
            "ON CONFLICT(name) DO UPDATE SET\n"
            "  description=excluded.description, category=excluded.category,\n"
            "  command=excluded.command, common=excluded.common,\n"
            "  content=excluded.content, is_deleted=0;"
        )
        lines.append("")
    lines.append("COMMIT;")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"seed_skills: wrote {OUT.relative_to(ENGINE.parent)} "
          f"({len(skills)} skill(s): {', '.join(s['name'] for s in skills)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
