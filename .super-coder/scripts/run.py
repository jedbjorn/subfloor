#!/usr/bin/env python3
"""Launch a shell against this repo.

super-coder is forked into ONE repo, so a shell works the repo root — no
per-shell workdir, no cross-repo cwd confusion (that is the whole inversion).

Flow:
    1. username-only auth (v1: no password challenge — pick a name)
    2. pick a shell (arg shortname · --first · interactive picker)
    3. open a session archive row
    4. compose the boot artifact and dual-write CLAUDE.md + AGENTS.md at root
       (dev-flavor shells: write to their worktree root, not the repo root)
    5. exec the harness  (skipped when RENDER_ONLY=1 — used to verify headless)

Usage:
    python3 .super-coder/scripts/run.py [shortname] [--first]
    RENDER_ONLY=1 python3 .super-coder/scripts/run.py --first   # render, don't exec

Headless (`./sc run <shortname> [-p "<prompt>"] [--harness <h>] [-m <model>]
[--effort <level>]`):
the same render-then-exec path minus the picker and the TTY. The harness runs
non-interactively via its adapter's `headless` block (claude -p · codex exec ·
opencode run), streams a final message, and exits — the ephemeral-worker
primitive of sprint eventing (specs_sc/sprint-eventing.md). Default prompt
drains the inbox; a liveness guard refuses a shell whose worktree already
hosts a live session (one shell, one session). Harness + model resolve:
explicit flags → the shell's flavor_defaults (a sprint's `models:` line rides
in AS flags — the planner passes it on every `sc run` it issues).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "render"))
from compose import compose_boot  # noqa: E402
import flat  # noqa: E402

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
import install  # noqa: E402  — reuse its canonical HARNESS_BIN (one source of truth)
import git_prune  # noqa: E402  — boot-time prune of provably-merged local branches
import ports as ports_mod  # noqa: E402  — derive the per-fork API base URL
import style  # noqa: E402  — launcher ANSI; degrades to plain text off-TTY
import seed_skills  # noqa: E402  — boot-time self-heal of stale engine skills
import shell_liveness  # noqa: E402  — headless boot's one-shell-one-session guard

sys.path.insert(0, str(ENGINE / "api"))
import model_catalog  # noqa: E402  — HARNESS_PROVIDER: one source for harness → provider

ADAPTERS = ENGINE / "adapters"

DEFAULT_HEADLESS_PROMPT = "Check your inbox and act on your unread messages."


def resolve_headless_model(flag_model: "str | None", fdef: "dict | None",
                           harness: str) -> "str | None":
    """Headless model resolution: an explicit -m wins; else the shell's
    (flavor, harness) default; else None — the harness picks its own."""
    if flag_model:
        return flag_model
    return fdef["models"].get(harness) if fdef else None


def _headless_effort_args(hcfg: dict, effort: "str | None",
                          harness: str = "?") -> list[str]:
    if not effort:
        return []
    ecfg = hcfg.get("effort") or {}
    if ecfg.get("flag"):
        return [ecfg["flag"], effort]
    if ecfg.get("config_flag") and ecfg.get("config_key"):
        return [ecfg["config_flag"], f'{ecfg["config_key"]}="{effort}"']
    if ecfg.get("env"):
        return []
    raise ValueError(
        f"harness '{harness}' cannot apply effort '{effort}'")


def headless_effort_env(adapter: dict, effort: "str | None") -> dict[str, str]:
    ecfg = ((adapter.get("headless") or {}).get("effort") or {})
    return {ecfg["env"]: effort} if effort and ecfg.get("env") else {}


def validate_headless_request(adapter: dict, model: "str | None",
                              effort: "str | None") -> None:
    hcfg = adapter.get("headless") or {}
    harness = adapter.get("harness", "?")
    if not hcfg.get("launch"):
        raise ValueError(f"harness '{harness}' has no headless adapter")
    if model and not hcfg.get("model_flag"):
        raise ValueError(
            f"harness '{harness}' cannot apply requested model '{model}'")
    _headless_effort_args(hcfg, effort, harness)


def headless_command(adapter: dict, prompt: str, model: "str | None" = None,
                     sandbox_flags: "list[str] | None" = None,
                     effort: "str | None" = None) -> "list[str] | None":
    """The non-interactive exec argv from the adapter's `headless` block —
    launch prefix + model flag + sandbox flags + the prompt as the final
    positional. None when the harness declares no headless block (e.g. vibe,
    which takes no model from the launch seam — see the spec's non-goals)."""
    hcfg = adapter.get("headless")
    if not hcfg or not hcfg.get("launch"):
        return None
    validate_headless_request(adapter, model, effort)
    cmd = list(hcfg["launch"])
    if model:
        cmd += [hcfg["model_flag"], model]
    cmd += _headless_effort_args(hcfg, effort, adapter.get("harness", "?"))
    cmd += list(sandbox_flags or [])
    if hcfg.get("prompt_flag"):
        cmd += [hcfg["prompt_flag"], prompt]
    else:
        cmd.append(prompt)
    return cmd


def load_adapter(harness: str) -> dict:
    """The harness-specific seam (adapters/<harness>/adapter.json): launch argv,
    which files to emit at the repo root, and extra launch env. Unknown harness
    falls back to running its own name + reading AGENTS.md."""
    path = ADAPTERS / harness / "adapter.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"harness": harness, "launch": [harness], "boot_artifact": "AGENTS.md",
            "emit": [], "env": {}}


def emit_adapter(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Copy the adapter's harness-specific config files (e.g. opencode.json) to
    `root` (the working directory). These are emitted artifacts (gitignored),
    regenerated each launch from the tracked template in the adapter dir."""
    adir = ADAPTERS / adapter["harness"]
    written = []
    for fname in adapter.get("emit", []):
        src = adir / fname
        if src.exists():
            dst = root / fname
            dst.parent.mkdir(parents=True, exist_ok=True)  # fname may be nested (e.g. .codex/hooks.json)
            atomic_write(dst, src.read_text())
            written.append(fname)
    return written


def resolve_opencode_plugins(work_dir: Path) -> None:
    """Rewrite opencode.json `plugin` entries that point into the engine to
    ABSOLUTE paths. The template registers
    `./.super-coder/adapters/opencode/protect-default-branch.js` — relative to the
    opencode.json location (the worktree root). A fork gitignores .super-coder/,
    so from a shell worktree that path does not exist and opencode silently loads
    NO plugin → the branch-guard never runs. Same trap the hooks fell into;
    resolve to the installed engine (verified: opencode loads plugins by absolute
    path). No-op when no engine-relative plugin entry is present (e.g. the source
    repo, where the relative path already resolves)."""
    cfg_path = work_dir / "opencode.json"
    if not cfg_path.exists():
        return
    try:
        cfg = json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    plugins = cfg.get("plugin")
    if not isinstance(plugins, list):
        return
    changed = False
    resolved = []
    for p in plugins:
        if isinstance(p, str) and ".super-coder/" in p:
            resolved.append(str(ENGINE / p.split(".super-coder/", 1)[1]))
            changed = True
        else:
            resolved.append(p)
    if changed:
        cfg["plugin"] = resolved
        atomic_write(cfg_path, json.dumps(cfg, indent=2) + "\n")


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


def _merge_json_spec(spec: dict, root: Path = REPO_ROOT) -> list[str]:
    """Deep-merge each {repo-relative-path: patch} into that project-scoped JSON
    file under `root`, preserving any keys the fork already set. Writes the same
    bytes when the patch is already present, so re-running produces no git churn."""
    touched = []
    for rel, patch in (spec or {}).items():
        dst = root / rel
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


def apply_merge_json(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Always-on config patches the adapter declares at top-level `merge_json`
    (distinct from sandbox.merge_json, which is sandbox-only). Used to install
    engine-managed, gitignored harness config every launch — e.g. claude's
    PreToolUse branch-guard hook in .claude/settings.local.json (kept out of the
    fork's tracked .claude/settings.json so fork-owned config is never clobbered)."""
    return _merge_json_spec(adapter.get("merge_json") or {}, root)


def apply_sandbox(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Sandbox-only: elevate harness permissions to allow-all when booting
    INSIDE the docker sandbox (SC_SANDBOX, set by `sc launch`'s docker run). The
    container is the safety boundary, so permission prompts inside it are pure
    friction; the no-docker host escape hatch (`./sc boot` with SC_SANDBOX
    unset) keeps normal prompts. Each adapter declares sandbox.merge_json:
    {repo-relative-path: patch}; we deep-merge the patch into that
    project-scoped file (preserving any keys the fork set)."""
    if not os.environ.get("SC_SANDBOX"):
        return []
    return _merge_json_spec((adapter.get("sandbox") or {}).get("merge_json") or {}, root)


def ensure_worktree(work_dir: Path, shortname: str) -> None:
    """Create a git worktree for a shell at work_dir on branch shell/<shortname>.

    Idempotent: if work_dir already exists, assumes the worktree is intact and
    returns immediately. Creates the branch from HEAD if it doesn't exist yet;
    checks it out if it does. Exits with a clear message on git failure.
    """
    if work_dir.exists():
        return
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    branch = f"shell/{shortname.lower()}"
    existing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "branch", "--list", branch],
        capture_output=True, text=True,
    )
    branch_exists = bool(existing.stdout.strip())
    cmd = ["git", "-C", str(REPO_ROOT), "worktree", "add", str(work_dir)]
    cmd += [branch] if branch_exists else ["-b", branch]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"FATAL: could not create worktree at {work_dir}:\n{result.stderr.strip()}")


def link_worktree_map(work_dir: Path) -> "str | None":
    """Point a shell worktree's .sc-state/map.db at the ROOT's map DB.

    The dr_* repo map is a single derived cache at the main repo root (built by
    `./sc map`; read by map_db.py / compose.py via __file__, so writers + the
    renderer already resolve the root correctly). But boot.md and the map skills
    tell shells to query `sqlite3 .sc-state/map.db` — a CWD-relative path. From a
    worktree that file doesn't exist, and the sqlite3 CLI CREATES an empty one on
    open, which then shadows the root map for that worktree ('no such table:
    dr_section'). A symlink makes the documented path resolve to the real root DB
    from every worktree; sqlite keeps its -wal/-shm next to the resolved (root)
    file, so no stray sidecars land in the worktree. We do NOT commit the cache
    (it's a derived binary; the authored layer is tracked as map_content.sql).

    Healed every boot: an empty/stale shadow left by a pre-fix session — or stray
    -wal/-shm sidecars — are cleared and replaced with the symlink. A dangling
    link (root not mapped yet) is fine: the first query creates the DB at the
    root, where `./sc map` then populates it."""
    sc_state = work_dir / ".sc-state"
    link = sc_state / "map.db"
    target = REPO_ROOT / ".sc-state" / "map.db"
    try:
        sc_state.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.readlink() == target:
                return None
            link.unlink()
        elif link.exists():
            link.unlink()  # empty/stale per-worktree shadow — root is canonical
        for sidecar in ("map.db-wal", "map.db-shm"):
            p = sc_state / sidecar
            if p.exists() and not p.is_symlink():
                p.unlink()
        link.symlink_to(target)
    except OSError as e:
        return f"→ map link: skipped ({e})"
    return None


def trust_codex_worktree(work_dir: Path) -> "str | None":
    """Mark a codex shell's worktree as a trusted project in codex's config, so
    its project-local .codex/hooks.json (the branch-guard) actually LOADS.

    codex loads project-local hooks ONLY when the project's .codex/ layer is
    trusted, and trust is keyed per-directory. Shells run in worktrees, which are
    NOT the trusted main root — so without this the branch-guard never loads and
    codex worktree shells run with NO edit-time guard. (Verified: interactive
    codex fires the PreToolUse hook iff the project is trusted; `codex exec` runs
    no hooks at all, and `--dangerously-bypass-hook-trust` only skips per-hook
    hash review, not this layer-load trust.)

    Idempotent text-append to $CODEX_HOME/config.toml (default ~/.codex). This is
    the one place the engine writes under the codex home — additive project-trust
    only, never auth/history (an FnB-approved deviation from the otherwise
    hands-off ~/.codex policy)."""
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    cfg = home / "config.toml"
    header = f'[projects."{work_dir}"]'
    try:
        text = cfg.read_text() if cfg.exists() else ""
        if header in text:
            return None  # already trusted (codex writes exactly this stanza)
        home.mkdir(parents=True, exist_ok=True)
        sep = "" if (not text or text.endswith("\n")) else "\n"
        with cfg.open("a") as f:
            f.write(f'{sep}\n{header}\ntrust_level = "trusted"\n')
        return f"→ codex: trusted worktree layer (hooks load) → {cfg}"
    except OSError as e:
        return f"→ codex trust: skipped ({e})"


def _git(work_dir: Path, *args: str, timeout: int = 15) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(work_dir), *args],
                          capture_output=True, text=True, timeout=timeout)


def sync_worktree(work_dir: Path, shortname: str) -> str:
    """Bring a shell's worktree base in line with the default branch — or say
    why not. Returns a one-line status for the boot doc + launch print.

    Doctrine: `shell/<shortname>` is a MOVING BASE pinned to origin/<default>,
    not a content branch — work happens on feature branches cut from it, so a
    worktree is born at first-boot HEAD and drifts as PRs merge unless someone
    moves it. This does, when provably nothing can be lost: HEAD is the shell
    base branch, the tree is clean, and there are no local-only commits → fetch
    + `reset --hard origin/<default>` (NEVER pull/merge: merge bubbles on a
    long-lived branch, and squash-merged work replays as conflicts). Anything
    local → no touch; the status tells the shell to surface it to the FnB
    (git skill, 'Sync before you start'). Soft-fails on network/timeout —
    an offline boot must never block on a drift check.
    """
    default = (os.environ.get("SC_PROTECTED_BRANCHES") or "main").split()[0]
    upstream = f"origin/{default}"
    try:
        if _git(work_dir, "fetch", "origin", default, "--quiet",
                timeout=20).returncode != 0:
            return f"drift check skipped (could not fetch {upstream} — offline?)"
        if _git(work_dir, "rev-parse", "--verify", "--quiet",
                upstream).returncode != 0:
            return f"drift check skipped (no {upstream})"
        behind = int(_git(work_dir, "rev-list", "--count",
                          f"HEAD..{upstream}").stdout.strip() or 0)
        ahead = int(_git(work_dir, "rev-list", "--count",
                         f"{upstream}..HEAD").stdout.strip() or 0)
        dirty = bool(_git(work_dir, "status", "--porcelain").stdout.strip())
        branch = _git(work_dir, "symbolic-ref", "--short", "HEAD").stdout.strip()

        local = [p for p, on in ((f"{ahead} unmerged local commit(s)", ahead),
                                 ("uncommitted changes", dirty)) if on]
        if behind == 0:
            note = f" ({'; '.join(local)})" if local else ""
            return f"in sync with {upstream}{note}"
        if branch != f"shell/{shortname.lower()}":
            return (f"{behind} behind {upstream}, mid-work on `{branch}` — not "
                    "auto-synced; land or stash first (git skill: 'Sync before "
                    "you start')")
        if local:
            return (f"⚠ {behind} behind {upstream} with {' + '.join(local)} — "
                    "NOT auto-synced. Surface the local work to the FnB before "
                    "doing anything else (git skill: 'Sync before you start')")
        if _git(work_dir, "reset", "--hard", upstream).returncode != 0:
            return f"⚠ {behind} behind {upstream} — auto-sync FAILED; see git skill"
        return f"auto-synced to {upstream} (was {behind} behind; nothing local to lose)"
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return "drift check skipped (git timed out or errored)"


def ensure_harness_path() -> None:
    """Prepend the dirs where the official installers drop harness binaries onto
    this process's PATH, so detection (shutil.which) and exec (execvpe) agree
    with what `./sc install` / `./sc ensure-harness` installed.

    The opencode installer drops its binary in ~/.opencode/bin and only edits a
    shell rc — a dir a fresh launch shell does NOT carry on PATH. Without this,
    detect_harnesses() silently never offers opencode even though ensure-harness
    reported it installed: install.py trusts HARNESS_BIN, the launcher trusted
    PATH only, and they disagreed. Reuse install.HARNESS_BIN so there is one
    source for where a harness lives.

    In the sandbox this is a no-op: the image's ENV PATH already carries every
    baked binary dir, and folding host dirs in is actively wrong for kimi —
    host `~/.kimi-code` (its bin/ + config in one dir) is bind-mounted for
    creds, and prepending its bin/ would shadow the image's own kimi binary
    with the host's (a darwin binary on a macOS host)."""
    if os.environ.get("SC_SANDBOX"):
        return
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
    print(f"\n{style.bold('Harness:')}")
    for i, h in enumerate(detected, 1):
        mark = style.dim("  (default)") if h == dflt else ""
        name = style.bold(h) if h == dflt else h
        print(f"  {style.dim(f'{i}.')} {name}{mark}")
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


def open_db():
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(
            f"FATAL: no usable DB at {DB_PATH}.\n"
            f"  Rebuild it from text:  ./sc rebuild"
        )
    con = db_driver.connect(DB_PATH)
    con.execute("SELECT 1 FROM shells LIMIT 1")  # smoke
    return con


# ── Auth (username-only) ────────────────────────────────────────────────────

def authenticate(con, interactive: bool = True):
    # SC_USER env wins; else prompt on a TTY; else (headless: `./sc verify`, CI)
    # default to the first active user so launch doesn't EOFError without a TTY.
    # `interactive=False` (an `./sc run` headless boot) never prompts even on a
    # TTY — the caller is usually another shell's session, not an operator.
    username = os.environ.get("SC_USER")
    if not username:
        if interactive and sys.stdin.isatty():
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

def list_shells(con, user_id: int) -> list:
    shells = [dict(row) for row in con.execute(
        "SELECT shell_id, display_name, shortname, mandate, is_shared, flavor, "
        "current_state FROM shells "
        "WHERE (user_id=? OR is_shared=1) AND COALESCE(is_deleted,0)=0 "
        "ORDER BY flavor IS NULL, flavor, shell_id",
        (user_id,),
    ).fetchall()]
    refs_by_shell = [_sprint_doc_refs(shell) for shell in shells]
    referenced = set().union(*refs_by_shell)
    active = set()
    if referenced:
        placeholders = ",".join("?" for _ in referenced)
        docs = con.execute(
            f"SELECT document_id, frozen, body FROM documents "
            f"WHERE kind='doc' AND document_id IN ({placeholders})",
            tuple(sorted(referenced)),
        ).fetchall()
        active = {doc["document_id"] for doc in docs
                  if _sprint_doc_is_active(doc)}
    for shell, refs in zip(shells, refs_by_shell):
        shell["sprint_reserved"] = bool(refs & active)
    return shells


def flavor_defaults(con) -> dict:
    """flavor -> {'default_harness', 'models': {harness: model}} launch defaults.
    The (flavor, harness) matrix: each flavor names a model per harness, and one
    harness is the picker default (is_default). Empty if the table is absent
    (older fork mid-migration) so the launcher degrades to its prior behavior
    rather than failing."""
    try:
        rows = con.execute(
            "SELECT flavor, harness, model, is_default FROM flavor_defaults")
    except db_driver.OperationalError:
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


def _sprint_doc_refs(shell) -> set[int]:
    """Tracker document ids named by current_state's sprint marker lines."""
    current_state = dict(shell).get("current_state") or ""
    refs = set()
    prefix = "SPRINT doc="
    for line in current_state.splitlines():
        marker = line.strip()
        if not marker.startswith(prefix):
            continue
        value = marker[len(prefix):].split(maxsplit=1)[0]
        if value.isdigit():
            refs.add(int(value))
    return refs


def _sprint_doc_is_active(doc) -> bool:
    """The tracker contract says ACTIVE, and freeze has not revoked authority."""
    if doc["frozen"]:
        return False
    for line in (doc["body"] or "").splitlines():
        field, separator, value = line.strip().partition(":")
        if field == "status" and separator:
            status = value.strip().split(maxsplit=1)
            return bool(status) and status[0] == "ACTIVE"
    return False


def _is_sprint_reserved(shell) -> bool:
    """Picker-only annotation computed by list_shells; never a boot gate."""
    return bool(dict(shell).get("sprint_reserved"))


def _shell_status(shell, snap: "dict | None") -> str:
    """Styled picker status derived from liveness plus sprint reservation."""
    if shell["flavor"] == "admin":
        label, paint = "Exempt", style.dim
    elif not snap or not snap.get("supported"):
        label, paint = "Unknown", style.dim
    else:
        state = shell_liveness.session_state(shell["shortname"] or "", snap)
        if state == "busy":
            label, paint = "Busy", style.amber
        elif state == "orphan":
            label, paint = "Orphaned", style.red
        elif snap.get("indeterminate"):
            label, paint = "Unknown", style.dim
        elif _is_sprint_reserved(shell):
            label, paint = "Sprint", style.amber
        else:
            label, paint = "Available", style.green
    return f"{paint(label)}{' ' * (12 - len(label))}"


def confirm_live(shell, snap: "dict | None") -> bool:
    """Interactive twin of the headless liveness refusal: booting a shell whose
    worktree already hosts a live session runs two sessions against one tree +
    one memory row set, so warn and put the call to the operator. True → boot
    (dormant, admin-exempt, no snapshot, or the operator said yes)."""
    if not snap or shell["flavor"] == "admin" or not shell["shortname"]:
        return True
    state = shell_liveness.session_state(shell["shortname"], snap)
    if state is None:
        return True
    pids, orphans = shell_liveness.orphan_split(shell["shortname"], snap)
    if state == "orphan":
        print(style.yellow(
            f"\n  ⚠ {shell['shortname']} slot is held by an ORPHANED session "
            f"(pid {', '.join(map(str, orphans))} — terminal closed / parent "
            f"gone)."))
        print(style.dim(
            f"    Verify it is idle (`ps -o etime=,stat= -p {orphans[0]}`; no "
            f"busy children), `kill` it, then boot. An orphan can still be "
            f"mid-work — never kill unverified."))
    else:
        print(style.yellow(
            f"\n  ⚠ {shell['shortname']} already has a live session "
            f"(pid {', '.join(map(str, pids))}) — one shell, one session."))
    return input("  Boot anyway? [y/N]: ").strip().lower() in ("y", "yes")


def pick_shell(shells: list, requested: str | None,
               first: bool, defaults: dict | None = None,
               snap: "dict | None" = None):
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
    # Interactive picker — shells grouped by flavor, each group labelled. The
    # pick number is the row's 1-based position in the already-grouped list, so
    # it always reads 1, 2, 3… down the screen. shell_id is global and
    # non-contiguous, so showing it here made the numbering jump around within a
    # group; position tracks the display order instead.
    print(style.dim(f"\n{'#':>3}  {'Name':<16}{'Shortname':<14}{'Status':<12}"
                    f"{'Default (harness · model)'}"))
    _sentinel = object()
    cur_flavor: object = _sentinel
    for n, s in enumerate(shells, 1):
        if s["flavor"] != cur_flavor:
            cur_flavor = s["flavor"]
            print(f"\n{style.accent(cur_flavor or '(bespoke)')}")
        num = style.dim(f"{n:>3}")
        name = style.bold("{:<16}".format(s["display_name"] or ""))
        short = "{:<14}".format(s["shortname"] or "")
        print(f"{num}  {name}{short}{_shell_status(s, snap)}"
              f"{style.dim(_default_label(defaults, s['flavor']))}")
    if snap and snap.get("indeterminate"):
        print(style.dim(f"\n  ⚠ {snap['indeterminate']} harness process(es) "
                        f"with unreadable cwd — liveness markers are partial."))
    while True:
        choice = input("\nPick (#): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(shells):
            chosen = shells[int(choice) - 1]
            if not confirm_live(chosen, snap):
                continue          # operator declined — back to the picker
            return chosen
        print("  invalid choice")


# ── Session archive ─────────────────────────────────────────────────────────

def _is_unused(narrative: str) -> bool:
    """A freshly-opened session whose narrative is still just the 'Session start'
    stub (no work appended). Detected by a single timestamp entry."""
    return (narrative or "").count("\n[") <= 1


def session_provider(harness: str, model: "str | None") -> "str | None":
    """Boot-time provider for the archive row. opencode model ids are
    provider-prefixed ("ollama-cloud/<model>") — the prefix wins; otherwise the
    harness's home provider (claude→anthropic, codex→openai, vibe→mistral).
    model_catalog maps kimi→"kimi-for-coding" for the model datalist, but its
    wire.jsonl reports provider="kimi" natively — pin that value here (ahead of
    the map lookup) so boot-row and sweep-row providers agree."""
    if harness in model_catalog.PREFIXED_HARNESSES and model and "/" in model:
        return model.split("/", 1)[0]
    if harness == "kimi":
        return "kimi"
    return model_catalog.HARNESS_PROVIDER.get(harness)


def open_session(con, shell_id: int,
                 lifecycle: "dict | None" = None) -> tuple[str, int]:
    """`lifecycle` carries the launch telemetry persisted onto the archive row
    (started_at/harness/provider/model/sprint_ref — migration 0071). ended_at is
    NOT written here: run.py execs the harness, so no code runs at exit; the
    analytics sweep backfills it from harness session data."""
    life = {"started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **(lifecycle or {})}
    life_cols = ["started_at", "harness", "provider", "model", "sprint_ref"]
    # Reuse the active session if it was opened but never used (e.g. install
    # opened session 0001, or a prior launch did no work) — avoids phantom empty
    # sessions and the incidental first-snapshot diff. The reused stub becomes
    # THIS launch's session, so its lifecycle is overwritten with this launch's.
    active = con.execute(
        "SELECT active_archive_id FROM shells WHERE shell_id=?", (shell_id,)
    ).fetchone()[0]
    if active:
        row = con.execute(
            "SELECT archive_id, session_id, full_narrative FROM shell_memory_archives "
            "WHERE archive_id=?", (active,)
        ).fetchone()
        # …but a stub that actually LAUNCHED a harness session is not unused:
        # attributed usage rows prove real spend under this archive's lifecycle,
        # and reusing it would overwrite that lifecycle with this boot's (three
        # headless one-shots once collapsed into one kimi-flavored archive).
        # The pre-session sweep runs before this, so attribution is current.
        if row and _is_unused(row["full_narrative"]) and not con.execute(
                "SELECT 1 FROM session_token_usage WHERE archive_id=? LIMIT 1",
                (row["archive_id"],)).fetchone():
            con.execute(
                f"UPDATE shell_memory_archives SET {', '.join(c + '=?' for c in life_cols)} "
                "WHERE archive_id=?",
                [life.get(c) for c in life_cols] + [row["archive_id"]])
            con.commit()
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
        "INSERT INTO shell_memory_archives "
        f"(shell_id, session_id, date, full_narrative, {', '.join(life_cols)}) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' for _ in life_cols)})",
        [shell_id, session_id, today, narrative] + [life.get(c) for c in life_cols],
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

def _port_listening(port: int) -> bool:
    """Is anything serving on 127.0.0.1:port right now? Best-effort, fast."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def review_gui_panel(api_port: int, has_key: bool) -> str:
    """The one URL the operator always needs — every fork serves the review
    GUI on its own port, so every boot restates it, prominently."""
    url = f"http://127.0.0.1:{api_port}"
    status = (style.green("up") if _port_listening(api_port)
              else style.yellow("not running — ./sc serve"))
    token = "SC_API_TOKEN set" if has_key else "no api key"
    return style.panel([
        f"{style.bold('Review GUI')}  {style.cyan(style.bold(url))}",
        f"{status}{style.dim(' · api on the same port · ' + token)}",
    ])


def main() -> None:
    args = sys.argv[1:]
    first = "--first" in args
    headless = "--headless" in args
    # --harness <name> / --harness=<name> forces the harness and skips the
    # picker; its value must not be mistaken for the shell shortname positional.
    # Headless adds -p/--prompt and -m/--model (value-taking, same rule).
    flag_harness = None
    flag_model = None
    flag_effort = None
    prompt = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--harness":
            flag_harness = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in ("-m", "--model"):
            flag_model = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a == "--effort":
            flag_effort = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in ("-p", "--prompt"):
            prompt = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--harness="):
            flag_harness = a.split("=", 1)[1]
        elif a.startswith("--model="):
            flag_model = a.split("=", 1)[1]
        elif a.startswith("--effort="):
            flag_effort = a.split("=", 1)[1]
        elif a.startswith("--prompt="):
            prompt = a.split("=", 1)[1]
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    requested = positional[0] if positional else None
    if headless and not requested:
        sys.exit('usage: ./sc run <shortname> [-p "<prompt>"] [--harness <h>] '
                 '[-m <model>] [--effort <level>]')

    # Wordmark banner — interactive boots only; headless/verify logs stay clean.
    if not headless and not os.environ.get("RENDER_ONLY") and sys.stdin.isatty():
        print(style.banner(REPO_ROOT.name))

    con = open_db()
    # Self-heal stale engine skills before anything this boot reads them
    # (compose's SKILLS block, render_skill_md). A DB stranded by an in-place
    # `0001` regen repairs itself from assets/skills/ instead of needing a manual
    # `./sc rebuild`. Project-local skills are never touched (no upstream to lag).
    # Skipped under RENDER_ONLY: headless verify must not mutate, and it rebuilds
    # fresh anyway. Best-effort — a heal failure never blocks a launch.
    heal_note = None
    if not os.environ.get("RENDER_ONLY"):
        try:
            healed = seed_skills.sync_engine_skills(con)
            if healed:
                heal_note = f"{len(healed)} stale engine skill(s) → {', '.join(healed)}"
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            heal_note = None

    user = authenticate(con, interactive=not headless)
    fdefaults = flavor_defaults(con)
    # Liveness snapshot for the interactive picker: one /proc pass (ms) so the
    # boot list can show shell status — Busy / Orphaned / Sprint / Available /
    # Exempt — and
    # confirm before booting into a live worktree. Headless keeps its own lazy
    # compute below; non-TTY boots (--first, piped) can't confirm, so no snap.
    snap = (shell_liveness.compute()
            if not headless and sys.stdin.isatty() else None)
    chosen = pick_shell(list_shells(con, user["user_id"]), requested, first,
                        fdefaults, snap)
    # Direct interactive boots (`./sc enter dev3`) skip the picker and its
    # confirm — run the same guard here. Picker path already confirmed.
    if requested and not headless and not confirm_live(chosen, snap):
        sys.exit(f"aborted — shell '{chosen['shortname']}' has a live session "
                 f"(one shell, one session; see shell_liveness)")

    # Liveness guard (headless): one shell, one session. A headless boot
    # into a worktree that already hosts a live harness would run two sessions
    # of the same shell against one tree + one memory row set. Interactive
    # boots warn + confirm (above); `sc run` is scripted, so it refuses.
    # Admin boots at the repo root (no worktree signal), so it isn't guarded.
    if headless and chosen["shortname"] and chosen["flavor"] != "admin":
        snap = shell_liveness.compute()
        if snap.get("supported") and shell_liveness.is_active(chosen["shortname"], snap):
            pids, orphans = shell_liveness.orphan_split(chosen["shortname"], snap)
            if pids and len(orphans) == len(pids):
                # The slot-holder outlived its terminal/parent — still refuse
                # (it may be mid-work), but name the fix instead of a dead end.
                sys.exit(
                    f"sc run: shell '{chosen['shortname']}' slot is held by an "
                    f"ORPHANED session (pid {', '.join(map(str, orphans))} — "
                    f"terminal closed / parent gone). Verify it is idle "
                    f"(`ps -o etime=,stat= -p <pid>`; no busy children), "
                    f"`kill <pid>`, then re-run. An orphan can still be "
                    f"mid-work — never kill unverified.")
            sys.exit(f"sc run: shell '{chosen['shortname']}' already has a live "
                     f"session — one shell, one session (see shell_liveness)")
        if snap.get("supported") and snap.get("indeterminate"):
            print(f"→ liveness: {snap['indeterminate']} unreadable harness process(es) — "
                  f"proceeding, but liveness was indeterminate")

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
               or pick_harness(detect_harnesses(), default_harness, first or headless)
               or default_harness)

    # Resolve + validate the complete headless route before opening a session.
    # `sc run` is the sprint-worker primitive, so high effort is its default;
    # the orchestration skill passes it explicitly as well for auditability.
    flavor_model = fdef["models"].get(harness) if fdef else None
    session_model = (resolve_headless_model(flag_model, fdef, harness)
                     if headless else flavor_model)
    session_effort = flag_effort or ("high" if headless else None)
    adapter = load_adapter(harness)
    if headless:
        try:
            validate_headless_request(adapter, session_model, session_effort)
        except ValueError as e:
            sys.exit(f"sc run: {e}")

    feedback = not headless and not os.environ.get("RENDER_ONLY")
    map_note = None
    trust_note = None
    with style.spinner("sweeping analytics", enabled=feedback) as spinner:
        # Now that the harness is known, resolve THIS flavor's model for it (the
        # (flavor, harness) cell). None when the flavor has no entry for the chosen
        # harness (e.g. opencode as a manual fallback) — then the harness picks its own.
        # Pre-session analytics sweep (doc #11): pull harness-side usage data into
        # session_token_usage + backfill the PREVIOUS session's ended_at. MUST run
        # before open_session — the stub-reuse check there relies on the previous
        # boot's session being attributed to its archive already. Incremental
        # (mtime-gated), so steady-state cost is near zero; the first-ever sweep of
        # harness history is the one large pass. Best-effort like the prune — a
        # broken parser must never block a boot. Skipped under RENDER_ONLY
        # (headless verify must not mutate).
        sweep_note = None
        if not os.environ.get("RENDER_ONLY"):
            try:
                import analytics
                s = analytics.sweep(quiet=True)
                if s["inserted"] or s["updated"]:
                    sweep_note = (f"{s['inserted']} new, {s['updated']} refreshed "
                                  f"session-usage row(s)")
            except Exception:
                sweep_note = None

        spinner.label = "opening session"
        # The model this launch will actually route (headless resolves via flags →
        # flavor default; interactive routes the flavor default). None = the harness
        # picks its own — recorded as NULL, honest about what we know at boot.
        session_id, archive_id = open_session(con, chosen["shell_id"], lifecycle={
            "harness": harness,
            "provider": session_provider(harness, session_model),
            "model": session_model,
            "sprint_ref": os.environ.get("SC_SPRINT_REF") or None,
        })

        full = con.execute(
            "SELECT shell_id, display_name, shortname, partner, role, mandate, "
            "current_state, system_prompt, connections, flavor, api_key FROM shells WHERE shell_id=?",
            (chosen["shell_id"],),
        ).fetchone()
        api_port = ports_mod.resolve().get("port")

        # Every shell gets an isolated git worktree so parallel shells can work on
        # separate branches without clobbering each other — planner/reviewer commit
        # their own artifacts (specs, snapshots, state) there too. All artifacts
        # (CLAUDE.md, AGENTS.md, skills, harness config) land in the worktree root;
        # the harness is exec'd from there. The ONE exception is the admin flavor:
        # it maintains `main` itself (engine updates, migrations, applying approved
        # patches), so it boots in the repo root — no worktree, no shell/* branch.
        # The branch-guard exempts it via SC_SHELL_FLAVOR (exported at exec below).
        work_dir = REPO_ROOT
        sync_note = None
        if chosen["shortname"] and chosen["flavor"] != "admin":
            spinner.label = "syncing worktree"
            work_dir = REPO_ROOT / ".sc-worktrees" / chosen["shortname"].lower()
            ensure_worktree(work_dir, chosen["shortname"])
            sync_note = sync_worktree(work_dir, chosen["shortname"])
            map_note = link_worktree_map(work_dir)
            if harness == "codex":
                trust_note = trust_codex_worktree(work_dir)

        # Repo-global branch hygiene: delete local branches whose PR is provably
        # merged (git_hygiene's `stale` set — gh-confirmed MERGED, never a base or a
        # checked-out branch). The unattended subset of the git_cleanup skill, run
        # once per boot from whichever shell launches next. Best-effort and silent:
        # soft-fails so it never blocks a launch, and surfaces a line only when it
        # actually removed something. Skipped under RENDER_ONLY (headless verify must
        # not mutate) and opt-out-able per fork via SC_NO_AUTOPRUNE=1.
        prune_note = None
        if not os.environ.get("SC_NO_AUTOPRUNE") and not os.environ.get("RENDER_ONLY"):
            spinner.label = "pruning merged branches"
            try:
                prune_note = git_prune.status_line(git_prune.prune(fetch=False))
            except Exception:
                prune_note = None

        spinner.label = "rendering boot doc + skills"
        content = compose_boot(con, full, user, session_id, archive_id,
                               work_dir=work_dir if work_dir != REPO_ROOT else None,
                               sync_note=sync_note,
                               source_mode=install.is_source_repo(),
                               api_key=full["api_key"],
                               api_port=api_port)

        # Render this shell's granted skills to .claude/skills/<name>/SKILL.md —
        # harness-consumed, gitignored, rebuilt per boot (like the boot artifact).
        skills = flat.render_skill_md(con, full["shell_id"], work_dir)
        con.close()

        # One compose, two outputs — Claude Code reads CLAUDE.md, the AGENTS.md
        # harnesses read AGENTS.md. Both at the working directory root.
        for name in ("CLAUDE.md", "AGENTS.md"):
            atomic_write(work_dir / name, content)

    if map_note:
        print(map_note)
    if trust_note:
        print(trust_note)

    print(f"\n→ booted {style.bold(full['display_name'])} "
          f"(shell_id={full['shell_id']}, session={session_id})")
    if work_dir != REPO_ROOT:
        print(f"→ worktree: {work_dir}")
        print(f"→ sync: {sync_note}")
    elif chosen["flavor"] == "admin":
        print("→ working dir: repo root (admin — maintains main directly)")
    if heal_note:
        print(f"→ heal: {heal_note}")
    if prune_note:
        print(f"→ prune: {prune_note}")
    if sweep_note:
        print(f"→ analytics: {sweep_note}")
    print(f"→ wrote {work_dir / 'CLAUDE.md'}")
    print(f"→ wrote {work_dir / 'AGENTS.md'}")
    if headless and api_port and full["api_key"]:
        print(f"→ api: http://127.0.0.1:{api_port} (SC_API_TOKEN set)")
    print(f"→ skills: {len(skills['written'])} written, "
          f"{len(skills['skipped'])} unchanged → .claude/skills/")

    # Harness was resolved up front (override / picker / default); the adapter
    # seam owns the launch command + any harness-specific config to emit.
    emitted = emit_adapter(adapter, work_dir)
    resolve_opencode_plugins(work_dir)  # engine-relative plugin path → absolute (loads in worktrees)
    print(f"→ harness: {style.bold(harness)} "
          f"(reads {adapter.get('boot_artifact', 'AGENTS.md')})")
    if emitted:
        print(f"→ emitted {', '.join(emitted)}")

    # Flavor model default: route the model to the harness the operator picked.
    # The adapter declares HOW it takes a model — a launch flag (claude/codex:
    # `--model <id>`) or a config-file key (opencode: opencode.json "model"). A
    # NULL flavor model, or a harness declaring neither, skips this. Still
    # overridable in-session / via the harness's own `-m`. Headless resolves
    # its model separately (flags → flavor default) through the headless
    # block's model_flag, so this interactive routing is skipped there.
    model_args: list[str] = []
    mcfg = adapter.get("model") or {}
    if headless:
        pass
    elif flavor_model and mcfg.get("flag"):
        model_args = [mcfg["flag"], flavor_model]
        print(f"→ model: {flavor_model} (flavor default for {chosen['flavor']})")
    elif flavor_model and mcfg.get("file"):
        mfile = work_dir / mcfg["file"]
        if mfile.exists():
            try:
                cfg = json.loads(mfile.read_text())
            except (json.JSONDecodeError, OSError):
                cfg = {}
            cfg[mcfg.get("key", "model")] = flavor_model
            atomic_write(mfile, json.dumps(cfg, indent=2) + "\n")
            print(f"→ model: {flavor_model} (flavor default for {chosen['flavor']})")
    merged = apply_merge_json(adapter, work_dir)
    if merged:
        print(f"→ harness config → {', '.join(merged)}")
    sandboxed = apply_sandbox(adapter, work_dir)
    if sandboxed:
        print(f"→ sandbox: allow-all permissions → {', '.join(sandboxed)}")

    # Sandbox-only launch flags — e.g. codex's approval/sandbox bypass, safe
    # because the container is the safety boundary. The no-docker host path keeps
    # the harness's normal prompts (SC_SANDBOX unset). The two flag sets are
    # disjoint by launch mode: `launch_flags` for interactive, `headless_flags`
    # for headless — a non-interactive run can't answer a permission prompt (it
    # auto-denies and the worker silently stalls), so e.g. claude gets its bypass
    # flag there; codex declares the same flag in both. They are NOT folded
    # together because a harness's interactive flag can be invalid headless —
    # `kimi -p` hard-errors on `--yolo`/`--auto` (prompt mode is always
    # auto-permission, no flag needed).
    sandbox_flags: list[str] = []
    sandbox_env: dict[str, str] = {}
    if os.environ.get("SC_SANDBOX"):
        scfg = adapter.get("sandbox") or {}
        key = "headless_flags" if headless else "launch_flags"
        sandbox_flags = list(scfg.get(key) or [])
        if sandbox_flags:
            print(f"→ sandbox: launch flags → {' '.join(sandbox_flags)}")
        # Sandbox-only launch env — e.g. claude's IS_SANDBOX=1, required because
        # the rootless container runs the harness as uid 0 and claude refuses
        # bypass-permissions mode as root unless the env marks it as sandboxed.
        sandbox_env = {k: str(v) for k, v in (scfg.get("env") or {}).items()}
        if sandbox_env:
            print(f"→ sandbox: launch env → {' '.join(sandbox_env)}")

    # Headless: resolve the non-interactive argv now (before RENDER_ONLY) so a
    # render-only run still validates the adapter + prints what would exec.
    headless_cmd = None
    if headless:
        hmodel = session_model  # resolved up front (persisted on the archive row)
        headless_cmd = headless_command(
            adapter, prompt or DEFAULT_HEADLESS_PROMPT, hmodel, sandbox_flags,
            session_effort)
        if headless_cmd is None:
            sys.exit(f"sc run: harness '{harness}' has no headless adapter — "
                     f"use claude, codex, opencode, or kimi")
        if hmodel:
            src = "explicit -m" if flag_model else f"flavor default for {chosen['flavor']}"
            print(f"→ model: {hmodel} ({src})")
        print(f"→ effort: {session_effort}")
        print(f"→ headless prompt: {(prompt or DEFAULT_HEADLESS_PROMPT)[:120]}")

    # Close the boot summary with the review GUI — the link lives in a different
    # place per fork, so every interactive boot restates it where it can't be
    # missed. Headless/verify keep the plain `→ api:` line instead.
    if not headless and not os.environ.get("RENDER_ONLY") and api_port:
        print(f"\n{review_gui_panel(api_port, bool(full['api_key']))}")

    if os.environ.get("RENDER_ONLY"):
        print("→ RENDER_ONLY set — not exec'ing the harness.")
        return

    # --name labels the session in the harness prompt box, resume picker, and
    # the terminal title — the cross-terminal way to show which shell you're in
    # (Konsole's tab is patched separately, since it ignores the program title).
    # Adapter-declared, so only harnesses that support it (claude) get the flag.
    name_args: list[str] = []
    ncfg = adapter.get("name") or {}
    if not headless and ncfg.get("flag") and full["display_name"]:
        name_args = [ncfg["flag"], full["display_name"]]

    cmd = (headless_cmd if headless else
           (adapter.get("launch") or [harness]) + name_args + model_args + sandbox_flags)
    effort_env = headless_effort_env(adapter, session_effort) if headless else {}
    env = {**os.environ, **{k: str(v) for k, v in adapter.get("env", {}).items()},
           **sandbox_env, **effort_env}
    # The booted shell's flavor, inherited by everything the harness spawns.
    # branch-guard.sh reads it to exempt the admin shell (which works on main
    # by mandate); like SC_PROTECTED_BRANCHES it's a guardrail, not a boundary.
    env["SC_SHELL_FLAVOR"] = chosen["flavor"] or ""
    env["SC_API_TOKEN"] = full["api_key"] or ""
    env["SC_API_BASE"] = f"http://127.0.0.1:{api_port}" if api_port else ""
    # Optional fast-path for the branch-guard hooks: the absolute engine path, so
    # they skip the `git rev-parse --git-common-dir` walk. NOT load-bearing — the
    # hooks resolve the engine env-independently (a fork gitignores .super-coder/,
    # so it is absent from worktrees; a worktree-relative path failed open). This
    # just saves a subshell per edit on the normal launch path.
    env["SC_ENGINE_DIR"] = str(ENGINE)
    # The shell's HOME worktree — the dir we exec the harness from (below). The
    # branch-guard reads it to judge "outside your worktree" against the assigned
    # tree, not the live cwd: a shell whose cwd has drifted to the repo root (to
    # run a root-level command) is still working correctly when it edits into its
    # own worktree, and must not be warned. For admin this is REPO_ROOT, but admin
    # exits the guard earlier via SC_SHELL_FLAVOR, so it never reads this.
    env["SC_SHELL_WORKTREE"] = str(work_dir)
    # cwd-proofing (the recurring "my edits vanished" trap). The engine + its live
    # DBs sit at the MAIN worktree root, but the harness is exec'd from the shell's
    # own worktree (os.chdir(work_dir) below). Historically a shell would `cd` to the
    # root for a convenient `./sc …` call — and because Bash cwd persists, every
    # LATER bare git/grep then silently targeted the main tree (a different branch),
    # so the shell's own worktree edits looked gone. Kill the trigger structurally:
    # export the root and prepend it to PATH so `sc …` resolves bare from ANY cwd,
    # and raw DB reads can address the engine by $SC_ROOT — no `cd` ever needed. One
    # invariant ("never cd; address the engine by path") instead of per-command
    # vigilance. Works in both the docker sandbox and the no-docker host path since
    # run.py is the single exec chokepoint for every harness.
    env["SC_ROOT"] = str(REPO_ROOT)
    env["PATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PATH", "")])
    # Operator-declared shared dirs that all shells may write into without
    # branch-guard warnings — host-level handoff/screenshot folders. Set
    # SC_SHARED_DIRS (space-separated absolute paths) in the launch environment;
    # run.py passes it through automatically (via {**os.environ} above), so no
    # explicit assignment is needed. This comment documents it as a first-class
    # supported env var alongside SC_PROTECTED_BRANCHES and SC_SHELL_WORKTREE.
    if not headless:
        set_terminal_tab_title(full["display_name"])
    os.chdir(work_dir)
    print(f"→ exec {' '.join(cmd)}\n")
    os.execvpe(cmd[0], cmd, env)


def set_terminal_tab_title(name: str) -> None:
    """Best-effort: pin this Konsole tab's title to the shell's name.

    Konsole's default tab format is ``%d : %n`` (dir : program), which ignores
    the window-title escapes the harness emits — so the tab never shows which
    shell you're talking to. We run *inside* the shell's Konsole session, so we
    set that session's tab title format to a literal over DBus (the same thing
    the GUI "Rename Tab" does). It persists for the tab and survives the
    harness's own title updates. No-op outside Konsole or if qdbus is absent.

    Non-Konsole terminals get the name via the harness itself (e.g. claude's
    ``--name`` writes it into the window title); this only patches Konsole's
    tab, which the standard title escapes can't reach.
    """
    svc = os.environ.get("KONSOLE_DBUS_SERVICE")
    sess = os.environ.get("KONSOLE_DBUS_SESSION")
    if not (svc and sess and name):
        return
    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    if not qdbus:
        return
    for ctx in ("0", "1"):  # 0 = local, 1 = remote (ssh) tab-title context
        try:
            subprocess.run(
                [qdbus, svc, sess,
                 "org.kde.konsole.Session.setTabTitleFormat", ctx, name],
                check=False, capture_output=True, timeout=3,
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
