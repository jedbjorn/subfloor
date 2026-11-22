"""Long-lived paired browsers for the standalone Manager operator."""
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import sys
import threading
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

try:
    import auth_sessions
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
    import auth_sessions

from manage import atomic
from operator_password import matches

SESSIONS_MAX = 50
TOUCH_SECONDS = 600
RATE_FREE = 3
RATE_QUIET_SECONDS = 3600
RATE_MAX_SECONDS = 300


class AuthError(ValueError):
    def __init__(self, status, code, message, retry_after=0):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def stamp(moment):
    if not isinstance(moment, (int, float)):
        return None
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def device_label(device, user_agent):
    if isinstance(device, str):
        cleaned = "".join(ch for ch in device if not unicodedata.category(ch).startswith("C")
                          and unicodedata.category(ch) not in ("Zl", "Zp")).strip()[:80]
        if cleaned:
            return cleaned
    agent = user_agent if isinstance(user_agent, str) else ""
    browser = next((name for mark, name in (("Edg/", "Edge"), ("OPR/", "Opera"),
                    ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
                   if mark in agent), "Browser")
    system = next((name for mark, name in (("Android", "Android"), ("iPhone", "iPhone"),
                   ("iPad", "iPad"), ("Windows", "Windows"), ("Mac OS X", "macOS"),
                   ("Linux", "Linux")) if mark in agent), "")
    return f"{browser} on {system}" if system else browser


class BrowserAuth:
    def __init__(self, settings, service_id):
        self.settings = Path(settings)
        self.state = self.settings.parent
        self.private = self.state / "browser-auth.json"
        self.public = self.state / "browser-sessions"
        self.public.mkdir(mode=0o700, exist_ok=True)
        suffix = hashlib.sha256(service_id.encode()).hexdigest()[:8]
        self.cookie_name = auth_sessions.COOKIE_PREFIX + suffix
        auth_sessions.DIR = self.public
        self._thread_lock = threading.RLock()
        with self.locked():
            self.commit(self.load())

    @contextlib.contextmanager
    def locked(self):
        with self._thread_lock:
            fd = os.open(self.state / "browser-auth.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def empty(self):
        return {"version": 1, "sessions": [],
                "rate": {"failures": 0, "last_failure": None, "blocked_until": None}}

    def load(self):
        try:
            record = json.loads(self.private.read_text())
        except FileNotFoundError:
            record = self.empty()
        if (not isinstance(record, dict) or record.get("version") != 1
                or not isinstance(record.get("sessions"), list)):
            raise ValueError(f"paired-browser store cannot be read: {self.private}")
        record.setdefault("rate", self.empty()["rate"])
        record["sessions"] = [session for session in record["sessions"] if isinstance(session, dict)]
        published = {entry["hash"] for entry in auth_sessions.read_public()["sessions"]}
        record["sessions"] = [session for session in record["sessions"]
                              if session.get("token_sha256") in published]
        return record

    def save(self, store):
        atomic(self.private, json.dumps(store))

    def publish(self, store, only=None):
        config = json.loads(self.settings.read_text())
        record = {"version": auth_sessions.FILE_VERSION, "cookie": self.cookie_name,
                  "enrolled": bool(config.get("operator_verifier")),
                  "sessions": [{"hash": session["token_sha256"], "user_id": 1}
                               for session in store["sessions"]
                               if isinstance(session.get("token_sha256"), str)
                               and (only is None or session["token_sha256"] in only)],
                  "updated_at": stamp(time.time())}
        atomic(self.public / auth_sessions.FILE_NAME, json.dumps(record))

    def commit(self, store):
        wanted = {session.get("token_sha256") for session in store["sessions"]}
        published = {entry["hash"] for entry in auth_sessions.read_public()["sessions"]}
        if published - wanted:
            self.publish(store, only=published)
        self.save(store)
        self.publish(store)

    def public_session(self, session, current=None):
        return {"id": session.get("id"), "device": session.get("device"),
                "created_at": stamp(session.get("created")),
                "last_seen_at": stamp(session.get("last_seen")),
                "current": session.get("id") == current}

    def find(self, store, token):
        if not auth_sessions.well_formed(token) or auth_sessions.verify_token(token) is None:
            return None
        presented = auth_sessions.token_hash(token)
        found = None
        for session in store["sessions"]:
            stored = session.get("token_sha256")
            if isinstance(stored, str) and hmac.compare_digest(stored, presented):
                found = session
        return found

    def session(self, token, touch=False):
        with self.locked():
            store = self.load()
            session = self.find(store, token)
            if session is None:
                return None
            now = time.time()
            if touch and now - (session.get("last_seen") or 0) >= TOUCH_SECONDS:
                session["last_seen"] = now
                self.save(store)
            return self.public_session(session, session.get("id"))

    def rate_wait(self, store, now):
        until = store["rate"].get("blocked_until")
        if not isinstance(until, (int, float)) or until - now > RATE_MAX_SECONDS:
            return 0
        return max(0, until - now)

    def rate_check(self, store, now):
        wait = self.rate_wait(store, now)
        if wait:
            raise AuthError(429, "rate_limited",
                            f"too many failed attempts; try again in {max(1, math.ceil(wait))} seconds",
                            max(1, math.ceil(wait)))

    def pair(self, username, password, device, user_agent):
        with self.locked():
            store = self.load()
            now = time.time()
            self.rate_check(store, now)
            config = json.loads(self.settings.read_text())
            credential = base64.b64encode(f"{username}:{password}".encode("utf-8", "surrogatepass")).decode()
            if not matches("Basic " + credential, config.get("operator_verifier")):
                rate = store["rate"]
                failures = rate.get("failures") if type(rate.get("failures")) is int else 0
                last = rate.get("last_failure")
                if isinstance(last, (int, float)) and now - last > RATE_QUIET_SECONDS:
                    failures = 0
                failures += 1
                over = failures - RATE_FREE
                delay = 0 if over <= 0 else min(2 ** min(over - 1, 40), RATE_MAX_SECONDS)
                store["rate"] = {"failures": failures, "last_failure": now,
                                 "blocked_until": now + delay if delay else None}
                self.save(store)
                raise AuthError(401, "invalid_credentials",
                                "Pairing failed. Check the username and Manager password.")
            token = secrets.token_urlsafe(32)
            session = {"id": secrets.token_hex(6), "token_sha256": auth_sessions.token_hash(token),
                       "device": device_label(device, user_agent), "created": now, "last_seen": now}
            store["sessions"].append(session)
            store["sessions"].sort(key=lambda row: row.get("last_seen") or 0)
            store["sessions"] = store["sessions"][-SESSIONS_MAX:]
            store["rate"] = self.empty()["rate"]
            self.commit(store)
            return token, {"signed_in": True, "user": {"username": "operator"},
                           "session": self.public_session(session, session["id"])}

    def status(self, token):
        session = self.session(token, touch=True) if token else None
        with self.locked():
            store = self.load()
            wait = self.rate_wait(store, time.time())
        config = json.loads(self.settings.read_text())
        enrolled = bool(config.get("operator_verifier"))
        return {"enrolled": enrolled, "signed_in": session is not None and enrolled,
                "user": {"username": "operator"} if session and enrolled else None,
                "session": session, "retry_after": max(1, math.ceil(wait)) if wait else 0}

    def sessions(self, token):
        with self.locked():
            store = self.load()
            current = self.find(store, token)
            if current is None:
                return None
            rows = sorted(store["sessions"], key=lambda row: row.get("last_seen") or 0, reverse=True)
            return [self.public_session(row, current.get("id")) for row in rows]

    def all_sessions(self):
        with self.locked():
            store = self.load()
            rows = sorted(store["sessions"], key=lambda row: row.get("last_seen") or 0, reverse=True)
            return [self.public_session(row) for row in rows]

    def sign_out(self, token):
        with self.locked():
            store = self.load()
            current = self.find(store, token)
            if current is None:
                return False
            store["sessions"].remove(current)
            self.commit(store)
            return True

    def revoke(self, token, session_id):
        with self.locked():
            store = self.load()
            current = self.find(store, token)
            if current is None:
                return None
            target = next((row for row in store["sessions"] if row.get("id") == session_id), None)
            if target is None or not isinstance(session_id, str):
                raise AuthError(404, "session_not_found", "No such paired browser.")
            store["sessions"].remove(target)
            self.commit(store)
            return {"revoked": target["id"], "current": target is current}

    def revoke_id(self, session_id):
        with self.locked():
            store = self.load()
            keep = [] if session_id is None else [
                row for row in store["sessions"] if row.get("id") != session_id]
            removed = len(store["sessions"]) - len(keep)
            if removed:
                store["sessions"] = keep
                self.commit(store)
            return removed

    def cookie(self, token, secure=False):
        value = auth_sessions.set_cookie(self.cookie_name, token)
        return value + ("; Secure" if secure else "")

    def clear_cookie(self, secure=False):
        value = auth_sessions.clear_cookie(self.cookie_name)
        return value + ("; Secure" if secure else "")
