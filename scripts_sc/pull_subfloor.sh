#!/bin/sh
# sc-cachy bare-metal layer — deterministic upstream pull (step 1 of `make dos-u`).
#
# This install is a full-history fork of subfloor ITSELF (branch
# feat/bare-metal-super-coder), deliberately remote-less: upstream arrives by
# local-path fetch from the subfloor sibling clone. This script encodes the
# proven update procedure so `make dos-u` is one determinate chain:
#
#   make dos-u   →  backup DB → fast-forward sibling subfloor main from GitHub
#                   → fetch + merge into this repo → ./sc update
#                   (the merge runs here; ./sc update is the make recipe)
#   make dos-r   →  ./sc restart (its own DB backup → down → launch)
#
# On merge conflict the merge is left in progress and this exits non-zero, so
# make stops BEFORE ./sc update. Resolve preserving the bare-metal layer, then
# rerun `make dos-u` (the pull becomes a no-op) or run `./sc update` directly.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SUBFLOOR="${SC_SUBFLOOR_DIR:-$HOME/Repos/subfloor}"
DB="$ROOT/.super-coder/shell_db.db"
BACKUP_DIR="$ROOT/db_backups/sc-cachy"

[ -d "$SUBFLOOR/.git" ] || {
  echo "✗ subfloor sibling not found at $SUBFLOOR (override: SC_SUBFLOOR_DIR)" >&2
  exit 1
}

if [ -n "$(git -C "$ROOT" ls-files -u)" ]; then
  echo "✗ a merge is already in progress — resolve it first (see guidance below)" >&2
  git -C "$ROOT" diff --name-only --diff-filter=U | sed 's/^/    /' >&2
  exit 1
fi
if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "✗ working tree has uncommitted changes — commit or stash before pulling upstream" >&2
  exit 1
fi

# 1. DB backup (the update that follows applies migrations).
ts="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp "$DB" "$BACKUP_DIR/shell_db.pre-pull.$ts.db"
echo "→ DB backed up -> db_backups/sc-cachy/shell_db.pre-pull.$ts.db"

# 2. Fast-forward the sibling's main from GitHub (ff-only: a diverged sibling
#    main is an error to look at, never something to silently rewrite).
echo "→ fast-forwarding $SUBFLOOR main from origin"
git -C "$SUBFLOOR" fetch origin main:main || {
  echo "✗ could not fast-forward subfloor main (diverged or offline)." >&2
  echo "  Inspect: git -C $SUBFLOOR log --oneline main..origin/main" >&2
  exit 1
}

# 3. Fetch + merge into this repo.
git -C "$ROOT" fetch "$SUBFLOOR" main
if git -C "$ROOT" merge-base --is-ancestor FETCH_HEAD HEAD; then
  echo "→ already up to date with subfloor main ($(git -C "$ROOT" rev-parse --short FETCH_HEAD))"
  exit 0
fi
echo "→ merging subfloor main ($(git -C "$ROOT" rev-parse --short FETCH_HEAD))"
if ! SC_HOME_MAINTENANCE=1 git -C "$ROOT" merge --no-edit FETCH_HEAD; then
  cat >&2 <<'EOF'

✗ merge conflict — resolve preserving the BARE-METAL LAYER, then rerun
  `make dos-u` (or `./sc update` directly). The known hot spots:

  sc                                  take THEIRS (thin bootstrap since #1009);
                                      the dispatcher body is engine-owned
  .super-coder/scripts/dispatch.sh    keep the bare-metal specialization:
                                      host-native launch/enter/down/restart/logs,
                                      sandbox-* compatibility verbs,
                                      SC_BACKUP_DIR bridge, doctor --check-host
  .super-coder/scripts/update.py      keep is_source_repo() delegating to
                                      install.is_source_repo() (remote-less
                                      source-repo detection)
  .super-coder/api/server.py          keep SC_HOME_MAINTENANCE publish commit +
                                      local-only "no origin remote" success path

  Commit the resolution with SC_HOME_MAINTENANCE=1 git commit.
EOF
  exit 1
fi
echo "→ merge committed: $(git -C "$ROOT" log --oneline -1)"
