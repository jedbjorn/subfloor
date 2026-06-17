#!/usr/bin/env python3
"""Windows Test VM — config read/write + live connection checks.

Link-only by design: the engine never creates the VM. The operator brings a
ready Windows VM (OpenSSH enabled, a clean snapshot, a transfer dir, and — via
the admin `configure_winbox` skill — a baked toolchain). These checks validate
that the operator-supplied `vm` block actually reaches a reachable, provisioned
box BEFORE it is saved to instance.json.

The config lives under the `vm` key of `.super-coder/instance.json` (so there is
no schema migration — the VM is a host resource, not shell state). It holds a
key PATH, never key material — secrets posture matches the rest of the engine.

Each check runs ONE real host-side command and returns {ok, output}, mirroring
api/server.py's run_script contract so the GUI can render it the same way.

    domain    virsh dominfo <domain>                 VM exists / visible to libvirt
    ssh       ssh ... echo ok                        auth + remote exec work
    transfer  write+read+rm a probe in transfer_dir  host side of the share works
    snapshot  virsh snapshot-info <domain> <snap>    the named clean snapshot exists
    toolchain ssh ... dotnet --version               box is provisioned (verify-only)

The `toolchain` check is verify-only — it confirms `configure_winbox` has run;
it never installs anything.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import ports

CHECKS = ("domain", "ssh", "transfer", "snapshot", "toolchain")


# -- config (instance.json `vm` block) ---------------------------------------

def read() -> dict | None:
    """The persisted vm block, or None if the fork has not configured one."""
    return ports.resolve(persist=False).get("vm")


def write(vm: dict | None) -> dict | None:
    """Persist (or clear) the vm block, preserving every other config key."""
    cfg = ports.resolve(persist=False)
    if vm:
        cfg["vm"] = vm
    else:
        cfg.pop("vm", None)
    ports.save(cfg)
    return cfg.get("vm")


# -- check primitives --------------------------------------------------------

def _run(argv: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except FileNotFoundError as e:
        return False, f"command not found: {e.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return False, f"timed out (>{timeout}s)"


def _missing(cfg: dict, *fields: str) -> str | None:
    absent = [f for f in fields if not str(cfg.get(f, "")).strip()]
    return ("missing required field(s): " + ", ".join(absent)) if absent else None


def _ssh_argv(cfg: dict, remote: str) -> list[str]:
    """An ssh invocation against the configured guest. BatchMode keeps it
    non-interactive (no password/passphrase prompt can hang the server)."""
    key = os.path.expanduser(str(cfg.get("ssh_key_path", "")))
    return [
        "ssh", "-i", key,
        "-p", str(cfg.get("ssh_port", 22)),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{cfg.get('ssh_user')}@{cfg.get('ssh_host')}", remote,
    ]


# -- the five checks ---------------------------------------------------------

def _check_domain(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "domain"):
        return False, m
    return _run(["virsh", "dominfo", str(cfg["domain"])], timeout=15)


def _check_ssh(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return False, m
    return _run(_ssh_argv(cfg, "echo ok"), timeout=20)


def _check_transfer(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "transfer_dir"):
        return False, m
    d = Path(os.path.expanduser(str(cfg["transfer_dir"])))
    if not d.is_dir():
        return False, f"transfer_dir does not exist or is not a directory: {d}"
    probe = d / ".sc_vm_probe"
    try:
        probe.write_text("ok")
        back = probe.read_text()
        probe.unlink()
    except OSError as e:
        return False, f"transfer_dir not writable host-side: {e}"
    if back != "ok":
        return False, "wrote a probe file but read back unexpected content"
    return True, f"wrote + read back a probe in {d} (host side of the share OK)"


def _check_snapshot(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "domain", "snapshot"):
        return False, m
    ok, out = _run(
        ["virsh", "snapshot-info", str(cfg["domain"]),
         "--snapshotname", str(cfg["snapshot"])], timeout=15)
    if not ok and "Domain snapshot not found" in out:
        return False, (f"snapshot '{cfg['snapshot']}' not found on domain "
                       f"'{cfg['domain']}' — create the clean snapshot first.\n{out}")
    return ok, out


def _check_toolchain(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return False, m
    ok, out = _run(_ssh_argv(cfg, "dotnet --version"), timeout=20)
    if ok:
        return True, (f".NET SDK present: {out or '(version printed)'} — "
                      "configure_winbox has run (verify-only; nothing installed).")
    return False, ("toolchain probe failed — run the admin `configure_winbox` "
                   f"skill to provision the box, then re-snapshot.\n{out}")


_CHECKS = {
    "domain": _check_domain,
    "ssh": _check_ssh,
    "transfer": _check_transfer,
    "snapshot": _check_snapshot,
    "toolchain": _check_toolchain,
}


def validate(check: str, cfg: dict) -> dict | None:
    """Run one live check against the CANDIDATE config in `cfg` (the in-progress
    wizard form, not necessarily what is saved). Returns {ok, output, check} or
    None for an unknown check name (→ 404 at the API layer)."""
    fn = _CHECKS.get(check)
    if fn is None:
        return None
    ok, out = fn(cfg or {})
    return {"ok": ok, "output": out or "(no output)", "check": check}
