#!/usr/bin/env python3
"""Inspect and resolve locally callable model routes.

`sc models refresh` is the CLI twin of Shells → Default Models → Refresh.
`sc models resolve <harness> <selector>` is the generic headless-launch seam:
it returns one exact `sc run` call or fails with the reason that route cannot
be honored on this machine.
"""
from __future__ import annotations

import json
import os
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


def _open_db():
    if not DB_PATH.exists():
        raise SystemExit(f"models: no DB at {DB_PATH} — run `sc rebuild`")
    return db_driver.connect(DB_PATH)


def _shell_api_enabled() -> bool:
    """A launched shell reads the live server; no token means root DB mode."""
    if not mem.SC_API_TOKEN and not os.environ.get("DSH_SC_MEM_CREDENTIAL_FILE"):
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
    if (
        binding["control_state"] == "controlled"
        and binding["effective_effort"] is not None
    ):
        command.extend(["--effort", binding["effective_effort"]])
    return command


def _resolution_error(exc: route_bindings.RouteResolutionError) -> dict:
    return {"ok": False, "code": exc.code, "error": exc.message,
            "details": exc.details}


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
    if harness in route_bindings.LIVE_NATIVE_HARNESSES:
        return resolve_row(
            None, harness, selector, shell=shell, effort=effort, now=now,
        )
    if harness == "deepseek":
        model_catalog.ensure_deepseek_route(con, selector)
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
        live_options = None
        if harness in route_bindings.LIVE_NATIVE_HARNESSES and selector is not None:
            proof = route_bindings._probe_controlled_route(harness, selector)
            binding, binding_digest = route_bindings.resolve_live_native(
                harness, selector, effort, route_proof=proof,
            )
            advertised = proof._advertised_options_by_model or {}
            live_options = list(advertised.get(selector) or [])
        elif con is not None:
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
        "source": (
            "harness-live"
            if binding["contract_version"]
            == route_bindings.LIVE_NATIVE_CONTRACT_VERSION
            else (row.get("source") if row else None)
        ),
        "availability": (
            "available"
            if binding["contract_version"]
            == route_bindings.LIVE_NATIVE_CONTRACT_VERSION
            else (row.get("availability") if row else None)
        ),
        "stale": False,
        "cli_version": row.get("cli_version") if row else None,
        "harness_version": row.get("harness_version") if row else None,
        "harness_support_state": (
            row.get("harness_support_state") if row else None
        ),
        "supported_efforts": (
            live_options
            if binding["contract_version"]
            == route_bindings.LIVE_NATIVE_CONTRACT_VERSION
            else (
                json.loads(row["supported_efforts"] or "[]")
                if row and isinstance(row.get("supported_efforts"), str)
                else (row.get("supported_efforts") if row else [])
            )
        ),
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


def _resolve_args(args: list[str]) -> tuple[str, str | None, str | None, str, bool]:
    values = args[1:]
    positional: list[str] = []
    effort = None
    shell = "<shell>"
    as_json = False
    i = 0
    while i < len(values):
        value = values[i]
        if value == "--json":
            as_json = True
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
    return positional[0], positional[1] if len(positional) == 2 else None, \
        effort, shell, as_json


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("usage: sc models refresh | list [harness] | "
              "resolve <harness> [<selector>] [--effort <level>] "
              "[--shell <shortname>] [--json]")
        return 0
    command = args[0]
    if command == "list" and _shell_api_enabled():
        return _print_routes(_api_routes(
            harness=args[1] if len(args) > 1 else None
        ))
    if command == "resolve" and _shell_api_enabled():
        harness, selector, effort, shell, as_json = _resolve_args(args)
        try:
            harness = route_bindings.normalize_harness(harness)
        except route_bindings.RouteResolutionError as exc:
            return _print_resolved(_resolution_error(exc), as_json)
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
        harness, selector, effort, shell, as_json = _resolve_args(args)
        data = resolve(
            con, harness, selector, shell=shell, effort=effort,
        )
        return _print_resolved(data, as_json)
    finally:
        con.close()


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
