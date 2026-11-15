"""Launch-owned GUI/TUI seat; independent of the host/container execution seat."""
from __future__ import annotations


def select(flavor: str | None, *, headless: bool) -> str | None:
    if flavor == "admin":
        return None
    return "gui" if headless else "tui"


def inject(env: dict[str, str], seat: str | None) -> None:
    # A nested Admin launch must not inherit the invoking shell's seat.
    env.pop("SC_SEAT", None)
    # A shell session started from inside a subagent's process tree is not that
    # subagent: left set, its own subagent launches would be refused as nested.
    env.pop("SC_JOB_AGENT_RUN", None)
    if seat is not None:
        env["SC_SEAT"] = seat


GUI_SEAT = """## SEAT — GUI

The engine launched you into a browser chat (or a headless `./sc run`). The
FnB reads your replies in the web UI, and each turn ends when you stop.

- **Background work.** Native background tasks and subagents die when your
  turn ends. Run long work with `sc job start`, subagents with `sc job agent`
  (`delegation` skill), and bounded waits with `sc job start --until`,
  `sc job watch pr`, or `sc job watch timer`. Dev-kit hooks are wrapped for
  you. Do not schedule in-session timers.
- **Wakes.** Engine messages reach you at a turn boundary, into this idle
  chat, or as a new chat. A finished job or watch wakes you, so end your turn
  after starting one.
- **Watching.** The engine's wakes are your watcher. PR events for PRs you
  opened arrive on their own; for any other PR use one `sc job watch pr`.
  Never loop on `gh` in your turn.
- **Orchestration.** Delegate through `sc job agent`."""

CLI_SEAT = """## SEAT — CLI

The FnB launched you in a terminal session (`./sc enter`) and is typing to you
directly.

- **Background work.** Your harness's native background tools and subagents
  work here and notify you in session. `sc job` is optional.
- **Wakes.** Nothing the engine sends interrupts this session; messages wait
  for your next boot. Run `sc mem message check` when you expect one.
- **Watching.** Set up your own bounded monitors for the PRs, checks, and
  jobs you are waiting on, and stop them when you finish.
- **Orchestration.** For in-chat delivery with your harness's agents, use
  `cli_orchestration`; `sc job agent` also works here."""


def boot_block(flavor: str | None, seat: str | None, conversion: dict | None = None) -> str:
    """The launch seat's one `## SEAT — GUI|CLI` section; empty for Admin."""
    if flavor == "admin" or seat is None:
        return ""
    if seat == "tui":
        return CLI_SEAT
    if seat != "gui":
        raise ValueError(f"invalid launch seat: {seat!r}")
    block = GUI_SEAT
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


def _shell_process_seats(shortname: str, snapshot: dict) -> list[str | None]:
    """SC_SEAT of each incarnation-checked live harness process of one shell.

    None marks a live process without a marker (launched before seats existed).
    The liveness scan already scopes pids to this instance's worktrees and
    same-user readable processes; a process that exits mid-read is skipped.
    """
    import shell_liveness

    seats: list[str | None] = []
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
        marker = None
        for entry in environment.split(b"\0"):
            if entry in {b"SC_SEAT=gui", b"SC_SEAT=tui"}:
                marker = entry.split(b"=", 1)[1].decode("ascii")
        seats.append(marker)
    return seats


def live_seat(shortname: str, flavor: str | None, *, snapshot: dict | None = None) -> str | None:
    """The one seat a shell is live in, for display.

    Old launches have no marker; absence or conflicting holders means unknown,
    never an inferred TUI seat.
    """
    if flavor == "admin":
        return None
    import shell_liveness

    snapshot = shell_liveness.compute() if snapshot is None else snapshot
    seats = {seat for seat in _shell_process_seats(shortname, snapshot) if seat}
    return seats.pop() if len(seats) == 1 else None


def live_seats(shortname: str | None, flavor: str | None, *,
               snapshot: dict | None = None) -> frozenset[str] | None:
    """Seats of every live process of one shell; None when unknown.

    Empty = nothing live. Unknown = Admin, no shortname, liveness unsupported,
    or a live process without a seat marker: skill filtering must then fall back
    to the full grant set rather than hide a skill from a session it cannot see.
    """
    if flavor == "admin" or not shortname:
        return None
    import shell_liveness

    snapshot = shell_liveness.compute() if snapshot is None else snapshot
    if not snapshot.get("supported", False):
        return None
    seats = _shell_process_seats(shortname, snapshot)
    if None in seats:
        return None
    return frozenset(seats)


def launch_seats(shortname: str | None, flavor: str | None, seat: str | None, *,
                 snapshot: dict | None = None) -> frozenset[str] | None:
    """Seat set a launch projects skills for: its own seat plus live other seats.

    Both seats of one shell share a worktree, so a GUI launch must not delete a
    CLI-only skill from under a live CLI session of the same shell.
    """
    if seat is None:
        return None
    others = live_seats(shortname, flavor, snapshot=snapshot)
    if others is None:
        return None
    return frozenset({seat}) | others
