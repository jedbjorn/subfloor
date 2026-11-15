#!/usr/bin/env python3
"""Recognising a signed-in browser (Feature #94, spec #271).

The host auth authority (the engine-ops service, `host_auth.py`) signs the
operator in and keeps the credentials and the session records in host-private
state. This module is the other half: what anything that only VERIFIES needs,
and nothing more. The review server uses it under both runtimes; the authority
uses its cookie helpers so the cookie is spelled in one place.

The authority publishes one file, `sessions.json`, in a directory of its own:

    {"version": 1,
     "cookie": "sc_session_1a2b3c4d",      the cookie's name for this install
     "enrolled": true,                     a GUI user exists
     "sessions": [{"hash": "<sha256 hex of a session token>", "user_id": 1}],
     "updated_at": "2026-01-01T00:00:00Z"}

It holds no secret: a SHA-256 of a 256-bit random token lets a reader
recognise that token and gives it nothing to sign in with. Where it is:

* on the host: `sandbox_devkit.auth_sessions_dir(<checkout>)`, a sibling of
  the engine-ops private state and never inside it;
* in the docker sandbox: that directory, and nothing else of the authority's,
  mounted read-only at `sandbox_devkit.AUTH_SESSIONS_TARGET`.

A missing or unreadable file means no sessions: nobody is signed in.

The one call a verifier makes:

    session = auth_sessions.session_from_cookie_header(headers.get("Cookie"))
    # -> {"user_id": 1, "hash": "..."} or None

Stdlib only. This module never writes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent

FILE_NAME = "sessions.json"
FILE_VERSION = 1
MAX_FILE_BYTES = 1024 * 1024
# Must equal sandbox_devkit.AUTH_SESSIONS_TARGET (a test holds them together);
# spelled here so the in-container reader imports nothing to find its file.
SANDBOX_DIR = Path("/run/super-coder/auth-sessions")
# The published directory. None = derived (`sessions_dir`). Tests point it at
# a temporary directory, in code.
DIR: Path | None = None

COOKIE_PREFIX = "sc_session_"
# Browsers cap a cookie's lifetime at about 400 days; the engine never expires
# a session, so the cookie is re-issued with this lifetime as it is used.
COOKIE_MAX_AGE = 400 * 24 * 60 * 60
# A session token is 32 random bytes, URL-safe base64 without padding.
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_COOKIE_NAME = re.compile(r"^sc_session_[0-9a-f]{8}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
# How many cookies of the right name one request may present. A browser sends
# one; more are a client stuffing guesses into a single request.
MAX_PRESENTED = 4

_cache_lock = threading.Lock()
_cache: tuple[tuple, dict] | None = None


def in_sandbox() -> bool:
    return bool(os.environ.get("SC_SANDBOX")) or any(
        os.path.exists(marker) for marker in ("/.dockerenv", "/run/.containerenv"))


def sessions_dir() -> Path:
    """The directory `sessions.json` is read from, here."""
    if DIR is not None:
        return DIR
    if in_sandbox():
        return SANDBOX_DIR
    scripts = str(ENGINE / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import sandbox_devkit  # noqa: PLC0415  (the one derivation of the path)

    return sandbox_devkit.auth_sessions_dir(REPO_ROOT)


def token_hash(token: str) -> str:
    """What is stored and published for a session token: its SHA-256, hex."""
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def well_formed(token) -> bool:
    return isinstance(token, str) and bool(_TOKEN.match(token))


def _empty() -> dict:
    return {"version": FILE_VERSION, "cookie": None, "enrolled": False,
            "sessions": [], "updated_at": None}


def _normalise(record) -> dict:
    """The published record with only what this module understands in it."""
    if not isinstance(record, dict) or record.get("version") != FILE_VERSION:
        return _empty()
    cookie = record.get("cookie")
    sessions = []
    for entry in record.get("sessions") or []:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("hash")
        if isinstance(digest, str) and _HASH.match(digest):
            sessions.append({"hash": digest, "user_id": entry.get("user_id")})
    return {
        "version": FILE_VERSION,
        "cookie": cookie if isinstance(cookie, str) and _COOKIE_NAME.match(cookie) else None,
        "enrolled": record.get("enrolled") is True,
        "sessions": sessions,
        "updated_at": record.get("updated_at"),
    }


def read_public() -> dict:
    """What the authority has published; the empty record when it has not.

    The file is replaced atomically by the authority, so each call checks its
    identity (inode, size, times) and re-reads only when that changed: a
    revocation is seen by the next request. A link is never followed.
    """
    global _cache
    path = sessions_dir() / FILE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return _empty()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return _empty()
        signature = (str(path), info.st_dev, info.st_ino, info.st_size,
                     info.st_mtime_ns, info.st_ctime_ns)
        with _cache_lock:
            if _cache is not None and _cache[0] == signature:
                return _cache[1]
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        return _empty()
    finally:
        os.close(fd)
    try:
        record = _normalise(json.loads(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return _empty()
    with _cache_lock:
        _cache = (signature, record)
    return record


def cookie_name() -> str | None:
    """This install's session cookie name, or None before the authority has
    published one (then no session exists either)."""
    return read_public()["cookie"]


def enrolled() -> bool:
    """Whether a GUI user exists on this install (awaiting or active)."""
    return read_public()["enrolled"]


def verify_token(token) -> dict | None:
    """The published session a presented token belongs to, or None.

    Every published hash is compared, in constant time each, with no early
    exit. A hash is not a token: presenting a published hash matches nothing,
    because what is compared is the hash OF what was presented.
    """
    if not well_formed(token):
        return None
    presented = token_hash(token)
    found = None
    for entry in read_public()["sessions"]:
        if hmac.compare_digest(entry["hash"], presented):
            found = entry
    return dict(found) if found else None


def presented_tokens(cookie_header, name: str | None) -> list[str]:
    """The well-formed values a `Cookie` header carries under `name`."""
    if not name or not isinstance(cookie_header, str):
        return []
    tokens: list[str] = []
    for part in cookie_header.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name and well_formed(value) and value not in tokens:
            tokens.append(value)
            if len(tokens) >= MAX_PRESENTED:
                break
    return tokens


def session_from_cookie_header(cookie_header) -> dict | None:
    """THE check for a verifier: the session a request's `Cookie` header
    proves, as `{"user_id", "hash"}`, or None when it proves none.

    Tolerant of everything: no header, no file, a file that is not the
    authority's format, or a cookie of another install on the same host
    (cookies are scoped by host, not by port) all mean "not signed in".
    """
    for token in presented_tokens(cookie_header, cookie_name()):
        session = verify_token(token)
        if session is not None:
            return session
    return None


# ── the cookie, spelled once ─────────────────────────────────────────────────

def set_cookie(name: str, token: str) -> str:
    """The `Set-Cookie` value that issues, or renews, a session cookie.

    `HttpOnly` (no script reads it), `SameSite=Strict`, `Path=/`, and the
    longest lifetime browsers keep. No `Domain`, so it is bound to the exact
    host the browser used (`127.0.0.1` or `localhost`); cookies ignore ports,
    which is why the review API and the authority both receive it. No `Secure`:
    the GUI is served over loopback http, and browsers disagree about `Secure`
    cookies there. A deployment over TLS adds it (out of scope here).
    """
    return (f"{name}={token}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; "
            "SameSite=Strict")


def clear_cookie(name: str) -> str:
    """The `Set-Cookie` value that removes the session cookie."""
    return f"{name}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
