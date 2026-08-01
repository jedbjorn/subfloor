"""Converge DB skill grants into bounded, engine-managed filesystem mirrors."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path

import artifact_policy
import seed_skills

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
ADAPTERS = ENGINE / "adapters"
DEFAULT_SKILL_DIR = Path(".claude/skills")
LEGACY_SKILLS_DIR = Path("skills_sc")
RENDER_BANNER = "rendered_by: super-coder"


class ProjectionError(RuntimeError):
    """A managed projection cannot be reconciled without leaving its checkout."""


def _validated_relative(raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectionError(
            f"adapter skill_dirs entry must be a non-empty string: {raw!r}"
        )
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ProjectionError(
            f"adapter skill_dirs entry must stay inside the checkout: {raw}"
        )
    return path


def managed_skill_dirs(adapters: Path = ADAPTERS) -> tuple[Path, ...]:
    """Return the validated union of every engine-managed harness skill root."""
    roots = {DEFAULT_SKILL_DIR}
    for config_path in sorted(adapters.glob("*/adapter.json")):
        try:
            config = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectionError(f"cannot read adapter {config_path}: {exc}") from exc
        for raw in config.get("skill_dirs") or ():
            roots.add(_validated_relative(raw))
    return tuple(sorted(roots, key=lambda path: (path != DEFAULT_SKILL_DIR, str(path))))


def _bounded_root(checkout: Path, relative: Path) -> Path:
    relative = _validated_relative(str(relative))
    checkout_root = checkout.resolve()
    root = checkout / relative
    if root.is_symlink():
        raise ProjectionError(f"managed skill root is a symlink: {root}")
    resolved = root.resolve(strict=False)
    if not resolved.is_relative_to(checkout_root):
        raise ProjectionError(f"managed skill root escapes checkout {checkout}: {root}")
    if root.exists() and not root.is_dir():
        raise ProjectionError(f"managed skill root is not a directory: {root}")
    return root


def _skill_rows(con, shell_id: int | None) -> list:
    if shell_id is None:
        return []
    return con.execute(
        "SELECT s.name, s.description, s.content FROM skills s "
        "JOIN resolved_shell_skills ss ON ss.skill_id=s.skill_id "
        "WHERE ss.shell_id=? AND s.is_deleted=0 ORDER BY s.name",
        (shell_id,),
    ).fetchall()


def _skill_body(row) -> str:
    description = (row["description"] or "").strip().replace("\n", " ")
    return "\n".join(
        [
            "---",
            f"name: {row['name']}",
            f"description: {description}",
            "---",
            "",
            (row["content"] or "").strip(),
            "",
        ]
    )


def _banner_owned_skill_directory(path: Path) -> bool:
    """Return whether a real directory carries a managed SKILL.md banner."""
    if path.is_symlink() or not path.is_dir():
        return False
    skill_file = path / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        return False
    try:
        return RENDER_BANNER in skill_file.read_text()
    except (OSError, UnicodeError):
        return False


def _upstream_skill_slugs() -> set[str]:
    names = (*seed_skills.seeded_skill_names(), *seed_skills.tombstoned_skill_names())
    return {name.strip().lower().replace(" ", "-") for name in names}


def _remove_managed_tree(path: Path, managed_names: set[str]) -> bool:
    """Remove a proven engine-managed directory without following symlinks."""
    if path.name not in managed_names and not _banner_owned_skill_directory(path):
        return False
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)
    return True


def reconcile_root(
    con, shell_id: int | None, checkout: Path, relative: Path, *, create: bool
) -> dict:
    """Render one exact managed root without following filesystem symlinks."""
    root = _bounded_root(checkout, relative)
    if not root.exists() and not create:
        return {"written": [], "skipped": [], "deleted": []}

    rows = _skill_rows(con, shell_id)
    if shell_id is None:
        current = {
            row[0].strip().lower().replace(" ", "-")
            for row in con.execute(
                "SELECT name FROM skills WHERE is_deleted=0 ORDER BY name"
            )
        }
    else:
        current = {row["name"].strip().lower().replace(" ", "-") for row in rows}
    written: list[Path] = []
    skipped: list[Path] = []
    deleted: list[Path] = []
    managed_names = _upstream_skill_slugs()

    if root.exists():
        for child in root.iterdir():
            if child.name in current:
                if child.is_symlink():
                    raise ProjectionError(
                        f"granted skill directory is a symlink: {child}"
                    )
                continue
            if (child.is_dir() or child.is_symlink()) and _remove_managed_tree(
                child, managed_names
            ):
                deleted.append(child)

    for row in rows:
        slug = row["name"].strip().lower().replace(" ", "-")
        directory = root / slug
        if directory.is_symlink():
            raise ProjectionError(f"granted skill directory is a symlink: {directory}")
        path = directory / "SKILL.md"
        body = _skill_body(row)
        if path.exists() and path.read_text() == body:
            skipped.append(path)
            continue
        artifact_policy.atomic_write_text(path, body)
        written.append(path)
    return {"written": written, "skipped": skipped, "deleted": deleted}


def reconcile_shell(
    con, shell_id: int | None, checkout: Path, *, ensure_dirs: Iterable[Path | str] = ()
) -> dict:
    """Converge all existing managed roots, creating only explicitly used roots."""
    managed = managed_skill_dirs()
    ensured = {_validated_relative(str(path)) for path in ensure_dirs}
    unknown = ensured - set(managed)
    if unknown:
        raise ProjectionError(
            "requested skill root is not declared by an adapter: "
            + ", ".join(str(path) for path in sorted(unknown, key=str))
        )

    written: list[Path] = []
    skipped: list[Path] = []
    deleted: list[Path] = []
    visited: list[str] = []
    for relative in managed:
        candidate = checkout / relative
        if (
            relative not in ensured
            and not candidate.exists()
            and not candidate.is_symlink()
        ):
            continue
        summary = reconcile_root(
            con, shell_id, checkout, relative, create=relative in ensured
        )
        written.extend(summary["written"])
        skipped.extend(summary["skipped"])
        deleted.extend(summary["deleted"])
        visited.append(str(relative))
    return {
        "written": written,
        "skipped": skipped,
        "deleted": deleted,
        "dirs": visited,
    }


def shell_checkout(
    shortname: str | None, flavor: str | None, repo_root: Path = REPO_ROOT
) -> Path:
    if shortname and flavor != "admin":
        return repo_root / ".sc-worktrees" / shortname.lower()
    return repo_root


def reconcile_shell_ids(
    con, shell_ids: Iterable[int], *, repo_root: Path = REPO_ROOT
) -> dict:
    ids = sorted({int(shell_id) for shell_id in shell_ids})
    if not ids:
        return {"written": [], "skipped": [], "deleted": [], "checkouts": []}
    marks = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT shell_id, shortname, flavor FROM shells WHERE shell_id IN ({marks}) "
        "AND COALESCE(is_deleted,0)=0 ORDER BY shell_id",
        tuple(ids),
    ).fetchall()
    return _reconcile_shell_rows(con, rows, repo_root=repo_root)


def reconcile_flavors(
    con, flavors: Iterable[str], *, repo_root: Path = REPO_ROOT
) -> dict:
    names = sorted(set(flavors))
    if not names:
        return {"written": [], "skipped": [], "deleted": [], "checkouts": []}
    marks = ",".join("?" for _ in names)
    rows = con.execute(
        f"SELECT shell_id, shortname, flavor FROM shells WHERE flavor IN ({marks}) "
        "AND COALESCE(is_deleted,0)=0 ORDER BY shell_id",
        tuple(names),
    ).fetchall()
    return _reconcile_shell_rows(con, rows, repo_root=repo_root)


def reconcile_assignment_targets(
    con, shell_ids: Iterable[int], *, repo_root: Path = REPO_ROOT
) -> dict:
    """Reconcile a Bespoke target or every live sibling in a target's flavor."""
    ids = sorted({int(shell_id) for shell_id in shell_ids})
    if not ids:
        return {"written": [], "skipped": [], "deleted": [], "checkouts": []}
    marks = ",".join("?" for _ in ids)
    targets = con.execute(
        f"SELECT shell_id, flavor FROM shells WHERE shell_id IN ({marks}) "
        "AND COALESCE(is_deleted,0)=0",
        tuple(ids),
    ).fetchall()
    bespoke = [row["shell_id"] for row in targets if row["flavor"] is None]
    flavors = [row["flavor"] for row in targets if row["flavor"] is not None]
    rows = []
    if bespoke:
        bespoke_marks = ",".join("?" for _ in bespoke)
        rows.extend(
            con.execute(
                f"SELECT shell_id, shortname, flavor FROM shells "
                f"WHERE shell_id IN ({bespoke_marks}) AND COALESCE(is_deleted,0)=0",
                tuple(bespoke),
            ).fetchall()
        )
    if flavors:
        flavor_marks = ",".join("?" for _ in set(flavors))
        rows.extend(
            con.execute(
                f"SELECT shell_id, shortname, flavor FROM shells "
                f"WHERE flavor IN ({flavor_marks}) AND COALESCE(is_deleted,0)=0",
                tuple(sorted(set(flavors))),
            ).fetchall()
        )
    return _reconcile_shell_rows(con, rows, repo_root=repo_root)


def _reconcile_shell_rows(con, rows, *, repo_root: Path) -> dict:
    written: list[Path] = []
    skipped: list[Path] = []
    deleted: list[Path] = []
    checkouts: list[Path] = []
    seen: set[Path] = set()
    for row in rows:
        checkout = shell_checkout(row["shortname"], row["flavor"], repo_root)
        if checkout in seen or not checkout.is_dir():
            continue
        seen.add(checkout)
        summary = reconcile_shell(con, row["shell_id"], checkout)
        written.extend(summary["written"])
        skipped.extend(summary["skipped"])
        deleted.extend(summary["deleted"])
        checkouts.append(checkout)
    return {
        "written": written,
        "skipped": skipped,
        "deleted": deleted,
        "checkouts": checkouts,
    }


def cleanup_legacy_skills_sc(checkout: Path) -> list[Path]:
    """Remove only banner-owned legacy files; preserve mixed foreign content."""
    root = checkout / LEGACY_SKILLS_DIR
    if root.is_symlink():
        raise ProjectionError(f"legacy skills_sc root is a symlink: {root}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ProjectionError(f"legacy skills_sc root is not a directory: {root}")
    deleted: list[Path] = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_file():
            continue
        try:
            managed = RENDER_BANNER in child.read_text()
        except (OSError, UnicodeError):
            managed = False
        if managed:
            child.unlink()
            deleted.append(child)
    if not any(root.iterdir()):
        root.rmdir()
        deleted.append(root)
    return deleted


def reconcile_existing_checkouts(con, *, repo_root: Path = REPO_ROOT) -> dict:
    """Sweep root plus existing direct shell worktrees without creating either."""
    shell_rows = con.execute(
        "SELECT shell_id, shortname, flavor FROM shells "
        "WHERE COALESCE(is_deleted,0)=0 ORDER BY shell_id"
    ).fetchall()
    by_checkout = {
        shell_checkout(row["shortname"], row["flavor"], repo_root): row
        for row in shell_rows
    }
    checkouts = [repo_root]
    worktrees = repo_root / ".sc-worktrees"
    if worktrees.is_dir() and not worktrees.is_symlink():
        checkouts.extend(
            path
            for path in sorted(worktrees.iterdir())
            if path.is_dir() and not path.is_symlink()
        )

    written: list[Path] = []
    skipped: list[Path] = []
    deleted: list[Path] = []
    for checkout in checkouts:
        row = by_checkout.get(checkout)
        summary = reconcile_shell(
            con, row["shell_id"] if row is not None else None, checkout
        )
        written.extend(summary["written"])
        skipped.extend(summary["skipped"])
        deleted.extend(summary["deleted"])
        legacy = cleanup_legacy_skills_sc(checkout)
        written.extend(legacy)
        deleted.extend(legacy)
    return {
        "written": written,
        "skipped": skipped,
        "deleted": deleted,
        "checkouts": checkouts,
    }


def partial_failure_message(action: str, exc: Exception) -> str:
    return (
        f"{action} committed in the DB, but skill projection failed: {exc}. "
        "Fix the named path, then run `./sc update --no-fetch` (or "
        "`./sc render skills <shortname>`) to retry exact cleanup."
    )
