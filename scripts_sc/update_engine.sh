#!/bin/sh
# Update this installation's materialized engine from the sibling subfloor clone.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SUBFLOOR="${SC_SUBFLOOR_DIR:-$HOME/Repos/subfloor}"
REMOTE=sc-engine-local
PIN="$ROOT/.sc-state/engine.ref"
PY="${SC_PYTHON:-python3}"

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
sh "$ROOT/scripts_sc/host_sc.sh" down
if [ "$CONVERTED" -eq 1 ]; then
  # The old source-shaped floor intentionally differs from the first
  # materialized pin. Only this one-time boundary crossing is forced.
  "$PY" "$ROOT/scripts_sc/installed_update.py" --ref "$TARGET" --force "$@"
else
  "$PY" "$ROOT/scripts_sc/installed_update.py" --ref "$TARGET" "$@"
fi

# Never turn a green engine no-op into a false host-level success. A failed or
# mismatched update leaves the runtime stopped for diagnosis.
ACTUAL="$(sed -n '1p' "$PIN" 2>/dev/null || true)"
[ "$ACTUAL" = "$TARGET" ] || {
  echo "update_engine: engine pin mismatch after update" >&2
  echo "  expected: $TARGET" >&2
  echo "  actual:   ${ACTUAL:-<missing>}" >&2
  exit 1
}

# The engine updater reconciles the DB and generated surfaces. Restart the
# install-owned host server only after the exact target pin is published.
sh "$ROOT/scripts_sc/host_sc.sh" restart
echo "✓ sc-cachy engine aligned with subfloor origin/main at ${TARGET}"
