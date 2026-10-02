"""Session-owned observation hook and conservative native schedule guard.

Native command-hook failures can fail open. The driver also installs a native
permission deny and keeps scheduling unavailable until both paths are proved.
No raw hook record or transcript is persisted by this asset.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from asset_client import MAX_FRAME_BYTES, call


def denied(event: dict[str, Any]) -> bool:
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "CronCreate":
        return False
    args = event.get("tool_input")
    # Explicit false avoids depending on a changing native omitted default.
    return (os.environ.get("SC_F89_SCHEDULING") != "1" or not isinstance(args, dict)
            or args.get("durable") is not False)


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_FRAME_BYTES + 1)
        event = json.loads(data) if len(data) <= MAX_FRAME_BYTES else None
        if not isinstance(event, dict):
            raise TypeError("invalid event")
        if denied(event):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "deny", "permissionDecisionReason":
                "Experimental chat schedules require proven session-only support and explicit durable:false."}}))
            return 0
        call({"kind": "hook", "event": event})
        return 0
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        # Do not expose event/input/environment in native stderr. exit 2 is
        # native blocking behavior for PreToolUse, not a fail-open exit 1.
        if "--event" in sys.argv and sys.argv[-1] == "PreToolUse":
            print("Experimental runtime guard unavailable; native tool was blocked.", file=sys.stderr)
            return 2
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
