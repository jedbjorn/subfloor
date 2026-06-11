#!/usr/bin/env bash
# super-coder branch guard — ONE branch-decision script, four consumers.
#
# Refuses an operation while HEAD is a protected (default) branch, forcing a
# feature branch BEFORE work lands. This is the enforcement behind the git
# skill's "never commit straight to the default branch" — a lazy-loaded skill
# can't fire before the first edit; this does. Wired as:
#   • claude   — PreToolUse hook (.claude/settings.local.json)
#   • codex    — PreToolUse hook (.codex/hooks.json, matcher ^apply_patch$)
#   • opencode — plugin tool.execute.before shells out here
#   • git      — pre-commit hook (.super-coder/hooks/pre-commit), the universal
#                backstop that also covers vibe (which has no pre-tool hook)
#
# Contract is shared across all four: exit 0 = allow; exit 2 = block + print the
# reason to stderr. Claude/codex surface stderr to the model; git aborts the
# commit and prints it. We read no stdin — the decision needs only the branch.
# Operates on the cwd's repo (callers run it at the repo root / pass cwd).
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

# symbolic-ref answers "which branch is HEAD" reliably: it returns the name even
# on an unborn branch (first commit on main → still guarded), and returns nothing
# on detached HEAD (rebases/bisects stay unblocked). Fall back to rev-parse.
branch=$(git symbolic-ref --short HEAD 2>/dev/null \
         || git rev-parse --abbrev-ref HEAD 2>/dev/null \
         || echo "")

# Protected set: default branches. Override per-fork with SC_PROTECTED_BRANCHES
# (space-separated) if the fork's default branch isn't main/master.
protected="${SC_PROTECTED_BRANCHES:-main master}"

for p in $protected; do
  if [ "$branch" = "$p" ]; then
    echo "Blocked: HEAD is on protected default branch '$branch'. Create a feature branch first:" >&2
    echo "    git checkout -b feat/<short-desc>   # or fix/ chore/ docs/" >&2
    echo "then retry. One branch per unit of work — see the 'git' skill (branch -> commit -> push -> PR -> stop)." >&2
    exit 2
  fi
done

exit 0
