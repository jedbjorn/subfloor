#!/usr/bin/env python3
"""Resolve the proposed DSH execution boundary without trusting environment.

This module freezes the Linux contributor used by the DSH preparation
contract.  It is deliberately not wired into ``sc`` until the pre-effect
policy unit lands.  The resolver reads only kernel cgroup evidence and a
sealed descriptor inherited from the engine-owned launcher.  Mutable DSH or
SC environment values participate only after provenance has been resolved.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

CONTRACT = "sc-dsh-linux-cgroup-v2-v2"
PRODUCTION_CONTRACT = "sc-dsh-linux-cgroup-v2-v3"
ISSUER_CONTRACT = "sc-dsh-prototype-issuer-key-v1"
ISSUER_ALGORITHM = "rsa-pkcs1v15-sha256"
IDENTITY_CONTRACT = "sc-dsh-prototype-current-identity-v1"
CREDENTIAL_CONTRACT = "sc-dsh-prototype-credential-v1"
PRODUCTION_CREDENTIAL_CONTRACT = "sc-dsh-binding-credential-v1"
DOMAIN_NAME = re.compile(r"[a-f0-9]{32}\.scope")
DESCRIPTOR_FD = 198
SHORTNAME = re.compile(r"[A-Z][A-Z0-9_-]{1,31}")
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
# Linux UAPI values from <linux/fcntl.h>. Some supported Python builds omit
# these names even though their kernels implement memfd sealing.
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
REQUIRED_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008
BRIDGE_NAMES = {"DSH_SHELL"}
EARLY_OVERRIDE_NAMES = {"SC_DISPATCH", "SC_CALLER_ROOT", "SC_MEM_AS"}
AUTHORIZED_CLASS = "dsh_shell_authorized"
NEUTRAL_CLASS = "identity_neutral_read_only"
REFUSED_CLASS = "refused"
ALIASES = (
    "DSH_SC_SHELL_ID",
    "DSH_SC_SHELL_SHORTNAME",
    "DSH_SC_SHELL_WORKTREE",
    "DSH_SC_API_BASE",
    "DSH_SC_MEM_CREDENTIAL_FILE",
    "DSH_SC_BINDING_GENERATION",
    "DSH_SC_PLUGIN_HEALTH_GENERATION",
)
LITERAL_ROUTE_FILES = {
    "job": ".super-coder/scripts/job.py",
    "mem": ".super-coder/scripts/mem.py",
    "pr": ".super-coder/scripts/pr_cli.py",
    "sprint": ".super-coder/scripts/sprint_cli.py",
    "visual-qa": ".super-coder/scripts/visual_qa.py",
    "vm": ".super-coder/scripts/vm.py",
}
CUSTOM_ROUTE_POLICIES = {
    "feature": "feature",
    "models": "models",
    "skill": "skill",
    "map-extractor": "map-extractor",
    "vm-mcp-relay": "vm-mcp-relay",
}


@dataclass(frozen=True)
class Resolution:
    provenance: str
    reason: str
    context: ExecutionDomainContext | None = None


@dataclass(frozen=True)
class ExecutionDomainContext:
    cgroup: str
    domain_id: str
    fork_id: str
    profile_id: str
    registry_snapshot_generation: int
    execution_session_id: str
    root_session_id: str
    conversation_id: str
    lifecycle_epoch: int
    shell_id: int
    shell_shortname: str
    shell_worktree: str
    api_base: str
    credential_file: str
    binding_record_generation: int
    plugin_contract_generation: str
    lineage_record_generation: int | None


@dataclass(frozen=True)
class IdentityContext:
    shell_id: int
    shell_shortname: str
    shell_worktree: str
    api_base: str
    credential_file: str
    binding_generation: int
    plugin_health_generation: str
    token: str


@dataclass
class StableJson:
    path: Path
    fd: int
    fingerprint: tuple[int, ...]
    data: dict[str, object]

    def assert_stable(self) -> None:
        descriptor = _fingerprint(os.fstat(self.fd))
        visible = _fingerprint(self.path.lstat())
        if descriptor != self.fingerprint or visible != self.fingerprint:
            raise ValueError(f"{self.path.name} changed during identity validation")


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


@contextmanager
def _owner_json(path: Path, label: str):
    try:
        visible = path.lstat()
        if stat.S_ISLNK(visible.st_mode) or not stat.S_ISREG(visible.st_mode):
            raise ValueError(f"{label} is not a nonsymlink regular file")
        if visible.st_uid != os.geteuid() or visible.st_mode & 0o077:
            raise ValueError(f"{label} is not owner-only")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if _fingerprint(opened) != _fingerprint(visible):
            raise ValueError(f"{label} changed before read")
        payload = os.pread(fd, 65_537, 0)
        if not payload or len(payload) > 65_536:
            raise ValueError(f"{label} has an invalid size")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise TypeError(f"{label} is not a JSON object")
        holder = StableJson(path, fd, _fingerprint(opened), data)
        yield holder
        holder.assert_stable()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}") from exc
    finally:
        if "fd" in locals():
            os.close(fd)


def _unified_membership(text: str) -> str:
    rows = []
    for raw in text.splitlines():
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            rows.append(parts[2])
    if len(rows) != 1 or not rows[0].startswith("/"):
        raise ValueError("missing or ambiguous unified cgroup-v2 membership")
    return rows[0]


def _sealed_descriptor(fd: int) -> dict[str, object]:
    seals = fcntl.fcntl(fd, F_GET_SEALS)
    if seals & REQUIRED_SEALS != REQUIRED_SEALS:
        raise ValueError("execution descriptor is not immutably sealed")
    payload = os.pread(fd, 65_537, 0)
    if not payload or len(payload) > 65_536:
        raise ValueError("execution descriptor has an invalid size")
    data = json.loads(payload)
    prototype = {
        "contract",
        "cgroup",
        "domain_id",
        "binding_generation",
        "expires_monotonic_ns",
        "non_delegated",
        "issuer_key_id",
        "root_pid",
        "root_start_ticks",
        "cgroup_device",
        "cgroup_inode",
        "cgroup_owner_uid",
        "signature",
    }
    production = {
        "contract",
        "cgroup",
        "domain_id",
        "descriptor_device",
        "descriptor_inode",
        "expires_monotonic_ns",
        "non_delegated",
        "cgroup_device",
        "cgroup_inode",
        "cgroup_owner_uid",
        "issuer_pid",
        "issuer_start_ticks",
        "host_pid",
        "host_start_ticks",
        "root_pid",
        "root_start_ticks",
        "fork_id",
        "profile_id",
        "registry_snapshot_generation",
        "execution_session_id",
        "root_session_id",
        "conversation_id",
        "lifecycle_epoch",
        "shell_id",
        "shell_shortname",
        "shell_worktree",
        "api_base",
        "credential_file",
        "binding_record_generation",
        "plugin_contract_generation",
        "lineage_record_generation",
    }
    expected = production if data.get("contract") == PRODUCTION_CONTRACT else prototype
    if set(data) != expected:
        raise ValueError("execution descriptor has an unknown schema")
    opened = os.fstat(fd)
    if data.get("contract") == PRODUCTION_CONTRACT and (
        data["descriptor_device"] != opened.st_dev
        or data["descriptor_inode"] != opened.st_ino
    ):
        raise ValueError("execution descriptor file identity mismatches")
    return data


def _descriptor_payload(descriptor: Mapping[str, object]) -> bytes:
    unsigned = {name: value for name, value in descriptor.items() if name != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()


def _verify_issuer_signature(
    descriptor: Mapping[str, object],
    issuer_key: Path,
) -> None:
    with _owner_json(issuer_key, "prototype issuer key") as key:
        expected = {
            "contract",
            "key_id",
            "algorithm",
            "modulus_hex",
            "public_exponent",
        }
        if set(key.data) != expected:
            raise ValueError("prototype issuer key has an unknown schema")
        if key.data["contract"] != ISSUER_CONTRACT:
            raise ValueError("prototype issuer key contract is stale")
        if key.data["algorithm"] != ISSUER_ALGORITHM:
            raise ValueError("prototype issuer algorithm is unsupported")
        if descriptor["issuer_key_id"] != key.data["key_id"]:
            raise ValueError("execution descriptor names another issuer")
        try:
            modulus = int(str(key.data["modulus_hex"]), 16)
            exponent = int(key.data["public_exponent"])
            signature = bytes.fromhex(str(descriptor["signature"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("prototype issuer key or signature is malformed") from exc
        width = (modulus.bit_length() + 7) // 8
        if width < 256 or exponent != 65_537 or len(signature) != width:
            raise ValueError("prototype issuer key or signature is malformed")
        digest_info = SHA256_DIGEST_INFO + hashlib.sha256(
            _descriptor_payload(descriptor)
        ).digest()
        expected_signature = (
            b"\x00\x01"
            + b"\xff" * (width - len(digest_info) - 3)
            + b"\x00"
            + digest_info
        )
        recovered = pow(
            int.from_bytes(signature, "big"), exponent, modulus
        ).to_bytes(width, "big")
        if not hmac.compare_digest(recovered, expected_signature):
            raise ValueError("execution descriptor issuer signature is invalid")


def _process_identity(process_root: Path, pid: int) -> tuple[int, int]:
    text = (process_root / str(pid) / "stat").read_text()
    close = text.rfind(")")
    if close < 0:
        raise ValueError("process identity is malformed")
    fields = text[close + 2:].split()
    if len(fields) < 20:
        raise ValueError("process identity is malformed")
    return int(fields[1]), int(fields[19])


def _require_root_lineage(
    descriptor: Mapping[str, object],
    process_root: Path,
) -> None:
    root_pid = descriptor["root_pid"]
    root_ticks = descriptor["root_start_ticks"]
    if not isinstance(root_pid, int) or root_pid < 1:
        raise ValueError("execution root pid is malformed")
    if not isinstance(root_ticks, int) or root_ticks < 1:
        raise ValueError("execution root start time is malformed")
    current = os.getpid()
    seen = set()
    for _ in range(256):
        if current in seen or current < 1:
            break
        seen.add(current)
        parent, start_ticks = _process_identity(process_root, current)
        if current == root_pid:
            if start_ticks != root_ticks:
                raise ValueError("execution root was reused")
            return
        current = parent
    raise ValueError("current process is outside the issued execution lineage")


def _verify_live_issuer(
    descriptor: Mapping[str, object],
    *,
    descriptor_fd: int,
    issuer_identity: Path,
    process_root: Path,
) -> None:
    with _owner_json(issuer_identity, "execution issuer identity") as identity:
        expected = {
            "contract",
            "fork_id",
            "profile_id",
            "host_boot_generation",
            "host_pid",
            "host_start_ticks",
            "observed_at",
        }
        if set(identity.data) != expected:
            raise ValueError("execution issuer identity has an unknown schema")
        if identity.data["contract"] != "sc-dsh-host-identity-v1":
            raise ValueError("execution issuer identity contract is stale")
        for name in ("fork_id", "profile_id", "host_pid", "host_start_ticks"):
            if descriptor[name] != identity.data[name]:
                raise ValueError("execution descriptor names another Host issuer")

    issuer_pid = descriptor["issuer_pid"]
    issuer_ticks = descriptor["issuer_start_ticks"]
    host_pid = descriptor["host_pid"]
    host_ticks = descriptor["host_start_ticks"]
    root_pid = descriptor["root_pid"]
    root_ticks = descriptor["root_start_ticks"]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in (
            issuer_pid,
            issuer_ticks,
            host_pid,
            host_ticks,
            root_pid,
            root_ticks,
        )
    ):
        raise ValueError("execution issuer lineage is malformed")
    issuer_parent, observed_issuer_ticks = _process_identity(process_root, issuer_pid)
    _host_parent, observed_host_ticks = _process_identity(process_root, host_pid)
    root_parent, observed_root_ticks = _process_identity(process_root, root_pid)
    if (
        issuer_parent != host_pid
        or observed_issuer_ticks != issuer_ticks
        or observed_host_ticks != host_ticks
        or root_parent != issuer_pid
        or observed_root_ticks != root_ticks
    ):
        raise ValueError("execution issuer lineage is not live")
    inherited = (process_root / str(issuer_pid) / "fd" / str(DESCRIPTOR_FD)).stat()
    opened = os.fstat(descriptor_fd)
    if (inherited.st_dev, inherited.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError("execution issuer does not hold this descriptor")


def _managed_resolution(
    membership: str,
    *,
    cgroup_root: Path,
    descriptor_fd: int | None,
    issuer_key: Path | None,
    issuer_identity: Path | None,
    process_root: Path,
    now_monotonic_ns: int,
) -> Resolution:
    if descriptor_fd is None:
        return Resolution("unknown", "managed membership has no descriptor")
    try:
        descriptor = _sealed_descriptor(descriptor_fd)
        if descriptor["contract"] == PRODUCTION_CONTRACT:
            if issuer_identity is None:
                credential_file = descriptor.get("credential_file")
                if not isinstance(credential_file, str):
                    raise ValueError("managed membership has no live production issuer")
                credential_path = Path(credential_file)
                if not credential_path.is_absolute():
                    raise ValueError("managed membership has no live production issuer")
                issuer_identity = (
                    credential_path.parent.parent / "host-identity.json"
                )
            _verify_live_issuer(
                descriptor,
                descriptor_fd=descriptor_fd,
                issuer_identity=issuer_identity,
                process_root=process_root,
            )
        else:
            if issuer_key is None:
                raise ValueError("managed membership has no trusted prototype issuer")
            _verify_issuer_signature(descriptor, issuer_key)
        name = Path(membership).name
        if not DOMAIN_NAME.fullmatch(name):
            raise ValueError("managed cgroup name is malformed")
        if descriptor["contract"] not in {CONTRACT, PRODUCTION_CONTRACT}:
            raise ValueError("execution descriptor contract is stale")
        if descriptor["cgroup"] != membership:
            raise ValueError("execution descriptor names another cgroup")
        if descriptor["domain_id"] != name.removesuffix(".scope"):
            raise ValueError("execution descriptor domain identity mismatches")
        if descriptor["non_delegated"] is not True:
            raise ValueError("execution domain is delegated")
        binding_generation = descriptor.get(
            "binding_record_generation", descriptor.get("binding_generation")
        )
        if not isinstance(binding_generation, int):
            raise TypeError("binding generation is malformed")
        if binding_generation < 1:
            raise ValueError("binding generation is malformed")
        expires = descriptor["expires_monotonic_ns"]
        if not isinstance(expires, int) or expires <= now_monotonic_ns:
            raise ValueError("execution descriptor is stale")

        root = cgroup_root.resolve(strict=True)
        domain = (root / membership.lstrip("/")).resolve(strict=True)
        if not domain.is_relative_to(root):
            raise ValueError("managed cgroup escapes the cgroup-v2 mount")
        mode = domain.stat().st_mode
        metadata = domain.stat()
        if not stat.S_ISDIR(mode) or mode & 0o222:
            raise ValueError("managed cgroup admission is writable")
        if (
            descriptor["cgroup_device"] != metadata.st_dev
            or descriptor["cgroup_inode"] != metadata.st_ino
            or descriptor["cgroup_owner_uid"] != metadata.st_uid
        ):
            raise ValueError("execution descriptor names another cgroup identity")
        if (domain / "cgroup.type").read_text().strip() != "domain":
            raise ValueError("managed cgroup is not a cgroup-v2 domain")
        if (domain / "cgroup.subtree_control").read_text().strip():
            raise ValueError("managed execution domain delegates controllers")
        procs = (domain / "cgroup.procs").stat()
        if not stat.S_ISREG(procs.st_mode) or procs.st_mode & 0o222:
            raise ValueError("managed cgroup admission is not protected")
        _require_root_lineage(descriptor, process_root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return Resolution("unknown", str(exc))
    context = None
    if descriptor["contract"] == PRODUCTION_CONTRACT:
        try:
            context = ExecutionDomainContext(
                cgroup=str(descriptor["cgroup"]),
                domain_id=str(descriptor["domain_id"]),
                fork_id=str(descriptor["fork_id"]),
                profile_id=str(descriptor["profile_id"]),
                registry_snapshot_generation=int(
                    descriptor["registry_snapshot_generation"]
                ),
                execution_session_id=str(descriptor["execution_session_id"]),
                root_session_id=str(descriptor["root_session_id"]),
                conversation_id=str(descriptor["conversation_id"]),
                lifecycle_epoch=int(descriptor["lifecycle_epoch"]),
                shell_id=int(descriptor["shell_id"]),
                shell_shortname=str(descriptor["shell_shortname"]),
                shell_worktree=str(descriptor["shell_worktree"]),
                api_base=str(descriptor["api_base"]),
                credential_file=str(descriptor["credential_file"]),
                binding_record_generation=int(
                    descriptor["binding_record_generation"]
                ),
                plugin_contract_generation=str(
                    descriptor["plugin_contract_generation"]
                ),
                lineage_record_generation=(
                    int(descriptor["lineage_record_generation"])
                    if descriptor["lineage_record_generation"] is not None
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            return Resolution("unknown", f"execution identity is malformed: {exc}")
    return Resolution(
        "managed", "sealed non-delegated cgroup-v2 membership", context
    )


def resolve_linux(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    descriptor_fd: int | None = None,
    issuer_key: Path | None = None,
    issuer_identity: Path | None = None,
    process_root: Path = Path("/proc"),
    now_monotonic_ns: int | None = None,
) -> Resolution:
    """Resolve native, managed, or unknown from OS evidence only."""
    try:
        membership = _unified_membership(proc_cgroup.read_text())
    except (OSError, ValueError) as exc:
        return Resolution("unknown", str(exc))
    if "/sc-dsh/" not in membership:
        if descriptor_fd is not None:
            return Resolution("unknown", "descriptor is outside its named cgroup")
        return Resolution("native", "no managed cgroup membership")
    return _managed_resolution(
        membership,
        cgroup_root=cgroup_root,
        descriptor_fd=descriptor_fd,
        issuer_key=issuer_key,
        issuer_identity=issuer_identity,
        process_root=process_root,
        now_monotonic_ns=(
            time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        ),
    )


def classify(
    resolution: Resolution,
    *,
    environment: Mapping[str, str],
    command_class: str,
    aliases: Sequence[str],
) -> str:
    """Classify only after provenance; never inspect selector targets here."""
    bridge_present = {
        name for name in environment
        if name in BRIDGE_NAMES or name.startswith("DSH_SC_")
    }
    if resolution.provenance == "unknown":
        return "refused"
    if resolution.provenance == "native":
        return "refused" if bridge_present else "native"
    if any(name.startswith("SC_") for name in environment):
        return "refused"
    if command_class == NEUTRAL_CLASS:
        return "neutral"
    if command_class != AUTHORIZED_CLASS:
        return "refused"
    if any(not environment.get(name) for name in aliases):
        return "refused"
    return "authorized"


def _loopback_api_base(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.port is None
    ):
        raise ValueError("API base is malformed")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("API base is not a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("API base is not loopback")
    return value.rstrip("/")


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is malformed") from exc
    if parsed < 1 or str(parsed) != str(value):
        raise ValueError(f"{label} is malformed")
    return parsed


def resolve_identity(
    *,
    environment: Mapping[str, str],
    aliases: Sequence[str],
    identity_record: Path,
) -> IdentityContext:
    expected_aliases = set(aliases)
    presented = {name for name in environment if name.startswith("DSH_SC_")}
    if presented != expected_aliases:
        raise ValueError("DSH alias set is partial or unknown")
    values = {name: environment.get(name, "") for name in aliases}
    if any(not value for value in values.values()):
        raise ValueError("DSH alias set is incomplete")

    shell_id = _positive_int(values["DSH_SC_SHELL_ID"], "shell id")
    shortname = values["DSH_SC_SHELL_SHORTNAME"]
    if not SHORTNAME.fullmatch(shortname):
        raise ValueError("shell shortname is malformed")
    worktree = Path(values["DSH_SC_SHELL_WORKTREE"])
    if not worktree.is_absolute() or not worktree.is_dir():
        raise ValueError("shell worktree is not an absolute directory")
    api_base = _loopback_api_base(values["DSH_SC_API_BASE"])
    binding_generation = _positive_int(
        values["DSH_SC_BINDING_GENERATION"], "binding generation"
    )
    plugin_generation = values["DSH_SC_PLUGIN_HEALTH_GENERATION"]
    credential_path = Path(values["DSH_SC_MEM_CREDENTIAL_FILE"])
    if not credential_path.is_absolute():
        raise ValueError("credential path is not absolute")

    with (
        _owner_json(identity_record, "current identity record") as record,
        _owner_json(credential_path, "credential artifact") as credential,
    ):
        expected_record = {
            "contract",
            "current",
            "shell_id",
            "shell_shortname",
            "shell_worktree",
            "api_base",
            "credential_file",
            "binding_generation",
            "plugin_health_generation",
        }
        if set(record.data) != expected_record:
            raise ValueError("current identity record has an unknown schema")
        if record.data["contract"] != IDENTITY_CONTRACT or record.data["current"] is not True:
            raise ValueError("current identity record is stale")
        expected_credential = {
            "contract",
            "token",
            "api_base",
            "shell_id",
            "shell_shortname",
            "binding_generation",
            "plugin_health_generation",
        }
        if set(credential.data) != expected_credential:
            raise ValueError("credential artifact has an unknown schema")
        if credential.data["contract"] != CREDENTIAL_CONTRACT:
            raise ValueError("credential artifact contract is stale")
        expected_values = {
            "shell_id": shell_id,
            "shell_shortname": shortname,
            "shell_worktree": str(worktree),
            "api_base": api_base,
            "credential_file": str(credential_path),
            "binding_generation": binding_generation,
            "plugin_health_generation": plugin_generation,
        }
        if any(record.data[name] != value for name, value in expected_values.items()):
            raise ValueError("current identity record mismatches aliases")
        for name in (
            "shell_id",
            "shell_shortname",
            "api_base",
            "binding_generation",
            "plugin_health_generation",
        ):
            if credential.data[name] != expected_values[name]:
                raise ValueError("credential artifact mismatches current identity")
        token = credential.data["token"]
        if not isinstance(token, str) or not token:
            raise ValueError("credential token is malformed")

        request = urllib.request.Request(
            f"{api_base}/_sc/mem/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status != 200:
                    raise ValueError("authenticated whoami refused")
                payload = response.read(65_537)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError("authenticated whoami is unavailable") from exc
        if len(payload) > 65_536:
            raise ValueError("authenticated whoami response is oversized")
        try:
            whoami = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("authenticated whoami response is malformed") from exc
        if whoami != {"shell_id": shell_id, "shell_shortname": shortname}:
            raise ValueError("authenticated whoami identity mismatches")
        record.assert_stable()
        credential.assert_stable()

    return IdentityContext(
        shell_id=shell_id,
        shell_shortname=shortname,
        shell_worktree=str(worktree),
        api_base=api_base,
        credential_file=str(credential_path),
        binding_generation=binding_generation,
        plugin_health_generation=plugin_generation,
        token=token,
    )


def resolve_execution_identity(
    *,
    context: ExecutionDomainContext,
    environment: Mapping[str, str],
) -> IdentityContext:
    """Validate the production descriptor's one immutable shell identity."""
    presented = {name for name in environment if name.startswith("DSH_SC_")}
    if presented != set(ALIASES):
        raise ValueError("DSH alias set is partial or unknown")
    expected_aliases = {
        "DSH_SC_SHELL_ID": str(context.shell_id),
        "DSH_SC_SHELL_SHORTNAME": context.shell_shortname,
        "DSH_SC_SHELL_WORKTREE": context.shell_worktree,
        "DSH_SC_API_BASE": context.api_base,
        "DSH_SC_MEM_CREDENTIAL_FILE": context.credential_file,
        "DSH_SC_BINDING_GENERATION": str(context.binding_record_generation),
        "DSH_SC_PLUGIN_HEALTH_GENERATION": context.plugin_contract_generation,
    }
    if any(environment.get(name) != value for name, value in expected_aliases.items()):
        raise ValueError("DSH aliases disagree with the execution descriptor")

    if context.shell_id < 1 or SHORTNAME.fullmatch(context.shell_shortname) is None:
        raise ValueError("execution shell identity is malformed")
    worktree = Path(context.shell_worktree)
    if not worktree.is_absolute() or not worktree.is_dir():
        raise ValueError("execution shell worktree is unavailable")
    api_base = _loopback_api_base(context.api_base)
    credential_path = Path(context.credential_file)
    if not credential_path.is_absolute():
        raise ValueError("credential path is not absolute")

    with _owner_json(credential_path, "credential artifact") as credential:
        expected_fields = {
            "contract",
            "token",
            "api_base",
            "shell_id",
            "shell_shortname",
            "root_session_id",
            "conversation_id",
            "lifecycle_epoch",
            "binding_generation",
            "plugin_contract_generation",
        }
        if set(credential.data) != expected_fields:
            raise ValueError("credential artifact has an unknown schema")
        if credential.data["contract"] != PRODUCTION_CREDENTIAL_CONTRACT:
            raise ValueError("credential artifact contract is stale")
        expected_values = {
            "api_base": api_base,
            "shell_id": context.shell_id,
            "shell_shortname": context.shell_shortname,
            "root_session_id": context.root_session_id,
            "conversation_id": context.conversation_id,
            "lifecycle_epoch": context.lifecycle_epoch,
            "binding_generation": context.binding_record_generation,
            "plugin_contract_generation": context.plugin_contract_generation,
        }
        if any(credential.data[name] != value for name, value in expected_values.items()):
            raise ValueError("credential artifact mismatches execution identity")
        token = credential.data["token"]
        if not isinstance(token, str) or not token:
            raise ValueError("credential token is malformed")

        request = urllib.request.Request(
            f"{api_base}/_sc/mem/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status != 200:
                    raise ValueError("authenticated whoami refused")
                payload = response.read(65_537)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError("authenticated whoami is unavailable") from exc
        if len(payload) > 65_536:
            raise ValueError("authenticated whoami response is oversized")
        try:
            whoami = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("authenticated whoami response is malformed") from exc
        if (
            not isinstance(whoami, dict)
            or whoami.get("shell_id") != context.shell_id
            or whoami.get("shortname") != context.shell_shortname
        ):
            raise ValueError("authenticated whoami identity mismatches")
        credential.assert_stable()

    return IdentityContext(
        shell_id=context.shell_id,
        shell_shortname=context.shell_shortname,
        shell_worktree=str(worktree),
        api_base=api_base,
        credential_file=str(credential_path),
        binding_generation=context.binding_record_generation,
        plugin_health_generation=context.plugin_contract_generation,
        token=token,
    )


def _route_class(arguments: Sequence[str], policy: Mapping[str, object]) -> str:
    route = arguments[0] if arguments else "help"
    public = policy.get("public_command_policy")
    if not isinstance(public, dict):
        return REFUSED_CLASS
    if route in {"help", "-h", "--help"}:
        return NEUTRAL_CLASS
    selector_policies = public.get("selector_policies")
    if isinstance(selector_policies, dict) and route in selector_policies:
        top_class = AUTHORIZED_CLASS
    else:
        top_class = ""
    for command_class in (AUTHORIZED_CLASS, REFUSED_CLASS):
        routes = public.get(command_class)
        if isinstance(routes, list) and (
            route in routes or (route.startswith("boot-") and "boot-*" in routes)
        ):
            top_class = command_class
            break
    if not top_class:
        return REFUSED_CLASS
    if top_class != AUTHORIZED_CLASS:
        return top_class

    custom = policy.get("custom_subcommand_policy")
    if route in CUSTOM_ROUTE_POLICIES:
        name = CUSTOM_ROUTE_POLICIES[route]
        rows = custom.get(name) if isinstance(custom, dict) else None
        token = arguments[1] if len(arguments) > 1 else "<bare-default-list>"
        if route == "feature" and token == "<bare-default-list>":
            return AUTHORIZED_CLASS
        return rows.get(token, REFUSED_CLASS) if isinstance(rows, dict) else REFUSED_CLASS

    literal_file = LITERAL_ROUTE_FILES.get(route)
    if literal_file is not None:
        literals = policy.get("literal_subcommand_policy")
        rows = literals.get(literal_file) if isinstance(literals, dict) else None
        token = arguments[1] if len(arguments) > 1 else ""
        if route == "mem" and token in {"seed", "lns"}:
            dynamic = custom.get("mem.dynamic") if isinstance(custom, dict) else None
            command_class = (
                dynamic.get(token, REFUSED_CLASS)
                if isinstance(dynamic, dict)
                else REFUSED_CLASS
            )
        else:
            command_class = (
                rows.get(token, REFUSED_CLASS)
                if isinstance(rows, dict)
                else REFUSED_CLASS
            )
        if command_class != AUTHORIZED_CLASS:
            return command_class
        if route == "vm" and token == "mcp":
            nested = custom.get("vm.mcp") if isinstance(custom, dict) else None
            child = arguments[2] if len(arguments) > 2 else ""
            return (
                nested.get(child, REFUSED_CLASS)
                if isinstance(nested, dict)
                else REFUSED_CLASS
            )
    return AUTHORIZED_CLASS


def _load_policy(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("DSH command policy is unavailable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ValueError("DSH command policy is stale")
    return value


def _refusal(reason: str) -> int:
    bounded = " ".join(reason.split())[:240] or "admission failed"
    print(f"sc: DSH command refused before effect: {bounded}", file=sys.stderr)
    return 77


def _exec_dispatch(
    dispatch: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    if not dispatch.is_file() or not os.access(dispatch, os.R_OK):
        raise ValueError("canonical dispatcher is unavailable")
    os.execve("/bin/sh", ["sh", str(dispatch), *arguments], dict(environment))


def admit_sc(
    *,
    caller_root: Path,
    live_root: Path,
    policy_path: Path,
    arguments: Sequence[str],
) -> int:
    """Admit one stable ``sc`` route before ambient selectors or effects."""
    try:
        os.fstat(DESCRIPTOR_FD)
    except OSError:
        descriptor_fd = None
    else:
        descriptor_fd = DESCRIPTOR_FD
    resolution = resolve_linux(descriptor_fd=descriptor_fd)
    bridge_present = {
        name for name in os.environ
        if name in BRIDGE_NAMES or name.startswith("DSH_SC_")
    }
    default_dispatch = live_root / ".super-coder" / "scripts" / "dispatch.sh"

    if resolution.provenance == "unknown":
        return _refusal(resolution.reason)
    if resolution.provenance == "native":
        if bridge_present:
            return _refusal("native caller presented DSH authority facts")
        dispatch = default_dispatch
        override = os.environ.get("SC_DISPATCH", "")
        if override:
            dispatch = Path(override)
            if not dispatch.is_file() or not os.access(dispatch, os.R_OK):
                print(
                    f"\u2717 ./sc: SC_DISPATCH is set but not readable: {dispatch}",
                    file=sys.stderr,
                )
                return 1
        environment = dict(os.environ)
        environment["SC_CALLER_ROOT"] = str(caller_root)
        try:
            _exec_dispatch(dispatch, arguments, environment)
        except ValueError as exc:
            print(f"\u2717 ./sc: {exc}", file=sys.stderr)
            return 1
        raise AssertionError("unreachable")

    if any(name.startswith("SC_") for name in os.environ):
        return _refusal("managed caller presented an ambient SC selector")
    try:
        policy = _load_policy(policy_path)
    except ValueError as exc:
        return _refusal(str(exc))
    command_class = _route_class(arguments, policy)
    if command_class == NEUTRAL_CLASS:
        print("super-coder — managed DSH command surface")
        print("  help is identity-neutral; unknown and operator routes are refused")
        return 0
    if command_class != AUTHORIZED_CLASS:
        route = arguments[0] if arguments else "help"
        return _refusal(f"route {route!r} is not shell-authorized")
    if resolution.context is None:
        return _refusal("managed execution has no immutable identity context")
    try:
        canonical_caller = caller_root.resolve(strict=True)
        if canonical_caller != Path(resolution.context.shell_worktree).resolve(strict=True):
            raise ValueError("invoked sc does not belong to the bound shell worktree")
        identity = resolve_execution_identity(
            context=resolution.context,
            environment=os.environ,
        )
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("SC_")
        }
        environment.update({
            "DSH_SC_SHELL_ID": str(identity.shell_id),
            "DSH_SC_SHELL_SHORTNAME": identity.shell_shortname,
            "DSH_SC_SHELL_WORKTREE": identity.shell_worktree,
            "DSH_SC_API_BASE": identity.api_base,
            "DSH_SC_MEM_CREDENTIAL_FILE": identity.credential_file,
            "DSH_SC_BINDING_GENERATION": str(identity.binding_generation),
            "DSH_SC_PLUGIN_HEALTH_GENERATION": identity.plugin_health_generation,
            "SC_CALLER_ROOT": identity.shell_worktree,
        })
        _exec_dispatch(default_dispatch, arguments, environment)
    except ValueError as exc:
        return _refusal(str(exc))
    raise AssertionError("unreachable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proc-cgroup", type=Path, default=Path("/proc/self/cgroup"))
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--descriptor-fd", type=int)
    parser.add_argument("--prototype-issuer-key", type=Path)
    parser.add_argument("--issuer-identity", type=Path)
    parser.add_argument("--process-root", type=Path, default=Path("/proc"))
    parser.add_argument("--now-monotonic-ns", type=int)
    parser.add_argument(
        "--command-class",
        choices=(AUTHORIZED_CLASS, NEUTRAL_CLASS, REFUSED_CLASS),
        default=AUTHORIZED_CLASS,
    )
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--identity-record", type=Path)
    parser.add_argument("--policy-route")
    parser.add_argument("--protected-effect", type=Path)
    parser.add_argument("--default-dispatch", type=Path)
    parser.add_argument("--native-credential-dir", type=Path)
    parser.add_argument("--admit-sc", action="store_true")
    parser.add_argument("--caller-root", type=Path)
    parser.add_argument("--live-root", type=Path)
    parser.add_argument("--policy-contract", type=Path)
    parser.add_argument("dispatch_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dispatch_args = list(args.dispatch_args)
    if dispatch_args[:1] == ["--"]:
        dispatch_args.pop(0)
    if args.admit_sc:
        if args.caller_root is None or args.live_root is None or args.policy_contract is None:
            return _refusal("stable bootstrap omitted a fixed admission path")
        return admit_sc(
            caller_root=args.caller_root,
            live_root=args.live_root,
            policy_path=args.policy_contract,
            arguments=dispatch_args,
        )
    resolution = resolve_linux(
        proc_cgroup=args.proc_cgroup,
        cgroup_root=args.cgroup_root,
        descriptor_fd=args.descriptor_fd,
        issuer_key=args.prototype_issuer_key,
        issuer_identity=args.issuer_identity,
        process_root=args.process_root,
        now_monotonic_ns=args.now_monotonic_ns,
    )
    command_class = args.command_class
    if args.policy_route is not None:
        if args.policy_route in {"help", "-h", "--help"}:
            command_class = NEUTRAL_CLASS
        elif args.policy_route == "mem":
            command_class = AUTHORIZED_CLASS
        else:
            command_class = REFUSED_CLASS
    decision = classify(
        resolution,
        environment=os.environ,
        command_class=command_class,
        aliases=args.alias,
    )
    identity = None
    refusal_reason = resolution.reason
    if decision == "authorized":
        try:
            if args.identity_record is None:
                raise ValueError("authorized route has no current identity record")
            identity = resolve_identity(
                environment=os.environ,
                aliases=args.alias,
                identity_record=args.identity_record,
            )
        except (TypeError, ValueError) as exc:
            decision = "refused"
            refusal_reason = str(exc)
    if decision in {"refused"}:
        print(json.dumps({
            "decision": decision,
            "provenance": resolution.provenance,
            "reason": refusal_reason,
        }))
        return 77
    credential_count = None
    if decision == "native" and args.native_credential_dir is not None:
        credential_count = len(list(args.native_credential_dir.glob("*.json")))
    if decision == "authorized" and args.protected_effect is not None:
        args.protected_effect.write_text(json.dumps({
            "shell_id": identity.shell_id,
            "shell_shortname": identity.shell_shortname,
            "binding_generation": identity.binding_generation,
            "plugin_health_generation": identity.plugin_health_generation,
        }, sort_keys=True))
    if decision != "neutral" and args.default_dispatch is not None:
        dispatch = Path(os.environ.get("SC_DISPATCH", str(args.default_dispatch)))
        os.execv(dispatch, [str(dispatch), *dispatch_args])
    receipt = {
        "decision": decision,
        "provenance": resolution.provenance,
        "credential_count": credential_count,
    }
    if args.policy_route is not None:
        receipt["route"] = args.policy_route
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
