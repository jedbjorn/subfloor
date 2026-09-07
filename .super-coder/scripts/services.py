#!/usr/bin/env python3
"""Operational services the GUI's Scripts page toggles: the four HOST-side
brokers (vm / ts / pm2 / db) and the Postgres sidecar.

Status is read in-process — the instance.json block says "configured", the
broker's unix socket (bind-mounted, so it answers from the host AND the
sandbox) says "running", `systemctl --user` says "supervised by systemd", and
`docker inspect` says whether the sidecar container is up. Lifecycle goes
through the engine dispatcher's FIXED verbs (`ts-broker-up`, `pg-down`, …),
exactly what `./sc <verb>` runs, so the GUI passes a registry key + an action
name and nothing arbitrary executes. Brokers start host processes, so lifecycle
is refused in the sandbox with the host command to run instead — the same
contract the /api/ts/status and /api/pm2/status proxies keep.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dbq  # noqa: E402
import pm2  # noqa: E402
import ports  # noqa: E402
import ts  # noqa: E402
import vm  # noqa: E402

ENGINE = ports.ENGINE
REPO_ROOT = ports.REPO_ROOT
DISPATCH = ENGINE / "scripts" / "dispatch.sh"

BROKER_ACTIONS = ("up", "down", "install", "uninstall")
SIDECAR_ACTIONS = ("up", "down")

# Display order. `block` is the instance.json key that marks the service as
# configured; `verb` is the dispatcher verb prefix (`<verb>-up`, `<verb>-down`,
# and for brokers `<verb>-install` / `<verb>-uninstall`); `unit` is the systemd
# --user unit dispatch.sh writes on install (the fork's repo basename fills in).
# `onboarding` is what the GUI shows when the operator switches on a service
# that has no config block yet — the CLI one-time setup, in prose.
SERVICES: dict[str, dict] = {
    "ts": {
        "name": "Tailnet broker",
        "desc": "Lets sandboxed shells run commands on declared tailnet hosts "
                "through the host's already-authenticated Tailscale node.",
        "kind": "broker", "block": "ts", "verb": "ts-broker", "module": ts,
        "unit": "sc-ts-broker-{repo}.service",
        "onboarding": (
            "No tailnet is linked to this fork. On the host: 1. `tailscale up` "
            "and confirm `tailscale status` shows your node. 2. Add a `ts` block "
            "to .super-coder/instance.json naming the hosts shells may reach, "
            "e.g. {\"allowed_hosts\": [\"build-box\"], \"ssh_user\": \"deploy\"} "
            "(PUT /api/ts writes it; POST /api/ts/validate/{daemon|auth|peer|ssh} "
            "tests it). 3. Switch the broker on here. "
            "See .super-coder/docs/tailscale-broker.md."),
    },
    "vm": {
        "name": "Windows VM broker",
        "desc": "Drives the linked Windows test VM (status, start, reset, push, "
                "exec) for sandboxed shells.",
        "kind": "broker", "block": "vm", "verb": "vm-broker", "module": vm,
        "unit": "sc-vm-broker-{repo}.service",
        "onboarding": (
            "No Windows VM is linked. Use the Windows Test VM card's "
            "\"configure…\" wizard on this page to link one (every field is "
            "live-tested before it saves), then switch the broker on here."),
    },
    "pm2": {
        "name": "PM2 broker",
        "desc": "Lets sandboxed shells see and restart the host's declared "
                "pm2-supervised processes, fail-closed on the allowlist.",
        "kind": "broker", "block": "pm2", "verb": "pm2-broker", "module": pm2,
        "unit": "sc-pm2-broker-{repo}.service",
        "onboarding": (
            "No pm2 stack is linked. On the host: 1. Confirm `pm2 jlist` lists "
            "your app processes. 2. Add a `pm2` block to "
            ".super-coder/instance.json naming the processes shells may manage, "
            "e.g. {\"processes\": [\"myapp-api\"], \"health_url\": "
            "\"http://127.0.0.1:8000/health\"} (PUT /api/pm2 writes it; "
            "POST /api/pm2/validate/{daemon|procs|health} tests it). "
            "3. Switch the broker on here. See .super-coder/docs/pm2-broker.md."),
    },
    "db": {
        "name": "Read-only DB broker",
        "desc": "Runs SELECT-only, table-allowlisted reads of the fork's live "
                "app database for sandboxed shells; the DSN never leaves the host.",
        "kind": "broker", "block": "db", "verb": "db-broker", "module": dbq,
        "unit": "sc-db-broker-{repo}.service",
        "onboarding": (
            "No live DB is linked. On the host: 1. `./sc db-init` adds the `db` "
            "block (dsn_env, allow_tables, row_cap) and prints the setup. "
            "2. Provision a read-only role and GRANT SELECT on the allowlisted "
            "tables. 3. `export SC_RO_DSN=postgresql://sc_ro:…@host:5432/db` in "
            "the broker's environment. 4. Switch the broker on here. "
            "See .super-coder/docs/db-broker.md."),
    },
    "pg": {
        "name": "Postgres sidecar",
        "desc": "A per-fork postgres:17 container on the sandbox network for "
                "developing and testing the fork's app; data lives in a named "
                "volume. The engine DB stays SQLite.",
        "kind": "sidecar", "block": "pg", "verb": "pg", "module": None,
        "unit": None,
        "onboarding": (
            "The Postgres sidecar is not enabled for this fork. Enabling it adds "
            "a `pg` key to .super-coder/instance.json (what `./sc pg-init` does) "
            "and starts the container; `./sc launch` then starts it with every "
            "launch and forwards DATABASE_URL into the sandbox."),
    },
}


def _repo_name() -> str:
    return REPO_ROOT.name


def in_sandbox() -> bool:
    return bool(os.environ.get("SC_SANDBOX"))


def _configured(spec: dict, cfg: dict) -> bool:
    return spec["block"] in cfg


def _broker_running(spec: dict) -> bool:
    try:
        return bool(spec["module"].broker_call("GET", "/health", timeout=3).get("ok"))
    except ConnectionError:
        return False


def _sidecar_running() -> bool | None:
    """True/False from docker; None when docker is not reachable here (the
    sandbox has no docker socket, and a no-docker host has no sidecar)."""
    if not shutil.which("docker"):
        return None
    try:
        p = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}",
             f"sc-pg-{_repo_name()}"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.returncode == 0 and p.stdout.strip() == "true"


def _systemd_state(spec: dict) -> tuple[bool | None, bool]:
    """(enabled, active) for the broker's systemd --user unit. `enabled` is
    None when systemctl is not available here (sandbox, or a non-systemd host)."""
    if not spec["unit"] or not shutil.which("systemctl"):
        return None, False
    unit = spec["unit"].format(repo=_repo_name())
    try:
        enabled = subprocess.run(["systemctl", "--user", "is-enabled", unit],
                                 capture_output=True, text=True, timeout=10, check=False)
        active = subprocess.run(["systemctl", "--user", "is-active", unit],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    return enabled.returncode == 0, active.stdout.strip() == "active"


def status(key: str, cfg: dict | None = None) -> dict | None:
    spec = SERVICES.get(key)
    if spec is None:
        return None
    cfg = ports.resolve(persist=False) if cfg is None else cfg
    configured = _configured(spec, cfg)
    if spec["kind"] == "broker":
        running: bool | None = _broker_running(spec)
        persistent, systemd_active = _systemd_state(spec)
        actions = BROKER_ACTIONS
    else:
        running = _sidecar_running()
        persistent, systemd_active = None, False
        actions = SIDECAR_ACTIONS
    return {
        "key": key, "name": spec["name"], "desc": spec["desc"],
        "kind": spec["kind"], "configured": configured, "running": running,
        # `persistent` = a systemd unit is enabled (survives reboot); None when
        # that cannot be known here. `supervisor` names who owns the live
        # process, so the UI can explain why `down` would be refused.
        "persistent": persistent,
        "supervisor": ("systemd" if systemd_active else "pidfile") if running else None,
        "onboarding": spec["onboarding"],
        "actions": list(actions),
        "host_command": f"./sc {spec['verb']}-up",
    }


def list_services() -> dict:
    cfg = ports.resolve(persist=False)
    return {
        "services": [status(k, cfg) for k in SERVICES],
        # Lifecycle needs the host: brokers are host processes and the sidecar
        # needs the host's docker. The GUI disables the toggles in the sandbox
        # and shows the host command instead.
        "host_lifecycle": not in_sandbox(),
    }


def _dispatch(verb: str, timeout: int = 120) -> dict:
    """Run ONE fixed dispatcher verb the way `./sc <verb>` would, from this
    fork's root. The dispatcher backgrounds brokers with nohup and redirects
    their output to the run/ log, so capturing here never blocks on them."""
    try:
        p = subprocess.run(["sh", str(DISPATCH), verb], capture_output=True,
                           text=True, cwd=str(REPO_ROOT), timeout=timeout,
                           env={**os.environ, "SC_CALLER_ROOT": str(REPO_ROOT)},
                           check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": f"{verb}: timed out (>{timeout}s)"}
    return {"ok": p.returncode == 0, "code": p.returncode,
            "output": (p.stdout + p.stderr).strip() or "(no output)"}


def run(key: str, action: str, *, init: bool = False) -> dict | None:
    """Apply one lifecycle action. Returns None for an unknown key/action, else
    {ok, code, output, service} where `service` is the fresh status row.
    `init=True` is the sidecar's "enable and start": `pg-init` before `pg-up`."""
    spec = SERVICES.get(key)
    if spec is None:
        return None
    actions = BROKER_ACTIONS if spec["kind"] == "broker" else SIDECAR_ACTIONS
    if action not in actions:
        return None
    if in_sandbox():
        return {"ok": False, "code": "host_required",
                "output": (f"{spec['name']}: lifecycle runs on the host — run "
                           f"`./sc {spec['verb']}-{action}` there, then refresh.")}
    before = status(key)
    if action in ("up", "install") and not before["configured"] and not (init and key == "pg"):
        return {"ok": False, "code": "not_configured",
                "output": spec["onboarding"], "service": before}
    outputs: list[str] = []
    if init and key == "pg" and not before["configured"]:
        r = _dispatch("pg-init")
        outputs.append(r["output"])
        if not r["ok"]:
            return {**r, "output": "\n".join(outputs), "service": status(key)}
    r = _dispatch(f"{spec['verb']}-{action}")
    outputs.append(r["output"])
    return {**r, "output": "\n".join(outputs), "service": status(key)}
