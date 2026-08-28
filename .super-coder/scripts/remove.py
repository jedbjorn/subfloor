#!/usr/bin/env python3
"""Safely remove an installed subfloor engine from its host repository.

The removal backup is the one intentional residue:

    .sc-state/db_backups/removal/<timestamp>/

Everything destructive is gated on an exact target, clean shell worktrees, a
quiesced repo runtime, and a verified WAL-safe database backup.  Machine-wide
harnesses, credentials, shared engine-base images, project files, branches, and
unrelated Git configuration are outside this command's ownership boundary.
Install-labeled fork-extension images and dependency volumes are removed only
after the verified backup gate.

Usage:
    ./sc remove [--dry-run] [--yes]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
STATE_DIR = REPO_ROOT / ".sc-state"
DB_PATH = ENGINE / "shell_db.db"
BACKUP_ROOT = STATE_DIR / "db_backups" / "removal"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_backup
import engine_manifest
import install
import sandbox_devkit
import sc_wrapper

BACKUP_IGNORE = "/.sc-state/db_backups/"
BACKUP_IGNORE_COMMENT = "# subfloor removal backup — preserved after make dos-remove"
MANAGED_WORKTREES = ".sc-worktrees"
VISUAL_QA_WORKFLOW = Path(".github/workflows/subfloor-visual-qa.yml")
VISUAL_QA_MARKER = "# managed-by: subfloor — visual-qa shim "

_ACTIVE_MANIFEST: Path | None = None
_ACTIVE_DATA: dict | None = None


class RemoveError(RuntimeError):
    """A safety gate failed; no further teardown may proceed."""


def _run(
    *args: str,
    cwd: Path,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RemoveError(f"{' '.join(args)} failed: {detail or result.returncode}")
    return result


def _inside(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def validate_target(repo_root: Path = REPO_ROOT) -> Path:
    root = repo_root.resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise RemoveError(f"refusing unsafe repository root: {root}")
    if (repo_root / ".git").is_file():
        raise RemoveError(
            "remove must run from the main checkout, not a linked worktree"
        )
    top = _run("git", "rev-parse", "--show-toplevel", cwd=repo_root)
    if top.returncode != 0:
        raise RemoveError("remove requires the installed host to be a Git repository")
    git_root = Path(top.stdout.strip()).resolve()
    if git_root != root:
        raise RemoveError(f"engine root and Git root disagree: {root} != {git_root}")

    origin = _run("git", "remote", "get-url", "origin", cwd=root)
    if origin.returncode == 0:
        name = origin.stdout.strip().rstrip("/").split("/")[-1].removesuffix(".git")
        if name in install.SOURCE_REPO_NAMES:
            raise RemoveError(
                "this is the subfloor source repository; remove is fork-only"
            )
    return root


def _parse_worktrees(text: str) -> list[Path]:
    paths: list[Path] = []
    for line in text.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.removeprefix("worktree ")))
    return paths


def managed_worktrees(repo_root: Path) -> list[Path]:
    result = _run("git", "worktree", "list", "--porcelain", cwd=repo_root, check=True)
    managed = (repo_root / MANAGED_WORKTREES).absolute()
    return [path for path in _parse_worktrees(result.stdout) if _inside(path, managed)]


def dirty_worktrees(worktrees: list[Path]) -> list[Path]:
    dirty: list[Path] = []
    for path in worktrees:
        result = _run("git", "status", "--porcelain", "--untracked-files=all", cwd=path)
        if result.returncode != 0 or result.stdout.strip():
            dirty.append(path)
    return dirty


def remove_worktrees(repo_root: Path, worktrees: list[Path]) -> None:
    newly_dirty = dirty_worktrees(worktrees)
    if newly_dirty:
        joined = "\n  - ".join(str(path) for path in newly_dirty)
        raise RemoveError(f"worktree became dirty; refusing removal:\n  - {joined}")
    for path in worktrees:
        _run("git", "worktree", "remove", str(path), cwd=repo_root, check=True)
        print(f"  removed clean worktree {path}")
    _run("git", "worktree", "prune", cwd=repo_root, check=True)


def engine_drift(repo_root: Path) -> tuple[dict[str, str], list[str]]:
    edits = engine_manifest.local_edits()
    manifest = ENGINE / "engine.manifest"
    known: set[str] = set()
    try:
        known = set(json.loads(manifest.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    added: list[str] = []
    engine_root = repo_root / ".super-coder"
    if engine_root.is_dir() and known:
        for path in engine_root.rglob("*"):
            if not path.is_file() or path.name == "engine.manifest":
                continue
            engine_rel = path.relative_to(engine_root)
            if (
                engine_rel.parts[0] in {"run", "logs"}
                or path.name == "instance.json"
                or path.name.startswith("shell_db.db")
            ):
                continue
            rel = str(path.relative_to(repo_root))
            if rel not in known and "__pycache__" not in path.parts:
                added.append(rel)
    return edits, sorted(added)


def show_plan(
    repo_root: Path,
    worktrees: list[Path],
    edits: dict[str, str],
    added: list[str],
) -> None:
    print("subfloor remove — teardown plan")
    print(f"  repository : {repo_root}")
    print(f"  database   : {DB_PATH if DB_PATH.exists() else '(no live DB)'}")
    print(f"  backups    : {BACKUP_ROOT}/<UTC timestamp>/")
    print(f"  worktrees  : {len(worktrees)} clean managed worktree(s)")
    if edits or added:
        print(f"  engine drift: {len(edits)} changed + {len(added)} added file(s)")
        for path, state in sorted(edits.items()):
            print(f"    {state:8} {path}")
        for path in added:
            print(f"    added    {path}")
    print("  remove     : repo runtime, engine, generated state, shell worktrees,")
    print("               install-owned extension images/volumes and integration")
    print("  preserve   : project files, branches, unrelated Git config, shared")
    print("               machine tools, harness profiles,")
    print("               credentials, engine-base images, and backups")


def confirm(repo_root: Path) -> None:
    phrase = f"REMOVE {repo_root.name}"
    if not sys.stdin.isatty():
        raise RemoveError(
            f"non-interactive stdin requires --yes (confirmation is {phrase!r})"
        )
    try:
        answer = input(f"Type '{phrase}' to continue: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != phrase:
        raise RemoveError("remove aborted; nothing was stopped or deleted")


def ensure_backup_ignore(repo_root: Path) -> None:
    gitignore = repo_root / ".gitignore"
    text = install._read_gitignore(gitignore) if gitignore.exists() else ""
    install.gitignore_without_managed(text)  # validate before first mutation
    lines = text.splitlines()
    if BACKUP_IGNORE in {line.strip() for line in lines}:
        return
    suffix = "" if not text or text.endswith(("\n", "\r")) else "\n"
    install._write_gitignore(
        gitignore,
        text + suffix + f"\n{BACKUP_IGNORE_COMMENT}\n{BACKUP_IGNORE}\n"
    )


def new_backup_dir(repo_root: Path) -> Path:
    root = repo_root / ".sc-state" / "db_backups" / "removal"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = root / stamp
    destination.mkdir(parents=True, mode=0o700)
    os.chmod(destination, 0o700)
    db_backup._probe_writable(destination)
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _db_metadata(path: Path) -> tuple[int, str]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()
    return version, integrity


def write_manifest(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def backup_database(repo_root: Path, destination: Path) -> tuple[Path | None, dict]:
    source = repo_root / ".super-coder" / "shell_db.db"
    engine_ref_path = repo_root / ".sc-state" / "engine.ref"
    engine_ref = ""
    try:
        engine_ref = engine_ref_path.read_text().strip()
    except OSError:
        pass
    data: dict = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(repo_root),
        "engine_ref": engine_ref or None,
        "status": "preparing",
        "removed": [],
        "preserved": [str(destination)],
        "errors": [],
    }
    if not source.exists():
        data["database"] = None
        data["status"] = "backed_up"
        return None, data

    source_version, _ = _db_metadata(source)
    backup = db_backup.backup_database(source, destination, "removal", keep=1_000_000)
    if backup is None:
        raise RemoveError("live database disappeared before backup")
    os.chmod(backup, 0o600)
    backup_version, integrity = _db_metadata(backup)
    if integrity != "ok":
        raise RemoveError(f"backup integrity check failed: {integrity}")
    if backup_version != source_version:
        raise RemoveError(
            f"backup user_version mismatch: {backup_version} != {source_version}"
        )
    data["database"] = {
        "source": str(source),
        "file": backup.name,
        "bytes": backup.stat().st_size,
        "sha256": _sha256(backup),
        "user_version": backup_version,
        "integrity_check": integrity,
    }
    data["status"] = "backed_up"
    return backup, data


def stop_running_jobs(repo_root: Path) -> None:
    jobs = repo_root / ".super-coder" / "run" / "jobs"
    if not jobs.is_dir():
        return
    for jobdir in sorted(path for path in jobs.iterdir() if path.is_dir()):
        try:
            meta = json.loads((jobdir / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("finished_at") is not None:
            continue
        pid = meta.get("pid")
        if not pid:
            continue
        result = _run(str(repo_root / "sc"), "job", "kill", jobdir.name, cwd=repo_root)
        if result.returncode != 0 and "already finished" not in result.stderr:
            raise RemoveError(
                f"could not stop durable job {jobdir.name}: "
                f"{(result.stderr or result.stdout).strip()}"
            )


def _pg_configured(repo_root: Path) -> bool:
    try:
        instance = json.loads(
            (repo_root / ".super-coder" / "instance.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        return False
    return "pg" in instance


def quiesce_runtime(repo_root: Path) -> None:
    stop_running_jobs(repo_root)
    docker = shutil.which("docker")
    result = _run(str(repo_root / "sc"), "down", cwd=repo_root)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        # `sc down` ends with its PostgreSQL absence proof.  On a deliberately
        # no-Docker host that proof cannot run even when this fork never
        # configured a sidecar; all preceding host-broker shutdowns have still
        # completed.  Accept only that narrow case, then prove the engine API
        # listener is absent below.  A configured sidecar remains fail-closed.
        if docker is None and not _pg_configured(repo_root):
            print(
                "→ no Docker CLI and no configured PG sidecar; checking host listener"
            )
        else:
            raise RemoveError(
                "runtime shutdown failed: "
                + ((result.stderr or result.stdout).strip() or str(result.returncode))
            )
    if docker:
        names = _run(
            docker,
            "ps",
            "-a",
            "--format",
            "{{.Names}}",
            cwd=repo_root,
        )
        if names.returncode == 0:
            forbidden = {f"sc-{repo_root.name}", f"sc-pg-{repo_root.name}"}
            remaining = forbidden & set(names.stdout.splitlines())
            if remaining:
                raise RemoveError(
                    "runtime shutdown left container(s): "
                    + ", ".join(sorted(remaining))
                )
    port_result = _run(
        sys.executable,
        str(repo_root / ".super-coder" / "scripts" / "ports.py"),
        "port",
        cwd=repo_root,
    )
    if port_result.returncode == 0 and port_result.stdout.strip().isdigit():
        port = int(port_result.stdout.strip())
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.25)
        except OSError:
            pass
        else:
            connection.close()
            raise RemoveError(
                f"runtime shutdown left a listener on 127.0.0.1:{port}; "
                "stop the foreground server and retry"
            )


def cleanup_makefile(repo_root: Path) -> bool:
    path = repo_root / "Makefile"
    if not path.exists():
        return False
    text = path.read_text()
    if text == install.INSTALLER_MAKEFILE:
        path.unlink()
        return True
    updated = text.replace(install.APPENDED_ALIASES_BLOCK, "")
    updated = install._ALIASES_RE.sub("", updated)
    if updated == text:
        return False
    updated = re.sub(r"\n{3,}", "\n\n", updated)
    path.write_text(updated)
    return True


def cleanup_gitignore(repo_root: Path) -> bool:
    path = repo_root / ".gitignore"
    existing = install._read_gitignore(path) if path.exists() else ""
    try:
        updated = install.gitignore_without_managed(existing)
    except install.GitignoreError as exc:
        raise RemoveError(str(exc)) from exc
    if BACKUP_IGNORE not in {line.strip() for line in updated.splitlines()}:
        separator = "" if not updated or updated.endswith(("\n", "\r")) else "\n"
        updated += separator + f"{BACKUP_IGNORE_COMMENT}\n{BACKUP_IGNORE}\n"
    if updated == existing:
        return False
    install._write_gitignore(path, updated)
    return True


def cleanup_hooks(repo_root: Path) -> bool:
    current = _run("git", "config", "--get", "core.hooksPath", cwd=repo_root)
    if current.returncode != 0:
        return False
    raw = current.stdout.strip()
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        configured = repo_root / configured
    if configured.resolve() != (repo_root / ".super-coder" / "hooks").resolve():
        return False
    _run("git", "config", "--unset", "core.hooksPath", cwd=repo_root, check=True)
    return True


def cleanup_upstream_remotes(repo_root: Path) -> list[str]:
    remotes = _run("git", "remote", cwd=repo_root, check=True).stdout.splitlines()
    removed: list[str] = []
    for remote in remotes:
        if remote == "origin":
            continue
        url = _run("git", "remote", "get-url", remote, cwd=repo_root)
        if url.returncode != 0:
            continue
        name = url.stdout.strip().rstrip("/").split("/")[-1].removesuffix(".git")
        if name not in install.SOURCE_REPO_NAMES:
            continue
        _run("git", "remote", "remove", remote, cwd=repo_root, check=True)
        removed.append(remote)
    return removed


def _remove_path(path: Path, repo_root: Path) -> bool:
    if not _inside(path, repo_root):
        raise RemoveError(f"refusing path outside repository: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return True
    if path.is_dir():
        if not path.resolve().is_relative_to(repo_root.resolve()):
            raise RemoveError(f"refusing directory outside repository: {path}")
        shutil.rmtree(path)
        return True
    return False


def cleanup_visual_qa(repo_root: Path) -> bool:
    path = repo_root / VISUAL_QA_WORKFLOW
    if not path.is_file():
        return False
    try:
        first = path.read_text().splitlines()[0]
    except (OSError, IndexError):
        return False
    if not first.startswith(VISUAL_QA_MARKER):
        return False
    path.unlink()
    return True


def cleanup_shared_scaffolds(repo_root: Path) -> list[str]:
    removed: list[str] = []
    for relative in (Path("shared/redlines"), Path("shared")):
        directory = repo_root / relative
        keep = directory / ".gitkeep"
        if not directory.is_dir():
            continue
        entries = list(directory.iterdir())
        if entries == [keep]:
            keep.unlink()
            directory.rmdir()
            removed.append(str(relative))
    return removed


def cleanup_state(repo_root: Path) -> list[str]:
    state = repo_root / ".sc-state"
    removed: list[str] = []
    if not state.is_dir():
        return removed
    for child in list(state.iterdir()):
        if child.name == "db_backups":
            continue
        if _remove_path(child, repo_root):
            removed.append(str(child.relative_to(repo_root)))
    return removed


def remove_installation(repo_root: Path) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    preserved: list[str] = []
    if cleanup_hooks(repo_root):
        removed.append("git config core.hooksPath")
    removed.extend(f"git remote {name}" for name in cleanup_upstream_remotes(repo_root))
    if cleanup_makefile(repo_root):
        removed.append("Makefile subfloor alias integration")
    if cleanup_gitignore(repo_root):
        removed.append(".gitignore subfloor rules")
    if cleanup_visual_qa(repo_root):
        removed.append(str(VISUAL_QA_WORKFLOW))
    elif (repo_root / VISUAL_QA_WORKFLOW).exists():
        preserved.append(str(VISUAL_QA_WORKFLOW))
    removed.extend(cleanup_shared_scaffolds(repo_root))

    for relative in install.GENERATED_INSTALL_PATHS:
        if _remove_path(repo_root / relative, repo_root):
            removed.append(str(relative))
    removed.extend(cleanup_state(repo_root))

    # Engine and dispatcher are last: every fallible engine-backed operation
    # above must finish while the installed command still exists.
    for relative in (Path(".super-coder"), Path("sc")):
        if _remove_path(repo_root / relative, repo_root):
            removed.append(str(relative))
    return removed, preserved


def cleanup_devkit_resources(repo_root: Path) -> list[str]:
    if shutil.which("docker") is None:
        return []
    try:
        return sandbox_devkit.cleanup_owned_resources(repo_root / ".super-coder")
    except sandbox_devkit.SandboxImageError as exc:
        raise RemoveError(f"could not remove owned dev-kit resources: {exc}") from exc


def verify_removed(repo_root: Path) -> list[str]:
    remaining: list[str] = []
    for relative in (
        Path(".super-coder"),
        Path(".sc-worktrees"),
        Path("sc"),
        *install.GENERATED_INSTALL_PATHS,
    ):
        if (repo_root / relative).exists() or (repo_root / relative).is_symlink():
            remaining.append(str(relative))
    makefile = repo_root / "Makefile"
    if makefile.is_file() and install._ALIASES_RE.search(makefile.read_text()):
        remaining.append("Makefile alias include")
    return remaining


def _signal_handler(signum: int, _frame: object) -> None:
    if _ACTIVE_MANIFEST is not None and _ACTIVE_DATA is not None:
        _ACTIVE_DATA["status"] = "partial"
        _ACTIVE_DATA.setdefault("errors", []).append(f"interrupted by signal {signum}")
        try:
            write_manifest(_ACTIVE_MANIFEST, _ACTIVE_DATA)
        except OSError:
            pass
        print(
            f"\nremove interrupted; backup manifest: {_ACTIVE_MANIFEST}",
            file=sys.stderr,
        )
    raise SystemExit(128 + signum)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./sc remove",
        description="Safely remove subfloor from this host repository.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the removal plan; change nothing"
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the typed confirmation only"
    )
    return parser


def main(argv: list[str]) -> int:
    global _ACTIVE_DATA, _ACTIVE_MANIFEST
    args = build_parser().parse_args(argv)
    manifest_path: Path | None = None
    data: dict | None = None
    try:
        repo_root = validate_target()
        worktrees = managed_worktrees(repo_root)
        dirty = dirty_worktrees(worktrees)
        if dirty:
            joined = "\n  - ".join(str(path) for path in dirty)
            raise RemoveError(
                f"dirty managed worktree(s) must be resolved first:\n  - {joined}"
            )
        edits, added = engine_drift(repo_root)
        try:
            install.validate_gitignore(repo_root)
        except install.GitignoreError as exc:
            raise RemoveError(str(exc)) from exc
        show_plan(repo_root, worktrees, edits, added)
        if args.dry_run:
            print("dry-run: no services stopped and no files changed")
            return 0
        if not args.yes:
            confirm(repo_root)

        ensure_backup_ignore(repo_root)
        destination = new_backup_dir(repo_root)
        print(f"→ backup destination ready: {destination}")
        quiesce_runtime(repo_root)
        remove_worktrees(repo_root, worktrees)

        _backup, data = backup_database(repo_root, destination)
        manifest_path = destination / "manifest.json"
        _ACTIVE_DATA = data
        _ACTIVE_MANIFEST = manifest_path
        write_manifest(manifest_path, data)
        if data["database"] is None:
            print("→ no live DB existed; recorded database: null")
        else:
            print(
                f"→ verified WAL-safe DB backup: {destination / data['database']['file']}"
            )

        data["removed"].extend(cleanup_devkit_resources(repo_root))
        removed, preserved = remove_installation(repo_root)
        data["removed"].extend(removed)
        data["preserved"].extend(preserved)
        remaining = verify_removed(repo_root)
        if remaining:
            data["status"] = "partial"
            data["errors"].append("remaining install surfaces: " + ", ".join(remaining))
            write_manifest(manifest_path, data)
            raise RemoveError(
                "teardown incomplete; remaining surfaces: " + ", ".join(remaining)
            )

        try:
            wrapper_result = sc_wrapper.unregister_install(repo_root)
        except sc_wrapper.WrapperError as exc:
            raise RemoveError(str(exc)) from exc
        print(f"→ managed host sc wrapper: {wrapper_result}")

        data["status"] = "removed"
        write_manifest(manifest_path, data)
        _ACTIVE_DATA = None
        _ACTIVE_MANIFEST = None
        print("→ subfloor removed from this repository")
        print(f"  preserved backup: {destination}")
        print("  review and commit the teardown with: git status")
        return 0
    except (RemoveError, OSError, sqlite3.Error) as exc:
        if manifest_path is not None and data is not None:
            data["status"] = "partial"
            data.setdefault("errors", []).append(str(exc))
            try:
                write_manifest(manifest_path, data)
            except OSError:
                pass
            print(f"  backup manifest: {manifest_path}", file=sys.stderr)
        print(f"remove: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for _sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(_sig, _signal_handler)
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
