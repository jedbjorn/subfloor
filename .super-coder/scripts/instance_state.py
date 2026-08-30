"""Resolve one installation's private engine-state namespace.

This module establishes the path contract only.  Production consumers remain
on their legacy paths until the maintenance-cutover prerequisites in spec #133
exist.  Importing this module must therefore never relocate, copy, publish, or
open the live engine database.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

INSTANCE_ID_KEY = "instance_id"
INSTANCE_ID_LENGTH = 32
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
STATE_VENDOR = "subfloor"
STATE_COLLECTION = "instances"
OWNER_METADATA = "owner.json"


class InstanceStateError(RuntimeError):
    """The private instance-state identity or filesystem boundary is unsafe."""


@dataclass(frozen=True)
class InstanceState:
    """Canonical private paths for one opaque installation identity."""

    instance_id: str
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "shell_db.db"

    @property
    def snapshot(self) -> Path:
        return self.root / "content.sql"

    @property
    def backups(self) -> Path:
        return self.root / "db_backups"

    @property
    def maintenance_lock(self) -> Path:
        return self.root / "maintenance.lock"

    @property
    def database_generation(self) -> Path:
        return self.root / "database-generation"

    @property
    def relocation_receipt(self) -> Path:
        return self.root / "relocation.json"

    @property
    def recovery_evidence(self) -> Path:
        return self.root / "recovery"


@dataclass(frozen=True)
class DeferredConsumer:
    """One production cutover owner intentionally deferred behind spec #133."""

    owner: str
    paths: tuple[str, ...]
    responsibility: str


# Exact WU141 preparation inventory.  These owners must adopt InstanceState in
# the stopped-runtime relocation unit, not opportunistically in this resolver
# unit.  Keeping the inventory executable lets focused tests pin the boundary.
DEFERRED_CONSUMERS = (
    DeferredConsumer(
        "api_and_daemons",
        (
            ".super-coder/api/server.py",
            ".super-coder/api/conversation_routes.py",
            ".super-coder/scripts/conversation_broker.py",
            ".super-coder/scripts/conversation_reaper.py",
            ".super-coder/scripts/sprint_runtime.py",
            ".super-coder/scripts/sprint_pr_watcher.py",
        ),
        "API lifetime and daemon DB-path injection",
    ),
    DeferredConsumer(
        "db_driver",
        (".super-coder/scripts/db_driver.py",),
        "canonical connection entry point",
    ),
    DeferredConsumer(
        "snapshot_and_render",
        (
            ".super-coder/scripts/artifact_policy.py",
            ".super-coder/scripts/snapshot.py",
            ".super-coder/scripts/render.py",
        ),
        "private snapshot and render inputs",
    ),
    DeferredConsumer(
        "backup_and_rebuild",
        (".super-coder/scripts/db_backup.py", ".super-coder/scripts/rebuild.py"),
        "WAL-safe backup selection and canonical publication",
    ),
    DeferredConsumer(
        "install_and_update",
        (".super-coder/scripts/install.py", ".super-coder/scripts/update.py"),
        "fresh identity creation and stopped-runtime relocation",
    ),
    DeferredConsumer(
        "rollback_remove_and_eject",
        (
            ".super-coder/scripts/rollback.py",
            ".super-coder/scripts/remove.py",
            ".super-coder/scripts/eject.py",
        ),
        "deterministic recovery and exact private-state ownership",
    ),
    DeferredConsumer(
        "shell_entry_and_liveness",
        (
            ".super-coder/scripts/run.py",
            ".super-coder/scripts/shell_liveness.py",
        ),
        "launch refusal and bounded liveness reads",
    ),
    DeferredConsumer(
        "catalogue_writers",
        (
            ".super-coder/scripts/seed_skills.py",
            ".super-coder/scripts/skill.py",
            ".super-coder/scripts/models.py",
            ".super-coder/scripts/analytics.py",
            ".super-coder/scripts/init_fork.py",
            ".super-coder/scripts/seed_dogfood.py",
        ),
        "remaining direct engine-DB owners",
    ),
)


def _valid_instance_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == INSTANCE_ID_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_json_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InstanceStateError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstanceStateError(f"{label} must contain a JSON object")
    return payload


def _lstat_owned_regular(path: Path, label: str, owner_uid: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstanceStateError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise InstanceStateError(f"refusing symlinked {label}")
    if not stat.S_ISREG(info.st_mode):
        raise InstanceStateError(f"refusing non-file {label}")
    if info.st_uid != owner_uid:
        raise InstanceStateError(f"refusing foreign-owned {label}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise InstanceStateError(f"refusing writable-by-others {label}")
    return info


def _atomic_write_json(path: Path, payload: dict, owner_uid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        _lstat_owned_regular(path, "instance configuration", owner_uid)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def ensure_instance_id(
    config_path: Path,
    *,
    create: bool = True,
    owner_uid: int | None = None,
    id_factory: Callable[[], str] | None = None,
) -> str:
    """Read or atomically create the opaque ID in the owner-local config."""
    uid = os.geteuid() if owner_uid is None else owner_uid
    config_path = Path(config_path)
    if config_path.exists() or config_path.is_symlink():
        _lstat_owned_regular(config_path, "instance configuration", uid)
        payload = _load_json_object(config_path, "instance configuration")
    else:
        if not create:
            raise InstanceStateError("instance configuration has no instance ID")
        payload = {}
    current = payload.get(INSTANCE_ID_KEY)
    if current is not None:
        if not _valid_instance_id(current):
            raise InstanceStateError("instance configuration has an invalid instance ID")
        return current
    if not create:
        raise InstanceStateError("instance configuration has no instance ID")
    candidate = (id_factory or (lambda: secrets.token_hex(16)))()
    if not _valid_instance_id(candidate):
        raise InstanceStateError("instance ID factory returned an invalid identifier")
    payload[INSTANCE_ID_KEY] = candidate
    _atomic_write_json(config_path, payload, uid)
    return candidate


def _state_home(environ: Mapping[str, str]) -> Path:
    configured = environ.get("XDG_STATE_HOME", "").strip()
    if configured:
        home = Path(configured).expanduser()
    else:
        configured_home = environ.get("HOME", "").strip()
        home = (
            Path(configured_home).expanduser() / ".local" / "state"
            if configured_home
            else Path.home() / ".local" / "state"
        )
    if not home.is_absolute():
        raise InstanceStateError("XDG state home must be absolute")
    return home


def _ensure_owned_directory(
    path: Path,
    label: str,
    owner_uid: int,
    *,
    create: bool,
) -> bool:
    existed = path.exists() or path.is_symlink()
    if not existed:
        if not create:
            raise InstanceStateError(f"{label} does not exist")
        try:
            path.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except OSError as exc:
            raise InstanceStateError(f"cannot create {label}: {exc}") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstanceStateError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise InstanceStateError(f"refusing symlinked {label}")
    if not stat.S_ISDIR(info.st_mode):
        raise InstanceStateError(f"refusing non-directory {label}")
    if info.st_uid != owner_uid:
        raise InstanceStateError(f"refusing foreign-owned {label}")
    if stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE:
        raise InstanceStateError(f"refusing non-private {label}; mode must be 0700")
    return existed


def _ensure_private_root(
    root: Path,
    instance_id: str,
    *,
    owner_uid: int,
    create: bool,
) -> None:
    vendor = root.parent.parent
    collection = root.parent
    if create:
        try:
            vendor.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        except OSError as exc:
            raise InstanceStateError(
                f"cannot create private state namespace: {exc}"
            ) from exc
    _ensure_owned_directory(vendor, "private state namespace", owner_uid, create=create)
    _ensure_owned_directory(
        collection, "private state collection", owner_uid, create=create
    )
    existed = _ensure_owned_directory(
        root, "private instance-state directory", owner_uid, create=create
    )
    metadata = root / OWNER_METADATA
    if metadata.exists() or metadata.is_symlink():
        _lstat_owned_regular(metadata, "private state owner metadata", owner_uid)
        payload = _load_json_object(metadata, "private state owner metadata")
        if payload.get(INSTANCE_ID_KEY) != instance_id or payload.get("owner_uid") != owner_uid:
            raise InstanceStateError("refusing foreign private instance state")
        return
    if existed or not create:
        raise InstanceStateError("refusing unclaimed private instance state")
    _atomic_write_json(
        metadata,
        {INSTANCE_ID_KEY: instance_id, "owner_uid": owner_uid},
        owner_uid,
    )


def resolve(
    *,
    instance_config: Path,
    environ: Mapping[str, str] | None = None,
    state_home: Path | None = None,
    create: bool = True,
    owner_uid: int | None = None,
    id_factory: Callable[[], str] | None = None,
) -> InstanceState:
    """Resolve and validate private state without touching live DB artifacts.

    ``state_home`` and ``id_factory`` are explicit test/installer injection
    seams.  Production identity always comes from ``instance_config``; callers
    cannot supply an instance ID or database path.
    """
    uid = os.geteuid() if owner_uid is None else owner_uid
    instance_id = ensure_instance_id(
        instance_config,
        create=create,
        owner_uid=uid,
        id_factory=id_factory,
    )
    base = Path(state_home) if state_home is not None else _state_home(
        os.environ if environ is None else environ
    )
    if not base.is_absolute():
        raise InstanceStateError("private state root must be absolute")
    root = base / STATE_VENDOR / STATE_COLLECTION / instance_id
    _ensure_private_root(root, instance_id, owner_uid=uid, create=create)
    return InstanceState(instance_id=instance_id, root=root)


def deferred_consumer_inventory() -> tuple[DeferredConsumer, ...]:
    """Return the production adoption inventory deferred to WU142/U2."""
    return DEFERRED_CONSUMERS
