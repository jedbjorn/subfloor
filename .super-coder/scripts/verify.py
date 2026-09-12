#!/usr/bin/env python3
"""Exercise rebuild and render-only boot in an isolated disposable checkout."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import instance_state

ENGINE = Path(__file__).resolve().parents[1]
ROOT = ENGINE.parent


def _copy_engine(source: str, names: list[str]) -> set[str]:
    # Copy authored engine code, never private instance identity or runtime data.
    return {
        name for name in names
        if name in {"instance.json", "run", "node_modules", "__pycache__",
                    ".sc-state", "cache", "tmp"}
        or name.endswith((".pyc", ".db", ".db-wal", ".db-shm", ".sock"))
    }


def main(argv: list[str]) -> int:
    if argv:
        raise SystemExit("usage: ./sc verify")
    # Snapshot is the only per-instance input; verify must not open the live DB.
    snapshot = instance_state.active_snapshot_path(ROOT)
    if not snapshot.exists():
        snapshot = ROOT / ".sc-state" / "content.sql"
    with tempfile.TemporaryDirectory(prefix="sc-verify-") as directory:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("SC_", "GIT_", "PYTHON", "XDG_",
                                      "CODEX_", "CLAUDE_", "OPENCODE_", "KIMI_"))
               and key not in {"RENDER_ONLY", "HARNESS", "HARNESS_BIN"}}
        env.update({
            "HOME": str(Path(directory) / "home"),
            "XDG_STATE_HOME": str(Path(directory) / "state"),
            "XDG_CACHE_HOME": str(Path(directory) / "cache"),
            "XDG_CONFIG_HOME": str(Path(directory) / "config"),
            "XDG_DATA_HOME": str(Path(directory) / "data"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        candidate = Path(directory) / "checkout"
        candidate.mkdir()
        for parent, dirs, files in os.walk(ENGINE, followlinks=False):
            omitted = _copy_engine(parent, dirs + files)
            for name in dirs + files:
                if name not in omitted and (Path(parent) / name).is_symlink():
                    raise SystemExit(f"verify: refusing linked engine path {Path(parent) / name}")
            dirs[:] = [name for name in dirs if name not in omitted]
        shutil.copytree(ENGINE, candidate / ".super-coder", ignore=_copy_engine)
        shutil.copy2(ROOT / "sc", candidate / "sc")
        if snapshot.exists():
            content = candidate / ".sc-state" / "local" / "content.sql"
            content.parent.mkdir(parents=True)
            shutil.copy2(snapshot, content)
        retired = ROOT / ".sc-state" / "local" / "skills_retired.json"
        if not retired.exists():
            retired = ROOT / ".sc-state" / "skills_retired.json"
        if retired.is_file() and not retired.is_symlink():
            target = candidate / ".sc-state" / "local" / "skills_retired.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(retired, target)
        # Preserve only the boot harness choice. Private instance identity and
        # work-repo paths must never be carried into a disposable checkout.
        config = ENGINE / "instance.json"
        if config.is_file() and not config.is_symlink():
            settings = json.loads(config.read_text())
            harness = settings.get("harness") if isinstance(settings, dict) else None
            if isinstance(harness, str):
                (candidate / ".super-coder" / "instance.json").write_text(
                    json.dumps({"harness": harness}) + "\n"
                )
        # The render-only boot checks source-repository provenance. A fresh,
        # local git identity makes that check exercise the copied candidate.
        origin = subprocess.run(
            ["git", "-C", str(ROOT), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False, env=env,
        ).stdout.strip() or "https://example.invalid/verify-fork.git"
        remote_name = origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if not remote_name.endswith(".git"):
            remote_name += ".git"
        remote_name = Path(remote_name).name
        bare = Path(directory) / remote_name
        subprocess.run(["git", "init", "-q", "-b", "main", str(candidate)],
                       env=env, check=True)
        git_env = {**env, "GIT_AUTHOR_NAME": "Verify", "GIT_AUTHOR_EMAIL": "verify@invalid",
                   "GIT_COMMITTER_NAME": "Verify", "GIT_COMMITTER_EMAIL": "verify@invalid"}
        subprocess.run(["git", "-C", str(candidate), "add", "sc", ".super-coder"],
                       env=git_env, check=True)
        subprocess.run(["git", "-C", str(candidate), "commit", "-qm", "candidate source"],
                       env=git_env, check=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(candidate), str(bare)],
                       env=git_env, check=True)
        subprocess.run(["git", "-C", str(candidate), "remote", "add", "origin", str(bare)],
                       env=git_env, check=True)
        subprocess.run(["git", "-C", str(candidate), "fetch", "-q", "origin", "main"],
                       env=git_env, check=True)
        subprocess.run(["git", "-C", str(candidate), "branch", "--set-upstream-to",
                        "origin/main", "main"], env=git_env, check=False)
        python = sys.executable
        scripts = candidate / ".super-coder" / "scripts"
        resolved = subprocess.run(
            [python, str(scripts / "instance_state.py"), "active-database",
             str(candidate / ".super-coder")], cwd=candidate, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        candidate_db = instance_state.legacy_database_path(candidate / ".super-coder")
        if Path(resolved).resolve() != candidate_db.resolve():
            raise SystemExit(f"verify: candidate database escaped disposable checkout: {resolved}")
        steps = [
            [python, str(scripts / "rebuild.py")],
            [python, str(scripts / "init_fork.py"), "--username", "verify"],
            [python, str(scripts / "render.py"), "flat"],
            [python, str(scripts / "run.py"), "--first"],
        ]
        # init_fork is needed only when the authored snapshot has no shell.
        for index, command in enumerate(steps):
            if index == 1:
                import sqlite3
                db = candidate_db
                with sqlite3.connect(db) as con:
                    populated = con.execute(
                        "SELECT EXISTS(SELECT 1 FROM users WHERE is_active=1) "
                        "AND EXISTS(SELECT 1 FROM shells WHERE COALESCE(is_deleted,0)=0)"
                    ).fetchone()[0]
                if populated:
                    continue
            step_env = dict(env)
            if index == 2:
                step_env["SC_ADMIN"] = "1"
            if index == 3:
                step_env["RENDER_ONLY"] = "1"
                import sqlite3
                db = candidate_db
                with sqlite3.connect(db) as con:
                    selected = con.execute(
                        "SELECT shortname FROM shells WHERE flavor!='admin' "
                        "AND COALESCE(is_deleted,0)=0 ORDER BY shell_id LIMIT 1"
                    ).fetchone()
                if selected:
                    command = [*command, selected[0]]
            result = subprocess.run(command, cwd=candidate, env=step_env, check=False)
            if result.returncode:
                return result.returncode
    print("verify: candidate passed; live engine memory and artifacts were unchanged")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
