#!/usr/bin/env python3
"""Automated branch prune — the acting sibling of git_hygiene.py's read pass.

`git_hygiene.py` reports which local branches are stale (PR provably merged,
not a worktree base, not checked out anywhere); this deletes exactly that set
and nothing else. It re-derives no criteria of its own: the safety predicate
lives in one place — `git_hygiene.compute()` -> `branch['stale']` — and this
consumes it verbatim.

Where the `git_cleanup` skill is the human-driven, full-spectrum tidy (dirty
worktrees, outstanding-work triage, remote sync), this is the narrow,
unattended subset safe to run on every boot: provably-merged local branches.
The launcher calls it once per boot from any shell; it is repo-global (one
fork, one host repo, many worktrees sharing a branch namespace), so whichever
shell boots next clears everyone's merged branches.

Safety — all inherited from git_hygiene's `stale` flag:
  - merged    authoritative via `gh pr list` (state == MERGED). When gh is
              unavailable a squash-merge cannot be proven merged, so it is
              NOT pruned (fail-safe: keep the branch).
  - not base  never the default branch or a `shell/<shortname>` worktree base.
  - not live  never a branch checked out in any worktree; `git branch -D`
              refuses those too — a second, independent guard.

Soft-fails by design: any git/gh/network hiccup yields an empty or partial
result, never an exception — a prune must never block a launch.

Run standalone:
    python3 .super-coder/scripts/git_prune.py --dry-run   # show, don't delete
    python3 .super-coder/scripts/git_prune.py             # delete, print JSON
    python3 .super-coder/scripts/git_prune.py --text      # delete, human line
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import git_hygiene

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent


def _delete_branch(name: str, repo: Path) -> bool:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", name],
            capture_output=True, text=True, timeout=12).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def prune(repo: Path = REPO_ROOT, *, dry_run: bool = False,
          fetch: bool = False, snapshot: dict | None = None) -> dict:
    """Delete every branch git_hygiene marks stale. Acts only on the `stale`
    set — never its own deletion criteria. Pass `snapshot` to reuse an existing
    git_hygiene.compute() result (or to inject one in tests); otherwise it is
    computed here. Soft-fails to an `error` result rather than raising."""
    try:
        snap = snapshot if snapshot is not None else git_hygiene.compute(fetch=fetch)
    except Exception:
        return {"deleted": [], "failed": [], "candidates": 0,
                "gh_available": False, "dry_run": dry_run, "error": True}

    stale = [b["name"] for b in snap.get("branches", []) if b.get("stale")]
    deleted, failed = [], []
    for name in stale:
        if dry_run or _delete_branch(name, repo):
            deleted.append(name)
        else:
            failed.append(name)
    return {
        "deleted": deleted,
        "failed": failed,
        "candidates": len(stale),
        "gh_available": snap.get("gh_available", False),
        "dry_run": dry_run,
    }


def status_line(result: dict) -> str | None:
    """One-line boot note — None when nothing happened, so the launcher stays
    silent on the common no-op boot and speaks only when a branch was removed."""
    if result.get("error"):
        return None
    deleted, failed = result.get("deleted", []), result.get("failed", [])
    if not deleted and not failed:
        return None
    verb = "would prune" if result.get("dry_run") else "pruned"
    parts: list[str] = []
    if deleted:
        s = "es" if len(deleted) != 1 else ""
        parts.append(f"{verb} {len(deleted)} merged branch{s} ({', '.join(deleted)})")
    if failed:
        parts.append(f"{len(failed)} could not be deleted ({', '.join(failed)})")
    return "; ".join(parts)


def main(argv: list[str]) -> int:
    result = prune(dry_run="--dry-run" in argv, fetch="--no-fetch" not in argv)
    if "--text" in argv:
        print(status_line(result) or "nothing to prune")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
