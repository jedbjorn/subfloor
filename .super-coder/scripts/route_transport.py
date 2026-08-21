"""Apply immutable v2 route bindings without re-resolving their meaning."""
from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "api"))

import opencode_config
import route_bindings


@dataclass(frozen=True)
class TransportProjection:
    harness: str
    model: str | None
    effort: str | None
    argument_tail: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    route_agent: str | None = None
    native_variant_id: str | None = None

    def env(self) -> dict[str, str]:
        return dict(self.environment)


def project(
    binding: Mapping[str, Any],
    binding_digest: str,
    *,
    expected_harness: str | None = None,
    worktree: Path | None = None,
    interface: str = "conversation",
) -> TransportProjection:
    """Validate one binding and derive its sole permitted transport."""
    if interface not in {"conversation", "headless", "interactive"}:
        raise ValueError(f"unsupported route transport interface: {interface}")
    value = dict(binding)
    route_bindings.validate_v2_binding(value)
    if route_bindings.digest_json(value) != binding_digest:
        raise route_bindings.RouteResolutionError(
            "thinking_evidence_missing",
            "Route binding digest does not match its canonical content",
            {},
        )
    harness = value["harness"]
    if expected_harness is not None and harness != expected_harness:
        raise route_bindings.RouteResolutionError(
            "thinking_evidence_missing",
            "Route binding harness does not match the launch adapter",
            {"binding_harness": harness, "launch_harness": expected_harness},
        )

    state = value["control_state"]
    model = value["requested_model"]
    if state != "controlled":
        if value["effective_effort"] is not None:
            raise AssertionError("uncontrolled routes cannot emit effort")
        return TransportProjection(harness=harness, model=model, effort=None)

    effort = value["effective_effort"]
    model_default = effort == route_bindings.DEFAULT_EFFORT
    if harness == "claude":
        return TransportProjection(
            harness, model, effort,
            argument_tail=() if model_default else ("--effort", effort),
        )
    if harness == "codex":
        return TransportProjection(
            harness,
            model,
            effort,
            argument_tail=(
                () if model_default
                else ("-c", f'model_reasoning_effort="{effort}"')
            ),
        )
    if harness == "kimi":
        return TransportProjection(
            harness,
            model,
            effort,
            environment=(
                () if model_default
                else (("KIMI_MODEL_THINKING_EFFORT", effort),)
            ),
        )
    if harness == "opencode":
        if worktree is None:
            raise opencode_config.OpenCodeConfigError(
                "HARNESS_CONFIG_INVALID",
                "OpenCode controlled routes require an exact worktree",
            )
        agent = opencode_config.ensure_route_agent(
            worktree, value, binding_digest
        )
        if interface == "interactive":
            arguments = ("--model", model, "--agent", agent)
        elif interface == "headless":
            arguments = (
                ("--agent", agent) if model_default
                else ("--agent", agent, "--variant", value["native_variant_id"])
            )
        else:
            arguments = ()
        return TransportProjection(
            harness,
            model,
            effort,
            argument_tail=arguments,
            route_agent=agent,
            native_variant_id=value["native_variant_id"],
        )
    if harness == "deepseek":
        return TransportProjection(harness, model, effort)
    raise AssertionError(f"no controlled transport for {harness}")


def context_projection(
    context: Any,
    harness: str,
    *,
    interface: str = "conversation",
) -> TransportProjection | None:
    """Project a v2 context or return None only for an explicit legacy row."""
    binding = getattr(context, "route_binding", None)
    digest = getattr(context, "binding_digest", None)
    if binding is None and digest is None:
        return None
    if binding is None or digest is None:
        raise route_bindings.RouteResolutionError(
            "thinking_evidence_missing",
            "Route binding and digest must be supplied together",
            {"harness": harness},
        )
    projection = project(
        binding,
        digest,
        expected_harness=harness,
        worktree=context.checked_worktree(),
        interface=interface,
    )
    if context.model is not None and context.model != projection.model:
        raise route_bindings.RouteResolutionError(
            "thinking_evidence_missing",
            "Stored model does not match the immutable route binding",
            {"harness": harness},
        )
    if context.effort is not None and context.effort != projection.effort:
        raise route_bindings.RouteResolutionError(
            "thinking_evidence_missing",
            "Stored effort does not match the immutable route binding",
            {"harness": harness},
        )
    return projection
