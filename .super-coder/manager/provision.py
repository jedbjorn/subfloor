"""Bootstrap one owner-selected checkout from the supported engine remote."""
import os
import subprocess
from pathlib import Path

from inventory import command, install_config

SOURCE = "https://github.com/jedbjorn/subfloor.git"
COMMIT = "chore: install subfloor"
PINS = ("sc", ".sc-state/engine.source", ".sc-state/engine.ref")
# Paths the installer creates, removes or renders. Refuse occupied project
# paths up front, including staged deletions whose worktree path is absent.
RESERVED = ("sc", ".super-coder", ".sc-state", ".sc-worktrees", "CLAUDE.md", "AGENTS.md",
            "opencode.json", ".claude/settings.local.json", ".codex/hooks.json", ".claude/skills",
            ".agents/skills", ".opencode/skills", "roadmap_sc.md", "docs_sc", "specs_sc", "skills_sc")


def git(repo, *args):
    result = command("git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args)
    if not result or result.returncode:
        raise ValueError((result.stderr.strip() if result else "") or f"git {args[0]} failed in {repo}")
    return result.stdout.strip()


def identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def primary(repo):
    if (repo.resolve() != repo or repo.stat().st_uid != os.getuid()
            or not repo.is_dir() or (repo / ".git").is_symlink()
            or git(repo, "rev-parse", "--show-toplevel") != str(repo)):
        raise ValueError("Select an owner-held primary Git checkout under the repository root.")
    dirs = git(repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir").splitlines()
    if len(dirs) != 2 or not Path(dirs[0]).samefile(dirs[1]):
        raise ValueError("Select the primary checkout instead of a linked worktree.")
    return [*identity(repo), *identity(Path(dirs[0]))]


def bootstrap_files(repo):
    paths = [*PINS, ".gitignore"]
    for parent in ("shared", "shared/redlines"):
        if not (repo / parent).exists():
            paths.append(parent + "/.gitkeep")
    return paths


def preflight(repo):
    for name in (*RESERVED, ".gitignore", "shared", "shared/redlines"):
        path = repo / name
        if path.resolve() != path:
            raise ValueError(f"Install path resolves outside its expected location: {path}")
        if name in RESERVED and path.exists():
            raise ValueError(f"Install path already exists: {path}")
    for name in ("shared", "shared/redlines"):
        if (repo / name).exists() and not (repo / name).is_dir():
            raise ValueError(f"Directory required: {repo / name}")
    if git(repo, "status", "--porcelain", "--", *RESERVED, ".gitignore") or git(repo, "ls-files", "--", *RESERVED):
        raise ValueError("Resolve project changes or tracked files on the bootstrap paths first.")
    if git(repo, "ls-files", "--unmerged"):
        raise ValueError("Resolve the project's Git conflicts before installing.")
    remotes = git(repo, "remote").splitlines()
    if "super-coder" in remotes and git(repo, "remote", "get-url", "super-coder") != SOURCE:
        raise ValueError("The existing super-coder remote has a different source.")
    if "origin" in remotes:
        origin = git(repo, "remote", "get-url", "origin")
        if origin.rstrip("/").split("/")[-1].removesuffix(".git") in ("super-coder", "subfloor"):
            raise ValueError("The Subfloor source repository cannot be provisioned as a fork.")


def prepared(repo, mode, previous):
    """Resolve exactly what this submission would resume before offering Apply."""
    if previous and previous.get("stage") != "done":
        if previous["mode"] != mode:
            raise ValueError("Resume using the original new/existing repository choice.")
        if previous.get("directory") and (not repo.exists() or identity(repo) != previous["directory"]):
            raise ValueError("The checkout was replaced since provisioning began.")
        if previous.get("primary") and primary(repo) != previous["primary"]:
            raise ValueError("The Git checkout changed since provisioning began.")
        return previous
    if repo.exists():
        key = primary(repo)
        cfg = install_config(repo.parent, repo)
        if cfg and cfg.get("installed_at"):
            raise ValueError(f"Already installed at {repo}. Refresh inventory to open it.")
        if mode == "create":
            raise ValueError(f"Name already occupied: {repo}")
        preflight(repo)
        return {"stage": "bootstrap", "mode": mode, "primary": key,
                "directory": identity(repo), "files": bootstrap_files(repo)}
    if mode == "install":
        raise ValueError(f"Existing checkout was not found: {repo}")
    return {"stage": "create", "mode": mode, "files": bootstrap_files(repo)}


def seed_file(path, data, *, executable=False):
    if path.resolve() != path:
        raise ValueError(f"Unexpected symbolic link at {path}")
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Bootstrap path has different contents: {path}")
    else:
        with path.open("xb") as file:
            file.write(data)
    if executable:
        path.chmod(0o755)


def provision(repo, state, save, run, launch):
    def advance(stage):
        state["stage"] = stage
        save(state)

    if state["stage"] == "create":
        repo.mkdir(mode=0o700)
        state["directory"] = identity(repo)
        advance("initialize")
    if state["stage"] == "initialize":
        run("Initialize Git", "git", "init", "-b", "main")
        state["primary"] = primary(repo)
        advance("bootstrap")
    if state["stage"] == "bootstrap":
        if primary(repo) != state["primary"]:
            raise ValueError("Git checkout identity changed.")
        preflight(repo)
        head = command("git", "-C", str(repo), "rev-parse", "--verify", "HEAD")
        if head is None:
            raise ValueError("Cannot verify the project history.")
        if head.returncode:
            run("Initialize history", "git", "-c", "core.hooksPath=/dev/null", "commit",
                "--allow-empty", "--only", "-m", "Initialize repository")
        if "super-coder" not in git(repo, "remote").splitlines():
            git(repo, "remote", "add", "-t", "main", "super-coder", SOURCE)
        run("Fetch Subfloor", "git", "fetch", "super-coder", "refs/heads/main:refs/remotes/super-coder/main")
        state["ref"] = git(repo, "rev-parse", "refs/remotes/super-coder/main^{commit}")
        advance("materialize")
    if state["stage"] == "materialize":
        ref = state["ref"]
        launcher = subprocess.check_output(["git", "show", ref + ":sc"], cwd=repo)
        seed_file(repo / "sc", launcher, executable=True)
        folder = repo / ".sc-state"
        if folder.resolve() != folder:
            raise ValueError("Unsafe bootstrap state directory.")
        folder.mkdir(exist_ok=True)
        seed_file(folder / "engine.source", (SOURCE + "\n").encode())
        seed_file(folder / "engine.ref", (ref + "\n").encode())
        advance("install")
    if state["stage"] in ("install", "commit"):
        for path in PINS:
            if (repo / path).resolve() != repo / path:
                raise ValueError(f"Bootstrap path changed: {repo / path}")
        if ((repo / PINS[1]).read_text().strip() != SOURCE
                or (repo / PINS[2]).read_text().strip() != state["ref"]
                or (repo / "sc").read_bytes() != subprocess.check_output(["git", "show", state["ref"] + ":sc"], cwd=repo)):
            raise ValueError("The checkout no longer matches the selected bootstrap revision.")
    if state["stage"] == "install":
        cfg = install_config(repo.parent, repo) or {}
        if not cfg.get("installed_at"):
            run("Install Subfloor", "./sc", "install", "--runtime", state["runtime"],
                "--username=" + state["username"])
        cfg = install_config(repo.parent, repo)
        if not cfg or not cfg.get("installed_at") or cfg.get("runtime") != state["runtime"]:
            raise ValueError("Installer did not confirm the selected runtime and installation.")
        state["instance"] = cfg.get("instance_id")
        advance("commit")
    if state["stage"] == "commit":
        files = state["files"]
        git(repo, "add", "-f", "--", *files)
        if git(repo, "diff", "--cached", "--name-only", "--", *files):
            run("Commit bootstrap files", "git", "-c", "core.hooksPath=/dev/null", "commit",
                "--only", "-m", COMMIT, "--", *files)
        advance("launch")
    if state["stage"] == "launch":
        cfg = install_config(repo.parent, repo)
        if not cfg or not cfg.get("installed_at") or cfg.get("instance_id") != state["instance"]:
            raise ValueError("The provisioned instance changed before launch.")
        launch(cfg)
        advance("done")
