"""Who is calling an engine maintenance step: one answer for every Admin gate.

Engine SQL (``engine_sql.py``), shared-instance serialization (``snapshot`` and
``render flat`` through ``_serialize_guard.py``) and map-setup's owner update
bridge (``map_setup.py``) all ask the same question through ``resolve``.

Identity derives from the launched shell's bearer token, not from a
self-declared flavor string or an "I am Admin" flag:

1. ``SC_API_TOKEN`` with ``SC_API_BASE`` -> the API's ``/_sc/mem/whoami``
   names the flavor.
2. The API is unreachable -> a read-only lookup of the token in the canonical
   active engine DB, so an API-down host Admin can still diagnose and
   serialize.
3. No ``SC_API_TOKEN`` at all -> the caller is not a launched shell. It is the
   host operator's seat or an engine-internal process (install, update, the
   API server's own subprocesses, render-check, verify), and it is admitted by
   the serialization and update-bridge gates.

Rule 3 is deliberate (decision #428). These gates are confusion rails for
capable collaborators, not containment: a launched shell always carries its
token, so a shell only reaches rule 3 by stripping its own identity — the same
class of deliberate step-around as ``git commit --no-verify``, which #428
accepts for rails.

One self-declared input remains and is accepted on the same terms:
``SC_API_BASE`` is caller-controlled, so a process that points it at a stub
answering ``admin`` to ``/whoami`` passes the gate. That takes a deliberate
act against one's own environment, the same step-around class as stripping
the token; this module is a rail against confusion, not proof of identity.

Engine-internal callers that run on behalf of the engine
pass their children ``engine_internal_env`` so an operator's ambient token
never decides whether the engine may maintain itself.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import instance_state

ENGINE = Path(__file__).resolve().parents[1]
ADMIN = "admin"
# The bearer identity a launched shell carries. Dropping both makes a child an
# engine-internal process rather than a shell acting under its own identity.
SHELL_IDENTITY_VARIABLES = ("SC_API_TOKEN", "SC_API_BASE")


@dataclass(frozen=True)
class Caller:
    """The resolved caller. ``flavor`` is None when it could not be resolved."""

    token: str
    flavor: str | None

    @property
    def launched_shell(self) -> bool:
        return bool(self.token)

    @property
    def admin(self) -> bool:
        return self.flavor == ADMIN

    @property
    def maintains_instance(self) -> bool:
        """True for the host operator/engine-internal seat and for Admin."""
        return not self.launched_shell or self.admin


def api_flavor(token: str, base: str) -> str | None:
    """The token's flavor from the live API, or None when it is unreachable."""
    if not token or not base:
        return None
    request = urllib.request.Request(
        base.rstrip("/") + "/_sc/mem/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read()).get("flavor")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def local_flavor(token: str, db_path: Path) -> str | None:
    """The token's flavor from a read-only open of the engine DB, or None."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT flavor FROM shells WHERE api_key=? "
                "AND COALESCE(is_deleted,0)=0",
                (token,),
            ).fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None
    return None if row is None else row[0]


def _active_db(engine: Path) -> Path | None:
    try:
        return instance_state.active_database_path(engine)
    except (OSError, instance_state.InstanceStateError):
        return None


def resolve(
    environ: Mapping[str, str] | None = None,
    *,
    token: str | None = None,
    base: str | None = None,
    db_path: Callable[[], Path | None] | None = None,
) -> Caller:
    """Resolve the caller from its bearer token (see the module docstring).

    ``token``/``base`` override the environment for a caller that obtained
    its identity another way (engine SQL's Admin credential discovery).
    ``db_path`` is called only when the API cannot answer.
    """
    env = os.environ if environ is None else environ
    token = env.get("SC_API_TOKEN", "") if token is None else token
    base = env.get("SC_API_BASE", "") if base is None else base
    if not token:
        return Caller(token="", flavor=None)
    flavor = api_flavor(token, base)
    if flavor is not None:
        return Caller(token=token, flavor=flavor)
    path = (db_path or (lambda: _active_db(ENGINE)))()
    return Caller(token=token, flavor=None if path is None else local_flavor(token, path))


def refusal(op: str, confusion: str) -> str:
    """One refusal: the confusion first, then who may run it, then the remedy."""
    return (
        f"{op}: refused — {confusion}\n"
        f"  {op} is an Admin step: run it from the Admin seat in the main "
        "checkout, or use the GUI's Save locally."
    )


def require_maintainer(op: str, confusion: str,
                       environ: Mapping[str, str] | None = None) -> Caller:
    """Exit unless the caller is Admin, the host operator, or the engine."""
    caller = resolve(environ)
    if not caller.maintains_instance:
        raise SystemExit(refusal(op, confusion))
    return caller


def engine_internal_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child environment that acts as the engine, not as a launched shell."""
    env = dict(os.environ if environ is None else environ)
    for name in SHELL_IDENTITY_VARIABLES:
        env.pop(name, None)
    return env
