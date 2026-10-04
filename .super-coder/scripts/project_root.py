"""Caller-root resolution for project-subject commands (spec #267, U3).

Every checkout of an install dispatches the LIVE engine floor, so a script's
own location (``ENGINE.parent``) is the main checkout even when the operator
typed the command in a shell's linked worktree. A command whose subject is the
shell's PROJECT — its dev-kit hooks, its visual-QA app, the tree the
Cartographer maps, a body file the operator names — must therefore resolve from
the checkout that invoked ``sc`` instead. The dispatcher classifies each verb
and, for project-subject verbs only, exports three variables for the one
command it runs:

``SC_PROJECT_ROOT``
    the checkout holding the ``sc`` that was invoked (the caller root);
``SC_INVOCATION_CWD``
    the operator's working directory before the dispatcher moved to that root,
    against which a relative FILE argument resolves;
``SC_PROJECT_ENGINE``
    the engine directory the dispatcher executed. The values are honored only
    by that engine's own scripts, so an inherited copy never redirects a
    different engine (a test fixture's copy, a worktree's source engine).

Without them — a direct ``python3 .super-coder/scripts/<x>.py`` run, a test, a
live-instance verb — the project is the checkout that holds this engine and
relative paths resolve against the process cwd, exactly as before.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
ROOT_VAR = "SC_PROJECT_ROOT"
CWD_VAR = "SC_INVOCATION_CWD"
ENGINE_VAR = "SC_PROJECT_ENGINE"
VARIABLES = (ROOT_VAR, CWD_VAR, ENGINE_VAR)


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _bound(env: Mapping[str, str]) -> bool:
    raw = env.get(ENGINE_VAR)
    if not raw:
        return False
    try:
        return Path(raw).resolve() == ENGINE
    except OSError:
        return False


def _exported_dir(env: Mapping[str, str], name: str) -> Path | None:
    if not _bound(env):
        return None
    raw = env.get(name)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() and path.is_dir() else None


def project_root(environ: Mapping[str, str] | None = None) -> Path:
    """The checkout a project-subject command acts on."""
    exported = _exported_dir(_environ(environ), ROOT_VAR)
    return exported.resolve() if exported is not None else ENGINE.parent


def invocation_cwd(environ: Mapping[str, str] | None = None) -> Path:
    """The directory the operator typed the command in."""
    exported = _exported_dir(_environ(environ), CWD_VAR)
    return exported if exported is not None else Path.cwd()


def invocation_path(raw: str | os.PathLike[str],
                    environ: Mapping[str, str] | None = None) -> Path:
    """An operator-named file: absolute as given, relative to the invocation cwd."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else invocation_cwd(environ) / path


def scrubbed(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child environment without the one-command project identity.

    Fork hooks, app servers and supervised jobs are not this command; a nested
    engine script they start must resolve its own project, never inherit ours.
    """
    return {k: v for k, v in _environ(environ).items() if k not in VARIABLES}
