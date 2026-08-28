#!/bin/sh
# super-coder entry point — a stable bootstrap, not the dispatcher (spec #105,
# single-owner dispatcher). The dispatcher body is engine-owned and
# materialized by `./sc update` (.super-coder/scripts/dispatch.sh), so every
# checkout of an install — the main checkout or any linked worktree, on any
# branch age — dispatches the LIVE engine floor. This file only resolves WHERE
# that floor is during normal dispatch: it carries no verbs or help text and
# never writes. A stale committed copy remains harmless for ordinary dispatch.
# Run from the repo root:  ./sc <command> [args]
set -e
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CALLER_ROOT="$here"

# LIVE_ROOT: the main worktree root (owner of .super-coder/ and the live DB),
# resolved via git's common dir — the algorithm the dispatcher body documents
# (spec #68). Resolution failure = standalone root: one identity, caller is
# live, and we never guess a second target.
LIVE_ROOT="$CALLER_ROOT"
_root="$(cd "$here" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd -P || true)"
[ -n "$_root" ] && [ -d "$_root/.super-coder" ] && LIVE_ROOT="$_root"

DISPATCH="$LIVE_ROOT/.super-coder/scripts/dispatch.sh"

# SC_DISPATCH is an operator-owned maintainer test seam. The selected body owns
# its own host boundary, just as any locally edited checkout does.
if [ -n "${SC_DISPATCH:-}" ]; then
  if [ ! -f "$SC_DISPATCH" ] || [ ! -r "$SC_DISPATCH" ]; then
    echo "✗ ./sc: SC_DISPATCH is set but not readable: $SC_DISPATCH" >&2
    exit 1
  fi
  DISPATCH="$SC_DISPATCH"
fi

if [ ! -f "$DISPATCH" ]; then
  {
    if [ ! -d "$LIVE_ROOT/.super-coder" ]; then
      echo "✗ ./sc: no engine found."
      echo "    caller root : $CALLER_ROOT"
      echo "    live root   : $LIVE_ROOT"
      echo "  Neither holds .super-coder/. Run from a super-coder install, or"
      echo "  install one first (see README)."
    else
      echo "✗ ./sc: engine floor predates this launcher."
      echo "    engine       : $LIVE_ROOT/.super-coder"
      echo "    missing body : $DISPATCH"
      echo "  This bootstrap dispatches an engine-owned body that this floor does"
      echo "  not carry. Run ./sc from the main checkout ($LIVE_ROOT), or finish"
      echo "  the update/rollback so the engine and launcher are a matched pair."
    fi
  } >&2
  exit 1
fi

SC_CALLER_ROOT="$here" exec sh "$DISPATCH" "$@"
