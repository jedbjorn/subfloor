#!/usr/bin/env python3
"""Browser-native conversation adapter registry."""
from __future__ import annotations

from typing import Any

from .base import (
    AdapterCapabilities,
    AdapterError,
    ConversationAdapter,
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ProbeResult,
    ReconcileResult,
    SessionInspection,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter, JsonLineRpcProcess, RpcTransport
from .deepseek import DeepSeekAdapter, DeepSeekCarrierProcess, DeepSeekTransport
from .kimi import KimiAdapter
from .opencode import OpenCodeAdapter


ADAPTER_TYPES = {
    "opencode": OpenCodeAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "kimi": KimiAdapter,
}


def adapter_for(harness: str, **kwargs: Any) -> ConversationAdapter:
    try:
        adapter_type = ADAPTER_TYPES[harness]
    except KeyError as exc:
        raise AdapterError(
            "HARNESS_CONVERSATION_UNSUPPORTED",
            f"no conversation adapter for harness: {harness}",
        ) from exc
    return adapter_type(**kwargs)


__all__ = [
    "ADAPTER_TYPES",
    "AdapterCapabilities",
    "AdapterError",
    "ClaudeAdapter",
    "CodexAdapter",
    "ConversationAdapter",
    "ConversationContext",
    "DeepSeekAdapter",
    "DeepSeekCarrierProcess",
    "DeepSeekTransport",
    "InterruptResult",
    "JsonLineRpcProcess",
    "KimiAdapter",
    "NativeTurn",
    "NormalizedEvent",
    "OpenCodeAdapter",
    "ProbeResult",
    "ReconcileResult",
    "RpcTransport",
    "SessionInspection",
    "adapter_for",
]
