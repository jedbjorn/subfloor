"""One launch policy per seat: which engine paths a harness environment carries.

Decision #427 retired the Landlock execution view. Every shell runs as a plain
same-user process; nothing wraps the harness argv and no launch is refused on
view grounds. What survives is one environment guard, kept by FnB decision:
non-Admin seats do not receive ``SC_ENGINE_DIR`` or ``SC_ROOT``. It prevents a
specific confusion: a shell that is handed the main checkout's engine and root
paths ``cd``s there and then edits, commits or runs ``./sc`` against the stale
default-branch tree instead of its own worktree. Engine state is unadvertised
behind the API (decision #428) rather than masked.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ADMIN = "admin"
SHELL = "shell"
RENDER_ONLY = "render-only"
LABELS = (ADMIN, SHELL, RENDER_ONLY)

# The maintenance paths only an Admin seat receives.
MAINTENANCE_VARIABLES = ("SC_ENGINE_DIR", "SC_ROOT")


@dataclass(frozen=True)
class ExecutionView:
    """The launch label for one seat; it never alters the harness argv."""

    mode: str

    @property
    def maintenance_environment(self) -> bool:
        """True when this seat receives the engine maintenance paths."""
        return self.mode == ADMIN

    def command(self, argv: Sequence[str]) -> list[str]:
        return [str(value) for value in argv]

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        env = {str(key): str(value) for key, value in source.items()}
        if not self.maintenance_environment:
            for name in MAINTENANCE_VARIABLES:
                env.pop(name, None)
        return env


def build(*, flavor: str | None, render_only: bool = False) -> ExecutionView:
    """Label a seat from its canonical shell flavor, never a caller string.

    The label does not depend on source or downstream repository mode: both
    launch every flavor the same way.
    """
    if render_only:
        return ExecutionView(mode=RENDER_ONLY)
    return ExecutionView(mode=ADMIN if flavor == ADMIN else SHELL)
