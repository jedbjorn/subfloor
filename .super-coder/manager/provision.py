"""Bootstrap one owner-selected checkout from the supported engine remote."""
import hashlib
import os
import subprocess
from pathlib import Path

from inventory import command, install_config

SOURCE = "https://github.com/jedbjorn/subfloor.git"
COMMIT = "chore: install subfloor"
PINS = ("sc", ".sc-state/engine.source", ".sc-state/engine.ref")
SCAFFOLDS = ("shared/.gitkeep", "shared/redlines/.gitkeep")
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
    for name in SCAFFOLDS:
        if not (repo / name).parent.exists():
            paths.append(name)
    return paths


def check_scaffolds(repo, files, *, committing=False):
    # A parent directory can still be a staged file in Git (a file/directory
    # conflict). Adding its children would replace that file in the index.
    parents = {str(Path(name).parent) for name in SCAFFOLDS}
    indexed = git(repo, "ls-files", "-z", "--", "shared").split("\0")
    changed = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "--", "shared").split("\0")
    if parents.intersection(indexed) or parents.intersection(changed):
        raise ValueError("Resolve project work on the scaffold directories first.")
    for name in SCAFFOLDS:
        path = repo / name
        if path.parent.resolve() != path.parent:
            raise ValueError(f"Unexpected scaffold directory: {path.parent}")
        if name not in files:
            if not path.parent.is_dir():
                raise ValueError(f"Project scaffold directory changed: {path.parent}")
            continue
        if path.resolve() != path or (path.exists() and (not path.is_file() or path.stat().st_size)):
            raise ValueError(f"Unexpected bootstrap file: {path}")
        if committing and not path.is_file():
            raise ValueError(f"Installer scaffold is missing: {path}")
        # A retry can see our intent-to-add entry or an already committed scaffold.
        index = git(repo, "ls-files", "--stage", "--", name)
        if index and committing and index == f"100644 {git(repo, 'hash-object', str(path))} 0\t{name}":
            continue
        if index or git(repo, "diff", "--cached", "--name-only", "--", name):
            raise ValueError(f"Resolve project work on the bootstrap path first: {path}")


def preflight(repo):
    check_scaffolds(repo, bootstrap_files(repo))
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


def bootstrap_output(repo, files):
    output = {}
    for name in files:
        path = repo / name
        if path.resolve() != path or not path.is_file():
            raise ValueError(f"Installer output is missing or replaced: {path}")
        output[name] = [path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest()]
    return output


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
        if bootstrap_files(repo) != state["files"]:
            raise ValueError("Project scaffold directories changed since confirmation.")
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
        check_scaffolds(repo, state["files"])
        cfg = install_config(repo.parent, repo) or {}
        if cfg.get("installed_at") and ("output" not in state or "base" not in state):
            raise ValueError("Cannot verify installer output after an interrupted checkpoint. Inspect the checkout before retrying.")
        if not cfg.get("installed_at"):
            run("Install Subfloor", "./sc", "install", "--runtime", state["runtime"],
                "--username=" + state["username"])
        cfg = install_config(repo.parent, repo)
        if not cfg or not cfg.get("installed_at") or cfg.get("runtime") != state["runtime"]:
            raise ValueError("Installer did not confirm the selected runtime and installation.")
        state["instance"] = cfg.get("instance_id")
        state["output"] = bootstrap_output(repo, state["files"])
        state["base"] = git(repo, "rev-parse", "HEAD")
        advance("commit")
    if state["stage"] == "commit":
        files = state["files"]
        if state.get("output") != bootstrap_output(repo, files):
            raise ValueError("Cannot verify unchanged installer output. Inspect the bootstrap files before retrying.")
        if git(repo, "diff", "--cached", "--ita-invisible-in-index", "--name-only", "--", *files):
            raise ValueError("Resolve staged project work on the bootstrap paths before retrying.")
        check_scaffolds(repo, files, committing=True)
        if git(repo, "diff", "--name-only", state["base"], "HEAD", "--", *files):
            # The commit may have landed just before its checkpoint write.
            committed = git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", *files).splitlines()
            if set(committed) != set(files) or git(repo, "diff", "HEAD", "--name-only", "--", *files):
                raise ValueError("Project history changed on the bootstrap paths. Inspect the checkout before retrying.")
        # A path-limited commit needs index entries for new files, but no
        # installer contents need to be staged, even across a failed commit.
        git(repo, "add", "--intent-to-add", "-f", "--", *files)
        if git(repo, "diff", "HEAD", "--name-only", "--", *files):
            run("Commit bootstrap files", "git", "-c", "core.hooksPath=/dev/null", "commit",
                "--only", "-m", COMMIT, "--", *files)
        advance("launch")
    if state["stage"] == "launch":
        cfg = install_config(repo.parent, repo)
        if not cfg or not cfg.get("installed_at") or cfg.get("instance_id") != state["instance"]:
            raise ValueError("The provisioned instance changed before launch.")
        launch(cfg)
        advance("done")
