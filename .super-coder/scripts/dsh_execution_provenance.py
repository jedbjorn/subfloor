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
import json
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CONTRACT = "sc-dsh-linux-cgroup-v2-v1"
DOMAIN_PREFIX = "/sc-dsh/"
DOMAIN_NAME = re.compile(r"[a-f0-9]{32}\.scope")
REQUIRED_SEALS = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)
BRIDGE_NAMES = {"DSH_SHELL"}
EARLY_OVERRIDE_NAMES = {"SC_DISPATCH", "SC_CALLER_ROOT", "SC_MEM_AS"}
AUTHORIZED_CLASS = "dsh_shell_authorized"
NEUTRAL_CLASS = "identity_neutral_read_only"
REFUSED_CLASS = "refused"


@dataclass(frozen=True)
class Resolution:
    provenance: str
    reason: str


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
    seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
    if seals & REQUIRED_SEALS != REQUIRED_SEALS:
        raise ValueError("execution descriptor is not immutably sealed")
    payload = os.pread(fd, 65_537, 0)
    if not payload or len(payload) > 65_536:
        raise ValueError("execution descriptor has an invalid size")
    data = json.loads(payload)
    expected = {
        "contract",
        "cgroup",
        "domain_id",
        "binding_generation",
        "expires_monotonic_ns",
        "non_delegated",
    }
    if set(data) != expected:
        raise ValueError("execution descriptor has an unknown schema")
    return data


def _managed_resolution(
    membership: str,
    *,
    cgroup_root: Path,
    descriptor_fd: int | None,
    now_monotonic_ns: int,
) -> Resolution:
    if descriptor_fd is None:
        return Resolution("unknown", "managed membership has no descriptor")
    try:
        descriptor = _sealed_descriptor(descriptor_fd)
        name = Path(membership).name
        if not DOMAIN_NAME.fullmatch(name):
            raise ValueError("managed cgroup name is malformed")
        if descriptor["contract"] != CONTRACT:
            raise ValueError("execution descriptor contract is stale")
        if descriptor["cgroup"] != membership:
            raise ValueError("execution descriptor names another cgroup")
        if descriptor["domain_id"] != name.removesuffix(".scope"):
            raise ValueError("execution descriptor domain identity mismatches")
        if descriptor["non_delegated"] is not True:
            raise ValueError("execution domain is delegated")
        if not isinstance(descriptor["binding_generation"], int):
            raise TypeError("binding generation is malformed")
        if descriptor["binding_generation"] < 1:
            raise ValueError("binding generation is malformed")
        expires = descriptor["expires_monotonic_ns"]
        if not isinstance(expires, int) or expires <= now_monotonic_ns:
            raise ValueError("execution descriptor is stale")

        root = cgroup_root.resolve(strict=True)
        domain = (root / membership.lstrip("/")).resolve(strict=True)
        if not domain.is_relative_to(root):
            raise ValueError("managed cgroup escapes the cgroup-v2 mount")
        mode = domain.stat().st_mode
        if not stat.S_ISDIR(mode) or mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("managed cgroup is writable outside its owner")
        if (domain / "cgroup.type").read_text().strip() != "domain":
            raise ValueError("managed cgroup is not a cgroup-v2 domain")
        if (domain / "cgroup.subtree_control").read_text().strip():
            raise ValueError("managed execution domain delegates controllers")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return Resolution("unknown", str(exc))
    return Resolution("managed", "sealed non-delegated cgroup-v2 membership")


def resolve_linux(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    descriptor_fd: int | None = None,
    now_monotonic_ns: int | None = None,
) -> Resolution:
    """Resolve native, managed, or unknown from OS evidence only."""
    try:
        membership = _unified_membership(proc_cgroup.read_text())
    except (OSError, ValueError) as exc:
        return Resolution("unknown", str(exc))
    if not membership.startswith(DOMAIN_PREFIX):
        if descriptor_fd is not None:
            return Resolution("unknown", "descriptor is outside its named cgroup")
        return Resolution("native", "no managed cgroup membership")
    return _managed_resolution(
        membership,
        cgroup_root=cgroup_root,
        descriptor_fd=descriptor_fd,
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
    if environment.get("DSH_SHELL") != "1":
        return "refused"
    if any(not environment.get(name) for name in aliases):
        return "refused"
    return "authorized"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proc-cgroup", type=Path, default=Path("/proc/self/cgroup"))
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--descriptor-fd", type=int)
    parser.add_argument("--now-monotonic-ns", type=int)
    parser.add_argument(
        "--command-class",
        choices=(AUTHORIZED_CLASS, NEUTRAL_CLASS, REFUSED_CLASS),
        default=AUTHORIZED_CLASS,
    )
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--default-dispatch", type=Path)
    parser.add_argument("--native-credential-dir", type=Path)
    parser.add_argument("dispatch_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    resolution = resolve_linux(
        proc_cgroup=args.proc_cgroup,
        cgroup_root=args.cgroup_root,
        descriptor_fd=args.descriptor_fd,
        now_monotonic_ns=args.now_monotonic_ns,
    )
    decision = classify(
        resolution,
        environment=os.environ,
        command_class=args.command_class,
        aliases=args.alias,
    )
    if decision in {"refused"}:
        print(json.dumps({"decision": decision, "provenance": resolution.provenance}))
        return 77
    credential_count = None
    if decision == "native" and args.native_credential_dir is not None:
        credential_count = len(list(args.native_credential_dir.glob("*.json")))
    if args.default_dispatch is not None:
        dispatch = Path(os.environ.get("SC_DISPATCH", str(args.default_dispatch)))
        os.execv(dispatch, [str(dispatch), *args.dispatch_args])
    print(json.dumps({
        "decision": decision,
        "provenance": resolution.provenance,
        "credential_count": credential_count,
    }))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
