#!/usr/bin/env python3
"""Reconcile a fork after a super-coder engine update — IN PLACE.

The shell updates its own substrate: it pulls the new engine, lays new
migrations under its own feet, and keeps every row it has written. This is the
local shell handing off to its next boot — not a destructive rebuild. Because
all state lives in the DB and engine code is read live each session, a code-only
update needs no DB work; only schema changes touch the DB, and they do so as
in-place migrations (never a rebuild-from-snapshot, which would revert the DB to
the last snapshot and lose unsnapshotted in-session writes).

B7 model: the engine is a **gitignored, materialized dependency** — it is not
committed to the fork. So an update FETCHES the engine and MATERIALIZES it into
`.super-coder/` (copy from the fetched ref), instead of `git checkout`ing tracked
paths. The upstream SHA is pinned in `.sc-state/engine.ref`; the previous pin is
kept as `.sc-state/engine.ref.prev` — the engine half of the restore point that
makes `./sc rollback` sound (DB + engine restored together).

Flow:
    1. attempt to fast-forward the current checkout with `git pull --ff-only`,
       then fetch upstream engine objects. Checkout sync failures warn but
       never prevent an engine update.
    2. capture the restore point: the current `engine.ref` → `engine.ref.prev`.
    3. materialize the engine paths at the new ref into the
       gitignored `.super-coder/` dir. Per-instance
       content (`.sc-state/`, the DB, instance.json) is never in the materialize
       set, so it survives untouched. --no-fetch reconciles the working tree as-is.
    4. back up the live DB (the other half of the restore point).
    5. migrate IN PLACE — apply only un-applied migrations (ledger-tracked),
       preserving all rows incl. in-session writes. No DB yet (fresh fork) ->
       fall back to a from-text rebuild.
    6. sync the engine skills catalogue (idempotent, id-stable UPSERT) —
       new/changed engine skills reach the fork without a rebuild, while
       project-local skills are left intact.
    7. re-grant common skills to every flavor pack + Bespoke shell.
    8. wire the auto-remap hooks + map the repo + snapshot the (live) state.
    9. only after every step succeeds, atomically publish the new `engine.ref`.

Then review + commit (only `.sc-state/` — content.sql + engine.ref — moves; the
engine is ignored). Restart the session to boot onto the new floor.

The materialize is guarded by the engine hash manifest (engine_manifest.py):
an engine file locally modified since the last materialize BLOCKS the update
(instead of being silently overwritten) until the operator reverts it,
upstreams it, or passes --force to discard it. `--ref <tag|sha>` pins the
materialize to a specific upstream version instead of the branch head.

Usage:
    ./sc update [--no-fetch] [--branch <name>] [--ref <tag|sha>] [--force]
    python3 .super-coder/scripts/update.py [same flags]
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
STATE_DIR = REPO_ROOT / ".sc-state"
ENGINE_REF = STATE_DIR / "engine.ref"
ENGINE_REF_PREV = STATE_DIR / "engine.ref.prev"
PY = sys.executable

sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import callable_floor  # noqa: E402
import db_driver  # noqa: E402
import engine_manifest  # noqa: E402
import install as install_mod  # noqa: E402  (ensure_harnesses)
import migrate as migrate_mod  # noqa: E402
import ports  # noqa: E402
import rebuild as rebuild_mod  # noqa: E402
import seed_skills  # noqa: E402
import shell_factory  # noqa: E402
import skill_projection  # noqa: E402
sys.path.insert(0, str(ENGINE / "render"))
import flat  # noqa: E402

EJECTED_MARKER = STATE_DIR / "ejected"

# The installed materialize set — engine_manifest.py owns the source-repo and
# fresh-install answer. Updates resolve the target ref's list independently;
# this export remains the warned fallback and the stable name callers know.
ENGINE_PATHS = engine_manifest.ENGINE_PATHS

_VISUAL_QA_MARKER_RE = re.compile(
    r"^# managed-by: subfloor — visual-qa shim v(?P<version>\d+)$"
)


def git(
    *args: str,
    check: bool = True,
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess:
    root = repo_root if repo_root is not None else REPO_ROOT
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"update: `git {' '.join(args)}` failed:\n{r.stderr.strip()}")
    return r


def run_script(name: str, *, update_target_ref: str | None = None) -> None:
    # update is an admin operation — pass SC_ADMIN so snapshot/render clear the
    # serialize guard (harmless for non-serializing scripts like map_setup.py).
    env = {**os.environ, "SC_ADMIN": "1"}
    if update_target_ref is not None:
        env["SC_UPDATE_TARGET_REF"] = update_target_ref
    if subprocess.run([PY, str(ENGINE / "scripts" / name)], env=env).returncode != 0:
        sys.exit(f"update: {name} failed.")


def run_update_compat() -> None:
    """Finish a partially adopted legacy update before reconciliation.

    The first old-updater adoption reaches this script through the freshly
    materialized map-setup process. This early call covers recovery when that
    legacy run materialized the new floor but stopped before map setup: the next
    invocation completes the bridge before doing any other update work.
    """
    script = ENGINE / "scripts" / "update_compat.py"
    if not script.is_file():
        return
    result = subprocess.run([PY, str(script)], check=False)
    if result.returncode != 0:
        sys.exit("update: legacy compatibility phase failed")


def repair_git_worktrees() -> tuple[Path, ...]:
    """Repair every shell worktree on every update, healthy or relocated.

    Linked-worktree metadata contains absolute paths in both the main repo and
    each worktree's ``.git`` file. All super-coder shell worktrees live below
    ``.sc-worktrees`` and move with the fork, so their current directories are
    the authoritative repair targets even when Git still lists the old paths.
    The unconditional call is intentional: update is the fleet-wide healing
    seam and ``git worktree repair`` is idempotent on healthy links.
    """
    root = REPO_ROOT / ".sc-worktrees"
    if root.is_dir():
        candidates = tuple(
            sorted(
                (
                    path
                    for path in root.iterdir()
                    if path.is_dir() and (path / ".git").exists()
                ),
                key=lambda path: path.name,
            )
        )
    else:
        candidates = ()
    result = git(
        "worktree",
        "repair",
        *(str(path) for path in candidates),
        check=False,
    )
    if result.returncode != 0:
        sys.exit(
            "update: could not repair linked worktrees:\n"
            f"{result.stderr.strip()}"
        )
    for path in candidates:
        probe = git(
            "rev-parse",
            "--show-toplevel",
            check=False,
            repo_root=path,
        )
        if probe.returncode != 0:
            sys.exit(
                f"update: shell worktree remains unusable after repair: {path}\n"
                f"{probe.stderr.strip()}"
            )
    print(f"→ repair git worktrees ({len(candidates)} shell worktree(s) checked)")
    return candidates


def refresh_installed_brokers() -> tuple[str, ...]:
    """Rewrite and restart broker units that already belong to this fork.

    The unit files embed absolute engine paths. A whole-repo move therefore
    leaves an enabled service executing the old checkout even after the engine
    update itself succeeds. Never opt a fork into a new broker here: only an
    already-present unit is refreshed, through the same public install command
    the operator originally used.
    """
    if os.environ.get("SC_SANDBOX") or shutil.which("systemctl") is None:
        return ()
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    )
    unit_dir = config_home / "systemd" / "user"
    refreshed: list[str] = []
    repo_name = REPO_ROOT.name
    for kind in ("vm", "ts", "pm2", "db"):
        unit = unit_dir / f"sc-{kind}-broker-{repo_name}.service"
        if not unit.is_file():
            continue
        command = f"{kind}-broker-install"
        print(f"→ refresh installed {kind}-broker service (repo path may have moved)")
        result = subprocess.run([str(REPO_ROOT / "sc"), command], cwd=REPO_ROOT)
        if result.returncode != 0:
            print(
                f"  WARNING: {command} failed; unit remains at {unit}. "
                f"Re-run `./sc {command}` after fixing systemd access."
            )
            continue
        refreshed.append(kind)
    return tuple(refreshed)


def is_source_repo() -> bool:
    """The SOURCE repo (origin basename in install.SOURCE_REPO_NAMES — both
    names valid across the super-coder → subfloor rename) tracks the engine as
    its canonical source — it must NEVER untrack or materialize over it. A
    fork's origin is its own repo (the engine upstream is a separate remote).
    A miss here is destructive: the fork branch below git-rm-caches the
    engine — exactly what hit the dogfood repo the day origin was renamed."""
    url = git("remote", "get-url", "origin", check=False).stdout.strip()
    return bool(url) and (url.rstrip("/").split("/")[-1].removesuffix(".git")
                          in install_mod.SOURCE_REPO_NAMES)


def _workflow_version(text: str) -> int | None:
    first_line = text.splitlines()[0] if text else ""
    match = _VISUAL_QA_MARKER_RE.fullmatch(first_line)
    return int(match.group("version")) if match else None


def ensure_workflows(
    repo_root: Path = REPO_ROOT,
    template_root: Path | None = None,
    *,
    source_repo: bool | None = None,
) -> tuple[str, list[Path]]:
    """Reconcile the fork-tracked Visual QA shim and example config.

    Returns ``(action, changed_paths)`` where action is one of ``source``,
    ``seeded``, ``updated``, ``unmanaged``, or ``current``.
    """
    source = source_repo if source_repo is not None else is_source_repo()
    if source:
        return "source", []

    templates = template_root or ENGINE / "templates" / "fork"
    workflow_template = templates / "subfloor-visual-qa.yml"
    current = workflow_template.read_text()
    current_version = _workflow_version(current)
    if current_version is None:
        raise ValueError(f"Visual QA workflow template has no managed marker: {workflow_template}")

    changed: list[Path] = []
    workflow_relative = install_mod.VISUAL_QA_TEMPLATE_TARGETS[
        "subfloor-visual-qa.yml"
    ]
    workflow = repo_root / workflow_relative
    if not workflow.exists():
        workflow.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workflow_template, workflow)
        changed.append(workflow_relative)
        action = "seeded"
    else:
        installed_version = _workflow_version(workflow.read_text())
        if installed_version is None:
            action = "unmanaged"
        elif installed_version < current_version:
            shutil.copy2(workflow_template, workflow)
            changed.append(workflow_relative)
            action = "updated"
        else:
            action = "current"

    example_relative = install_mod.VISUAL_QA_TEMPLATE_TARGETS[
        "visual-qa.example.json"
    ]
    example = repo_root / example_relative
    if not example.exists():
        example.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(templates / "visual-qa.example.json", example)
        changed.append(example_relative)

    return action, changed


def super_coder_remote() -> str:
    """The remote pointing at the engine upstream. Prefer a URL match (either
    name across the super-coder → subfloor rename), else a remote literally
    named after it."""
    named = None
    for line in git("remote", "-v", check=False).stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        repo_name = (
            url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            .removesuffix(".git")
        )
        if repo_name in install_mod.SOURCE_REPO_NAMES:
            return name
        if name in install_mod.SOURCE_REPO_NAMES:
            named = name
    if named:
        return named
    sys.exit("update: no engine upstream remote found. Add it:\n"
             "  git remote add super-coder https://github.com/jedbjorn/subfloor.git")


def _literal_engine_paths_at(
    ref: str,
    source_path: str,
    *,
    repo_root: Path,
) -> tuple[list[str] | None, str | None]:
    """Read a literal ``ENGINE_PATHS = list[str]`` assignment from ``ref``."""
    shown = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{source_path}"],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return None, f"{source_path} is unavailable"

    try:
        tree = ast.parse(shown.stdout, filename=f"{ref}:{source_path}")
    except SyntaxError:
        return None, f"{source_path} is not valid Python"

    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ENGINE_PATHS"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ENGINE_PATHS"
        ):
            value = node.value
        if value is None:
            continue
        try:
            paths = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            return None, f"{source_path} does not assign a literal list[str]"
        if not isinstance(paths, list) or not all(
            isinstance(path, str) for path in paths
        ):
            return None, f"{source_path} does not assign a literal list[str]"
        return paths, None

    return None, f"{source_path} does not define ENGINE_PATHS"


def _engine_path_exists_at(ref: str, path: str, *, repo_root: Path) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}:{path}"],
        capture_output=True,
    ).returncode == 0


def _engine_paths_for(
    ref: str,
    repo_root: Path = REPO_ROOT,
    *,
    fallback_used: list[bool] | None = None,
) -> list[str]:
    """Resolve the target ref's allow-list, then keep paths present at ``ref``.

    New engines declare their list in ``engine_manifest.py``. Refs before that
    module split declare it in ``update.py``. If neither source satisfies the
    literal-list contract, use the installed list as a warned fallback.
    ``fallback_used`` receives that provenance when a caller must keep two
    resolutions under the same authority.

    `git archive` aborts wholesale if any pathspec matches nothing, so a single
    engine file retired upstream (e.g. a dropped schema variant) would otherwise
    break every fork's update the one time it crosses that deletion. The target
    list is authoritative; the installed export remains the fallback and the
    comparison baseline for reporting additions and retirements."""
    sources = (
        ".super-coder/scripts/engine_manifest.py",
        ".super-coder/scripts/update.py",
    )
    reasons: list[str] = []
    resolved: list[str] | None = None
    resolution_source = ""
    for source_path in sources:
        resolved, reason = _literal_engine_paths_at(
            ref, source_path, repo_root=repo_root
        )
        if resolved is not None:
            resolution_source = f"{source_path} at target ref"
            break
        if reason is not None:
            reasons.append(reason)

    used_installed_fallback = resolved is None
    if used_installed_fallback:
        print(
            f"WARNING: update could not resolve ENGINE_PATHS at {ref}: "
            f"{'; '.join(reasons)}; falling back to installed ENGINE_PATHS.",
            file=sys.stderr,
        )
        resolved = list(ENGINE_PATHS)
        resolution_source = "installed ENGINE_PATHS fallback"

    present, missing = [], []
    for path in resolved:
        exists = _engine_path_exists_at(ref, path, repo_root=repo_root)
        (present if exists else missing).append(path)

    added = [path for path in present if path not in ENGINE_PATHS]
    retired = [path for path in ENGINE_PATHS if path not in present]
    if added:
        print(
            f"  note: {len(added)} engine path(s) newly materialized at "
            f"{ref[:12]}: {', '.join(added)}"
        )
    if retired:
        print(
            f"  note: {len(retired)} installed engine path(s) retired at "
            f"{ref[:12]} — skipping: {', '.join(retired)}"
        )
    target_only_missing = [path for path in missing if path not in ENGINE_PATHS]
    if target_only_missing:
        print(
            f"  note: {len(target_only_missing)} target engine path(s) absent "
            f"at {ref[:12]} — skipping: {', '.join(target_only_missing)}"
        )
    if not present:
        sys.exit(f"update: no engine paths exist at {ref} — wrong ref or remote?")
    print(
        f"  resolved {len(present)} engine path(s) for {ref[:12]} from "
        f"{resolution_source}"
    )
    if fallback_used is not None:
        fallback_used.append(used_installed_fallback)
    return present


def _engine_files_at(
    ref: str,
    repo_root: Path | None = None,
    *,
    engine_paths: list[str] | None = None,
) -> list[str]:
    """The exact FILE list upstream ships under the paths resolved for ``ref``.

    This is what a materialize writes, so it is what the manifest must cover
    and nothing more. Locally-added files under engine dirs (e.g. a fork-local
    skill's SKILL.md) and upstream-retired stragglers on disk stay out of the
    manifest: they are not upstream-owned, so they must never guard — and later
    block — a future update (see engine_manifest.write_manifest)."""
    if repo_root is None:
        repo_root = REPO_ROOT
    paths = (
        engine_paths
        if engine_paths is not None
        else _engine_paths_for(ref, repo_root=repo_root)
    )
    return git(
        "ls-tree",
        "-r",
        "--name-only",
        ref,
        "--",
        *paths,
        repo_root=repo_root,
    ).stdout.splitlines()


_MATERIALIZE_FAILURE_REMEDY = (
    "\n  engine.ref was not advanced; the recorded engine pin remains "
    "unchanged.\n"
    "  no automated recovery is available; report the target engine ref "
    "upstream"
)


def _materialized_engine_paths(repo_root: Path = REPO_ROOT) -> list[str]:
    """Load ENGINE_PATHS from the engine manifest that is currently on disk."""
    manifest_path = repo_root / ".super-coder" / "scripts" / "engine_manifest.py"
    spec = importlib.util.spec_from_file_location(
        "_sc_materialized_engine_manifest",
        manifest_path,
    )
    if spec is None or spec.loader is None:
        sys.exit(
            "update: could not load the materialized engine manifest at "
            f"{manifest_path}"
            + _MATERIALIZE_FAILURE_REMEDY
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.exit(
            "update: reloading the materialized engine manifest failed: "
            f"{exc}"
            + _MATERIALIZE_FAILURE_REMEDY
        )
    paths = getattr(module, "ENGINE_PATHS", None)
    if not isinstance(paths, list) or not all(
        isinstance(path, str) for path in paths
    ):
        sys.exit(
            "update: materialized engine manifest does not expose "
            "ENGINE_PATHS as list[str]"
            + _MATERIALIZE_FAILURE_REMEDY
        )
    return list(paths)


def _assert_materialized_engine_paths(
    engine_paths: list[str],
    repo_root: Path = REPO_ROOT,
) -> None:
    """Refuse to record a successful update over a short engine tree."""
    missing = [path for path in engine_paths if not (repo_root / path).exists()]
    if not missing:
        return
    sys.exit(
        "update: materialized engine is incomplete; missing declared path(s): "
        f"{', '.join(missing)}\n"
        + _MATERIALIZE_FAILURE_REMEDY.lstrip("\n")
    )


def materialize_engine(
    ref: str,
    *,
    engine_paths: list[str] | None = None,
) -> None:
    """Write the engine paths at `ref` into the working tree WITHOUT touching the
    git index — the engine is gitignored, so a `git checkout -- <paths>` (which
    stages) is wrong. `git archive | tar -x` copies the fetched tree over the
    top, leaving the gitignored per-instance files (shell_db.db*, instance.json)
    in place. (Files deleted upstream linger until a future doctor sweep — same
    gap the old checkout had; acceptable for a wholesale-overwrite dependency.)"""
    paths = (
        engine_paths
        if engine_paths is not None
        else _engine_paths_for(ref, repo_root=REPO_ROOT)
    )
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "archive",
            ref,
            "--",
            *paths,
        ],
        capture_output=True,
    )
    if archive.returncode != 0:
        sys.exit("update: git archive of the engine failed:\n"
                 + archive.stderr.decode(errors="replace").strip())
    extract = subprocess.run(["tar", "-x", "-C", str(REPO_ROOT)], input=archive.stdout)
    if extract.returncode != 0:
        sys.exit("update: extracting the engine archive failed.")


def _path_matches_ref(rel: str, ref: str) -> bool:
    """Whether one materialized path already has the exact target bytes."""
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{ref}:{rel}"],
        capture_output=True,
        check=False,
    )
    path = REPO_ROOT / rel
    if shown.returncode != 0:
        return not path.exists()
    return path.is_file() and path.read_bytes() == shown.stdout


def check_local_edits(force: bool, *, target_ref: str | None = None) -> None:
    """Block the materialize when engine files were edited locally since the
    last one — a wholesale overwrite would discard those edits silently. The
    operator's real options are stated; --force is the explicit discard."""
    edits = engine_manifest.local_edits()
    if target_ref is not None and edits:
        already_target = {
            rel for rel in edits if _path_matches_ref(rel, target_ref)
        }
        if already_target:
            edits = {
                rel: kind for rel, kind in edits.items() if rel not in already_target
            }
            print(
                "→ update retry: "
                f"{len(already_target)} manifest mismatch(es) already match "
                f"target {target_ref[:12]}"
            )
    if edits:
        # Two owners, one file: tracked engine files (`sc`) are re-installed by
        # every branch checkout, so a manifest mismatch can be an OLDER
        # COMMITTED engine copy rather than a fork patch (#581). A copy
        # byte-equal to HEAD's or the pinned ref's committed version is
        # engine-owned either way — safe to overwrite, wrong to block on.
        refs = ["HEAD"]
        pin = callable_floor.read_engine_ref(REPO_ROOT)
        if pin:
            refs.append(pin)
        committed = {
            rel
            for rel, kind in edits.items()
            if kind == "modified"
            and any(_path_matches_ref(rel, ref) for ref in refs)
        }
        if committed:
            edits = {
                rel: kind for rel, kind in edits.items() if rel not in committed
            }
            print(
                f"→ {len(committed)} manifest mismatch(es) match a committed "
                "engine copy (HEAD or the pinned ref) — a stale checkout, "
                "not a fork edit"
            )
    if not edits:
        return
    print(f"✗ {len(edits)} engine file(s) locally modified since the last materialize:")
    for rel, kind in sorted(edits.items()):
        print(f"    {kind:8} {rel}")
    if force:
        print("  --force: discarding the local edits (overwritten by the new engine).")
        return
    sys.exit(
        "update: refusing to overwrite local engine edits. Your options:\n"
        "  - revert them (the engine is upstream-owned; see README →\n"
        "    'Customize a fork vs diverge from it')\n"
        "  - upstream them: PR the change to super-coder, then update normally\n"
        "  - ./sc update --force   discard the local edits and take upstream's engine\n"
        "  - ./sc eject            one-way: stop tracking upstream and own the engine")


def sync_repo_checkout() -> None:
    """Fast-forward the current repo checkout before reconciling the engine.

    Source installs lay the floor FROM THE WORKING TREE, while installed forks
    may need app changes that accompany a newer engine. In either case, updating
    from a checkout behind its own upstream can reconcile an incoherent floor
    and report success.

    Fast-forward ONLY. This never merges, rebases, resets, or changes branch.
    Git itself protects overlapping uncommitted work; non-overlapping local work
    is not a reason to skip the fast-forward.

    ADVISORY, NEVER BLOCKING (FnB ruling 2026-07-27). Anything that cannot be
    fast-forwarded warns loudly, names the remedy, and lets the update proceed
    from the current tree. An operator unable to update is a worse failure than
    an operator updating one commit behind, and the warning is what the silent
    version lacked. `--no-fetch` skips this step outright.
    """
    branch = git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    if not branch or branch == "HEAD":
        print("→ engine sync: detached HEAD — skipped (no branch to fast-forward)")
        return
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                   check=False)
    tracking = upstream.stdout.strip()
    if upstream.returncode != 0 or not tracking:
        print(f"→ engine sync: '{branch}' tracks no upstream — skipped")
        return
    before = git("rev-parse", "HEAD", check=False).stdout.strip()
    pull = git("pull", "--ff-only", check=False)
    if pull.returncode != 0:
        print(f"! engine sync: `git pull --ff-only` failed for "
              f"{branch} → {tracking}.")
        print(f"  {pull.stderr.strip().splitlines()[-1] if pull.stderr.strip() else ''}")
        print(f"  Updating anyway from the CURRENT tree — update never merges, "
              f"rebases or resets. Reconcile {REPO_ROOT} by hand for the newer floor.")
        return
    after = git("rev-parse", "HEAD", check=False).stdout.strip()
    if after == before:
        print(f"→ engine sync: {branch} already current with {tracking}")
        return
    advanced = git("rev-list", "--count", f"{before}..{after}",
                   check=False).stdout.strip()
    print(f"→ engine sync: fast-forwarded {branch} {before[:7]} → {after[:7]} "
          f"({advanced or '?'} commit(s) from {tracking})")


def fetch_update_ref(branch: str, ref: str | None = None) -> str:
    """Refresh remote objects and resolve the engine ref without touching the
    installed engine floor."""
    remote = super_coder_remote()
    if ref:
        # Pin to an explicit upstream version. `git fetch <remote> <ref>` serves
        # a branch, a tag, or (on GitHub) a reachable commit SHA; FETCH_HEAD is
        # the one name that works for all three.
        print(f"→ fetch engine objects (pinned ref: {ref})")
        git("fetch", remote, ref)
        sha = git("rev-parse", "FETCH_HEAD").stdout.strip()
    else:
        print(f"→ fetch engine objects ({remote}/{branch})")
        git("fetch", remote, branch)
        sha = git("rev-parse", f"{remote}/{branch}").stdout.strip()
    return sha


def publish_engine_ref(sha: str) -> None:
    """Atomically record a fully successful update as the current engine pin."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pending = STATE_DIR / "engine.ref.pending"
    pending.write_text(sha + "\n")
    os.replace(pending, ENGINE_REF)
    print(f"  engine pinned at {sha[:12]} (.sc-state/engine.ref)")


def materialize_fetched_engine(
    sha: str,
    *,
    force: bool = False,
    publish_ref: bool = True,
) -> None:
    """Lay an already-fetched ref onto the installed floor."""

    check_local_edits(force, target_ref=sha)

    # Restore point (engine half): remember where we were before overwriting.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ENGINE_REF.exists():
        shutil.copy2(ENGINE_REF, ENGINE_REF_PREV)
    else:
        # First update after B7 (or a fresh fork): no prior pin. Record HEAD's
        # engine ref if discoverable; else leave prev absent (rollback will warn).
        ENGINE_REF_PREV.unlink(missing_ok=True)

    resolved_paths = _engine_paths_for(sha, repo_root=REPO_ROOT)
    materialize_engine(sha, engine_paths=resolved_paths)
    materialized_paths = _materialized_engine_paths(REPO_ROOT)
    delta = [path for path in materialized_paths if path not in resolved_paths]
    materializable_delta = [
        path
        for path in delta
        if _engine_path_exists_at(sha, path, repo_root=REPO_ROOT)
    ]
    if materializable_delta:
        print(
            "WARNING: materialized engine manifest declares "
            f"{len(materializable_delta)} path(s) missed by target-ref "
            "resolution; materializing the delta: "
            f"{', '.join(materializable_delta)}",
            file=sys.stderr,
        )
        materialize_engine(sha, engine_paths=materializable_delta)
    _assert_materialized_engine_paths(
        resolved_paths + materializable_delta,
        REPO_ROOT,
    )
    callable_floor.require_callable_floor(
        REPO_ROOT,
        expected_ref=sha,
        context="update",
    )
    n = engine_manifest.write_manifest(
        materialized_paths,
        files=_engine_files_at(
            sha,
            repo_root=REPO_ROOT,
            engine_paths=materialized_paths,
        ),
    )
    if publish_ref:
        publish_engine_ref(sha)
        print(f"  manifest covers {n} files")
    else:
        print(
            f"  engine materialized at {sha[:12]} · manifest over {n} files · "
            "pin pending successful update"
        )


def _linked_worktree_paths() -> tuple[Path, ...]:
    listed = git("worktree", "list", "--porcelain", check=False)
    if listed.returncode != 0:
        print(
            "  WARNING: could not list linked worktrees for dispatcher repair: "
            + listed.stderr.strip(),
            file=sys.stderr,
        )
        return ()
    root = REPO_ROOT.resolve()
    paths = []
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.removeprefix("worktree ")).resolve()
        if path != root:
            paths.append(path)
    return tuple(paths)


def reconcile_linked_dispatchers(
    sha: str | None,
    *,
    worktrees: tuple[Path, ...] | None = None,
    target_bytes: bytes | None = None,
) -> tuple[Path, ...]:
    """Lay the current dispatcher into clean linked-worktree launcher copies.

    A shell branch keeps the tracked ``sc`` from its branch point. Updating the
    live engine therefore leaves direct ``./sc`` calls on retired behavior even
    though PATH resolves the main checkout correctly. Only a dispatcher whose
    bytes still match that worktree's own ``HEAD:sc`` is engine-managed here;
    local edits are preserved and named rather than overwritten.

    ``target_bytes`` supplies the current dispatcher directly for source repos,
    where there is no fetched pin to show it from — the working tree's tracked
    ``sc`` IS the current dispatcher there.
    """
    if target_bytes is None:
        shown = git("show", f"{sha}:sc", check=False)
        if shown.returncode != 0:
            print(
                f"  WARNING: target {sha[:12]} has no dispatcher to reconcile",
                file=sys.stderr,
            )
            return ()
        target = shown.stdout.encode()
    else:
        target = target_bytes
    canonical = REPO_ROOT / "sc"
    try:
        canonical_mode = canonical.stat().st_mode
    except OSError as exc:
        print(f"  WARNING: cannot stat canonical dispatcher: {exc}", file=sys.stderr)
        return ()

    changed = []
    managed_dispatchers = set()
    managed_refs = [callable_floor.read_engine_ref(REPO_ROOT)]
    try:
        previous = (
            REPO_ROOT / ".sc-state" / "engine.ref.prev"
        ).read_text().strip()
    except OSError:
        previous = None
    if previous and re.fullmatch(r"[0-9a-fA-F]{40}", previous):
        managed_refs.append(previous)
    for managed_ref in managed_refs:
        if managed_ref is None:
            continue
        prior = git("show", f"{managed_ref}:sc", check=False)
        if prior.returncode == 0:
            managed_dispatchers.add(prior.stdout.encode())
    candidates = worktrees if worktrees is not None else _linked_worktree_paths()
    for worktree in candidates:
        dispatcher = worktree / "sc"
        head = git("show", "HEAD:sc", check=False, repo_root=worktree)
        try:
            current = dispatcher.read_bytes()
        except OSError:
            current = None
        managed_versions = {head.stdout.encode()} if head.returncode == 0 else set()
        managed_versions.update(managed_dispatchers)
        if current not in managed_versions:
            print(
                f"  WARNING: dispatcher locally edited, left stale: {dispatcher}",
                file=sys.stderr,
            )
            continue
        if current == target:
            continue
        dispatcher.write_bytes(target)
        os.chmod(dispatcher, canonical_mode)
        changed.append(worktree)

    if changed:
        print(f"→ reconciled current dispatcher into {len(changed)} linked worktree(s)")
    return tuple(changed)


def repair_callable_dispatcher(sha: str) -> bool:
    """Heal a dispatcher missed by the already-running legacy updater."""
    issues = callable_floor.inspect_callable_floor(
        REPO_ROOT,
        expected_ref=sha,
    )
    if not issues:
        return False

    edits = engine_manifest.local_edits()
    unsafe = {
        rel: kind
        for rel, kind in edits.items()
        if rel == "sc" or not _path_matches_ref(rel, sha)
    }
    if unsafe:
        print(
            "→ legacy update bridge: dispatcher repair deferred to the update "
            "local-edit guard"
        )
        return False

    materialize_engine(sha, engine_paths=["sc"])
    callable_floor.require_callable_floor(
        REPO_ROOT,
        expected_ref=sha,
        context="update compatibility repair",
    )
    materialized_paths = _materialized_engine_paths(REPO_ROOT)
    engine_manifest.write_manifest(
        materialized_paths,
        files=_engine_files_at(
            sha,
            repo_root=REPO_ROOT,
            engine_paths=materialized_paths,
        ),
    )
    print(f"→ legacy update bridge: repaired callable dispatcher at {sha[:12]}")
    return True


def fetch_and_materialize(branch: str, ref: str | None = None,
                          force: bool = False) -> None:
    """Compatibility wrapper for callers that already performed their own
    preflight. update.main deliberately calls the two phases separately."""
    sha = fetch_update_ref(branch, ref=ref)
    materialize_fetched_engine(sha, force=force)


def migrate_engine_untrack() -> None:
    """One-time B7 migration for a fork that predates the gitignore model: stop
    tracking `.super-coder/` and ensure .gitignore keeps it out. Idempotent — a
    no-op once done. (Fresh installs are already untracked by install.py.)"""
    tracked = git("ls-files", "--error-unmatch", ".super-coder",
                  check=False).returncode == 0
    if tracked:
        git("rm", "-r", "--cached", "--quiet", ".super-coder", check=False)
        print("→ B7: untracked .super-coder/ (engine is now a gitignored dependency)")
    gi = REPO_ROOT / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    if "/.super-coder/" not in text.splitlines():
        with gi.open("a") as f:
            f.write(("" if text.endswith("\n") or not text else "\n")
                    + "\n# super-coder — engine is a gitignored materialized dependency (B7)\n"
                    + "/.super-coder/\n/.sc-state/engine.ref.prev\n")
        print("→ B7: added /.super-coder/ to .gitignore")


LEGACY_GENERATED_PATHS = (
    ".sc-state/content.sql",
    ".sc-state/map_content.sql",
    ".sc-state/map.config.json",
    ".sc-state/skills_retired.json",
    "roadmap_sc.md",
    "docs_sc",
    "specs_sc",
    "skills_sc",
)


def migrate_generated_artifacts_local() -> None:
    """Preserve legacy tracked state locally, then remove it from Git's index."""
    copied = artifact_policy.prepare_local_state()
    if copied:
        print(f"→ artifacts: localized {len(copied)} legacy state file(s)")
    tracked = [
        path for path in LEGACY_GENERATED_PATHS
        if git("ls-files", "--error-unmatch", "--", path,
               check=False).returncode == 0
    ]
    if not tracked:
        return
    result = git(
        "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", *tracked,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(
            "update: localized legacy artifacts but could not untrack them:\n"
            + result.stderr.strip()
        )
    print(
        f"→ artifacts: untracked {len(tracked)} generated path(s); "
        "local copies remain under .sc-state/local/"
    )


def migrate_or_rebuild(*, backup: bool = True) -> None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        print("→ no live DB (fresh fork) — building from text")
        rebuild_mod.main([])
        return
    # This restore point pairs with engine.ref.prev.  Keep it distinct from a
    # later verify/rebuild diagnostic backup so rollback cannot combine a
    # current-schema DB with the previous engine.
    if backup:
        rebuild_mod.backup_existing(prefix="preupdate")
    print("→ migrate in place (pending migrations → the live DB; data preserved)")
    # The updater already owns the preupdate restore point above. Bare
    # `./sc migrate` opts into its separate premigrate class at the CLI seam.
    migrate_mod.migrate(str(DB_PATH), backup=False)


def stop_pm2_review_server() -> tuple[str, str] | None:
    """Stop this fork's running legacy PM2 review server, if it has one.

    A stopped process stays registered in PM2, so starting the same name after
    migration loads the newly materialized server code.  An absent PM2 binary
    or an unregistered/stopped process means this install has no PM2 service to
    cut over.
    """
    pm2_bin = shutil.which("pm2")
    if pm2_bin is None:
        return None
    process = f"sc-{ports.resolve(persist=False).get('repo', REPO_ROOT.name)}"
    probe = subprocess.run(
        [pm2_bin, "pid", process], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        sys.exit(
            f"update: could not inspect PM2 process {process}:\n"
            + (probe.stderr or probe.stdout).strip()
        )
    pids = [line.strip() for line in probe.stdout.splitlines()]
    if not any(pid.isdigit() and int(pid) > 0 for pid in pids):
        return None
    stopped = subprocess.run(
        [pm2_bin, "stop", process], capture_output=True, text=True, check=False
    )
    if stopped.returncode != 0:
        sys.exit(
            f"update: could not stop old PM2 server {process}; "
            "refusing to migrate the live DB:\n"
            + (stopped.stderr or stopped.stdout).strip()
        )
    print(f"→ stopped old PM2 server {process} before DB migration")
    return pm2_bin, process


def start_pm2_review_server(service: tuple[str, str] | None) -> None:
    if service is None:
        return
    pm2_bin, process = service
    started = subprocess.run(
        [pm2_bin, "start", process], capture_output=True, text=True, check=False
    )
    if started.returncode != 0:
        sys.exit(
            f"update: DB migration succeeded but new PM2 server {process} "
            "could not start:\n" + (started.stderr or started.stdout).strip()
        )
    print(f"→ started new PM2 server {process} after DB migration")


def migrate_with_service_cutover(*, backup: bool = True) -> None:
    service = stop_pm2_review_server()
    if backup:
        migrate_or_rebuild()
    else:
        migrate_or_rebuild(backup=False)
    # Deliberately not in finally: a failed destructive migration must leave
    # the old server stopped instead of restarting code against a changed or
    # incompatible floor.
    start_pm2_review_server(service)


def sync_skills() -> None:
    """Re-apply the engine skills seed against the live DB.

    The seed is id-stable and UPSERTs by name, so new/changed engine catalogue
    skills land without a rebuild and existing skill_ids — and the grants that
    reference them — stay valid. It deliberately does not retire names absent
    from assets/skills because those may be project-local skills serialized by
    the fork snapshot. The migrate ledger would otherwise skip the already-
    stamped seed file; catalogue currency is a per-update sync, not a one-time
    migration.
    """
    seed = seed_skills.OUT
    if not seed.exists():
        print("  (no skills seed to sync)")
        return
    seed_skills.validate_upstream_skill_namespace(
        seed_skills.seeded_skill_names())
    con = db_driver.connect(DB_PATH)
    try:
        con.executescript(seed.read_text())
        con.commit()
        reconciled = seed_skills.reconcile_tombstoned_skills(con)
        # The seed just reset every engine row to is_deleted=0 — re-assert the
        # fork retire list (.sc-state/skills_retired.json) before regrant()
        # hands the common catalogue back to every flavor/Bespoke pack.
        flipped = seed_skills.apply_retired(con)
        pack_changes = seed_skills.reconcile_standard_flavor_packs(con)
    finally:
        con.close()
    print(f"  synced catalogue from {seed.name}")
    if reconciled.changed_names:
        print(
            "  removed tombstoned skills: "
            f"{', '.join(reconciled.changed_names)} "
            f"({reconciled.grant_count} grant(s))"
        )
    if flipped:
        print(f"  fork retire list re-applied: {', '.join(flipped)}")
    if pack_changes:
        print(f"  standard flavor grants reconciled: {pack_changes}")


def regrant() -> int:
    con = db_driver.connect(DB_PATH)
    try:
        # Newly-added COMMON skills join every standard flavor pack and every
        # Bespoke shell. Opt-ins stay exactly where the operator assigned them.
        template_flavors = {
            p.stem for p in (ENGINE / "templates" / "shells").glob("*.json")
        }
        live_flavors = {
            r[0] for r in con.execute(
                "SELECT DISTINCT flavor FROM shells WHERE flavor IS NOT NULL")
        }
        added = 0
        for flavor in sorted(template_flavors | live_flavors):
            if flavor in template_flavors:
                added += shell_factory.reconcile_flavor_pack(con, flavor)
            else:
                cur = con.execute(
                    "INSERT OR IGNORE INTO flavor_skills (flavor, skill_id) "
                    "SELECT ?, skill_id FROM skills "
                    "WHERE is_deleted=0 AND common=1", (flavor,))
                added += cur.rowcount
        cur = con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
            "SELECT s.shell_id, k.skill_id FROM shells s, skills k "
            "WHERE COALESCE(s.is_deleted,0)=0 AND s.flavor IS NULL "
            "AND k.is_deleted=0 AND k.common=1")
        added += cur.rowcount
        con.commit()
        return added
    finally:
        con.close()


def reconcile_skill_projections() -> dict:
    """Sweep existing checkouts and the active catalogue render after DB sync."""
    con = db_driver.connect(DB_PATH)
    try:
        try:
            summary = skill_projection.reconcile_existing_checkouts(con)
        except skill_projection.ProjectionError as exc:
            sys.exit(skill_projection.partial_failure_message(
                "update catalogue reconciliation", exc
            ))
        catalogue = flat.render_visibility(con)
    finally:
        con.close()
    summary["written"].extend(catalogue["written"])
    summary["skipped"].extend(catalogue["skipped"])
    return summary


def expire_sandbox_harnesses() -> str | None:
    """Expire the harness CLIs baked into the sandbox image; return the epoch.

    `ensure_harnesses()` installs into THIS machine's $HOME, which is the whole
    runtime on the no-docker path and none of it on the docker path: the sandbox
    mounts harness state homes, but its launchers must resolve image-owned
    binaries. Those binaries sit behind a docker layer cache with no expiry of
    its own.
    An update therefore used to lay a new engine floor on top of harness CLIs
    frozen at whenever the image was first built, with no command able to move
    them (a claude one release short of Opus 5 survived exactly that).

    Rolling the epoch expires those layers for the next build. Nothing downloads
    here: the rebuild rides the relaunch an update already requires, so the
    sandbox comes back current in the bounce the new floor needs anyway.

    Returns None when docker is absent — the roll is harmless (there is no image
    to expire) but announcing it would describe a runtime this host does not have.
    """
    epoch = install_mod.roll_harness_epoch()
    if install_mod.docker_status().get("state") == "absent":
        return None
    return epoch


def main(argv: list[str]) -> int:
    run_update_compat()
    no_fetch = "--no-fetch" in argv
    force = "--force" in argv
    branch = "main"
    if "--branch" in argv:
        i = argv.index("--branch")
        if i + 1 < len(argv):
            branch = argv[i + 1]
    ref = None
    if "--ref" in argv:
        i = argv.index("--ref")
        if i + 1 < len(argv):
            ref = argv[i + 1]
        if "--branch" in argv:
            sys.exit("update: --ref and --branch are mutually exclusive — a ref "
                     "IS the pin; a branch is what to track.")

    source = is_source_repo()
    if EJECTED_MARKER.exists() and not source:
        sys.exit("update: this fork has EJECTED — the engine is fork source now, "
                 "not an upstream dependency (.sc-state/ejected). There is no "
                 "upstream to update from; edit .super-coder/ directly and commit "
                 "like any other code. (To re-adopt upstream, that's a manual "
                 "re-fork — see README → 'Customize a fork vs diverge from it'.)")
    if not source:
        try:
            install_mod.validate_gitignore(REPO_ROOT)
        except install_mod.GitignoreError as exc:
            sys.exit(f"update: {exc}")

    # Reconcile every shell worktree before any pull or engine materialization.
    # A whole fork move preserves the directories but invalidates Git's absolute
    # links; update is the one command that must heal the entire set every time.
    worktrees = repair_git_worktrees()

    # Keep the app/source checkout current before reconciling its engine. This
    # is deliberately advisory: dirty, detached, offline, or diverged checkouts
    # warn and continue from their current tree. The one allowed mutation is an
    # explicit fast-forward; update never creates a merge, rebase, or reset.
    if not no_fetch:
        sync_repo_checkout()

    target_sha = None
    if not source and not no_fetch:
        target_sha = fetch_update_ref(branch, ref=ref)
    if source:
        # The source repo IS the engine — it has no upstream to materialize from
        # and must keep tracking .super-coder/. Reconcile its own tree only.
        print("→ super-coder SOURCE repo — engine is tracked here; "
              "skipping fetch/materialize/untrack (reconcile in place only)")
        # --no-fetch keeps its meaning (touch no network) and is the escape
        # hatch for reconciling a tree deliberately.
        no_fetch = True
    else:
        migrate_engine_untrack()  # one-time B7: untrack the engine (idempotent)
        # Replace the one owned range from the current canonical renderer so an
        # already-installed fork never silently commits a new derived cache.
        try:
            changed_gitignore = install_mod.ensure_gitignore()
        except install_mod.GitignoreError as exc:
            sys.exit(f"update: {exc}")
        if changed_gitignore:
            print("→ .gitignore: installed canonical subfloor ignore block")
        migrate_generated_artifacts_local()

    if no_fetch:
        print("→ --no-fetch: reconciling against the current working tree "
              "(engine + engine.ref unchanged)")
    else:
        assert target_sha is not None
        materialize_fetched_engine(
            target_sha, force=force, publish_ref=False
        )

    workflow_action, workflow_changes = ensure_workflows(source_repo=source)
    if workflow_action == "seeded":
        print("→ visual QA: seeded the managed workflow shim")
    elif workflow_action == "updated":
        print("→ visual QA: refreshed the managed workflow shim")
    elif workflow_action == "unmanaged":
        print("→ visual QA: workflow has no managed marker — leaving fork-owned file unchanged")
    if workflow_changes:
        paths = " ".join(str(path) for path in workflow_changes)
        print(f"  commit these fork-owned files: git add {paths}")

    # Harnesses can be ADDED upstream between releases (e.g. codex landed after
    # dos-arch installed), so a fork that updates must pick up any newly-required
    # harness — not just the ones present at first install. Best-effort + native
    # installers (no npm); a failure warns and continues (install by hand later).
    # Auth/login stays manual; this only ensures the CLI binary is present.
    print("→ ensure harnesses installed (claude + opencode + codex + vibe + kimi)")
    install_mod.ensure_harnesses()

    # …and that covers the HOST only — see expire_sandbox_harnesses() for the
    # half of the fleet ensure_harnesses() cannot reach.
    epoch = expire_sandbox_harnesses()
    if epoch:
        print(f"→ expire the sandbox's baked harness CLIs (epoch {epoch})")
        print("  they reinstall on the next image build — normal `./sc restart` / `make dos-r`")

    migrate_with_service_cutover()

    # Broker systemd units contain absolute ExecStart paths. Refresh only the
    # services this fork had already installed so a moved repo does not keep
    # running the pre-move engine after an otherwise successful update.
    refresh_installed_brokers()

    print("→ sync skills catalogue (id-stable)")
    sync_skills()
    print("→ re-grant catalogue skills to all shells")
    grant_changes = regrant()
    print(f"  {grant_changes} grant change(s)")
    print("→ reconcile managed skill projections")
    projections = reconcile_skill_projections()
    print(
        f"  {len(projections['written'])} changed, "
        f"{len(projections['skipped'])} unchanged across "
        f"{len(projections['checkouts'])} existing checkout(s)"
    )
    print(
        "  note: DB and disk are current; already-running harness sessions may "
        "retain previously loaded skill text until reboot"
    )
    print("→ wire map automation + map the repo")
    run_script("map_setup.py", update_target_ref=target_sha)
    print("→ snapshot the live state")
    run_script("snapshot.py")

    # Self-heal the make wiring: forks installed before the engine scripted this
    # (or whose include was removed) get the `dos-*` aliases appended now. Source
    # repo manages its own Makefile — skip it. Idempotent; a no-op if already wired.
    if not source:
        print("→ wire make aliases (dos- command standard)")
        print(f"  {install_mod.wire_make_aliases()}")

    if target_sha is not None:
        publish_engine_ref(target_sha)
        reconcile_linked_dispatchers(target_sha, worktrees=worktrees)
    elif source:
        # A source repo tracks the engine in its working tree — the canonical
        # `sc` IS the current dispatcher, and there is no fetched pin to show
        # it from. Skipping reconciliation here left source-repo shell
        # worktrees on stale launchers forever (flag #166: skills documented
        # `sc sprint` while worktree dispatchers predated the verb).
        canonical = REPO_ROOT / "sc"
        if canonical.is_file():
            reconcile_linked_dispatchers(
                None,
                worktrees=worktrees,
                target_bytes=canonical.read_bytes(),
            )
    else:
        dispatcher_ref = callable_floor.read_engine_ref(REPO_ROOT)
        if dispatcher_ref:
            reconcile_linked_dispatchers(dispatcher_ref, worktrees=worktrees)

    print("\nupdate: done — new floor laid in place; your rows are intact.")
    if source:
        # Source repo tracks the engine itself — no fork repin PR; just commit
        # the reconciled tree on a branch as usual.
        print("  Review + commit the reconciled tree (the engine is tracked here).")
    else:
        # Fork repin: the update edited tracked files in place but did NOT touch
        # git — it never branches, commits, or changes branch, so a bare `./sc
        # update` on `main` leaves the repin uncommitted on main. Spell out the
        # full flow so the operator lands a PR and returns to main instead of
        # sitting stranded on the repin branch (the engine is gitignored — only
        # engine.ref + any genuinely authored install files).
        try:
            pin = (REPO_ROOT / ".sc-state" / "engine.ref").read_text().strip()[:12]
        except Exception:
            pin = ""
        branch_hint = f"repin-{pin}" if pin else "repin-<sha>"
        print("  This edited tracked files in place but did NOT touch git. Recommended flow:")
        print(f"    git checkout -b {branch_hint}")
        print("    git add sc .sc-state/engine.ref .gitignore   "
              "# sc when the materialize changed it; + Makefile/workflow "
              "changes when reported")
        print("    git commit -m 'chore(engine): repin' && git push -u origin HEAD")
        print("    gh pr create")
        print("    git checkout main        # return to main — don't stay stranded on the repin branch")
        print("  After the PR merges:")
        print("    git pull --ff-only       # brings the repin onto local main")
    print("  A bad update? `./sc rollback` restores the DB + engine together.")
    print("  Restart your session to boot onto the new floor.")
    if epoch:
        print("  Image-owned tool changes activate through a normal `./sc restart`;")
        print("  `restart --no-build` deliberately retains the selected image.")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
