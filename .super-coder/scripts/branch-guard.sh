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
#   1. TARGET FILE (claude + opencode) — the edited path arrives as $1 (opencode
#      plugin) or in claude's PreToolUse JSON on stdin. We resolve the branch of
#      the repo that OWNS that file and block if it is protected. This catches the
#      foot-gun a cwd-only check misses: a worktree shell editing files in the
#      stale main checkout (a DIFFERENT repo dir on `main`) — the cwd is a clean
#      feature branch, so the old cwd check waved it through. If the target is
#      outside the shell's own worktree but NOT on a protected branch, we ALLOW
#      but emit a loud warning (claude additionalContext + stderr). codex's
#      apply_patch hook supplies no usable target → it uses Check 2 only.
#   2. CWD (all consumers, and the fallback when there is no target) — the
#      original behavior: block if HEAD of the cwd's repo is a protected branch.
#
# SCRATCH EXEMPTION (before either check): a resolved target under /tmp,
# /var/tmp, /dev/shm, or $TMPDIR is allowed outright — scratch writes never land
# on a branch, and shells use /tmp heavily; without this they'd hit the cwd
# fallback (Check 2) and get blocked whenever the cwd sits on a protected branch.
#
# The harness hooks locate this script env-independently, the same way `sc` does:
# walk to the MAIN worktree root via `git rev-parse --git-common-dir`/.. and read
# its .super-coder/. A fork gitignores the engine, so it is ABSENT from every
# shell worktree — a worktree-relative path resolved to nothing and the hook
# failed open; $SC_ENGINE_DIR (exported by run.py) is only an optional fast-path
# override, never a dependency. The pre-commit hook resolves the script by its own
# $0 path (it runs outside run.py's env).
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

# The shell's HOME worktree — the tree it is MEANT to edit in. run.py exports
# SC_SHELL_WORKTREE (the dir it exec's the harness from) for every non-admin
# shell. "Outside your worktree" is judged against THIS, not the live cwd: a
# planner/dev/reviewer whose cwd has drifted to the repo root (to run a
# root-level command) is still working correctly when it edits into its own
# worktree, so it must not be warned. Falls back to the cwd for callers run.py
# didn't launch — the git pre-commit backstop, which has no SC_SHELL_WORKTREE.
home_toplevel() {
  if [ -n "${SC_SHELL_WORKTREE:-}" ] && [ -d "${SC_SHELL_WORKTREE}" ]; then
    toplevel_of "$SC_SHELL_WORKTREE"
  else
    toplevel_of .
  fi
}

feature_branch_hint() {
  echo "    git checkout -b feat/<short-desc>   # or fix/ chore/ docs/" >&2
  echo "then retry. One branch per unit of work — see the 'git' skill (branch -> commit -> push -> PR -> stop)." >&2
}

# ── Check 1: the target file ────────────────────────────────────────────────
# Resolve the edited file, in priority order:
#   1. an explicit $1 arg — the opencode plugin extracts the path from the tool
#      args and passes it here (it has the path structured; nothing to pipe).
#   2. a PreToolUse JSON payload on stdin — claude pipes tool_input.file_path /
#      notebook_path. (codex's apply_patch hook pipes no usable target, so it
#      falls through to the cwd check — see the codex adapter README.)
#   3. neither → empty → fall through to the cwd check (Check 2).
target="${1:-}"
if [ -z "$target" ]; then
  payload="$(cat 2>/dev/null || true)"
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
fi

# ── Scratch exemption: /tmp & friends ───────────────────────────────────────
# A write whose target is a temp/scratch path can never land on a branch, so the
# branch guard has nothing to protect. Shells lean on /tmp heavily for scratch
# files, and without this such a write falls through to the cwd-branch check
# (Check 2) and is blocked whenever the shell's cwd happens to sit on a protected
# branch — a pure false positive. Exempt the standard scratch roots up front.
# Only applies when we HAVE a resolved target: a no-target call (codex
# apply_patch) carries no path and still uses the cwd check.
if [ -n "$target" ]; then
  case "$target" in
    /tmp/* | /var/tmp/* | /dev/shm/* ) exit 0 ;;
  esac
  if [ -n "${TMPDIR:-}" ]; then
    case "$target" in
      "${TMPDIR%/}"/* ) exit 0 ;;
    esac
  fi
fi

# ── Shared-dir exemption: SC_SHARED_DIRS ────────────────────────────────────
# Operator-declared dirs that all shells may write into regardless of branch or
# worktree — host-level shared folders used for handoffs, screenshots, drafts.
# Set SC_SHARED_DIRS to a space-separated list of absolute paths in the launch
# environment; run.py passes it through unchanged. Only applies when we have a
# resolved target (no-target callers like codex apply_patch still use Check 2).
if [ -n "$target" ] && [ -n "${SC_SHARED_DIRS:-}" ]; then
  abs_target="$(realpath -m "$target" 2>/dev/null || echo "$target")"
  for _sd in $SC_SHARED_DIRS; do
    case "$abs_target" in
      "${_sd%/}" | "${_sd%/}"/* ) exit 0 ;;
    esac
  done
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
    # ── Gitignored exemption (#317) ─────────────────────────────────────────
    # A target git itself refuses to track can never land on ANY branch, so the
    # protected-branch rule has nothing to protect. Concretely: the boot-granted
    # shared/ handoff dir is gitignored in the main-root checkout, and blocking
    # it forced shells to side-step the hook (write to /tmp, cp via Bash) to
    # complete a documented workflow. check-ignore answers for not-yet-existing
    # paths too; tracked files are never "ignored", so no real repo file gains
    # a bypass.
    if git -C "$tdir" check-ignore -q -- "$target" 2>/dev/null; then
      exit 0
    fi
    tgt_branch="$(branch_of "$tdir")"
    if is_protected "$tgt_branch"; then
      echo "Blocked: this edit targets '$target', which is in repo '$tgt_top' on protected branch '$tgt_branch'." >&2
      echo "You are about to write to a default-branch checkout (often the stale repo root, NOT your worktree)." >&2
      home_top="$(home_toplevel)"
      if [ -n "$home_top" ] && [ "$home_top" != "$tgt_top" ]; then
        echo "Your worktree is '$home_top' (on '$(branch_of "$home_top")'). Edit there, or create a feature branch:" >&2
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
    home_top="$(home_toplevel)"
    if [ -n "$home_top" ] && [ "$tgt_top" != "$home_top" ]; then
      warn="⚠ branch-guard: editing '$target' OUTSIDE your worktree. That file is in '$tgt_top' (on '$tgt_branch'); your worktree is '$home_top'. Allowed because it is not a protected branch — but cross-tree edits are how stale-tree/wrong-tree mistakes happen. Confirm this path is intentional."
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
