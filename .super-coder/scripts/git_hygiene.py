#!/usr/bin/env python3
"""Repo git-hygiene snapshot — what's dirty, stale, or clean across this fork's
worktrees, computed live in one pass from a single vantage point.

A fork manages ONE host repo with many shells, each in its own worktree under
`.sc-worktrees/<shortname>/` (the dev-worktree model). Any single process on
the host can see every worktree on disk — `git worktree list` enumerates them
and `git -C <path> status` reads each one's dirtiness — so this never has to
poll a shell. The review server calls compute() on demand (the UI's refresh
button is the only trigger); no DB, no hooks, no persistence.

Reporting only, by design: it surfaces state, it never prunes or mutates.

Three buckets:
  - dirty worktree   — uncommitted changes in a working tree (yellow/orange)
  - stale branch     — a local branch whose PR is merged (prunable, reported)
  - clean            — in sync, nothing outstanding

"Stale" is best-effort. A squash-merge (the project default) makes a branch's
commits non-ancestors of main, so ancestry alone can't see it — authoritative
merge state comes from `gh pr list`. When gh is absent/unauthed/offline we fall
back to git ancestry (catches non-squash merges) and otherwise report
merged=null ("unknown") rather than guess. That's the "as best we can" line.

Run standalone:
    python3 .super-coder/scripts/git_hygiene.py            # JSON
    python3 .super-coder/scripts/git_hygiene.py --text      # human table
    python3 .super-coder/scripts/git_hygiene.py --no-fetch  # skip network fetch
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent

# Infrastructure branches that are never "stale prunable work": the default
# branch and the per-shell worktree bases (`shell/<shortname>`), which are
# long-lived moving bases pinned to origin/<default>, not feature branches.
_DIRTY_SAMPLE = 8     # cap the file-name sample carried per dirty worktree


def _git(*args: str, cwd: Path = REPO_ROOT, timeout: int = 12) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout)


def _out(*args: str, cwd: Path = REPO_ROOT, timeout: int = 12) -> str:
    try:
        r = _git(*args, cwd=cwd, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def default_branch() -> str:
    """The repo's default branch — SC_PROTECTED_BRANCHES[0] is authoritative in a
    fork (the launcher sets it); fall back to origin/HEAD, then 'main'."""
    env = (os.environ.get("SC_PROTECTED_BRANCHES") or "").split()
    if env:
        return env[0]
    head = _out("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        return head[len("origin/"):]
    return "main"


def _porcelain_worktrees() -> list[dict]:
    """Parse `git worktree list --porcelain` into one dict per worktree."""
    blocks: list[dict] = []
    cur: dict = {}
    for line in _out("worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"abs": val}
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "", 1)
        elif key == "detached":
            cur["detached"] = True
        elif key == "HEAD":
            cur["head"] = val
    if cur:
        blocks.append(cur)
    return blocks


def _ahead_behind(cwd: Path, base: str) -> tuple[int, int]:
    """(ahead, behind) of HEAD vs base — (0,0) if base is unresolvable."""
    raw = _out("rev-list", "--left-right", "--count", f"{base}...HEAD", cwd=cwd)
    parts = raw.split()
    if len(parts) != 2:
        return 0, 0
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0
    return ahead, behind


def _dirty(cwd: Path) -> tuple[int, list[str]]:
    """(changed-entry count, sample of 'XY path' lines) for a working tree."""
    out = _out("status", "--porcelain", cwd=cwd)
    if not out:
        return 0, []
    lines = out.splitlines()
    return len(lines), lines[:_DIRTY_SAMPLE]


def _gh_merged_prs() -> tuple[dict[str, dict] | None, bool]:
    """Map headRefName -> {number, state} from gh, or (None, False) if gh is
    unavailable. Best-effort: any failure (no gh, no auth, offline) degrades."""
    if shutil.which("gh") is None:
        return None, False
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", "300",
             "--json", "number,headRefName,state,mergedAt"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None, False
        by_branch: dict[str, dict] = {}
        for pr in json.loads(r.stdout or "[]"):
            ref = pr.get("headRefName")
            if not ref:
                continue
            # A reused branch name carries several PRs; the NEWEST (highest
            # number) is authoritative. MERGED-always-wins was unsafe: a new
            # OPEN PR on a branch whose earlier PR merged got misread as merged,
            # marking the branch stale -> `git branch -D` on live work. Newest-
            # wins also handles reopen-then-merge (newest merged -> prunable).
            num = pr.get("number") or 0
            prev = by_branch.get(ref)
            if prev is None or num > (prev.get("number") or 0):
                by_branch[ref] = {"number": pr.get("number"), "state": pr.get("state")}
        return by_branch, True
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None, False


def compute(fetch: bool = True) -> dict:
    """Live git-hygiene snapshot of the whole repo. Pure read — never mutates."""
    default = default_branch()
    upstream = f"origin/{default}"

    fetched = False
    if fetch:
        try:
            fetched = _git("fetch", "origin", default, "--quiet",
                           timeout=20).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            fetched = False

    has_upstream = bool(_out("rev-parse", "--verify", "--quiet", upstream))
    base = upstream if has_upstream else default

    # ── worktrees ──────────────────────────────────────────────────────────
    worktrees = []
    wt_branches = set()
    for wt in _porcelain_worktrees():
        abs_path = Path(wt["abs"])
        try:
            rel = str(abs_path.relative_to(REPO_ROOT)) or "."
        except ValueError:
            rel = str(abs_path)
        branch = wt.get("branch")
        if branch:
            wt_branches.add(branch)
        dirty_n, sample = _dirty(abs_path)
        ahead, behind = _ahead_behind(abs_path, base)
        worktrees.append({
            "path": "." if rel == os.curdir else rel,
            "abs": str(abs_path),
            "branch": branch,
            "detached": wt.get("detached", False),
            "is_main": abs_path.resolve() == REPO_ROOT.resolve(),
            "dirty": dirty_n,
            "dirty_files": sample,
            "ahead": ahead,
            "behind": behind,
        })

    # ── branches (staleness) ───────────────────────────────────────────────
    pr_map, gh_available = _gh_merged_prs()
    local = [b for b in _out("for-each-ref", "--format=%(refname:short)",
                             "refs/heads/").splitlines() if b]
    branches = []
    for name in local:
        is_base = name == default or name.startswith("shell/")
        checked_out = name in wt_branches
        pr = (pr_map or {}).get(name)
        if pr is not None:
            merged = pr.get("state") == "MERGED"
        elif gh_available:
            merged = False            # gh ran and reported no PR for this branch
        else:
            # gh unavailable — fall back to git ancestry (squash-merges miss this)
            anc = _git("merge-base", "--is-ancestor", name, base)
            merged = (anc.returncode == 0) or None  # None = genuinely unknown
        stale = bool(merged) and not is_base and not checked_out
        branches.append({
            "name": name,
            "is_base": is_base,
            "checked_out": checked_out,
            "merged": merged,                 # True / False / None(unknown)
            "pr": pr,
            "stale": stale,
        })

    dirty_wts = sum(1 for w in worktrees if w["dirty"])
    stale_n = sum(1 for b in branches if b["stale"])
    return {
        "repo": {
            "name": REPO_ROOT.name,
            "root": str(REPO_ROOT),
            "default_branch": default,
        },
        "fetched": fetched,
        "gh_available": gh_available,
        "worktrees": worktrees,
        "branches": branches,
        "summary": {
            "worktrees": len(worktrees),
            "dirty_worktrees": dirty_wts,
            "stale_branches": stale_n,
            "all_clean": dirty_wts == 0 and stale_n == 0,
        },
    }


def _print_text(d: dict) -> None:
    r = d["repo"]
    print(f"{r['name']}  (default: {r['default_branch']})"
          f"   fetch={'ok' if d['fetched'] else 'skipped'}"
          f"  gh={'ok' if d['gh_available'] else 'unavailable'}")
    print("\nWORKTREES")
    for w in d["worktrees"]:
        if w["dirty"]:
            state = f"✎ dirty ({w['dirty']} file{'s' if w['dirty'] != 1 else ''})"
        else:
            state = "✅ clean"
        drift = []
        if w["behind"]:
            drift.append(f"{w['behind']} behind")
        if w["ahead"]:
            drift.append(f"{w['ahead']} ahead")
        tail = f"  [{', '.join(drift)}]" if drift else ""
        print(f"  {w['path']:<28} {w['branch'] or '(detached)':<22} {state}{tail}")
    stale = [b for b in d["branches"] if b["stale"]]
    print(f"\nSTALE BRANCHES (PR merged, prunable) — {len(stale)}")
    for b in stale:
        pr = f"PR #{b['pr']['number']}" if b.get("pr") else "merged"
        print(f"  ⊘ {b['name']:<34} {pr:<10} git branch -D {b['name']}")
    if not stale:
        print("  (none)")


def main(argv: list[str]) -> int:
    d = compute(fetch="--no-fetch" not in argv)
    if "--text" in argv:
        _print_text(d)
    else:
        print(json.dumps(d, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
