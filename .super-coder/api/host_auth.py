#!/usr/bin/env python3
"""The host auth authority: who may use the review GUI (Feature #94, spec #271).

The engine-ops service (`engine_ops_service.py`) is the authority; this module
is its core and the `./sc auth` verbs. It owns three things:

* **The credential store**, `auth.json` in the service's host-private state
  directory (`engine_ops.private_dir()`: mode 0700, outside every sandbox
  mount, refused at the launch itself if a mount overlaps it). Mode 0600.
  Users are a list keyed by the engine's `users.user_id`: username, state
  (`awaiting` first sign-in, or `active`), scrypt password hash with its
  parameters, TOTP secret, the last accepted TOTP step, and the hash of the
  one-time claim code. Sessions hold the SHA-256 of the token, a device
  label, and created / last-seen times; the token itself is never stored.
  The failed-attempt counter is in the same file, so it survives a restart.
  Nothing secret is written to the engine DB, the checkout, `.sc-state/`, or
  `run/`.
* **The published session-hash list** (`auth_sessions.py`): the hashes, the
  cookie name, and whether a user exists, in a directory of its own that is
  mounted read-only into the docker sandbox. Written here, atomically, before
  the store on every change, so a revocation reaches the review server even if
  the second write fails.
* **The checks**: scrypt (`hashlib.scrypt`) and RFC 6238 TOTP (SHA-1, 30 s,
  six digits, one step either side, a step accepted once), both evaluated
  before answering, one generic failure, the same work for an unknown user as
  for a wrong password, and a growing delay after failures that is per
  install.

Writers: the service process and the `./sc auth` verbs, both through this
module, serialised by a lock file in the private directory. The store is read
from disk for every decision, so a `./sc auth user reset` is seen by a running
service at once.

Stdlib only.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_sessions  # noqa: E402  (the published list and the cookie)
import engine_ops  # noqa: E402  (the private directory, the pin, the origin)

STORE_NAME = "auth.json"
LOCK_NAME = "auth.lock"
STORE_VERSION = 1

AWAITING = "awaiting"
ACTIVE = "active"

# scrypt cost for new hashes; each stored hash carries the parameters it was
# made with, so these can be raised without invalidating anything.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 256

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1                   # steps accepted either side of now
TOTP_SECRET_BYTES = 20            # 160 bits, the RFC 4226 recommendation
TOTP_ISSUER = "Subfloor"

# Failed attempts (wrong password, wrong code, wrong claim code), per install.
# The first few cost nothing; after that every further failure doubles the
# wait before the next attempt is even looked at, up to the ceiling.
RATE_FREE = 3
RATE_BASE_SECONDS = 1.0
RATE_MAX_SECONDS = 300.0
# A counter with no failure for this long starts again from zero.
RATE_QUIET_SECONDS = 3600.0

# How long a first sign-in may sit between `begin` and `confirm`. Kept in the
# service's memory only: nothing is saved before confirmation.
ENROLMENT_SECONDS = 900.0
ENROLMENT_MAX = 8
# `last_seen` is written at most this often per session.
TOUCH_SECONDS = 600.0
SESSIONS_MAX = 50                 # per user; the least recently seen goes
DEVICE_MAX = 80
USERNAME_MAX = 64
CLAIM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I
CLAIM_LENGTH = 8

SETUP_CLI = "./sc auth user add <username>"
RESET_CLI = "./sc auth user reset <username>"
UNLOCK_CLI = "./sc auth unlock"

# The engine DB the usernames are read from. None = the install's active one.
# Tests point it at a temporary file, in code.
ENGINE_DB: Path | None = None
# The published directory. None = derived from the checkout's path.
PUBLIC_DIR: Path | None = None

_thread_lock = threading.RLock()
_lock_depth = 0
_lock_fd: int | None = None
# One scrypt at a time: each takes tens of megabytes for a moment.
_scrypt_lock = threading.Lock()
# enrolment id -> {user_id, claim_hash, secret, expires}. Memory only.
_pending: dict[str, dict] = {}


class AuthError(Exception):
    """A refusal with a stable code, for the service to answer with."""

    def __init__(self, status: int, code: str, message: str,
                 details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _clock() -> float:
    return time.time()


def _stamp(moment) -> str | None:
    if not isinstance(moment, (int, float)):
        return None
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── crypto ───────────────────────────────────────────────────────────────────

def hotp(secret: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    """RFC 4226 HOTP with HMAC-SHA-1."""
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp_step(moment: float) -> int:
    return int(moment) // TOTP_PERIOD


def totp_at(secret_b32: str, moment: float, digits: int = TOTP_DIGITS) -> str:
    """The RFC 6238 code for `moment` (SHA-1, 30-second steps)."""
    return hotp(_secret_bytes(secret_b32), totp_step(moment), digits)


def _secret_bytes(secret_b32: str) -> bytes:
    text = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode().rstrip("=")


def totp_accept(secret_b32: str, code, moment: float, last_step) -> int | None:
    """The step `code` is valid for, or None.

    The current step and one either side are all computed and compared, in
    constant time each, whatever matches. A step at or before `last_step` has
    been used already and is not accepted again.
    """
    presented = code if isinstance(code, str) else ""
    presented = presented.strip().replace(" ", "")
    current = totp_step(moment)
    floor = last_step if type(last_step) is int else -1
    try:
        secret = _secret_bytes(secret_b32)
    except (ValueError, TypeError):
        secret = b"\0" * TOTP_SECRET_BYTES
        floor = current + TOTP_WINDOW + 1          # nothing can match
    accepted = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        matches = hmac.compare_digest(
            hotp(secret, step).encode(), presented.encode("utf-8", "replace"))
        if matches and step > floor and accepted is None:
            accepted = step
    return accepted


def otpauth_uri(username: str, secret_b32: str) -> str:
    """The `otpauth://` URI an authenticator app reads (QR code or by hand)."""
    label = quote(f"{TOTP_ISSUER}:{username}", safe="")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(TOTP_ISSUER, safe='')}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}")


def _scrypt(secret: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    with _scrypt_lock:
        return hashlib.scrypt(
            secret.encode("utf-8", "surrogatepass"), salt=salt, n=n, r=r, p=p,
            dklen=dklen, maxmem=max(64 * 1024 * 1024, 256 * n * r))


def hash_secret(secret: str) -> dict:
    """A stored scrypt hash of a password or claim code, with its parameters."""
    salt = secrets.token_bytes(16)
    digest = _scrypt(secret, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return {"algo": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "dklen": SCRYPT_DKLEN, "salt": salt.hex(), "hash": digest.hex()}


def _usable_hash(record) -> bool:
    if not isinstance(record, dict) or record.get("algo") != "scrypt":
        return False
    n, r, p, dklen = (record.get(key) for key in ("n", "r", "p", "dklen"))
    if not all(type(value) is int for value in (n, r, p, dklen)):
        return False
    if not (2 ** 14 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32
            and 1 <= p <= 4 and 16 <= dklen <= 64):
        return False
    return all(isinstance(record.get(key), str) for key in ("salt", "hash"))


def verify_secret(secret, record) -> bool:
    """Whether `secret` matches a stored hash.

    With no usable hash to compare against (unknown user, user awaiting first
    sign-in) the same scrypt work is done against a throwaway salt and the
    answer is no, so the time taken does not say which case it was.
    """
    text = secret if isinstance(secret, str) else ""
    if len(text) > PASSWORD_MAX:
        text = text[:PASSWORD_MAX] + "\0"          # never equal to a real one
    if _usable_hash(record):
        try:
            salt = bytes.fromhex(record["salt"])
            expected = bytes.fromhex(record["hash"])
            digest = _scrypt(text, salt, record["n"], record["r"], record["p"],
                             record["dklen"])
            return hmac.compare_digest(digest, expected)
        except ValueError:
            pass
    _scrypt(text, secrets.token_bytes(16), SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return False


def new_claim_code() -> str:
    return "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(CLAIM_LENGTH))


def show_claim_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _claim_text(value) -> str:
    """A typed claim code as stored: upper case, no dashes or spaces."""
    text = value if isinstance(value, str) else ""
    return re.sub(r"[\s-]", "", text).upper()


# ── the store ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked():
    """One writer at a time: across this process's threads and across the
    service and the CLI (a lock file in the private directory)."""
    global _lock_depth, _lock_fd
    with _thread_lock:
        if _lock_depth == 0:
            path = engine_ops.private_dir() / LOCK_NAME
            _lock_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(_lock_fd, fcntl.LOCK_EX)
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
            if _lock_depth == 0 and _lock_fd is not None:
                try:
                    fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(_lock_fd)
                    _lock_fd = None


def _empty_store() -> dict:
    return {"version": STORE_VERSION, "users": [], "sessions": [],
            "rate": {"failures": 0, "last_failure": None, "blocked_until": None}}


def store_path() -> Path:
    return engine_ops.private_dir() / STORE_NAME


def load() -> dict:
    """The store as it is on disk now; the empty store when there is none.

    A file that exists but cannot be understood is an error, never "no users":
    reading it as empty would let the next write discard the operator's
    credentials.
    """
    path = store_path()
    try:
        os.lstat(path)
    except FileNotFoundError:
        return _empty_store()
    record = engine_ops.read_private(path)
    if (not record or record.get("version") != STORE_VERSION
            or not isinstance(record.get("users"), list)
            or not isinstance(record.get("sessions"), list)):
        raise engine_ops.StateError(
            f"the sign-in store {path} cannot be read. Nothing was changed. "
            "Move it aside to start again (every user must then be re-created "
            f"with {SETUP_CLI}).")
    if not isinstance(record.get("rate"), dict):
        record["rate"] = _empty_store()["rate"]
    record["users"] = [user for user in record["users"] if isinstance(user, dict)]
    record["sessions"] = [s for s in record["sessions"] if isinstance(s, dict)]
    return record


def cookie_name() -> str:
    """This install's session cookie name. Cookies are scoped by host and not
    by port, so every install on this machine needs its own."""
    import sandbox_devkit  # noqa: PLC0415  (scripts/, on sys.path)

    key = sandbox_devkit.install_key(engine_ops.REPO_ROOT).rsplit("-", 1)[1]
    return auth_sessions.COOKIE_PREFIX + key[:8]


def public_dir() -> Path:
    """The directory the session-hash list is published in (created 0700).

    Never inside the private directory, nor the other way round: this one is
    mounted into the sandbox.
    """
    import sandbox_devkit  # noqa: PLC0415  (scripts/, on sys.path)

    path = (PUBLIC_DIR if PUBLIC_DIR is not None
            else sandbox_devkit.auth_sessions_dir(engine_ops.REPO_ROOT))
    if sandbox_devkit.paths_overlap(path, engine_ops.private_dir()):
        raise engine_ops.StateError(
            f"the session-hash directory {path} overlaps the private state "
            "directory; it is mounted into the sandbox and must be separate")
    if PUBLIC_DIR is not None:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    else:
        try:
            path = sandbox_devkit.ensure_auth_sessions_dir(engine_ops.REPO_ROOT)
        except sandbox_devkit.SandboxImageError as exc:
            raise engine_ops.StateError(str(exc)) from exc
    return path


def publish(store: dict) -> None:
    """Write the session-hash list the review server verifies against.

    Hashes, the cookie name, and whether a user exists. No username, no
    secret. Replaced atomically.
    """
    directory = public_dir()
    record = {
        "version": auth_sessions.FILE_VERSION,
        "cookie": cookie_name(),
        "enrolled": bool(store["users"]),
        "sessions": [{"hash": session["token_sha256"],
                      "user_id": session.get("user_id")}
                     for session in store["sessions"]
                     if isinstance(session.get("token_sha256"), str)],
        "updated_at": _stamp(_clock()),
    }
    engine_ops.write_private(directory / auth_sessions.FILE_NAME, record)


def _commit(store: dict) -> None:
    """Publish, then save. In that order a removed session stops being
    recognised by the review server even if the save then fails, and a new
    one whose save fails was never handed to anybody."""
    publish(store)
    engine_ops.write_private(store_path(), store)


def republish() -> None:
    """Bring the published list back in step with the store (service start)."""
    with _locked():
        publish(load())


# ── users and sessions inside a loaded store ─────────────────────────────────

def _name_key(username) -> str:
    return username.strip().casefold() if isinstance(username, str) else ""


def _visible(character: str) -> bool:
    """A character fit for a label or a name: not a control, format,
    surrogate, private-use or unassigned code point, nor a line or paragraph
    separator."""
    category = unicodedata.category(character)
    return not category.startswith("C") and category not in ("Zl", "Zp")


def _find_user(store: dict, username) -> dict | None:
    key = _name_key(username)
    if not key:
        return None
    for user in store["users"]:
        if _name_key(user.get("username")) == key:
            return user
    return None


def _user_by_id(store: dict, user_id) -> dict | None:
    for user in store["users"]:
        if user.get("user_id") == user_id:
            return user
    return None


def _find_session(store: dict, token) -> dict | None:
    """The stored session a presented token belongs to, with an active user."""
    if not auth_sessions.well_formed(token):
        return None
    presented = auth_sessions.token_hash(token)
    found = None
    for session in store["sessions"]:
        stored = session.get("token_sha256")
        if isinstance(stored, str) and hmac.compare_digest(stored, presented):
            found = session
    if found is None:
        return None
    user = _user_by_id(store, found.get("user_id"))
    if user is None or user.get("state") != ACTIVE:
        return None
    return found


def _device_label(device, user_agent) -> str:
    text = device if isinstance(device, str) else ""
    text = "".join(ch for ch in text if _visible(ch)).strip()[:DEVICE_MAX]
    if text:
        return text
    agent = user_agent if isinstance(user_agent, str) else ""
    browser = next((name for mark, name in (
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
        ("Chrome/", "Chrome"), ("Safari/", "Safari")) if mark in agent), "Browser")
    system = next((name for mark, name in (
        ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
        ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux"))
        if mark in agent), "")
    return f"{browser} on {system}" if system else browser


def _new_session(store: dict, user: dict, device, user_agent) -> tuple[str, dict]:
    """Add a session for `user`. Returns (token, record); only the token's
    hash is in the record."""
    token = secrets.token_urlsafe(32)
    now = _clock()
    record = {
        "id": secrets.token_hex(6),
        "user_id": user["user_id"],
        "token_sha256": auth_sessions.token_hash(token),
        "device": _device_label(device, user_agent),
        "created": now,
        "last_seen": now,
    }
    store["sessions"].append(record)
    own = [s for s in store["sessions"] if s.get("user_id") == user["user_id"]]
    if len(own) > SESSIONS_MAX:
        own.sort(key=lambda s: s.get("last_seen") or 0)
        for stale in own[:len(own) - SESSIONS_MAX]:
            store["sessions"].remove(stale)
    return token, record


def _public_session(session: dict, current_id=None) -> dict:
    return {
        "id": session.get("id"),
        "device": session.get("device"),
        "created_at": _stamp(session.get("created")),
        "last_seen_at": _stamp(session.get("last_seen")),
        "current": session.get("id") == current_id,
    }


def _public_user(user: dict) -> dict:
    return {"user_id": user.get("user_id"), "username": user.get("username")}


# ── rate limit ───────────────────────────────────────────────────────────────

def _rate_wait(store: dict, now: float) -> float:
    """Seconds until the next attempt will be looked at; 0 when it will be."""
    until = store["rate"].get("blocked_until")
    if not isinstance(until, (int, float)):
        return 0.0
    # No wait is ever longer than the ceiling. One that reads longer was
    # recorded against a wall clock that has since been set back; it is void
    # rather than a lockout.
    if until - now > RATE_MAX_SECONDS:
        return 0.0
    return max(0.0, until - now)


def _rate_check(store: dict, now: float) -> None:
    wait = _rate_wait(store, now)
    if wait > 0:
        seconds = max(1, math.ceil(wait))
        raise AuthError(
            429, "rate_limited",
            f"too many failed attempts; try again in {seconds} seconds",
            {"retry_after": seconds, "unlock_cli": UNLOCK_CLI})


def _rate_fail(store: dict, now: float) -> None:
    rate = store["rate"]
    failures = rate.get("failures") if type(rate.get("failures")) is int else 0
    last = rate.get("last_failure")
    if isinstance(last, (int, float)) and now - last > RATE_QUIET_SECONDS:
        failures = 0
    failures += 1
    over = failures - RATE_FREE
    delay = 0.0 if over <= 0 else min(
        RATE_BASE_SECONDS * (2 ** min(over - 1, 40)), RATE_MAX_SECONDS)
    store["rate"] = {"failures": failures, "last_failure": now,
                     "blocked_until": now + delay if delay else None}


def _rate_clear(store: dict) -> None:
    store["rate"] = _empty_store()["rate"]


# ── what the service calls ───────────────────────────────────────────────────

GENERIC_FAILURE = (
    "sign-in failed: check the username, the password, and the six-digit "
    "code. If they are right, check that this device's clock is correct.")


def _refuse_sign_in() -> AuthError:
    return AuthError(401, "invalid_credentials", GENERIC_FAILURE)


def status(token=None) -> dict:
    """What a caller may know: whether a user exists and whether this request
    is signed in. With `token`, the session is touched (see `touch`)."""
    with _locked():
        store = load()
        session = touch(token, store) if token else None
        user = _user_by_id(store, session.get("user_id")) if session else None
        wait = _rate_wait(store, _clock())
        return {
            "enrolled": bool(store["users"]),
            "signed_in": session is not None,
            "user": _public_user(user) if user else None,
            "session": (_public_session(session, session.get("id"))
                        if session else None),
            "retry_after": max(1, math.ceil(wait)) if wait > 0 else 0,
            "setup_cli": SETUP_CLI,
        }


def session_for(token) -> dict | None:
    """The session a presented token proves, or None. Reads the store now, so
    a revocation, a sign-out, or a reset holds from the next request."""
    if not auth_sessions.well_formed(token):
        return None
    with _locked():
        session = _find_session(load(), token)
        return dict(session) if session else None


def touch(token, store: dict | None = None) -> dict | None:
    """`session_for`, and record that the session was used. `last_seen` is
    written at most every TOUCH_SECONDS, so use does not mean a write."""
    with _locked():
        current = load() if store is None else store
        session = _find_session(current, token)
        if session is None:
            return None
        now = _clock()
        seen = session.get("last_seen")
        if not isinstance(seen, (int, float)) or now - seen >= TOUCH_SECONDS:
            session["last_seen"] = now
            engine_ops.write_private(store_path(), current)
        return dict(session)


def identify(username) -> dict:
    """Which form the sign-in page shows for a username.

    `first_sign_in` is true only for a user awaiting first sign-in. An unknown
    username and a completed user get the same answer.
    """
    with _locked():
        user = _find_user(load(), username)
    return {"first_sign_in": bool(user and user.get("state") == AWAITING)}


def sign_in(username, password, code, device=None, user_agent=None) -> tuple[str, dict]:
    """Password and authenticator code. Returns (token, payload).

    Both are evaluated before anything is answered, against the user's record
    or against throwaway values when there is none to compare with. Every
    refusal is the same `invalid_credentials`.
    """
    with _locked():
        store = load()
        now = _clock()
        _rate_check(store, now)
        user = _find_user(store, username)
        if user is not None and user.get("state") != ACTIVE:
            user = None
        password_ok = verify_secret(password, user.get("password") if user else None)
        secret = user.get("totp_secret") if user else None
        step = totp_accept(
            secret if isinstance(secret, str) else new_totp_secret(), code, now,
            user.get("totp_last_step") if user else None)
        if user is None or not password_ok or step is None:
            _rate_fail(store, now)
            engine_ops.write_private(store_path(), store)
            raise _refuse_sign_in()
        user["totp_last_step"] = step
        token, session = _new_session(store, user, device, user_agent)
        _rate_clear(store)
        _commit(store)
        return token, {
            "signed_in": True, "user": _public_user(user),
            "session": _public_session(session, session["id"]),
        }


def _purge_pending(now: float) -> None:
    for key in [k for k, v in _pending.items() if v["expires"] <= now]:
        _pending.pop(key, None)
    while len(_pending) >= ENROLMENT_MAX:
        _pending.pop(min(_pending, key=lambda k: _pending[k]["expires"]), None)


def first_begin(username, claim_code) -> dict:
    """First sign-in, step one: the username and the one-time claim code.

    On success the authority creates the TOTP secret and returns it, once,
    with its `otpauth://` URI. Nothing is saved: the secret and the claim stay
    in this process's memory until `first_confirm`, and the claim code is not
    used up until then.
    """
    with _locked():
        store = load()
        now = _clock()
        _rate_check(store, now)
        user = _find_user(store, username)
        if user is not None and user.get("state") != AWAITING:
            user = None
        claim = user.get("claim") if user else None
        if not verify_secret(_claim_text(claim_code), claim) or user is None:
            _rate_fail(store, now)
            engine_ops.write_private(store_path(), store)
            raise AuthError(
                401, "invalid_claim",
                "first sign-in refused: check the username and the claim code. "
                f"A claim code works once; a new one comes from {RESET_CLI}.")
        _purge_pending(now)
        for key in [k for k, v in _pending.items() if v["user_id"] == user["user_id"]]:
            _pending.pop(key, None)
        enrolment = secrets.token_urlsafe(24)
        secret = new_totp_secret()
        _pending[enrolment] = {
            "user_id": user["user_id"], "claim_hash": claim["hash"],
            "secret": secret, "expires": now + ENROLMENT_SECONDS,
        }
        return {
            "enrolment": enrolment,
            "username": user["username"],
            "secret": secret,
            "otpauth_uri": otpauth_uri(user["username"], secret),
            "issuer": TOTP_ISSUER,
            "algorithm": "SHA1", "digits": TOTP_DIGITS, "period": TOTP_PERIOD,
            "expires_in": int(ENROLMENT_SECONDS),
            "password_min": PASSWORD_MIN,
        }


def first_confirm(enrolment, password, code, device=None,
                  user_agent=None) -> tuple[str, dict]:
    """First sign-in, step two: the chosen password and one code from the
    authenticator that was just linked. Returns (token, payload).

    Only now are the password hash and the secret saved, the claim code used
    up, and the first session issued. The user must still be awaiting first
    sign-in with the claim the enrolment began with, so a completed user
    cannot be claimed again and a reset in between ends the enrolment.
    """
    with _locked():
        store = load()
        now = _clock()
        _rate_check(store, now)
        pending = _pending.get(enrolment) if isinstance(enrolment, str) else None
        if pending is not None and pending["expires"] <= now:
            _pending.pop(enrolment, None)
            pending = None
        user = _user_by_id(store, pending["user_id"]) if pending else None
        claim = user.get("claim") if user else None
        if (user is None or user.get("state") != AWAITING
                or not isinstance(claim, dict)
                or not hmac.compare_digest(str(claim.get("hash")),
                                           pending["claim_hash"])):
            if isinstance(enrolment, str):
                _pending.pop(enrolment, None)
            raise AuthError(
                409, "enrolment_expired",
                "this first sign-in is no longer open; start again from the "
                "username and the claim code")
        if (not isinstance(password, str) or len(password) < PASSWORD_MIN
                or len(password) > PASSWORD_MAX):
            raise AuthError(
                400, "password_unacceptable",
                f"choose a password of {PASSWORD_MIN} to {PASSWORD_MAX} characters",
                {"password_min": PASSWORD_MIN, "password_max": PASSWORD_MAX})
        step = totp_accept(pending["secret"], code, now, None)
        if step is None:
            _rate_fail(store, now)
            engine_ops.write_private(store_path(), store)
            raise AuthError(
                401, "invalid_code",
                "that code does not match the authenticator just linked. "
                "Check that this device's clock is correct and try the next code.")
        user.update({
            "state": ACTIVE,
            "password": hash_secret(password),
            "totp_secret": pending["secret"],
            # The confirming code's step is spent: it cannot also sign in.
            "totp_last_step": step,
            "claim": None,
            "activated": now,
        })
        token, session = _new_session(store, user, device, user_agent)
        _rate_clear(store)
        _commit(store)
        _pending.pop(enrolment, None)
        return token, {
            "signed_in": True, "user": _public_user(user),
            "session": _public_session(session, session["id"]),
        }


def sign_out(token) -> bool:
    """End the session a token proves. True when there was one."""
    with _locked():
        store = load()
        session = _find_session(store, token)
        if session is None:
            return False
        store["sessions"].remove(session)
        _commit(store)
        return True


def list_sessions(token) -> list[dict] | None:
    """The signed-in user's devices, most recently seen first; None when the
    token proves no session."""
    with _locked():
        store = load()
        session = _find_session(store, token)
        if session is None:
            return None
        own = [s for s in store["sessions"]
               if s.get("user_id") == session.get("user_id")]
        own.sort(key=lambda s: s.get("last_seen") or 0, reverse=True)
        return [_public_session(s, session.get("id")) for s in own]


def revoke(token, session_id) -> dict | None:
    """Revoke one of the signed-in user's sessions by id.

    Returns `{"revoked": id, "current": bool}`; None when the token proves no
    session. Raises AuthError 404 when the id is not one of this user's.
    """
    with _locked():
        store = load()
        session = _find_session(store, token)
        if session is None:
            return None
        target = next((s for s in store["sessions"]
                       if s.get("id") == session_id
                       and s.get("user_id") == session.get("user_id")), None)
        if target is None or not isinstance(session_id, str):
            raise AuthError(404, "session_not_found", "no such device session")
        store["sessions"].remove(target)
        _commit(store)
        return {"revoked": target["id"], "current": target is session}


# ── what the host CLI calls ──────────────────────────────────────────────────

def engine_users() -> list[dict]:
    """Active users of the engine, `[{user_id, username}]`, read from the
    engine DB on the host. Read-only: nothing is written to the DB, and the
    review API is not needed (it may be down, or in the sandbox)."""
    path = ENGINE_DB
    if path is None:
        import instance_state  # noqa: PLC0415  (scripts/, on sys.path)

        path = instance_state.active_database_path(engine_ops.ENGINE)
    if not Path(path).is_file():
        raise engine_ops.StateError(
            f"the engine database {path} does not exist yet (./sc init creates "
            "the engine user)")
    uri = "file:" + quote(str(path)) + "?mode="
    last: Exception | None = None
    # `mode=ro` first. A WAL database with no reader attached cannot always be
    # opened that way; then open it read-write and forbid writes on the
    # connection instead.
    for mode in ("ro", "rw"):
        try:
            con = sqlite3.connect(uri + mode, uri=True, timeout=5)
        except sqlite3.Error as exc:
            last = exc
            continue
        try:
            con.execute("PRAGMA query_only=ON")
            rows = con.execute(
                "SELECT user_id, username FROM users WHERE is_active=1 "
                "ORDER BY user_id").fetchall()
            return [{"user_id": row[0], "username": row[1]} for row in rows]
        except sqlite3.Error as exc:
            last = exc
        finally:
            con.close()
    raise engine_ops.StateError(f"cannot read the engine's users from {path}: {last}")


def add_user(username: str) -> tuple[dict, str]:
    """Create a GUI user awaiting first sign-in. Returns (user, claim code).

    The username must be an active engine user; the record is keyed by that
    row's `user_id`. No password and no TOTP secret are set. The claim code is
    returned once and stored only as a hash.
    """
    name = username.strip() if isinstance(username, str) else ""
    if not name or len(name) > USERNAME_MAX or not all(map(_visible, name)):
        raise AuthError(400, "bad_username", "a username is required")
    known = engine_users()
    match = next((row for row in known
                  if _name_key(row["username"]) == _name_key(name)), None)
    if match is None:
        names = ", ".join(str(row["username"]) for row in known) or "(none)"
        raise AuthError(
            404, "unknown_engine_user",
            f"{name!r} is not an active engine user. Engine users: {names}")
    with _locked():
        store = load()
        if _user_by_id(store, match["user_id"]) or _find_user(store, match["username"]):
            raise AuthError(
                409, "user_exists",
                f"{match['username']} already has a GUI user. To start it "
                f"again: {RESET_CLI.replace('<username>', str(match['username']))}")
        code = new_claim_code()
        user = {
            "user_id": match["user_id"], "username": match["username"],
            "state": AWAITING, "password": None, "totp_secret": None,
            "totp_last_step": None, "claim": hash_secret(code),
            "created": _clock(), "activated": None,
        }
        store["users"].append(user)
        store["users"].sort(key=lambda u: u.get("user_id") or 0)
        _commit(store)
        return dict(user), code


def reset_user(username: str) -> tuple[dict, str]:
    """Clear a user's password, TOTP secret, and every session; back to
    awaiting first sign-in with a new claim code."""
    with _locked():
        store = load()
        user = _find_user(store, username)
        if user is None:
            raise AuthError(404, "unknown_user", f"no GUI user named {username!r}")
        code = new_claim_code()
        user.update({
            "state": AWAITING, "password": None, "totp_secret": None,
            "totp_last_step": None, "claim": hash_secret(code), "activated": None,
        })
        store["sessions"] = [s for s in store["sessions"]
                             if s.get("user_id") != user["user_id"]]
        for key in [k for k, v in _pending.items() if v["user_id"] == user["user_id"]]:
            _pending.pop(key, None)
        _commit(store)
        return dict(user), code


def list_users() -> list[dict]:
    with _locked():
        store = load()
        return [{
            "user_id": user.get("user_id"), "username": user.get("username"),
            "state": user.get("state"),
            "sessions": sum(1 for s in store["sessions"]
                            if s.get("user_id") == user.get("user_id")),
            "created_at": _stamp(user.get("created")),
        } for user in store["users"]]


def all_sessions() -> list[dict]:
    with _locked():
        store = load()
        names = {user.get("user_id"): user.get("username") for user in store["users"]}
        rows = sorted(store["sessions"], key=lambda s: s.get("last_seen") or 0,
                      reverse=True)
        return [{**_public_session(s), "username": names.get(s.get("user_id"))}
                for s in rows]


def revoke_sessions(session_id: str | None) -> int:
    """Revoke one session by id, or every session with None. Returns how many."""
    with _locked():
        store = load()
        keep = [] if session_id is None else [
            s for s in store["sessions"] if s.get("id") != session_id]
        removed = len(store["sessions"]) - len(keep)
        if removed:
            store["sessions"] = keep
            _commit(store)
        return removed


def unlock() -> int:
    """Clear the failed-attempt counter. Returns how many failures it held."""
    with _locked():
        store = load()
        failures = store["rate"].get("failures") or 0
        _rate_clear(store)
        engine_ops.write_private(store_path(), store)
        return failures


# ── `./sc auth …` ────────────────────────────────────────────────────────────

USAGE = """usage: ./sc auth <command>      (host terminal, main checkout)
  user add <username> [--port <review GUI port>]
                           create the GUI user for an engine user; prints a
                           one-time claim code for first sign-in in the GUI,
                           and confirms the GUI origin the host accepts
  user reset <username>    clear the password, the authenticator, and every
                           session; prints a new claim code
  user list                each user's state
  sessions [list]          signed-in devices
  sessions revoke <id>     sign one device out (--all: every device)
  unlock                   clear the failed-attempt delay
  origin [<port>|--show]   confirm or show the GUI origin the host accepts"""


def _usage() -> int:
    print(USAGE, file=sys.stderr)
    return 2


def _print_claim(user: dict, code: str) -> None:
    print(f"→ {user['username']} (engine user {user['user_id']}) is awaiting "
          "first sign-in")
    print(f"  one-time claim code:  {show_claim_code(code)}")
    print("  Open the review GUI, enter the username and this code, choose a "
          "password,")
    print("  and link an authenticator app. The code is shown once and works "
          "once.")


def _cli_user(argv: list[str]) -> int:
    if not argv:
        return _usage()
    action, rest = argv[0], argv[1:]
    if action == "list" and not rest:
        users = list_users()
        if not users:
            print(f"no GUI user yet — create one with {SETUP_CLI}")
            return 0
        for user in users:
            print(f"{user['user_id']}\t{user['username']}\t{user['state']}\t"
                  f"{user['sessions']} session(s)")
        return 0
    if action == "reset" and len(rest) == 1:
        user, code = reset_user(rest[0])
        print("→ password, authenticator, and every session cleared")
        _print_claim(user, code)
        return 0
    if action == "add":
        port = None
        names = []
        index = 0
        while index < len(rest):
            if rest[index] == "--port" and index + 1 < len(rest):
                port = rest[index + 1]
                index += 2
                continue
            if rest[index].startswith("-"):
                return _usage()
            names.append(rest[index])
            index += 1
        if len(names) != 1:
            return _usage()
        # Everything that can refuse is checked before the origin is asked
        # for, and the user is created only after the origin is recorded.
        known = engine_users()
        if not any(_name_key(row["username"]) == _name_key(names[0]) for row in known):
            listed = ", ".join(str(row["username"]) for row in known) or "(none)"
            raise AuthError(
                404, "unknown_engine_user",
                f"{names[0]!r} is not an active engine user. Engine users: {listed}")
        with _locked():
            if _find_user(load(), names[0]) is not None:
                raise AuthError(
                    409, "user_exists",
                    f"{names[0]} already has a GUI user. To start it again: "
                    f"{RESET_CLI.replace('<username>', names[0])}")
        chosen = engine_ops.choose_gui_port(port, prog="auth user add", keep=True)
        if chosen is None:
            print("→ nothing was created", file=sys.stderr)
            return 1
        try:
            config = engine_ops.pin_gui_port(chosen)
        except ValueError as exc:
            print(f"✗ auth user add: {exc}", file=sys.stderr)
            return 1
        print(f"→ the host accepts the GUI at "
              f"http://{engine_ops.BIND}:{config['gui_port']}")
        user, code = add_user(names[0])
        _print_claim(user, code)
        return 0
    return _usage()


def _cli_sessions(argv: list[str]) -> int:
    if not argv or argv == ["list"]:
        rows = all_sessions()
        if not rows:
            print("no device is signed in")
            return 0
        for row in rows:
            print(f"{row['id']}\t{row['username']}\t{row['device']}\t"
                  f"last seen {row['last_seen_at']}\tsince {row['created_at']}")
        return 0
    if argv[0] == "revoke" and len(argv) == 2:
        target = None if argv[1] == "--all" else argv[1]
        removed = revoke_sessions(target)
        if not removed and target is not None:
            print(f"✗ no session with id {target}", file=sys.stderr)
            return 1
        print(f"→ {removed} session(s) revoked")
        return 0
    return _usage()


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if engine_ops.in_sandbox():
        print("✗ ./sc auth refused: sign-in is managed on the HOST, in a host "
              "terminal, and this is the sandbox. Nothing was read or changed.",
              file=sys.stderr)
        return 1
    if not argv:
        return _usage()
    command, rest = argv[0], argv[1:]
    try:
        if command == "user":
            return _cli_user(rest)
        if command == "sessions":
            return _cli_sessions(rest)
        if command == "unlock" and not rest:
            print(f"→ failed-attempt delay cleared ({unlock()} recorded failure(s))")
            return 0
        if command == "origin":
            return engine_ops.pin_command(rest, prog="auth origin")
        return _usage()
    except AuthError as exc:
        print(f"✗ auth: {exc.message}", file=sys.stderr)
        return 1
    except engine_ops.StateError as exc:
        print(f"✗ auth: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
