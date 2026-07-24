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

Two effects, one command:

  1. Regenerate `migrations/0001_seed_skills.sql` — idempotent UPSERTs for the
     authored engine catalogue. SOURCE REPO (and ejected forks) ONLY: in a
     tracking fork the seed is materialized from upstream and manifest-guarded,
     so a local regen would be an engine edit that blocks the next `./sc
     update`. The seed deliberately never deletes/retires names absent from
     assets/skills; those may be project-local skills.
  2. UPSERT the parsed asset skills into the LIVE DB (all repos) — so a grant
     right after seeding resolves (#253). The migration alone can't do that:
     the ledger marks 0001 applied once and never re-runs it.

Usage:
    python3 .super-coder/scripts/seed_skills.py
    ./sc seed-skills
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKILLS_DIR = ENGINE / "assets" / "skills"
OUT = ENGINE / "migrations" / "0001_seed_skills.sql"
DB_PATH = ENGINE / "shell_db.db"
RETIRED_FILE = ENGINE.parent / ".sc-state" / "skills_retired.json"


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
    """Parse every skill under assets/skills/ — the seed's source of truth.
    NOTE: in a fork this set can include fork-LOCAL skills (authored assets);
    the engine/local line is seeded_skill_names(), not this set."""
    if not SKILLS_DIR.exists():
        return []
    return [parse_skill(d / "SKILL.md")
            for d in sorted(SKILLS_DIR.iterdir())
            if (d / "SKILL.md").exists()]


def seeded_skill_names() -> list[str]:
    """Names the engine seed (migrations/0001) owns — THE engine/local boundary.

    Asset-file presence is NOT the boundary (#253): a fork-authored skill keeps
    its SKILL.md under assets/skills/ (gitignored engine territory) as its
    authoring source, so classifying by asset presence miscounts it as engine —
    snapshot then omits it from content.sql and the skill is lost on the next
    update. The seed is authoritative instead: in a tracking fork, 0001 is
    materialized from upstream and never regenerated locally (see main), so it
    lists exactly the upstream catalogue; in the source repo it is regenerated
    from assets and the two sets coincide.

    Parsed by executing the seed against a scratch in-memory `skills` table —
    the seed is generated SQL, so running it is the one parse that can't drift
    from its format. Falls back to asset names when no seed exists yet.
    """
    if not OUT.exists():
        return [s["name"] for s in engine_skill_specs()]
    con = sqlite3.connect(":memory:")
    try:
        con.execute(
            "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL UNIQUE, description TEXT, category TEXT, "
            "content TEXT, command TEXT, common INTEGER NOT NULL DEFAULT 1, "
            "is_deleted INTEGER NOT NULL DEFAULT 0)")
        con.executescript(OUT.read_text())
        return [r[0] for r in con.execute("SELECT name FROM skills ORDER BY name")]
    finally:
        con.close()


def retired_skill_names() -> list[str]:
    """The fork's retire list — engine skills this fork has taken out of
    service (`.sc-state/skills_retired.json`, tracked, fork-owned; written by
    `./sc skill retire`). The seed/sync resurrects engine rows on every update,
    so retirement must live OUTSIDE the DB and be re-applied after each sync —
    same shape as the flavor overlays (#247). Loud on a malformed file: a
    silently-ignored list means superseded skills quietly come back."""
    if not RETIRED_FILE.exists():
        return []
    try:
        names = json.loads(RETIRED_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(
            f"fork retire list {RETIRED_FILE} is not valid JSON: {e}") from e
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(
            f"fork retire list {RETIRED_FILE} must be a JSON array of skill names")
    return names


def apply_retired(con) -> list[str]:
    """Converge the live DB's engine rows to the fork retire list: listed names
    get is_deleted=1, unlisted engine names get is_deleted=0. is_deleted is the
    one switch every surface already respects — the per-shell SKILL.md render,
    the boot-doc SKILLS block, the catalogue render, common regrants, and
    create_shell all filter on it — so one flip retires a skill everywhere.
    Grant rows are left in place (inert while retired), so unretiring restores
    who-had-what. Returns the names flipped; commits its own writes.

    Only ENGINE names converge: local skills retire via `./sc skill rm`, and a
    listed name that isn't engine (typo, or upstream removed the skill) is
    warned about, never acted on."""
    retired = set(retired_skill_names())
    engine = set(seeded_skill_names())
    for name in sorted(retired - engine):
        print(f"  ⚠ retire list: '{name}' is not an engine skill — ignored "
              "(local skills retire via `./sc skill rm`)")
    flipped: list[str] = []
    for name in sorted(engine):
        want = 1 if name in retired else 0
        row = con.execute(
            "SELECT is_deleted FROM skills WHERE name=?",
            (name,),
        ).fetchone()
        if row is None or row[0] == want:
            continue
        cur = con.execute(
            "UPDATE skills SET is_deleted=? WHERE name=? AND is_deleted<>?",
            (want, name, want))
        if cur.rowcount:
            flipped.append(name)
    if flipped:
        con.commit()
    return flipped


def _engine_specs() -> list[dict]:
    """Asset specs that are genuinely ENGINE skills (name in the seed). A
    fork-local skill authored under assets/skills/ is excluded: it has no
    upstream to lag, and healing it "from assets" would clobber a DB row that
    is canonical once seeded."""
    seeded = set(seeded_skill_names())
    return [s for s in engine_skill_specs() if s["name"] in seeded]


def stale_engine_skills(con, specs: list[dict] | None = None) -> list[str]:
    """Engine skills whose live-DB row lags assets/skills/. Names only.

    This is the drift the in-place `0001` regen can strand: a DB built before
    `./sc seed-skills` ran carries the OLD body, and the migrate ledger marks
    `0001` applied so `./sc migrate` never re-seeds it (currency is a per-update
    sync — see update.sync_skills — or a `./sc rebuild`). Rendering the flat
    mirror from such a DB writes STALE, possibly content-deleting, files.

    Scoped to engine skills BY NAME (seed names ∩ assets — see _engine_specs):
    a project-local skill has no upstream to lag, is never inspected here, and
    so can never trip this guard — admin-authored repo-local skills are safe
    even while their asset file lingers under assets/skills/.
    """
    stale: list[str] = []
    for want in (_engine_specs() if specs is None else specs):
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


def sync_engine_skills(con, specs: list[dict] | None = None) -> list[str]:
    """Self-heal: bring the live DB's engine skills current with assets/skills/.

    Idempotent UPSERT BY NAME of exactly the engine skills that lag (per
    stale_engine_skills); returns the names healed, empty when already fresh.
    This is the cure the tripwire used to only point at — a DB stranded by an
    in-place `0001` regen repairs itself instead of needing a manual rebuild.

    Project-local skills (name absent from the seed) are never touched: they
    aren't in the stale set, have no upstream to heal from, and stay durable
    via content.sql. `main` passes specs=engine_skill_specs() (ALL assets) so
    an explicit `./sc seed-skills` also upserts freshly-authored local skills;
    the implicit boot/render heal stays engine-only. The caller owns the
    transaction boundary intent; we commit our own writes."""
    if specs is None:
        specs = _engine_specs()
    stale = stale_engine_skills(con, specs)
    by_name = {s["name"]: s for s in specs}
    for name in stale:                       # stale ⊆ spec names, so always hits
        s = by_name[name]
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
    if stale:
        con.commit()
    # The heal above (and the full-seed sync on update) resurrects rows with
    # is_deleted=0 — re-assert the fork retire list so a retired skill stays
    # retired across heals, syncs, and rebuilds.
    apply_retired(con)
    return stale


def _fork_mode() -> bool:
    """True when this repo is an installed fork TRACKING upstream. There the
    engine — including migrations/0001 — is materialized, upstream-owned, and
    hash-manifest-guarded, so regenerating the seed locally would be an engine
    edit that blocks the next `./sc update` (and would leak fork-local skills
    into a file the next materialize rewrites anyway). The source repo and an
    ejected fork own their engine and do regenerate."""
    import update  # lazy — update.py imports this module; safe at call time
    return not update.is_source_repo() and not update.EJECTED_MARKER.exists()


def _upsert_live(skills: list[dict]) -> None:
    """The documented half of seeding: put the parsed asset skills IN THE LIVE
    DB, so a grant right after (`./sc skill grant`) resolves instead of being a
    silent no-op (#253). Passing ALL asset specs (not just seed names) is what
    lets a freshly-authored fork-local skill land."""
    if not DB_PATH.exists() or not DB_PATH.stat().st_size:
        print("seed_skills: no live DB yet — skills land on first rebuild/launch.")
        return
    import db_driver  # lazy — sibling module; keeps import surface unchanged
    con = db_driver.connect(DB_PATH)
    try:
        synced = sync_engine_skills(con, specs=skills)
    finally:
        con.close()
    if synced:
        print(f"seed_skills: live DB — upserted {len(synced)} skill(s): "
              + ", ".join(synced))
    else:
        print("seed_skills: live DB already current.")


def main() -> int:
    if not SKILLS_DIR.exists():
        sys.exit(f"seed_skills: no {SKILLS_DIR}")
    skills = [parse_skill(d / "SKILL.md")
              for d in sorted(SKILLS_DIR.iterdir())
              if (d / "SKILL.md").exists()]
    if not skills:
        sys.exit("seed_skills: no skills found under assets/skills/")

    if _fork_mode():
        print("seed_skills: fork — the engine seed (0001) is upstream-owned; "
              "skipping regen. Local skills persist via `./sc snapshot` "
              "(.sc-state/content.sql), not the seed.")
        _upsert_live(skills)
        return 0

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
    _upsert_live(skills)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
