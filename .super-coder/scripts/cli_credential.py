#!/usr/bin/env python3
"""The engine CLI credential (Feature #94, spec #271).

The review server refuses every `/api/*` request that carries neither a
signed-in browser session nor a credential it accepts. A few `sc` verbs call
`/api/*` over HTTP from a terminal that has no browser session and may have no
shell identity either (`./sc snapshot` and `./sc update` on the host). This is
the credential they present.

* The review server mints a fresh random token at every boot and keeps it in
  memory. Once it holds its port, and not before, it writes the token to
  `.super-coder/run/api/cli-token` (directory `0700`, file `0600`), replacing
  the file by rename. A second server that fails to bind never publishes, so
  the file names the token of the server that is listening. Nothing deletes
  the file at shutdown.
* A caller reads the file and sends `Authorization: Bearer <token>`.
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


def provision(run_dir: Path | None = None) -> str:
    """Mint a token, publish it owner-only, and return it."""
    token = mint()
    publish(token, run_dir)
    return token


def publish(token: str, run_dir: Path | None = None) -> None:
    """Publish `token` owner-only.

    The file is written as a new inode and renamed over the name, so a link
    planted at the path is replaced and never followed.
    """
    run_dir = run_dir or RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=f".{FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token.encode("ascii"))
        os.replace(tmp, run_dir / FILE_NAME)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(run_dir: Path | None = None) -> str | None:
    """The published token, or None when there is none that can be trusted.

    None for a missing file (no server has booted here, or an engine older
    than this credential is running), a link, a file that is not a regular
    file owned by this user, or one readable by group or others.
    """
    path = (run_dir or RUN_DIR) / FILE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def authorization(run_dir: Path | None = None) -> dict[str, str]:
    """Request headers that present the credential; empty when there is none.

    Empty is deliberate, not an error: an engine from before sign-in publishes
    no token and asks for none, and `./sc update` talks to exactly that engine
    while replacing it.
    """
    token = read(run_dir)
    return {"Authorization": f"Bearer {token}"} if token else {}
