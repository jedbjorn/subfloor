#!/usr/bin/env python3
"""Render the DB's content out to FnB-visibility surfaces.

Two render targets live here, both pure (read DB, write files — never the
reverse):

  • Flat `_sc` visibility files — `specs_sc/`, `docs_sc/`, `skills_sc/`,
    `roadmap_sc.md` at the repo root. These are TRACKED (committed) and exist
    for the outsider FnB browsing the repo without localhost. The `_sc` suffix
    flags provenance and avoids colliding with a host repo's own `/docs`.
    DB → flat is one-way; the files are never read back.

  • Harness skills — `.claude/skills/<name>/SKILL.md` for the booting shell's
    granted skills. Consumed natively by Claude Code / OpenCode / Crush.
    Like the boot artifact (CLAUDE.md/AGENTS.md) this is GITIGNORED and rebuilt
    at launch — a per-shell cache, not tracked content.

Render is incremental: an artifact whose composed content already matches what
is on disk is skipped (no write, no mtime churn), so re-rendering an unchanged
DB is a no-op and the git tree stays clean. The render banner carries no
timestamp for the same reason — content must be a deterministic function of the
DB alone.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent

# The do-not-edit banner (spec §Content & Render). No timestamp — render must be
# deterministic so unchanged DB → unchanged file → clean diff.
BANNER_KEYS = [
    "rendered_by: super-coder",
    "source: db",
    "edit: changes here are overwritten — author via the shell or localhost GUI",
]


def with_banner(body: str) -> str:
    """Stamp the render banner onto a markdown body.

    A document body may already open with its own YAML frontmatter (themed
    markdown does). YAML frontmatter must be the very first thing in the file,
    so we cannot prepend a second block — instead we splice the banner keys into
    the existing frontmatter. Bodies with no frontmatter get a fresh banner
    block. Either way the warning travels with the file and the YAML stays valid.
    """
    body = body.lstrip("\n")
    lines = body.split("\n")
    if lines and lines[0].strip() == "---":
        return "\n".join([lines[0], *BANNER_KEYS, *lines[1:]])
    return "\n".join(["---", *BANNER_KEYS, "---", "", body])


# ── Incremental writer ──────────────────────────────────────────────────────

def _write_if_changed(path: Path, content: str, written: list, skipped: list) -> None:
    if not content.endswith("\n"):
        content += "\n"
    if path.exists() and path.read_text() == content:
        skipped.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    written.append(path)


# ── Flat visibility render ────────────────────────────────────────────────────

def _render_documents(con, written, skipped) -> None:
    """specs (kind='spec') → specs_sc/, docs (kind='doc') → docs_sc/.

    The document's own `render_path` is authoritative when set; otherwise we
    derive a stable path from kind + title.
    """
    rows = con.execute(
        "SELECT feature_id, kind, seq, title, body, render_path FROM documents "
        "ORDER BY feature_id, kind, seq"
    ).fetchall()
    for r in rows:
        if not r["body"]:
            continue
        rel = r["render_path"]
        if not rel:
            base = "specs_sc" if r["kind"] == "spec" else "docs_sc"
            slug = (r["title"] or f"{r['kind']}-{r['feature_id']}-{r['seq']}")
            slug = slug.lower().replace(" ", "-").replace("—", "-")
            slug = "".join(c for c in slug if c.isalnum() or c in "-_")
            rel = f"{base}/{slug}.md"
        _write_if_changed(REPO_ROOT / rel, with_banner(r["body"]), written, skipped)


_ROADMAP_ORDER = ["next", "near_term", "long_term", "brainstorm", "shipped"]
_ROADMAP_LABEL = {
    "next": "Next", "near_term": "Near term", "long_term": "Long term",
    "brainstorm": "Brainstorm", "shipped": "Shipped",
}


def _render_roadmap(con, written, skipped) -> None:
    """roadmap_sc.md — the static board for outsiders. Status is a planning
    horizon; a feature's open flags are listed as its blockers (joined on
    feature_id)."""
    rows = con.execute(
        "SELECT r.feature_id, r.title, r.roadmap_status, r.summary, "
        "s.shortname AS owner FROM roadmap r "
        "LEFT JOIN shells s ON s.shell_id = r.owning_shell "
        "ORDER BY r.sort_order, r.feature_id"
    ).fetchall()
    flags_by_feature: dict[int, list] = {}
    for f in con.execute(
        "SELECT feature_id, display_name, description FROM flags "
        "WHERE resolved=0 AND COALESCE(is_deleted,0)=0 AND feature_id IS NOT NULL "
        "ORDER BY flag_id"
    ).fetchall():
        flags_by_feature.setdefault(f["feature_id"], []).append(f)

    parts = ["# Roadmap", "",
             "> Rendered from the DB. Status is a planning horizon; a feature's "
             "open flags are its blockers.", ""]
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["roadmap_status"], []).append(r)
    for status in _ROADMAP_ORDER:
        if status not in buckets:
            continue
        parts.append(f"## {_ROADMAP_LABEL[status]}")
        parts.append("")
        for r in buckets[status]:
            owner = f" · owner: `{r['owner']}`" if r["owner"] else ""
            parts.append(f"### {r['title']}{owner}")
            if r["summary"]:
                parts.append(r["summary"])
            blockers = flags_by_feature.get(r["feature_id"], [])
            if blockers:
                parts.append("")
                parts.append("**Blockers:**")
                for b in blockers:
                    name = f"`{b['display_name']}` " if b["display_name"] else ""
                    parts.append(f"- {name}{b['description'] or ''}")
            else:
                parts.append("")
                parts.append("_No open flags._")
            parts.append("")
    body = "\n".join(parts).rstrip()
    _write_if_changed(REPO_ROOT / "roadmap_sc.md", with_banner(body), written, skipped)


def _skill_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _render_skills_catalogue(con, written, skipped) -> None:
    """skills_sc/ — the substrate's skill catalogue for browsers: one file per
    skill plus a README index. This is the *catalogue* (every non-deleted
    skill), distinct from `.claude/skills/` which renders one shell's grants."""
    rows = con.execute(
        "SELECT name, description, category, command, content FROM skills "
        "WHERE is_deleted=0 ORDER BY name"
    ).fetchall()
    index = ["# Skills", "",
             "> The substrate's skill catalogue, rendered from the DB. "
             "Per-shell grants live in `.claude/skills/` (rebuilt at boot).", ""]
    for r in rows:
        slug = _skill_slug(r["name"])
        index.append(f"- [`{r['name']}`](skills_sc/{slug}.md) — "
                     f"{(r['description'] or '').strip().splitlines()[0] if r['description'] else ''}")
        meta = []
        if r["category"]:
            meta.append(f"**Category:** {r['category']}")
        if r["command"]:
            meta.append(f"**Command:** `{r['command']}`")
        parts = [f"# {r['name']}", ""]
        if r["description"]:
            parts += [r["description"].strip(), ""]
        if meta:
            parts += ["  ·  ".join(meta), ""]
        if r["content"]:
            parts += ["---", "", r["content"].strip()]
        _write_if_changed(REPO_ROOT / "skills_sc" / f"{slug}.md",
                          with_banner("\n".join(parts).rstrip()), written, skipped)
    _write_if_changed(REPO_ROOT / "skills_sc" / "README.md",
                      with_banner("\n".join(index).rstrip()), written, skipped)


def render_visibility(con: sqlite3.Connection) -> dict:
    """Render the tracked flat `_sc` visibility files. Returns a written/skipped
    summary. Incremental: unchanged artifacts are not rewritten."""
    written: list[Path] = []
    skipped: list[Path] = []
    _render_documents(con, written, skipped)
    _render_roadmap(con, written, skipped)
    _render_skills_catalogue(con, written, skipped)
    return {"written": written, "skipped": skipped}


# ── Harness skill render (per booting shell; gitignored cache) ────────────────

def render_skill_md(con: sqlite3.Connection, shell_id: int) -> dict:
    """Render the booting shell's granted skills to `.claude/skills/<name>/SKILL.md`
    (Agent Skills format: name + description frontmatter, content body).

    Harness-consumed and gitignored, like the boot artifact — rebuilt every
    launch for whichever shell boots. Stale skill folders (a grant since
    revoked, or another shell's skills) are pruned so the dir reflects exactly
    this shell's current grants."""
    rows = con.execute(
        "SELECT s.name, s.description, s.content FROM skills s "
        "JOIN shell_skills ss ON ss.skill_id = s.skill_id "
        "WHERE ss.shell_id=? AND s.is_deleted=0 ORDER BY s.name",
        (shell_id,),
    ).fetchall()
    skills_root = REPO_ROOT / ".claude" / "skills"
    written: list[Path] = []
    skipped: list[Path] = []
    current = {_skill_slug(r["name"]) for r in rows}

    # Prune folders that no longer correspond to a current grant.
    if skills_root.exists():
        for child in skills_root.iterdir():
            if child.is_dir() and child.name not in current:
                for f in child.rglob("*"):
                    if f.is_file():
                        f.unlink()
                child.rmdir()

    for r in rows:
        desc = (r["description"] or "").strip().replace("\n", " ")
        body = "\n".join([
            "---", f"name: {r['name']}", f"description: {desc}", "---", "",
            (r["content"] or "").strip(), "",
        ])
        path = skills_root / _skill_slug(r["name"]) / "SKILL.md"
        if path.exists() and path.read_text() == body:
            skipped.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        written.append(path)
    return {"written": written, "skipped": skipped}
