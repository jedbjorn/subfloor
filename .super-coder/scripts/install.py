#!/usr/bin/env python3
"""Install super-coder into a fork — first-launch bootstrap.

Run once, in a host repo that has just pulled the engine
(`git checkout super-coder/main -- .super-coder sc`). It takes that repo
from "engine present" to "a team you can launch":

    1. Guard   — refuse to run in the super-coder SOURCE repo, or on a fork that
                 is already installed (both would destroy content). --force skips.
    2. Require — python3 + sqlite3 (+ a heads-up if git/curl missing, and a
                 docker preflight for the sandbox run path — advisory, not fatal).
    3. Harness — ensure claude + opencode + codex + vibe + kimi are installed (official native
                 installers, no npm); pick the launch default → instance.json.
    4. Strip   — super-coder's own per-instance content; a fork inherits the
                 SYSTEM (schema + skill catalogue + render chain), never the memory.
    5. Build   — the system DB (schema + migrations; no per-instance content yet).
    6. Seed    — the fork's first user + starting TEAM (delegates to init_fork:
                 two planners, four dev, two reviewers, an admin, and the singleton
                 cartographer). The designated primary alone receives CC lineage +
                 a genesis seed; roster/operational shells are role-only. Shells
                 ship pre-named, so install asks only for a username; no shell-naming
                 interview.
    7. Persist — `./sc snapshot` (serialize the team) + `./sc render` (flat _sc).
    8. Done    — print how to launch.

Usage:
    ./sc install                      # interactive (prompts for your username only)
    python3 .super-coder/scripts/install.py [init_fork flags] [--force]
        e.g. … --username Sam         # fully non-interactive
        # The team ships pre-named; per-shell overrides (--name/--flavor/…) are
        # optional and never prompted — see init_fork.py.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
PY = sys.executable
IS_MAC = platform.system() == "Darwin"  # guidance arms differ (colima/brew vs systemd/apt)


def _platform_identity() -> tuple[str, str, str, str]:
    """Return the kernel and os-release identity without guessing a distro."""
    kernel = platform.system()
    release = "/etc/os-release"
    fields: dict[str, str] = {}
    try:
        with open(release, encoding="utf-8") as stream:
            for line in stream:
                key, separator, value = line.rstrip("\n").partition("=")
                if separator and key in {"ID", "ID_LIKE", "VERSION_ID"}:
                    parsed = _os_release_value(value)
                    if parsed is None:
                        fields.clear()
                        break
                    fields[key] = parsed
    except (OSError, UnicodeError):
        pass
    return (
        kernel,
        fields.get("ID", ""),
        fields.get("ID_LIKE", ""),
        fields.get("VERSION_ID", ""),
    )


def _os_release_value(value: str) -> str | None:
    if value[:1] in {'"', "'"} or value[-1:] in {'"', "'"}:
        quote = value[:1] or value[-1:]
        if len(value) < 2 or value[0] != quote or value[-1] != quote:
            return None
        return value[1:-1]
    return value


def _is_wsl() -> bool:
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as stream:
            runtime = stream.read().lower()
    except OSError:
        runtime = ""
    return bool(
        "microsoft" in runtime
        or "wsl" in runtime
        or os.environ.get("WSL_DISTRO_NAME")
        or os.environ.get("WSL_INTEROP")
    )


def require_supported_host() -> None:
    """Refuse unsupported hosts before installer imports or repository writes."""
    kernel, distro_id, distro_like, version_id = _platform_identity()
    supported = not _is_wsl() and kernel == "Linux" and (
        (distro_id == "ubuntu" and version_id == "26.04")
        or (distro_id == "fedora" and version_id == "44")
        or distro_id == "arch"
        or "arch" in distro_like.split()
    )
    if supported:
        return
    raise SystemExit(
        "✗ subfloor refused: unsupported host.\n"
        f"  detected kernel: {kernel or 'unknown'}\n"
        "  detected distribution: "
        f"ID={distro_id or 'unknown'}; ID_LIKE={distro_like or 'unknown'}; "
        f"VERSION_ID={version_id or 'unknown'}\n"
        "  supported hosts: Ubuntu LTS, Fedora stable, Arch-compatible Linux.\n"
        "  Create a supported Linux VM, keep the checkout on the guest filesystem, "
        "then run ./sc install inside the guest.\n"
        "  The rejected command was not run and no native compatibility path exists."
    )


# Direct execution reaches this module before `main`, so enforce the boundary
# before loading engine modules that could otherwise create local bytecode. An
# imported installer remains inspectable by tests and maintenance tooling; its
# action entry point enforces the same boundary below.
if __name__ == "__main__":
    require_supported_host()

sys.path.insert(0, str(ENGINE / "scripts"))
import callable_floor  # noqa: E402
import engine_manifest  # noqa: E402
import global_pointer  # noqa: E402
import ports as ports_mod  # noqa: E402


# --- make-alias wiring (shared by install + update) -------------------------
ALIASES_INCLUDE = "-include .super-coder/aliases.mk"
INSTALLER_MAKEFILE = (
    "# Fork Makefile — super-coder convenience aliases (make dos-e / dos-enter).\n"
    "# Every target is dos--prefixed; add your own targets below the include.\n"
    f"{ALIASES_INCLUDE}\n"
)
APPENDED_ALIASES_BLOCK = (
    "\n# super-coder convenience aliases (designs-OS 'dos-' command standard).\n"
    "# Appended by ./sc; every target is dos--prefixed so it can't collide with\n"
    "# this Makefile's own targets. Delete this line to opt out — `./sc <cmd>`\n"
    "# stays equivalent.\n"
    f"{ALIASES_INCLUDE}\n"
)
# Matches an existing include of the alias file in any form: hard `include` or
# soft `-include`, with arbitrary surrounding whitespace.
_ALIASES_RE = re.compile(r"^\s*-?include\s+\.super-coder/aliases\.mk\s*$", re.M)


def wire_make_aliases(repo_root: Path | None = None) -> str:
    """Ensure the fork's Makefile pulls in the engine's `dos-*` aliases.

    The house `dos-` prefix is collision-proof by design — every alias target is
    `dos-`prefixed — so wiring is safe to script rather than leave to the
    operator. A fork almost always already has its own Makefile; #13 ("never
    clobber a host Makefile") forbids *overwriting* it, not *appending* a single
    additive, non-colliding `-include` line. So:

      - no Makefile      → write a one-line one;
      - Makefile present → append the include if missing, else leave it alone.

    `-include` (not hard `include`) so a not-yet-materialized engine (fresh fork
    clone before the first `./sc update`) is a silent no-op, never a fatal `make`
    error. Idempotent — safe to call on every install AND every update. Returns a
    one-line status for the caller to print.
    """
    mk = (repo_root or REPO_ROOT) / "Makefile"
    if not mk.exists():
        mk.write_text(INSTALLER_MAKEFILE)
        return "wrote Makefile (-include .super-coder/aliases.mk) → `make dos-e` works"
    text = mk.read_text()
    if _ALIASES_RE.search(text):
        return "Makefile already wired (-include .super-coder/aliases.mk) — left as-is"
    sep = "" if text.endswith("\n") else "\n"
    mk.write_text(
        text + sep + APPENDED_ALIASES_BLOCK
    )
    return "appended -include .super-coder/aliases.mk to existing Makefile → `make dos-e` works"

# super-coder's own per-instance content — present in a freshly-pulled fork
# because the git checkout brought it along. A fork must not inherit it.
STRIP = [
    REPO_ROOT / ".sc-state" / "content.sql",
    ENGINE / "snapshot" / "content.sql",  # legacy pre-B7 location (one-release)
    ENGINE / "assets" / "seed" / "super-coder-founding-spec.md",
]


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def report_host_runtime(*, report: bool = True) -> None:
    """Validate and report the exact interpreter before installer mutation."""
    version = platform.python_version()
    executable = str(Path(sys.executable).resolve())
    if sys.version_info < (3, 9):
        raise SystemExit(
            f"install: Python 3.9+ required; selected {executable} reports {version}.\n"
            f"  recovery: {'brew install python; ' if IS_MAC else ''}"
            "export SC_PYTHON=/absolute/path/to/python3"
        )
    try:
        import sqlite3
    except ImportError:
        raise SystemExit(
            f"install: selected Python {executable} ({version}) cannot import sqlite3.\n"
            f"  recovery: {'brew install python; ' if IS_MAC else ''}"
            "export SC_PYTHON=/absolute/path/to/python3"
        )
    if report:
        print(
            f"  python    {executable} · {version} · "
            f"sqlite3 {sqlite3.sqlite_version} ✓"
        )


def run_critical_phase(
    name: str,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Stream one required installer phase and stop truthfully on failure."""
    step(name)
    sys.stdout.flush()
    sys.stderr.flush()
    completed = subprocess.run(argv, env=env, check=False)
    if completed.returncode == 0:
        return
    print(f"install: critical phase failed: {name}", file=sys.stderr)
    print(f"  interpreter: {Path(PY).resolve()}", file=sys.stderr)
    print(f"  argv: {shlex.join(argv)}", file=sys.stderr)
    print(f"  exit code: {completed.returncode}", file=sys.stderr)
    print("  repair the reported cause, then retry: ./sc install", file=sys.stderr)
    raise SystemExit(completed.returncode)


# Repo basenames that identify the SOURCE repo (canonical set — update.py and
# map_repo.py key off this too). Both names stay valid through the
# super-coder → subfloor rename: GitHub redirects the old URL, so either can
# appear in a checkout's origin. Getting this wrong is not cosmetic — a source
# repo misread as a fork gets its tracked engine `git rm --cached`-ed by the
# B7 untrack migration (this fired on the dogfood repo the day of the rename).
SOURCE_REPO_NAMES = ("super-coder", "subfloor")

VISUAL_QA_TEMPLATE_TARGETS = {
    "subfloor-visual-qa.yml": Path(".github/workflows/subfloor-visual-qa.yml"),
    "visual-qa.example.json": Path(".sc-state/visual-qa.example.json"),
}

# Repo-local surfaces emitted or wholly owned by an installed engine.  Teardown
# imports this inventory so install and remove cannot quietly disagree about
# which generated paths belong to subfloor.  Mixed host files (Makefile,
# .gitignore, shared/) are handled surgically instead.
GENERATED_INSTALL_PATHS = (
    Path("CLAUDE.md"),
    Path("AGENTS.md"),
    Path("opencode.json"),
    Path(".claude/settings.local.json"),
    Path(".codex/hooks.json"),
    Path(".claude/skills"),
    Path(".agents/skills"),
    Path(".opencode/skills"),
    Path("roadmap_sc.md"),
    Path("docs_sc"),
    Path("specs_sc"),
    Path("skills_sc"),
)


def origin_basename() -> str | None:
    p = sh("git", "-C", str(REPO_ROOT), "remote", "get-url", "origin")
    if p.returncode != 0:
        return None
    return p.stdout.strip().rstrip("/").split("/")[-1].removesuffix(".git")


def is_source_repo() -> bool:
    """The source repo's origin is …/super-coder or …/subfloor. A fork's origin
    is its own repo (the engine upstream is a separate, differently-named
    remote)."""
    return origin_basename() in SOURCE_REPO_NAMES


def work_repo() -> str | None:
    """Return this install's declared work project, if it has one.

    An external-work install keeps its engine, local map database, and hooks in
    the home repo but directs shells to maintain a different project.
    """
    cfg = ENGINE / "instance.json"
    try:
        raw = (json.loads(cfg.read_text()).get("work_repo") or "").strip()
    except (OSError, json.JSONDecodeError):
        return None
    return str(Path(raw).expanduser()) if raw else None


def seed_visual_qa_files(
    repo_root: Path = REPO_ROOT,
    template_root: Path | None = None,
    *,
    source_repo: bool | None = None,
) -> list[Path]:
    """Seed Visual QA's fork-owned workflow and inactive example config.

    Existing files are always preserved. The source repository owns the
    templates but must never receive the fork-facing copies.
    """
    source = source_repo if source_repo is not None else is_source_repo()
    if source:
        return []

    templates = template_root or ENGINE / "templates" / "fork"
    written: list[Path] = []
    for template_name, relative_target in VISUAL_QA_TEMPLATE_TARGETS.items():
        target = repo_root / relative_target
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(templates / template_name, target)
        written.append(relative_target)
    return written


def already_installed() -> bool:
    if not ports_mod.CONFIG.exists():
        return False
    try:
        return "installed_at" in json.loads(ports_mod.CONFIG.read_text())
    except json.JSONDecodeError:
        return False


def starting_team_exists() -> bool:
    """Whether an incomplete prior install already persisted its seeded team."""
    db = ENGINE / "shell_db.db"
    if not db.exists():
        return False
    import sqlite3

    try:
        with sqlite3.connect(db) as con:
            return bool(con.execute(
                "SELECT EXISTS(SELECT 1 FROM shells WHERE COALESCE(is_deleted,0)=0)"
            ).fetchone()[0])
    except sqlite3.Error:
        return False


def detect_harness() -> str | None:
    for h in ("claude", "opencode", "codex", "vibe", "kimi"):
        if _harness_installed(h):
            return h
    return None


# Official NATIVE installers — no npm. Claude Code dropped npm as the primary
# path (https://code.claude.com/docs/en/setup); opencode + codex + vibe + kimi ship
# their own scripts too. Pipe-to-shell, latest version. vibe installs via uv (its
# script checks for / uses `uv tool install mistral-vibe`); a missing uv makes its
# install fail best-effort, same as any other harness. kimi (Kimi Code) is a
# single binary dropped into its own config home, ~/.kimi-code/bin.
HARNESS_INSTALL = {
    "claude":   "curl -fsSL https://claude.ai/install.sh | bash",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
    "codex":    "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "vibe":     "curl -LsSf https://mistral.ai/vibe/install.sh | bash",
    "kimi":     "curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash",
}
# Where each installer drops its binary. Checked post-install because the new
# bin dir is NOT on this process's PATH — the installer edits shell rc files,
# which only a fresh shell picks up. shutil.which alone would miss a just-
# installed CLI. Codex's native installer drops a launcher into ~/.local/bin,
# but its standalone package lives under ~/.codex; the Dockerfile relocates
# that executable because the runtime mounts ~/.codex for durable state.)
HARNESS_BIN = {
    "claude":   Path.home() / ".local" / "bin" / "claude",
    "opencode": Path.home() / ".opencode" / "bin" / "opencode",
    "codex":    Path.home() / ".local" / "bin" / "codex",
    "vibe":     Path.home() / ".local" / "bin" / "vibe",
    "kimi":     Path.home() / ".kimi-code" / "bin" / "kimi",
}


def _harness_installed(name: str) -> bool:
    return bool(shutil.which(name)) or HARNESS_BIN.get(name, Path("/nonexistent")).exists()


# ── Harness epoch (sandbox harness freshness) ────────────────────────────────
# The functions above install harnesses on THIS machine's $HOME. That is the
# right thing on the no-docker path, where the host IS the runtime — and a no-op
# for shells on the docker path, where the harness binaries are baked into the
# `super-coder-sandbox` image. The container mounts harness state homes
# (~/.claude, ~/.codex, …), but an image launcher must never resolve a binary
# from those mounts. Binaries cannot be host-selected: they are host-ABI
# artifacts (a darwin binary is fatal in a linux container, vibe's entry point
# carries an absolute shebang into a host uv interpreter, glibc baselines differ
# across the distros we support), which is why the Dockerfile bakes them.
#
# Baking froze them: docker serves the installer RUNs from layer cache forever,
# so `./sc launch|restart` — which DO run `docker build` — could never make a
# harness newer, and `docker rm -f` on every launch discards any in-container
# update. A claude one release behind Opus 5 therefore stayed one release behind
# through an update, a harness update, and a restart.
#
# The epoch is that layer's cache key. `./sc restart`, `./sc update`, and
# `./sc update-harnesses` roll it; `./sc build` passes it as
# SC_HARNESS_EPOCH; the Dockerfile references it inside both harness RUNs, so a
# changed value re-runs the installers (which resolve "latest" themselves —
# the epoch is an expiry token, never a version pin).
#
# MACHINE-scoped, not per-repo: every fork on a host shares the image tag, so
# the harness layer is a machine fact and a per-repo file would let one fork
# roll an epoch its neighbours never see. Every explicit refresh gets a unique
# UTC token: a normal restart means "ask every installer for current now", even
# after another refresh earlier the same day. Plain launch/build remain
# cache-warm; restart --no-build deliberately pins the existing image. The path
# follows the engine's existing host-state idiom (XDG_CONFIG_HOME).
HARNESS_EPOCH_UNSET = "0"  # the Dockerfile's default — "never rolled here"


def harness_epoch_path() -> Path:
    """Where the rolled epoch is stored. SC_HARNESS_EPOCH_FILE overrides (tests)."""
    override = os.environ.get("SC_HARNESS_EPOCH_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "super-coder" / "harness-epoch"


def harness_epoch() -> str:
    """The stored epoch, or "0" when never rolled — so an untouched machine
    builds exactly the image an un-instrumented build would have produced."""
    try:
        value = harness_epoch_path().read_text().strip()
    except OSError:
        return HARNESS_EPOCH_UNSET
    return value or HARNESS_EPOCH_UNSET


def roll_harness_epoch() -> str:
    """Give the image's harness layers a fresh cache key and return it.

    Each explicit refresh must be unique: restart is the operator's convergence
    boundary, so a second restart on the same day still asks the official
    installers for current releases. Microseconds make sequential calls unique
    while keeping the image label human-readable."""
    value = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = harness_epoch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    return value


# ── Harness install progress ─────────────────────────────────────────────────
# A real %-bar isn't possible: the work is third-party installer scripts
# (curl | bash) whose duration + byte counts we don't know. Instead, run each
# with a live spinner + elapsed seconds so it never looks frozen, capture the
# installer's (noisy) output, and surface it only on failure. TTY-gated: under
# a pipe / CI we drop to plain "installing… / done" lines (no escape codes).

_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spin(name: str, label: str, stop: threading.Event, t0: float) -> None:
    """Animate one spinner line in place until `stop` is set. TTY only."""
    i = 0
    while not stop.is_set():
        frame = _SPIN_FRAMES[i % len(_SPIN_FRAMES)]
        elapsed = int(time.monotonic() - t0)
        sys.stdout.write(f"\r  {frame} {name:9} {label}…  {elapsed}s ")
        sys.stdout.flush()
        i += 1
        stop.wait(0.1)


def _run_harness_install(name: str, cmd: str, label: str) -> tuple[int, str, int]:
    """Run one installer with a spinner (TTY) or a plain line (non-TTY). Captures
    combined stdout+stderr (drained safely via communicate, so a chatty installer
    can't deadlock on a full pipe). Returns (rc, captured_output, elapsed_s).
    Prints no outcome line — the caller decides success and reports it."""
    tty = sys.stdout.isatty()
    t0 = time.monotonic()
    if not tty:
        print(f"  · {name:9} {label}…  ($ {cmd})", flush=True)
    proc = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    stop = threading.Event()
    spinner = None
    if tty:
        spinner = threading.Thread(target=_spin, args=(name, label, stop, t0), daemon=True)
        spinner.start()
    out, _ = proc.communicate()
    stop.set()
    if spinner:
        spinner.join()
        sys.stdout.write("\r" + " " * 48 + "\r")  # wipe the spinner line
        sys.stdout.flush()
    return proc.returncode, out or "", int(time.monotonic() - t0)


def _report_install(name: str, ok: bool, rc: int, out: str, elapsed: int,
                    done: str, cmd: str) -> None:
    """Print the per-harness outcome: a ✓ line on success, or a ✗ line plus the
    tail of the captured installer output (the error usually lands last) + a
    by-hand retry hint on failure."""
    if ok:
        print(f"  ✓ {name:9} {done}   {elapsed}s")
        return
    print(f"  ✗ {name:9} failed (rc={rc}) — installer output:")
    for line in out.strip().splitlines()[-20:]:
        print(f"  | {line}")
    print(f"    ↪ retry by hand: {cmd}")


def update_harnesses() -> dict[str, str]:
    """Force-update all harness CLIs by re-running their official native
    installers regardless of whether they're already present. Unlike
    ensure_harnesses(), never skips an installed harness — the installers
    are idempotent and self-update to latest."""
    status: dict[str, str] = {}
    have_curl = bool(shutil.which("curl"))
    for name, cmd in HARNESS_INSTALL.items():
        if not have_curl:
            print(f"  ⚠ {name:9} curl unavailable — update by hand: {cmd}")
            status[name] = "no-curl"
            continue
        present = _harness_installed(name)
        label = "updating" if present else "installing"
        done = "updated" if present else "installed"
        rc, out, elapsed = _run_harness_install(name, cmd, label)
        ok = rc == 0
        _report_install(name, ok, rc, out, elapsed, done, cmd)
        status[name] = done if ok else "failed"
    global_pointer.write_global_pointers()
    return status


def ensure_harnesses() -> dict[str, str]:
    """Install any missing harness CLI via its official native installer (no
    npm) — claude + opencode + codex + vibe + kimi, so a fork can launch and run any. Best
    effort: a failed install warns and continues (the harness is only needed at
    launch and can be installed by hand later). Returns {name: status}."""
    status: dict[str, str] = {}
    have_curl = bool(shutil.which("curl"))
    for name, cmd in HARNESS_INSTALL.items():
        if _harness_installed(name):
            print(f"  ✓ {name:9} already installed")
            status[name] = "present"
            continue
        if not have_curl:
            print(f"  ⚠ {name:9} missing, and curl is unavailable — install by hand: {cmd}")
            status[name] = "no-curl"
            continue
        rc, out, elapsed = _run_harness_install(name, cmd, "installing")
        ok = rc == 0 and _harness_installed(name)
        _report_install(name, ok, rc, out, elapsed, "installed", cmd)
        status[name] = "installed" if ok else "failed"
    fresh = [n for n, s in status.items() if s == "installed"]
    if fresh:
        dirs = sorted({str(HARNESS_BIN[n].parent) for n in fresh})
        print(f"  ↪ new CLIs live in {', '.join(dirs)} — open a NEW shell (or update "
              f"PATH) before `./sc launch`, since this shell's PATH predates them.")
    global_pointer.write_global_pointers()
    return status


# ── Docker preflight (the default run mode is a sandbox container) ────────────
# Advisory only: real docker setup needs root + a re-login, so install GUIDES with
# the right commands for the state it finds, never mutates. Mirrors the git/curl
# warnings — a missing/under-configured docker is not fatal, because the no-docker
# escape hatch (`./sc serve` + `./sc boot`) still runs the shell on the host.

def docker_status() -> dict:
    """Docker availability + mode. 'absent' (no CLI) · 'no-daemon' (CLI but no
    reachable daemon / no socket access) · 'rootless' · 'rootful'."""
    if not shutil.which("docker"):
        return {"state": "absent"}
    p = sh("docker", "info", "--format", "{{.SecurityOptions}}")
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()
        return {"state": "no-daemon", "detail": tail[-1] if tail else ""}
    return {"state": "rootless" if "rootless" in (p.stdout or "").lower() else "rootful"}


def report_docker() -> dict:
    """Print the docker preflight block for the sandbox run path. Returns status."""
    st = docker_status()
    user = os.environ.get("USER", "$USER")
    state = st["state"]
    if state == "rootless":
        print("  docker    ✓ rootless — the default, nothing to set up. The sandbox runs")
        print("            the container as root, which under rootless maps to YOU, so repo")
        print("            writes come out yours (no phantom-uid problem). Only wart: claude")
        print("            runs as root inside (its --dangerously-skip-permissions flag is")
        print("            blocked — the sandbox replaces the need for it).")
    elif state == "rootful":
        print("  docker    ✓ rootful — also fine: 1:1 uid bind-mounts, harness runs as you")
        print("            (no claude-as-root wart). Either mode works; duser() adapts.")
    elif state == "no-daemon":
        print("  docker    ⚠ CLI present but no daemon reachable. Start one:")
        if IS_MAC:
            print("            colima  : colima start   (or launch Docker Desktop)")
        else:
            print(f"            rootful : sudo usermod -aG docker {user} && sudo systemctl enable --now docker.socket  (re-login)")
            print("            rootless: dockerd-rootless-setuptool.sh install && systemctl --user enable --now docker")
        if st.get("detail"):
            print(f"            ({st['detail']})")
    else:  # absent
        print("  docker    ⚠ not found — the default run mode is a sandbox container.")
        if IS_MAC:
            print("            Install it (e.g. colima: brew install colima docker && colima start),")
            print("            then `./sc doctor`.")
        else:
            print("            Install it (e.g. Arch: sudo pacman -S docker), then `./sc doctor`.")
        print("            Or run without docker via the escape hatch: ./sc serve + ./sc boot")
    return st


# ── Harness login preflight ──────────────────────────────────────────────────
# The sandbox mounts your host harness creds in (binaries are baked in the image;
# auth is host-mounted so you don't re-login on every restart). So a one-time
# host login is what makes those cred files exist. We detect + guide; the login
# itself is an interactive oauth flow we can't script.

def harness_login_status() -> dict:
    """Heuristic 'logged in?' per harness, from the host cred files the sandbox
    mounts. claude stores an oauthAccount in ~/.claude.json; opencode writes
    ~/.local/share/opencode/auth.json on `auth login`."""
    claude = False
    cj = Path.home() / ".claude.json"
    if cj.exists():
        try:
            claude = "oauthAccount" in json.loads(cj.read_text())
        except (json.JSONDecodeError, OSError):
            claude = False
    oc = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    opencode = oc.exists() and oc.stat().st_size > 2
    # codex writes ~/.codex/auth.json on ChatGPT/API login (unless using the
    # system keyring, which we can't probe — false negative is safe: it only
    # downgrades the ✓ to a ⚠ reminder).
    cx = Path.home() / ".codex" / "auth.json"
    codex = cx.exists() and cx.stat().st_size > 2
    return {"claude": claude, "opencode": opencode, "codex": codex}


def report_logins() -> dict:
    """Print the harness-login preflight. The sandbox can't run a harness you
    haven't logged into; the login lives on the host and gets mounted in."""
    st = harness_login_status()
    if st["claude"]:
        print("  claude    ✓ logged in")
    else:
        print("  claude    ⚠ not logged in — run `claude` then `/login` once on the host")
        print("            (creates ~/.claude.json, which the sandbox mounts in).")
    if st["opencode"]:
        print("  opencode  ✓ logged in")
    else:
        print("  opencode  ⚠ not logged in — run `opencode auth login` once on the host")
        print("            (creates ~/.local/share/opencode/auth.json, mounted in).")
    if st["codex"]:
        print("  codex     ✓ logged in")
    else:
        print("  codex     ⚠ not logged in — run `codex` then sign in with ChatGPT once on the host")
        print("            (creates ~/.codex/auth.json, which the sandbox mounts in).")
    return st


# Ignore lines a fork needs — the rebuilt/derived artifacts. The git checkout
# that brings the engine in doesn't carry subfloor's .gitignore, so install and
# update own one exact sentinel-bounded range in the mixed host file.
_GITIGNORE_BEGIN = "# >>> subfloor managed ignores >>>"
_GITIGNORE_END = "# <<< subfloor managed ignores <<<"
_LEGACY_GITIGNORE_MARKER = "# super-coder — rebuilt/derived; never commit"
_LEGACY_GITIGNORE_TOPUP = "# super-coder — engine ignore rules added by `./sc update`"
_LEGACY_GITIGNORE_PATTERNS = {
    "/.super-coder/shell_db.db",
    "/.super-coder/shell_db.db-wal",
    "/.super-coder/shell_db.db-shm",
    "/.super-coder/instance.json",
}
_GITIGNORE_MARKER = _GITIGNORE_BEGIN  # compatibility for callers/tests
_GITIGNORE_BLOCK = f"""{_GITIGNORE_BEGIN}
# The engine is a materialized, gitignored DEPENDENCY (B7) — fetched from
# upstream, refreshed by `./sc update`, never committed to the fork. Your project
# is everything ELSE in this repo.
/.super-coder/
# Boot artifacts + per-shell skill render — rebuilt at launch from the DB.
/CLAUDE.md
/AGENTS.md
/opencode.json
/.claude/skills/
/.agents/skills/
/.opencode/skills/
# Engine-managed harness config re-emitted each launch (per-harness branch-guard
# hook); kept apart from a fork's own tracked config (claude settings.json /
# codex config.toml).
/.claude/settings.local.json
/.codex/hooks.json
# Shell worktrees — one per shell, linked inside the repo root.
/.sc-worktrees/
# The engine pin is tracked. Every generated instance artifact is local-only.
/.sc-state/engine.ref.prev
/.sc-state/content.sql
/.sc-state/map_content.sql
/.sc-state/map.config.json
/.sc-state/skills_retired.json
/.sc-state/local/
/roadmap_sc.md
/docs_sc/
/specs_sc/
/skills_sc/
# Map DB — derived cache of the repo (dr_*), rebuilt by `./sc map`.
/.sc-state/map.db
/.sc-state/map.db-wal
/.sc-state/map.db-shm
/.sc-state/db_backups/
{_GITIGNORE_END}
"""


def _required_ignores() -> list[str]:
    """The ignore patterns inside the canonical managed range."""
    return [ln.strip() for ln in _GITIGNORE_BLOCK.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


class GitignoreError(ValueError):
    """The mixed host file cannot be changed without guessing ownership."""


def _gitignore_lines(text: str) -> list[tuple[int, int, int, str]]:
    """Return (line number, start, end, content) without normalizing bytes."""
    result = []
    offset = 0
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        content = raw
        if content.endswith("\r\n"):
            content = content[:-2]
        elif content.endswith(("\n", "\r")):
            content = content[:-1]
        end = offset + len(raw)
        result.append((number, offset, end, content))
        offset = end
    return result


def _sentinel_span(text: str) -> tuple[int, int] | None:
    lines = _gitignore_lines(text)
    begins = [line for line in lines if line[3] == _GITIGNORE_BEGIN]
    ends = [line for line in lines if line[3] == _GITIGNORE_END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1:
        begin_lines = ", ".join(str(line[0]) for line in begins) or "none"
        end_lines = ", ".join(str(line[0]) for line in ends) or "none"
        raise GitignoreError(
            "malformed subfloor managed ignore sentinels: "
            f"begin lines {begin_lines}; end lines {end_lines}; "
            "expected exactly one ordered pair"
        )
    begin, end = begins[0], ends[0]
    if begin[0] >= end[0]:
        raise GitignoreError(
            "malformed subfloor managed ignore sentinels: "
            f"begin line {begin[0]} follows end line {end[0]}"
        )
    return begin[1], end[2]


def _legacy_span(text: str) -> tuple[int, int] | None:
    lines = _gitignore_lines(text)
    initial = [i for i, line in enumerate(lines) if line[3] == _LEGACY_GITIGNORE_MARKER]
    topups = [i for i, line in enumerate(lines) if line[3] == _LEGACY_GITIGNORE_TOPUP]
    if not initial and not topups:
        return None
    if len(initial) != 1:
        locations = ", ".join(str(lines[i][0]) for i in initial) or "none"
        raise GitignoreError(
            "ambiguous legacy subfloor ignore range: "
            f"initial marker lines {locations}; expected exactly one"
        )
    start_i = initial[0]
    before = [i for i in topups if i < start_i]
    if before:
        locations = ", ".join(str(lines[i][0]) for i in before)
        raise GitignoreError(
            "ambiguous legacy subfloor ignore range: "
            f"update marker before initial marker at lines {locations}"
        )

    final_marker_i = topups[-1] if topups else start_i
    patterns = set(_required_ignores()) | _LEGACY_GITIGNORE_PATTERNS
    ambiguous = []
    seen = set()
    for line in lines[start_i + 1:final_marker_i]:
        content = line[3].strip()
        if content in patterns:
            seen.add(content)
        elif content and not content.startswith("#"):
            ambiguous.append((line[0], line[3]))

    last_pattern = None
    pending_ambiguous = []
    for line in lines[final_marker_i + 1:]:
        content = line[3].strip()
        if content in patterns:
            if content in seen:
                break
            seen.add(content)
            ambiguous.extend(pending_ambiguous)
            pending_ambiguous.clear()
            last_pattern = line
            continue
        if content and not content.startswith("#"):
            pending_ambiguous.append((line[0], line[3]))
    if ambiguous:
        detail = "; ".join(f"line {number}: {content}" for number, content in ambiguous)
        raise GitignoreError(f"ambiguous legacy subfloor ignore range: {detail}")
    if last_pattern is None:
        marker_line = lines[final_marker_i][0]
        raise GitignoreError(
            "ambiguous legacy subfloor ignore range: "
            f"marker at line {marker_line} has no recognized managed pattern"
        )
    return lines[start_i][1], last_pattern[2]


def _managed_gitignore_span(text: str) -> tuple[int, int] | None:
    sentinel = _sentinel_span(text)
    return sentinel if sentinel is not None else _legacy_span(text)


def gitignore_without_managed(text: str) -> str:
    """Remove only the canonical or bounded legacy engine-owned range."""
    span = _managed_gitignore_span(text)
    if span is None:
        return text
    return text[:span[0]] + text[span[1]:]


def _read_gitignore(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write_gitignore(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def validate_gitignore(repo_root: Path = REPO_ROOT) -> None:
    """Fail closed on malformed ownership without changing the host file."""
    path = repo_root / ".gitignore"
    if path.exists():
        _managed_gitignore_span(_read_gitignore(path))


def ensure_gitignore(repo_root: Path = REPO_ROOT) -> bool:
    """Install or refresh the one canonical engine-owned ignore range."""
    gi = repo_root / ".gitignore"
    existing = _read_gitignore(gi) if gi.exists() else ""
    span = _managed_gitignore_span(existing)
    if span is None:
        separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
        updated = existing + separator + _GITIGNORE_BLOCK
    else:
        updated = existing[:span[0]] + _GITIGNORE_BLOCK + existing[span[1]:]
    if updated == existing:
        return False
    _write_gitignore(gi, updated)
    return True


def sc_remote() -> str | None:
    """The remote pointing at the engine upstream (the bootstrap checkout
    added it) — matched by either name across the super-coder → subfloor
    rename."""
    named = None
    for line in sh("git", "-C", str(REPO_ROOT), "remote", "-v").stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        if any(n in url for n in SOURCE_REPO_NAMES):
            return name
        if name in SOURCE_REPO_NAMES:
            named = name
    return named


def untrack_engine() -> bool:
    """B7: the engine is a gitignored materialized dependency, not fork source.
    The bootstrap `git checkout super-coder/<ref> -- .super-coder sc` staged it
    into the fork's index; drop it (files stay on disk, only git stops tracking).
    Idempotent: a no-op once already untracked."""
    tracked = sh("git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch",
                 ".super-coder").returncode == 0
    if not tracked:
        return False
    sh("git", "-C", str(REPO_ROOT), "rm", "-r", "--cached", "--quiet", ".super-coder")
    return True


def resolve_engine_ref() -> str | None:
    """Resolve the materialized upstream SHA without publishing a pin."""
    remote = sc_remote()
    if not remote:
        return None
    # rev-parse on an unfetched ref echoes the ref name and exits non-zero — guard
    # on BOTH (a clean exit AND a 40-hex SHA) so a miss leaves the pin for update.
    r = sh("git", "-C", str(REPO_ROOT), "rev-parse", f"{remote}/main")
    sha = r.stdout.strip()
    if r.returncode != 0 or len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        return None
    return sha


def write_engine_ref(sha: str) -> None:
    """Publish a callable engine SHA as the fork's rollback/version pin."""
    state = REPO_ROOT / ".sc-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "engine.ref").write_text(sha + "\n")


def step(msg: str) -> None:
    print(f"\n\033[1m→ {msg}\033[0m")


def main(argv: list[str]) -> int:
    require_supported_host()
    force = "--force" in argv
    skip_harness = "--skip-harness-install" in argv
    # super-coder's own flags — strip them so they don't reach init_fork's parser.
    own = {"--force", "--skip-harness-install", "--ensure-harness", "--update-harnesses",
           "--check-docker", "--harness-epoch", "--roll-harness-epoch"}
    fork_args = [a for a in argv if a not in own]

    # Standalone, machine-readable: the sandbox harness epoch. `sc` shells out to
    # these rather than reimplementing the file format, so there is one owner of
    # it. Bare values on stdout — no step banner — because callers capture them.
    if "--harness-epoch" in argv:
        print(harness_epoch())
        return 0
    if "--roll-harness-epoch" in argv:
        print(roll_harness_epoch())
        return 0

    report_host_runtime(report=any(flag in argv for flag in (
        "--update-harnesses", "--ensure-harness", "--check-docker"
    )))

    # Standalone: force-update all harness CLIs to latest and exit.
    if "--update-harnesses" in argv:
        step("Updating harness CLIs to latest (claude + opencode + codex + vibe + kimi)")
        update_harnesses()
        return 0

    # Standalone: just ensure the harness CLIs and exit (for an already-installed
    # fork). Runs before the guards so it works anywhere.
    if "--ensure-harness" in argv:
        step("Ensuring harness CLIs (claude + opencode + codex + vibe + kimi)")
        ensure_harnesses()
        return 0

    # Standalone preflight (re-run after configuring docker / logging in) —
    # `./sc doctor`: is the sandbox ready to launch + boot a harness?
    if "--check-docker" in argv:
        step("Sandbox runtime (docker)")
        report_docker()
        step("Harness login (host creds the sandbox mounts in)")
        report_logins()
        return 0

    # Wrapper lifecycle is install-only. Keep the import below the standalone
    # harness/doctor exits so minimal maintenance fixtures and older engines
    # can run those independent commands without the newly added module.
    import sc_wrapper

    # 1. Guards ---------------------------------------------------------------
    if is_source_repo() and not force:
        sys.exit("install: this is the super-coder SOURCE repo — the installer is "
                 "for forks. (Run it in a host repo that pulled the engine.) "
                 "Use --force only if you really mean to re-init the source.")
    if already_installed() and not force:
        sys.exit("install: this fork is already installed (.super-coder/instance.json "
                 "has installed_at). Re-installing destroys content — pass --force "
                 "to override, or just `./sc launch`.")
    try:
        sc_wrapper.check_install()
    except sc_wrapper.WrapperError as exc:
        sys.exit(f"install: {exc}")
    try:
        validate_gitignore(REPO_ROOT)
    except GitignoreError as exc:
        sys.exit(f"install: {exc}")

    # 2. Requirements ---------------------------------------------------------
    step("Checking requirements")
    report_host_runtime()
    brew = " (brew install git curl)" if IS_MAC else ""
    if not shutil.which("git"):
        print(f"  ⚠ git not on PATH — needed for the commit→PR flow later.{brew}")
    if not shutil.which("curl"):
        print(f"  ⚠ curl not on PATH — needed to auto-install a missing harness.{brew}")
    # Docker is the default run path (the sandbox); guide if it's missing or
    # under-configured. Never fatal — `./sc serve`+`boot` run without it.
    report_docker()

    # 3. Ensure harness CLIs --------------------------------------------------
    # Install claude + opencode + codex + vibe + kimi if missing, via their official NATIVE
    # installers (no npm). The harness picker lets a fork launch + run any, so we
    # want all present. --skip-harness-install detects only (CI / air-gapped).
    # instance.json's harness is the launch default; the picker overrides it
    # per-launch.
    step("Ensuring harness CLIs (claude + opencode + codex + vibe + kimi)")
    if skip_harness:
        print("  --skip-harness-install set — detecting only, not installing")
        for n in HARNESS_INSTALL:
            print(f"  {n:9} {'✓ present' if _harness_installed(n) else 'absent'}")
        global_pointer.write_global_pointers()
    else:
        ensure_harnesses()
    harness = detect_harness() or "claude"  # claude preferred; both should be present
    print(f"  → default harness for instance.json: {harness}")

    # 3.1 Harness login — the sandbox mounts host creds in, so a one-time host
    # login is what populates them. Detect + guide; the oauth flow isn't scriptable.
    step("Harness login (one-time, on the host — the sandbox mounts these creds in)")
    report_logins()

    # 3.5 Wire the host repo's .gitignore -------------------------------------
    step("Wiring .gitignore")
    try:
        changed_gitignore = ensure_gitignore()
    except GitignoreError as exc:
        sys.exit(f"install: {exc}")
    print("  installed canonical subfloor ignore block" if changed_gitignore
          else "  (already canonical)")

    # 3.55 Engine = gitignored dependency (B7) — untrack it + pin its version ---
    # The bootstrap checkout staged .super-coder/ into the fork's index; drop it
    # so the fork's git surfaces show only the project, and record the upstream
    # SHA so `./sc rollback` has an engine version to restore to.
    step("Making the engine a dependency (untrack + pin)")
    print("  git rm -r --cached .super-coder (files kept on disk)" if untrack_engine()
          else "  (engine already untracked)")
    pinned = resolve_engine_ref()
    callable_floor.require_callable_floor(
        REPO_ROOT,
        expected_ref=pinned,
        allow_unpinned=pinned is None,
        context="install",
    )
    if pinned is not None:
        write_engine_ref(pinned)
        print(f"  pinned engine.ref at {pinned[:12]}")
    else:
        print("  (could not resolve upstream ref — `./sc update` will pin it)")
    # First engine hash manifest: the checkout just brought the engine in, so
    # disk == upstream right now. From here, `./sc update` detects (and refuses
    # to silently overwrite) any local edit to an engine file.
    n = engine_manifest.write_manifest(engine_manifest.ENGINE_PATHS)
    print(f"  engine manifest written ({n} files) — local engine edits now detected on update")

    # 3.6 Create the shared scratch / handoff dir -----------------------------
    # A host-repo dir for screenshots, drafts, quick handoffs. The CONNECTIONS
    # boot block states its path by convention (<repo_root>/shared) — create it
    # so the path it points at exists.
    step("Creating shared/ (scratch + handoff dir)")
    shared = REPO_ROOT / "shared"
    if shared.exists():
        print("  (already present)")
    else:
        shared.mkdir()
        (shared / ".gitkeep").write_text("")
        print(f"  created {shared.relative_to(REPO_ROOT)}/")
    # redline_review's documented drop dir — create it so the skill's Step 1
    # ("list shared/redlines/") works as written on a fresh fork.
    redlines = shared / "redlines"
    if not redlines.exists():
        redlines.mkdir()
        (redlines / ".gitkeep").write_text("")
        print(f"  created {redlines.relative_to(REPO_ROOT)}/")

    # 3.7 Seed the fork-tracked Visual QA shim + inactive example config. The
    # live config remains opt-in at .sc-state/visual-qa.json.
    step("Seeding Visual QA CI")
    visual_qa_files = seed_visual_qa_files()
    if visual_qa_files:
        for path in visual_qa_files:
            print(f"  created {path}")
    else:
        print("  (already present or source repo)")

    # 4. Strip super-coder's per-instance content -----------------------------
    step("Stripping super-coder's per-instance content (a fork inherits the system, not the memory)")
    for p in STRIP:
        if p.exists():
            p.unlink()
            print(f"  removed {p.relative_to(REPO_ROOT)}")
        else:
            print(f"  (already absent) {p.relative_to(REPO_ROOT)}")

    # 5. Build the system DB --------------------------------------------------
    run_critical_phase(
        "Building the system DB (schema + migrations)",
        [PY, str(ENGINE / "scripts/rebuild.py")],
    )

    # 6. Seed the starting team (interactive: username only) ------------------
    if starting_team_exists():
        step("Seeding this fork's starting team")
        print("  (already seeded by an incomplete prior install)")
    else:
        run_critical_phase(
            "Seeding this fork's starting team",
            [PY, str(ENGINE / "scripts/init_fork.py"), *fork_args],
        )

    # 7. Wire the auto-remap hooks + map the host repo --------------------------
    # map-setup points core.hooksPath at the tracked hooks so the dr_* catalogue
    # stays fresh on every pull/checkout/rebase, then runs the initial map. The
    # Cartographer shell (seeded above) tunes map.config.json + heals later.
    run_critical_phase(
        "Wiring map automation + mapping the repo (dr_* catalogue)",
        [PY, str(ENGINE / "scripts/map_setup.py")],
    )

    # 8. Persist: snapshot + render ------------------------------------------
    # Admin/setup surface — pass SC_ADMIN so the serialize guard lets it through.
    admin_env = {**os.environ, "SC_ADMIN": "1"}
    run_critical_phase(
        "Serializing the installed state",
        [PY, str(ENGINE / "scripts/snapshot.py")],
        env=admin_env,
    )
    run_critical_phase(
        "Rendering installed shell surfaces",
        [PY, str(ENGINE / "scripts/render.py"), "flat"],
        env=admin_env,
    )

    # 8.5 Wire `make` aliases. The `dos-` prefix can't collide with the fork's own
    # targets, so we append the include rather than leave it to the operator (#13
    # forbids clobbering a host Makefile, not appending one non-colliding line).
    step("Wiring make aliases")
    print(f"  {wire_make_aliases()}")

    # The user-local command is shared by every installed checkout. Register
    # only after all checkout setup phases have succeeded, and revalidate the
    # target while holding the lifecycle lock so a concurrent install/remove
    # cannot replace or steal it between preflight and commit.
    step("Wiring managed host sc wrapper")
    try:
        print(f"  {sc_wrapper.register_install(REPO_ROOT)}")
    except sc_wrapper.WrapperError as exc:
        sys.exit(f"install: {exc}")

    # Record harness + installed marker in instance.json ---------------------
    cfg = ports_mod.resolve(persist=False)
    cfg["harness"] = harness
    cfg["installed_at"] = datetime.now(timezone.utc).date().isoformat()
    ports_mod.CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")

    # 9. Done -----------------------------------------------------------------
    step("Installed ✓")
    print(f"  harness : {harness}")
    print(f"  GUI port: {cfg['port']}  (http://127.0.0.1:{cfg['port']})")
    print("\nNext:")
    print("  git add -A && git commit -m 'install super-coder'")
    print("  ./sc launch        # or: make launch — starts the sandbox + GUI")
    print("  ./sc enter         # or: make enter  — attach + boot your shell")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
