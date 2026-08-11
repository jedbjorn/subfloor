#!/bin/sh
# super-coder entry point — a stable bootstrap, not the dispatcher (spec #105,
# single-owner dispatcher). The dispatcher body is engine-owned and
# materialized by `./sc update` (.super-coder/scripts/dispatch.sh), so every
# checkout of an install — the main checkout or any linked worktree, on any
# branch age — dispatches the LIVE engine floor. This file only resolves WHERE
# that floor is during normal dispatch: it carries no verbs or help text and
# never writes. The sole exception is the real-host preflight for SC_DISPATCH,
# because that maintainer override replaces the body that normally owns the
# gate. A stale committed copy remains harmless for ordinary dispatch. Run from
# the repo root:  ./sc <command> [args]
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

# SC_DISPATCH is the one path that can replace the guarded live body. Gate that
# maintainer path here, before selecting it, so an edited dispatcher cannot
# bypass the real-host boundary it is supposed to preserve. These probes stay
# literal so focused tests can patch only a disposable launcher copy.
sc_bootstrap_platform_unquote() {
  _platform_value="$1"
  case "$_platform_value" in
    \"*\") _platform_value=${_platform_value#\"}; _platform_value=${_platform_value%\"} ;;
    \'*\') _platform_value=${_platform_value#\'}; _platform_value=${_platform_value%\'} ;;
    \"*|\'*|*\"|*\') return 1 ;;
  esac
  printf '%s\n' "$_platform_value"
}

sc_bootstrap_require_supported_host() {
  SC_BOOTSTRAP_PLATFORM_KERNEL="$(command -p uname -s 2>/dev/null || true)"
  _platform_release=/etc/os-release
  _platform_wsl_release=/proc/sys/kernel/osrelease
  SC_BOOTSTRAP_PLATFORM_ID=""
  SC_BOOTSTRAP_PLATFORM_ID_LIKE=""
  SC_BOOTSTRAP_PLATFORM_VERSION_ID=""
  _platform_invalid=0
  _platform_wsl=0
  _platform_wsl_kernel="$(command -p cat "$_platform_wsl_release" 2>/dev/null || true)"
  case "$_platform_wsl_kernel" in
    *[Mm]icrosoft*|*[Ww][Ss][Ll]*) _platform_wsl=1 ;;
  esac
  if [ -n "${WSL_DISTRO_NAME:-}" ] || [ -n "${WSL_INTEROP:-}" ]; then
    _platform_wsl=1
  fi
  if [ -r "$_platform_release" ] && command -p iconv -f UTF-8 -t UTF-8 "$_platform_release" >/dev/null 2>&1; then
    if LC_ALL=C command -p od -An -v -t x1 "$_platform_release" | command -p grep -Eq '(^|[[:space:]])00([[:space:]]|$)'; then
      _platform_invalid=1
    fi
    while IFS='=' read -r _platform_key _platform_value || [ -n "$_platform_key" ]; do
      [ "$_platform_invalid" = 1 ] && break
      case "$_platform_key" in
        ID) SC_BOOTSTRAP_PLATFORM_ID="$(sc_bootstrap_platform_unquote "$_platform_value")" || _platform_invalid=1 ;;
        ID_LIKE) SC_BOOTSTRAP_PLATFORM_ID_LIKE="$(sc_bootstrap_platform_unquote "$_platform_value")" || _platform_invalid=1 ;;
        VERSION_ID) SC_BOOTSTRAP_PLATFORM_VERSION_ID="$(sc_bootstrap_platform_unquote "$_platform_value")" || _platform_invalid=1 ;;
      esac
    done < "$_platform_release"
  fi
  if [ "$_platform_invalid" = 1 ]; then
    SC_BOOTSTRAP_PLATFORM_ID=""
    SC_BOOTSTRAP_PLATFORM_ID_LIKE=""
    SC_BOOTSTRAP_PLATFORM_VERSION_ID=""
  fi
  if [ "$_platform_wsl" != 1 ]; then
    case "$SC_BOOTSTRAP_PLATFORM_KERNEL:$SC_BOOTSTRAP_PLATFORM_ID:$SC_BOOTSTRAP_PLATFORM_VERSION_ID" in
      Linux:ubuntu:26.04|Linux:fedora:44|Linux:arch:*) return 0 ;;
    esac
    case " $SC_BOOTSTRAP_PLATFORM_ID_LIKE " in
      *" arch ") [ "$SC_BOOTSTRAP_PLATFORM_KERNEL" = Linux ] && return 0 ;;
    esac
  fi
  {
    echo '✗ subfloor refused: unsupported host.'
    echo "  detected kernel: ${SC_BOOTSTRAP_PLATFORM_KERNEL:-unknown}"
    echo "  detected distribution: ID=${SC_BOOTSTRAP_PLATFORM_ID:-unknown}; ID_LIKE=${SC_BOOTSTRAP_PLATFORM_ID_LIKE:-unknown}; VERSION_ID=${SC_BOOTSTRAP_PLATFORM_VERSION_ID:-unknown}"
    echo '  supported hosts: Ubuntu LTS, Fedora stable, Arch-compatible Linux.'
    echo '  Create a supported Linux VM, keep the checkout on the guest filesystem, then run ./sc install inside the guest.'
    echo '  The rejected command was not run and no native compatibility path exists.'
  } >&2
  exit 1
}

# Maintainer override (canonical source repo only): exec that checkout's tracked
# dispatcher body instead of the live floor's so a maintainer can test an edit
# before repinning. No arbitrary readable body is an override candidate.
if [ -n "${SC_DISPATCH:-}" ]; then
  sc_bootstrap_require_supported_host
  _maintainer_dispatch="$CALLER_ROOT/.super-coder/scripts/dispatch.sh"
  _maintainer_origin="$(command -p git -C "$CALLER_ROOT" remote get-url origin 2>/dev/null || true)"
  _maintainer_origin=${_maintainer_origin%/}
  _maintainer_repo=${_maintainer_origin##*/}
  _maintainer_repo=${_maintainer_repo##*:}
  _maintainer_repo=${_maintainer_repo%.git}
  _canonical_source=0
  case "$_maintainer_repo" in super-coder|subfloor) _canonical_source=1 ;; esac
  if [ "$SC_DISPATCH" != "$_maintainer_dispatch" ] \
    || [ "$_canonical_source" != 1 ] \
    || ! command -p git -C "$CALLER_ROOT" ls-files --error-unmatch -- .super-coder/scripts/dispatch.sh >/dev/null 2>&1; then
    echo "✗ ./sc: SC_DISPATCH is restricted to the canonical source checkout's tracked dispatcher: $_maintainer_dispatch" >&2
    exit 1
  fi
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
