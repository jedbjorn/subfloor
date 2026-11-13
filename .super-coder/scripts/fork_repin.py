#!/usr/bin/env python3
"""Land a fork's engine pin on its default branch without operator git work.

`./sc update` rewrites a handful of tracked, engine-owned files in place: the
`sc` bootstrap, `.sc-state/engine.ref`, `.sc-state/engine.source` and the
managed block of `.gitignore`. Shell worktrees only see what is committed, so a
pin that never reaches the fork's default branch leaves every worktree on a
stale (or, for a fork that never committed `sc`, missing) launcher — issue #702.

`sync` closes that gap (decision #459). It runs after `sc update` and on every
`sc launch` / `sc restart`:

1. compare the working copies of those files with `<remote>/<default>`;
2. on drift, build one commit on top of the remote branch with git plumbing (the
   main checkout's branch, index and other edits are never touched), force-push
   it to the fixed `subfloor/repin` branch and open (or reuse) its PR;
3. merge it — immediately, or by enabling auto-merge when the base requires
   checks — under the FnB's standing directive, which covers only this
   engine-authored PR class;
4. fast-forward the main checkout once the pin is on the remote branch.

Every failure is a one-line warning: launch and restart never block on it, and
the next run retries from wherever this one stopped.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from install import SOURCE_REPO_NAMES

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
MANAGED_FILES = ("sc", ".sc-state/engine.ref", ".sc-state/engine.source")
GITIGNORE = ".gitignore"
GITIGNORE_BEGIN = "# >>> subfloor managed ignores >>>"
GITIGNORE_END = "# <<< subfloor managed ignores <<<"
REPIN_BRANCH = "subfloor/repin"
NETWORK_TIMEOUT = 90
_GITHUB_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


class RepinError(RuntimeError):
    """One step could not complete; the caller reports it and moves on."""


def _git(repo: Path, *args: str, data: bytes | None = None,
         env: dict[str, str] | None = None,
         timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=data, capture_output=True,
        env=env, timeout=timeout, check=False,
    )


def _git_ok(repo: Path, *args: str, **kwargs) -> str:
    result = _git(repo, *args, **kwargs)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise RepinError(f"git {args[0]} failed: {detail[-1] if detail else result.returncode}")
    return result.stdout.decode(errors="replace").strip()


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    # Run outside any checkout so --repo alone names the target; a fork has a
    # second (engine upstream) remote gh could otherwise prompt about.
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, cwd=tempfile.gettempdir(),
        timeout=NETWORK_TIMEOUT, check=False,
    )


def _gh_ok(*args: str) -> str:
    result = _gh(*args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RepinError(f"gh {args[0]} {args[1]} failed: {detail[-1] if detail else result.returncode}")
    return result.stdout.strip()


def _remote_blob(repo: Path, ref: str, path: str) -> bytes | None:
    result = _git(repo, "show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def _managed_block(text: str) -> str | None:
    start = text.find(GITIGNORE_BEGIN)
    end = text.find(GITIGNORE_END, start)
    if start < 0 or end < 0:
        return None
    return text[start:end + len(GITIGNORE_END)]


def _spliced_gitignore(local: str, remote: str | None) -> str | None:
    """Remote `.gitignore` carrying the local managed block, else None."""
    block = _managed_block(local)
    if block is None:
        return None
    remote = remote or ""
    current = _managed_block(remote)
    if current is not None:
        return remote.replace(current, block, 1)
    if remote and not remote.endswith("\n"):
        remote += "\n"
    return f"{remote}\n{block}\n" if remote else f"{block}\n"


def desired_changes(repo: Path, upstream: str) -> dict[str, tuple[str, bytes]]:
    """Managed paths whose local content is missing from ``upstream``."""
    changes: dict[str, tuple[str, bytes]] = {}
    for rel in MANAGED_FILES:
        path = repo / rel
        if not path.is_file():
            continue  # never commit a deletion
        local = path.read_bytes()
        if local != _remote_blob(repo, upstream, rel):
            mode = "100755" if os.access(path, os.X_OK) else "100644"
            changes[rel] = (mode, local)
    gitignore = repo / GITIGNORE
    if gitignore.is_file():
        remote = _remote_blob(repo, upstream, GITIGNORE)
        remote_text = remote.decode() if remote is not None else None
        spliced = _spliced_gitignore(gitignore.read_text(), remote_text)
        if spliced is not None and spliced != remote_text:
            changes[GITIGNORE] = ("100644", spliced.encode())
    return changes


def build_commit(repo: Path, upstream: str, changes: dict[str, tuple[str, bytes]],
                 message: str) -> str:
    """One commit on ``upstream`` with ``changes``, built off a private index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if not _git(repo, "config", "user.name").stdout.strip():
            env.setdefault("GIT_AUTHOR_NAME", "Subfloor")
            env.setdefault("GIT_COMMITTER_NAME", "Subfloor")
        if not _git(repo, "config", "user.email").stdout.strip():
            env.setdefault("GIT_AUTHOR_EMAIL", "subfloor@users.noreply.github.com")
            env.setdefault("GIT_COMMITTER_EMAIL", "subfloor@users.noreply.github.com")
        _git_ok(repo, "read-tree", upstream, env=env)
        for rel, (mode, data) in sorted(changes.items()):
            blob = _git_ok(repo, "hash-object", "-w", "--stdin", data=data)
            _git_ok(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=env)
        tree = _git_ok(repo, "write-tree", env=env)
        return _git_ok(repo, "commit-tree", tree, "-p", upstream, "-m", message, env=env)


def fast_forward(repo: Path, branch: str, upstream: str) -> str:
    """Bring the main checkout onto ``upstream``; return a one-line outcome.

    Managed files whose local bytes already equal ``upstream`` (the update
    wrote them in place, tracked or not) would make git refuse the
    fast-forward, so they are set aside first and restored if it still fails.
    """
    current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.decode().strip()
    if current != branch:
        return f"main checkout is on '{current or 'detached HEAD'}'; `git pull --ff-only` on {branch} picks up the pin"
    if _git_ok(repo, "rev-parse", "HEAD") == _git_ok(repo, "rev-parse", upstream):
        return ""
    if _git(repo, "merge-base", "--is-ancestor", "HEAD", upstream).returncode != 0:
        return f"local {branch} has diverged from {upstream}; reconcile it by hand"
    aside: list[tuple[Path, bytes, int, bool]] = []
    for rel in (*MANAGED_FILES, GITIGNORE):
        path = repo / rel
        if not path.is_file() or path.read_bytes() != _remote_blob(repo, upstream, rel):
            continue
        tracked = _git(repo, "cat-file", "-e", f"HEAD:{rel}").returncode == 0
        aside.append((path, path.read_bytes(), path.stat().st_mode, tracked))
        if tracked:
            _git_ok(repo, "checkout", "HEAD", "--", rel)
        else:
            path.unlink()
    result = _git(repo, "merge", "--ff-only", "--quiet", upstream)
    if result.returncode == 0:
        return f"main checkout fast-forwarded to {upstream}"
    for path, data, mode, _tracked in aside:
        path.write_bytes(data)
        os.chmod(path, mode)
    detail = result.stderr.decode(errors="replace").strip().splitlines()
    return f"fast-forward refused ({detail[0] if detail else 'unknown'}); `git pull --ff-only` after clearing it"


def _github_repo(repo: Path, remote: str) -> str:
    url = _git_ok(repo, "remote", "get-url", remote)
    match = _GITHUB_RE.search(url)
    if not match:
        raise RepinError(f"{remote} is not a GitHub remote ({url})")
    return f"{match.group(1)}/{match.group(2)}"


def _default_branch(repo: Path, remote: str) -> str:
    ref = _git(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    name = ref.stdout.decode().strip()
    return name.split("/", 1)[1] if ref.returncode == 0 and "/" in name else "main"


def _merge_method(slug: str) -> str:
    allowed = _gh_ok("repo", "view", slug, "--json",
                     "squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed",
                     "-q", "[.squashMergeAllowed,.mergeCommitAllowed,.rebaseMergeAllowed]|@tsv")
    flags = allowed.split()
    for flag, method in zip(flags, ("--squash", "--merge", "--rebase")):
        if flag == "true":
            return method
    raise RepinError(f"{slug} allows no merge method")


def open_and_merge(slug: str, branch: str, title: str, body: str) -> tuple[int, bool]:
    """Open or reuse the repin PR and merge it; return (number, merged now)."""
    number = _gh_ok("pr", "list", "--repo", slug, "--head", REPIN_BRANCH, "--base", branch,
                    "--state", "open", "--json", "number", "-q", ".[0].number")
    if not number:
        url = _gh_ok("pr", "create", "--repo", slug, "--base", branch, "--head", REPIN_BRANCH,
                     "--title", title, "--body", body)
        number = url.rstrip("/").rsplit("/", 1)[-1]
    pr = int(number)
    method = _merge_method(slug)
    # The branch is left in place: every repin force-pushes it fresh.
    if _gh("pr", "merge", str(pr), "--repo", slug, method).returncode == 0:
        return pr, True
    # Required checks (or another base policy) still pending: let GitHub land
    # it when they pass. The next launch then fast-forwards.
    _gh_ok("pr", "merge", str(pr), "--repo", slug, method, "--auto")
    return pr, False


def is_source_repo(repo: Path, remote: str = "origin") -> bool:
    url = _git(repo, "remote", "get-url", remote).stdout.decode().strip()
    name = url.rstrip("/").split("/")[-1].split(":")[-1].removesuffix(".git")
    return bool(url) and name in SOURCE_REPO_NAMES


def sync(repo: Path = REPO_ROOT, remote: str = "origin") -> list[str]:
    """Reconcile the fork's committed pin with its working copy; report lines."""
    if is_source_repo(repo, remote):
        return []  # the engine source tracks itself; its changes ship by PR
    branch = _default_branch(repo, remote)
    upstream = f"{remote}/{branch}"
    _git_ok(repo, "fetch", "--quiet", remote,
            f"+refs/heads/{branch}:refs/remotes/{upstream}", timeout=NETWORK_TIMEOUT)
    changes = desired_changes(repo, upstream)
    if not changes:
        # The pin is on the remote branch; pull only when this checkout still
        # lacks it (a repin merged after an earlier launch).
        behind = desired_changes(repo, "HEAD")
        return [fast_forward(repo, branch, upstream)] if behind else []

    pin = (repo / ".sc-state" / "engine.ref").read_text().strip()[:12] \
        if (repo / ".sc-state" / "engine.ref").is_file() else "unpinned"
    title = f"chore(engine): repin {pin}"
    paths = ", ".join(sorted(changes))
    body = (
        f"Engine-authored repin: lands the live engine pin on `{branch}` ({paths}).\n\n"
        "Opened and merged automatically by `sc update` / `sc launch` / `sc restart` "
        "(Subfloor decision #459). It touches only engine-managed paths."
    )
    slug = _github_repo(repo, remote)
    commit = build_commit(repo, upstream, changes, title)
    _git_ok(repo, "push", "--quiet", "--force", remote,
            f"{commit}:refs/heads/{REPIN_BRANCH}", timeout=NETWORK_TIMEOUT)
    pr, merged = open_and_merge(slug, branch, title, body)
    if not merged:
        return [f"PR #{pr} ({paths}) set to auto-merge; the next launch pulls it"]
    _git_ok(repo, "fetch", "--quiet", remote,
            f"+refs/heads/{branch}:refs/remotes/{upstream}", timeout=NETWORK_TIMEOUT)
    return [f"merged PR #{pr} ({paths})", fast_forward(repo, branch, upstream)]


def main(argv: list[str]) -> int:
    if argv != ["sync"]:
        print("usage: fork_repin.py sync", file=sys.stderr)
        return 2
    try:
        lines = sync()
    except (RepinError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"⚠ repin: {exc} — the next launch retries", file=sys.stderr)
        return 0
    for line in filter(None, lines):
        print(f"→ repin: {line}")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
