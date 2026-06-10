#!/usr/bin/env bash
# super-coder branch guard — Claude Code PreToolUse hook for Edit/Write/NotebookEdit.
#
# Refuses file edits while HEAD is a protected (default) branch, forcing a
# feature branch BEFORE any work lands. This is the enforcement behind the git
# skill's "never commit straight to the default branch" — a lazy-loaded skill
# can't fire before the first edit; this hook does.
#
# Contract: exit 0 = allow the tool call; exit 2 = block it and feed stderr back
# to the model (Claude Code PreToolUse protocol). Any other non-zero is a
# non-blocking error. We do not read stdin — the decision needs only the branch.
set -euo pipefail

# Not a git work tree (rare for a fork, but be safe) → nothing to guard.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

# Protected set: default branches. Override per-fork with SC_PROTECTED_BRANCHES
# (space-separated) if the fork's default branch isn't main/master. Detached
# HEAD reports as "HEAD" and is not protected — rebases/bisects stay unblocked.
protected="${SC_PROTECTED_BRANCHES:-main master}"

for p in $protected; do
  if [ "$branch" = "$p" ]; then
    echo "Blocked: you are on '$branch' (a protected default branch). Create a feature branch before editing files:" >&2
    echo "    git checkout -b feat/<short-desc>   # or fix/ chore/ docs/" >&2
    echo "then retry the edit. One branch per unit of work — see the 'git' skill (branch -> commit -> push -> PR -> stop)." >&2
    exit 2
  fi
done

exit 0
