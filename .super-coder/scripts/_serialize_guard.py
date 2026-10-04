"""Guard: serializing the shared instance is an Admin step.

`snapshot.py` and `render.py flat` rewrite the shared instance's ignored local
snapshot and renders. A shell's `sc mem` write is already live in the shared
engine DB and visible to every shell, so re-serializing from a shell is never
needed; it only dirties the shared tree and can collide with another
serialization in flight.

The caller is resolved from its bearer token by `engine_identity` — the same
check engine SQL uses — never from a self-declared variable. Admin, the host
operator's seat and engine-internal callers (the API's Save locally, install,
update, verify) pass; any other launched shell gets one refusal naming that
confusion.
"""
from __future__ import annotations

import engine_identity

CONFUSION = (
    "shared instance serialization is an Admin step: your `sc mem` write is "
    "already live in the engine DB, and re-serializing from a shell dirties "
    "the shared tree and can collide with another serialization."
)


def require_admin(op: str) -> None:
    """Exit with the confusion-first refusal unless the caller may serialize."""
    engine_identity.require_maintainer(op, CONFUSION)
