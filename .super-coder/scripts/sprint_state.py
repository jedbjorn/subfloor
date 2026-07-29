"""Authoritative sprint identity, writeability, and active-state predicates.

The ``sprints`` row is the only executable identity/state source. Document
titles and bodies are display content; unit count is board content. Keeping
those concerns separate lets a newly declared zero-unit sprint accept board
writes while only an explicit ``active`` state enters runtime projections.
"""

_IS_SPRINT_DOC = (
    "EXISTS (SELECT 1 FROM sprints sp "
    "WHERE sp.sprint_doc_id={d}.document_id)"
)
_WRITABLE = (
    "EXISTS (SELECT 1 FROM sprints sp "
    "WHERE sp.sprint_doc_id={d}.document_id "
    "AND sp.state IN ('declared','active')) "
    "AND {d}.frozen=0"
)
_ACTIVE = (
    "EXISTS (SELECT 1 FROM sprints sp "
    "WHERE sp.sprint_doc_id={d}.document_id AND sp.state='active') "
    "AND {d}.frozen=0"
)


def sprint_doc_clause(alias: str = "d") -> str:
    """Sprint-document identity as a SQL fragment over a `documents` alias."""
    return _IS_SPRINT_DOC.format(d=alias)


def writable_board_clause(alias: str = "d") -> str:
    """Declared/active board-mutability over a ``documents`` alias."""
    return _WRITABLE.format(d=alias)


def live_sprint_clause(alias: str = "d") -> str:
    """Authoritative active state as a SQL fragment."""
    return _ACTIVE.format(d=alias)


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
    """Is this sprint explicitly active and unfrozen?"""
    return _probe(con, doc_id, live_sprint_clause())


def live_sprint_doc_ids(con) -> "set[int]":
    """Every live sprint's document_id — the poller's whole world."""
    return {row[0] for row in con.execute(
        f"SELECT d.document_id FROM documents d WHERE {live_sprint_clause()}")}


def has_units(con, doc_id) -> bool:
    """Does this sprint document currently hold at least one board unit?"""
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        return False
    return con.execute(
        "SELECT 1 FROM sprint_units WHERE sprint_doc_id=? LIMIT 1",
        (doc_id,)).fetchone() is not None
