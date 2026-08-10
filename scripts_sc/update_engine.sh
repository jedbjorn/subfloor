#!/bin/sh
# Update this installation's materialized engine from the sibling subfloor clone.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SUBFLOOR="${SC_SUBFLOOR_DIR:-$HOME/Repos/subfloor}"
REMOTE=sc-engine-local

# The upstream make recipe invokes "$SC update ...".
[ "${1:-}" = update ] && shift

case " $* " in
  *" --no-fetch "*|*" --branch "*|*" --ref "*)
    echo "update_engine: --no-fetch/--branch/--ref are owned by this pinned update path" >&2
    exit 2
    ;;
esac

[ -d "$SUBFLOOR/.git" ] || {
  echo "update_engine: sibling subfloor clone not found at $SUBFLOOR" >&2
  echo "  override with SC_SUBFLOOR_DIR=/absolute/path" >&2
  exit 1
}
[ -z "$(git -C "$ROOT" ls-files -u)" ] || {
  echo "update_engine: unresolved Git merge; resolve or abort it before updating" >&2
  exit 1
}

echo "→ refreshing sibling subfloor origin/main"
git -C "$SUBFLOOR" fetch origin main
TARGET="$(git -C "$SUBFLOOR" rev-parse --verify origin/main)"

# This remote is updater plumbing only. It fetches engine objects from the
# already-refreshed sibling without merging either repository.
if git -C "$ROOT" remote get-url "$REMOTE" >/dev/null 2>&1; then
  git -C "$ROOT" remote set-url "$REMOTE" "$SUBFLOOR"
else
  git -C "$ROOT" remote add "$REMOTE" "$SUBFLOOR"
fi

CONVERTED=0
if git -C "$ROOT" ls-files --error-unmatch .super-coder/schema.sql >/dev/null 2>&1; then
  echo "→ one-time conversion: untracking engine source (files remain in place)"
  git -C "$ROOT" rm -r --cached --quiet .super-coder
  CONVERTED=1
fi

echo "→ updating materialized engine to ${TARGET}"
if [ "$CONVERTED" -eq 1 ]; then
  # The old source-shaped floor intentionally differs from the first
  # materialized pin. Only this one-time boundary crossing is forced.
  "$ROOT/sc" update --ref "$TARGET" --force "$@"
else
  "$ROOT/sc" update --ref "$TARGET" "$@"
fi

# The standard updater reconciles the DB and generated surfaces. Restart the
# install-owned host server afterward so it executes the newly pinned code.
sh "$ROOT/scripts_sc/host_sc.sh" restart
echo "✓ sc-cachy engine aligned with subfloor origin/main at ${TARGET}"
