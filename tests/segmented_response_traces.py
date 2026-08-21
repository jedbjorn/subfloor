"""Executable event traces for segmented assistant response contracts."""

HISTORICAL_SEGMENT_TRACES = (
    {
        "name": "plain_prose",
        "events": (
            ("assistant.delta", {"text": "plain "}),
            ("assistant.delta", {"text": "prose"}),
        ),
        "expected": (
            ("assistant", 0, 3, 4, "plain prose"),
        ),
    },
    {
        "name": "prose_tool_prose",
        "events": (
            ("assistant.delta", {"text": "plan"}),
            ("tool.started", {"name": "write"}),
            ("tool.completed", {"name": "write"}),
            ("assistant.delta", {"text": "result "}),
            ("assistant.delta", {"text": "done"}),
        ),
        "expected": (
            ("assistant", 0, 3, 3, "plan"),
            ("assistant", 5, 6, 7, "result done"),
        ),
    },
    {
        "name": "multiple_tools",
        "events": (
            ("assistant.delta", {"text": "before"}),
            ("tool.started", {"name": "read"}),
            ("tool.completed", {"name": "read"}),
            ("tool.started", {"name": "write"}),
            ("tool.completed", {"name": "write"}),
            ("assistant.delta", {"text": "after"}),
        ),
        "expected": (
            ("assistant", 0, 3, 3, "before"),
            ("assistant", 7, 8, 8, "after"),
        ),
    },
    {
        "name": "tool_before_prose",
        "events": (
            ("tool.started", {"name": "read"}),
            ("tool.completed", {"name": "read"}),
            ("assistant.delta", {"text": "first visible text"}),
        ),
        "expected": (
            ("assistant", 4, 5, 5, "first visible text"),
        ),
    },
    {
        "name": "permission_and_input_pauses",
        "events": (
            ("assistant.delta", {"text": "explain"}),
            ("permission.requested", {"detail": "approve write"}),
            ("assistant.delta", {"text": "approved"}),
            ("input.requested", {"detail": "choose target"}),
            ("assistant.delta", {"text": "continued"}),
        ),
        "expected": (
            ("assistant", 0, 3, 3, "explain"),
            ("activity", 4, 4, 4, "permission.requested"),
            ("assistant", 4, 5, 5, "approved"),
            ("activity", 6, 6, 6, "input.requested"),
            ("assistant", 6, 7, 7, "continued"),
        ),
    },
)


PENDING_BOUNDARY_TRACE = (
    ("assistant.delta", {"text": "before tool"}),
    ("tool.started", {"name": "write"}),
    ("tool.completed", {"name": "write"}),
)


LIVE_SEGMENT_EVENTS = (
    {
        "sequence": 6,
        "event_type": "tool.started",
        "message_id": 1,
        "run_id": 77,
        "created_at": "2026-07-30 20:01:00",
        "payload": {"name": "write"},
    },
    {
        "sequence": 7,
        "event_type": "tool.completed",
        "message_id": 1,
        "run_id": 77,
        "created_at": "2026-07-30 20:01:01",
        "payload": {"name": "write"},
    },
    {
        "sequence": 8,
        "event_type": "assistant.delta",
        "message_id": 1,
        "run_id": 77,
        "created_at": "2026-07-30 20:01:02",
        "payload": {"text": "after tool"},
    },
)


def version_three_snapshot() -> dict:
    """Return a fresh snapshot paused after an initial assistant segment."""
    return {
        "conversation_id": "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "projection_version": 3,
        "through_sequence": 5,
        "assistant_cursor": {
            "run_id": 77,
            "segment_anchor_sequence": 0,
        },
        "controls": {
            "conversation_version": 4,
            "conversation_state": "running",
            "queued_count": 0,
            "active_run_id": 77,
            "close_requested_at": None,
        },
        "items": [
            {
                "item_id": "message:1",
                "kind": "user",
                "order_sequence": 1,
                "message_id": 1,
                "run_id": None,
                "created_at": "2026-07-30 20:00:00",
                "text": "use a tool",
                "state": "running",
                "completed_at": None,
                "text_truncated": False,
            },
            {
                "item_id": "run:77:assistant:0",
                "kind": "assistant",
                "order_sequence": 3,
                "message_id": 1,
                "run_id": 77,
                "created_at": "2026-07-30 20:00:02",
                "text": "before tool",
                "outcome": None,
                "segment": "answer",
                "segment_anchor_sequence": 0,
                "first_sequence": 3,
                "last_sequence": 3,
                "text_truncated": False,
            },
        ],
        "truncation": None,
    }
