"""A signed-in browser for tests (Feature #94, spec #271).

The review API answers `/api/*` only to a session the host auth authority
published. A test that plays the browser operator signs in the way the
authority does on disk: it writes the published `sessions.json` (hashes only)
into a temporary directory, points `auth_sessions` at it, and sends the
session cookie. No authority runs, and nothing is written outside the
temporary directory.

    from signin_fixture import sign_in

    browser = sign_in(self)                  # a unittest.TestCase, or None
    status, headers, body = server.dispatch_http(
        "GET", "/api/shells", browser.raw(), b"")
    browser.revoke()                         # the next request is signed out

For a server in another process, publish into the directory that process
derives (`sign_in(..., directory=...)`) and send `browser.headers()`.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _part in ("api", "scripts"):
    _path = str(REPO / ".super-coder" / _part)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import auth_sessions  # noqa: E402

COOKIE_NAME = "sc_session_0f1e2d3c"
DEFAULT_HOST = "127.0.0.1:8800"


def publish(directory: Path, tokens_by_user: list[tuple[str, int]],
            *, enrolled: bool = True, cookie: str | None = COOKIE_NAME) -> Path:
    """Write `sessions.json` as the authority does: atomically, hashes only."""
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "version": auth_sessions.FILE_VERSION,
        "cookie": cookie,
        "enrolled": enrolled,
        "sessions": [
            {"hash": auth_sessions.token_hash(token), "user_id": user_id}
            for token, user_id in tokens_by_user
        ],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    path = directory / auth_sessions.FILE_NAME
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sessions.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(record, handle)
    os.replace(tmp, path)
    return path


class SignedIn:
    """One browser session, and the headers a browser would send with it."""

    def __init__(self, directory: Path, user_id: int = 1,
                 host: str = DEFAULT_HOST):
        self.directory = directory
        self.user_id = user_id
        self.host = host
        self.cookie_name = COOKIE_NAME
        self.token = secrets.token_urlsafe(32)
        self.hash = auth_sessions.token_hash(self.token)
        self.enrolled = True
        self.publish()

    # -- the published list ---------------------------------------------------
    def publish(self) -> None:
        publish(self.directory, [(self.token, self.user_id)],
                enrolled=self.enrolled)

    def revoke(self) -> None:
        """Remove the hash from the published list, as a revocation does."""
        publish(self.directory, [], enrolled=self.enrolled)

    def path(self) -> Path:
        return self.directory / auth_sessions.FILE_NAME

    # -- what the browser sends ----------------------------------------------
    @property
    def cookie(self) -> str:
        return f"{self.cookie_name}={self.token}"

    def origin(self, host: str | None = None) -> str:
        return f"http://{host or self.host}"

    def headers(self, host: str | None = None, *, origin: bool = True,
                **extra: str) -> dict[str, str]:
        """Request headers of a same-origin request from the GUI.

        `Origin` is included by default: a browser sends it with every
        mutation, and the gate demands it there. It is harmless on a `GET`.
        """
        host = host or self.host
        out = {"Host": host, "Cookie": self.cookie}
        if origin:
            out["Origin"] = self.origin(host)
        out.update(extra)
        return out

    def raw(self, host: str | None = None, *, origin: bool = True,
            **extra: str) -> str:
        """The same headers as the raw block `dispatch_http` takes."""
        return "\r\n".join(
            f"{key}: {value}"
            for key, value in self.headers(host, origin=origin, **extra).items())

    def with_cookie(self, headers_raw: str) -> str:
        """`headers_raw` with the session cookie added and nothing else: for a
        request that already says what `Host` and `Origin` it wants."""
        lines = [line for line in (headers_raw or "").split("\r\n") if line.strip()]
        lines.append(f"Cookie: {self.cookie}")
        return "\r\n".join(lines)

    def add_to(self, headers_raw: str, host: str | None = None) -> str:
        """`headers_raw` with this session added, keeping what it already says.

        Adds `Cookie`; adds `Host` and `Origin` only when the block names none.
        """
        lines = [line for line in (headers_raw or "").split("\r\n") if line.strip()]
        names = {line.split(":", 1)[0].strip().lower() for line in lines}
        named_host = next(
            (line.split(":", 1)[1].strip() for line in lines
             if line.split(":", 1)[0].strip().lower() == "host"), None)
        host = named_host or host or self.host
        if "host" not in names:
            lines.append(f"Host: {host}")
        if "cookie" not in names:
            lines.append(f"Cookie: {self.cookie}")
        if "origin" not in names:
            lines.append(f"Origin: {self.origin(host)}")
        return "\r\n".join(lines)


def sign_in(case=None, *, user_id: int = 1, directory: Path | None = None,
            host: str = DEFAULT_HOST) -> SignedIn:
    """Publish one session and make this process's verifier read it.

    With a `unittest.TestCase` the previous state is restored by a cleanup;
    without one, call `sign_out_all()` when done. Pass `directory` to publish
    where another process will look, in which case this process's verifier is
    left alone.
    """
    if directory is not None:
        return SignedIn(Path(directory), user_id, host)
    tmp = Path(tempfile.mkdtemp(prefix="sc-signin-"))
    previous = auth_sessions.DIR
    auth_sessions.DIR = tmp

    def restore() -> None:
        auth_sessions.DIR = previous
        shutil.rmtree(tmp, ignore_errors=True)

    if case is not None:
        case.addCleanup(restore)
    browser = SignedIn(tmp, user_id, host)
    browser.restore = restore
    return browser


def signed_out(case=None) -> Path:
    """Point the verifier at an empty directory: nobody is signed in."""
    tmp = Path(tempfile.mkdtemp(prefix="sc-signout-"))
    previous = auth_sessions.DIR
    auth_sessions.DIR = tmp

    def restore() -> None:
        auth_sessions.DIR = previous
        shutil.rmtree(tmp, ignore_errors=True)

    if case is not None:
        case.addCleanup(restore)
    return tmp


_LAUNCHER = (
    "import runpy, sys\n"
    "from pathlib import Path\n"
    "api, sessions, script = sys.argv[1:4]\n"
    "sys.path.insert(0, api)\n"
    "import auth_sessions\n"
    "auth_sessions.DIR = Path(sessions)\n"
    "sys.argv = [script] + sys.argv[4:]\n"
    "runpy.run_path(script, run_name='__main__')\n"
)


def server_argv(repo: Path, sessions_dir: Path, *args: str) -> list[str]:
    """argv that runs `repo`'s review server reading sessions from
    `sessions_dir`.

    The verifier takes its directory in code, never from the environment, so
    a test that runs the server as another process sets it in that process
    before the server starts. Publish there with `sign_in(directory=...)`.
    """
    api = Path(repo) / ".super-coder" / "api"
    return [sys.executable, "-c", _LAUNCHER, str(api), str(sessions_dir),
            str(api / "server.py"), *args]
