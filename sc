#!/bin/sh
# super-coder entry point — a stable bootstrap, not the dispatcher (spec #105,
# single-owner dispatcher). The dispatcher body is engine-owned and
# materialized by `./sc update` (.super-coder/scripts/dispatch.sh), so every
# checkout of an install — the main checkout or any linked worktree, on any
# branch age — dispatches the LIVE engine floor. During normal dispatch this
# file only resolves WHERE that floor is: it carries no verbs or help text and
# never writes. Its one admission exception is `install` in a fresh main
# checkout. That path materializes the exact tracked engine provenance before
# handing control to the engine-owned installer. A stale committed copy remains
# harmless for ordinary dispatch.
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
sc_boot_linked=0
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
          sc_boot_linked=1
          sc_common_parent=${sc_commondir%/*}
          [ -d "$sc_common_parent/.super-coder" ] && LIVE_ROOT="$sc_common_parent"
        fi
      fi ;;
  esac
fi

DISPATCH="$LIVE_ROOT/.super-coder/scripts/dispatch.sh"
if [ -n "${SC_DISPATCH:-}" ]; then
  if [ ! -f "$SC_DISPATCH" ] || [ ! -r "$SC_DISPATCH" ]; then
    echo "✗ ./sc: SC_DISPATCH is set but not readable: $SC_DISPATCH" >&2
    exit 1
  fi
  DISPATCH="$SC_DISPATCH"
fi

sc_bootstrap_refuse() {
  echo "✗ ./sc install: cannot materialize the engine: $1" >&2
  echo "  No engine was published and no launch or health check was attempted." >&2
  exit 1
}

sc_bootstrap_engine() {
  sc_boot_state="$CALLER_ROOT/.sc-state"
  sc_boot_ref_file="$sc_boot_state/engine.ref"
  sc_boot_source_file="$sc_boot_state/engine.source"

  [ -d "$sc_boot_state" ] && [ ! -L "$sc_boot_state" ] ||
    sc_bootstrap_refuse "missing or unsafe .sc-state directory"
  [ -f "$sc_boot_ref_file" ] && [ ! -L "$sc_boot_ref_file" ] ||
    sc_bootstrap_refuse "missing or unsafe .sc-state/engine.ref"
  [ -f "$sc_boot_source_file" ] && [ ! -L "$sc_boot_source_file" ] ||
    sc_bootstrap_refuse "missing or unsafe .sc-state/engine.source"

  IFS= read -r sc_boot_ref < "$sc_boot_ref_file" ||
    sc_bootstrap_refuse "engine.ref must contain one newline-terminated SHA"
  case "$sc_boot_ref" in
    *[!0-9a-f]*|'') sc_bootstrap_refuse "engine.ref is not a lowercase SHA-1" ;;
  esac
  [ "${#sc_boot_ref}" -eq 40 ] ||
    sc_bootstrap_refuse "engine.ref is not a 40-character SHA-1"
  [ "$(awk 'END { print NR }' "$sc_boot_ref_file")" -eq 1 ] ||
    sc_bootstrap_refuse "engine.ref must contain exactly one line"

  IFS= read -r sc_boot_source < "$sc_boot_source_file" ||
    sc_bootstrap_refuse "engine.source must contain one newline-terminated locator"
  [ "$(awk 'END { print NR }' "$sc_boot_source_file")" -eq 1 ] ||
    sc_bootstrap_refuse "engine.source must contain exactly one line"
  case "$sc_boot_source" in
    *' '*|*'	'*) sc_bootstrap_refuse "engine.source contains whitespace" ;;
    https://*|ssh://*|git://*|file://*|git@?*:?*) : ;;
    *) sc_bootstrap_refuse "engine.source is not a supported absolute Git locator" ;;
  esac

  for sc_boot_tool in git tar awk cmp mktemp; do
    command -v "$sc_boot_tool" >/dev/null 2>&1 ||
      sc_bootstrap_refuse "$sc_boot_tool is unavailable"
  done
  mkdir -p "$sc_boot_state/local" ||
    sc_bootstrap_refuse "cannot create the local bootstrap staging directory"
  [ ! -L "$sc_boot_state/local" ] ||
    sc_bootstrap_refuse ".sc-state/local is an unsafe symlink"
  sc_boot_candidate=$(mktemp -d "$sc_boot_state/local/engine-bootstrap.XXXXXX") ||
    sc_bootstrap_refuse "cannot create a private bootstrap candidate"
  sc_boot_cleanup() {
    rm -rf -- "$sc_boot_candidate"
  }
  trap sc_boot_cleanup 0 1 2 15

  git -C "$CALLER_ROOT" fetch --no-tags "$sc_boot_source" "$sc_boot_ref" ||
    sc_bootstrap_refuse "the declared source could not fetch engine.ref"
  sc_boot_fetched=$(git -C "$CALLER_ROOT" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)
  [ "$sc_boot_fetched" = "$sc_boot_ref" ] ||
    sc_bootstrap_refuse "the fetched commit does not equal engine.ref"

  git -C "$CALLER_ROOT" cat-file blob "$sc_boot_ref:sc" > "$sc_boot_candidate/sc" ||
    sc_bootstrap_refuse "engine.ref does not contain the stable launcher"
  cmp -s "$CALLER_ROOT/sc" "$sc_boot_candidate/sc" ||
    sc_bootstrap_refuse "the tracked launcher does not match engine.ref"
  git -C "$CALLER_ROOT" archive --format=tar \
    --output="$sc_boot_candidate/engine.tar" "$sc_boot_ref" .super-coder ||
    sc_bootstrap_refuse "engine.ref does not contain a materializable engine"
  tar -xf "$sc_boot_candidate/engine.tar" -C "$sc_boot_candidate" ||
    sc_bootstrap_refuse "the engine archive could not be extracted"
  rm -f -- "$sc_boot_candidate/engine.tar" "$sc_boot_candidate/sc"

  [ -r "$sc_boot_candidate/.super-coder/scripts/dispatch.sh" ] &&
    [ -r "$sc_boot_candidate/.super-coder/scripts/install.py" ] &&
    [ -r "$sc_boot_candidate/.super-coder/schema.sql" ] ||
    sc_bootstrap_refuse "the staged engine is incomplete"
  [ ! -e "$CALLER_ROOT/.super-coder" ] && [ ! -L "$CALLER_ROOT/.super-coder" ] ||
    sc_bootstrap_refuse "a partial engine target appeared during staging"
  mv "$sc_boot_candidate/.super-coder" "$CALLER_ROOT/.super-coder" ||
    sc_bootstrap_refuse "the complete engine could not be published"
  echo "→ materialized engine ${sc_boot_ref%????????????????????????????} from tracked provenance"
  sc_boot_cleanup
  trap - 0 1 2 15
  DISPATCH="$CALLER_ROOT/.super-coder/scripts/dispatch.sh"
}

if [ ! -f "$DISPATCH" ] && [ ! -e "$LIVE_ROOT/.super-coder" ] &&
   [ "$sc_boot_linked" -eq 0 ] && [ -z "${SC_DISPATCH:-}" ] &&
   [ "${1:-}" = install ]; then
  sc_bootstrap_engine
fi
if [ ! -f "$DISPATCH" ]; then
  {
    if [ ! -d "$LIVE_ROOT/.super-coder" ]; then
      echo "✗ ./sc: no engine found."
      echo "    caller root : $CALLER_ROOT"
      echo "    live root   : $LIVE_ROOT"
      if [ "$sc_boot_linked" -eq 1 ]; then
        echo "  A linked worktree cannot own the initial engine materialization."
        echo "  Run ./sc install from the primary checkout first."
      else
        echo "  Neither holds .super-coder/. Run from a super-coder install, or"
        echo "  install one first (see README)."
      fi
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
