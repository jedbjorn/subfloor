#!/usr/bin/env python3
"""Read-only schema inspection for the live dr_* repository map."""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import artifact_policy


class MapSchemaError(RuntimeError):
    pass


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MapSchemaError(f"cannot open live map DB read-only at {path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _objects(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE name GLOB 'dr_*' AND type IN ('table', 'view') "
        "ORDER BY name"
    ).fetchall()


def _available_hint(connection: sqlite3.Connection) -> str:
    names = [row["name"] for row in _objects(connection)]
    return ", ".join(names) if names else "(none)"


def list_objects(connection: sqlite3.Connection) -> None:
    for row in _objects(connection):
        print(f"{row['name']}\t{row['type']}")


def describe_object(connection: sqlite3.Connection, name: str) -> None:
    if re.fullmatch(r"dr_[A-Za-z0-9_]+", name) is None:
        raise MapSchemaError("object name must be one exact dr_* identifier")
    object_row = connection.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE name=? AND type IN ('table', 'view')",
        (name,),
    ).fetchone()
    if object_row is None:
        raise MapSchemaError(
            f"unknown map object {name!r}; available: {_available_hint(connection)}"
        )
    columns = connection.execute(
        "SELECT cid, name, type, \"notnull\", dflt_value, pk, hidden "
        "FROM pragma_table_xinfo(?) ORDER BY cid",
        (name,),
    ).fetchall()
    indexes = connection.execute(
        "SELECT name, \"unique\", origin, partial "
        "FROM pragma_index_list(?) ORDER BY name",
        (name,),
    ).fetchall()
    print(f"object: {object_row['name']} ({object_row['type']})")
    print("columns:")
    print("ordinal\tname\ttype\tnullable\tdefault\tprimary_key")
    for column in columns:
        nullable = "no" if column["notnull"] or column["pk"] else "yes"
        default = column["dflt_value"] if column["dflt_value"] is not None else "-"
        print(
            f"{column['cid']}\t{column['name']}\t{column['type'] or '-'}\t"
            f"{nullable}\t{default}\t{column['pk']}"
        )
    print("indexes:")
    print("name\tunique\torigin\tpartial")
    if not indexes:
        print("(none)")
        return
    for index in indexes:
        print(
            f"{index['name']}\t{index['unique']}\t"
            f"{index['origin']}\t{index['partial']}"
        )


def main(argv: list[str]) -> int:
    if argv in (["-h"], ["--help"]):
        print("usage: sc map-schema [dr_table]")
        return 0
    if len(argv) > 1:
        raise MapSchemaError("usage: sc map-schema [dr_table]")
    path = artifact_policy.map_db_path()
    connection = _connect(path)
    try:
        if argv:
            describe_object(connection, argv[0])
        else:
            list_objects(connection)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    try:
        raise SystemExit(run_cli(main, sys.argv[1:]))
    except MapSchemaError as exc:
        raise SystemExit(f"map-schema: {exc}") from exc
