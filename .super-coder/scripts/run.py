#!/usr/bin/env python3
"""Launch a shell against this repo.

super-coder is forked into ONE repo, so a shell works the repo root — no
per-shell workdir, no cross-repo cwd confusion (that is the whole inversion).

Flow:
    1. username-only auth (v1: no password challenge — pick a name)
    2. pick a shell (arg shortname · --first · interactive picker)
    3. open a session archive row
    4. compose the boot artifact and dual-write CLAUDE.md + AGENTS.md at root
    5. exec the harness  (skipped when RENDER_ONLY=1 — used to verify headless)

Usage:
    python3 .super-coder/scripts/run.py [shortname] [--first]
    RENDER_ONLY=1 python3 .super-coder/scripts/run.py --first   # render, don't exec
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "render"))
from compose import compose_boot  # noqa: E402
import flat  # noqa: E402

sys.path.insert(0, str(ENGINE / "scripts"))
import install  # noqa: E402  — reuse its canonical HARNESS_BIN (one source of truth)

ADAPTERS = ENGINE / "adapters"


def load_adapter(harness: str) -> dict:
    """The harness-specific seam (adapters/<harness>/adapter.json): launch argv,
    which files to emit at the repo root, and extra launch env. Unknown harness
    falls back to running its own name + reading AGENTS.md."""
    path = ADAPTERS / harness / "adapter.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"harness": harness, "launch": [harness], "boot_artifact": "AGENTS.md",
            "emit": [], "env": {}}


def emit_adapter(adapter: dict) -> list[str]:
    """Copy the adapter's harness-specific config files (e.g. opencode.json) to
    the repo root. These are emitted artifacts (gitignored), regenerated each
    launch from the tracked template in the adapter dir."""
    adir = ADAPTERS / adapter["harness"]
    written = []
    for fname in adapter.get("emit", []):
        src = adir / fname
        if src.exists():
            atomic_write(REPO_ROOT / fname, src.read_text())
            written.append(fname)
    return written


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base (patch wins on scalar conflicts);
    mutates and returns base. Nested dicts merge key-wise so a fork's other
    settings survive."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def apply_sandbox(adapter: dict) -> list[str]:
    """Sandbox-only: elevate harness permissions to allow-all when booting
    INSIDE the docker sandbox (SC_SANDBOX, set by `sc launch`'s docker run). The
    container is the safety boundary, so permission prompts inside it are pure
    friction; the no-docker host escape hatch (`./sc boot` with SC_SANDBOX
    unset) keeps normal prompts. Each adapter declares sandbox.merge_json:
    {repo-relative-path: patch}; we deep-merge the patch into that
    project-scoped file (preserving any keys the fork set). Paths are
    repo-relative, so this never touches host-global config (~/.claude etc.)."""
    if not os.environ.get("SC_SANDBOX"):
        return []
    spec = (adapter.get("sandbox") or {}).get("merge_json") or {}
    touched = []
    for rel, patch in spec.items():
        dst = REPO_ROOT / rel
        cur: dict = {}
        if dst.exists():
            try:
                cur = json.loads(dst.read_text())
            except (json.JSONDecodeError, OSError):
                print(f"  ! {rel} is not valid JSON — leaving it untouched")
                continue
        _deep_merge(cur, patch)
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dst, json.dumps(cur, indent=2) + "\n")
        touched.append(rel)
    return touched


def ensure_harness_path() -> None:
    """Prepend the dirs where the official installers drop harness binaries onto
    this process's PATH, so detection (shutil.which) and exec (execvpe) agree
    with what `./sc install` / `./sc ensure-harness` installed.

    The opencode installer drops its binary in ~/.opencode/bin and only edits a
    shell rc — a dir a fresh launch shell does NOT carry on PATH. Without this,
    detect_harnesses() silently never offers opencode even though ensure-harness
    reported it installed: install.py trusts HARNESS_BIN, the launcher trusted
    PATH only, and they disagreed. Reuse install.HARNESS_BIN so there is one
    source for where a harness lives."""
    try:
        bin_dirs = [p.parent for p in install.HARNESS_BIN.values()]
    except Exception:
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    add = [str(d) for d in bin_dirs if d.is_dir() and str(d) not in parts]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + parts)


def detect_harnesses() -> list[str]:
    """Harnesses installable RIGHT NOW: an adapter dir with adapter.json whose
    launch command is also on PATH (after ensure_harness_path() has folded in
    the installer bin dirs). Adapter-dir order. Drives the launch-time picker —
    we only offer a harness the host can actually exec."""
    if not ADAPTERS.exists():
        return []
    found = []
    for d in sorted(ADAPTERS.iterdir()):
        cfg = d / "adapter.json"
        if not (d.is_dir() and cfg.exists()):
            continue
        try:
            adapter = json.loads(cfg.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        cmd = (adapter.get("launch") or [d.name])[0]
        if shutil.which(cmd):
            found.append(adapter.get("harness", d.name))
    return found


def pick_harness(detected: list[str], default: str, first: bool) -> str | None:
    """Resolve the harness when no explicit override (--harness / HARNESS) was
    given. Returns None when nothing is detected so the caller can fall back to
    instance.json/'claude' — preserving the old silent behavior on a host with
    no harness CLI on PATH (headless verify, CI). The pick is per-launch only:
    nothing is written back, so two terminals can boot the same fork on
    different harnesses in parallel."""
    if not detected:
        return None
    if len(detected) == 1:
        return detected[0]
    dflt = default if default in detected else detected[0]
    # --first and non-TTY (verify/CI) never prompt — take the default silently.
    if first or not sys.stdin.isatty():
        return dflt
    print("\nHarness:")
    for i, h in enumerate(detected, 1):
        mark = "  (default)" if h == dflt else ""
        print(f"  {i}. {h}{mark}")
    while True:
        choice = input(f"\nPick (1-{len(detected)}, Enter for {dflt}): ").strip()
        if not choice:
            return dflt
        if choice.isdigit() and 1 <= int(choice) <= len(detected):
            return detected[int(choice) - 1]
        print("  invalid choice")


def _configured_harness() -> str | None:
    cfg = ENGINE / "instance.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("harness")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def open_db() -> sqlite3.Connection:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(
            f"FATAL: no usable DB at {DB_PATH}.\n"
            f"  Rebuild it from text:  ./sc rebuild"
        )
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    # Coexist with the review server writing the same file from another process
    # (see server.py db()): WAL + a busy_timeout instead of "database is locked".
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("SELECT 1 FROM shells LIMIT 1")  # smoke
    return con


# ── Auth (username-only) ────────────────────────────────────────────────────

def authenticate(con: sqlite3.Connection) -> sqlite3.Row:
    # SC_USER env wins; else prompt on a TTY; else (headless: `./sc verify`, CI)
    # default to the first active user so launch doesn't EOFError without a TTY.
    username = os.environ.get("SC_USER")
    if not username:
        if sys.stdin.isatty():
            username = input("Username: ").strip()
        else:
            row = con.execute(
                "SELECT username FROM users WHERE is_active=1 ORDER BY user_id LIMIT 1"
            ).fetchone()
            username = row["username"] if row else None
    if not username:
        sys.exit("aborted — no user (set SC_USER or provision a user)")
    row = con.execute(
        "SELECT user_id, username FROM users "
        "WHERE LOWER(username)=LOWER(?) AND is_active=1",
        (username,),
    ).fetchone()
    if row is None:
        sys.exit(f"no active user '{username}'")
    return row


# ── Shell selection ─────────────────────────────────────────────────────────

def list_shells(con: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT shell_id, display_name, shortname, mandate, is_shared, flavor FROM shells "
        "WHERE (user_id=? OR is_shared=1) AND COALESCE(is_deleted,0)=0 "
        "ORDER BY is_shared, shell_id",
        (user_id,),
    ).fetchall()


def flavor_defaults(con: sqlite3.Connection) -> dict:
    """flavor -> {'default_harness', 'models': {harness: model}} launch defaults.
    The (flavor, harness) matrix: each flavor names a model per harness, and one
    harness is the picker default (is_default). Empty if the table is absent
    (older fork mid-migration) so the launcher degrades to its prior behavior
    rather than failing."""
    try:
        rows = con.execute(
            "SELECT flavor, harness, model, is_default FROM flavor_defaults")
    except sqlite3.OperationalError:
        return {}
    out: dict = {}
    for r in rows:
        fd = out.setdefault(r["flavor"], {"default_harness": None, "models": {}})
        fd["models"][r["harness"]] = r["model"]
        if r["is_default"]:
            fd["default_harness"] = r["harness"]
    return out


def _default_label(defaults: dict, flavor: str | None) -> str:
    """Picker annotation: the harness (+ short model id) a shell of this flavor
    boots with by default, so the operator knows which harness to launch if they
    forget. Blank for bespoke shells with no flavor default."""
    fd = defaults.get(flavor)
    if not fd or not fd["default_harness"]:
        return ""
    harness = fd["default_harness"]
    model = fd["models"].get(harness)
    return harness + (f" · {model.split('/')[-1]}" if model else "")


def pick_shell(shells: list[sqlite3.Row], requested: str | None,
               first: bool, defaults: dict | None = None) -> sqlite3.Row:
    defaults = defaults or {}
    if not shells:
        sys.exit("FATAL: no shells available to this user.")
    if requested:
        # Case-insensitive: auto-names are upper (DEV3) but `./sc launch-dev3` works.
        chosen = next((s for s in shells if (s["shortname"] or "").lower()
                       == requested.lower()), None)
        if chosen is None:
            avail = ", ".join(s["shortname"] or "?" for s in shells)
            sys.exit(f"no shell '{requested}'. Available: {avail}")
        return chosen
    if first or not sys.stdin.isatty():
        return shells[0]
    # Interactive picker — the Default column tells the operator the intended
    # harness/model (advisory; overridable at launch).
    print(f"\n{'ID':>3}  {'Name':<16}{'Shortname':<14}{'Default (harness · model)'}")
    for s in shells:
        print(f"{s['shell_id']:>3}  {(s['display_name'] or ''):<16}"
              f"{(s['shortname'] or ''):<14}{_default_label(defaults, s['flavor'])}")
    valid = {s["shell_id"] for s in shells}
    while True:
        choice = input("\nPick (ID): ").strip()
        if choice.isdigit() and int(choice) in valid:
            return next(s for s in shells if s["shell_id"] == int(choice))
        print("  invalid id")


# ── Session archive ─────────────────────────────────────────────────────────

def _is_unused(narrative: str) -> bool:
    """A freshly-opened session whose narrative is still just the 'Session start'
    stub (no work appended). Detected by a single timestamp entry."""
    return (narrative or "").count("\n[") <= 1


def open_session(con: sqlite3.Connection, shell_id: int) -> tuple[str, int]:
    # Reuse the active session if it was opened but never used (e.g. install
    # opened session 0001, or a prior launch did no work) — avoids phantom empty
    # sessions and the incidental first-snapshot diff.
    active = con.execute(
        "SELECT active_archive_id FROM shells WHERE shell_id=?", (shell_id,)
    ).fetchone()[0]
    if active:
        row = con.execute(
            "SELECT archive_id, session_id, full_narrative FROM shell_memory_archives "
            "WHERE archive_id=?", (active,)
        ).fetchone()
        if row and _is_unused(row["full_narrative"]):
            return row["session_id"], row["archive_id"]

    last = con.execute(
        "SELECT MAX(CAST(session_id AS INTEGER)) FROM shell_memory_archives WHERE shell_id=?",
        (shell_id,),
    ).fetchone()[0]
    session_id = f"{(last or 0) + 1:04d}"
    today, now_hm = str(date.today()), datetime.now().strftime("%H:%M")
    narrative = (f"# {session_id} | {today} | session opened\n\n"
                 f"## Narrative\n\n[{now_hm}] Session start.\n")
    cur = con.execute(
        "INSERT INTO shell_memory_archives (shell_id, session_id, date, full_narrative) "
        "VALUES (?, ?, ?, ?)",
        (shell_id, session_id, today, narrative),
    )
    archive_id = cur.lastrowid
    con.execute("UPDATE shells SET active_archive_id=? WHERE shell_id=?",
                (archive_id, shell_id))
    con.commit()
    return session_id, archive_id


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    first = "--first" in args
    # --harness <name> / --harness=<name> forces the harness and skips the
    # picker; its value must not be mistaken for the shell shortname positional.
    flag_harness = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--harness":
            flag_harness = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--harness="):
            flag_harness = a.split("=", 1)[1]
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    requested = positional[0] if positional else None

    con = open_db()
    user = authenticate(con)
    fdefaults = flavor_defaults(con)
    chosen = pick_shell(list_shells(con, user["user_id"]), requested, first, fdefaults)

    # This shell's flavor default (advisory): the harness it boots with. The
    # model is resolved AFTER the harness pick — a flavor names a model PER
    # harness, so the model tracks whichever harness the operator lands on. Both
    # are overridable — the flavor default only sets the fallback, never a lock.
    fdef = fdefaults.get(chosen["flavor"])
    flavor_harness = fdef["default_harness"] if fdef else None

    # Harness pick, right after the shell pick: an explicit --harness / HARNESS
    # override wins silently; otherwise offer the harnesses on PATH when more
    # than one is present (per-launch, never persisted), falling back to this
    # shell's flavor default, then the fork's instance.json value / 'claude'.
    # Fold the installer bin dirs onto PATH first so detection sees an installed-
    # but-not-yet-on-PATH harness (e.g. opencode in ~/.opencode/bin).
    ensure_harness_path()
    default_harness = flavor_harness or _configured_harness() or "claude"
    harness = (flag_harness or os.environ.get("HARNESS")
               or pick_harness(detect_harnesses(), default_harness, first)
               or default_harness)

    # Now that the harness is known, resolve THIS flavor's model for it (the
    # (flavor, harness) cell). None when the flavor has no entry for the chosen
    # harness (e.g. opencode as a manual fallback) — then the harness picks its own.
    flavor_model = fdef["models"].get(harness) if fdef else None

    session_id, archive_id = open_session(con, chosen["shell_id"])

    full = con.execute(
        "SELECT shell_id, display_name, shortname, partner, role, mandate, "
        "current_state, system_prompt, connections, flavor FROM shells WHERE shell_id=?",
        (chosen["shell_id"],),
    ).fetchone()
    content = compose_boot(con, full, user, session_id, archive_id)

    # Render this shell's granted skills to .claude/skills/<name>/SKILL.md —
    # harness-consumed, gitignored, rebuilt per boot (like the boot artifact).
    skills = flat.render_skill_md(con, full["shell_id"])
    con.close()

    # One compose, two outputs — Claude Code reads CLAUDE.md, the AGENTS.md
    # harnesses read AGENTS.md. Both at the repo root.
    for name in ("CLAUDE.md", "AGENTS.md"):
        atomic_write(REPO_ROOT / name, content)

    print(f"\n→ booted {full['display_name']} "
          f"(shell_id={full['shell_id']}, session={session_id})")
    print(f"→ wrote {REPO_ROOT/'CLAUDE.md'}")
    print(f"→ wrote {REPO_ROOT/'AGENTS.md'}")
    print(f"→ skills: {len(skills['written'])} written, "
          f"{len(skills['skipped'])} unchanged → .claude/skills/")

    # Harness was resolved up front (override / picker / default); the adapter
    # seam owns the launch command + any harness-specific config to emit.
    adapter = load_adapter(harness)
    emitted = emit_adapter(adapter)
    print(f"→ harness: {harness} (reads {adapter.get('boot_artifact', 'AGENTS.md')})")
    if emitted:
        print(f"→ emitted {', '.join(emitted)}")

    # Flavor model default: route the model to the harness the operator picked.
    # The adapter declares HOW it takes a model — a launch flag (claude/codex:
    # `--model <id>`) or a config-file key (opencode: opencode.json "model"). A
    # NULL flavor model, or a harness declaring neither, skips this. Still
    # overridable in-session / via the harness's own `-m`.
    model_args: list[str] = []
    mcfg = adapter.get("model") or {}
    if flavor_model and mcfg.get("flag"):
        model_args = [mcfg["flag"], flavor_model]
        print(f"→ model: {flavor_model} (flavor default for {chosen['flavor']})")
    elif flavor_model and mcfg.get("file"):
        mfile = REPO_ROOT / mcfg["file"]
        if mfile.exists():
            try:
                cfg = json.loads(mfile.read_text())
            except (json.JSONDecodeError, OSError):
                cfg = {}
            cfg[mcfg.get("key", "model")] = flavor_model
            atomic_write(mfile, json.dumps(cfg, indent=2) + "\n")
            print(f"→ model: {flavor_model} (flavor default for {chosen['flavor']})")
    sandboxed = apply_sandbox(adapter)
    if sandboxed:
        print(f"→ sandbox: allow-all permissions → {', '.join(sandboxed)}")

    # Sandbox-only launch flags — e.g. codex's approval/sandbox bypass, safe
    # because the container is the safety boundary. The no-docker host path keeps
    # the harness's normal prompts (SC_SANDBOX unset).
    sandbox_flags: list[str] = []
    if os.environ.get("SC_SANDBOX"):
        sandbox_flags = (adapter.get("sandbox") or {}).get("launch_flags") or []
        if sandbox_flags:
            print(f"→ sandbox: launch flags → {' '.join(sandbox_flags)}")

    if os.environ.get("RENDER_ONLY"):
        print("→ RENDER_ONLY set — not exec'ing the harness.")
        return

    cmd = (adapter.get("launch") or [harness]) + model_args + sandbox_flags
    env = {**os.environ, **{k: str(v) for k, v in adapter.get("env", {}).items()}}
    os.chdir(REPO_ROOT)
    print(f"→ exec {' '.join(cmd)}\n")
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
