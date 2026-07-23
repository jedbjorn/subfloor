#!/usr/bin/env python3
"""Select and create WAL-safe engine DB backups.

Selection is ordered and fail-closed:

1. ``SC_DB_BACKUP_DIR`` when set and writable.
2. ``~/db_backups/<repo>`` when writable.
3. The gitignored repo-local ``.sc-state/db_backups`` directory.

Every candidate is created and write-probed before it is returned.  Callers
that are about to stop supervised processes can therefore resolve the
destination first and know a predictable permission failure cannot strand the
fork offline.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Mapping

KEEP_BACKUPS = 5


class BackupDestinationError(RuntimeError):
    """No configured backup destination can be written."""


def preferred_home_dir(
    repo_root: Path, environ: Mapping[str, str] | None = None
) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home()).expanduser()
    return home / "db_backups" / repo_root.name


def candidate_dirs(
    repo_root: Path, environ: Mapping[str, str] | None = None
) -> list[Path]:
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    override = env.get("SC_DB_BACKUP_DIR", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend(
        [
            preferred_home_dir(repo_root, env),
            repo_root / ".sc-state" / "db_backups",
        ]
    )
    # An override may intentionally name the normal home or local directory.
    # Probe it once and preserve the documented priority.
    return list(dict.fromkeys(candidates))


def _probe_writable(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".sc-write-probe-{os.getpid()}-{time.time_ns()}"
    fd: int | None = None
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"ok")
    finally:
        if fd is not None:
            os.close(fd)
        probe.unlink(missing_ok=True)


def select_backup_dir(
    repo_root: Path, environ: Mapping[str, str] | None = None
) -> Path:
    failures: list[str] = []
    for candidate in candidate_dirs(repo_root, environ):
        try:
            _probe_writable(candidate)
        except OSError as exc:
            failures.append(f"{candidate}: {exc.strerror or exc}")
            continue
        return candidate
    detail = "\n  - ".join(failures)
    raise BackupDestinationError(
        "no writable DB backup destination; tried:\n  - "
        f"{detail}\nSet SC_DB_BACKUP_DIR to a writable directory and retry."
    )


def latest_backup(
    repo_root: Path,
    pattern: str,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Find the newest matching backup across every configured candidate.

    The writable destination may change between update and rollback (for
    example, a restricted seat adds ``SC_DB_BACKUP_DIR`` later, or home becomes
    read-only). Discovery therefore reads all candidate directories instead of
    hiding an existing restore point behind the currently selected writer.
    """
    matches: list[Path] = []
    for directory in candidate_dirs(repo_root, environ):
        try:
            matches.extend(directory.glob(pattern))
        except OSError:
            continue
    if not matches:
        return None
    return max(matches, key=lambda path: (path.stat().st_mtime_ns, path.name))


def backup_database(
    src: Path,
    directory: Path,
    prefix: str,
    *,
    keep: int = KEEP_BACKUPS,
) -> Path | None:
    """Create a WAL-safe SQLite online backup and prune old same-prefix files."""
    if not src.exists():
        return None
    _probe_writable(directory)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = directory / f"shell_db.{prefix}.{stamp}.db"
    src_con = sqlite3.connect(src)
    dst_con = sqlite3.connect(dst)
    try:
        with dst_con:
            src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    for old in sorted(directory.glob(f"shell_db.{prefix}.*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return dst


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in {"select", "backup"}:
        print(
            "usage: db_backup.py select <repo-root> | "
            "backup <db> <repo-root> <prefix> [destination]",
            file=sys.stderr,
        )
        return 2
    command = argv[0]
    try:
        if command == "select" and len(argv) == 2:
            print(select_backup_dir(Path(argv[1]).resolve()))
            return 0
        if command == "backup" and len(argv) in {4, 5}:
            src = Path(argv[1])
            repo_root = Path(argv[2]).resolve()
            destination = (
                Path(argv[4]) if len(argv) == 5 else select_backup_dir(repo_root)
            )
            result = backup_database(src, destination, argv[3])
            if result is None:
                print("→ no DB yet — nothing to back up")
            else:
                print(f"→ DB backed up -> {result}")
            return 0
    except (BackupDestinationError, OSError, sqlite3.Error) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    print("db_backup.py: invalid arguments", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
