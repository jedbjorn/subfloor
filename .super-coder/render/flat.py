#!/usr/bin/env python3
"""Render the DB's content out to FnB-visibility surfaces.

Two render targets live here, both pure (read DB, write files — never the
reverse):

  • Flat `_sc` visibility files — `specs_sc/`, `docs_sc/`, `skills_sc/`,
    `roadmap_sc.md`. Tracked mode writes them at the repo root; local mode writes
    the same logical tree beneath ignored `.sc-state/local/renders/`. The `_sc`
    suffix flags provenance and avoids colliding with a host repo's own `/docs`.
    DB → flat is one-way; the files are never read back.

  • Harness skills — granted skills rendered as Agent Skills `SKILL.md` files
    for the booting shell. `.claude/skills` is the stable cross-harness mirror;
    adapters may request an additional native tree such as Codex's
    `.agents/skills` or OpenCode's `.opencode/skills`. These are generated caches,
    not sources of truth. The boot doc's `## SKILLS` block points at the stable
    mirror so every harness can load the same procedure with a file read, never
    an ad-hoc DB query. Like the boot artifact (CLAUDE.md/AGENTS.md), managed
    skill trees are rebuilt at launch for the selected shell.

Render is incremental: an artifact whose composed content already matches what
is on disk is skipped (no write, no mtime churn), so re-rendering an unchanged
DB is a no-op and the git tree stays clean. The render banner carries no
timestamp for the same reason — content must be a deterministic function of the
DB alone.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import skill_projection  # noqa: E402

# The do-not-edit banner (spec §Content & Render). No timestamp — render must be
# deterministic so unchanged DB → unchanged file → clean diff.
BANNER_KEYS = [
    "rendered_by: super-coder",
    "source: db",
    "edit: changes here are overwritten — author via the shell or localhost GUI",
]


def with_banner(body: str, extra: list[str] | None = None) -> str:
    """Stamp the render banner (+ optional `extra` frontmatter keys) onto a body.

    A document body may already open with its own YAML frontmatter (themed
    markdown does). YAML frontmatter must be the very first thing in the file,
    so we cannot prepend a second block — instead we splice the keys into the
    existing frontmatter. Bodies with no frontmatter get a fresh banner block.
    Either way the warning + metadata travel with the file and the YAML stays
    valid. `extra` carries per-document metadata (feature, roadmap_status,
    frozen) so a reader of the rendered spec sees where its feature sits.
    """
    keys = [*BANNER_KEYS, *(extra or [])]
    body = body.lstrip("\n")
    lines = body.split("\n")
    if lines and lines[0].strip() == "---":
        return "\n".join([lines[0], *keys, *lines[1:]])
    return "\n".join(["---", *keys, "---", "", body])


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


def _document_target(root: Path, rel: str, kind: str) -> Path:
    """Confine DB-authored render paths to their managed visibility folder."""
    path = Path(rel)
    expected = "specs_sc" if kind == "spec" else "docs_sc"
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected:
        raise ValueError(
            f"invalid {kind} render_path {rel!r}; expected a relative path under {expected}/"
        )
    return root / path


def document_rel_path(row) -> str:
    """Resolve one document row to its managed repo-relative render path."""
    if row["render_path"]:
        return row["render_path"]
    base = "specs_sc" if row["kind"] == "spec" else "docs_sc"
    slug = row["title"] or f"{row['kind']}-{row['feature_id']}-{row['seq']}"
    slug = slug.lower().replace(" ", "-").replace("—", "-")
    slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    return f"{base}/{slug}.md"


# ── Flat visibility render ────────────────────────────────────────────────────

def _render_documents(con, written, skipped, root: Path) -> None:
    """specs (kind='spec') → specs_sc/, docs (kind='doc') → docs_sc/.

    The document's own `render_path` is authoritative when set; otherwise we
    derive a stable path from kind + title. Files without a current source row
    are removed so both managed directories remain exact DB projections.
    """
    rows = con.execute(
        "SELECT d.document_id, d.feature_id, d.kind, d.seq, d.title, d.body, d.render_path, "
        "d.frozen, r.roadmap_status, r.title AS feature_title FROM documents d "
        "LEFT JOIN roadmap r ON r.feature_id = d.feature_id "
        "ORDER BY d.feature_id, d.kind, d.seq"
    ).fetchall()
    owners: dict[Path, tuple[int, str]] = {}
    for row in rows:
        if not row["body"]:
            continue
        rel = document_rel_path(row)
        target = _document_target(root, rel, row["kind"])
        previous = owners.get(target)
        if previous is not None:
            raise ValueError(
                f"duplicate document render path {rel!r}: "
                f"document IDs {previous[0]} and {row['document_id']}"
            )
        owners[target] = (row["document_id"], rel)

    expected: set[Path] = set()
    for r in rows:
        if not r["body"]:
            continue
        rel = document_rel_path(r)
        target = _document_target(root, rel, r["kind"])
        expected.add(target)
        # Per-document metadata into the rendered frontmatter — where the
        # feature sits in the plan + whether this spec is frozen.
        extra = [
            f"feature: {r['feature_title'] or ''}",
            f"roadmap_status: {r['roadmap_status'] or ''}",
            f"frozen: {'true' if r['frozen'] else 'false'}",
        ]
        _write_if_changed(target, with_banner(r["body"], extra), written, skipped)

    for dirname in ("specs_sc", "docs_sc"):
        managed_root = root / dirname
        if not managed_root.exists():
            continue
        for path in sorted(managed_root.rglob("*")):
            if path.is_file() and path not in expected:
                path.unlink()
                written.append(path)


# Board order: delivered first, then the committed funnel backward, with
# brainstorm/retired as end caps (see server.py _ORDER for the rationale).
_ROADMAP_ORDER = ["shipped", "in_progress", "next", "near_term", "long_term", "brainstorm", "retired"]
_ROADMAP_LABEL = {
    "brainstorm": "Brainstorm", "in_progress": "In Progress", "next": "Next",
    "near_term": "Near Term", "long_term": "Long Term", "shipped": "Shipped",
    "retired": "Retired",
}


def _render_roadmap(con, written, skipped, root: Path) -> None:
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
    flag_columns = {row[1] for row in con.execute("PRAGMA table_info(flags)")}
    runtime_filter = (
        " AND COALESCE(blocks_runtime,1)=1"
        if "blocks_runtime" in flag_columns
        else ""
    )
    for f in con.execute(
        "SELECT feature_id, display_name, description FROM flags "
        "WHERE resolved=0 AND COALESCE(is_deleted,0)=0 "
        "AND feature_id IS NOT NULL" + runtime_filter + " "
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
    _write_if_changed(root / "roadmap_sc.md", with_banner(body), written, skipped)


def _skill_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _render_skills_catalogue(con, written, skipped, root: Path) -> None:
    """skills_sc/ — the substrate's skill catalogue for browsers: one file per
    skill plus a README index. This is the *catalogue* (every non-deleted
    skill), distinct from `.claude/skills/` which renders one shell's grants.
    Retired skill mirrors are pruned so the directory remains an exact render,
    not an accumulating archive."""
    rows = con.execute(
        "SELECT name, description, category, command, content FROM skills "
        "WHERE is_deleted=0 ORDER BY name"
    ).fetchall()
    skills_root = root / "skills_sc"
    current = {
        "README.md",
        *(f"{_skill_slug(row['name'])}.md" for row in rows),
    }
    if skills_root.exists():
        for path in skills_root.glob("*.md"):
            if path.name not in current:
                path.unlink()
                written.append(path)
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
        _write_if_changed(skills_root / f"{slug}.md",
                          with_banner("\n".join(parts).rstrip()), written, skipped)
    _write_if_changed(skills_root / "README.md",
                      with_banner("\n".join(index).rstrip()), written, skipped)


def render_skills_catalogue(
    con: sqlite3.Connection, root: Path | None = None
) -> dict:
    """Render only the managed ``skills_sc`` catalogue.

    Update reconciliation uses this narrow surface so an unrelated document or
    roadmap render error cannot prevent native skill projections from converging.
    """
    root = root or artifact_policy.render_root()
    written: list[Path] = []
    skipped: list[Path] = []
    _render_skills_catalogue(con, written, skipped, root)
    return {"written": written, "skipped": skipped}


def render_visibility(con: sqlite3.Connection, root: "Path | None" = None) -> dict:
    """Render flat `_sc` visibility files under the active artifact root.

    Returns a written/skipped
    summary. Incremental: unchanged artifacts are not rewritten.

    `root` overrides the write base (defaults to the active artifact root). The
    hermetic render-check passes a temp dir so it can render the committed
    SOURCE and diff it against the committed mirror without touching the tree."""
    root = root or artifact_policy.render_root()
    written: list[Path] = []
    skipped: list[Path] = []
    _render_documents(con, written, skipped, root)
    _render_roadmap(con, written, skipped, root)
    _render_skills_catalogue(con, written, skipped, root)
    return {"written": written, "skipped": skipped}


# ── Harness skill render (per booting shell; gitignored cache) ────────────────

def render_skill_md(con: sqlite3.Connection, shell_id: int,
                    work_dir: "Path | None" = None,
                    skills_dir: "Path | None" = None) -> dict:
    """Render the booting shell's granted skills to
    `<skills_dir>/<name>/SKILL.md` (Agent Skills format: name + description
    frontmatter, content body). The default is `.claude/skills`; adapters may
    request an additional native directory such as `.agents/skills` or
    `.opencode/skills`.

    Harness-consumed and gitignored, like the boot artifact — rebuilt every
    launch for whichever shell boots. Stale skill folders (a grant since
    revoked, or another shell's skills) are pruned so the dir reflects exactly
    this shell's current grants.

    work_dir overrides the write root (used for dev-shell worktrees).
    skills_dir is relative to that root."""
    return skill_projection.reconcile_root(
        con,
        shell_id,
        work_dir or REPO_ROOT,
        skills_dir or Path(".claude/skills"),
        create=True,
    )
