"""An operator session for a host-side maintainer tool (Feature #94).

The review server answers `/api/*` only to a signed-in operator. The
maintainer tools here play the operator over HTTP, or in a browser, so on an
install with sign-in they need a session. They get one the way the operator
ruled: a host CLI verb, run in the install the tool drives, mints a labelled,
revocable session.

    <install>/sc auth sessions mint --label <tool> --json

It shows in that install's `./sc auth sessions` under the tool's name and is
revoked like any other. This module asks for it, hands the tool the `Cookie`
and the same-origin `Origin` to send, and revokes it when the tool is done:
on a normal exit, on a failure, and on Ctrl-C.

Which install: the one whose review server the tool calls, named by its
checkout path, never this source checkout by assumption. The tools drive a
disposable install of their own, which has no GUI user yet. With `enrol`, the
helper first runs that install's own `./sc auth user add <engine user>` and
mints for that user. Without it, an install with no user is an error that
says what to run.

Only a host-side caller can do any of this: the verbs refuse inside the
sandbox, where the store is absent. Nothing here adds a way past the gate; the
session is an ordinary one that the gate checks as it checks a browser's.

The cookie value is held in memory only. It is never printed, logged, or
written to a file, and `scrub` removes it from text on its way to one.

An engine from before sign-in has no `GET /api/auth/status`; a tool gets a
session object that sends nothing, and runs as it always did.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REDACTED = "[REDACTED]"
# Read from the tool's environment by `sc` verbs that call an engine over
# HTTP. The auth verbs do not, but nothing of the engine this tool was started
# from belongs in a command run in another install.
_FOREIGN_ENV = ("SC_API_TOKEN", "SC_API_BASE")


class SessionUnavailable(RuntimeError):
    """The install requires sign-in and no session could be obtained."""


def _status(base_url: str, *, opener=urllib.request.urlopen,
            timeout: float = 10, headers: dict | None = None) -> dict | None:
    """`GET /api/auth/status` (an open route), or None when it is not the
    sign-in status shape: an older engine, nothing listening, not JSON."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/auth/status", headers=headers or {})
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if (isinstance(payload, dict) and "signed_in" in payload
            and "enrolled" in payload):
        return payload
    return None


def requires_sign_in(base_url: str, *, opener=urllib.request.urlopen,
                     timeout: float = 10) -> bool:
    """Whether the review server at `base_url` gates its API behind sign-in.

    False for an engine that has no such route, and for anything that does
    not answer: a server that is down is some other step's failure to report.
    """
    payload = _status(base_url, opener=opener, timeout=timeout)
    return payload is not None and payload.get("signed_in") is not True


def _origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _tail(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "(no message)"


def _sc(install: Path, argv: list[str], *, run, timeout: float):
    """Run an auth verb of `install`'s own `./sc`, output captured."""
    env = {key: value for key, value in os.environ.items()
           if key not in _FOREIGN_ENV}
    try:
        return (run or subprocess.run)(
            [str(install / "sc"), "auth", *argv], cwd=str(install), env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=max(1.0, timeout), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionUnavailable(
            f"could not run {install / 'sc'} auth {argv[0]}: "
            f"{type(exc).__name__}") from exc


class ToolSession:
    """One minted session and what to send with it. With no token (an install
    without sign-in) it sends nothing and has nothing to revoke."""

    def __init__(self, base_url: str, tool: str, *, install: Path | None = None,
                 session_id: str | None = None, cookie_name: str | None = None,
                 token: str | None = None, run=None,
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.tool = tool
        self.install = install
        self.session_id = session_id
        self.gated = token is not None
        self._cookie_name = cookie_name
        self._token = token
        self._secret = token             # kept for `scrub` after revocation
        self._run = run
        self._timeout = timeout
        self._revoked: bool | None = None

    def __repr__(self) -> str:           # never the cookie
        return f"<ToolSession {self.tool!r} id={self.session_id} gated={self.gated}>"

    def headers(self) -> dict[str, str]:
        """What a same-origin request from the GUI carries: the session
        cookie, and the `Origin` the gate demands of a mutation."""
        if self._token is None:
            return {}
        return {"Cookie": f"{self._cookie_name}={self._token}",
                "Origin": _origin(self.base_url)}

    def browser_cookies(self) -> list[dict[str, str]]:
        """The cookie in the form a Playwright context takes."""
        if self._token is None:
            return []
        return [{"name": self._cookie_name, "value": self._token,
                 "url": _origin(self.base_url)}]

    def scrub(self, text: str) -> str:
        """`text` without the cookie value, for anything headed to a file or
        a terminal."""
        return text.replace(self._secret, REDACTED) if self._secret else text

    def revoke(self) -> bool:
        """End the session. True when it is gone, or there was none. Tried
        once; a failure is reported on stderr with the command to run."""
        if self._revoked is not None or not self.gated:
            return self._revoked is not False
        self._token = None               # stop sending it, whatever follows
        problem = None
        try:
            done = self._run_sc(["sessions", "revoke", self.session_id])
            if done.returncode != 0:
                problem = _tail(done.stderr)
        except SessionUnavailable as exc:
            problem = str(exc)
        self._revoked = problem is None
        if problem is not None:
            print(f"{self.tool}: the tool session {self.session_id} was NOT "
                  f"revoked ({self.scrub(problem)}). End it with: ./sc auth "
                  f"sessions revoke {self.session_id}  (in {self.install})",
                  file=sys.stderr)
        return self._revoked

    def _run_sc(self, argv: list[str]):
        return _sc(self.install, argv, run=self._run, timeout=self._timeout)


def open_session(install: Path | str | None, base_url: str, tool: str, *,
                 enrol: str | None = None, opener=urllib.request.urlopen,
                 run=None, timeout: float = 60.0) -> ToolSession:
    """Mint a session labelled `tool` in `install`, whose review server is at
    `base_url`. The caller revokes it (`ToolSession.revoke`), or uses
    `operator_session`, which does.

    `enrol` names the engine user to create a GUI user for when the install
    has none: for a disposable install the tool itself made. Raises
    `SessionUnavailable`, with what to do, when no session can be had.
    """
    status = _status(base_url, opener=opener)
    if status is None or status.get("signed_in") is True:
        return ToolSession(base_url, tool)
    if install is None or not (Path(install) / "sc").is_file():
        raise SessionUnavailable(
            f"{tool}: the install at {base_url} requires GUI sign-in, and the "
            f"tool was not given that install's checkout ({install}) to mint "
            "a session in. Nothing was changed.")
    install = Path(install)
    if status.get("enrolled") is not True:
        if enrol is None:
            raise SessionUnavailable(
                f"{tool}: the install at {install} requires GUI sign-in and "
                "has no GUI user to mint a session for. Create one there with "
                "./sc auth user add <username>, or name the engine user for "
                "the tool to create. Nothing was changed.")
        port = urlparse(base_url).port
        # stdout carries the new user's one-time claim code: not kept.
        done = _sc(install, ["user", "add", enrol, "--port", str(port)],
                   run=run, timeout=timeout)
        if done.returncode != 0:
            raise SessionUnavailable(
                f"{tool}: could not create the GUI user {enrol!r} in "
                f"{install}: {_tail(done.stderr)}")
    argv = ["sessions", "mint", "--label", tool, "--json"]
    if enrol is not None:
        argv += ["--user", enrol]
    done = _sc(install, argv, run=run, timeout=timeout)
    if done.returncode != 0:
        raise SessionUnavailable(
            f"{tool}: could not mint an operator session in {install}: "
            f"{_tail(done.stderr)}")
    try:
        minted = json.loads(done.stdout)
        session = ToolSession(
            base_url, tool, install=install, session_id=str(minted["id"]),
            cookie_name=str(minted["cookie_name"]),
            token=str(minted["cookie_value"]), run=run, timeout=timeout)
    except (ValueError, KeyError, TypeError):
        # Nothing of what was printed is repeated: it may hold the cookie.
        raise SessionUnavailable(
            f"{tool}: ./sc auth sessions mint in {install} did not answer "
            "with a session. Check ./sc auth sessions there.") from None
    # The session must be one THIS server accepts: a checkout that is not the
    # one serving `base_url` mints a cookie its gate has never heard of.
    seen = _status(base_url, opener=opener, headers=session.headers())
    if seen is None or seen.get("signed_in") is not True:
        session.revoke()
        raise SessionUnavailable(
            f"{tool}: the review server at {base_url} did not accept the "
            f"session minted in {install}. Is that the checkout it serves, "
            "and is its user an active engine user? The session was revoked.")
    return session


@contextlib.contextmanager
def operator_session(install: Path | str | None, base_url: str, tool: str,
                     **options):
    """`open_session`, revoked on the way out: normal exit, exception, or
    Ctrl-C."""
    session = open_session(install, base_url, tool, **options)
    try:
        yield session
    finally:
        session.revoke()
