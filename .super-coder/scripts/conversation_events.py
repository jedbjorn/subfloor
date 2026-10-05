"""Process-local wake hints for durable conversation event replay.

The database is the source of truth.  This condition only prevents a live SSE
consumer from polling while it waits for the next committed sequence.  A
generation snapshot taken before the replay query closes the query/wait race:
if a commit lands between those operations, ``wait`` returns immediately.
"""

from __future__ import annotations

import threading
import time

from conversation_adapters.base import NORMALIZED_EVENTS

# Every conversation_events writer validates against this vocabulary. The UI
# listener contract is bound to it so adding a type requires a listener.
CONVERSATION_EVENT_TYPES = NORMALIZED_EVENTS | frozenset(
    {
        "conversation.created",
        "conversation.updated",
        "conversation.renamed",
        "conversation.close.requested",
        "conversation.closed",
        "conversation.reopened",
        "message.accepted",
        "run.resumed",
        "run.interrupt.requested",
        "run.unknown",
        "run.deferred",
        "run.reaped",
        "run.process.snapshot",
        "run.process.ended",
    }
)


def require_event_type(event_type: str) -> str:
    """Reject unlisted types before they can enter durable event replay."""
    if event_type not in CONVERSATION_EVENT_TYPES:
        raise ValueError(f"unsupported conversation event type: {event_type}")
    return event_type


_CONDITION = threading.Condition()
_GENERATIONS: dict[str, int] = {}


def generation(conversation_id: str) -> int:
    with _CONDITION:
        return _GENERATIONS.get(conversation_id, 0)


def notify(conversation_id: str) -> int:
    with _CONDITION:
        value = _GENERATIONS.get(conversation_id, 0) + 1
        _GENERATIONS[conversation_id] = value
        _CONDITION.notify_all()
        return value


def wait(conversation_id: str, after: int, timeout: float) -> int:
    deadline = time.monotonic() + max(0.0, timeout)
    with _CONDITION:
        while _GENERATIONS.get(conversation_id, 0) == after:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _CONDITION.wait(remaining)
        return _GENERATIONS.get(conversation_id, 0)
