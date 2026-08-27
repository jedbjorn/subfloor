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

# Resolve only the fixed engine-owned provenance contributor before admission.
# This deliberately does not execute git, inspect SC_DISPATCH/SC_CALLER_ROOT,
# discover credentials, or import a command handler. Linked-worktree .git files
# name their common directory, so shell builtins are sufficient to find the
# main checkout that owns the materialized engine.
case "$0" in
  */*) sc_boot_dir=${0%/*} ;;
  *) sc_boot_dir=. ;;
esac
CALLER_ROOT="$(CDPATH= cd -- "$sc_boot_dir" && pwd -P)"
LIVE_ROOT="$CALLER_ROOT"
if [ -f "$CALLER_ROOT/.git" ]; then
  IFS= read -r sc_gitdir_line < "$CALLER_ROOT/.git" || sc_gitdir_line=
  case "$sc_gitdir_line" in
    "gitdir: "*)
      sc_gitdir=${sc_gitdir_line#gitdir: }
      case "$sc_gitdir" in
        /*) : ;;
        *) sc_gitdir="$CALLER_ROOT/$sc_gitdir" ;;
      esac
      sc_gitdir="$(CDPATH= cd -- "$sc_gitdir" 2>/dev/null && pwd -P || true)"
      if [ -n "$sc_gitdir" ] && [ -r "$sc_gitdir/commondir" ]; then
        IFS= read -r sc_commondir < "$sc_gitdir/commondir" || sc_commondir=
        case "$sc_commondir" in
          /*) : ;;
          *) sc_commondir="$sc_gitdir/$sc_commondir" ;;
        esac
        sc_commondir="$(CDPATH= cd -- "$sc_commondir" 2>/dev/null && pwd -P || true)"
        if [ -n "$sc_commondir" ]; then
          sc_common_parent=${sc_commondir%/*}
          [ -d "$sc_common_parent/.super-coder" ] && LIVE_ROOT="$sc_common_parent"
        fi
      fi ;;
  esac
fi

SC_PROVENANCE="$LIVE_ROOT/.super-coder/scripts/dsh_execution_provenance.py"
SC_POLICY="$LIVE_ROOT/.super-coder/assets/deepseek/dsh-shell-authority-contract.json"
SC_ADMISSION_FLOOR="$LIVE_ROOT/.super-coder/assets/deepseek/dsh-command-admission-v1.json"
SC_SYSTEM_PYTHON="$(command -p -v python3 2>/dev/null || true)"
if [ -n "$SC_SYSTEM_PYTHON" ] && [ -r "$SC_PROVENANCE" ] && \
   [ -r "$SC_POLICY" ] && [ -r "$SC_ADMISSION_FLOOR" ]; then
  exec "$SC_SYSTEM_PYTHON" "$SC_PROVENANCE" \
    --admit-sc \
    --caller-root "$CALLER_ROOT" \
    --live-root "$LIVE_ROOT" \
    --policy-contract "$SC_POLICY" \
    -- "$@"
fi

# Compatibility floor for a native caller rolling back to an older engine.
# Managed/ambiguous DSH provenance and every DSH bridge fact remain refused;
# only a clean native caller may use the legacy dispatcher bootstrap.
sc_native=0
if [ -r /proc/self/cgroup ]; then
  sc_unified_count=0
  sc_managed_membership=0
  while IFS= read -r sc_cgroup_row; do
    case "$sc_cgroup_row" in
      0::/*)
        sc_unified_count=$((sc_unified_count + 1))
        case "$sc_cgroup_row" in *"/sc-dsh/"*) sc_managed_membership=1 ;; esac ;;
    esac
  done < /proc/self/cgroup
  [ "$sc_unified_count" -eq 1 ] && [ "$sc_managed_membership" -eq 0 ] && sc_native=1
fi
if [ "$sc_native" -ne 1 ] || [ "${DSH_SHELL+x}" = x ] || \
   [ "${DSH_SC_SHELL_ID+x}" = x ] || [ "${DSH_SC_SHELL_SHORTNAME+x}" = x ] || \
   [ "${DSH_SC_SHELL_WORKTREE+x}" = x ] || [ "${DSH_SC_API_BASE+x}" = x ] || \
   [ "${DSH_SC_MEM_CREDENTIAL_FILE+x}" = x ] || \
   [ "${DSH_SC_BINDING_GENERATION+x}" = x ] || \
   [ "${DSH_SC_PLUGIN_HEALTH_GENERATION+x}" = x ]; then
  echo "sc: DSH command refused before effect: provenance contributor unavailable" >&2
  exit 77
fi

DISPATCH="$LIVE_ROOT/.super-coder/scripts/dispatch.sh"
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
SC_CALLER_ROOT="$CALLER_ROOT" exec sh "$DISPATCH" "$@"
