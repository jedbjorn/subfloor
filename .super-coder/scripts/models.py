#!/usr/bin/env python3
"""Inspect and resolve locally callable model routes.

`sc models refresh` is the CLI twin of Shells → Default Models → Refresh.
`sc models resolve <harness> <selector>` is the sprint skill's lazy-load seam:
it returns one exact, high-effort `sc run` call or fails with the reason that
route cannot be honored on this machine.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))
import model_catalog  # noqa: E402
import db_driver  # noqa: E402


def _open_db():
    if not DB_PATH.exists():
        raise SystemExit(f"models: no DB at {DB_PATH} — run `sc rebuild`")
    return db_driver.connect(DB_PATH)


def _route(con, harness: str, selector: str):
    try:
        row = con.execute(
            "SELECT * FROM model_routes WHERE harness=? AND selector=?",
            (harness, selector)).fetchone()
    except db_driver.OperationalError:
        raise SystemExit("models: model_routes unavailable — run `sc rebuild` to migrate")
    if row is None:
        model_catalog.catalog(con=con)
        row = con.execute(
            "SELECT * FROM model_routes WHERE harness=? AND selector=?",
            (harness, selector)).fetchone()
    return dict(row) if row else None


def _command(harness: str, selector: str, shell: str, effort: str) -> list[str]:
    return ["./sc", "run", shell, "--harness", harness, "-m", selector,
            "--effort", effort]


def resolve(con, harness: str, selector: str, *, shell: str = "<shell>",
            effort: str = "high") -> dict:
    row = _route(con, harness, selector)
    if row is None:
        return {"ok": False, "error": f"no route for {harness}/{selector}; refresh models"}
    failures = []
    if row["availability"] != "available":
        failures.append(f"source is {row['availability']} ({row['source']}), not local")
    if not row["headless_supported"]:
        failures.append("harness has no headless launch seam")
    if effort == "high" and not row["high_effort_supported"]:
        failures.append("model has no locally verified high-effort route")
    command = _command(harness, selector, shell, effort)
    return {"ok": not failures, "harness": harness, "selector": selector,
            "source": row["source"], "availability": row["availability"],
            "stale": bool(row["stale"]), "cli_version": row["cli_version"],
            "supported_efforts": json.loads(row["supported_efforts"] or "[]"),
            "command": command, "error": "; ".join(failures) or None}


def _print_resolved(data: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(data, indent=2))
    elif not data["ok"]:
        print(f"models: {data['error']}", file=sys.stderr)
    else:
        suffix = " · stale last-known-good" if data["stale"] else ""
        print(f"route: {data['harness']}/{data['selector']} · {data['source']}{suffix}")
        print(f"call:  {shlex.join(data['command'])}")
    return 0 if data["ok"] else 2


def _list(con, harness: str | None) -> int:
    sql = ("SELECT harness, selector, source, availability, stale, "
           "headless_supported, high_effort_supported FROM model_routes")
    params: tuple = ()
    if harness:
        sql += " WHERE harness=?"
        params = (harness,)
    sql += " ORDER BY harness, availability='available' DESC, selector"
    try:
        rows = con.execute(sql, params).fetchall()
    except db_driver.OperationalError:
        raise SystemExit("models: model_routes unavailable — run `sc rebuild` to migrate")
    if not rows:
        model_catalog.catalog(con=con)
        rows = con.execute(sql, params).fetchall()
    for r in rows:
        runnable = (r["availability"] == "available" and r["headless_supported"]
                    and r["high_effort_supported"])
        state = "runnable" if runnable else r["availability"]
        if r["stale"]:
            state += "/stale"
        print(f"{r['harness']}/{r['selector']}\t{state}\t{r['source']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("usage: sc models refresh | list [harness] | "
              "resolve <harness> <selector> [--shell <shortname>] [--json]")
        return 0
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
        if args[0] != "resolve" or len(args) < 3:
            raise SystemExit("models: expected refresh, list, or resolve <harness> <selector>")
        shell = "<shell>"
        if "--shell" in args:
            i = args.index("--shell")
            shell = args[i + 1] if i + 1 < len(args) else ""
            if not shell:
                raise SystemExit("models: --shell requires a shortname")
        data = resolve(con, args[1], args[2], shell=shell)
        return _print_resolved(data, "--json" in args)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
