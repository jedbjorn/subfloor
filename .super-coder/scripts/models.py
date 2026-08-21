#!/usr/bin/env python3
"""Inspect and resolve locally callable model routes.

`sc models refresh` is the CLI twin of Shells → Default Models → Refresh.
`sc models resolve <harness> <selector>` is the generic headless-launch seam:
it returns one exact `sc run` call or fails with the reason that route cannot
be honored on this machine.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from urllib.parse import urlencode

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))
import model_catalog  # noqa: E402
import route_bindings  # noqa: E402
import db_driver  # noqa: E402
import mem  # noqa: E402

SPRINT_ADMISSION_CATEGORIES = frozenset({
    "harness-unavailable",
    "runtime-unavailable",
    "credential-or-authentication",
    "catalogue-unavailable",
    "catalogue-stale",
    "exact-model-absent",
    "exact-model-unavailable",
    "tool-capability-unsupported",
    "tool-capability-unproven",
    "route-evidence-invalid",
    "provider-option-drift",
    "unknown",
})
SPRINT_ADMISSION_KEYS = (
    "contract_version",
    "requested_provider",
    "requested_model",
    "admitted",
    "error_code",
    "category",
    "required_surface",
    "required_capability",
    "freshness",
    "authentication",
    "tool_capability",
    "exit_class",
)
SPRINT_ADMISSION_ERROR_CODES = {
    category: "ROUTE_ADMISSION_" + category.replace("-", "_").upper()
    for category in SPRINT_ADMISSION_CATEGORIES
}
SPRINT_ADMISSION_CODE_CATEGORIES = {
    "harness_unavailable": "harness-unavailable",
    "runtime_unavailable": "runtime-unavailable",
    "credential_or_authentication": "credential-or-authentication",
    "catalogue_unavailable": "catalogue-unavailable",
    "catalogue_stale": "catalogue-stale",
    "exact_model_absent": "exact-model-absent",
    "exact_model_unavailable": "exact-model-unavailable",
    "tool_capability_unsupported": "tool-capability-unsupported",
    "tool_capability_unproven": "tool-capability-unproven",
    "route_evidence_invalid": "route-evidence-invalid",
    "provider_option_drift": "provider-option-drift",
}


def _open_db():
    if not DB_PATH.exists():
        raise SystemExit(f"models: no DB at {DB_PATH} — run `sc rebuild`")
    return db_driver.connect(DB_PATH)


def _shell_api_enabled() -> bool:
    """A launched shell reads the live server; no token means root DB mode."""
    if not mem.SC_API_TOKEN:
        return False
    mem._PROG = "models"
    mem._require_api()
    return True


def _api_route_projection(*, harness: str | None = None,
                          selector: str | None = None) -> dict:
    query = urlencode({
        name: value for name, value in (
            ("harness", harness), ("selector", selector)
        ) if value is not None
    })
    path = "/_sc/model-routes" + (f"?{query}" if query else "")
    return mem._api("GET", path)


def _api_routes(*, harness: str | None = None,
                selector: str | None = None) -> list[dict]:
    return _api_route_projection(harness=harness, selector=selector).get(
        "routes"
    ) or []


def _route(con, harness: str, selector: str):
    try:
        row = con.execute(
            "SELECT * FROM model_routes WHERE harness=? AND selector=?",
            (harness, selector)).fetchone()
    except db_driver.OperationalError:
        raise SystemExit("models: model_routes unavailable — run `sc rebuild` to migrate")
    return dict(row) if row else None


def _command(binding: dict, shell: str) -> list[str]:
    command = ["./sc", "run", shell, "--harness", binding["harness"]]
    if binding["requested_model"] is not None:
        command.extend(["-m", binding["requested_model"]])
    if binding["control_state"] == "controlled":
        command.extend(["--effort", binding["effective_effort"]])
    return command


def _resolution_error(exc: route_bindings.RouteResolutionError) -> dict:
    return {"ok": False, "code": exc.code, "error": exc.message,
            "details": exc.details}


def _json_object(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _sprint_admission_identity(selector: str | None) -> tuple[str, str]:
    value = selector if isinstance(selector, str) else ""
    provider, separator, model = value.partition("/")
    if not separator or not provider or not model:
        return "unknown", value or "unknown"
    return provider, model


def _sprint_admission_category(
    data: dict, route: dict | None, harness_error: str | None
) -> str:
    code = data.get("code") if isinstance(data.get("code"), str) else None
    if code in SPRINT_ADMISSION_CODE_CATEGORIES:
        return SPRINT_ADMISSION_CODE_CATEGORIES[code]
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    runtime_error = details.get("runtime_error")
    if runtime_error in {"HARNESS_UNAVAILABLE", "HARNESS_DISABLED"}:
        return "harness-unavailable"
    if runtime_error in {
        "HARNESS_RUNTIME_INCOMPATIBLE",
        "HARNESS_RUNTIME_MISSING",
        "HARNESS_RUNTIME_UNAVAILABLE",
    }:
        return "runtime-unavailable"
    last_error = harness_error or (route or {}).get("last_error")
    if last_error == model_catalog.DEEPSEEK_AUTHENTICATION_ERROR:
        return "credential-or-authentication"
    if last_error == model_catalog.DEEPSEEK_EXACT_MODEL_ABSENT:
        return "exact-model-absent"
    if last_error == getattr(
        model_catalog, "DEEPSEEK_PROVIDER_TOOLS_UNSUPPORTED", None
    ):
        return "tool-capability-unsupported"
    if last_error == model_catalog.DEEPSEEK_PROVIDER_TOOLS_UNVERIFIED:
        return "tool-capability-unproven"
    if last_error == model_catalog.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED:
        return "provider-option-drift"
    if last_error in {
        model_catalog.DEEPSEEK_DISCOVERY_EVIDENCE_INVALID,
        model_catalog.DEEPSEEK_PROVIDER_REGISTRY_INVALID,
    }:
        return "route-evidence-invalid"
    if last_error in {
        model_catalog.DEEPSEEK_DISCOVERY_ERROR,
        model_catalog.DEEPSEEK_DISCOVERY_LIMIT_ERROR,
    }:
        return "catalogue-unavailable"
    if route is None:
        return "exact-model-absent"
    if route.get("stale") or code in {
        "thinking_evidence_stale", "thinking_evidence_changed"
    }:
        return "catalogue-stale"
    if route.get("availability") != "available":
        return "exact-model-unavailable"
    if code == "unsupported_thinking_level":
        return "provider-option-drift"
    if code == "thinking_evidence_missing":
        return "route-evidence-invalid"
    return "unknown"


def sprint_admission_result(
    data: dict,
    route: dict | None,
    harness: str,
    selector: str | None,
    harness_error: str | None = None,
) -> dict:
    """Project one bounded Sprint/tool admission result without raw diagnostics."""
    provider, model = _sprint_admission_identity(selector)
    binding = data.get("binding") if isinstance(data.get("binding"), dict) else {}
    binding_selector = _json_object(binding.get("selector_binding"))
    route_selector = _json_object((route or {}).get("selector_binding"))
    exact_identity = bool(
        harness == "deepseek"
        and binding.get("harness") == harness
        and binding.get("requested_model") == selector
        and binding.get("provider_model") == model
        and isinstance(binding.get("adapter_metadata"), dict)
        and binding["adapter_metadata"].get("provider_route") == provider
    )
    tools_supported = bool(
        binding_selector is not None
        and route_selector is not None
        and binding_selector.get("tool_capability_verified") is True
        and route_selector.get("tool_capability_verified") is True
    )
    admitted = bool(
        data.get("ok") is True
        and route is not None
        and route.get("harness") == harness
        and route.get("selector") == selector
        and route.get("availability") == "available"
        and not bool(route.get("stale"))
        and exact_identity
        and tools_supported
    )
    if admitted:
        category = None
    elif data.get("ok") is True and not exact_identity:
        category = "route-evidence-invalid"
    elif data.get("ok") is True and not tools_supported:
        category = "tool-capability-unproven"
    else:
        category = _sprint_admission_category(data, route, harness_error)
    if category not in SPRINT_ADMISSION_CATEGORIES and category is not None:
        category = "unknown"
    freshness = (
        "fresh"
        if admitted
        else "missing"
        if route is None
        else "stale"
        if bool(route.get("stale"))
        else "fresh"
    )
    tool_capability = (
        "supported"
        if admitted
        else "unsupported"
        if category == "tool-capability-unsupported"
        else "unproven"
        if category == "tool-capability-unproven"
        else "unknown"
    )
    authenticated_evidence = harness_error in {
        model_catalog.DEEPSEEK_EXACT_MODEL_ABSENT,
        model_catalog.DEEPSEEK_PROVIDER_TOOLS_UNSUPPORTED,
        model_catalog.DEEPSEEK_PROVIDER_TOOLS_UNVERIFIED,
        model_catalog.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        model_catalog.DEEPSEEK_DISCOVERY_EVIDENCE_INVALID,
    }
    authentication = (
        "verified"
        if admitted or authenticated_evidence
        or category == "tool-capability-unsupported"
        else "failed"
        if category == "credential-or-authentication"
        else "unproven"
    )
    result = {
        "contract_version": 1,
        "requested_provider": provider,
        "requested_model": model,
        "admitted": admitted,
        "error_code": None if admitted else SPRINT_ADMISSION_ERROR_CODES[category],
        "category": category,
        "required_surface": "sprint",
        "required_capability": "reviewer-shell-tool-execution",
        "freshness": freshness,
        "authentication": authentication,
        "tool_capability": tool_capability,
        "exit_class": "success" if admitted else "route-rejected",
    }
    if tuple(result) != SPRINT_ADMISSION_KEYS:
        raise AssertionError("Sprint admission result key order drifted")
    return result


def resolve(con, harness: str, selector: str | None = None, *,
            shell: str = "<shell>", effort: str | None = None,
            now=None, runtime_status: dict | None = None,
            runtime_scope: dict | None = None) -> dict:
    try:
        harness = route_bindings.normalize_harness(harness)
    except route_bindings.RouteResolutionError as exc:
        return _resolution_error(exc)
    if selector is None or harness == "vibe":
        if runtime_scope is None:
            runtime_scope = model_catalog.harness_versions.runtime_scope()
        if runtime_status is None:
            runtime_status = model_catalog.harness_runtime_status(harness)
        return resolve_row(
            None, harness, selector, shell=shell, effort=effort, now=now,
            runtime_status=runtime_status, runtime_scope=runtime_scope,
        )
    observed_row = _route(con, harness, selector)
    return resolve_row(
        observed_row, harness, selector, shell=shell, effort=effort, now=now,
        con=con,
    )


def resolve_row(row: dict | None, harness: str, selector: str | None, *,
                shell: str = "<shell>", effort: str | None = None,
                now=None,
                con=None, runtime_status: dict | None = None,
                runtime_scope: dict | None = None) -> dict:
    """Resolve through ``con``'s owned freshness write, or purely if omitted."""
    try:
        if con is not None:
            binding, binding_digest = route_bindings.resolve_persisted_v2(
                con, row, harness, selector, effort, now=now,
                runtime_status=runtime_status,
                runtime_scope=runtime_scope,
            )
        else:
            binding, binding_digest = route_bindings.resolve_v2(
                row, harness, selector, effort, now=now,
                runtime_status=runtime_status,
                runtime_scope=runtime_scope,
            )
    except route_bindings.RouteResolutionError as exc:
        return _resolution_error(exc)
    return {
        "ok": True,
        "harness": binding["harness"],
        "selector": binding["requested_model"],
        "source": row.get("source") if row else None,
        "availability": row.get("availability") if row else None,
        "stale": False,
        "cli_version": row.get("cli_version") if row else None,
        "harness_version": row.get("harness_version") if row else None,
        "harness_support_state": (
            row.get("harness_support_state") if row else None
        ),
        "supported_efforts": json.loads(row["supported_efforts"] or "[]")
        if row and isinstance(row.get("supported_efforts"), str)
        else (row.get("supported_efforts") if row else []),
        "binding": binding,
        "binding_digest": binding_digest,
        "command": _command(binding, shell),
        "error": None,
    }


def _print_resolved(data: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(data, indent=2))
    elif not data["ok"]:
        print(f"models: {data.get('code', 'route_error')}: {data['error']}",
              file=sys.stderr)
    else:
        binding = data["binding"]
        print(f"route: {binding['control_state']} · {data['harness']}/"
              f"{data['selector'] or '<default>'} · {data['binding_digest']}")
        print(f"call:  {shlex.join(data['command'])}")
    return 0 if data["ok"] else 2


def _print_sprint_admission(data: dict) -> int:
    print(json.dumps(data, separators=(",", ":"), sort_keys=False))
    return 0 if data["admitted"] else 2


def _print_routes(rows) -> int:
    if not rows:
        print("models: no routes — run `sc models refresh`", file=sys.stderr)
        return 2
    for r in rows:
        runnable = (r["availability"] == "available" and r["headless_supported"]
                    and r["high_effort_supported"])
        state = "runnable" if runnable else r["availability"]
        if r["stale"]:
            state += "/stale"
        support = r.get("harness_support_state") if isinstance(r, dict) \
            else r["harness_support_state"]
        observed = r.get("harness_version") if isinstance(r, dict) \
            else r["harness_version"]
        detail = " · ".join(value for value in (support, observed) if value)
        print(f"{r['harness']}/{r['selector']}\t{state}\t{r['source']}"
              + (f"\t{detail}" if detail else ""))
    return 0


def _list(con, harness: str | None) -> int:
    sql = ("SELECT harness, selector, source, availability, stale, "
           "headless_supported, high_effort_supported,harness_version,"
           "harness_support_state FROM model_routes")
    params: tuple = ()
    if harness:
        sql += " WHERE harness=?"
        params = (harness,)
    sql += " ORDER BY harness, availability='available' DESC, selector"
    try:
        rows = con.execute(sql, params).fetchall()
    except db_driver.OperationalError:
        raise SystemExit("models: model_routes unavailable — run `sc rebuild` to migrate")
    return _print_routes(rows)


def _resolve_args(
    args: list[str],
) -> tuple[str, str | None, str | None, str, bool, bool]:
    values = args[1:]
    positional: list[str] = []
    effort = None
    shell = "<shell>"
    as_json = False
    sprint_admission_json = False
    i = 0
    while i < len(values):
        value = values[i]
        if value == "--json":
            as_json = True
            i += 1
            continue
        if value == "--sprint-admission-json":
            sprint_admission_json = True
            i += 1
            continue
        if value in {"--effort", "--shell"}:
            if i + 1 >= len(values) or values[i + 1].startswith("--"):
                raise SystemExit(f"models: {value} requires a value")
            if value == "--shell" and not values[i + 1].strip():
                raise SystemExit("models: --shell requires a non-blank value")
            if value == "--effort":
                effort = values[i + 1]
            else:
                shell = values[i + 1]
            i += 2
            continue
        if value.startswith("-"):
            raise SystemExit(f"models: unknown option {value}")
        positional.append(value)
        i += 1
    if not positional or len(positional) > 2:
        raise SystemExit("models: resolve requires <harness> [<selector>]")
    if as_json and sprint_admission_json:
        raise SystemExit("models: choose --json or --sprint-admission-json")
    return positional[0], positional[1] if len(positional) == 2 else None, \
        effort, shell, as_json, sprint_admission_json


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("usage: sc models refresh | list [harness] | "
              "resolve <harness> [<selector>] [--effort <level>] "
              "[--shell <shortname>] [--json|--sprint-admission-json]")
        return 0
    command = args[0]
    if command == "list" and _shell_api_enabled():
        return _print_routes(_api_routes(
            harness=args[1] if len(args) > 1 else None
        ))
    if command == "resolve" and _shell_api_enabled():
        harness, selector, effort, shell, as_json, sprint_json = _resolve_args(args)
        try:
            harness = route_bindings.normalize_harness(harness)
        except route_bindings.RouteResolutionError as exc:
            data = _resolution_error(exc)
            if sprint_json:
                return _print_sprint_admission(
                    sprint_admission_result(data, None, harness, selector)
                )
            return _print_resolved(data, as_json)
        projection = _api_route_projection(
            harness=harness, selector=selector
        )
        routes = projection.get("routes") or []
        route = routes[0] if routes else None
        runtime_scope = None
        runtime_status = None
        if selector is None or harness == "vibe":
            runtime_scope = model_catalog.harness_versions.runtime_scope()
            runtime_status = model_catalog.harness_runtime_status(harness)
        data = resolve_row(
            route, harness, selector, shell=shell,
            effort=effort,
            runtime_status=runtime_status,
            runtime_scope=runtime_scope,
        )
        if sprint_json:
            return _print_sprint_admission(
                sprint_admission_result(
                    data,
                    route,
                    harness,
                    selector,
                    projection.get("harness_error"),
                )
            )
        return _print_resolved(data, as_json)

    con = _open_db()
    try:
        if args[0] == "refresh":
            payload = model_catalog.catalog(refresh=True, con=con)
            print("models: " + ("stale — " + payload.get("error", "refresh failed")
                                if payload.get("stale") else
                                "refreshed from " + ", ".join(payload.get("sources") or [])))
            return 2 if payload.get("stale") else 0
        if args[0] == "list":
            return _list(con, args[1] if len(args) > 1 else None)
        if args[0] != "resolve":
            raise SystemExit("models: expected refresh, list, or resolve")
        harness, selector, effort, shell, as_json, sprint_json = _resolve_args(args)
        route = _route(con, harness, selector) if selector is not None else None
        data = resolve(
            con, harness, selector, shell=shell, effort=effort,
        )
        if sprint_json:
            return _print_sprint_admission(
                sprint_admission_result(
                    data,
                    route,
                    harness,
                    selector,
                    model_catalog.latest_harness_error(con, harness),
                )
            )
        return _print_resolved(data, as_json)
    finally:
        con.close()


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
