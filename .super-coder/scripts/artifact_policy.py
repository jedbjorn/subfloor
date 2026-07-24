#!/usr/bin/env python3
"""Resolve per-instance artifact persistence without changing engine behavior.

Downstream forks default to ``tracked`` for backward compatibility. An instance
may opt into ``local`` through ``.super-coder/instance.json``:

    {"artifact_mode": "local"}

The super-coder source carries ``.super-coder/source-policy.json``. That policy
applies only while the marker is tracked by the current repository; the same
file is ignored engine material in downstream forks and cannot change their
default.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
STATE_DIR = REPO_ROOT / ".sc-state"
LOCAL_DIR = STATE_DIR / "local"
INSTANCE_CONFIG = ENGINE / "instance.json"
SOURCE_POLICY = ENGINE / "source-policy.json"

TRACKED = "tracked"
LOCAL = "local"
VALID_MODES = {TRACKED, LOCAL}


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
    if value not in VALID_MODES:
        allowed = ", ".join(sorted(VALID_MODES))
        raise ArtifactPolicyError(
            f"invalid artifact_mode {value!r} in {path}; expected one of: {allowed}"
        )
    return value


def _source_policy_is_tracked() -> bool:
    """A materialized engine contains SOURCE_POLICY but does not track it."""
    if not SOURCE_POLICY.exists():
        return False
    rel = SOURCE_POLICY.relative_to(REPO_ROOT)
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(rel)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def mode() -> str:
    override = os.environ.get("SC_ARTIFACT_MODE")
    if override is not None:
        if override not in VALID_MODES:
            allowed = ", ".join(sorted(VALID_MODES))
            raise ArtifactPolicyError(
                f"invalid SC_ARTIFACT_MODE {override!r}; expected one of: {allowed}"
            )
        return override
    configured = _read_mode(INSTANCE_CONFIG)
    if configured is not None:
        return configured
    if _source_policy_is_tracked():
        configured = _read_mode(SOURCE_POLICY)
        if configured is not None:
            return configured
    return TRACKED


def tracks_local_artifacts() -> bool:
    return mode() == TRACKED


def content_path() -> Path:
    return STATE_DIR / "content.sql" if tracks_local_artifacts() else LOCAL_DIR / "content.sql"


def render_root() -> Path:
    return REPO_ROOT if tracks_local_artifacts() else LOCAL_DIR / "renders"


def map_db_path() -> Path:
    return STATE_DIR / "map.db" if tracks_local_artifacts() else LOCAL_DIR / "map" / "map.db"


def map_content_path() -> Path:
    if tracks_local_artifacts():
        return STATE_DIR / "map_content.sql"
    return LOCAL_DIR / "map" / "content.sql"


def map_config_path() -> Path:
    if tracks_local_artifacts():
        return STATE_DIR / "map.config.json"
    return LOCAL_DIR / "map" / "config.json"


def retired_skills_path() -> Path:
    if tracks_local_artifacts():
        return STATE_DIR / "skills_retired.json"
    return LOCAL_DIR / "skills_retired.json"


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
    if tracks_local_artifacts():
        return []
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


def set_mode(value: str) -> list[Path]:
    if value not in VALID_MODES:
        allowed = " | ".join(sorted(VALID_MODES))
        raise ArtifactPolicyError(f"usage: sc artifact-mode set <{allowed}>")
    payload: dict = {}
    if INSTANCE_CONFIG.exists():
        try:
            loaded = json.loads(INSTANCE_CONFIG.read_text())
        except json.JSONDecodeError as exc:
            raise ArtifactPolicyError(
                f"cannot change artifact mode: {INSTANCE_CONFIG} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise ArtifactPolicyError(
                f"cannot change artifact mode: {INSTANCE_CONFIG} must contain a JSON object"
            )
        payload = loaded
    payload["artifact_mode"] = value
    atomic_write_text(INSTANCE_CONFIG, json.dumps(payload, indent=2) + "\n")
    return prepare_local_state() if value == LOCAL else []


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "show"
    if command == "show":
        print(json.dumps({
            "artifact_mode": mode(),
            "snapshot": str(content_path().relative_to(REPO_ROOT)),
            "renders": str(render_root().relative_to(REPO_ROOT)),
            "git_publication": tracks_local_artifacts(),
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
    if command == "set" and len(argv) == 2:
        copied = set_mode(argv[1])
        print(f"artifact_mode: {argv[1]}")
        if copied:
            print(f"localized {len(copied)} existing artifact(s) without deleting the originals")
        print("next: SC_ADMIN=1 ./sc snapshot && SC_ADMIN=1 ./sc render")
        return 0
    raise ArtifactPolicyError(
        "usage: sc artifact-mode [show | set <tracked|local> | path <artifact>]"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ArtifactPolicyError as exc:
        raise SystemExit(f"artifact-mode: {exc}") from exc
