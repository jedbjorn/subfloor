"""Guard: serializing shared instance state is an admin/GUI operation.

`snapshot.py` and `render.py flat` write ignored local artifacts for the shared
instance. A shell's `./sc mem` write
is already live and visible to all shells through the shared engine DB, so
per-write serialization is never needed from a shell and can collide with
another serialization.

Serialization is therefore gated to admin surfaces — the GUI/API, install, update,
and render-check — which set `SC_ADMIN=1` on the subprocess. A shell running
`./sc snapshot` / `./sc render flat` directly gets one clear refusal instead of
silently dirtying the shared tree.
"""
from __future__ import annotations

import os
import sys


def is_admin() -> bool:
    return os.environ.get("SC_ADMIN") == "1"


def require_admin(op: str) -> None:
    """Exit with a clear message unless SC_ADMIN=1 is set."""
    if is_admin():
        return
    sys.exit(
        f"{op}: refused — serializing shared instance state is an admin/GUI step.\n"
        "  Your write is already live in the engine DB and shared with every shell.\n"
        "  To refresh the ignored local snapshot, use Save locally, or as admin:\n"
        "    SC_ADMIN=1 ./sc snapshot && SC_ADMIN=1 ./sc render flat"
    )
