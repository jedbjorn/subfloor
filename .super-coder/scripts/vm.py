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

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import ports

CHECKS = ("domain", "ssh", "transfer", "snapshot", "toolchain")

# The broker listens here — a unix socket inside the bind-mounted engine dir, so
# the same absolute path resolves on the host (where the broker runs) and in the
# sandbox (where windows_devkit curls it). No network surface; fs-perm gated.
RUN_DIR = ports.ENGINE / "run"
SOCKET = RUN_DIR / "vm-broker.sock"

# The GUI seam (#263): a broker-owned `ssh -N -L` forwards this unix socket to
# the guest's localhost-bound Windows-MCP port. Same posture as the broker
# socket — lives in the bind mount, fs-perm gated (0600), no network surface.
MCP_SOCKET = RUN_DIR / "vm-mcp.sock"
MCP_PIDFILE = RUN_DIR / "vm-mcp-tunnel.pid"
MCP_LOG = RUN_DIR / "vm-mcp-tunnel.log"


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
        # errors="replace": Windows guests emit non-UTF-8 constantly (UTF-16
        # files, OEM-codepage console output) — decode lossily, never raise.
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except FileNotFoundError as e:
        return False, f"command not found: {e.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return False, f"timed out (>{timeout}s)"


def _missing(cfg: dict, *fields: str) -> str | None:
    absent = [f for f in fields if not str(cfg.get(f, "")).strip()]
    return ("missing required field(s): " + ", ".join(absent)) if absent else None


def _virsh(cfg: dict, *args: str) -> list[str]:
    """A virsh argv against the configured connection. `libvirt_uri` in the vm
    block selects the hypervisor — set it to `qemu:///system` for a system-scope
    domain, which the default `qemu:///session` cannot see. Absent, virsh uses
    its own default (the `LIBVIRT_DEFAULT_URI` env var, else `qemu:///session`)."""
    uri = str(cfg.get("libvirt_uri", "")).strip()
    return ["virsh", *(["--connect", uri] if uri else []), *args]


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
    return _run(_virsh(cfg, "dominfo", str(cfg["domain"])), timeout=15)


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
        _virsh(cfg, "snapshot-info", str(cfg["domain"]),
               "--snapshotname", str(cfg["snapshot"])), timeout=15)
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


# -- the loop verbs (host-side; the broker exposes these over the socket) -----
#
# Verbs operate on the SAVED `vm` block — windows_devkit names a command, not a
# config. (validate() above is the exception: it tests a CANDIDATE block the
# wizard passes in, before it is saved.)

def do_exec(command: str, timeout: int = 120) -> dict:
    """Run one command in the guest over SSH. Returns {ok, exit, stdout, stderr}."""
    cfg = read() or {}
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return {"ok": False, "exit": -1, "stdout": "", "stderr": m}
    if not str(command).strip():
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "exec: empty command"}
    try:
        # errors="replace": guest output is routinely non-UTF-8 (UTF-16 files,
        # OEM codepages). A strict decode turned the whole exec into a 500 with
        # no exit code or partial output (#261) — lossy beats fatal here; callers
        # needing byte-exact output base64 it guest-side.
        p = subprocess.run(_ssh_argv(cfg, command), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return {"ok": p.returncode == 0, "exit": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except FileNotFoundError as e:
        return {"ok": False, "exit": 127, "stdout": "",
                "stderr": f"command not found: {e.filename} — is ssh installed on the host?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": 124, "stdout": "", "stderr": f"timed out (>{timeout}s)"}


def do_reset(running: bool = True) -> dict:
    """Revert to the clean snapshot. The clean snapshot is OFFLINE (this CPU's
    non-migratable invtsc flag refuses a live snapshot), so a bare revert lands
    powered-off. `running=True` adds `--running` to boot it — START a run from a
    clean booted box. `running=False` leaves it OFF — END a run clean *and*
    powered down in one op, so the 12 GB guest doesn't idle on the host."""
    cfg = read() or {}
    if m := _missing(cfg, "domain", "snapshot"):
        return {"ok": False, "output": m}
    argv = _virsh(cfg, "snapshot-revert", str(cfg["domain"]),
                  "--snapshotname", str(cfg["snapshot"]))
    if running:
        argv.append("--running")
    ok, out = _run(argv, timeout=60)
    state = "running" if running else "powered off"
    return {"ok": ok, "output": out or f"reverted '{cfg['domain']}' to '{cfg['snapshot']}' ({state})"}


def do_bake(shutdown_timeout: int = 180) -> dict:
    """(Re)bake the CLEAN snapshot: graceful shutdown → delete the old snapshot
    → snapshot-create-as OFFLINE. The one-command form of the deploy doc's
    'provision, then bake' step, run AFTER configure_winbox has provisioned +
    verified the toolchain.

    HOST-side only, and deliberately NOT a broker verb: the snapshot is the
    fork's trust anchor — every test run reverts to it. A sandboxed shell may
    exec/reset AGAINST the snapshot, but must never redefine it; if a
    compromised sandbox could re-bake, it could persist tampering across every
    future reset. So baking stays with the operator, where virsh lives."""
    if os.environ.get("SC_SANDBOX"):
        return {"ok": False, "output":
                "bake refuses to run in the sandbox — the clean snapshot is the "
                "trust anchor every test reverts to; only the HOST may redefine "
                "it. Ask the operator to run: ./sc vm-bake"}
    cfg = read() or {}
    if m := _missing(cfg, "domain", "snapshot"):
        return {"ok": False, "output": m}
    dom, snap = str(cfg["domain"]), str(cfg["snapshot"])
    steps = []

    ok, state = _run(_virsh(cfg, "domstate", dom), timeout=15)
    if not ok:
        return {"ok": False, "output": state}
    if "shut off" not in state:
        ok, out = _run(_virsh(cfg, "shutdown", dom), timeout=15)
        if not ok:
            return {"ok": False, "output": out}
        steps.append("graceful shutdown sent")
        deadline = time.monotonic() + shutdown_timeout
        while time.monotonic() < deadline:
            ok, state = _run(_virsh(cfg, "domstate", dom), timeout=15)
            if ok and "shut off" in state:
                break
            time.sleep(3)
        else:
            return {"ok": False, "output":
                    f"guest did not shut off within {shutdown_timeout}s (state: "
                    f"{state.strip()}) — the clean snapshot must be OFFLINE. "
                    f"Shut it down in the guest and re-run ./sc vm-bake"}

    ok, _out = _run(_virsh(cfg, "snapshot-info", dom, "--snapshotname", snap),
                    timeout=15)
    if ok:  # an old bake exists — replace, never stack
        ok, out = _run(_virsh(cfg, "snapshot-delete", dom,
                              "--snapshotname", snap), timeout=120)
        if not ok:
            return {"ok": False, "output": out}
        steps.append(f"deleted old '{snap}'")

    ok, out = _run(_virsh(cfg, "snapshot-create-as", dom, snap, "--description",
                          "pristine OS + toolchain (sc vm-bake)"), timeout=300)
    if not ok:
        return {"ok": False, "output": out}
    steps.append(f"baked '{snap}' (offline)")
    return {"ok": True,
            "output": "; ".join(steps) + " — guest left powered off"}


def do_push(src: str, dest: str | None = None) -> dict:
    """Stage a host-visible artifact into transfer_dir (the host side of the
    guest's virtio-fs share). `src` is a path in the bind-mounted repo; the guest
    sees the copy under its mapped share. The fast path — no scp, no guest auth.

    Contained by design: `src` must resolve inside the repo and `dest` must stay
    inside transfer_dir. The broker socket is reachable from the sandbox (same
    uid, socket in the bind-mount), so without these an in-sandbox caller could
    read host files (`src: ~/.ssh/...`) or write outside the share as the host
    user (`dest: ../../..`) — a sandbox→host escape. fs-perm (0600) gates other
    users, not the sandbox."""
    cfg = read() or {}
    if m := _missing(cfg, "transfer_dir"):
        return {"ok": False, "output": m}
    repo_root = ports.ENGINE.parent.resolve()
    src_p = Path(os.path.expanduser(str(src)))
    if not src_p.is_absolute():
        src_p = repo_root / src_p
    src_p = src_p.resolve()
    if not src_p.is_relative_to(repo_root):
        return {"ok": False, "output": f"push: src must be inside the repo: {src}"}
    if not src_p.is_file():
        return {"ok": False, "output": f"push: source not found: {src_p}"}
    d = Path(os.path.expanduser(str(cfg["transfer_dir"]))).resolve()
    if not d.is_dir():
        return {"ok": False, "output": f"transfer_dir does not exist: {d}"}
    target = (d / (dest or src_p.name)).resolve()
    if not target.is_relative_to(d):
        return {"ok": False, "output": f"push: dest escapes transfer_dir: {dest}"}
    try:
        shutil.copy2(src_p, target)
    except OSError as e:
        return {"ok": False, "output": f"push failed: {e}"}
    return {"ok": True, "output": f"staged {src_p.name} -> {target} (guest sees it via the share)"}


def do_capture(command: str | None = None) -> dict:
    """Collect installer/test state: optionally exec a command for its stdout,
    and always grab a `virsh screenshot` of the guest console (GUI installers
    show state on-screen, not on stdout). Screenshot returned base64."""
    cfg = read() or {}
    result: dict = {"ok": True}
    if command and str(command).strip():
        result["exec"] = do_exec(command)
        result["ok"] = bool(result["exec"].get("ok"))
    if m := _missing(cfg, "domain"):
        result["screenshot_error"] = m
        return result
    shot = Path(tempfile.gettempdir()) / f"sc_vm_{cfg['domain']}.ppm"
    ok, out = _run(_virsh(cfg, "screenshot", str(cfg["domain"]), str(shot)), timeout=30)
    if ok and shot.exists():
        data = shot.read_bytes()
        result["screenshot_b64"] = base64.b64encode(data).decode()
        result["screenshot_bytes"] = len(data)
        result["screenshot_format"] = "ppm"
        try:
            shot.unlink()
        except OSError:
            pass
    else:
        result["ok"] = False
        result["screenshot_error"] = out or "virsh screenshot produced no file"
    return result


# -- MCP tunnel (the GUI seam — broker-owned ssh forward, #263) ---------------
#
# Sandboxed seats cannot hold a live MCP session against the guest's Windows-MCP
# server: no ssh, no key, no route across libvirt NAT, and a host-loopback
# tunnel is invisible to the container. The seam: the broker (host-side, where
# the key lives) opens ONE `ssh -N -L` that forwards a UNIX SOCKET in the
# bind-mounted run/ dir straight to the guest's localhost-bound Windows-MCP
# port. OpenSSH does the byte plumbing — no HTTP proxying in the broker, so
# SSE/chunked streaming passes through untouched. In-sandbox, vm_mcp_relay.py
# bridges TCP→socket because `claude mcp add --transport http` only speaks TCP.

def _tunnel_pid() -> int | None:
    """The live tunnel's pid, or None (no pidfile / stale pidfile)."""
    try:
        pid = int(MCP_PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def mcp_status() -> dict:
    pid = _tunnel_pid()
    running = pid is not None and MCP_SOCKET.exists()
    return {"ok": True, "running": running, "pid": pid,
            "socket": str(MCP_SOCKET) if running else None}


def do_mcp_up(wait: float = 15) -> dict:
    """Open the MCP tunnel. Idempotent — an already-live tunnel is reported, not
    doubled. The forward target is the SAVED block's `mcp_port` (default 8000,
    what `windows_vm_gui`'s guest prep bakes), never a caller-named port — same
    rule as every other verb: the sandbox names an action, not a destination."""
    cfg = read() or {}
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return {"ok": False, "output": m}
    if (pid := _tunnel_pid()) and MCP_SOCKET.exists():
        return {"ok": True, "output": f"tunnel already up (pid {pid})",
                "socket": str(MCP_SOCKET), "pid": pid}
    do_mcp_down()  # clear any half-dead remnant (stale pid or orphaned socket)
    port = int(cfg.get("mcp_port", 8000))
    key = os.path.expanduser(str(cfg.get("ssh_key_path", "")))
    argv = [
        "ssh", "-i", key,
        "-p", str(cfg.get("ssh_port", 22)),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",   # a dead forward must not linger as a live pid
        "-o", "ServerAliveInterval=30",     # drop (and surface) a hung guest, don't wedge
        "-o", "StreamLocalBindUnlink=yes",  # replace a stale socket file on reopen
        "-o", "StreamLocalBindMask=0177",   # socket lands 0600, matching the broker's
        "-N", "-L", f"{MCP_SOCKET}:127.0.0.1:{port}",
        f"{cfg.get('ssh_user')}@{cfg.get('ssh_host')}",
    ]
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(MCP_LOG, "wb") as log:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, start_new_session=True)
    except FileNotFoundError as e:
        return {"ok": False,
                "output": f"command not found: {e.filename} — is ssh installed on the host?"}
    MCP_PIDFILE.write_text(str(p.pid))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if p.poll() is not None:  # ssh died — auth/route/forward failure
            err = MCP_LOG.read_text(errors="replace").strip()[-500:]
            MCP_PIDFILE.unlink(missing_ok=True)
            return {"ok": False,
                    "output": f"ssh tunnel exited (rc {p.returncode}): {err or '(no output)'}"}
        if MCP_SOCKET.exists():
            return {"ok": True,
                    "output": f"tunnel up — {MCP_SOCKET} -> guest 127.0.0.1:{port}",
                    "socket": str(MCP_SOCKET), "pid": p.pid, "port": port}
        time.sleep(0.2)
    p.terminate()
    MCP_PIDFILE.unlink(missing_ok=True)
    return {"ok": False, "output": f"tunnel socket did not appear within {wait}s"}


def do_mcp_down() -> dict:
    """Close the MCP tunnel. Idempotent — safe to call with nothing running."""
    pid = _tunnel_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    MCP_PIDFILE.unlink(missing_ok=True)
    MCP_SOCKET.unlink(missing_ok=True)
    return {"ok": True,
            "output": f"tunnel stopped (pid {pid})" if pid else "tunnel not running"}


# -- client: HTTP over the broker's unix socket ------------------------------

def broker_call(method: str, path: str, body: dict | None = None,
                timeout: int = 130) -> dict:
    """Speak HTTP/1.1 to the broker over its unix socket and return parsed JSON.
    Raises ConnectionError if the broker is not listening (so callers can render
    a 'start the broker' hint). Used by the in-sandbox server to proxy validate."""
    payload = b"" if body is None else json.dumps(body).encode()
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: vm-broker\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + payload
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        s.connect(str(SOCKET))
        s.sendall(req)
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise ConnectionError(f"vm-broker not reachable at {SOCKET}: {e}") from e
    finally:
        s.close()
    _, _, raw_body = b"".join(chunks).partition(b"\r\n\r\n")
    try:
        return json.loads(raw_body.decode() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad broker response",
                "raw": raw_body[:200].decode("latin1")}


# -- host CLI (path lookup for `sc`; verbs for manual no-broker testing) ------

def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "sock"
    if mode == "sock":
        print(SOCKET)
    elif mode == "configured":
        # exit 0 if this fork has linked a VM (so the launch hook can self-skip)
        return 0 if read() else 1
    elif mode == "exec":
        print(json.dumps(do_exec(" ".join(argv[1:]))))
    elif mode == "reset":
        # `vm.py reset` boots clean; `vm.py reset off` lands clean + powered off
        print(json.dumps(do_reset(running=(argv[1:2] != ["off"]))))
    elif mode == "push":
        print(json.dumps(do_push(argv[1] if len(argv) > 1 else "",
                                 argv[2] if len(argv) > 2 else None)))
    elif mode == "capture":
        print(json.dumps(do_capture(" ".join(argv[1:]) or None)))
    elif mode == "bake":
        r = do_bake()
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    elif mode == "mcp-sock":
        print(MCP_SOCKET)
    elif mode == "mcp-up":
        r = do_mcp_up()
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    elif mode == "mcp-down":
        print(json.dumps(do_mcp_down()))
    elif mode == "mcp-status":
        print(json.dumps(mcp_status()))
    elif mode == "validate":
        print(json.dumps(validate(argv[1] if len(argv) > 1 else "", read() or {})))
    else:
        sys.exit("usage: vm.py [sock|exec <cmd>|reset|bake|push <src> [dest]|capture [cmd]"
                 "|mcp-sock|mcp-up|mcp-down|mcp-status|validate <check>]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
