#!/usr/bin/env python3
"""`./sc vm adopt` — two-command adoption of a bring-your-own-licence Windows guest.

The operator runs ONE line in an elevated PowerShell inside the guest
(`assets/winbox/bootstrap.ps1`, fetched from the public repository) and ONE
line on the host: this verb. Adoption then runs the phases in `PHASES`, each
of which reports `done`, `skipped` or `failed` with a reason:

    locate -> wait -> install_key -> harden -> provision -> verify
           -> write_block -> broker -> baseline

Every phase is idempotent and detects satisfaction by OBSERVATION, never by a
stored "already adopted" flag: the saved key authenticating is what makes the
key phase skip, `PasswordAuthentication no` already being in `sshd_config` is
what makes hardening skip. So a re-run after a toolchain change is the same
command, and a run interrupted half-way resumes where it stopped.

Host-only (spec #232): it needs `virsh`, `ssh-keygen`, `scp` and the operator's
TTY, none of which a sandboxed shell has. `SC_SANDBOX` is refused up front with
`adopt_sandboxed`.

The guest admin password is used exactly once, for the single
password-authenticated ssh that installs the host-generated public key. It
reaches ssh through an `SSH_ASKPASS` helper that prints ONE environment
variable, so it never appears on any argv, in any log, in the JSON result, in
`instance.json` or anywhere under `.sc-state/` (decision #353: key material
stays host-side; the engine persists paths, never secrets).

Mocked at `vm._run`, `subprocess.run` and the small local seams (`_capture`,
`_run_env`, `_tcp_open`, `_dispatch_verb`) — see tests/test_vm_adopt.py.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import vm
import winbox

OPERATION = "adopt"

DEFAULT_SNAPSHOT = "baseline"
DEFAULT_LIBVIRT_URI = "qemu:///system"
DEFAULT_SSH_WAIT = 900          # 15 minutes: the operator may still be typing
SSH_POLL_INTERVAL = 5
TCP_PROBE_TIMEOUT = 5
KEY_INSTALL_TIMEOUT = 60
HARDEN_TIMEOUT = 60
RESTART_SSHD_TIMEOUT = 30
RESTART_SETTLE_WAIT = 90
KEYSCAN_TIMEOUT = 30
SCP_TIMEOUT = 300
PROVISION_TIMEOUT = 40 * 60     # the .NET SDK alone can take ten minutes
CHECK_TIMEOUT = 120
BROKER_TIMEOUT = 120
# `./sc vm-broker-up` returns as soon as the nohup'd broker is spawned; the
# unix socket appears a moment later. Poll for it rather than reading health
# through a socket that does not exist yet.
BROKER_READY_WAIT = 20
BROKER_READY_INTERVAL = 1

# The public repository the guest fetches `bootstrap.ps1` from. It is the
# ENGINE's own source repo, not the fork's: one copy of the script serves
# every fork, and a fork testing a branch passes `--bootstrap-url` instead of
# editing this. Named here so the URL is not spelled twice.
BOOTSTRAP_REPO = "jedbjorn/subfloor"
BOOTSTRAP_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/" + BOOTSTRAP_REPO + "/{ref}"
    "/.super-coder/assets/winbox/bootstrap.ps1"
)
BOOTSTRAP_DEFAULT_REF = "main"

# Guest host key mismatch: ssh refuses and says so on stderr. The text is
# stable across OpenSSH 7.x-10.x and is the only usable signal, because the
# exit code (255) is shared with every other connection failure.
HOST_KEY_CHANGED = (
    "REMOTE HOST IDENTIFICATION HAS CHANGED",
    "Host key verification failed",
)

# The declared order, in the order `run()` executes them, and the order every
# result's `phases` list carries. tests/test_vm_adopt.py compares the two, so a
# phase renamed in one place and not the other fails rather than quietly
# changing the JSON contract.
PHASES = (
    "locate", "wait", "install_key", "harden", "provision", "verify",
    "write_block", "broker", "baseline",
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAC = re.compile(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


NO_TTY = (
    "adopt has no terminal to read the guest password from, and `getpass` "
    "would fall back to reading it from stdin WITH ECHO. Re-run adopt on a "
    "TTY, or pass --password-file <path> (read once, never copied)."
)


class AdoptFailure(Exception):
    """A phase failed: stop here, report this code, write nothing further."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- seams -------------------------------------------------------------------

def _capture(argv: list[str], timeout: int) -> tuple[int, str, str]:
    """Run argv and keep stdout and stderr APART.

    `vm._run` merges the two, which is right for exit-code decisions and wrong
    for anything that parses output: the OpenSSH 10.x client prints a
    "post-quantum key exchange" warning on stderr for every connection to a
    Windows guest, and `ssh-keyscan` writes its comment lines there too. Where
    output is data (the provisioning report, a host key, a check's stdout) we
    read stdout only and let stderr be noise whenever the exit code is 0.
    """
    try:
        process = subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out (>{timeout}s)"
    return process.returncode, process.stdout, process.stderr


def _run_env(argv: list[str], env: dict, timeout: int) -> tuple[int, str, str]:
    """`_capture` with an explicit environment — the askpass path.

    The password lives in `env` (read by the helper, never on argv) and in
    nothing else: no shell, no log, no temp file with the secret in it.
    """
    try:
        process = subprocess.run(
            argv, capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out (>{timeout}s)"
    return process.returncode, process.stdout, process.stderr


def _tcp_open(host: str, port: int, timeout: float = TCP_PROBE_TIMEOUT) -> bool:
    """True when something accepts a TCP connection there."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dispatch_verb(verb: str, timeout: int = BROKER_TIMEOUT) -> tuple[bool, str]:
    """Run ONE fixed dispatcher verb the way `./sc <verb>` would.

    Same shape as services._dispatch; duplicated rather than imported because
    `services` imports `vm`, and `vm` reaches adoption.
    """
    dispatch = vm.ports.ENGINE / "scripts" / "dispatch.sh"
    root = vm.repo_root()
    try:
        process = subprocess.run(
            ["sh", str(dispatch), verb], capture_output=True, text=True,
            cwd=str(root), timeout=timeout, check=False,
            env={**os.environ, "SC_CALLER_ROOT": str(root)},
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        return False, f"command not found: {exc.filename}"
    except subprocess.TimeoutExpired:
        return False, f"{verb}: timed out (>{timeout}s)"
    return process.returncode == 0, (process.stdout + process.stderr).strip()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _stdin_is_tty() -> bool:
    """A seam: whether there is a terminal to prompt for the password on."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):  # a closed or replaced stdin
        return False


# -- helpers -----------------------------------------------------------------

def _safe(name: str) -> str:
    """A libvirt domain name reduced to a filename component."""
    cleaned = _SAFE_NAME.sub("_", str(name)).strip("._-")
    return cleaned or "domain"


def engine_ref() -> str:
    """The engine commit this install is pinned to, else `main`.

    The pin lives at `.sc-state/engine.ref` (callable_floor.read_engine_ref).
    """
    try:
        import callable_floor

        ref = callable_floor.read_engine_ref(vm.repo_root())
    except Exception:  # noqa: BLE001 — a missing pin is never fatal
        ref = None
    return ref or BOOTSTRAP_DEFAULT_REF


def bootstrap_line(url: str | None = None) -> str:
    """The single elevated-PowerShell line the operator runs IN the guest."""
    target = str(url or "").strip() or BOOTSTRAP_URL_TEMPLATE.format(ref=engine_ref())
    return (
        "powershell -ExecutionPolicy Bypass -Command "
        f"\"[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm {target} | iex\""
    )


def _ps(script: str) -> str:
    """A guest command running `script` under PowerShell, quoting-proof.

    The guest's default shell is cmd.exe, so anything with quotes, `&`, `|` or
    `%` in it would be re-parsed twice on the way in. `-EncodedCommand` takes
    UTF-16LE base64, which survives both passes byte-exact.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded}"
    )


def _state_dir() -> Path:
    return (vm.repo_root() / ".sc-state" / "local" / "vm").resolve()


def _table_rows(output: str) -> list[list[str]]:
    """Rows of a virsh table: header, `-----` rule and blank lines dropped."""
    rows = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or set(stripped) <= set("- "):
            continue
        fields = stripped.split()
        if fields and fields[0].lower() in ("interface", "name"):
            continue
        rows.append(fields)
    return rows


def _address(value: str) -> str | None:
    """The bare IPv4 out of `192.168.122.100/24`."""
    match = _IPV4.search(value or "")
    return match.group(1) if match else None


# -- the adoption run --------------------------------------------------------

class Adoption:
    """One `./sc vm adopt` run. Build it, `run()` it, read `phases`."""

    def __init__(self, *, domain: str, ssh_user: str, snapshot: str,
                 libvirt_uri: str | None, ssh_host: str | None,
                 password_file: str | None, bootstrap_url: str | None,
                 provision: bool, wait: int, saved: dict | None) -> None:
        self.domain = str(domain)
        self.ssh_user = str(ssh_user)
        self.snapshot = str(snapshot)
        self.libvirt_uri = str(libvirt_uri or "").strip()
        self.requested_host = str(ssh_host or "").strip() or None
        self.password_file = password_file
        self.bootstrap_url = bootstrap_url
        self.provision = bool(provision)
        self.wait = int(wait)
        self.saved = dict(saved or {})

        self.ssh_host: str | None = None
        self.address_source = "unknown"
        self.mac: str | None = None
        self.network: str | None = None
        self.ssh_port = int(self.saved.get("ssh_port") or 22)
        self.mcp_port = int(self.saved.get("mcp_port") or winbox.DEFAULT_MCP_PORT)
        self.key_path = _state_dir() / f"{_safe(self.domain)}.key"
        self.known_hosts = _state_dir() / f"{_safe(self.domain)}.known_hosts"
        self.workspace = winbox.DEFAULT_WORKSPACE
        self.profile: dict | None = None
        self.vm_block: dict | None = None
        self.phases: list[dict] = []
        # Non-fatal observations the operator must still see: an MCP listener
        # that has not appeared yet, a DHCP reservation libvirt refused. They
        # ride on the summary line and in the JSON, and never stop adoption.
        self.warnings: list[str] = []
        self.console_line = bootstrap_line(self.bootstrap_url)

    # -- phase bookkeeping ---------------------------------------------------

    def _report(self, name: str, status: str, detail: str) -> None:
        self.phases.append({"name": name, "status": status, "detail": detail})

    def _fail(self, name: str, code: str, detail: str) -> AdoptFailure:
        self._report(name, "failed", detail)
        return AdoptFailure(code, detail)

    def _warn(self, text: str) -> str:
        """Record a non-fatal observation and return it prefixed for a detail."""
        line = f"warning: {text}"
        self.warnings.append(text)
        return line

    def _guard_host_key(self, phase: str, *streams: str) -> None:
        """A CHANGED guest host key dead-ends every later ssh with the same
        opaque 255. Name it, and name the one file to remove.

        The pin is written by adoption itself, so the honest repair is to drop
        it and re-pin — which is what a re-run does. Reinstalling the guest or
        regenerating its host keys is the normal cause.
        """
        blob = "\n".join(str(stream or "") for stream in streams)
        if not any(marker in blob for marker in HOST_KEY_CHANGED):
            return
        raise self._fail(
            phase, "adopt_host_key_changed",
            f"the guest at {self.ssh_host}:{self.ssh_port} presented a "
            f"different host key than the one pinned in {self.known_hosts}. "
            "If the guest was reinstalled or its host keys were regenerated "
            "this is expected: remove the pin and re-run adopt —\n"
            f"  rm {self.known_hosts}",
        )

    # -- cfg shapes ----------------------------------------------------------

    def _virsh_cfg(self) -> dict:
        cfg = {"domain": self.domain}
        if self.libvirt_uri:
            cfg["libvirt_uri"] = self.libvirt_uri
        return cfg

    def _ssh_cfg(self, key_path: str | None = None) -> dict:
        """A cfg good enough for `vm._ssh_argv` / `vm._scp_argv` / `vm._ssh_ready`.

        `known_hosts_path` is included only once the pin file actually exists:
        `_ssh_base_opts` turns StrictHostKeyChecking to `yes` when it is set,
        and a `yes` against a file that is not there refuses every connection.
        """
        cfg = {
            "domain": self.domain,
            "ssh_host": self.ssh_host,
            "ssh_user": self.ssh_user,
            "ssh_key_path": str(key_path or self.key_path),
            "ssh_port": self.ssh_port,
            "workspace": self.workspace,
        }
        if self.libvirt_uri:
            cfg["libvirt_uri"] = self.libvirt_uri
        try:
            pinned = self.known_hosts.is_file() and self.known_hosts.stat().st_size > 0
        except OSError:
            pinned = False
        if pinned:
            cfg["known_hosts_path"] = str(self.known_hosts)
        return cfg

    # -- 1. locate -----------------------------------------------------------

    def phase_locate(self) -> None:
        cfg = self._virsh_cfg()
        notes: list[str] = []

        # Already adopted and still answering: there is nothing to discover.
        # Observation, not a stored flag — the guest has to actually answer.
        saved_host = str(self.saved.get("ssh_host") or "").strip()
        if saved_host and _tcp_open(saved_host, self.ssh_port):
            self.ssh_host = saved_host
            self.address_source = "the saved vm block"
            self._report(
                "locate", "skipped",
                f"{saved_host}:{self.ssh_port} from the saved vm block is "
                "already answering",
            )
            return

        ok, _state, started, output = vm._start_domain(cfg)
        if not ok:
            raise self._fail(
                "locate", "adopt_guest_not_found",
                f"domain '{self.domain}' is not usable: {output}".strip(),
            )
        if started:
            notes.append("started the powered-off domain")

        ok, output = vm._run(vm._virsh(cfg, "domiflist", self.domain), timeout=30)
        if ok:
            for fields in _table_rows(output):
                match = _MAC.search(" ".join(fields))
                if not match:
                    continue
                self.mac = match.group(1).lower()
                if len(fields) >= 3 and fields[1].lower() == "network":
                    self.network = fields[2]
                break
        if self.mac:
            notes.append(f"mac {self.mac}")
        else:
            notes.append("no MAC in virsh domiflist")

        address = self._from_lease(cfg)
        if address:
            self.ssh_host, self.address_source = address, "dhcp lease"
        else:
            address = self._from_arp(cfg)
            if address:
                self.ssh_host, self.address_source = address, "virsh domifaddr --source arp"
            elif self.requested_host:
                self.ssh_host, self.address_source = self.requested_host, "--ssh-host"

        if not self.ssh_host:
            detail = (
                f"no address for '{self.domain}' from the DHCP lease table, ARP or "
                "--ssh-host. Run the bootstrap line in the guest console (it prints "
                "the guest's addresses), then re-run adopt, passing --ssh-host <ip> "
                "for a guest with a static address:\n"
                f"  {self.console_line}"
            )
            raise self._fail("locate", "adopt_guest_not_found", detail)
        notes.insert(0, f"{self.ssh_host} via {self.address_source}")
        self._report("locate", "done", " · ".join(notes))

    def _from_lease(self, cfg: dict) -> str | None:
        if not (self.mac and self.network):
            return None
        ok, output = vm._run(
            vm._virsh(cfg, "net-dhcp-leases", self.network), timeout=30
        )
        if not ok:
            return None
        for line in output.splitlines():
            if self.mac in line.lower():
                return _address(line)
        return None

    def _from_arp(self, cfg: dict) -> str | None:
        ok, output = vm._run(
            vm._virsh(cfg, "domifaddr", self.domain, "--source", "arp"), timeout=30
        )
        if not ok:
            return None
        for fields in _table_rows(output):
            joined = " ".join(fields)
            if self.mac and self.mac not in joined.lower():
                continue
            address = _address(joined)
            if address:
                return address
        return None

    # -- 2. wait -------------------------------------------------------------

    def phase_wait(self) -> None:
        # stderr in BOTH modes: this is operator guidance, not a result, and
        # `--json` promises that stdout holds exactly one JSON object. The
        # same line is carried in the result as `bootstrap_line`.
        print(
            "Run this once in an ELEVATED PowerShell inside the guest "
            "(skip it if you already have):\n"
            f"  {self.console_line}\n",
            file=sys.stderr,
        )
        deadline = time.monotonic() + max(1, self.wait)
        attempts = 0
        while True:
            attempts += 1
            if _tcp_open(self.ssh_host, self.ssh_port):
                self._report(
                    "wait", "done",
                    f"{self.ssh_host}:{self.ssh_port} answered after "
                    f"{attempts} probe(s)",
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._fail(
                    "wait", "adopt_ssh_timeout",
                    f"{self.ssh_host}:{self.ssh_port} did not answer within "
                    f"{self.wait}s ({attempts} probe(s)) — the bootstrap line has "
                    "not run in the guest yet, or the guest firewall blocks it",
                )
            _sleep(min(SSH_POLL_INTERVAL, remaining))

    # -- 3. install key ------------------------------------------------------

    def phase_install_key(self) -> None:
        for candidate, label in self._candidate_keys():
            ready, _error = vm._ssh_ready(self._ssh_cfg(candidate), timeout=20)
            if ready:
                self.key_path = Path(candidate)
                self._report(
                    "install_key", "skipped",
                    f"{label} at {candidate} already authenticates as "
                    f"{self.ssh_user}@{self.ssh_host}",
                )
                return

        public_key = self._ensure_keypair()
        password = self._password()
        remote = self._key_install_command(public_key)
        temp_dir = Path(tempfile.mkdtemp(dir=str(_state_dir()), prefix="adopt-askpass-"))
        try:
            os.chmod(temp_dir, 0o700)
            helper = temp_dir / "askpass.sh"
            # The helper prints ONE environment variable. The secret is never
            # written to this file, so nothing on disk holds it even between
            # the write and the `finally` removal.
            helper.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$SC_ADOPT_PASSWORD"\n',
                encoding="utf-8",
            )
            os.chmod(helper, 0o700)
            env = {
                **os.environ,
                "SSH_ASKPASS": str(helper),
                "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": os.environ.get("DISPLAY") or ":0",
                "SC_ADOPT_PASSWORD": password,
            }
            code, _out, err = _run_env(
                self._password_ssh_argv(remote), env, KEY_INSTALL_TIMEOUT
            )
        finally:
            del password
            shutil.rmtree(temp_dir, ignore_errors=True)

        if code != 0:
            self._guard_host_key("install_key", err)
            raise self._fail(
                "install_key", "adopt_key_install_failed",
                f"the password-authenticated ssh exited {code}: "
                f"{(err or '').strip()[:300]}",
            )
        ready, error = vm._ssh_ready(self._ssh_cfg(), timeout=20)
        if not ready:
            raise self._fail(
                "install_key", "adopt_key_install_failed",
                f"the key was installed but does not authenticate: {error}",
            )
        self._report(
            "install_key", "done",
            f"appended {self.key_path}.pub to administrators_authorized_keys and "
            f"%USERPROFILE%\\.ssh\\authorized_keys (existing keys untouched); "
            "ACL applied; key auth verified",
        )

    def _candidate_keys(self) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        saved_key = str(self.saved.get("ssh_key_path") or "").strip()
        if saved_key:
            candidates.append((os.path.expanduser(saved_key), "the saved block's key"))
        generated = str(self.key_path)
        if generated not in [c[0] for c in candidates] and self.key_path.is_file():
            candidates.append((generated, "the adoption key"))
        return candidates

    def _ensure_keypair(self) -> str:
        directory = _state_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._fail(
                "install_key", "adopt_key_install_failed",
                f"{directory} is not writable: {exc}",
            ) from exc
        public = Path(f"{self.key_path}.pub")
        if not (self.key_path.is_file() and public.is_file()):
            ok, output = vm._run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "subfloor-adopt",
                 "-f", str(self.key_path)],
                timeout=60,
            )
            if not ok:
                raise self._fail(
                    "install_key", "adopt_key_install_failed",
                    f"ssh-keygen failed: {output}",
                )
        try:
            os.chmod(self.key_path, 0o600)
            text = public.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise self._fail(
                "install_key", "adopt_key_install_failed",
                f"the generated key pair is unreadable: {exc}",
            ) from exc
        if not text:
            raise self._fail(
                "install_key", "adopt_key_install_failed",
                f"{public} is empty",
            )
        return text.splitlines()[0].strip()

    def _password(self) -> str:
        if self.password_file:
            try:
                raw = Path(self.password_file).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise self._fail(
                    "install_key", "adopt_key_install_failed",
                    f"--password-file could not be read: {exc}",
                ) from exc
            # Exactly one trailing newline is stripped: a password may end in
            # spaces, and `echo secret > file` is the normal way to make one.
            password = raw.removesuffix("\n").removesuffix("\r")
        elif not _stdin_is_tty():
            raise self._fail("install_key", "adopt_config_invalid", NO_TTY)
        else:
            try:
                password = getpass.getpass(
                    f"Password for {self.ssh_user}@{self.ssh_host} "
                    "(used once, never stored): "
                )
            except (EOFError, OSError) as exc:
                raise self._fail(
                    "install_key", "adopt_key_install_failed",
                    "no TTY to read the guest password from — pass "
                    "--password-file <path> instead",
                ) from exc
        if not password:
            raise self._fail(
                "install_key", "adopt_key_install_failed",
                "the guest password was empty",
            )
        return password

    def _password_ssh_argv(self, remote: str) -> list[str]:
        """One password-only ssh. BatchMode off (it would suppress the askpass
        call); pubkey off so a half-installed key cannot mask a failure."""
        return [
            "ssh",
            "-o", "BatchMode=no",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "ConnectTimeout=10",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(self.ssh_port),
            f"{self.ssh_user}@{self.ssh_host}",
            remote,
        ]

    @staticmethod
    def _key_install_command(public_key: str) -> str:
        """A cmd.exe one-liner: append (never overwrite) and fix the ACL.

        `findstr ... || echo` keeps a re-run from stacking duplicate lines.
        Both files are written: `administrators_authorized_keys` is what sshd
        reads for an Administrators-group account, and the per-user file keeps
        the host in if the account is later demoted. An Administrators-group
        SSH session runs with the elevated token, so both the write under
        `C:\\ProgramData\\ssh` and `icacls` succeed over ssh.
        """
        # SIDs, never the localized display names: `Administrators` and
        # `SYSTEM` are `Administradores` / `SISTEMA` on a Spanish Windows and
        # icacls then fails with "No mapping between account names and
        # security IDs". *S-1-5-32-544 and *S-1-5-18 are invariant.
        admin = r"C:\ProgramData\ssh\administrators_authorized_keys"
        user = r"%USERPROFILE%\.ssh\authorized_keys"
        return (
            rf'if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh" & '
            rf'findstr /C:"{public_key}" "{user}" >nul 2>&1 || echo {public_key}>>"{user}" & '
            rf'findstr /C:"{public_key}" "{admin}" >nul 2>&1 || echo {public_key}>>"{admin}" & '
            rf'icacls "{admin}" /inheritance:r /grant *S-1-5-32-544:F /grant *S-1-5-18:F'
        )

    # -- 4. harden and pin ---------------------------------------------------

    # A directive appended to the END of sshd_config lands INSIDE whatever
    # `Match` block the stock Windows file ends with (`Match Group
    # administrators`), where it applies only to that group. So: replace the
    # FIRST existing directive (later ones are dead anyway — sshd takes the
    # first), and when there is none, insert before the first `Match` line so
    # the directive sits in global scope. Only then append.
    HARDEN_SCRIPT = r"""
$path = 'C:\ProgramData\ssh\sshd_config'
if (-not (Test-Path -LiteralPath $path)) { Write-Output 'MISSING'; exit 1 }
$old = [IO.File]::ReadAllText($path)
$lines = @($old -split "`r?`n")
$out = @(); $seen = $false
foreach ($line in $lines) {
  if (-not $seen -and $line -match '^\s*#?\s*PasswordAuthentication\s') {
    $out += 'PasswordAuthentication no'; $seen = $true
  } else { $out += $line }
}
if (-not $seen) {
  $out = @(); $placed = $false
  foreach ($line in $lines) {
    if (-not $placed -and $line -match '^\s*Match\s') {
      $out += 'PasswordAuthentication no'; $placed = $true
    }
    $out += $line
  }
  if (-not $placed) { $out += 'PasswordAuthentication no' }
}
$new = ($out -join "`r`n")
if ($new -ne $old) { [IO.File]::WriteAllText($path, $new); Write-Output 'CHANGED' }
else { Write-Output 'UNCHANGED' }
""".strip()

    def phase_harden(self) -> None:
        cfg = self._ssh_cfg()
        code, out, err = _capture(
            vm._ssh_argv(cfg, _ps(self.HARDEN_SCRIPT)), timeout=HARDEN_TIMEOUT
        )
        if code != 0:
            self._guard_host_key("harden", out, err)
            raise self._fail(
                "harden", "adopt_harden_failed",
                "could not set PasswordAuthentication no in sshd_config "
                f"(exit {code}): {(out + err).strip()[:300]}",
            )
        changed = "CHANGED" in out and "UNCHANGED" not in out
        notes: list[str] = []
        if changed:
            # The restart drops THIS session; a non-zero exit here is expected
            # and says nothing about whether sshd came back.
            _capture(
                vm._ssh_argv(cfg, _ps("Restart-Service sshd -Force")),
                timeout=RESTART_SSHD_TIMEOUT,
            )
            deadline = time.monotonic() + RESTART_SETTLE_WAIT
            while not _tcp_open(self.ssh_host, self.ssh_port):
                if time.monotonic() >= deadline:
                    raise self._fail(
                        "harden", "adopt_harden_failed",
                        f"sshd did not come back on {self.ssh_host}:{self.ssh_port} "
                        f"within {RESTART_SETTLE_WAIT}s after the restart",
                    )
                _sleep(2)
            ready, error = vm._ssh_ready(cfg, timeout=20)
            if not ready:
                self._guard_host_key("harden", error or "")
                raise self._fail(
                    "harden", "adopt_harden_failed",
                    f"key auth stopped working after the sshd restart: {error}",
                )
            notes.append("PasswordAuthentication no; sshd restarted; key auth confirmed")
        else:
            notes.append("PasswordAuthentication no was already set")

        notes.append(self._reserve_address())
        pin_note, pinned_change = self._pin_host_key()
        notes.append(pin_note)
        status = "done" if (changed or pinned_change) else "skipped"
        self._report("harden", status, " · ".join(n for n in notes if n))

    def _reserve_address(self) -> str:
        """A lease address only stays put with a reservation; a static in-guest
        address is left alone. Refusal is reported, never fatal — the guest is
        already reachable at the address we hold."""
        if self.address_source != "dhcp lease" or not (self.mac and self.network):
            return ""
        cfg = self._virsh_cfg()
        ok, output = vm._run(vm._virsh(cfg, "net-dumpxml", self.network), timeout=30)
        if ok and self.mac in output.lower():
            return f"dhcp reservation for {self.mac} already on '{self.network}'"
        ok, output = vm._run(
            vm._virsh(
                cfg, "net-update", self.network, "add", "ip-dhcp-host",
                f"<host mac='{self.mac}' ip='{self.ssh_host}'/>",
                "--live", "--config",
            ),
            timeout=30,
        )
        if not ok:
            return self._warn(
                f"dhcp reservation for {self.mac} was refused "
                f"({output.strip()[:160]}) — the address may change on reboot"
            )
        return f"reserved {self.ssh_host} for {self.mac} on '{self.network}'"

    def _pin_host_key(self) -> tuple[str, bool]:
        code, out, err = _capture(
            ["ssh-keyscan", "-p", str(self.ssh_port), "-t", "ed25519,ecdsa,rsa",
             self.ssh_host],
            timeout=KEYSCAN_TIMEOUT,
        )
        keys = "\n".join(
            line.strip() for line in out.splitlines()
            if line.strip() and not line.startswith("#")
        )
        if code != 0 or not keys:
            note = self._warn(
                "ssh-keyscan produced no host key "
                f"({(err or '').strip()[:160]}) — host-key checking stays accept-new"
            )
            return note, False
        keys += "\n"
        try:
            existing = self.known_hosts.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if existing == keys:
            return f"host key already pinned in {self.known_hosts}", False
        try:
            self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
            self.known_hosts.write_text(keys, encoding="utf-8")
            os.chmod(self.known_hosts, 0o600)
        except OSError as exc:
            return self._warn(f"could not write {self.known_hosts}: {exc}"), False
        return f"host key pinned in {self.known_hosts}", True

    # -- 5. provision --------------------------------------------------------

    def phase_provision(self) -> None:
        try:
            self.profile = winbox.load(vm.repo_root())
        except winbox.WinboxConfigError as exc:
            raise self._fail(
                "provision", "adopt_provision_failed",
                f"[{exc.code}] {exc.message}",
            ) from exc
        self.workspace = str(self.profile["workspace"])
        self.mcp_port = int(self.profile["mcp_port"])

        if not self.provision:
            self._report(
                "provision", "skipped",
                "--no-provision: the guest toolchain was left exactly as it is",
            )
            return

        cfg = self._ssh_cfg()
        code, out, err = _capture(
            vm._ssh_argv(
                cfg, f'cmd /c if not exist "{self.workspace}" mkdir "{self.workspace}"'
            ),
            timeout=60,
        )
        if code != 0:
            raise self._fail(
                "provision", "adopt_provision_failed",
                f"could not create the guest workspace {self.workspace}: "
                f"{(out + err).strip()[:300]}",
            )

        payload = winbox.guest_payload(self.profile)
        script = vm.ports.ENGINE / "assets" / "winbox" / "provision.ps1"
        staged = Path(tempfile.mkdtemp(dir=str(_state_dir()), prefix="adopt-stage-"))
        try:
            config_file = staged / "winbox.json"
            config_file.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            shipped = [(script, "provision.ps1"), (config_file, "winbox.json")]
            manifest = self.profile.get("winget_manifest")
            if manifest:
                shipped.append((Path(manifest), Path(manifest).name))
            for source, name in shipped:
                target = f"{self.workspace}\\{name}"
                code, out, err = _capture(
                    vm._scp_argv(cfg, str(source), vm._guest_spec(cfg, target)),
                    timeout=SCP_TIMEOUT,
                )
                if code != 0:
                    raise self._fail(
                        "provision", "adopt_provision_failed",
                        f"scp of {name} to {target} failed: "
                        f"{(out + err).strip()[:300]}",
                    )
        finally:
            shutil.rmtree(staged, ignore_errors=True)

        remote = (
            "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
            f'"{self.workspace}\\provision.ps1"'
        )
        code, out, err = _capture(vm._ssh_argv(cfg, remote), timeout=PROVISION_TIMEOUT)
        try:
            report = winbox.parse_report(out)
        except winbox.WinboxConfigError as exc:
            raise self._fail(
                "provision", "adopt_provision_failed",
                f"[{exc.code}] {exc.message} (provision.ps1 exited {code}; "
                f"stderr: {(err or '').strip()[:200]})",
            ) from exc
        failed = [step for step in report["steps"] if not step["ok"]]
        if failed or not report["ok"]:
            detail = "; ".join(
                f"{step['name']}: {step['detail'] or 'failed'}" for step in failed
            ) or "provision.ps1 reported ok=false with no failing step"
            raise self._fail(
                "provision", "adopt_provision_failed",
                f"failed step(s) — {detail}",
            )
        names = ", ".join(step["name"] for step in report["steps"])
        self._report(
            "provision", "done",
            f"{len(report['steps'])} step(s) ok in {self.workspace}"
            + (f": {names}" if names else ""),
        )

    # -- 6. verify -----------------------------------------------------------

    def phase_verify(self) -> None:
        cfg = self._ssh_cfg()
        failures: list[str] = []
        passed: list[str] = []

        code, out, err = _capture(vm._ssh_argv(cfg, "echo ok"), timeout=30)
        if code != 0 or "ok" not in out:
            self._guard_host_key("verify", out, err)
            failures.append(f"echo ok exited {code}: {(out + err).strip()[:200]}")
        else:
            passed.append("echo ok")

        if not self.provision:
            if failures:
                raise self._fail("verify", "adopt_verify_failed", "; ".join(failures))
            self._report(
                "verify", "done",
                "echo ok over key auth (--no-provision: declared checks and the "
                "MCP listener were not verified)",
            )
            return

        profile = self.profile or {}
        for check in profile.get("checks") or []:
            code, out, err = _capture(vm._ssh_argv(cfg, check), timeout=CHECK_TIMEOUT)
            if code != 0:
                failures.append(f"check '{check}' exited {code}: {(out + err).strip()[:200]}")
            else:
                passed.append(f"check '{check}'")

        warned: list[str] = []
        if profile.get("mcp"):
            port = int(profile.get("mcp_port") or winbox.DEFAULT_MCP_PORT)
            # `@(...).Count -gt 0`, never `-ne $null`: a cmdlet that matches
            # nothing returns an EMPTY ARRAY, and an empty array compared to
            # $null under the array-comparison rules yields nothing at all,
            # which PowerShell then prints as the empty string. The count is
            # unambiguous for zero, one and many.
            probe = (
                "powershell -NoProfile -Command \"@(Get-NetTCPConnection -State "
                f"Listen -LocalPort {port} -LocalAddress 127.0.0.1 "
                "-ErrorAction SilentlyContinue).Count -gt 0\""
            )
            code, out, err = _capture(vm._ssh_argv(cfg, probe), timeout=60)
            if code != 0 or "True" not in out:
                # NOT a failure. Windows-MCP is registered as a per-user LOGON
                # task, so on a guest with no interactive desktop session it is
                # installed and correct but has never started. Adoption
                # continues; `./sc vm mcp up` reports readiness, and
                # `windows_testing` documents re-running the install over exec
                # once a console session exists.
                warned.append(self._warn(
                    f"no Windows-MCP listener on 127.0.0.1:{port} yet — the "
                    "per-user login task has not run (no desktop session). "
                    "`./sc vm mcp up` reports readiness; re-run "
                    "`windows-mcp install` over `./sc vm exec` once the "
                    "adopting account is logged in on the console"
                ))
            else:
                passed.append(f"MCP listener on 127.0.0.1:{port}")

        if failures:
            raise self._fail(
                "verify", "adopt_verify_failed",
                "; ".join(failures) + " — the vm block was NOT written",
            )
        detail = ", ".join(passed) + " all passed" if passed else "nothing to check"
        self._report("verify", "done", "; ".join([detail, *warned]))

    # -- 7. write block and broker ------------------------------------------

    def phase_write_block(self) -> None:
        block = {
            "domain": self.domain,
            "snapshot": self.snapshot,
            "libvirt_uri": self.libvirt_uri or DEFAULT_LIBVIRT_URI,
            "ssh_host": self.ssh_host,
            "ssh_user": self.ssh_user,
            "ssh_key_path": str(self.key_path),
            "ssh_port": self.ssh_port,
            "mcp_port": self.mcp_port,
            "known_hosts_path": str(self.known_hosts),
            "workspace": self.workspace,
        }
        try:
            unchanged = vm.write_block_and_confirm(block)
        except vm.BlockWriteError as exc:
            raise self._fail(
                "write_block", "adopt_block_write_failed", str(exc),
            ) from exc
        self.vm_block = block
        self._report(
            "write_block", "skipped" if unchanged else "done",
            (
                f"the saved vm block already describes '{self.domain}' at "
                f"{self.ssh_user}@{self.ssh_host}:{self.ssh_port}"
                if unchanged else
                f"vm block saved for '{self.domain}' at "
                f"{self.ssh_user}@{self.ssh_host}:{self.ssh_port} · "
                f"workspace {self.workspace}"
            ),
        )
        self._phase_broker()

    RESUME_BROKER = (
        "the vm block IS written — start the broker with ./sc vm-broker-up "
        "and re-run adopt"
    )

    def _phase_broker(self) -> None:
        ok, output = _dispatch_verb("vm-broker-up")
        if not ok:
            raise self._fail(
                "broker", "adopt_broker_failed",
                f"./sc vm-broker-up failed: {output.strip()[:300]} — "
                + self.RESUME_BROKER,
            )
        # `vm-broker-up` nohups the broker and returns immediately, so its
        # exit status only says the process was SPAWNED. The socket appears
        # afterwards; reading health before it does reported "health
        # unreadable ... No such file or directory" on a real adoption run.
        deadline = time.monotonic() + BROKER_READY_WAIT
        last = "the broker socket never appeared"
        while True:
            try:
                response = vm.broker_call("GET", "/health", None, timeout=5)
            except (vm.BrokerConnectionError, vm.BrokerTimeoutError,
                    vm.BrokerResponseError) as exc:
                last = str(exc)
            else:
                if response.get("ok") is True:
                    self._report(
                        "broker", "done", "vm-broker up · healthy"
                    )
                    return
                last = f"the broker answered unhealthy: {response}"
            if time.monotonic() >= deadline:
                break
            _sleep(BROKER_READY_INTERVAL)
        raise self._fail(
            "broker", "adopt_broker_failed",
            f"the vm-broker did not answer within {BROKER_READY_WAIT}s of "
            f"./sc vm-broker-up ({last}) — " + self.RESUME_BROKER,
        )

    # -- 8. baseline ---------------------------------------------------------

    def phase_baseline(self) -> None:
        # The block we just wrote, passed explicitly: `bake` otherwise re-reads
        # the saved config, and an install whose instance.json has never been
        # through `ports ensure` reads back nothing at all (see
        # vm.write_block_and_confirm). Adoption already holds the truth.
        result = vm.do_bake(self.snapshot, cfg=self.vm_block or None)
        if not result.get("ok"):
            raise self._fail(
                "baseline", "adopt_baseline_failed",
                f"the baseline snapshot was not taken: {result.get('output')}",
            )
        self._report(
            "baseline", "done",
            f"{result.get('output')} (an existing '{self.snapshot}' is always "
            "replaced, never stacked)",
        )

    # -- the run -------------------------------------------------------------

    def run(self) -> dict:
        try:
            # Every phase that stages a file wants this directory; create it
            # once so a skipped key phase cannot leave provisioning without it.
            _state_dir().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _error(
                "adopt_config_invalid",
                f"{_state_dir()} could not be created: {exc}", [],
            )
        try:
            self.phase_locate()
            self.phase_wait()
            self.phase_install_key()
            self.phase_harden()
            self.phase_provision()
            self.phase_verify()
            self.phase_write_block()
            self.phase_baseline()
        except AdoptFailure as failure:
            return _error(
                failure.code, failure.message, self.phases,
                warnings=self.warnings, bootstrap_line=self.console_line,
            )
        result = {
            "domain": self.domain,
            "phases": self.phases,
            "vm": self.vm_block,
            # The guest console line is guidance, so it is printed on stderr
            # and never on stdout - but a `--json` caller still needs it, so
            # it rides in the result instead.
            "bootstrap_line": self.console_line,
            "warnings": list(self.warnings),
            "next_steps": [
                "./sc vm start",
                "./sc vm status",
                (
                    "grant the remote_seats and windows_testing skills to the "
                    "shells that will use the guest"
                ),
            ],
        }
        value = vm.operation_success(OPERATION, result)
        # The spec names `phases` and `vm` at the TOP level of the --json
        # object; the engine's stable envelope carries them under `result`.
        # Both are present so neither contract is broken.
        value["phases"] = self.phases
        value["vm"] = self.vm_block
        value["warnings"] = list(self.warnings)
        value["bootstrap_line"] = self.console_line
        return value


def _error(code: str, message: str, phases: list[dict], *,
           warnings: list[str] | None = None,
           bootstrap_line: str | None = None) -> dict:
    details = {
        "phases": phases,
        "warnings": list(warnings or []),
        "bootstrap_line": bootstrap_line or "",
    }
    value = vm.operation_error(OPERATION, code, message, details)
    value["phases"] = phases
    value["warnings"] = list(warnings or [])
    value["bootstrap_line"] = bootstrap_line or ""
    return value


# -- entry point -------------------------------------------------------------

def run_adopt(*, domain: str, ssh_user: str | None = None,
              ssh_host: str | None = None, snapshot: str = DEFAULT_SNAPSHOT,
              libvirt_uri: str | None = DEFAULT_LIBVIRT_URI,
              password_file: str | None = None, bootstrap_url: str | None = None,
              provision: bool = True, wait: int = DEFAULT_SSH_WAIT) -> dict:
    """Adopt a libvirt Windows domain. Returns the standard result envelope."""
    if os.environ.get("SC_SANDBOX"):
        return _error(
            "adopt_sandboxed",
            "adopt refuses to run in the sandbox — it needs virsh, ssh-keygen, "
            "scp and the operator's TTY, which live on the HOST. Ask the "
            f"operator to run: ./sc vm adopt --domain {domain}",
            [],
        )
    if not str(domain or "").strip():
        return _error("adopt_config_invalid", "--domain is required", [])
    saved = vm.read() or {}
    user = str(ssh_user or "").strip() or str(saved.get("ssh_user") or "").strip()
    if not user:
        return _error(
            "adopt_config_invalid",
            "--ssh-user is required: this fork has no saved vm block to take the "
            "guest account from. Pass the local administrator the bootstrap line "
            "reported in the guest.",
            [],
        )
    if not vm._valid_resource_name(snapshot):
        return _error(
            "adopt_config_invalid",
            "snapshot name must match [a-z0-9][a-z0-9-]{0,31}",
            [],
        )
    if (not password_file and not str(saved.get("ssh_key_path") or "").strip()
            and not _stdin_is_tty()):
        # `getpass` falls back to reading stdin WITH ECHO when there is no
        # terminal, which would put the guest password in a CI log or a pipe.
        # Refuse BEFORE locating and waiting rather than fifteen minutes in.
        #
        # A saved `ssh_key_path` is the exemption: that is the re-run path,
        # where the key phase skips and no password is ever read. `_password`
        # re-checks anyway, so a saved key that has stopped working still
        # refuses rather than echoing.
        return _error(
            "adopt_config_invalid", NO_TTY, [],
        )
    return Adoption(
        domain=domain, ssh_user=user, snapshot=snapshot, libvirt_uri=libvirt_uri,
        ssh_host=ssh_host, password_file=password_file,
        bootstrap_url=bootstrap_url, provision=provision, wait=wait,
        saved=saved,
    ).run()


_MARK = {"done": "✓", "skipped": "-", "failed": "✗"}


def human_report(value: dict) -> str:
    """The operator-facing rendering of one adoption run."""
    phases = value.get("phases") or []
    lines = [
        f"{_MARK.get(phase['status'], '?')} {phase['name']:<12} "
        f"{phase['status']:<8} {phase['detail']}"
        for phase in phases
    ]
    if not value.get("ok"):
        error = value["error"]
        lines.append(f"✗ adopt [{error['code']}]: {error['message']}")
        return "\n".join(lines)
    result = value["result"]
    block = result.get("vm") or {}
    lines.append("")
    warnings = result.get("warnings") or []
    lines.append(
        f"VM adopted: {result['domain']} · baseline '{block.get('snapshot')}' · "
        "guest powered off"
        + (f" · {len(warnings)} warning(s)" if warnings else "")
    )
    for warning in warnings:
        lines.append(f"  warning: {warning}")
    lines.append("Next: " + " · ".join(result["next_steps"]))
    return "\n".join(lines)
