#!/usr/bin/env python3
"""Scaffold collision-safe super-coder migrations."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
MIGRATIONS_DIR = ENGINE / "migrations"
SPRINT_REMOVAL_MANIFEST = (
    REPO_ROOT / "tests" / "fixtures" / "sprint_removal" / "manifest.json"
)

_MIGRATION_NAME = re.compile(
    r"^(?P<number>\d{4})_(?P<slug>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$"
)
_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

# Applied migrations are immutable. The only historical collision predates the
# scaffold, so allow exactly these two files and reject any third 0155 entry.
FROZEN_NUMBER_COLLISIONS = {
    "0155": frozenset(
        {
            "0155_reseed_catalogue_cleanup.sql",
            "0155_sprint_conversation_generations.sql",
        }
    )
}


class MigrationScaffoldError(RuntimeError):
    """The migration tree cannot be scaffolded safely."""


def _numbered_migrations(directory: Path) -> dict[str, set[str]]:
    by_number: dict[str, set[str]] = {}
    for path in directory.glob("*.sql"):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        by_number.setdefault(match.group("number"), set()).add(path.name)
    return by_number


def validate_unique_numbers(directory: Path = MIGRATIONS_DIR) -> None:
    """Reject duplicate numeric prefixes except the exact frozen 0155 pair."""
    for number, names in sorted(_numbered_migrations(directory).items()):
        if len(names) < 2:
            continue
        if names == FROZEN_NUMBER_COLLISIONS.get(number):
            continue
        joined = ", ".join(sorted(names))
        raise MigrationScaffoldError(
            f"duplicate migration number {number}: {joined}"
        )


def next_free_number(directory: Path = MIGRATIONS_DIR) -> str:
    by_number = _numbered_migrations(directory)
    highest = max((int(number) for number in by_number), default=0)
    candidate = highest + 1
    if candidate > 9999:
        raise MigrationScaffoldError("migration number space exhausted")
    return f"{candidate:04d}"


def _skeleton(number: str, slug: str) -> str:
    intent = slug.replace("_", " ")
    return (
        f"-- {number} — {intent}.\n"
        "-- Intent: describe the durable schema or system-content change here.\n"
        "-- Keep every statement idempotent (for example: IF NOT EXISTS or "
        "INSERT OR IGNORE).\n\n"
        "BEGIN;\n\n"
        "-- Migration statements go here.\n\n"
        "COMMIT;\n"
    )


def _load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationScaffoldError(
            f"cannot read removal manifest {path}: {exc}"
        ) from exc
    allowed = manifest.get("allowed_reference_files")
    if not isinstance(allowed, list) or not all(
        isinstance(item, str) for item in allowed
    ):
        raise MigrationScaffoldError(
            f"removal manifest {path} has no string allowed_reference_files list"
        )
    return manifest


def _write_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_name(f".{path.name}.migration-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def new_migration(
    slug: str,
    *,
    migrations_dir: Path = MIGRATIONS_DIR,
    manifest_path: Path = SPRINT_REMOVAL_MANIFEST,
) -> Path:
    if _SLUG.fullmatch(slug) is None:
        raise MigrationScaffoldError(
            "slug must be lowercase snake_case (letters, digits, underscores)"
        )

    validate_unique_numbers(migrations_dir)
    number = next_free_number(migrations_dir)
    filename = f"{number}_{slug}.sql"
    relative = f".super-coder/migrations/{filename}"
    manifest = _load_manifest(manifest_path) if manifest_path.exists() else None
    if manifest is not None:
        allowed = manifest["allowed_reference_files"]
        if relative not in allowed:
            allowed.append(relative)

    path = migrations_dir / filename
    created = False
    try:
        with path.open("x") as handle:
            handle.write(_skeleton(number, slug))
        created = True
        # Close the allocation race for different slugs that chose the same
        # number before either file existed. At most one scaffold survives.
        validate_unique_numbers(migrations_dir)
        if manifest is not None:
            _write_manifest(manifest_path, manifest)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    return path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="./sc migration")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new", help="allocate and scaffold a migration")
    create.add_argument("slug", help="lowercase snake_case filename slug")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        path = new_migration(args.slug)
    except (MigrationScaffoldError, OSError) as exc:
        print(f"migration: {exc}", file=sys.stderr)
        return 1
    print(f"migration: created {path.relative_to(REPO_ROOT)}")
    if SPRINT_REMOVAL_MANIFEST.exists():
        print(
            "migration: allowlisted in "
            f"{SPRINT_REMOVAL_MANIFEST.relative_to(REPO_ROOT)}"
        )
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
