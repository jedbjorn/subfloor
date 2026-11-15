#!/usr/bin/env python3
"""The engine CLI credential (Feature #94, spec #271).

The review server refuses every `/api/*` request that carries neither a
signed-in browser session nor a credential it accepts. A few `sc` verbs call
`/api/*` over HTTP from a terminal that has no browser session and may have no
shell identity either (`./sc snapshot` and `./sc update` on the host). This is
the credential they present.

* The review server mints a fresh random token at every boot and keeps it in
  memory. Once it holds its port, and not before, it writes the token to
  `.super-coder/run/api/cli-token.<port>` (directory `0700`, file `0600`),
  replacing the file by rename. The file is keyed by the port the server
  bound: a second server for the same checkout on another port publishes its
  own file and leaves this one alone, and a second server that fails to bind
  never publishes. So the file for a port names the token of the server
  listening on that port.
* A caller reads the file for the port it is about to call and sends
  `Authorization: Bearer <token>`.
* Nothing deletes a file: not at shutdown, and not at startup for another
  port, whose server may be alive. A file left by a server that has stopped
  opens nothing, because no server holds that token any more; the next server
  on that port replaces it.
* The server accepts it on the routes in `browser_gate.CLI_ROUTES` and nowhere
  else. It is never a browser session and never the operator.

What it is worth: exactly what a shell Bearer token is worth on those same
routes, which accept either. Anything that can read the checkout as the
owner can read this file, which is the population that could already run
the verb. It exists so the verb does not depend on a shell existing.

The server compares against the value in its memory, never against the file,
so writing the file grants nothing.

Stdlib only.
"""
from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
RUN_DIR = ENGINE / "run" / "api"
FILE_NAME = "cli-token"
PREFIX = "sccli_"
MAX_BYTES = 256


def mint() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def path_for(port: int, run_dir: Path | None = None) -> Path:
    """Where the server listening on `port` publishes its token."""
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError(f"not a TCP port: {port!r}")
    return (run_dir or RUN_DIR) / f"{FILE_NAME}.{port}"


def provision(port: int, run_dir: Path | None = None) -> str:
    """Mint a token, publish it for `port` owner-only, and return it."""
    token = mint()
    publish(token, port, run_dir)
    return token


def publish(token: str, port: int, run_dir: Path | None = None) -> None:
    """Publish `token`, owner-only, as the credential of the server on `port`.

    The file is written as a new inode and renamed over the name, so a link
    planted at the path is replaced and never followed. Only this port's file
    is touched.
    """
    path = path_for(port, run_dir)
    run_dir = path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token.encode("ascii"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(port: int, run_dir: Path | None = None) -> str | None:
    """The token published for `port`, or None when there is none to trust.

    None for a missing file (no server of this checkout has bound that port,
    or an engine older than this credential is running), a link, a file that
    is not a regular file owned by this user, or one readable by group or
    others.
    """
    try:
        fd = os.open(path_for(port, run_dir),
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            return None
        data = os.read(fd, MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not token.startswith(PREFIX) or len(token) <= len(PREFIX):
        return None
    return token


def authorization(port: int, run_dir: Path | None = None) -> dict[str, str]:
    """Request headers that present the credential of the server on `port`;
    empty when none is published for it.

    Empty is deliberate, not an error: an engine from before sign-in publishes
    no token and asks for none, and `./sc update` talks to exactly that engine
    while replacing it. A server that does ask refuses the request, and the
    caller then says `refused(port)`.
    """
    token = read(port, run_dir)
    return {"Authorization": f"Bearer {token}"} if token else {}


def refused(port: int, status: int, run_dir: Path | None = None) -> str:
    """The one message for a caller the server on `port` turned away."""
    path = path_for(port, run_dir)
    if read(port, run_dir) is None:
        return (
            f"the server on port {port} asked for the engine CLI credential "
            f"(HTTP {status}) and none is published for that port at {path}. "
            "Either no server of this checkout is running on that port, or "
            "the server answering there is not this checkout's. Start or "
            "restart this checkout's runtime (`./sc restart`) and retry."
        )
    return (
        f"the server on port {port} refused the engine CLI credential "
        f"published at {path} (HTTP {status}). The file is from a server that "
        "is no longer the one listening there. Restart this checkout's "
        "runtime (`./sc restart`), which publishes its own, and retry."
    )
