"""Public one-shot execution through the stock DeepSeek Host API."""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import deepseek_host
import deepseek_web


READINESS_MAX_ATTEMPTS = 3
READINESS_WINDOW_SECONDS = 5.0
READINESS_RETRY_DELAY_SECONDS = 0.05
TRANSIENT_READINESS_CODES = frozenset({
    "HARNESS_HOST_UNAVAILABLE",
    "HARNESS_PLUGIN_HEALTH_UNAVAILABLE",
    "HARNESS_REGISTRY_UNAVAILABLE",
    "HARNESS_REGISTRY_STALE_WRITER",
})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sc run --harness deepseek")
    parser.add_argument("--selector", required=True)
    parser.add_argument("--effort", default="default")
    parser.add_argument("prompt")
    return parser


def _terminal_proven(client: deepseek_host.HostTransport, session_ref: str) -> bool:
    try:
        value = client.call("session.history", {"sessionId": session_ref, "maxMessages": 200})
    except deepseek_host.DeepSeekHostError:
        return False
    events = value.get("events") if isinstance(value, Mapping) else None
    if not isinstance(events, list):
        return False
    starts = [
        event.get("event", {}).get("seq")
        for event in events
        if isinstance(event, Mapping)
        and isinstance(event.get("event"), Mapping)
        and event["event"].get("type") == "turn/start"
        and isinstance(event["event"].get("seq"), int)
    ]
    if not starts:
        return False
    boundary = max(starts)
    return any(
        isinstance(event, Mapping)
        and isinstance(event.get("event"), Mapping)
        and event["event"].get("type") == "turn/end"
        and isinstance(event["event"].get("seq"), int)
        and event["event"]["seq"] >= boundary
        for event in events
    )


def _finalize_unknown(client: deepseek_host.HostTransport, session_ref: str) -> None:
    try:
        cancelled = client.call("session.cancel", {"sessionId": session_ref})
    except deepseek_host.DeepSeekHostError:
        cancelled = None
    if not isinstance(cancelled, Mapping) or cancelled.get("accepted") is not True:
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_ONE_SHOT_BUSY",
            "one-shot cancellation lacks terminal proof; its binding requires recovery",
        )
    for attempt in range(3):
        if _terminal_proven(client, session_ref):
            return
        if attempt < 2:
            time.sleep(0.05)
    raise deepseek_host.DeepSeekHostError(
        "HARNESS_ONE_SHOT_BUSY",
        "one-shot cancellation lacks terminal proof; its binding requires recovery",
    )


def _run(
    selector: str,
    effort: str,
    prompt: str,
    *,
    worktree: Path,
    session_ref: str | None = None,
) -> int:
    if not prompt.strip():
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_MESSAGE_INVALID", "one-shot prompt must contain text"
        )
    session_ref = session_ref or f"sc-{uuid.uuid4().hex}"
    client = deepseek_host.DeepSeekHostClient()
    route = deepseek_host.route_for(client, selector)
    if effort != "default" and effort not in route.reasoning_efforts:
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_ROUTE_INVALID",
            f"reasoning effort is unavailable for the exact route: {effort}",
        )
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
    prompt_attempted = False
    terminal = False
    try:
        prompt_attempted = True
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
                terminal = True
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
    except BaseException:
        if prompt_attempted and not terminal:
            _finalize_unknown(client, session_ref)
        raise
    finally:
        stream.close()
    _finalize_unknown(client, session_ref)
    raise deepseek_host.DeepSeekHostError(
        "HARNESS_RECONCILIATION_UNKNOWN",
        "DeepSeek one-shot stream ended after cancellation terminalization",
    )


def run(selector: str, effort: str, prompt: str) -> int:
    """Run with one transactional binding through terminal cleanup."""
    worktree = Path(os.environ.get("SC_SHELL_WORKTREE", os.getcwd())).resolve()
    if not worktree.is_dir():
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_WORKTREE_MISSING", "one-shot worktree is unavailable"
        )
    env = os.environ
    wiring = (
        env.get("SC_API_TOKEN"),
        env.get("SC_API_BASE"),
        env.get("SC_SHELL_ID"),
        env.get("SC_SHELL_SHORTNAME"),
    )
    if not all(wiring):
        raise deepseek_host.DeepSeekHostError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek one-shot requires canonical shell identity",
        )
    try:
        proof_root = deepseek_web.proof_root_from_environment(
            env=env, surface="one-shot"
        )
    except deepseek_web.DeepSeekWebError as exc:
        raise deepseek_host.DeepSeekHostError(exc.code, exc.detail) from exc
    if proof_root is None:
        session_ref = f"sc-{uuid.uuid4().hex}"
    else:
        session_ref = proof_root
    started = time.monotonic()
    last_error: deepseek_web.DeepSeekWebError | None = None
    for attempt in range(READINESS_MAX_ATTEMPTS):
        try:
            deepseek_web.ensure(worktree, env=env)
            proof_authority = deepseek_web.preflight_candidate_execution(
                env=env,
                root_session_id=session_ref,
                conversation_id=f"one-shot:{session_ref}",
                lifecycle_epoch=1,
                worktree=worktree,
            )
            deepseek_web.bind_session_identity(
                env=env,
                root_session_id=session_ref,
                conversation_id=f"one-shot:{session_ref}",
                lifecycle_epoch=1,
                worktree=worktree,
                candidate_preflight=proof_authority,
            )
            break
        except deepseek_web.DeepSeekWebError as exc:
            last_error = exc
            remaining = READINESS_WINDOW_SECONDS - (time.monotonic() - started)
            if (
                exc.code not in TRANSIENT_READINESS_CODES
                or attempt + 1 >= READINESS_MAX_ATTEMPTS
                or remaining <= 0
            ):
                raise deepseek_host.DeepSeekHostError(exc.code, exc.detail) from exc
            time.sleep(min(READINESS_RETRY_DELAY_SECONDS, remaining))
    else:  # pragma: no cover - the final attempt always raises or breaks
        assert last_error is not None
        raise deepseek_host.DeepSeekHostError(
            last_error.code, last_error.detail
        ) from last_error
    try:
        result = _run(
            selector,
            effort,
            prompt,
            worktree=worktree,
            session_ref=session_ref,
        )
    except BaseException as exc:
        busy = (
            isinstance(exc, deepseek_host.DeepSeekHostError)
            and exc.code == "HARNESS_ONE_SHOT_BUSY"
        )
        try:
            deepseek_web.retire_session_identity(
                env=env, root_session_id=session_ref, quiesced=not busy
            )
        except deepseek_web.DeepSeekWebError as retire_exc:
            if busy:
                raise deepseek_host.DeepSeekHostError(
                    "HARNESS_ONE_SHOT_BUSY",
                    f"{exc.detail}; exact binding close failed: {retire_exc.code}",
                ) from exc
            raise deepseek_host.DeepSeekHostError(
                retire_exc.code, retire_exc.detail
            ) from retire_exc
        raise
    else:
        try:
            deepseek_web.retire_session_identity(
                env=env, root_session_id=session_ref, quiesced=True
            )
        except deepseek_web.DeepSeekWebError as exc:
            raise deepseek_host.DeepSeekHostError(exc.code, exc.detail) from exc
        return result
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
