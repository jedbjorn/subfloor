#!/usr/bin/env python3
"""Public one-shot execution through the stock DeepSeek Host API."""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Mapping

import deepseek_host


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sc run --harness deepseek")
    parser.add_argument("--selector", required=True)
    parser.add_argument("--effort", default="default")
    parser.add_argument("prompt")
    return parser


def run(selector: str, effort: str, prompt: str) -> int:
    if not prompt.strip():
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_MESSAGE_INVALID", "one-shot prompt must contain text"
        )
    worktree = Path(os.environ.get("SC_SHELL_WORKTREE", os.getcwd())).resolve()
    if not worktree.is_dir():
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_WORKTREE_MISSING", "one-shot worktree is unavailable"
        )
    client = deepseek_host.DeepSeekHostClient()
    route = deepseek_host.route_for(client, selector)
    if effort != "default" and effort not in route.reasoning_efforts:
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_ROUTE_INVALID",
            f"reasoning effort is unavailable for the exact route: {effort}",
        )
    session_ref = f"sc-{uuid.uuid4().hex}"
    created = client.call(
        "session.create", {"sessionId": session_ref, "cwd": str(worktree)}
    )
    if not isinstance(created, Mapping) or created.get("sessionId") != session_ref:
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_SESSION_MISMATCH",
            "DeepSeek Host did not preserve the one-shot session identity",
        )
    selected = {
        "provider": route.provider,
        "model": route.model,
        **({} if effort == "default" else {"reasoningEffort": effort}),
    }
    selected_result = client.call(
        "session.selectModel", {"sessionId": session_ref, **selected}
    )
    if (
        not isinstance(selected_result, Mapping)
        or selected_result.get("selected") != selected
    ):
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_ROUTE_MISMATCH",
            "DeepSeek Host did not select the exact one-shot route",
        )
    stream = client.open_events()
    wrote = False
    try:
        accepted = client.call(
            "session.prompt",
            {
                "sessionId": session_ref,
                "mode": "queue",
                "content": [{"type": "text", "text": prompt}],
            },
        )
        if not isinstance(accepted, Mapping) or accepted.get("accepted") is not True:
            raise deepseek_host.DeepSeekHostError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host did not accept the prompt"
            )
        for envelope in stream:
            payload = envelope.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if payload.get("sessionId") != session_ref:
                continue
            if payload.get("type") in {"approval/requested", "question/requested"}:
                client.call("session.cancel", {"sessionId": session_ref})
                raise deepseek_host.DeepSeekHostError(
                    "HARNESS_APPROVAL_UNSUPPORTED",
                    "one-shot execution cannot answer interactive requests",
                )
            if payload.get("type") != "session/event":
                continue
            event = payload.get("event")
            if not isinstance(event, Mapping):
                continue
            data = event.get("data")
            if event.get("type") == "assistant/chunk" and isinstance(data, Mapping):
                chunk = data.get("chunk")
                if (
                    isinstance(chunk, Mapping)
                    and chunk.get("type") == "text-delta"
                    and isinstance(chunk.get("text"), str)
                ):
                    sys.stdout.write(chunk["text"])
                    sys.stdout.flush()
                    wrote = True
            if event.get("type") == "turn/end" and isinstance(data, Mapping):
                reason = data.get("reason")
                kind = reason.get("kind") if isinstance(reason, Mapping) else None
                if wrote:
                    sys.stdout.write("\n")
                if kind == "completed" and wrote:
                    return 0
                raise deepseek_host.DeepSeekHostError(
                    "HARNESS_NATIVE_RUN_FAILED",
                    f"DeepSeek one-shot ended without an ordinary response: {kind or 'unknown'}",
                )
    finally:
        stream.close()
    raise deepseek_host.DeepSeekHostError(
        "HARNESS_RECONCILIATION_UNKNOWN",
        "DeepSeek one-shot event stream ended without terminal evidence",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args.selector, args.effort, args.prompt)
    except deepseek_host.DeepSeekHostError as exc:
        print(f"sc run: {exc.code}: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
