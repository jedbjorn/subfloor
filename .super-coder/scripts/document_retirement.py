#!/usr/bin/env python3
"""Document retirement reads shared by every surface (spec doc #251).

One implementation of the two rules the API reads and the task-context
projection both apply, so neither carries a second copy:

  retirement_projection   The column-tolerant SELECT fragment. A DB that
                          predates migration 0268 reads every document as
                          current (`retired` 0, the rest NULL).
  resolve_current         The chain rule: the first non-retired document
                          reachable through `superseded_by`.

Read-only and import-light: `api/server.py` and `scripts/task_context.py` both
import this module; it imports neither.
"""
from __future__ import annotations

DOCUMENT_CHAIN_HOPS = 10   # bound on superseded_by chain resolution

# Retirement (migration 0268). Same tolerance contract as the runtime-advisory
# flag columns in the API: the assemblers must still assemble against a DB that
# predates the migration, reading every document as current.
RETIREMENT_DEFAULTS = {
    "retired": "0",
    "retired_date": "NULL",
    "superseded_by": "NULL",
}


def document_columns(con) -> set[str]:
    return {row[1] for row in con.execute("PRAGMA table_info(documents)")}


def retirement_projection(columns: set[str], *, alias: str = "d") -> str:
    return ", ".join(
        f"{alias}.{name}"
        if name in columns
        else f"{RETIREMENT_DEFAULTS[name]} AS {name}"
        for name in RETIREMENT_DEFAULTS
    )


def resolve_current(by_id: dict, document_id):
    """The first non-retired document reachable through superseded_by.

    Bounded to DOCUMENT_CHAIN_HOPS. A pointer at a row that no longer exists
    is the end of the chain, not a failure: callers still show the raw
    pointer id, just without a title.
    """
    seen = set()
    node = document_id
    for _ in range(DOCUMENT_CHAIN_HOPS):
        row = by_id.get(node)
        if row is None or node in seen:
            return None
        if not row["retired"]:
            return row
        seen.add(node)
        node = row["superseded_by"]
        if node is None:
            return None
    return None
