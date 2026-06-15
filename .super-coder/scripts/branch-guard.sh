#!/usr/bin/env bash
# super-coder branch guard — ONE branch-decision script, four consumers.
#
# Refuses an operation while the work would land on a protected (default) branch,
# forcing a feature branch BEFORE work lands. This is the enforcement behind the
# git skill's "never commit straight to the default branch" — a lazy-loaded skill
# can't fire before the first edit; this does. Wired as:
#   • claude   — PreToolUse hook (.claude/settings.local.json)
#   • codex    — PreToolUse hook (.codex/hooks.json, matcher ^apply_patch$)
#   • opencode — plugin tool.execute.before shells out here
#   • git      — pre-commit hook (.super-coder/hooks/pre-commit), the universal
#                backstop that also covers vibe (which has no pre-tool hook)
#
# Contract is shared across all four: exit 0 = allow; exit 2 = block + print the
# reason to stderr. Claude/codex surface stderr to the model; git aborts the
# commit and prints it.
#
# TWO checks, in order of authority:
#   1. TARGET FILE (claude only) — claude passes the edit's PreToolUse JSON on
#      stdin (tool_input.file_path/notebook_path). We resolve the branch of the
#      repo that OWNS that file and block if it is protected. This catches the
#      foot-gun a cwd-only check misses: a worktree shell editing files in the
#      stale main checkout (a DIFFERENT repo dir on `main`) — the cwd is a clean
#      feature branch, so the old cwd check waved it through. If the target is
#      outside the shell's own worktree but NOT on a protected branch, we ALLOW
#      but emit a loud warning (claude additionalContext + stderr).
#   2. CWD (all consumers, and the fallback when there is no stdin target) — the
#      original behavior: block if HEAD of the cwd's repo is a protected branch.
#
# The harness hooks find this script via $SC_ENGINE_DIR (absolute path to the
# installed engine, exported by run.py at launch). A fork gitignores
# .super-coder/, so it is ABSENT from every shell worktree — a worktree-relative
# path resolved to nothing and the hook failed open. The pre-commit hook can't
# rely on that env (the FnB may commit from a plain terminal); it resolves the
# script by its own $0 path instead.
set -euo pipefail

# Not a git work tree (rare for a fork, but be safe) → nothing to guard.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# The admin shell is the ONE exemption: it boots in the repo root (no worktree)
# and maintains the default branch directly — engine updates, migrations,
# applying approved patches. run.py exports SC_SHELL_FLAVOR at launch; like
# SC_PROTECTED_BRANCHES this is a guardrail against accidents, not a security
# boundary.
if [ "${SC_SHELL_FLAVOR:-}" = "admin" ]; then
  exit 0
fi

# Protected set: default branches. Override per-fork with SC_PROTECTED_BRANCHES
# (space-separated) if the fork's default branch isn't main/master.
protected="${SC_PROTECTED_BRANCHES:-main master}"

is_protected() {
  local b="$1" p
  for p in $protected; do
    [ "$b" = "$p" ] && return 0
  done
  return 1
}

# Branch of the repo containing dir $1 ("" if none). symbolic-ref answers "which
# branch is HEAD" reliably: it returns the name even on an unborn branch (first
# commit on main → still guarded), and nothing on detached HEAD (rebases/bisects
# stay unblocked). Fall back to rev-parse.
branch_of() {
  git -C "$1" symbolic-ref --short HEAD 2>/dev/null \
    || git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null \
    || echo ""
}
toplevel_of() { git -C "$1" rev-parse --show-toplevel 2>/dev/null || echo ""; }

feature_branch_hint() {
  echo "    git checkout -b feat/<short-desc>   # or fix/ chore/ docs/" >&2
  echo "then retry. One branch per unit of work — see the 'git' skill (branch -> commit -> push -> PR -> stop)." >&2
}

# ── Check 1: the target file (claude PreToolUse stdin) ──────────────────────
# Read any JSON on stdin; other consumers pass nothing → target stays empty and
# we fall through to the cwd check. python3 is an engine dependency wherever the
# claude hook runs; if it is somehow absent we simply skip the target check.
payload="$(cat 2>/dev/null || true)"
target=""
if [ -n "$payload" ] && command -v python3 >/dev/null 2>&1; then
  target="$(printf '%s' "$payload" | python3 -c '
import sys, json
try:
    ti = (json.load(sys.stdin).get("tool_input") or {})
    print(ti.get("file_path") or ti.get("notebook_path") or "")
except Exception:
    print("")
' 2>/dev/null || echo "")"
fi

if [ -n "$target" ]; then
  # Deepest existing ancestor dir of the target (a new file's dir exists; its
  # path does not yet) so `git -C` has a real dir to resolve from.
  tdir="$target"
  while [ -n "$tdir" ] && [ "$tdir" != "/" ] && [ ! -d "$tdir" ]; do
    tdir="$(dirname "$tdir")"
  done
  [ -d "${tdir:-}" ] || tdir="."
  tgt_top="$(toplevel_of "$tdir")"
  if [ -n "$tgt_top" ]; then
    tgt_branch="$(branch_of "$tdir")"
    if is_protected "$tgt_branch"; then
      echo "Blocked: this edit targets '$target', which is in repo '$tgt_top' on protected branch '$tgt_branch'." >&2
      echo "You are about to write to a default-branch checkout (often the stale repo root, NOT your worktree)." >&2
      cwd_top="$(toplevel_of .)"
      if [ -n "$cwd_top" ] && [ "$cwd_top" != "$tgt_top" ]; then
        echo "Your worktree is '$cwd_top' (on '$(branch_of .)'). Edit there, or create a feature branch:" >&2
      else
        echo "Create a feature branch first:" >&2
      fi
      feature_branch_hint
      exit 2
    fi
    # In a repo, on a feature branch, but OUTSIDE the shell's own worktree →
    # allow with a loud warning (the chosen "block protected + warn out-of-tree"
    # policy). additionalContext puts the warning in claude's context; stderr
    # shows it to the human. Other harnesses never reach here (no stdin target).
    cwd_top="$(toplevel_of .)"
    if [ -n "$cwd_top" ] && [ "$tgt_top" != "$cwd_top" ]; then
      warn="⚠ branch-guard: editing '$target' OUTSIDE your worktree. That file is in '$tgt_top' (on '$tgt_branch'); your worktree is '$cwd_top'. Allowed because it is not a protected branch — but cross-tree edits are how stale-tree/wrong-tree mistakes happen. Confirm this path is intentional."
      printf '%s\n' "$warn" >&2
      python3 -c 'import json,sys; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":sys.argv[1]}}))' "$warn" 2>/dev/null || true
      exit 0
    fi
    # Target is inside the shell's own worktree on a feature branch → fine.
    exit 0
  fi
  # Target not in any git repo (e.g. /tmp scratch) → fall through to cwd check.
fi

# ── Check 2: the cwd (all consumers / no-stdin fallback) ────────────────────
branch="$(branch_of .)"
if is_protected "$branch"; then
  echo "Blocked: HEAD is on protected default branch '$branch'. Create a feature branch first:" >&2
  feature_branch_hint
  exit 2
fi

exit 0
