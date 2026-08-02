"""Authoritative one-active-chat registry operations.

Callers own transaction boundaries.  In particular, chat rotation must commit
``close_active`` before beginning the transaction that inserts and registers a
replacement chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ActiveChatError(RuntimeError):
    """The registry cannot perform the requested state change."""


class ActiveChatBusy(ActiveChatError):
    """The active chat owns queued or running work and cannot rotate."""


@dataclass(frozen=True)
class ActiveChat:
    shell_id: int
    chat_id: str
    state: str
    process_pid: int | None
    process_start_ticks: int | None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    process_group_id: int


def get(con, shell_id: int) -> ActiveChat | None:
    row = con.execute(
        "SELECT a.shell_id,a.chat_id,c.state,a.process_pid,"
        "a.process_start_ticks "
        "FROM active_shell_chats a JOIN conversations c "
        "ON c.conversation_id=a.chat_id WHERE a.shell_id=?",
        (shell_id,),
    ).fetchone()
    if row is None:
        return None
    return ActiveChat(
        int(row["shell_id"]),
        str(row["chat_id"]),
        str(row["state"]),
        int(row["process_pid"]) if row["process_pid"] is not None else None,
        (
            int(row["process_start_ticks"])
            if row["process_start_ticks"] is not None
            else None
        ),
    )


def has_live_process(active: ActiveChat) -> bool:
    """Return true only when the registry pid still names the same process."""
    if active.process_pid is None or active.process_start_ticks is None:
        return False
    return process_identity(str(active.process_pid)) == (
        active.process_pid,
        active.process_start_ticks,
    )


def close_active(con, shell_id: int) -> ActiveChat | None:
    """Close and unlink one shell's active chat inside the caller's write."""
    active = get(con, shell_id)
    if active is None:
        return None
    if active.state in {"queued", "running"}:
        raise ActiveChatBusy(
            f"active chat {active.chat_id} has a turn in progress"
        )
    changed = con.execute(
        "UPDATE conversations SET state='closed',closed_at=datetime('now'),"
        "last_activity_at=datetime('now'),version=version+1 "
        "WHERE conversation_id=? AND state=?",
        (active.chat_id, active.state),
    ).rowcount
    if changed != 1:
        raise ActiveChatError(
            f"active chat {active.chat_id} changed while it was closing"
        )
    # The migration trigger normally clears this row.  The explicit delete is
    # both self-documenting and a fail-loud backstop for partially migrated DBs.
    con.execute(
        "DELETE FROM active_shell_chats WHERE shell_id=? AND chat_id=?",
        (shell_id, active.chat_id),
    )
    if get(con, shell_id) is not None:
        raise ActiveChatError(f"active chat {active.chat_id} did not unlink")
    return active


def close_for_wake(con, shell_id: int) -> ActiveChat | None:
    """Close an idle/stale active chat for New delivery, never a live turn."""
    active = get(con, shell_id)
    if active is None:
        return None
    if has_live_process(active):
        raise ActiveChatBusy(f"active chat {active.chat_id} has a live turn")
    if active.state in {"queued", "running"}:
        message_ids = [
            int(row[0])
            for row in con.execute(
                "SELECT message_id FROM conversation_outbox "
                "WHERE conversation_id=? AND state IN ('pending','claimed')",
                (active.chat_id,),
            )
        ]
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            con.execute(
                "UPDATE conversation_messages SET state='cancelled',"
                "completed_at=datetime('now') WHERE message_id IN (" + marks + ")",
                message_ids,
            )
        con.execute(
            "UPDATE conversation_outbox SET state='cancelled',claim_owner=NULL,"
            "claimed_at=NULL,lease_expires_at=NULL "
            "WHERE conversation_id=? AND state IN ('pending','claimed')",
            (active.chat_id,),
        )
        target = "idle" if active.state == "queued" else "error"
        con.execute(
            "UPDATE conversations SET state=?,last_activity_at=datetime('now'),"
            "version=version+1 WHERE conversation_id=? AND state=?",
            (target, active.chat_id, active.state),
        )
    return close_active(con, shell_id)


def register(con, shell_id: int, chat_id: str) -> None:
    con.execute(
        "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (?,?)",
        (shell_id, chat_id),
    )


def process_details(process_ref: str | None) -> ProcessIdentity | None:
    """Resolve a numeric native process ref from one Linux procfs snapshot."""
    if process_ref is None or not process_ref.isdecimal():
        return None
    pid = int(process_ref)
    if pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields_after_comm = stat.rsplit(")", 1)[1].split()
        process_group_id = int(fields_after_comm[2])
        start_ticks = int(fields_after_comm[19])
    except (IndexError, OSError, ValueError):
        return None
    if process_group_id <= 0 or start_ticks < 0:
        return None
    return ProcessIdentity(pid, start_ticks, process_group_id)


def process_identity(process_ref: str | None) -> tuple[int | None, int | None]:
    """Resolve the registry's pid/start-ticks identity pair."""
    details = process_details(process_ref)
    if details is None:
        return None, None
    return details.pid, details.start_ticks


def set_process(
    con,
    *,
    shell_id: int,
    chat_id: str,
    pid: int | None,
    start_ticks: int | None,
) -> None:
    changed = con.execute(
        "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=?,"
        "updated_at=datetime('now') WHERE shell_id=? AND chat_id=?",
        (pid, start_ticks, shell_id, chat_id),
    ).rowcount
    if changed != 1:
        raise ActiveChatError(
            f"chat {chat_id} is not active for shell {shell_id}"
        )


def clear_process(con, *, shell_id: int, chat_id: str) -> bool:
    changed = con.execute(
        "UPDATE active_shell_chats SET process_pid=NULL,"
        "process_start_ticks=NULL,updated_at=datetime('now') "
        "WHERE shell_id=? AND chat_id=?",
        (shell_id, chat_id),
    ).rowcount
    return changed == 1
