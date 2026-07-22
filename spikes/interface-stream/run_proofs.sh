#!/usr/bin/env bash
# Run the full interface-stream proof matrix, verbose, tee a summary.
set -u
cd "$(dirname "$0")"
PY=/home/j3d1/super-coder/.sc-worktrees/dev3/.venv/bin/python
OUT="proofs-$(date +%Y%m%d-%H%M%S).log"
echo "interface-stream proof matrix -> $OUT"
"$PY" -m pytest tests/ -v -s -p no:cacheprovider 2>&1 | tee "$OUT"
rc=${PIPESTATUS[0]}
echo "----"
grep -E "^evidence" "$OUT" || true
echo "----"
grep -E "passed|failed|xfailed" "$OUT" | tail -3
echo "exit=$rc log=$OUT"
exit $rc
