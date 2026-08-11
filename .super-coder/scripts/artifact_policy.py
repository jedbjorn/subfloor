#!/usr/bin/env python3
"""Resolve generated per-instance artifacts to ignored local storage."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
STATE_DIR = REPO_ROOT / ".sc-state"
LOCAL_DIR = STATE_DIR / "local"
INSTANCE_CONFIG = ENGINE / "instance.json"
LOCAL = "local"
LEGACY_TRACKED = "tracked"


class ArtifactPolicyError(RuntimeError):
    pass


def _read_mode(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactPolicyError(f"cannot read artifact policy from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactPolicyError(f"artifact policy in {path} must be a JSON object")
    value = payload.get("artifact_mode")
    if value is None:
        return None
    if value not in {LOCAL, LEGACY_TRACKED}:
        raise ArtifactPolicyError(
            f"invalid artifact_mode {value!r} in {path}; generated artifacts "
            "are local-only"
        )
    return value


def mode() -> str:
    override = os.environ.get("SC_ARTIFACT_MODE")
    if override not in (None, LOCAL):
        raise ArtifactPolicyError(
            f"SC_ARTIFACT_MODE={override!r} is unsupported; generated "
            "artifacts are local-only"
        )
    # Read for validation and backward compatibility. A persisted legacy
    # ``tracked`` value is upgrade input, never an active behavior.
    _read_mode(INSTANCE_CONFIG)
    return LOCAL


def tracks_local_artifacts() -> bool:
    """Compatibility predicate: generated artifacts are never Git-tracked."""
    mode()
    return False


def content_path() -> Path:
    return LOCAL_DIR / "content.sql"


def render_root() -> Path:
    return LOCAL_DIR / "renders"


def map_db_path() -> Path:
    return LOCAL_DIR / "map" / "map.db"


def map_content_path() -> Path:
    return LOCAL_DIR / "map" / "content.sql"


def map_config_path() -> Path:
    return LOCAL_DIR / "map" / "config.json"


def retired_skills_path() -> Path:
    return LOCAL_DIR / "skills_retired.json"


def review_patch_root() -> Path:
    """Ignored, per-instance cache for canonical merged-PR patches."""
    return LOCAL_DIR / "review-patches"


def devkit_log_root(repo_root: Path | None = None) -> Path:
    """Ignored logs for fork-owned dev-kit verification hooks."""
    local_dir = LOCAL_DIR if repo_root is None else repo_root / ".sc-state" / "local"
    return local_dir / "devkit-logs"


@contextmanager
def content_write_lock():
    """Serialize snapshot + flat-render pairs across API and CLI processes."""
    path = LOCAL_DIR / ".content-write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        flock(handle.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(handle.fileno(), LOCK_UN)


def _copy_file_once(source: Path, destination: Path) -> bool:
    if destination.exists() or not source.exists() or source == destination:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".migrating")
    shutil.copy2(source, tmp)
    tmp.replace(destination)
    return True


def _backup_sqlite_once(source: Path, destination: Path) -> bool:
    if destination.exists() or not source.exists() or source == destination:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".migrating")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    tmp.replace(destination)
    return True


def prepare_local_state() -> list[Path]:
    """Copy old tracked state into local mode once, without deleting the source.

    Deletion/untracking stays a separate, reviewable Git change. A failed
    migration therefore leaves the old reconstruction source untouched.
    """
    copied: list[Path] = []
    pairs = [
        (STATE_DIR / "content.sql", LOCAL_DIR / "content.sql"),
        (STATE_DIR / "map_content.sql", LOCAL_DIR / "map" / "content.sql"),
        (STATE_DIR / "map.config.json", LOCAL_DIR / "map" / "config.json"),
        (STATE_DIR / "skills_retired.json", LOCAL_DIR / "skills_retired.json"),
    ]
    for source, destination in pairs:
        if _copy_file_once(source, destination):
            copied.append(destination)
    if _backup_sqlite_once(STATE_DIR / "map.db", LOCAL_DIR / "map" / "map.db"):
        copied.append(LOCAL_DIR / "map" / "map.db")
    return copied


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "show"
    if command == "show":
        print(json.dumps({
            "artifact_mode": mode(),
            "snapshot": str(content_path().relative_to(REPO_ROOT)),
            "renders": str(render_root().relative_to(REPO_ROOT)),
            "git_publication": False,
        }, indent=2))
        return 0
    if command == "path" and len(argv) == 2:
        paths = {
            "content": content_path,
            "renders": render_root,
            "map-db": map_db_path,
            "map-content": map_content_path,
            "map-config": map_config_path,
            "skills-retired": retired_skills_path,
        }
        resolver = paths.get(argv[1])
        if resolver is None:
            raise ArtifactPolicyError(
                "usage: sc artifact-mode path <content|renders|map-db|map-content|map-config|skills-retired>"
            )
        print(resolver())
        return 0
    raise ArtifactPolicyError(
        "usage: sc artifact-mode [show | path <artifact>] "
        "(mode switching retired; generated artifacts are local-only)"
    )


if __name__ == "__main__":
    from cli_entry import run_cli

    try:
        raise SystemExit(run_cli(main, sys.argv[1:]))
    except ArtifactPolicyError as exc:
        raise SystemExit(f"artifact-mode: {exc}") from exc
