"""The one structural definition of a live sprint (spec #76, H-1/H-2).

Liveness used to be a `status:` line inside `documents.body`, parsed by two
divergent implementations across five sites: a reformatted line killed the wake
path and every armed watch while boot still rendered the sprint live. The
definition now lives here, in structure, once — and the body's `status:` line is
display prose that nothing executable reads.

Three predicates, deliberately distinct. Collapsing any pair reintroduces a
defect this module exists to close:

- `is_sprint_doc` — the identity of a sprint document and nothing more. This is
  the write boundary for message tagging and watch registration (H-2): a
  coordination message sent during the declaration window (board declared,
  units not yet) and a result row sent after close must both stay legal, so
  neither `frozen` nor the unit count may appear here.
- `is_writable_sprint_board` — identity plus `frozen = 0`. A frozen sprint doc's
  board is not mutable. It deliberately omits the unit-count clause, because the
  route it guards CREATES the first unit: gating that on liveness would make
  every board undeclarable.
- `is_live_sprint` / `live_sprint_doc_ids` / `live_sprint_clause` — the full
  predicate. It gates assignment notices, poller scoping and the
  sprint-activity projection, and nothing else.

Ordering consequence, decided rather than discovered: a sprint doc with zero
`sprint_units` rows is not live.

Closing a sprint IS freezing its doc. The freeze route retires its PR watches
in the same transaction.

The boot renderer's directive predicate (`render/compose.py`) is NOT one of
these and must not be folded in: it is role-scoped (a dev retires on its last
non-terminal unit, a planner only on the freeze) and that split is deliberate.
It already reads structure, so it cannot drift from this module the way the
prose parsers did.

Dependency-free on purpose — the API, the poller and the broker all import it,
so no site re-derives a clause that can drift from this one.

Note on the title clause: SQLite's `LIKE` is ASCII-case-insensitive, so
`'Sprint: x'` matches. That is the behaviour every prose parser already had and
it is preserved deliberately — a swap to `GLOB` or a case-sensitive compare
would un-live every mixed-case sprint at once, silently.
"""

_IS_SPRINT_DOC = "{d}.kind='doc' AND {d}.title LIKE 'SPRINT:%'"
_UNFROZEN = "{d}.frozen=0"
_HAS_UNITS = ("EXISTS (SELECT 1 FROM sprint_units u "
              "WHERE u.sprint_doc_id={d}.document_id)")


def sprint_doc_clause(alias: str = "d") -> str:
    """Sprint-document identity as a SQL fragment over a `documents` alias."""
    return _IS_SPRINT_DOC.format(d=alias)


def writable_board_clause(alias: str = "d") -> str:
    """Board-mutability as a SQL fragment over a `documents` alias."""
    return f"{sprint_doc_clause(alias)} AND {_UNFROZEN.format(d=alias)}"


def live_sprint_clause(alias: str = "d") -> str:
    """Liveness as a SQL fragment, for callers that must JOIN the predicate
    rather than probe one id (the sprint-activity projection reads it this
    way, so the projection and the probes cannot disagree)."""
    return f"{writable_board_clause(alias)} AND {_HAS_UNITS.format(d=alias)}"


def _probe(con, doc_id, clause: str) -> bool:
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        return False
    return con.execute(
        f"SELECT 1 FROM documents d WHERE d.document_id=? AND {clause}",
        (doc_id,)).fetchone() is not None


def is_sprint_doc(con, doc_id) -> bool:
    """Does `doc_id` name a sprint document — at any unit count, frozen or not?"""
    return _probe(con, doc_id, sprint_doc_clause())


def is_writable_sprint_board(con, doc_id) -> bool:
    """May this sprint's board be written — i.e. is its doc unfrozen?"""
    return _probe(con, doc_id, writable_board_clause())


def is_live_sprint(con, doc_id) -> bool:
    """Is this sprint live: a sprint doc, unfrozen, holding at least one unit?"""
    return _probe(con, doc_id, live_sprint_clause())


def live_sprint_doc_ids(con) -> "set[int]":
    """Every live sprint's document_id — the poller's whole world."""
    return {row[0] for row in con.execute(
        f"SELECT d.document_id FROM documents d WHERE {live_sprint_clause()}")}


def has_units(con, doc_id) -> bool:
    """Does this document hold a board? The title is part of the liveness
    predicate, so `doc edit` refuses to retitle a doc for which this is true —
    a mid-sprint retitle would otherwise kill liveness silently, which is this
    spec's own defect class moved one field over."""
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        return False
    return con.execute(
        "SELECT 1 FROM sprint_units WHERE sprint_doc_id=? LIMIT 1",
        (doc_id,)).fetchone() is not None
