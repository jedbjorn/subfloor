"""Launch-owned GUI/TUI seat; independent of the host/container execution seat."""
from __future__ import annotations


def select(flavor: str | None, *, headless: bool) -> str | None:
    if flavor == "admin":
        return None
    return "gui" if headless else "tui"


def inject(env: dict[str, str], seat: str | None) -> None:
    # A nested Admin launch must not inherit the invoking shell's seat.
    env.pop("SC_SEAT", None)
    if seat is not None:
        env["SC_SEAT"] = seat


def boot_block(flavor: str | None, seat: str | None, conversion: dict | None = None) -> str:
    if flavor == "admin" or seat is None:
        return ""
    if seat == "tui":
        return (
            "\n\n## SEAT\n\n"
            "You hold a terminal. Use your harness's native background tools freely; "
            "they notify you in session. `sc job` is optional. "
            "Wakes reach you only at your next boot."
        )
    if seat != "gui":
        raise ValueError(f"invalid launch seat: {seat!r}")
    block = (
        "\n\n## SEAT\n\n"
        "You run headless. Native background tasks die when your turn ends. "
        "Long work goes through `sc job`; dev-kit hooks are wrapped for you; "
        "completion wakes you. Do not schedule in-session timers."
    )
    if conversion and conversion.get("tier") == "disarmed":
        block += ("\n\nClaude background conversion is disarmed for "
                  f"{conversion.get('version') or 'this installation'}: "
                  f"{conversion.get('error') or 'hook behavior was not verified'}. "
                  "Start long work with `sc job` yourself.")
    return block


def conversion_status(harness: str, seat: str | None, *, render_only: bool) -> dict | None:
    if harness != "claude" or seat != "gui":
        return None
    import seat_conversion

    return seat_conversion.current() if render_only else seat_conversion.ensure()


def inject_conversion(env: dict[str, str], conversion: dict | None) -> None:
    env.pop("SC_SEAT_CONVERSION_EVIDENCE", None)
    if conversion and conversion.get("evidence_path"):
        env["SC_SEAT_CONVERSION_EVIDENCE"] = conversion["evidence_path"]


def live_seat(shortname: str, flavor: str | None, *, snapshot: dict | None = None) -> str | None:
    """Read only SC_SEAT from an incarnation-checked live harness environment.

    Old launches have no marker; absence or conflicting holders means unknown,
    never an inferred TUI seat. The liveness scan already scopes pids to this
    instance's worktrees and same-user readable processes.
    """
    if flavor == "admin":
        return None
    import shell_liveness

    snapshot = shell_liveness.compute() if snapshot is None else snapshot
    seats = set()
    for process in snapshot.get("processes", []):
        if (process.get("shortname") or "").lower() != shortname.lower():
            continue
        pid, ticks = process["pid"], process.get("start_ticks")
        if ticks is None or shell_liveness._start_ticks(pid) != ticks:
            continue
        try:
            environment = (shell_liveness.PROC / str(pid) / "environ").read_bytes()
        except OSError:
            continue
        if shell_liveness._start_ticks(pid) != ticks:
            continue
        for entry in environment.split(b"\0"):
            if entry in {b"SC_SEAT=gui", b"SC_SEAT=tui"}:
                seats.add(entry.split(b"=", 1)[1].decode("ascii"))
    return seats.pop() if len(seats) == 1 else None
