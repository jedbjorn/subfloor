#!/usr/bin/env python3
"""Claude spawn-per-turn adapter for Interface chat hosting.

The driver buffers provider stdout for one short-lived process, normalizes only
the durable contract records, and commits the terminal event with the governing
turn/session state.  Provider transcripts remain the authority for retry
safety; process status and stdout presence never authorize replay.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import interface_chat

STDERR_LIMIT_BYTES = 4096
CLAUDE_MODEL = "fable"
CLAUDE_EFFORT = "low"


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int


@dataclasses.dataclass
class ParseOutcome:
    events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    previews: list[str] = dataclasses.field(default_factory=list)
    health_keys: list[str] = dataclasses.field(default_factory=list)
    provider_session_id: str | None = None
    terminal: str | None = None
    error: str | None = None
    aborted: bool = False


@dataclasses.dataclass(frozen=True)
class TurnRunResult:
    status: str
    turn_id: str | None
    provider_session_id: str | None = None
    retry_safe: bool = False
    previews: tuple[str, ...] = ()
    failure_code: str | None = None


def build_argv(
    prompt: str,
    *,
    provider_session_id: str | None,
    model: str = CLAUDE_MODEL,
    effort: str = CLAUDE_EFFORT,
) -> list[str]:
    """Return the exact Claude 2.1.220 production composition proved by S1b."""
    argv = ["claude", "-p", prompt]
    if provider_session_id:
        argv.extend(["--resume", provider_session_id])
    argv.extend(
        [
            "--model",
            model,
            "--effort",
            effort,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]
    )
    return argv


class SubprocessClaudeRunner:
    """One process per call; there is deliberately no resident harness."""

    def run(self, argv: list[str], *, env: dict[str, str], cwd: str) -> ProcessResult:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return ProcessResult(result.stdout, result.stderr, result.returncode)


def _session_id(row: dict[str, Any]) -> str | None:
    value = row.get("session_id")
    if value is None:
        value = row.get("sessionId")
    return value if isinstance(value, str) and value else None


def _unknown_key(row: dict[str, Any], prefix: str = "unknown") -> str:
    record_type = row.get("type")
    subtype = row.get("subtype")
    type_name = record_type if isinstance(record_type, str) else "missing_type"
    if isinstance(subtype, str):
        return f"{prefix}:{type_name}:{subtype}"
    return f"{prefix}:{type_name}"


def _usage_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    usage = row.get("usage")
    if not isinstance(usage, dict):
        return None
    names = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    payload = {name: usage.get(name) for name in names if name in usage}
    if "total_cost_usd" in row:
        payload["total_cost_usd"] = row["total_cost_usd"]
    return payload or None


class ClaudeStreamParser:
    """Map Claude stream-json or transcript JSONL to the normalized contract."""

    _FRAMING = {
        "message_start",
        "content_block_start",
        "content_block_stop",
        "message_delta",
        "message_stop",
    }
    _IGNORED_DELTAS = {
        "thinking_delta",
        "input_json_delta",
        "signature_delta",
    }

    def parse(
        self,
        text: str,
        *,
        expected_provider_session_id: str | None = None,
        expected_cwd: str | None = None,
        expected_model: str | None = None,
        require_boundary: bool = True,
        require_terminal: bool = True,
    ) -> ParseOutcome:
        outcome = ParseOutcome()
        saw_boundary = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                outcome.error = outcome.error or f"malformed_json_line_{line_number}"
                continue
            if not isinstance(row, dict):
                outcome.error = outcome.error or f"non_object_json_line_{line_number}"
                continue
            row_session = _session_id(row)
            if (
                expected_provider_session_id
                and row_session
                and row_session != expected_provider_session_id
            ):
                outcome.error = outcome.error or "provider_session_mismatch"
                continue

            record_type = row.get("type")
            if record_type == "system":
                subtype = row.get("subtype")
                if subtype == "init":
                    provider_id = _session_id(row)
                    if provider_id is None:
                        outcome.error = outcome.error or "missing_provider_session_id"
                        continue
                    if (
                        outcome.provider_session_id is not None
                        and outcome.provider_session_id != provider_id
                    ):
                        outcome.error = outcome.error or "provider_session_mismatch"
                        continue
                    outcome.provider_session_id = provider_id
                    saw_boundary = True
                    if expected_cwd is not None and row.get("cwd") != expected_cwd:
                        outcome.error = outcome.error or "provider_cwd_mismatch"
                    actual_model = row.get("model")
                    if expected_model is not None and not (
                        actual_model == expected_model
                        or (
                            isinstance(actual_model, str)
                            and actual_model.startswith(f"claude-{expected_model}-")
                        )
                    ):
                        outcome.error = outcome.error or "provider_model_mismatch"
                    if row.get("apiKeySource") not in (None, "none"):
                        outcome.error = outcome.error or "api_key_billing_disallowed"
                elif subtype in {"status", "thinking_tokens"}:
                    continue
                else:
                    outcome.health_keys.append(_unknown_key(row, "system"))
                continue

            if record_type == "rate_limit_event":
                info = row.get("rate_limit_info")
                if isinstance(info, dict) and info.get("status") not in (None, "allowed"):
                    outcome.health_keys.append(
                        f"rate_limit:{str(info.get('status'))[:48]}"
                    )
                continue

            if record_type == "stream_event":
                event = row.get("event")
                if not isinstance(event, dict):
                    outcome.health_keys.append("stream_event:missing_event")
                    continue
                event_type = event.get("type")
                if event_type == "content_block_delta":
                    delta = event.get("delta")
                    if not isinstance(delta, dict):
                        outcome.health_keys.append("stream_event:missing_delta")
                        continue
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text_delta = delta.get("text")
                        if isinstance(text_delta, str):
                            outcome.previews.append(text_delta)
                        else:
                            outcome.health_keys.append(
                                "stream_event:text_delta_without_text"
                            )
                    elif delta_type not in self._IGNORED_DELTAS:
                        outcome.health_keys.append(
                            f"stream_event:delta:{str(delta_type)[:48]}"
                        )
                elif event_type not in self._FRAMING:
                    outcome.health_keys.append(
                        f"stream_event:{str(event_type)[:48]}"
                    )
                continue

            if record_type == "assistant":
                message = row.get("message")
                contents = message.get("content") if isinstance(message, dict) else None
                if not isinstance(contents, list):
                    outcome.health_keys.append("assistant:missing_content")
                    continue
                for part in contents:
                    if not isinstance(part, dict):
                        outcome.health_keys.append("assistant:non_object_content")
                        continue
                    part_type = part.get("type")
                    if part_type == "text":
                        value = part.get("text")
                        if isinstance(value, str) and value:
                            outcome.events.append(
                                {
                                    "kind": "message_completed",
                                    "role": "assistant",
                                    "payload": {"text": value},
                                }
                            )
                    elif part_type == "tool_use":
                        outcome.events.append(
                            {
                                "kind": "tool_call",
                                "role": "tool",
                                "payload": {
                                    "tool_call_id": part.get("id"),
                                    "tool_name": part.get("name"),
                                    "arguments": part.get("input"),
                                },
                            }
                        )
                    elif part_type != "thinking":
                        outcome.health_keys.append(
                            f"assistant:content:{str(part_type)[:48]}"
                        )
                continue

            if record_type == "user":
                message = row.get("message")
                contents = message.get("content") if isinstance(message, dict) else None
                if not isinstance(contents, list):
                    continue
                for part in contents:
                    if not isinstance(part, dict) or part.get("type") != "tool_result":
                        continue
                    outcome.events.append(
                        {
                            "kind": "tool_result",
                            "role": "tool",
                            "payload": {
                                "tool_call_id": part.get("tool_use_id"),
                                "content": part.get("content"),
                                "is_error": bool(part.get("is_error")),
                            },
                        }
                    )
                continue

            if record_type == "result":
                usage = _usage_payload(row)
                if usage is not None:
                    outcome.events.append(
                        {"kind": "usage", "role": None, "payload": usage}
                    )
                failed = (
                    row.get("is_error") is True
                    or row.get("subtype") == "error_during_execution"
                    or row.get("terminal_reason") == "aborted_streaming"
                )
                if failed:
                    outcome.terminal = "failed"
                    outcome.aborted = row.get("terminal_reason") == "aborted_streaming"
                elif row.get("is_error") is False and row.get("subtype") == "success":
                    outcome.terminal = "completed"
                else:
                    outcome.terminal = "failed"
                    outcome.error = outcome.error or "unrecognized_terminal_result"
                continue

            outcome.health_keys.append(_unknown_key(row))

        if require_boundary and not saw_boundary:
            outcome.error = outcome.error or "missing_session_boundary"
        if require_terminal and outcome.terminal is None:
            outcome.error = outcome.error or "missing_terminal_result"
        return outcome


def _json_rows(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    malformed = False
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(value, dict):
            malformed = True
            continue
        rows.append(value)
    return rows, malformed


def _contains_text(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, list):
        return any(_contains_text(item, needle) for item in value)
    if isinstance(value, dict):
        return any(_contains_text(item, needle) for item in value.values())
    return False


def retry_allowed(
    *,
    anchor_present: bool,
    anchor_unambiguous: bool,
    prompt_present: bool,
    turn_failed: bool,
) -> bool:
    """The four independently pinned operands of the no-replay rule."""
    return (
        turn_failed
        and anchor_present
        and anchor_unambiguous
        and not prompt_present
    )


def _human_prompt_row(row: dict[str, Any]) -> bool:
    if row.get("type") != "user":
        return False
    message = row.get("message")
    if not isinstance(message, dict):
        return True
    contents = message.get("content")
    if isinstance(contents, list) and contents:
        return not all(
            isinstance(part, dict) and part.get("type") == "tool_result"
            for part in contents
        )
    return True


class ClaudeTranscriptResolver:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home) if home is not None else Path.home()

    def resolve(self, cwd: str, provider_session_id: str) -> Path:
        candidates = list(
            (self.home / ".claude" / "projects").glob(
                f"*/{provider_session_id}.jsonl"
            )
        )
        matches = []
        for path in candidates:
            try:
                rows, _ = _json_rows(path.read_bytes())
            except OSError:
                continue
            if any(
                (_session_id(row) == provider_session_id)
                and row.get("cwd") == cwd
                for row in rows
            ):
                matches.append(path.resolve())
        if len(matches) != 1:
            raise RuntimeError(
                "expected one validated Claude transcript, "
                f"found {len(matches)}"
            )
        return matches[0]

    def capture(
        self,
        *,
        cwd: str,
        provider_session_id: str | None,
    ) -> dict[str, Any]:
        if provider_session_id is None:
            return {"version": 1, "status": "missing", "reason": "new_session"}
        try:
            path = self.resolve(cwd, provider_session_id)
            records = self._line_records(path)
        except (OSError, RuntimeError) as exc:
            return {
                "version": 1,
                "status": "missing",
                "reason": type(exc).__name__,
            }
        if not records:
            return {"version": 1, "status": "missing", "reason": "empty_transcript"}
        offset, line = records[-1]
        return {
            "version": 1,
            "status": "ready",
            "path": str(path),
            "offset": offset,
            "next_offset": offset + len(line),
            "line_sha256": hashlib.sha256(line).hexdigest(),
            "file_size": path.stat().st_size,
        }

    @staticmethod
    def _line_records(path: Path) -> list[tuple[int, bytes]]:
        result = []
        offset = 0
        with path.open("rb") as stream:
            for line in stream:
                result.append((offset, line))
                offset += len(line)
        return result

    def resume_anchor(self, anchor: dict[str, Any]) -> dict[str, Any]:
        if anchor.get("status") != "ready" or not anchor.get("path"):
            return {"status": "gap", "reason": "pre-turn anchor missing"}
        path = Path(anchor["path"])
        try:
            records = self._line_records(path)
        except OSError:
            return {"status": "gap", "reason": "transcript missing"}
        expected = anchor.get("line_sha256")
        exact = [
            (offset, line)
            for offset, line in records
            if offset == anchor.get("offset")
            and hashlib.sha256(line).hexdigest() == expected
        ]
        if exact:
            offset, line = exact[0]
            status = "exact"
        else:
            relocated = [
                (offset, line)
                for offset, line in records
                if hashlib.sha256(line).hexdigest() == expected
            ]
            if len(relocated) != 1:
                return {
                    "status": "gap",
                    "reason": "stored anchor missing or ambiguous after rewrite",
                    "matches": len(relocated),
                }
            offset, line = relocated[0]
            status = "relocated"
        return {
            "status": status,
            "path": str(path.resolve()),
            "offset": offset,
            "next_offset": offset + len(line),
            "line_sha256": expected,
        }

    def suffix(
        self, anchor: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        resolution = self.resume_anchor(anchor)
        if resolution["status"] == "gap":
            return resolution, []
        path = Path(resolution["path"])
        with path.open("rb") as stream:
            stream.seek(resolution["next_offset"])
            data = stream.read()
        rows, malformed = _json_rows(data)
        if malformed:
            return {
                "status": "gap",
                "reason": "malformed post-anchor transcript",
            }, []
        return resolution, rows

    def retry_proof(
        self,
        anchor: dict[str, Any],
        prompt: str,
    ) -> tuple[dict[str, Any], bool]:
        resolution, rows = self.suffix(anchor)
        prompt_present = any(_contains_text(row, prompt) for row in rows)
        return resolution, retry_allowed(
            anchor_present=anchor.get("status") == "ready",
            anchor_unambiguous=resolution["status"] in {"exact", "relocated"},
            prompt_present=prompt_present,
            turn_failed=True,
        )

    def backfill(
        self,
        anchor: dict[str, Any],
        *,
        prompt: str,
        expected_provider_session_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        resolution, rows = self.suffix(anchor)
        if resolution["status"] == "gap":
            return resolution, [], []
        start = next(
            (
                index
                for index, row in enumerate(rows)
                if _human_prompt_row(row) and _contains_text(row, prompt)
            ),
            None,
        )
        if start is None:
            return {
                "status": "gap",
                "reason": "submitted prompt not found in post-anchor range",
            }, [], []
        bounded: list[dict[str, Any]] = []
        for row in rows[start:]:
            if bounded and _human_prompt_row(row):
                break
            bounded.append(row)
        text = "\n".join(json.dumps(row, separators=(",", ":")) for row in bounded)
        parsed = ClaudeStreamParser().parse(
            text,
            expected_provider_session_id=expected_provider_session_id,
            require_boundary=False,
            require_terminal=False,
        )
        if parsed.error is not None:
            return {"status": "gap", "reason": parsed.error}, [], parsed.health_keys
        return resolution, parsed.events, parsed.health_keys


def bounded_diagnostic(stderr: str, *, cwd: str) -> str:
    value = stderr.replace(str(Path.home()), "<HOME>").replace(cwd, "<CWD>")
    data = value.encode(errors="replace")
    if len(data) > STDERR_LIMIT_BYTES:
        data = data[-STDERR_LIMIT_BYTES:]
    return data.decode(errors="replace")


class ClaudeDriver:
    def __init__(
        self,
        store: interface_chat.ChatStore,
        *,
        runner: Any | None = None,
        resolver: ClaudeTranscriptResolver | None = None,
        model: str = CLAUDE_MODEL,
        effort: str = CLAUDE_EFFORT,
    ):
        self.store = store
        self.runner = runner or SubprocessClaudeRunner()
        self.resolver = resolver or ClaudeTranscriptResolver()
        self.model = model
        self.effort = effort

    def run_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        source: str = "composer",
        attempt_of: str | None = None,
    ) -> TurnRunResult:
        if source not in {"composer", "wake"}:
            raise interface_chat.ChatStoreError(f"unsupported turn source: {source}")
        session = self.store.session(session_id)
        if session["harness"] != "claude":
            raise interface_chat.ChatStoreError("Claude driver requires a Claude session")
        anchor = self.resolver.capture(
            cwd=session["cwd"],
            provider_session_id=session["provider_session_id"],
        )
        turn_id = uuid.uuid4().hex
        action = self.store.request_action(
            session_id,
            source,
            prompt=prompt,
            anchor=anchor,
            turn_id=turn_id,
            attempt_of=attempt_of,
        )
        if action.status != "accepted":
            return TurnRunResult(action.status, None)

        argv = build_argv(
            prompt,
            provider_session_id=session["provider_session_id"],
            model=self.model,
            effort=self.effort,
        )
        env = os.environ.copy()
        env["IS_SANDBOX"] = "1"
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        process_lost = False
        try:
            process = self.runner.run(argv, env=env, cwd=session["cwd"])
        except Exception as exc:  # noqa: BLE001 - lost spawn/process must close the turn
            process_lost = True
            process = ProcessResult("", type(exc).__name__, -1)

        parsed = ClaudeStreamParser().parse(
            process.stdout,
            expected_provider_session_id=session["provider_session_id"],
            expected_cwd=session["cwd"],
            expected_model=self.model,
        )
        if parsed.health_keys:
            self.store.increment_health("claude", parsed.health_keys)
        provider_id = parsed.provider_session_id or session["provider_session_id"]
        if (
            parsed.provider_session_id
            and parsed.error != "provider_session_mismatch"
        ):
            try:
                self.store.bind_provider_session(
                    session_id, parsed.provider_session_id
                )
            except interface_chat.ChatStoreError:
                parsed.error = parsed.error or "provider_session_conflict"
            else:
                provider_id = parsed.provider_session_id
                try:
                    transcript = self.resolver.resolve(
                        session["cwd"], parsed.provider_session_id
                    )
                except (OSError, RuntimeError):
                    transcript = None
                if transcript is not None:
                    self.store.bind_provider_session(
                        session_id,
                        parsed.provider_session_id,
                        transcript_locator=str(transcript),
                    )

        success = (
            process.returncode == 0
            and parsed.terminal == "completed"
            and parsed.error is None
        )
        if success:
            self.store.complete_turn(
                turn_id, parsed.events, exit_code=process.returncode
            )
            return TurnRunResult(
                "completed",
                turn_id,
                provider_session_id=provider_id,
                previews=tuple(parsed.previews),
            )

        resolution, retry_safe = self.resolver.retry_proof(anchor, prompt)
        self.store.update_anchor_resolution(turn_id, resolution)
        if process_lost:
            failure_code = "process_lost"
        elif parsed.error is not None:
            failure_code = parsed.error
        elif parsed.terminal != "completed":
            failure_code = "provider_failed"
        else:
            failure_code = "process_exit"
        self.store.fail_turn(
            turn_id,
            parsed.events,
            exit_code=process.returncode,
            failure_code=failure_code,
            diagnostic=bounded_diagnostic(process.stderr, cwd=session["cwd"]),
            retry_safe=retry_safe,
            aborted=parsed.aborted,
        )
        return TurnRunResult(
            "failed",
            turn_id,
            provider_session_id=provider_id,
            retry_safe=retry_safe,
            previews=tuple(parsed.previews),
            failure_code=failure_code,
        )

    def retry(self, turn_id: str) -> TurnRunResult:
        context = self.store.turn_context(turn_id)
        prompt = self.store.retry_prompt(turn_id)
        return self.run_turn(
            context["session_id"],
            prompt,
            source="composer",
            attempt_of=turn_id,
        )

    def backfill_turn(self, turn_id: str) -> tuple[str, int]:
        context = self.store.turn_context(turn_id)
        provider_id = context["provider_session_id"]
        if not provider_id:
            return "gap", 0
        anchor = json.loads(context["pre_turn_anchor_json"])
        prompt = context["submitted_prompt"]
        resolution, events, health = self.resolver.backfill(
            anchor,
            prompt=prompt,
            expected_provider_session_id=provider_id,
        )
        self.store.update_anchor_resolution(turn_id, resolution)
        if health:
            self.store.increment_health("claude", health)
        if resolution["status"] == "gap":
            return "gap", 0
        return resolution["status"], self.store.append_events(
            turn_id, events, transcript_anchor=resolution
        )
