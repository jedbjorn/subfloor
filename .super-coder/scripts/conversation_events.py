"""Process-local wake hints for durable conversation event replay.

The database is the source of truth.  This condition only prevents a live SSE
consumer from polling while it waits for the next committed sequence.  A
generation snapshot taken before the replay query closes the query/wait race:
if a commit lands between those operations, ``wait`` returns immediately.
"""

from __future__ import annotations

import threading
import time

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
