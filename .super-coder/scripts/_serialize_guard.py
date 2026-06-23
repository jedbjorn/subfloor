"""Guard: serializing to the shared main tree is an admin/GUI operation.

`snapshot.py` and `render.py flat` write the git-tracked `.sc-state/` snapshot +
`_sc` mirror into the MAIN worktree root (`REPO_ROOT = ENGINE.parent`) — the tree
shared by every shell's linked `.sc-worktrees/<name>/`. A shell's `./sc mem` write
is already live and visible to all shells through the shared engine DB, so
per-write serialization is never needed from a shell: it only churns and dirties
main, and collides with whatever other shells have checked out there.

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
        f"{op}: refused — serializing to the shared main tree is an admin/GUI step.\n"
        "  Your write is already live in the engine DB and shared with every shell.\n"
        "  To persist it to git, use the GUI Snapshot button, or as admin:\n"
        "    SC_ADMIN=1 ./sc snapshot && SC_ADMIN=1 ./sc render flat"
    )
