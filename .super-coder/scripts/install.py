#!/usr/bin/env python3
"""Install super-coder into a fork — first-launch bootstrap.

Run once, in a host repo that has just pulled the engine
(`git checkout super-coder/main -- .super-coder sc`). It takes that repo
from "engine present" to "a team you can launch":

    1. Guard   — refuse to run in the super-coder SOURCE repo, or on a fork that
                 is already installed (both would destroy content). --force skips.
    2. Require — python3 + sqlite3 (+ a heads-up if git/curl missing, and a
                 docker preflight for the sandbox run path — advisory, not fatal).
    3. Harness — ensure claude + opencode + codex + vibe are installed (official native
                 installers, no npm); pick the launch default → instance.json.
    4. Strip   — super-coder's own per-instance content; a fork inherits the
                 SYSTEM (schema + skill catalogue + render chain), never the memory.
    5. Build   — the system DB (schema + migrations; no per-instance content yet).
    6. Seed    — the fork's first user + starting TEAM (delegates to init_fork:
                 your primary planner plus an admin, two dev, a reviewer, and the
                 singleton cartographer — each with the CC lineage + a genesis seed
                 + skill grants). Shells ship pre-named, so install asks only for a
                 username; no shell-naming interview.
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
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
PY = sys.executable
IS_MAC = platform.system() == "Darwin"  # guidance arms differ (colima/brew vs systemd/apt)

sys.path.insert(0, str(ENGINE / "scripts"))
import engine_manifest  # noqa: E402
import ports as ports_mod  # noqa: E402


# --- make-alias wiring (shared by install + update) -------------------------
ALIASES_INCLUDE = "-include .super-coder/aliases.mk"
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
        mk.write_text(
            "# Fork Makefile — super-coder convenience aliases (make dos-e / dos-enter).\n"
            "# Every target is dos--prefixed; add your own targets below the include.\n"
            f"{ALIASES_INCLUDE}\n"
        )
        return "wrote Makefile (-include .super-coder/aliases.mk) → `make dos-e` works"
    text = mk.read_text()
    if _ALIASES_RE.search(text):
        return "Makefile already wired (-include .super-coder/aliases.mk) — left as-is"
    sep = "" if text.endswith("\n") else "\n"
    mk.write_text(
        text + sep
        + "\n# super-coder convenience aliases (designs-OS 'dos-' command standard).\n"
        + "# Appended by ./sc; every target is dos--prefixed so it can't collide with\n"
        + "# this Makefile's own targets. Delete this line to opt out — `./sc <cmd>`\n"
        + "# stays equivalent.\n"
        + f"{ALIASES_INCLUDE}\n"
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


def origin_basename() -> str | None:
    p = sh("git", "-C", str(REPO_ROOT), "remote", "get-url", "origin")
    if p.returncode != 0:
        return None
    return p.stdout.strip().rstrip("/").split("/")[-1].removesuffix(".git")


def is_source_repo() -> bool:
    """The super-coder source repo's origin is …/super-coder. A fork's origin is
    its own repo (super-coder is a separate, differently-named remote)."""
    return origin_basename() == "super-coder"


def already_installed() -> bool:
    if not ports_mod.CONFIG.exists():
        return False
    try:
        return "installed_at" in json.loads(ports_mod.CONFIG.read_text())
    except json.JSONDecodeError:
        return False


def detect_harness() -> str | None:
    for h in ("claude", "opencode", "codex", "vibe"):
        if _harness_installed(h):
            return h
    return None


# Official NATIVE installers — no npm. Claude Code dropped npm as the primary
# path (https://code.claude.com/docs/en/setup); opencode + codex + vibe ship their own
# scripts too. Pipe-to-shell, latest version. vibe installs via uv (its script
# checks for / uses `uv tool install mistral-vibe`); a missing uv makes its
# install fail best-effort, same as any other harness.
HARNESS_INSTALL = {
    "claude":   "curl -fsSL https://claude.ai/install.sh | bash",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
    "codex":    "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "vibe":     "curl -LsSf https://mistral.ai/vibe/install.sh | bash",
}
# Where each installer drops its binary. Checked post-install because the new
# bin dir is NOT on this process's PATH — the installer edits shell rc files,
# which only a fresh shell picks up. shutil.which alone would miss a just-
# installed CLI. (codex's native installer drops into ~/.local/bin, same as
# claude; ~/.codex is its config/auth home, not the binary.)
HARNESS_BIN = {
    "claude":   Path.home() / ".local" / "bin" / "claude",
    "opencode": Path.home() / ".opencode" / "bin" / "opencode",
    "codex":    Path.home() / ".local" / "bin" / "codex",
    "vibe":     Path.home() / ".local" / "bin" / "vibe",
}


def _harness_installed(name: str) -> bool:
    return bool(shutil.which(name)) or HARNESS_BIN.get(name, Path("/nonexistent")).exists()


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
    return status


def ensure_harnesses() -> dict[str, str]:
    """Install any missing harness CLI via its official native installer (no
    npm) — claude + opencode + codex + vibe, so a fork can launch and run any. Best
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
# that brings the engine in doesn't carry super-coder's .gitignore, so the
# installer appends them to the host repo's .gitignore (idempotent via marker).
_GITIGNORE_MARKER = "# super-coder — rebuilt/derived; never commit"
_GITIGNORE_BLOCK = f"""
{_GITIGNORE_MARKER}
# The engine is a materialized, gitignored DEPENDENCY (B7) — fetched from
# upstream, refreshed by `./sc update`, never committed to the fork. Your project
# is everything ELSE in this repo. The one fork-owned artifact that must survive,
# the DB serialization, lives in the tracked .sc-state/ below.
/.super-coder/
# Boot artifacts + per-shell skill render — rebuilt at launch from the DB.
/CLAUDE.md
/AGENTS.md
/opencode.json
/.claude/skills/
# Engine-managed harness config re-emitted each launch (per-harness branch-guard
# hook); kept apart from a fork's own tracked config (claude settings.json /
# codex config.toml).
/.claude/settings.local.json
/.codex/hooks.json
# Shell worktrees — one per shell, linked inside the repo root.
/.sc-worktrees/
# .sc-state/ is TRACKED (content.sql + engine.ref). Only the ephemeral
# pre-update restore pointer and the derived map cache are ignored.
/.sc-state/engine.ref.prev
# Map DB — derived cache of the repo (dr_*), rebuilt by `./sc map`. Its authored
# layer (sections) is tracked in .sc-state/map_content.sql.
/.sc-state/map.db
/.sc-state/map.db-wal
/.sc-state/map.db-shm
"""


def _required_ignores() -> list[str]:
    """The ignore PATTERNS in the block (path lines, not comments). Single source
    of truth — adding a line to _GITIGNORE_BLOCK above is enough for both fresh
    installs and `./sc update` top-ups."""
    return [ln.strip() for ln in _GITIGNORE_BLOCK.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def ensure_gitignore(repo_root: Path = REPO_ROOT) -> bool:
    """Ensure the host repo's .gitignore covers every engine-derived path.

    First install (marker absent): append the full annotated block. On a fork
    that already has the block, **top up** any patterns added in later releases
    (e.g. the map DB cache `/.sc-state/map.db`) — line-additive, so a fork that
    picks up new engine ignore rules via `./sc update` self-heals instead of
    silently committing a churning derived cache. Returns True if it changed."""
    gi = repo_root / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if _GITIGNORE_MARKER not in existing:
        with gi.open("a") as f:
            f.write(("" if existing.endswith("\n") or not existing else "\n") + _GITIGNORE_BLOCK)
        return True
    present = {ln.strip() for ln in existing.splitlines()}
    missing = [p for p in _required_ignores() if p not in present]
    if not missing:
        return False
    with gi.open("a") as f:
        f.write(("" if existing.endswith("\n") else "\n")
                + "# super-coder — engine ignore rules added by `./sc update`\n"
                + "\n".join(missing) + "\n")
    return True


def sc_remote() -> str | None:
    """The remote pointing at super-coder (the bootstrap checkout added it)."""
    named = None
    for line in sh("git", "-C", str(REPO_ROOT), "remote", "-v").stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        if "super-coder" in url:
            return name
        if name == "super-coder":
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


def pin_engine() -> str | None:
    """Record the upstream SHA the engine was materialized at → .sc-state/engine.ref
    (the fork's version record + the engine half of a sound rollback). Best-effort:
    if the remote ref can't be resolved, `./sc update` will pin it later."""
    state = REPO_ROOT / ".sc-state"
    state.mkdir(parents=True, exist_ok=True)
    remote = sc_remote()
    if not remote:
        return None
    # rev-parse on an unfetched ref echoes the ref name and exits non-zero — guard
    # on BOTH (a clean exit AND a 40-hex SHA) so a miss leaves the pin for update.
    r = sh("git", "-C", str(REPO_ROOT), "rev-parse", f"{remote}/main")
    sha = r.stdout.strip()
    if r.returncode != 0 or len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        return None
    (state / "engine.ref").write_text(sha + "\n")
    return sha


def step(msg: str) -> None:
    print(f"\n\033[1m→ {msg}\033[0m")


def main(argv: list[str]) -> int:
    force = "--force" in argv
    skip_harness = "--skip-harness-install" in argv
    # super-coder's own flags — strip them so they don't reach init_fork's parser.
    own = {"--force", "--skip-harness-install", "--ensure-harness", "--update-harnesses", "--check-docker"}
    fork_args = [a for a in argv if a not in own]

    # Standalone: force-update all harness CLIs to latest and exit.
    if "--update-harnesses" in argv:
        step("Updating harness CLIs to latest (claude + opencode + codex + vibe)")
        update_harnesses()
        return 0

    # Standalone: just ensure the harness CLIs and exit (for an already-installed
    # fork). Runs before the guards so it works anywhere.
    if "--ensure-harness" in argv:
        step("Ensuring harness CLIs (claude + opencode + codex + vibe)")
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

    # 1. Guards ---------------------------------------------------------------
    if is_source_repo() and not force:
        sys.exit("install: this is the super-coder SOURCE repo — the installer is "
                 "for forks. (Run it in a host repo that pulled the engine.) "
                 "Use --force only if you really mean to re-init the source.")
    if already_installed() and not force:
        sys.exit("install: this fork is already installed (.super-coder/instance.json "
                 "has installed_at). Re-installing destroys content — pass --force "
                 "to override, or just `./sc launch`.")

    # 2. Requirements ---------------------------------------------------------
    step("Checking requirements")
    try:
        import sqlite3  # noqa: F401
        print("  python3 + sqlite3 ✓")
    except ImportError:
        hint = "brew install python" if IS_MAC else "your package manager"
        sys.exit(f"  python3 is missing the sqlite3 module — install it ({hint}) and retry.")
    brew = " (brew install git curl)" if IS_MAC else ""
    if not shutil.which("git"):
        print(f"  ⚠ git not on PATH — needed for the commit→PR flow later.{brew}")
    if not shutil.which("curl"):
        print(f"  ⚠ curl not on PATH — needed to auto-install a missing harness.{brew}")
    # Docker is the default run path (the sandbox); guide if it's missing or
    # under-configured. Never fatal — `./sc serve`+`boot` run without it.
    report_docker()

    # 3. Ensure harness CLIs --------------------------------------------------
    # Install claude + opencode + codex + vibe if missing, via their official NATIVE
    # installers (no npm). The harness picker lets a fork launch + run any, so we
    # want all present. --skip-harness-install detects only (CI / air-gapped).
    # instance.json's harness is the launch default; the picker overrides it
    # per-launch.
    step("Ensuring harness CLIs (claude + opencode + codex + vibe)")
    if skip_harness:
        print("  --skip-harness-install set — detecting only, not installing")
        for n in HARNESS_INSTALL:
            print(f"  {n:9} {'✓ present' if _harness_installed(n) else 'absent'}")
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
    print("  added super-coder ignore lines" if ensure_gitignore()
          else "  (already present)")

    # 3.55 Engine = gitignored dependency (B7) — untrack it + pin its version ---
    # The bootstrap checkout staged .super-coder/ into the fork's index; drop it
    # so the fork's git surfaces show only the project, and record the upstream
    # SHA so `./sc rollback` has an engine version to restore to.
    step("Making the engine a dependency (untrack + pin)")
    print("  git rm -r --cached .super-coder (files kept on disk)" if untrack_engine()
          else "  (engine already untracked)")
    pinned = pin_engine()
    print(f"  pinned engine.ref at {pinned[:12]}" if pinned
          else "  (could not resolve upstream ref — `./sc update` will pin it)")
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

    # 4. Strip super-coder's per-instance content -----------------------------
    step("Stripping super-coder's per-instance content (a fork inherits the system, not the memory)")
    for p in STRIP:
        if p.exists():
            p.unlink()
            print(f"  removed {p.relative_to(REPO_ROOT)}")
        else:
            print(f"  (already absent) {p.relative_to(REPO_ROOT)}")

    # 5. Build the system DB --------------------------------------------------
    step("Building the system DB (schema + migrations)")
    r = subprocess.run([PY, str(ENGINE / "scripts/rebuild.py")])
    if r.returncode != 0:
        sys.exit("install: rebuild failed.")

    # 6. Seed the starting team (interactive: username only) ------------------
    step("Seeding this fork's starting team")
    r = subprocess.run([PY, str(ENGINE / "scripts/init_fork.py"), *fork_args])
    if r.returncode != 0:
        sys.exit("install: starting-team seeding failed (or was aborted).")

    # 7. Wire the auto-remap hooks + map the host repo --------------------------
    # map-setup points core.hooksPath at the tracked hooks so the dr_* catalogue
    # stays fresh on every pull/checkout/rebase, then runs the initial map. The
    # Cartographer shell (seeded above) tunes map.config.json + heals later.
    step("Wiring map automation + mapping the repo (dr_* catalogue)")
    subprocess.run([PY, str(ENGINE / "scripts/map_setup.py")])

    # 8. Persist: snapshot + render ------------------------------------------
    # Admin/setup surface — pass SC_ADMIN so the serialize guard lets it through.
    step("Serializing + rendering")
    admin_env = {**os.environ, "SC_ADMIN": "1"}
    subprocess.run([PY, str(ENGINE / "scripts/snapshot.py")], env=admin_env)
    subprocess.run([PY, str(ENGINE / "scripts/render.py"), "flat"], env=admin_env)

    # 8.5 Wire `make` aliases. The `dos-` prefix can't collide with the fork's own
    # targets, so we append the include rather than leave it to the operator (#13
    # forbids clobbering a host Makefile, not appending one non-colliding line).
    step("Wiring make aliases")
    print(f"  {wire_make_aliases()}")

    # Record harness + installed marker in instance.json ---------------------
    cfg = ports_mod.resolve(persist=False)
    cfg["harness"] = harness
    cfg["installed_at"] = date.today().isoformat()
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
    raise SystemExit(main(sys.argv[1:]))
