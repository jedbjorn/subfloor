#!/usr/bin/env bash
# super-coder telemetry hook — claude SessionEnd → real-time token capture.
#
# Wired by the claude adapter's merge_json (same seam as the branch-guard's
# PreToolUse entry): when a claude session ends, claude invokes this with the
# SessionEnd JSON on stdin; we POST the transcript path to the engine API's
# token-scoped ingest (`POST /_sc/mem/telemetry`), which validates the ref
# resolves under claude's data dir and runs the claude parser inline. The
# boot-time `sc analytics sweep` remains the backstop for missed hooks.
#
# Best-effort BY CONTRACT: a session must always be able to end — every
# failure path (no API env, no python3, curl timeout, server down) exits 0
# silently. SC_API_BASE + SC_API_TOKEN arrive from run.py's exec env; a
# harness launched outside run.py has neither and exits at the first check
# (its sessions are swept later, shown unattributed — deliberate).

[ -n "$SC_API_BASE" ] && [ -n "$SC_API_TOKEN" ] || exit 0

payload=$(python3 -c '
import json, sys
d = json.load(sys.stdin)
ref = d.get("transcript_path") or ""
if not ref:
    sys.exit(1)
print(json.dumps({"harness": "claude", "harness_session_ref": ref}))
' 2>/dev/null) || exit 0

curl -s -m 5 -X POST "$SC_API_BASE/_sc/mem/telemetry" \
     -H "Authorization: Bearer $SC_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d "$payload" >/dev/null 2>&1 || true
exit 0
