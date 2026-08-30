"""Resolve one installation's private engine-state namespace.

Every production consumer obtains its effective DB path through this module.
The effective target remains the repo-local legacy database until the
maintenance-cutover prerequisites in spec #133 exist.  This module never
relocates, copies, publishes, deletes, or opens the live engine database.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Collection, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
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


class MaintenanceCutoverRequired(InstanceStateError):
    """Private state cannot become active before the spec #133 cutover gate."""


RELOCATION_RECOVERY = (
    "relocation_incomplete: private-state publication must be recovered before "
    "this command can use engine state; run `./sc update` from the host Admin seat"
)


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
    def snapshot_lock(self) -> Path:
        return self.root / ".content-write.lock"

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

    @property
    def removal_backups(self) -> Path:
        """Preserved removal evidence outside the deletable live-state root."""
        return self.root.parent / f"{self.instance_id}.removed" / "db_backups"


@dataclass(frozen=True)
class StateConsumer:
    """One classified production state owner or reference."""

    owner: str
    paths: tuple[str, ...]
    responsibility: str


@dataclass(frozen=True)
class ActiveBackupPaths:
    """Pre-cutover backup destinations selected through the common seam."""

    home: Path
    local: Path
    override: Path | None = None

    @property
    def candidates(self) -> tuple[Path, ...]:
        ordered = (self.override, self.home, self.local)
        return tuple(dict.fromkeys(path for path in ordered if path is not None))


# Exact WU141 preparation inventory.  DB owners adopt active_database_path in
# this unit while private-location activation remains behind spec #133.
# Keeping the inventory executable lets focused tests pin both boundaries.
PRODUCTION_CONSUMERS = (
    StateConsumer(
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
    StateConsumer(
        "db_driver",
        (".super-coder/scripts/db_driver.py",),
        "canonical connection entry point",
    ),
    StateConsumer(
        "snapshot_and_render",
        (
            ".super-coder/scripts/artifact_policy.py",
            ".super-coder/scripts/snapshot.py",
            ".super-coder/scripts/render.py",
            ".super-coder/render/compose.py",
        ),
        "private snapshot and render inputs",
    ),
    StateConsumer(
        "backup_and_rebuild",
        (".super-coder/scripts/db_backup.py", ".super-coder/scripts/rebuild.py"),
        "WAL-safe backup selection and canonical publication",
    ),
    StateConsumer(
        "install_and_update",
        (
            ".super-coder/scripts/install.py",
            ".super-coder/scripts/update.py",
            ".super-coder/scripts/engine_manifest.py",
        ),
        "fresh identity creation and stopped-runtime relocation",
    ),
    StateConsumer(
        "rollback_remove_and_eject",
        (
            ".super-coder/scripts/rollback.py",
            ".super-coder/scripts/remove.py",
            ".super-coder/scripts/eject.py",
        ),
        "deterministic recovery and exact private-state ownership",
    ),
    StateConsumer(
        "shell_entry_and_liveness",
        (
            ".super-coder/scripts/dispatch.sh",
            ".super-coder/scripts/execution_view.py",
            ".super-coder/scripts/run.py",
            ".super-coder/scripts/shell_liveness.py",
        ),
        "launch refusal and bounded liveness reads",
    ),
    StateConsumer(
        "instance_configuration",
        (
            ".super-coder/scripts/ports.py",
            ".super-coder/scripts/feature.py",
        ),
        "locked owner-local configuration mutation",
    ),
    StateConsumer(
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
    StateConsumer(
        "legacy_and_candidate_paths",
        (
            ".super-coder/scripts/map_db.py",
            ".super-coder/scripts/map_repo.py",
            ".super-coder/scripts/render_check.py",
            ".super-coder/scripts/state_relocation.py",
        ),
        "legacy fallback classification and candidate-only verification",
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


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(
    path: Path,
    payload: dict,
    owner_uid: int,
    *,
    label: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        _lstat_owned_regular(path, label, owner_uid)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _instance_id_lock(config_path: Path, owner_uid: int):
    """Serialize one installation's initial identity assignment."""
    lock_directory = config_path.parent / "run"
    try:
        lock_directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        raise InstanceStateError(f"cannot create instance identity lock: {exc}") from exc
    try:
        directory_info = lock_directory.lstat()
    except OSError as exc:
        raise InstanceStateError(f"cannot inspect instance identity lock: {exc}") from exc
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
        directory_info.st_mode
    ):
        raise InstanceStateError("refusing unsafe instance identity lock directory")
    if directory_info.st_uid != owner_uid or stat.S_IMODE(directory_info.st_mode) & 0o022:
        raise InstanceStateError("refusing foreign instance identity lock directory")
    lock_path = lock_directory / "instance-id.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise InstanceStateError(f"cannot open instance identity lock: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid:
            raise InstanceStateError("refusing foreign instance identity lock")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise InstanceStateError("refusing writable-by-others instance identity lock")
        flock(descriptor, LOCK_EX)
        try:
            yield
        finally:
            flock(descriptor, LOCK_UN)
    finally:
        os.close(descriptor)


def _read_instance_config(config_path: Path, owner_uid: int) -> dict:
    if not (config_path.exists() or config_path.is_symlink()):
        return {}
    _lstat_owned_regular(config_path, "instance configuration", owner_uid)
    return _load_json_object(config_path, "instance configuration")


def _persisted_instance_id(payload: dict) -> str | None:
    current = payload.get(INSTANCE_ID_KEY)
    if current is None:
        return None
    if not _valid_instance_id(current):
        raise InstanceStateError("instance configuration has an invalid instance ID")
    return current


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
    payload = _read_instance_config(config_path, uid)
    current = _persisted_instance_id(payload)
    if current is not None:
        return current
    if not create:
        raise InstanceStateError("instance configuration has no instance ID")
    candidate = (id_factory or (lambda: secrets.token_hex(16)))()
    if not _valid_instance_id(candidate):
        raise InstanceStateError("instance ID factory returned an invalid identifier")
    with _instance_id_lock(config_path, uid):
        payload = _read_instance_config(config_path, uid)
        winner = _persisted_instance_id(payload)
        if winner is not None:
            return winner
        payload[INSTANCE_ID_KEY] = candidate
        _atomic_write_json(
            config_path,
            payload,
            uid,
            label="instance configuration",
        )
        winner = _persisted_instance_id(_read_instance_config(config_path, uid))
        if winner is None:
            raise InstanceStateError("instance ID publication did not persist")
        return winner


def merge_instance_config(
    config_path: Path,
    changes: Mapping[str, object],
    *,
    remove: Collection[str] = (),
    require_instance_id: bool = False,
    owner_uid: int | None = None,
) -> dict:
    """Lock, reread, and atomically merge one owner-local configuration."""
    if INSTANCE_ID_KEY in changes or INSTANCE_ID_KEY in remove:
        raise InstanceStateError("instance ID can only be assigned by the resolver")
    uid = os.geteuid() if owner_uid is None else owner_uid
    config_path = Path(config_path)
    with _instance_id_lock(config_path, uid):
        payload = _read_instance_config(config_path, uid)
        instance_id = _persisted_instance_id(payload)
        if require_instance_id and instance_id is None:
            raise InstanceStateError("instance configuration has no instance ID")
        for key in remove:
            payload.pop(key, None)
        payload.update(changes)
        if payload == _read_instance_config(config_path, uid):
            return payload
        _atomic_write_json(
            config_path,
            payload,
            uid,
            label="instance configuration",
        )
        durable = _read_instance_config(config_path, uid)
        if _persisted_instance_id(durable) != instance_id:
            raise InstanceStateError("instance identity changed during configuration update")
        return durable


def update_bound_instance_config(
    config_path: Path,
    changes: Mapping[str, object],
    *,
    owner_uid: int | None = None,
) -> dict:
    """Atomically update fields on an already-bound installation."""
    return merge_instance_config(
        config_path,
        changes,
        require_instance_id=True,
        owner_uid=owner_uid,
    )


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
        label="private state owner metadata",
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
    if not create:
        _ensure_private_root(root, instance_id, owner_uid=uid, create=False)
        return InstanceState(instance_id=instance_id, root=root)
    with _instance_id_lock(Path(instance_config), uid):
        durable_id = _persisted_instance_id(
            _read_instance_config(Path(instance_config), uid)
        )
        if durable_id != instance_id:
            raise InstanceStateError("instance identity changed during resolution")
        _ensure_private_root(root, instance_id, owner_uid=uid, create=True)
    return InstanceState(instance_id=instance_id, root=root)


def active_database_path(
    engine: Path,
    *,
    private_state: InstanceState | None = None,
) -> Path:
    """Return the only authoritative live DB target.

    A caller may inject a validated state object in tests/maintenance code.
    Production activation is otherwise derived from the owner-local instance
    identity and a durable relocation receipt.  A fresh bound installation
    selects private state before its first DB is built.  Two complete copies
    without a receipt are never guessed between.
    """
    engine = Path(engine)
    legacy = legacy_database_path(engine)
    state = private_state or _bound_private_state(engine)
    if state is None:
        return legacy
    receipt = _relocation_receipt(state)
    if receipt is not None and receipt["status"] == "publishing":
        raise MaintenanceCutoverRequired(RELOCATION_RECOVERY)
    if receipt is not None and receipt["status"] == "private":
        return state.database
    if legacy.exists() and state.database.exists():
        raise MaintenanceCutoverRequired(
            "conflicting legacy and private engine databases require Admin recovery"
        )
    if legacy.exists():
        return legacy
    return state.database


def maintenance_database_path(
    engine: Path,
    *,
    private_state: InstanceState | None = None,
) -> Path:
    """Return the recovery target without activating an incomplete cutover.

    Only lifecycle code that first runs ``relocate_legacy_state`` may use this
    selector. Ordinary consumers must use ``active_database_path`` so a durable
    ``publishing`` receipt remains fail-stopped.
    """
    engine = Path(engine)
    state = private_state or _bound_private_state(engine)
    if state is None:
        return legacy_database_path(engine)
    receipt = _relocation_receipt(state)
    if receipt is not None and receipt["status"] in {"publishing", "private"}:
        return state.database
    return active_database_path(engine, private_state=state)


def maintenance_snapshot_path(
    repo_root: Path,
    *,
    private_state: InstanceState | None = None,
) -> Path:
    """Return the snapshot paired with the recovery-only database selector."""
    root = Path(repo_root)
    state = private_state or _bound_private_state(root / ".super-coder")
    if state is not None and maintenance_database_path(
        root / ".super-coder", private_state=state
    ) == state.database:
        return state.snapshot
    return root / ".sc-state" / "local" / "content.sql"


def legacy_database_path(engine: Path) -> Path:
    """Return the migration-only historical DB target."""
    return Path(engine) / "shell_db.db"


def _bound_private_state(engine: Path) -> InstanceState | None:
    config = engine / "instance.json"
    if not (config.exists() or config.is_symlink()):
        return None
    try:
        return resolve(instance_config=config, create=False)
    except InstanceStateError as exc:
        if "does not exist" in str(exc) or "has no instance ID" in str(exc):
            return None
        raise


def maintenance_state(engine: Path) -> InstanceState:
    """Return the common lease namespace, including pre-instance legacy floors."""
    engine = Path(engine)
    state = _bound_private_state(engine)
    if state is not None:
        return state
    root = engine.parent / ".sc-state" / "local"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return InstanceState(instance_id="legacy", root=root)


def _relocation_receipt(state: InstanceState) -> dict | None:
    path = state.relocation_receipt
    if not (path.exists() or path.is_symlink()):
        return None
    _lstat_owned_regular(path, "relocation receipt", os.geteuid())
    payload = _load_json_object(path, "relocation receipt")
    if payload.get("instance_id") != state.instance_id:
        raise InstanceStateError("relocation receipt names a different instance")
    if payload.get("status") not in {"publishing", "private", "legacy"}:
        raise InstanceStateError("relocation receipt has an invalid status")
    return payload


def active_snapshot_path(
    repo_root: Path,
    *,
    private_state: InstanceState | None = None,
) -> Path:
    """Return the snapshot paired with the authoritative database."""
    root = Path(repo_root)
    state = private_state or _bound_private_state(root / ".super-coder")
    if state is not None and active_database_path(
        root / ".super-coder", private_state=state
    ) == state.database:
        return state.snapshot
    return root / ".sc-state" / "local" / "content.sql"


def active_snapshot_lock_path(
    repo_root: Path,
    *,
    private_state: InstanceState | None = None,
) -> Path:
    """Return the lock paired with the effective snapshot path."""
    root = Path(repo_root)
    state = private_state or _bound_private_state(root / ".super-coder")
    if state is not None and active_database_path(
        root / ".super-coder", private_state=state
    ) == state.database:
        return state.snapshot_lock
    return root / ".sc-state" / "local" / ".content-write.lock"


def active_backup_paths(
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
    *,
    private_state: InstanceState | None = None,
) -> ActiveBackupPaths:
    """Return ordered backup paths paired with the authoritative database."""
    env = os.environ if environ is None else environ
    root = Path(repo_root).expanduser()
    if not root.is_absolute():
        raise InstanceStateError("backup repository root must be absolute")
    root = root.resolve()
    configured = env.get("SC_DB_BACKUP_DIR", "").strip()
    override = Path(configured).expanduser() if configured else None
    if override is not None:
        if not override.is_absolute():
            override = root / override
        override = override.resolve()
    state = private_state or _bound_private_state(root / ".super-coder")
    if state is not None and active_database_path(
        root / ".super-coder", private_state=state
    ) == state.database:
        return ActiveBackupPaths(
            override=override,
            home=state.backups.resolve(),
            local=state.backups.resolve(),
        )
    home = Path(env.get("HOME") or Path.home()).expanduser()
    if not home.is_absolute():
        home = root / home
    home = home.resolve()
    return ActiveBackupPaths(
        override=override,
        home=(home / "db_backups" / root.name).resolve(),
        local=(root / ".sc-state" / "db_backups").resolve(),
    )


def removal_backup_root(
    repo_root: Path,
    *,
    private_state: InstanceState | None = None,
) -> Path:
    """Return a preserved backup root outside a private live-state deletion."""
    root = Path(repo_root)
    state = private_state or _bound_private_state(root / ".super-coder")
    if state is not None and active_database_path(
        root / ".super-coder", private_state=state
    ) == state.database:
        return state.removal_backups
    return root / ".sc-state" / "db_backups"


def production_consumer_inventory() -> tuple[StateConsumer, ...]:
    """Return the classified production state-owner/reference inventory."""
    return PRODUCTION_CONSUMERS


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "active-database":
            print(active_database_path(Path(argv[1])))
            return 0
        if len(argv) == 4 and argv[0] == "config-set":
            merge_instance_config(Path(argv[1]), {argv[2]: json.loads(argv[3])})
            return 0
        raise InstanceStateError(
            "usage: instance_state.py active-database <engine-directory> | "
            "config-set <instance.json> <key> <json-value>"
        )
    except (InstanceStateError, json.JSONDecodeError) as exc:
        raise SystemExit(f"instance-state: {exc}") from exc


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
