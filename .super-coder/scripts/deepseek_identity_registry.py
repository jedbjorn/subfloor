#!/usr/bin/env python3
"""Per-fork DeepSeek shell identity registry and profile materialization.

The registry is deliberately filesystem-owned.  The engine is the sole writer;
the stock DeepSeek Host plugin is a read-only consumer.  Every mutation is an
owner-only, flock-serialized transaction whose commit point is the atomic
registry replacement.  Credential bytes live only in unique mode-0600
artifacts and never in the registry or diagnostics.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent.resolve()
PLUGIN = ENGINE / "assets" / "deepseek" / "sc-shell-env-plugin.mjs"
EXECUTION_LAUNCHER = ENGINE / "scripts" / "deepseek_execution_domain.py"
EXECUTION_PROVENANCE = ENGINE / "scripts" / "dsh_execution_provenance.py"
DEFAULT_ROOT = ENGINE / "run" / "deepseek-identity"

REGISTRY_CONTRACT = "sc-dsh-identity-registry-v1"
CREDENTIAL_CONTRACT = "sc-dsh-binding-credential-v1"
HEALTH_CONTRACT = "sc-dsh-plugin-health-v1"
HOST_IDENTITY_CONTRACT = "sc-dsh-host-identity-v1"
PROFILE_CONTRACT = "sc-dsh-profile-v1"
SCHEMA_VERSION = 1
ALIASES = (
    "DSH_SC_SHELL_ID",
    "DSH_SC_SHELL_SHORTNAME",
    "DSH_SC_SHELL_WORKTREE",
    "DSH_SC_API_BASE",
    "DSH_SC_MEM_CREDENTIAL_FILE",
    "DSH_SC_BINDING_GENERATION",
    "DSH_SC_PLUGIN_HEALTH_GENERATION",
)
LIVE_STATES = frozenset({"active", "closing"})
SAFE_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class DeepSeekIdentityError(RuntimeError):
    """Stable fail-closed identity-registry error."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class SimulatedRegistryCrash(RuntimeError):
    """Test-only abrupt crash injected at a named durable boundary."""


@dataclass(frozen=True)
class RegistryLayout:
    fork_id: str
    profile_id: str
    root: Path
    dsh_home: Path
    profile_dir: Path
    registry: Path
    lock: Path
    credentials: Path
    health: Path
    host_identity: Path
    profile_identity: Path


@dataclass(frozen=True)
class TransactionReceipt:
    operation: str
    root_session_id: str
    snapshot_generation: int
    record_generation: int
    lifecycle_epoch: int
    state: str
    committed: bool = True


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_fork_id(repo_root: Path) -> str:
    resolved = repo_root.resolve(strict=True)
    if not resolved.is_dir():
        raise DeepSeekIdentityError(
            "HARNESS_FORK_IDENTITY_INVALID", "canonical fork root is not a directory"
        )
    return _sha256_bytes(str(resolved).encode())


def declared_variable_schema_digest() -> str:
    return _sha256_bytes(_canonical_json(list(ALIASES)))


def plugin_contract_generation(inputs: Mapping[str, str]) -> str:
    expected = {
        "canonical_fork_id",
        "dedicated_profile_id",
        "plugin_bundle_digest",
        "declared_variable_schema_digest",
        "canonical_registry_path_identity",
        "host_boot_generation",
        "plugin_load_hmr_generation",
    }
    if set(inputs) != expected or any(not value for value in inputs.values()):
        raise DeepSeekIdentityError(
            "HARNESS_PLUGIN_HEALTH_INVALID", "plugin contract inputs are incomplete"
        )
    return _sha256_bytes(_canonical_json(dict(inputs)))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validate_session(value: str, *, field: str) -> str:
    if not isinstance(value, str) or SAFE_SESSION.fullmatch(value) is None:
        raise DeepSeekIdentityError(
            "HARNESS_BINDING_INVALID", f"{field} is not a bounded DSH session identity"
        )
    return value


def _validate_loopback_api(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DeepSeekIdentityError(
            "HARNESS_BINDING_INVALID",
            "binding API base must be credential-free loopback HTTP",
        )
    return value.rstrip("/")


def _ensure_owner_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise DeepSeekIdentityError(
            "HARNESS_REGISTRY_UNSAFE", "identity registry directory is unsafe"
        )
    os.chmod(path, 0o700)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_owner_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    _ensure_owner_dir(path.parent)
    descriptor, temporary_raw = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_owner_json(path: Path, *, missing_code: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise OSError("unsafe owner-only artifact")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
                raise OSError("artifact identity changed before reading")
            value = json.load(handle)
            after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise OSError("artifact changed while reading")
    except FileNotFoundError as exc:
        raise DeepSeekIdentityError(
            missing_code, "owner-only artifact is missing"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepSeekIdentityError(
            "HARNESS_REGISTRY_UNSAFE", "owner-only artifact is unreadable or unsafe"
        ) from exc
    if not isinstance(value, dict):
        raise DeepSeekIdentityError(
            "HARNESS_REGISTRY_INVALID", "owner-only artifact is not a JSON object"
        )
    return value


def _crash(point: str, crash_at: str | None) -> None:
    if point == crash_at:
        raise SimulatedRegistryCrash(point)


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Read one Linux process identity without trusting its environment."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
        command_end = raw.rfind(")")
        if command_end < 0:
            return None
        fields = raw[command_end + 1 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


class DeepSeekIdentityRegistry:
    """One fork's profile, plugin health, and transactional binding registry."""

    def __init__(
        self,
        *,
        repo_root: Path = REPO_ROOT,
        runtime_root: Path = DEFAULT_ROOT,
        plugin_path: Path = PLUGIN,
    ) -> None:
        if not sys.platform.startswith("linux"):
            raise DeepSeekIdentityError(
                "HARNESS_PLATFORM_UNSUPPORTED",
                "per-execution DeepSeek identity is supported on Linux only",
            )
        repo = repo_root.resolve(strict=True)
        fork_id = canonical_fork_id(repo)
        profile_id = f"sc-{fork_id[:20]}"
        root = runtime_root.resolve() / fork_id
        dsh_home = root / "dsh-home"
        profile_dir = dsh_home / "profiles" / profile_id
        self.repo_root = repo
        self.plugin_path = plugin_path.resolve(strict=True)
        self.layout = RegistryLayout(
            fork_id=fork_id,
            profile_id=profile_id,
            root=root,
            dsh_home=dsh_home,
            profile_dir=profile_dir,
            registry=root / "registry.json",
            lock=root / "registry.lock",
            credentials=root / "credentials",
            health=root / "plugin-health.json",
            host_identity=root / "host-identity.json",
            profile_identity=root / "profile-identity.json",
        )

    @property
    def plugin_digest(self) -> str:
        return _sha256_bytes(
            _canonical_json(
                {
                    "identity_plugin": _sha256_file(self.plugin_path),
                    "execution_launcher": self.execution_launcher_digest,
                    "execution_provenance": self.execution_provenance_digest,
                }
            )
        )

    @property
    def execution_launcher_digest(self) -> str:
        return _sha256_file(EXECUTION_LAUNCHER)

    @property
    def execution_provenance_digest(self) -> str:
        return _sha256_file(EXECUTION_PROVENANCE)

    @property
    def schema_digest(self) -> str:
        return declared_variable_schema_digest()

    @property
    def registry_path_identity(self) -> str:
        return _sha256_bytes(str(self.layout.registry.resolve()).encode())

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "contract": REGISTRY_CONTRACT,
            "schema_version": SCHEMA_VERSION,
            "fork_id": self.layout.fork_id,
            "profile_id": self.layout.profile_id,
            "registry_path": str(self.layout.registry.resolve()),
            "snapshot_generation": 0,
            "records": {},
            "lineage": {},
        }

    def _validate_snapshot(self, value: Mapping[str, Any]) -> dict[str, Any]:
        expected = self._empty_snapshot()
        for key in (
            "contract",
            "schema_version",
            "fork_id",
            "profile_id",
            "registry_path",
        ):
            if value.get(key) != expected[key]:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_MISMATCH",
                    f"registry {key} disagrees with this fork",
                )
        generation = value.get("snapshot_generation")
        records = value.get("records")
        lineage = value.get("lineage")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not isinstance(records, dict)
            or not isinstance(lineage, dict)
        ):
            raise DeepSeekIdentityError(
                "HARNESS_REGISTRY_INVALID",
                "registry generation or record maps are invalid",
            )
        return json.loads(json.dumps(value))

    def _read_snapshot_unlocked(self) -> dict[str, Any]:
        return self._validate_snapshot(
            _read_owner_json(
                self.layout.registry, missing_code="HARNESS_REGISTRY_UNAVAILABLE"
            )
        )

    @contextmanager
    def _mutation_lock(self):
        _ensure_owner_dir(self.layout.root)
        descriptor = os.open(
            self.layout.lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        handle = os.fdopen(descriptor, "a+")
        try:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def ensure_registry(self) -> dict[str, Any]:
        with self._mutation_lock():
            try:
                return self._read_snapshot_unlocked()
            except DeepSeekIdentityError as exc:
                if exc.code != "HARNESS_REGISTRY_UNAVAILABLE":
                    raise
            value = self._empty_snapshot()
            _atomic_owner_write(self.layout.registry, _canonical_json(value) + b"\n")
            return value

    def materialize_profile(self) -> dict[str, str]:
        """Write the dedicated profile without invoking a package manager."""
        self.ensure_registry()
        for directory in (
            self.layout.dsh_home,
            self.layout.dsh_home / "profiles",
            self.layout.profile_dir,
            self.layout.credentials,
        ):
            _ensure_owner_dir(directory)
        manifest = {
            "name": f"dsh-profile-{self.layout.profile_id}",
            "private": True,
            "dependencies": {},
            "dsh": {
                "profile": {
                    "bundles": [
                        "@deepseek-ai/dsh-base",
                        "@deepseek-ai/dsh-web-app",
                    ]
                }
            },
        }
        plugin_url = self.plugin_path.as_uri()
        config = {
            "forkId": self.layout.fork_id,
            "profileId": self.layout.profile_id,
            "pluginBundleDigest": self.plugin_digest,
            "declaredVariableSchemaDigest": self.schema_digest,
            "registryPath": str(self.layout.registry.resolve()),
            "registryPathIdentity": self.registry_path_identity,
            "healthPath": str(self.layout.health.resolve()),
            "hostIdentityPath": str(self.layout.host_identity.resolve()),
            "executionLauncherPath": str(EXECUTION_LAUNCHER.resolve(strict=True)),
            "executionLauncherDigest": self.execution_launcher_digest,
            "cgroupRoot": "/sys/fs/cgroup",
            "descriptorFd": 198,
            "descriptorTtlSeconds": 86400,
        }
        patch = (
            "# engine-owned; regenerated before every stock DeepSeek Host boot\n"
            "- insert:\n"
            "  - id: sc-shell-identity\n"
            f"    name: {json.dumps(plugin_url)}\n"
            "    config:\n"
            + "".join(
                f"      {key}: {json.dumps(value)}\n" for key, value in config.items()
            )
        ).encode()
        profile_identity = {
            "contract": PROFILE_CONTRACT,
            "fork_id": self.layout.fork_id,
            "profile_id": self.layout.profile_id,
            "plugin_url": plugin_url,
            "plugin_bundle_digest": self.plugin_digest,
            "declared_variable_schema_digest": self.schema_digest,
            "registry_path": str(self.layout.registry.resolve()),
            "registry_path_identity": self.registry_path_identity,
            "execution_launcher": str(EXECUTION_LAUNCHER.resolve(strict=True)),
            "execution_launcher_digest": self.execution_launcher_digest,
            "execution_provenance_digest": self.execution_provenance_digest,
            "execution_descriptor_fd": 198,
            "cgroup_root": "/sys/fs/cgroup",
        }
        _atomic_owner_write(
            self.layout.profile_dir / "package.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n",
        )
        _atomic_owner_write(self.layout.profile_dir / "cordis.patch.yml", patch)
        _atomic_owner_write(
            self.layout.profile_dir / "pnpm-workspace.yaml",
            b"packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n",
        )
        _atomic_owner_write(self.layout.dsh_home / "cordis.patch.yml", b"[]\n")
        _atomic_owner_write(
            self.layout.profile_identity,
            json.dumps(profile_identity, indent=2, sort_keys=True).encode() + b"\n",
        )
        return {
            "DSH_HOME": str(self.layout.dsh_home),
            "SC_DSH_FORK_ID": self.layout.fork_id,
            "SC_DSH_PROFILE_ID": self.layout.profile_id,
        }

    def host_environment(self) -> dict[str, str]:
        values = self.materialize_profile()
        values["SC_DSH_HOST_BOOT_GENERATION"] = uuid.uuid4().hex
        return values

    def observe_host(
        self,
        *,
        host_boot_generation: str,
        host_pid: int,
        host_start_ticks: int | None = None,
    ) -> dict[str, Any]:
        """Publish the engine-observed Linux Host identity under the registry lock."""
        if not host_boot_generation or not isinstance(host_boot_generation, str):
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_INVALID",
                "Host boot generation is missing",
            )
        if not isinstance(host_pid, int) or isinstance(host_pid, bool) or host_pid <= 1:
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_INVALID",
                "Host PID is invalid",
            )
        observed_ticks = process_start_ticks(host_pid)
        expected_ticks = (
            observed_ticks if host_start_ticks is None else host_start_ticks
        )
        if (
            not isinstance(expected_ticks, int)
            or isinstance(expected_ticks, bool)
            or expected_ticks <= 0
            or observed_ticks != expected_ticks
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
                "Host process identity is not live",
            )
        identity = {
            "contract": HOST_IDENTITY_CONTRACT,
            "fork_id": self.layout.fork_id,
            "profile_id": self.layout.profile_id,
            "host_boot_generation": host_boot_generation,
            "host_pid": host_pid,
            "host_start_ticks": expected_ticks,
            "observed_at": _utc_now(),
        }
        with self._mutation_lock():
            if process_start_ticks(host_pid) != expected_ticks:
                raise DeepSeekIdentityError(
                    "HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
                    "Host process identity changed before publication",
                )
            _atomic_owner_write(
                self.layout.host_identity,
                _canonical_json(identity) + b"\n",
            )
        return identity

    def read_snapshot(self) -> dict[str, Any]:
        return self._read_snapshot_unlocked()

    def read_live_health(
        self, *, expected_host_boot_generation: str | None = None
    ) -> dict[str, Any]:
        health = _read_owner_json(
            self.layout.health, missing_code="HARNESS_PLUGIN_HEALTH_UNAVAILABLE"
        )
        if (
            health.get("contract") != HEALTH_CONTRACT
            or health.get("loaded") is not True
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
                "DeepSeek identity plugin is not loaded",
            )
        identity = _read_owner_json(
            self.layout.host_identity,
            missing_code="HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
        )
        host_boot_generation = (
            expected_host_boot_generation
            if expected_host_boot_generation is not None
            else health.get("host_boot_generation")
        )
        load_generation = health.get("plugin_load_hmr_generation")
        host_pid = health.get("host_pid")
        host_start_ticks = health.get("host_start_ticks")
        if (
            not isinstance(host_boot_generation, str)
            or not isinstance(load_generation, str)
            or not isinstance(host_pid, int)
            or isinstance(host_pid, bool)
            or not isinstance(host_start_ticks, int)
            or isinstance(host_start_ticks, bool)
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_INVALID",
                "plugin health Host identity or generations are malformed",
            )
        expected: dict[str, str] = {
            "canonical_fork_id": self.layout.fork_id,
            "dedicated_profile_id": self.layout.profile_id,
            "plugin_bundle_digest": self.plugin_digest,
            "declared_variable_schema_digest": self.schema_digest,
            "canonical_registry_path_identity": self.registry_path_identity,
            "host_boot_generation": host_boot_generation,
            "plugin_load_hmr_generation": load_generation,
        }
        generation = plugin_contract_generation(expected)
        if (
            health.get("fork_id") != self.layout.fork_id
            or health.get("profile_id") != self.layout.profile_id
            or health.get("registry_path") != str(self.layout.registry.resolve())
            or health.get("host_boot_generation") != host_boot_generation
            or health.get("plugin_contract_generation") != generation
            or identity.get("contract") != HOST_IDENTITY_CONTRACT
            or identity.get("fork_id") != self.layout.fork_id
            or identity.get("profile_id") != self.layout.profile_id
            or identity.get("host_boot_generation") != host_boot_generation
            or identity.get("host_pid") != host_pid
            or identity.get("host_start_ticks") != host_start_ticks
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_MISMATCH",
                "loaded DeepSeek plugin contract disagrees with this fork",
            )
        if (
            host_pid <= 1
            or host_start_ticks <= 0
            or process_start_ticks(host_pid) != host_start_ticks
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
                "loaded DeepSeek Host process is not live",
            )
        return health

    def _expect_snapshot(self, snapshot: Mapping[str, Any], expected: int) -> None:
        if snapshot["snapshot_generation"] != expected:
            raise DeepSeekIdentityError(
                "HARNESS_REGISTRY_STALE_WRITER",
                "registry snapshot changed before commit",
            )

    def _write_credential(
        self,
        *,
        token: str,
        api_base: str,
        shell_id: int,
        shell_shortname: str,
        root_session_id: str,
        conversation_id: str,
        lifecycle_epoch: int,
        record_generation: int,
        contract_generation: str,
        crash_at: str | None,
    ) -> Path:
        if not token:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "binding credential token is empty"
            )
        _ensure_owner_dir(self.layout.credentials)
        path = self.layout.credentials / f"binding-{uuid.uuid4().hex}.json"
        payload = {
            "contract": CREDENTIAL_CONTRACT,
            "token": token,
            "api_base": api_base,
            "shell_id": shell_id,
            "shell_shortname": shell_shortname,
            "root_session_id": root_session_id,
            "conversation_id": conversation_id,
            "lifecycle_epoch": lifecycle_epoch,
            "binding_generation": record_generation,
            "plugin_contract_generation": contract_generation,
        }
        _crash("before_artifact_fsync", crash_at)
        _atomic_owner_write(path, _canonical_json(payload) + b"\n")
        _crash("after_artifact_fsync", crash_at)
        return path

    def _commit(
        self,
        snapshot: dict[str, Any],
        *,
        root_session_id: str,
        operation: str,
        crash_at: str | None,
    ) -> TransactionReceipt:
        snapshot["snapshot_generation"] += 1
        _crash("before_registry_replace", crash_at)
        _atomic_owner_write(self.layout.registry, _canonical_json(snapshot) + b"\n")
        record = snapshot["records"][root_session_id]
        _crash("after_registry_replace", crash_at)
        return TransactionReceipt(
            operation=operation,
            root_session_id=root_session_id,
            snapshot_generation=snapshot["snapshot_generation"],
            record_generation=record["record_generation"],
            lifecycle_epoch=record["lifecycle_epoch"],
            state=record["state"],
        )

    def create_binding(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        conversation_id: str,
        lifecycle_epoch: int,
        shell_id: int,
        shell_shortname: str,
        shell_worktree: Path,
        api_base: str,
        token: str,
        plugin_contract_generation: str,
        crash_at: str | None = None,
    ) -> TransactionReceipt:
        root_session_id = _validate_session(root_session_id, field="root_session_id")
        if not conversation_id or not isinstance(conversation_id, str):
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "engine conversation identity is empty"
            )
        if lifecycle_epoch <= 0 or shell_id <= 0 or not shell_shortname:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID",
                "binding shell or lifecycle identity is invalid",
            )
        worktree = shell_worktree.resolve(strict=True)
        if not worktree.is_dir():
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "binding worktree is not a directory"
            )
        try:
            worktree.relative_to(self.repo_root)
        except ValueError as exc:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "binding worktree belongs to another fork"
            ) from exc
        api_base = _validate_loopback_api(api_base)
        live = self.read_live_health()
        if live["plugin_contract_generation"] != plugin_contract_generation:
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_MISMATCH", "binding used stale plugin health"
            )
        with self._mutation_lock():
            locked_health = self.read_live_health()
            if (
                locked_health["plugin_contract_generation"]
                != plugin_contract_generation
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PLUGIN_HEALTH_MISMATCH",
                    "plugin health changed before binding commit",
                )
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            if (
                root_session_id in snapshot["records"]
                or root_session_id in snapshot["lineage"]
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_REUSE_REFUSED",
                    "DSH session identity was already assigned",
                )
            credential = self._write_credential(
                token=token,
                api_base=api_base,
                shell_id=shell_id,
                shell_shortname=shell_shortname,
                root_session_id=root_session_id,
                conversation_id=conversation_id,
                lifecycle_epoch=lifecycle_epoch,
                record_generation=1,
                contract_generation=plugin_contract_generation,
                crash_at=crash_at,
            )
            now = _utc_now()
            snapshot["records"][root_session_id] = {
                "root_session_id": root_session_id,
                "conversation_id": conversation_id,
                "lifecycle_epoch": lifecycle_epoch,
                "shell_id": shell_id,
                "shell_shortname": shell_shortname,
                "shell_worktree": str(worktree),
                "api_base": api_base,
                "credential_file": str(credential),
                "record_generation": 1,
                "plugin_contract_generation": plugin_contract_generation,
                "state": "active",
                "created_at": now,
                "reopened_at": None,
                "recovered_at": None,
                "closed_at": None,
                "retired_artifacts": [],
                "tombstone_history": [],
            }
            return self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="create",
                crash_at=crash_at,
            )

    def reopen_binding(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        expected_record_generation: int,
        conversation_id: str,
        lifecycle_epoch: int,
        shell_id: int,
        shell_shortname: str,
        shell_worktree: Path,
        api_base: str,
        token: str,
        plugin_contract_generation: str,
        crash_at: str | None = None,
    ) -> TransactionReceipt:
        """Conditionally reopen one terminal tombstone for the same owner."""
        root_session_id = _validate_session(root_session_id, field="root_session_id")
        if not conversation_id or not isinstance(conversation_id, str):
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "engine conversation identity is empty"
            )
        if lifecycle_epoch <= 0 or shell_id <= 0 or not shell_shortname:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID",
                "binding shell or lifecycle identity is invalid",
            )
        worktree = shell_worktree.resolve(strict=True)
        if not worktree.is_dir():
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "binding worktree is not a directory"
            )
        try:
            worktree.relative_to(self.repo_root)
        except ValueError as exc:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_INVALID", "binding worktree belongs to another fork"
            ) from exc
        api_base = _validate_loopback_api(api_base)
        live = self.read_live_health()
        if live["plugin_contract_generation"] != plugin_contract_generation:
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_MISMATCH", "reopen used stale plugin health"
            )
        with self._mutation_lock():
            locked_health = self.read_live_health()
            if (
                locked_health["plugin_contract_generation"]
                != plugin_contract_generation
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PLUGIN_HEALTH_MISMATCH",
                    "plugin health changed before binding reopen",
                )
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            record = snapshot["records"].get(root_session_id)
            if (
                not isinstance(record, dict)
                or record.get("state") != "terminal"
                or root_session_id in snapshot["lineage"]
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_REOPEN_REFUSED",
                    "only an unambiguous terminal root binding may reopen",
                )
            if record.get("record_generation") != expected_record_generation:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_STALE_WRITER",
                    "terminal binding changed before reopen",
                )
            previous_epoch = record.get("lifecycle_epoch")
            same_owner = (
                record.get("root_session_id") == root_session_id
                and record.get("conversation_id") == conversation_id
                and record.get("shell_id") == shell_id
                and record.get("shell_shortname") == shell_shortname
                and record.get("shell_worktree") == str(worktree)
            )
            if (
                not same_owner
                or not isinstance(previous_epoch, int)
                or isinstance(previous_epoch, bool)
                or lifecycle_epoch <= previous_epoch
                or record.get("credential_file") is not None
                or record.get("retired_artifacts") != []
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_REOPEN_REFUSED",
                    "terminal binding owner or lifecycle epoch does not match reopen",
                )
            history = record.get("tombstone_history", [])
            if not isinstance(history, list):
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_INVALID", "tombstone history is malformed"
                )
            next_generation = expected_record_generation + 1
            credential = self._write_credential(
                token=token,
                api_base=api_base,
                shell_id=shell_id,
                shell_shortname=shell_shortname,
                root_session_id=root_session_id,
                conversation_id=conversation_id,
                lifecycle_epoch=lifecycle_epoch,
                record_generation=next_generation,
                contract_generation=plugin_contract_generation,
                crash_at=crash_at,
            )
            history.append(
                {
                    "state": "terminal",
                    "conversation_id": record["conversation_id"],
                    "lifecycle_epoch": previous_epoch,
                    "record_generation": expected_record_generation,
                    "shell_id": record["shell_id"],
                    "shell_shortname": record["shell_shortname"],
                    "shell_worktree": record["shell_worktree"],
                    "api_base": record["api_base"],
                    "plugin_contract_generation": record["plugin_contract_generation"],
                    "created_at": record.get("created_at"),
                    "reopened_at": record.get("reopened_at"),
                    "recovered_at": record.get("recovered_at"),
                    "closed_at": record.get("closed_at"),
                }
            )
            now = _utc_now()
            record.update(
                {
                    "lifecycle_epoch": lifecycle_epoch,
                    "api_base": api_base,
                    "credential_file": str(credential),
                    "record_generation": next_generation,
                    "plugin_contract_generation": plugin_contract_generation,
                    "state": "active",
                    "created_at": now,
                    "reopened_at": now,
                    "recovered_at": None,
                    "closed_at": None,
                    "retired_artifacts": [],
                    "tombstone_history": history,
                }
            )
            return self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="reopen",
                crash_at=crash_at,
            )

    def rotate_binding(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        expected_record_generation: int,
        token: str,
        plugin_contract_generation: str,
        recovery: bool = False,
        crash_at: str | None = None,
    ) -> TransactionReceipt:
        root_session_id = _validate_session(root_session_id, field="root_session_id")
        live = self.read_live_health()
        if live["plugin_contract_generation"] != plugin_contract_generation:
            raise DeepSeekIdentityError(
                "HARNESS_PLUGIN_HEALTH_MISMATCH", "binding used stale plugin health"
            )
        with self._mutation_lock():
            locked_health = self.read_live_health()
            if (
                locked_health["plugin_contract_generation"]
                != plugin_contract_generation
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PLUGIN_HEALTH_MISMATCH",
                    "plugin health changed before binding rotate",
                )
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            record = snapshot["records"].get(root_session_id)
            if not isinstance(record, dict) or record.get("state") not in LIVE_STATES:
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_NOT_LIVE",
                    "binding cannot rotate from its durable state",
                )
            if record.get("record_generation") != expected_record_generation:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_STALE_WRITER",
                    "binding record changed before rotate",
                )
            next_generation = expected_record_generation + 1
            credential = self._write_credential(
                token=token,
                api_base=record["api_base"],
                shell_id=record["shell_id"],
                shell_shortname=record["shell_shortname"],
                root_session_id=root_session_id,
                conversation_id=record["conversation_id"],
                lifecycle_epoch=record["lifecycle_epoch"],
                record_generation=next_generation,
                contract_generation=plugin_contract_generation,
                crash_at=crash_at,
            )
            old = record["credential_file"]
            record["credential_file"] = str(credential)
            record["record_generation"] = next_generation
            record["plugin_contract_generation"] = plugin_contract_generation
            record["retired_artifacts"] = [*record.get("retired_artifacts", []), old]
            if recovery:
                record["recovered_at"] = _utc_now()
                for lineage in snapshot["lineage"].values():
                    if (
                        isinstance(lineage, dict)
                        and lineage.get("root_session_id") == root_session_id
                        and lineage.get("lifecycle_epoch")
                        == record["lifecycle_epoch"]
                        and lineage.get("record_generation")
                        == expected_record_generation
                    ):
                        lineage["record_generation"] = next_generation
            return self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="recover" if recovery else "rotate",
                crash_at=crash_at,
            )

    def begin_close(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        expected_record_generation: int,
        crash_at: str | None = None,
    ) -> TransactionReceipt:
        with self._mutation_lock():
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            record = snapshot["records"].get(root_session_id)
            if not isinstance(record, dict) or record.get("state") != "active":
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_NOT_LIVE", "only an active binding may begin close"
                )
            if record.get("record_generation") != expected_record_generation:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_STALE_WRITER",
                    "binding record changed before close",
                )
            record["record_generation"] += 1
            record["state"] = "closing"
            _crash("after_closing_update", crash_at)
            return self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="closing",
                crash_at=crash_at,
            )

    def begin_close_roots(
        self,
        *,
        roots: Mapping[str, Mapping[str, Any]],
        require_live_root: bool = False,
        crash_at: str | None = None,
    ) -> dict[str, TransactionReceipt]:
        """Fence every present exact proof root in one registry commit."""
        with self._mutation_lock():
            snapshot = self._read_snapshot_unlocked()
            present: dict[str, dict[str, Any]] = {}
            for root_session_id, expected in sorted(roots.items()):
                _validate_session(root_session_id, field="root_session_id")
                record = snapshot["records"].get(root_session_id)
                if record is None:
                    continue
                if (
                    not isinstance(record, dict)
                    or record.get("conversation_id")
                    != expected.get("conversation_id")
                    or record.get("lifecycle_epoch")
                    != expected.get("lifecycle_epoch")
                    or record.get("state") not in {"active", "closing", "terminal"}
                ):
                    raise DeepSeekIdentityError(
                        "HARNESS_PROOF_BINDING_MISMATCH",
                        "proof teardown found a mismatched root lifecycle",
                )
                present[root_session_id] = record
            if require_live_root and not any(
                record["state"] in {"active", "closing"}
                for record in present.values()
            ):
                return {}
            changed = False
            for record in present.values():
                if record["state"] == "active":
                    record["record_generation"] += 1
                    record["state"] = "closing"
                    changed = True
            if changed:
                _crash("after_closing_update", crash_at)
                snapshot["snapshot_generation"] += 1
                _crash("before_registry_replace", crash_at)
                _atomic_owner_write(
                    self.layout.registry, _canonical_json(snapshot) + b"\n"
                )
                _crash("after_registry_replace", crash_at)
            generation = snapshot["snapshot_generation"]
            return {
                root_session_id: TransactionReceipt(
                    operation=(
                        "terminal"
                        if record["state"] == "terminal"
                        else "closing"
                    ),
                    root_session_id=root_session_id,
                    snapshot_generation=generation,
                    record_generation=record["record_generation"],
                    lifecycle_epoch=record["lifecycle_epoch"],
                    state=record["state"],
                )
                for root_session_id, record in present.items()
            }

    def retire_binding(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        expected_record_generation: int,
        quiesced: bool,
        crash_at: str | None = None,
    ) -> TransactionReceipt:
        if not quiesced:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_QUIESCENCE_UNKNOWN",
                "closing binding remains authoritative until quiescence is proven",
            )
        cleanup: list[str] = []
        with self._mutation_lock():
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            record = snapshot["records"].get(root_session_id)
            if not isinstance(record, dict) or record.get("state") != "closing":
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_NOT_CLOSING",
                    "binding is not awaiting terminal close",
                )
            if record.get("record_generation") != expected_record_generation:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_STALE_WRITER",
                    "binding record changed before retire",
                )
            cleanup = [record["credential_file"], *record.get("retired_artifacts", [])]
            record["record_generation"] += 1
            record["state"] = "terminal"
            record["closed_at"] = _utc_now()
            record["credential_file"] = None
            record["retired_artifacts"] = []
            receipt = self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="terminal",
                crash_at=crash_at,
            )
        _crash("before_artifact_cleanup", crash_at)
        for raw in cleanup:
            try:
                Path(raw).unlink()
            except FileNotFoundError:
                pass
        _fsync_dir(self.layout.credentials)
        _crash("after_artifact_cleanup", crash_at)
        return receipt

    def register_lineage(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        child_session_id: str,
        expected_record_generation: int,
    ) -> TransactionReceipt:
        _validate_session(child_session_id, field="child_session_id")
        if child_session_id == root_session_id:
            raise DeepSeekIdentityError(
                "HARNESS_LINEAGE_INVALID", "a root binding cannot be its own child"
            )
        with self._mutation_lock():
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            record = snapshot["records"].get(root_session_id)
            if not isinstance(record, dict) or record.get("state") != "active":
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_NOT_LIVE", "lineage root is not active"
                )
            if record.get("record_generation") != expected_record_generation:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_STALE_WRITER",
                    "lineage root changed before commit",
                )
            if (
                child_session_id in snapshot["records"]
                or child_session_id in snapshot["lineage"]
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_LINEAGE_INVALID",
                    "child session identity was already assigned",
                )
            snapshot["lineage"][child_session_id] = {
                "root_session_id": root_session_id,
                "lifecycle_epoch": record["lifecycle_epoch"],
                "record_generation": record["record_generation"],
            }
            return self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="lineage",
                crash_at=None,
            )

    def resolve_record(self, session_id: str) -> dict[str, Any]:
        session_id = _validate_session(session_id, field="session_id")
        snapshot = self.read_snapshot()
        root_session_id = session_id
        lineage = snapshot["lineage"].get(session_id)
        if lineage is not None:
            if not isinstance(lineage, dict):
                raise DeepSeekIdentityError(
                    "HARNESS_LINEAGE_INVALID", "lineage record is malformed"
                )
            raw_root_session_id = lineage.get("root_session_id")
            if not isinstance(raw_root_session_id, str):
                raise DeepSeekIdentityError(
                    "HARNESS_LINEAGE_INVALID", "lineage root identity is malformed"
                )
            root_session_id = _validate_session(
                raw_root_session_id, field="root_session_id"
            )
        record = snapshot["records"].get(root_session_id)
        if not isinstance(record, dict) or record.get("state") != "active":
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_NOT_LIVE", "session has no active root binding"
            )
        if lineage is not None and (
            lineage.get("lifecycle_epoch") != record.get("lifecycle_epoch")
            or lineage.get("record_generation") != record.get("record_generation")
        ):
            raise DeepSeekIdentityError(
                "HARNESS_LINEAGE_STALE",
                "child lineage no longer names the current root",
            )
        return record

    def binding_current(
        self,
        *,
        root_session_id: str,
        conversation_id: str,
        lifecycle_epoch: int,
        shell_id: int,
        shell_shortname: str,
        shell_worktree: Path,
        api_base: str,
        token: str,
        plugin_contract_generation: str,
    ) -> bool:
        """Prove one active root and credential are the exact admission identity."""
        try:
            record = self.resolve_record(root_session_id)
            credential = _read_owner_json(
                Path(record["credential_file"]),
                missing_code="HARNESS_BINDING_CREDENTIAL_UNAVAILABLE",
            )
            worktree = shell_worktree.resolve(strict=True)
            normalized_api = _validate_loopback_api(api_base)
        except (DeepSeekIdentityError, KeyError, OSError, TypeError):
            return False
        expected_record = {
            "root_session_id": root_session_id,
            "conversation_id": conversation_id,
            "lifecycle_epoch": lifecycle_epoch,
            "shell_id": shell_id,
            "shell_shortname": shell_shortname,
            "shell_worktree": str(worktree),
            "api_base": normalized_api,
            "plugin_contract_generation": plugin_contract_generation,
            "state": "active",
        }
        if any(record.get(key) != value for key, value in expected_record.items()):
            return False
        expected_credential = {
            "contract": CREDENTIAL_CONTRACT,
            "api_base": normalized_api,
            "shell_id": shell_id,
            "shell_shortname": shell_shortname,
            "root_session_id": root_session_id,
            "conversation_id": conversation_id,
            "lifecycle_epoch": lifecycle_epoch,
            "binding_generation": record.get("record_generation"),
            "plugin_contract_generation": plugin_contract_generation,
        }
        if any(
            credential.get(key) != value
            for key, value in expected_credential.items()
        ):
            return False
        raw_token = credential.get("token")
        return isinstance(raw_token, str) and hmac.compare_digest(raw_token, token)

    def recover_artifacts(self) -> dict[str, int]:
        """Idempotently remove only artifacts absent from the committed snapshot."""
        with self._mutation_lock():
            snapshot = self._read_snapshot_unlocked()
            referenced = {
                raw
                for record in snapshot["records"].values()
                if isinstance(record, dict)
                for raw in [
                    record.get("credential_file"),
                    *record.get("retired_artifacts", []),
                ]
                if isinstance(raw, str)
            }
            removed = 0
            _ensure_owner_dir(self.layout.credentials)
            for candidate in self.layout.credentials.glob("binding-*.json"):
                if str(candidate) in referenced:
                    continue
                candidate.unlink()
                removed += 1
            _fsync_dir(self.layout.credentials)
            return {
                "snapshot_generation": snapshot["snapshot_generation"],
                "removed_orphans": removed,
            }

    def cleanup_retired_artifacts(
        self,
        *,
        expected_snapshot_generation: int,
        root_session_id: str,
        expected_record_generation: int,
        quiesced: bool,
    ) -> TransactionReceipt:
        if not quiesced:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_QUIESCENCE_UNKNOWN",
                "retired credentials remain until quiescence",
            )
        cleanup: list[str]
        with self._mutation_lock():
            snapshot = self._read_snapshot_unlocked()
            self._expect_snapshot(snapshot, expected_snapshot_generation)
            record = snapshot["records"].get(root_session_id)
            if not isinstance(record, dict) or record.get("state") not in LIVE_STATES:
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_NOT_LIVE",
                    "binding cannot clean retired credentials",
                )
            if record.get("record_generation") != expected_record_generation:
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_STALE_WRITER", "binding changed before cleanup"
                )
            current_credential = _read_owner_json(
                Path(record["credential_file"]),
                missing_code="HARNESS_BINDING_CREDENTIAL_UNAVAILABLE",
            )
            if (
                current_credential.get("contract") != CREDENTIAL_CONTRACT
                or current_credential.get("binding_generation")
                != expected_record_generation
                or current_credential.get("root_session_id") != root_session_id
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_CREDENTIAL_MISMATCH",
                    "current binding credential disagrees with the registry",
                )
            next_generation = expected_record_generation + 1
            next_credential = self._write_credential(
                token=current_credential["token"],
                api_base=record["api_base"],
                shell_id=record["shell_id"],
                shell_shortname=record["shell_shortname"],
                root_session_id=root_session_id,
                conversation_id=record["conversation_id"],
                lifecycle_epoch=record["lifecycle_epoch"],
                record_generation=next_generation,
                contract_generation=record["plugin_contract_generation"],
                crash_at=None,
            )
            cleanup = [
                record["credential_file"],
                *record.get("retired_artifacts", []),
            ]
            record["credential_file"] = str(next_credential)
            record["record_generation"] = next_generation
            record["retired_artifacts"] = []
            receipt = self._commit(
                snapshot,
                root_session_id=root_session_id,
                operation="cleanup",
                crash_at=None,
            )
        for raw in cleanup:
            try:
                Path(raw).unlink()
            except FileNotFoundError:
                pass
        _fsync_dir(self.layout.credentials)
        return receipt

    def diagnostics(self) -> dict[str, Any]:
        snapshot = self.read_snapshot()
        states: dict[str, int] = {}
        for record in snapshot["records"].values():
            raw_state = record.get("state") if isinstance(record, dict) else None
            state = raw_state if isinstance(raw_state, str) else "invalid"
            states[state] = states.get(state, 0) + 1
        health_generation = None
        health_status = "unavailable"
        try:
            health = self.read_live_health()
            health_generation = health["plugin_contract_generation"]
            health_status = "loaded"
        except DeepSeekIdentityError:
            pass
        return {
            "contract": REGISTRY_CONTRACT,
            "fork_id": self.layout.fork_id,
            "profile_id": self.layout.profile_id,
            "registry_snapshot_generation": snapshot["snapshot_generation"],
            "binding_states": dict(sorted(states.items())),
            "lineage_count": len(snapshot["lineage"]),
            "plugin_health": health_status,
            "plugin_contract_generation": health_generation,
        }
