#!/usr/bin/env bash
# Demo: boot the service, start one bash generation, print attach instructions.
set -eu
cd "$(dirname "$0")"
PY=/home/j3d1/super-coder/.sc-worktrees/dev3/.venv/bin/python
PORT="${1:-18777}"
SPIKE_TOKEN=spike "$PY" server.py "$PORT" &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1.5
SID=$(curl -s -X POST "http://127.0.0.1:$PORT/api/interface/sessions" \
  -H 'Authorization: Bearer spike' -H 'Content-Type: application/json' \
  -d "{\"harness\":\"bash\",\"worktree\":\"$PWD\"}" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
echo "session: $SID"
echo "browser:  http://127.0.0.1:$PORT/  (token=spike session=$SID — click 'take lease', then 'connect')"
echo "cli:      $PY cli_client.py $SID --role writer --server http://127.0.0.1:$PORT"
wait $SRV
