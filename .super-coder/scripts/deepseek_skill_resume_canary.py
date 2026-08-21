#!/usr/bin/env python3
"""Run the real DeepSeek adapter skill-refresh canary against a mock provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence


ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import deepseek_runtime  # noqa: E402
import route_bindings  # noqa: E402
from conversation_adapters.base import ConversationContext  # noqa: E402
from conversation_adapters.deepseek import DeepSeekAdapter  # noqa: E402


BOOT_BYTES = "immutable DeepSeek skill canary boot bytes"
MODEL = "deepseek-v4-pro"
INITIAL_SKILLS = ("changed", "current", "revoked")
RESUMED_SKILLS = ("changed", "current", "new")


def _skill_body(name: str, description: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{body}\n"
    )


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_skill_body(name, description, body))


class _ProviderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "initial"
        self.requests: dict[str, list[dict[str, Any]]] = {
            "initial": [],
            "resumed": [],
        }

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def record(self, request: dict[str, Any]) -> tuple[str, int]:
        with self._lock:
            phase = self.phase
            bucket = self.requests[phase]
            bucket.append(request)
            return phase, len(bucket)


class _MockProvider:
    def __init__(self) -> None:
        self.state = _ProviderState()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    self.send_error(400)
                    return
                phase, request_number = state.record(payload)
                calls = (
                    ["changed"]
                    if phase == "initial"
                    else ["changed", "new", "revoked"]
                )
                chunks: list[dict[str, Any]] = [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_content": "",
                                }
                            }
                        ]
                    }
                ]
                if request_number == 1:
                    chunks.append(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": index,
                                                "id": f"{phase}-call-{index}",
                                                "type": "function",
                                                "function": {
                                                    "name": "skill",
                                                    "arguments": json.dumps(
                                                        {"name": name},
                                                        separators=(",", ":"),
                                                    ),
                                                },
                                            }
                                            for index, name in enumerate(calls)
                                        ]
                                    }
                                }
                            ]
                        }
                    )
                    chunks.append(
                        {
                            "choices": [
                                {"delta": {}, "finish_reason": "tool_calls"}
                            ],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                        }
                    )
                else:
                    chunks.extend(
                        [
                            {"choices": [{"delta": {"content": "done"}}]},
                            {
                                "choices": [
                                    {"delta": {}, "finish_reason": "stop"}
                                ],
                                "usage": {
                                    "prompt_tokens": 12,
                                    "completion_tokens": 1,
                                },
                            },
                        ]
                    )
                body = "".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                    for chunk in chunks
                ) + "data: [DONE]\n\n"
                encoded = body.encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_MockProvider":
        self.thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _binding() -> tuple[dict[str, Any], str]:
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "deepseek",
        "requested_model": MODEL,
        "provider_model": MODEL,
        "requested_effort": "default",
        "effective_effort": "default",
        "native_variant_id": None,
        "transport": "deepseek-provider-options-v1",
        "catalogue_generation": "a" * 32,
        "evidence_digest": None,
        "selector_binding": {
            "kind": "authenticated-provider-model",
            "selector": MODEL,
        },
        "adapter_metadata": {
            "provider_route": "deepseek-official",
            "transport_contract": "deepseek-provider-options-v1",
            "wire_evidence_digest": "c" * 64,
            "provider_options": {
                "omit": ["thinking", "reasoning_effort"],
                "set": {},
            },
        },
    }
    route_bindings.validate_v2_binding(binding)
    return binding, route_bindings.digest_json(binding)


def _context(worktree: Path, provider_url: str) -> ConversationContext:
    binding, digest = _binding()
    return ConversationContext(
        worktree=worktree,
        provider="deepseek-official",
        model=MODEL,
        effort="default",
        permission_mode="unrestricted",
        env={
            "DEEPSEEK_API_KEY": "sk-canary-never-persist",
            "DEEPSEEK_BASE_URL": provider_url,
        },
        route_binding=binding,
        binding_digest=digest,
        conversation_id="cv_" + "7" * 32,
        boot_content=BOOT_BYTES,
    )


def _runtime_status(carrier_python: Path) -> deepseek_runtime.RuntimeStatus:
    manifest = deepseek_runtime.load_runtime_manifest()
    return deepseek_runtime.RuntimeStatus(
        available=True,
        enabled=True,
        error=None,
        detail=None,
        carrier_python=str(carrier_python),
        python_version="3.10+",
        sdk_version="0.1.0rc7",
        runtime_version="0.1.0rc7",
        composition_sha256=str(manifest["composition"]["sha256"]),
    )


def _catalog_text(request: Mapping[str, Any]) -> str:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise AssertionError("provider request has no message list")
    catalogs = [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and "<available_skills>" in str(message.get("content") or "")
    ]
    if not catalogs:
        raise AssertionError("provider request has no model-visible skill catalog")
    return catalogs[-1]


def _tool_results(request: Mapping[str, Any]) -> list[str]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise AssertionError("provider request has no message list")
    return [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]


def _assert_catalog(text: str, present: Sequence[str], absent: Sequence[str]) -> None:
    for name in present:
        if f"`{name}`" not in text:
            raise AssertionError(f"skill catalog omitted {name}")
    for name in absent:
        if f"`{name}`" in text:
            raise AssertionError(f"skill catalog retained {name}")


def _terminal_event_types(adapter: DeepSeekAdapter, turn: Any) -> list[str]:
    events = list(adapter.stream(turn))
    terminal = [event.type for event in events if event.type.startswith("run.")]
    if terminal[-1:] != ["run.completed"]:
        raise AssertionError(f"DeepSeek canary did not complete: {terminal}")
    return [event.type for event in events]


def run_canary(carrier_python: Path) -> dict[str, object]:
    if not carrier_python.is_file():
        raise RuntimeError(f"carrier Python is missing: {carrier_python}")
    with tempfile.TemporaryDirectory(prefix="sc-deepseek-skill-canary-") as raw:
        root = Path(raw)
        worktree = root / "worktree"
        skill_root = worktree / ".agents" / "skills"
        worktree.mkdir()
        _write_skill(skill_root, "changed", "Changed skill", "version-one-body")
        _write_skill(skill_root, "current", "Current skill", "current-body")
        _write_skill(skill_root, "revoked", "Revoked skill", "revoked-body")
        state_root = root / "state"

        with _MockProvider() as provider:
            context = _context(worktree, provider.url)
            first = DeepSeekAdapter(
                runtime_probe=lambda **_: _runtime_status(carrier_python),
                state_root=state_root,
                stream_inactivity_seconds=5,
            )
            try:
                try:
                    first_turn = first.start(context, "Load the changed skill")
                except Exception as exc:
                    transport = first._transport_instance
                    stderr = "".join(getattr(transport, "stderr", ()))
                    raise RuntimeError(f"initial carrier failed: {stderr}") from exc
                first_pid = first_turn.process_ref
                first_events = _terminal_event_types(first, first_turn)
                layout = deepseek_runtime.conversation_layout(
                    str(context.conversation_id), state_root=state_root
                )
                identity_before = json.loads(layout.adapter_identity.read_text())
                process_before = json.loads(layout.process_identity.read_text())
            finally:
                first.close()

            initial_requests = provider.state.requests["initial"]
            if len(initial_requests) != 2:
                raise AssertionError(
                    f"initial turn made {len(initial_requests)} provider requests"
                )
            _assert_catalog(_catalog_text(initial_requests[0]), INITIAL_SKILLS, ("new",))
            initial_results = _tool_results(initial_requests[1])
            if not any("version-one-body" in result for result in initial_results):
                raise AssertionError("initial skill body did not reach the real skill tool")

            _write_skill(skill_root, "changed", "Changed skill", "version-two-body")
            _write_skill(skill_root, "new", "New skill", "new-grant-body")
            (skill_root / "revoked" / "SKILL.md").unlink()
            provider.state.set_phase("resumed")
            resumed_context = _context(worktree, provider.url)

            resumed_adapter = DeepSeekAdapter(
                runtime_probe=lambda **_: _runtime_status(carrier_python),
                state_root=state_root,
                stream_inactivity_seconds=5,
            )
            try:
                try:
                    resumed_turn = resumed_adapter.resume(
                        first_turn.session_ref,
                        resumed_context,
                        "Load the current grants",
                    )
                except Exception as exc:
                    transport = resumed_adapter._transport_instance
                    stderr = "".join(getattr(transport, "stderr", ()))
                    raise RuntimeError(f"resumed carrier failed: {stderr}") from exc
                resumed_pid = resumed_turn.process_ref
                resumed_events = _terminal_event_types(resumed_adapter, resumed_turn)
                identity_after = json.loads(layout.adapter_identity.read_text())
                process_after = json.loads(layout.process_identity.read_text())
            finally:
                resumed_adapter.close()

            resumed_requests = provider.state.requests["resumed"]
            if len(resumed_requests) != 2:
                raise AssertionError(
                    f"resumed turn made {len(resumed_requests)} provider requests"
                )
            _assert_catalog(
                _catalog_text(resumed_requests[0]), RESUMED_SKILLS, ("revoked",)
            )
            resumed_results = _tool_results(resumed_requests[1])
            serialized_results = json.dumps(resumed_results, sort_keys=True)
            if "version-two-body" not in serialized_results:
                raise AssertionError("changed skill body was stale after exact resume")
            if "new-grant-body" not in serialized_results:
                raise AssertionError("new grant body was unavailable after exact resume")
            if "revoked-body" in serialized_results:
                raise AssertionError("revoked skill body remained loadable after exact resume")
            if not any(
                'skill "revoked" is unknown or no longer available' in result
                for result in resumed_results
            ):
                raise AssertionError(
                    "revoked skill call did not fail as unavailable: "
                    + serialized_results
                )
            if first_turn.session_ref != resumed_turn.session_ref:
                raise AssertionError("exact native session identity changed on resume")
            if identity_after != identity_before:
                raise AssertionError("stored adapter identity changed on resume")
            expected_boot = hashlib.sha256(BOOT_BYTES.encode()).hexdigest()
            if identity_after.get("boot_sha256") != expected_boot:
                raise AssertionError("immutable boot digest changed on resume")
            if (process_before["pid"], process_before["start_ticks"]) == (
                process_after["pid"],
                process_after["start_ticks"],
            ):
                raise AssertionError("resume reused the old carrier process")
            if str(process_before["pid"]) != first_pid:
                raise AssertionError("initial process reference was not exact")
            if str(process_after["pid"]) != resumed_pid:
                raise AssertionError("resumed process reference was not exact")

        manifest = deepseek_runtime.load_runtime_manifest()
        return {
            "schema_version": 1,
            "contract": "deepseek-production-skill-resume-v1",
            "source_commit": manifest["source"]["commit"],
            "composition_sha256": manifest["composition"]["sha256"],
            "initial_catalog": list(INITIAL_SKILLS),
            "resumed_catalog": list(RESUMED_SKILLS),
            "changed_body_refreshed": True,
            "new_grant_loadable": True,
            "revoked_grant_absent": True,
            "boot_digest_preserved": True,
            "native_session_preserved": True,
            "fresh_carrier_process": True,
            "initial_terminal": first_events[-1],
            "resumed_terminal": resumed_events[-1],
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carrier-python", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    receipt = run_canary(args.carrier_python.absolute())
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt is not None:
        args.receipt.write_text(encoded)
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
