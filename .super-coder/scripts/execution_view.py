"""Authoritative restricted filesystem/process view for non-Admin shells."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import instance_state


class ExecutionViewError(RuntimeError):
    """A required shell view could not be established safely."""


RESTRICTED_VIEW_ERROR = (
    "restricted_shell_view_unavailable: the required shell execution view "
    "could not be established; launch Admin to repair the Subfloor runtime"
)


@dataclass(frozen=True)
class ExecutionView:
    """One canonical role/repository-mode launch policy.

    ``prefix`` is deliberately carried as parent-owned process state.  It is
    never serialized into the harness environment, where a shell could inspect
    private paths or spoof a later authorization decision.
    """

    mode: str
    prefix: tuple[str, ...] = ()
    masked_paths: tuple[Path, ...] = ()

    @property
    def restricted(self) -> bool:
        return bool(self.prefix)

    def command(self, argv: Sequence[str]) -> list[str]:
        command = [str(value) for value in argv]
        if not command:
            return command
        return [*self.prefix, *command] if self.restricted else command

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        env = {str(key): str(value) for key, value in source.items()}
        if self.restricted:
            env.pop("SC_ROOT", None)
            env.pop("SC_ENGINE_DIR", None)
            env["SC_EXECUTION_VIEW"] = self.mode
        return env

    def preflight(self, *, parent_pid: int | None = None) -> None:
        """Prove the exact wrapper starts and hides its parent's process root."""
        if not self.restricted:
            return
        pid = os.getpid() if parent_pid is None else parent_pid
        probe = (
            "parent=$1; shift; for path in \"$@\"; do "
            "for target in \"$path\" \"/proc/$parent/root$path\"; do "
            "if [ -d \"$target\" ]; then "
            "ls \"$target\" >/dev/null 2>&1 && exit 1; "
            "else cat \"$target\" >/dev/null 2>&1 && exit 1; fi; done; "
            "done; true"
        )
        command = [
            "/bin/sh", "-c", probe, "execution-view", str(pid),
            *(str(path) for path in self.masked_paths),
        ]
        try:
            completed = subprocess.run(
                self.command(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionViewError(RESTRICTED_VIEW_ERROR) from exc
        if completed.returncode != 0:
            raise ExecutionViewError(RESTRICTED_VIEW_ERROR)


def _private_state(engine: Path, environ: Mapping[str, str]) -> instance_state.InstanceState:
    try:
        return instance_state.resolve(
            instance_config=engine / "instance.json",
            environ=environ,
            create=False,
        )
    except instance_state.InstanceStateError as exc:
        raise ExecutionViewError(RESTRICTED_VIEW_ERROR) from exc


def _validate_masks(paths: Sequence[Path]) -> None:
    """Refuse aliases that could place a masked inode below an allowed tree."""
    pending = [Path(path) for path in paths]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ExecutionViewError(RESTRICTED_VIEW_ERROR) from exc
        if stat.S_ISLNK(info.st_mode) or (
            stat.S_ISREG(info.st_mode) and info.st_nlink != 1
        ):
            raise ExecutionViewError(RESTRICTED_VIEW_ERROR)
        if stat.S_ISDIR(info.st_mode):
            try:
                pending.extend(Path(entry.path) for entry in os.scandir(path))
            except OSError as exc:
                raise ExecutionViewError(RESTRICTED_VIEW_ERROR) from exc


def build(
    *,
    engine: Path,
    repo_root: Path,
    flavor: str | None,
    source_mode: bool,
    environ: Mapping[str, str] | None = None,
) -> ExecutionView:
    """Build a view from canonical launcher inputs, never caller role strings."""
    if flavor == "admin":
        return ExecutionView(mode="admin")

    env = os.environ if environ is None else environ
    engine = Path(engine).absolute()
    repo_root = Path(repo_root).absolute()
    private = _private_state(engine, env)
    backups = instance_state.active_backup_paths(repo_root, env)
    paths = [
        private.root,
        engine / "shell_db.db",
        engine / "shell_db.db-wal",
        engine / "shell_db.db-shm",
        engine / "snapshot" / "content.sql",
        repo_root / ".sc-state" / "content.sql",
        repo_root / ".sc-state" / "local" / "content.sql",
        repo_root / ".sc-state" / "local" / ".content-write.lock",
        *backups.candidates,
    ]
    if not source_mode:
        paths.extend((engine / "schema.sql", engine / "migrations"))
    _validate_masks(paths)
    prefix = [
        sys.executable,
        str(Path(__file__).with_name("execution_view_exec.py")),
    ]
    for path in paths:
        prefix.extend(("--mask", str(Path(path).absolute())))
    prefix.append("--")
    mode = "restricted-source" if source_mode else "restricted-downstream"
    return ExecutionView(
        mode=mode,
        prefix=tuple(prefix),
        masked_paths=tuple(Path(path).absolute() for path in paths),
    )
