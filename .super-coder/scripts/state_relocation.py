#!/usr/bin/env python3
"""Verified, fail-stopped relocation of legacy repo-local engine state."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock
from pathlib import Path

import instance_state

RECEIPT_VERSION = 1
GENERATION_LENGTH = 32
DATABASE_SUFFIXES = ("", "-wal", "-shm")


class RelocationError(RuntimeError):
    """Relocation cannot prove one authoritative database state."""


class MaintenanceBusy(RelocationError):
    """Another runtime or maintenance owner prevents canonical mutation."""


@dataclass(frozen=True)
class RelocationResult:
    database: Path
    state: instance_state.InstanceState
    relocated: bool
    recovered: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict) -> None:
    instance_state._atomic_write_json(
        path,
        payload,
        os.geteuid(),
        label=path.name.replace("-", " "),
    )


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        os.write(descriptor, value.encode())
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def ensure_database_generation(state: instance_state.InstanceState) -> str:
    path = state.database_generation
    if path.exists() or path.is_symlink():
        instance_state._lstat_owned_regular(
            path, "database generation", os.geteuid()
        )
        generation = path.read_text().strip()
        if len(generation) != GENERATION_LENGTH or any(
            character not in "0123456789abcdef" for character in generation
        ):
            raise RelocationError("database generation is invalid")
        return generation
    generation = secrets.token_hex(16)
    _atomic_write_text(path, generation + "\n")
    return generation


def _safe_lock_file(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MaintenanceBusy(f"cannot open maintenance lease: {exc}") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise MaintenanceBusy("refusing foreign or non-file maintenance lease")
    if stat.S_IMODE(info.st_mode) & 0o022:
        os.close(descriptor)
        raise MaintenanceBusy("refusing writable-by-others maintenance lease")
    return descriptor


def _ensure_private_directory(path: Path, *, create: bool = True) -> None:
    if not (path.exists() or path.is_symlink()):
        if not create:
            raise RelocationError(f"private directory is missing: {path.name}")
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RelocationError(f"refusing unsafe private directory: {path.name}")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RelocationError(f"refusing foreign or non-private directory: {path.name}")


@contextmanager
def exclusive_maintenance(
    state: instance_state.InstanceState,
    *,
    command: str,
) -> Iterator[None]:
    """Acquire the repo instance's non-blocking exclusive maintenance lease."""
    descriptor = _safe_lock_file(state.maintenance_lock)
    try:
        try:
            flock(descriptor, LOCK_EX | LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise MaintenanceBusy(
                "maintenance_busy: another runtime or maintenance command owns "
                "the instance lease"
            ) from exc
        metadata = {
            "instance_id": state.instance_id,
            "mode": "maintenance",
            "pid": os.getpid(),
            "command": command,
            "acquired_at": _utc_now(),
        }
        encoded = (json.dumps(metadata, sort_keys=True) + "\n").encode()
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        yield
    finally:
        try:
            flock(descriptor, LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def shared_runtime(
    state: instance_state.InstanceState,
    *,
    command: str,
) -> Iterator[None]:
    """Retain shared ownership for one runtime's complete DB lifetime."""
    descriptor = _safe_lock_file(state.maintenance_lock)
    try:
        try:
            flock(descriptor, LOCK_SH | LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise MaintenanceBusy(
                "maintenance_busy: an exclusive maintenance command owns the "
                "instance lease"
            ) from exc
        yield
    finally:
        try:
            flock(descriptor, LOCK_UN)
        finally:
            os.close(descriptor)


def _database_identities(paths: tuple[Path, ...]) -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for path in paths:
        if not path.exists():
            continue
        info = path.stat()
        identities.add((info.st_dev, info.st_ino))
    return identities


def refuse_live_database_owners(
    database: Path,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    """Refuse a legacy process holding the DB or either SQLite sidecar open."""
    targets = tuple(Path(str(database) + suffix) for suffix in DATABASE_SUFFIXES)
    identities = _database_identities(targets)
    if not identities or not proc_root.exists():
        return
    indeterminate: list[str] = []

    def _could_be_engine_runtime(process: Path) -> bool:
        try:
            command = (process / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, ProcessLookupError):
            return False
        except PermissionError:
            return True
        return any(
            marker in command
            for marker in (b".super-coder", b"server.py", b"shell_db.db")
        )

    for process in proc_root.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.geteuid():
                # A foreign uid cannot open the owner-only private DB. Skipping
                # it avoids treating protected system processes as ambiguity.
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if _could_be_engine_runtime(process):
                indeterminate.append(process.name)
            continue
        descriptors = process / "fd"
        try:
            entries = list(descriptors.iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if _could_be_engine_runtime(process):
                indeterminate.append(process.name)
            continue
        for entry in entries:
            try:
                info = entry.stat()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if (info.st_dev, info.st_ino) in identities:
                raise MaintenanceBusy(
                    "runtime_active: a live process still owns the canonical "
                    f"database (pid {process.name}); stop the repo runtime and retry"
                )
    if indeterminate:
        raise MaintenanceBusy(
            "runtime_active: live database ownership could not be proven; "
            "stop the repo runtime and retry from the host Admin seat"
        )


def database_fingerprint(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise RelocationError(f"refusing missing, symlinked, or non-file DB: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RelocationError(f"database integrity check failed: {integrity}")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        has_ledger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        ledger = (
            [
                row[0]
                for row in connection.execute(
                    "SELECT filename FROM schema_migrations ORDER BY filename"
                )
            ]
            if has_ledger
            else []
        )
        digest = hashlib.sha256()
        for statement in connection.iterdump():
            digest.update(statement.encode())
            digest.update(b"\n")
    finally:
        connection.close()
    return {
        "integrity": "ok",
        "user_version": user_version,
        "migration_ledger": ledger,
        "logical_sha256": digest.hexdigest(),
    }


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.unlink(missing_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        with destination_connection:
            source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)


def _verified_backup(
    source: Path,
    state: instance_state.InstanceState,
    fingerprint: dict,
) -> Path:
    _ensure_private_directory(state.backups)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    destination = state.backups / f"shell_db.preupdate.{stamp}.db"
    _sqlite_backup(source, destination)
    if database_fingerprint(destination) != fingerprint:
        destination.unlink(missing_ok=True)
        raise RelocationError("pre-update backup does not match the legacy database")
    return destination


def _copy_file_verified(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RelocationError(f"refusing unsafe legacy state file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise RelocationError(f"refusing unsafe private state file: {destination}")
        if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(
            source.read_bytes()
        ).digest():
            raise RelocationError(f"conflicting legacy and private state: {source.name}")
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    shutil.copy2(source, temporary)
    os.chmod(temporary, 0o600)
    if hashlib.sha256(temporary.read_bytes()).digest() != hashlib.sha256(
        source.read_bytes()
    ).digest():
        temporary.unlink(missing_ok=True)
        raise RelocationError(f"state copy verification failed: {source.name}")
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _relocate_auxiliary_state(repo_root: Path, state: instance_state.InstanceState) -> None:
    legacy_snapshot = repo_root / ".sc-state" / "local" / "content.sql"
    if legacy_snapshot.exists() or legacy_snapshot.is_symlink():
        _copy_file_verified(legacy_snapshot, state.snapshot)
        legacy_snapshot.unlink()
    legacy_lock = repo_root / ".sc-state" / "local" / ".content-write.lock"
    legacy_lock.unlink(missing_ok=True)

    legacy_backups = repo_root / ".sc-state" / "db_backups"
    if not legacy_backups.exists():
        return
    if legacy_backups.is_symlink() or not legacy_backups.is_dir():
        raise RelocationError("refusing unsafe legacy backup directory")
    destination_root = state.backups / "legacy"
    for source in sorted(legacy_backups.rglob("*")):
        relative = source.relative_to(legacy_backups)
        destination = destination_root / relative
        if source.is_dir() and not source.is_symlink():
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        _copy_file_verified(source, destination)
    shutil.rmtree(legacy_backups)


def _remove_legacy_database(database: Path) -> None:
    for suffix in ("-wal", "-shm", ""):
        Path(str(database) + suffix).unlink(missing_ok=True)
    _fsync_directory(database.parent)


def _read_receipt(state: instance_state.InstanceState) -> dict | None:
    return instance_state._relocation_receipt(state)


def _write_receipt(state: instance_state.InstanceState, payload: dict) -> None:
    _atomic_write_json(state.relocation_receipt, payload)


def _recover_publishing(
    legacy: Path,
    state: instance_state.InstanceState,
    receipt: dict,
) -> bool:
    expected = receipt.get("database")
    if not isinstance(expected, dict):
        raise RelocationError("relocation receipt has no database fingerprint")
    candidate_name = receipt.get("candidate")
    if not isinstance(candidate_name, str) or Path(candidate_name).name != candidate_name:
        raise RelocationError("relocation receipt has an invalid candidate")
    candidate = state.root / candidate_name
    recovered = True
    if state.database.exists():
        if database_fingerprint(state.database) != expected:
            raise RelocationError("published private database conflicts with its receipt")
    elif candidate.exists():
        if database_fingerprint(candidate) != expected:
            raise RelocationError("relocation candidate conflicts with its receipt")
        os.replace(candidate, state.database)
        _fsync_directory(state.root)
    elif legacy.exists() and database_fingerprint(legacy) == expected:
        _sqlite_backup(legacy, candidate)
        os.replace(candidate, state.database)
        _fsync_directory(state.root)
    else:
        raise RelocationError("fail-stopped relocation cannot recover its published state")
    receipt.update({"status": "private", "published_at": _utc_now()})
    _write_receipt(state, receipt)
    return recovered


def relocate_legacy_state(
    engine: Path,
    *,
    state: instance_state.InstanceState | None = None,
    command: str = "relocate",
    proc_root: Path = Path("/proc"),
    failpoint: str | None = None,
    lease_held: bool = False,
) -> RelocationResult:
    """Relocate once, or deterministically recover/replay the same move."""
    engine = Path(engine)
    repo_root = engine.parent
    resolved = state or instance_state.resolve(instance_config=engine / "instance.json")
    legacy = instance_state.legacy_database_path(engine)
    lease = (
        nullcontext()
        if lease_held
        else exclusive_maintenance(resolved, command=command)
    )
    with lease:
        receipt = _read_receipt(resolved)
        recovered = False
        if receipt is not None and receipt["status"] == "legacy":
            if not legacy.exists():
                raise RelocationError(
                    "legacy rollback receipt exists but legacy DB is missing"
                )
            if database_fingerprint(legacy) != receipt.get("database"):
                raise RelocationError("legacy rollback DB conflicts with its receipt")
            if resolved.database.exists():
                raise RelocationError(
                    "legacy rollback left conflicting private live state"
                )
            receipt = None
        if receipt is not None and receipt["status"] == "publishing":
            recovered = _recover_publishing(legacy, resolved, receipt)
            receipt = _read_receipt(resolved)
        if receipt is not None and receipt["status"] == "private":
            if not resolved.database.exists():
                raise RelocationError("relocation receipt exists but private DB is missing")
            if database_fingerprint(resolved.database) != receipt.get("database"):
                raise RelocationError("private database conflicts with relocation receipt")
            if legacy.exists():
                if database_fingerprint(legacy) != receipt.get("database"):
                    raise RelocationError("conflicting complete legacy and private databases")
                refuse_live_database_owners(legacy, proc_root=proc_root)
                _relocate_auxiliary_state(repo_root, resolved)
                _remove_legacy_database(legacy)
            ensure_database_generation(resolved)
            return RelocationResult(resolved.database, resolved, True, recovered)

        if legacy.exists() and resolved.database.exists():
            raise RelocationError(
                "conflicting complete legacy and private databases require Admin selection"
            )
        if not legacy.exists():
            ensure_database_generation(resolved)
            return RelocationResult(resolved.database, resolved, False, False)

        refuse_live_database_owners(legacy, proc_root=proc_root)
        fingerprint = database_fingerprint(legacy)
        backup = _verified_backup(legacy, resolved, fingerprint)
        candidate = resolved.root / f".shell_db.relocation.{os.getpid()}.tmp"
        _sqlite_backup(legacy, candidate)
        if database_fingerprint(candidate) != fingerprint:
            candidate.unlink(missing_ok=True)
            raise RelocationError("private relocation candidate failed verification")
        if failpoint == "after_candidate":
            raise RelocationError("injected failure after candidate verification")
        generation = ensure_database_generation(resolved)
        receipt = {
            "version": RECEIPT_VERSION,
            "status": "publishing",
            "instance_id": resolved.instance_id,
            "database_generation": generation,
            "database": fingerprint,
            "backup": str(backup),
            "candidate": candidate.name,
            "started_at": _utc_now(),
        }
        _write_receipt(resolved, receipt)
        if failpoint == "after_receipt":
            raise RelocationError("injected failure after relocation receipt")
        os.replace(candidate, resolved.database)
        _fsync_directory(resolved.root)
        if failpoint == "after_publish":
            raise RelocationError("injected failure after private publication")
        receipt.update({"status": "private", "published_at": _utc_now()})
        _write_receipt(resolved, receipt)
        _relocate_auxiliary_state(repo_root, resolved)
        _remove_legacy_database(legacy)
        return RelocationResult(resolved.database, resolved, True, False)


def restore_legacy_for_old_floor(
    engine: Path,
    *,
    state: instance_state.InstanceState,
    proc_root: Path = Path("/proc"),
    lease_held: bool = False,
) -> Path:
    """Reconstruct the verified legacy pair for an engine without the resolver."""
    engine = Path(engine)
    legacy = instance_state.legacy_database_path(engine)
    if not state.database.exists():
        raise RelocationError("private database is missing during legacy rollback")
    lease = (
        nullcontext()
        if lease_held
        else exclusive_maintenance(state, command="rollback-to-legacy")
    )
    with lease:
        refuse_live_database_owners(state.database, proc_root=proc_root)
        fingerprint = database_fingerprint(state.database)
        candidate = engine / f".shell_db.rollback.{os.getpid()}.tmp"
        _sqlite_backup(state.database, candidate)
        if database_fingerprint(candidate) != fingerprint:
            candidate.unlink(missing_ok=True)
            raise RelocationError("legacy rollback candidate failed verification")
        for suffix in ("-wal", "-shm"):
            Path(str(legacy) + suffix).unlink(missing_ok=True)
        os.replace(candidate, legacy)
        _fsync_directory(engine)
        if database_fingerprint(legacy) != fingerprint:
            raise RelocationError("published legacy rollback DB failed verification")
        if state.snapshot.exists():
            legacy_snapshot = engine.parent / ".sc-state" / "local" / "content.sql"
            _copy_file_verified(state.snapshot, legacy_snapshot)
        receipt = {
            "version": RECEIPT_VERSION,
            "status": "legacy",
            "instance_id": state.instance_id,
            "database_generation": ensure_database_generation(state),
            "database": fingerprint,
            "restored_at": _utc_now(),
        }
        _write_receipt(state, receipt)
        _remove_legacy_database(state.database)
        return legacy


def remove_private_state(
    state: instance_state.InstanceState,
    *,
    verified_backup: Path,
    proc_root: Path = Path("/proc"),
    lease_held: bool = False,
) -> None:
    """Delete only the claimed live root after proving its external backup."""
    backup = Path(verified_backup)
    try:
        backup.relative_to(state.removal_backups)
    except ValueError as exc:
        raise RelocationError(
            "removal backup is outside the owned archive root"
        ) from exc
    archive_root = state.removal_backups.parent
    archive_owner = archive_root / instance_state.OWNER_METADATA
    _ensure_private_directory(archive_root, create=False)
    _ensure_private_directory(state.removal_backups, create=False)
    archive_payload = instance_state._load_json_object(
        archive_owner, "removal archive owner metadata"
    )
    if archive_payload.get("instance_id") != state.instance_id:
        raise RelocationError("refusing foreign removal archive")
    lease = (
        nullcontext()
        if lease_held
        else exclusive_maintenance(state, command="remove")
    )
    with lease:
        refuse_live_database_owners(state.database, proc_root=proc_root)
        if state.database.exists() and database_fingerprint(
            backup
        ) != database_fingerprint(state.database):
            raise RelocationError("removal backup does not match private live state")
        owner = state.root / instance_state.OWNER_METADATA
        payload = instance_state._load_json_object(
            owner, "private state owner metadata"
        )
        if payload.get("instance_id") != state.instance_id:
            raise RelocationError("refusing to remove foreign private state")
        shutil.rmtree(state.root)
        _fsync_directory(state.root.parent)


def prepare_removal_archive(state: instance_state.InstanceState) -> Path:
    """Create and claim the preserved archive adjacent to the live root."""
    archive_root = state.removal_backups.parent
    _ensure_private_directory(archive_root)
    owner = archive_root / instance_state.OWNER_METADATA
    if owner.exists() or owner.is_symlink():
        payload = instance_state._load_json_object(
            owner, "removal archive owner metadata"
        )
        if payload.get("instance_id") != state.instance_id:
            raise RelocationError("refusing foreign removal archive")
    else:
        _atomic_write_json(
            owner,
            {"instance_id": state.instance_id, "owner_uid": os.geteuid()},
        )
    _ensure_private_directory(state.removal_backups)
    return state.removal_backups


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "relocate":
        print("usage: state_relocation.py relocate <engine-directory>", file=sys.stderr)
        return 2
    try:
        result = relocate_legacy_state(Path(argv[1]))
    except (RelocationError, instance_state.InstanceStateError, sqlite3.Error) as exc:
        print(f"state-relocation: {exc}", file=sys.stderr)
        return 1
    print(result.database)
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
