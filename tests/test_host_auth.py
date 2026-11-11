#!/usr/bin/env python3
"""The host auth authority (Feature #94, spec #271): sign-in on the engine-ops
service, its credential store, the published session-hash list, the launcher's
read-only mount of it, and the `./sc auth` verbs.

Acceptance 2 — password and TOTP are correct: RFC 4226 / RFC 6238 published
vectors, the one-step window, single use of a step, wrong password, failures
that cannot be told apart, and a rate limit that survives a service restart.
Acceptance 3 (service half) — session lifecycle: the cookie's attributes, the
token absent from disk with only its hash stored, revoke and sign-out holding
from the next request, the cookie renewed on use.
Acceptance 5 — engine operations need a session: `/status` and `/ops/*` refuse
without one and pass with one, whatever Origin the caller writes.
Acceptance 9 (service half) — first sign-in: only with the claim code and a
confirmed authenticator code; a wrong or reused claim code is refused; nothing
is saved before confirmation; a completed user cannot be claimed again; reset
returns the user to awaiting and ends every session.

Nothing here touches a real instance: every service serves a temporary
checkout with its private and published state in temporary directories, the
engine's users come from a temporary SQLite file, and the dispatcher tests
drive the real `sc` and `dispatch.sh` over a fake engine.

Run:
    python3 tests/test_host_auth.py
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_engine_ops_service as base  # noqa: E402  (the service fixture)
from test_host_runtime import HostRuntimeFixture  # noqa: E402

import auth_sessions  # noqa: E402
import engine_ops  # noqa: E402
import engine_ops_service  # noqa: E402
import host_auth  # noqa: E402
import ports  # noqa: E402
import sandbox_devkit  # noqa: E402

ENGINE = base.ENGINE
GUI = base.GUI
GUI_PORT = base.GUI_PORT
PASSWORD = "correct horse battery staple"
RFC_SECRET = b"12345678901234567890"


def build_engine_db(path: Path, users=((1, "Operator", 1), (2, "Second", 1),
                                       (3, "Retired", 0))) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE users (user_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT NOT NULL UNIQUE, email TEXT, initials TEXT, "
        "password_hash TEXT, password_salt TEXT, "
        "is_active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
    con.executemany(
        "INSERT INTO users (user_id, username, is_active) VALUES (?, ?, ?)", users)
    con.commit()
    con.close()


class AuthFixture(base.ServiceFixture):
    """The real service with nobody signed in, a controllable clock, and the
    engine's users in a temporary database."""

    signed_in = False

    def setUp(self) -> None:
        super().setUp()
        self.engine_db = self.base / "engine.db"
        build_engine_db(self.engine_db)
        self.now = 1_800_000_000.0
        for patcher in (
            mock.patch.object(host_auth, "ENGINE_DB", self.engine_db),
            mock.patch.object(host_auth, "_clock", side_effect=lambda: self.now),
            # The cheapest cost the store accepts: these tests are about the
            # checks, not about how long a hash takes.
            mock.patch.object(host_auth, "SCRYPT_N", 2 ** 14),
            mock.patch.object(engine_ops, "in_sandbox", return_value=False),
            # `./sc auth user add` pins the origin, which allocates and
            # publishes the service port through the instance config.
            mock.patch.object(ports, "REPO_ROOT", self.root),
            mock.patch.object(ports, "CONFIG",
                              self.root / ".super-coder" / "instance.json"),
            mock.patch.object(ports, "_free", return_value=True),
            mock.patch.object(ports, "_sibling_ports", return_value=set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ── driving ──────────────────────────────────────────────────────────────

    def tick(self, seconds: float = 30.0) -> None:
        self.now += seconds

    def code(self, secret: str, offset_steps: int = 0) -> str:
        return host_auth.totp_at(secret, self.now + 30 * offset_steps)

    def post(self, path: str, body: dict | None = None, **kwargs):
        kwargs.setdefault("origin", GUI)
        return self.call("POST", path, body={} if body is None else body, **kwargs)

    def get(self, path: str, **kwargs):
        kwargs.setdefault("origin", GUI)
        return self.call("GET", path, **kwargs)

    def cli(self, *argv: str, tty: bool = False, answer: str | None = None):
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        asked = mock.Mock(side_effect=[answer] if answer is not None else EOFError)
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch("builtins.input", asked), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = host_auth.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def add_user(self, username: str = "Operator") -> str:
        """`./sc auth user add`: returns the claim code it printed."""
        code, out, err = self.cli("user", "add", username, "--port", str(GUI_PORT))
        self.assertEqual(code, 0, out + err)
        return re.search(r"claim code:\s+(\S+)", out).group(1)

    def begin(self, username: str, claim: str):
        return self.post("/auth/first/begin",
                         {"username": username, "claim_code": claim})

    def enrol(self, username: str = "Operator", password: str = PASSWORD,
              device: str | None = "first browser"):
        """The whole first sign-in. Returns (cookie header, TOTP secret)."""
        claim = self.add_user(username)
        status, _headers, begun = self.begin(username, claim)
        self.assertEqual(status, 200, begun)
        status, headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": password,
            "code": self.code(begun["secret"]), "device": device})
        self.assertEqual(status, 200, out)
        self.tick()                      # the confirming step is spent
        return headers["Set-Cookie"].split(";")[0], begun["secret"]

    def sign_in(self, secret: str, username: str = "Operator",
                password: str = PASSWORD, code: str | None = None,
                device: str | None = None, **request):
        body = {"username": username, "password": password,
                "code": self.code(secret) if code is None else code}
        if device is not None:
            body["device"] = device
        return self.post("/auth/sign-in", body, **request)

    def store(self) -> dict:
        return json.loads((self.private / host_auth.STORE_NAME).read_text())

    def published(self) -> dict:
        return json.loads((self.public / auth_sessions.FILE_NAME).read_text())

    def every_file(self) -> dict[str, bytes]:
        """Every file under the temporary tree: checkout, private, published."""
        return {str(path): path.read_bytes()
                for path in self.base.rglob("*") if path.is_file()}

    def restart_as_a_new_process(self) -> int:
        """End this process's service and start the real service in a NEW
        process over the same private and published directories. Returns its
        port. That process runs on the real clock."""
        self.service.shutdown()
        self.service.server_close()
        launcher = self.base / "launcher"
        launcher.mkdir()
        script = launcher / "engine_ops_service.py"
        script.write_text(base.SERVICE_LAUNCHER)
        proc = subprocess.Popen(
            [sys.executable, str(script), str(ENGINE), str(self.root),
             str(self.private), "session", json.dumps(["noop"])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(base.reap, proc)
        port_file = self.root / "service.port"
        self.assertTrue(base.wait_for(port_file.exists, timeout=30))
        return int(port_file.read_text())

    def accepted_by(self, cookie: str) -> tuple[bool, bool]:
        """Whether the host authority, and the review server's check, accept
        a session cookie right now."""
        host = self.get("/status", cookie=cookie)[0] == 200
        review = auth_sessions.session_from_cookie_header(cookie) is not None
        return host, review

    @contextlib.contextmanager
    def write_fails(self, index: int):
        """Make the `index`-th atomic write from now fail, as a full disk or a
        crash at that point would leave things. Yields a list that holds the
        paths written before it, and whether the failure was reached."""
        real = engine_ops.write_private
        seen: list[str] = []
        state = {"hit": False}

        def write(path, record):
            if len(seen) == index and not state["hit"]:
                state["hit"] = True
                raise OSError(28, "No space left on device (injected)")
            seen.append(Path(path).name)
            return real(path, record)

        with mock.patch.object(engine_ops, "write_private", side_effect=write):
            yield seen, state

    def assert_nowhere_on_disk(self, secret: str, allow: tuple[str, ...] = ()) -> None:
        for name, data in self.every_file().items():
            if name.endswith(allow) and allow:
                continue
            self.assertNotIn(secret.encode(), data, name)


# ── acceptance 2: the checks ─────────────────────────────────────────────────

class CryptoTest(unittest.TestCase):
    def test_rfc_4226_hotp_vectors(self):
        expected = ["755224", "287082", "359152", "969429", "338314",
                    "254676", "287922", "162583", "399871", "520489"]
        self.assertEqual(
            [host_auth.hotp(RFC_SECRET, counter) for counter in range(10)], expected)

    def test_rfc_6238_totp_vectors(self):
        """Appendix B, SHA-1 rows (eight digits, 30-second step, T0 = 0)."""
        vectors = {
            59: "94287082", 1111111109: "07081804", 1111111111: "14050471",
            1234567890: "89005924", 2000000000: "69279037",
            20000000000: "65353130",
        }
        secret = base64.b32encode(RFC_SECRET).decode()
        for moment, code in vectors.items():
            self.assertEqual(
                host_auth.hotp(RFC_SECRET, host_auth.totp_step(moment), 8), code)
            self.assertEqual(host_auth.totp_at(secret, moment, 8), code)
            # Six digits are the low six of the same truncation.
            self.assertEqual(host_auth.totp_at(secret, moment), code[2:])
        self.assertEqual(host_auth.totp_step(59), 1)
        self.assertEqual(host_auth.totp_step(60), 2)
        self.assertEqual((host_auth.TOTP_PERIOD, host_auth.TOTP_DIGITS,
                          host_auth.TOTP_WINDOW), (30, 6, 1))

    def test_one_step_either_side_and_no_further(self):
        secret = host_auth.new_totp_secret()
        now = 1_700_000_015.0
        step = host_auth.totp_step(now)
        for offset in (-1, 0, 1):
            code = host_auth.totp_at(secret, now + 30 * offset)
            self.assertEqual(host_auth.totp_accept(secret, code, now, None),
                             step + offset, offset)
        for offset in (-3, -2, 2, 3):
            code = host_auth.totp_at(secret, now + 30 * offset)
            self.assertIsNone(host_auth.totp_accept(secret, code, now, None), offset)
        for bad in ("", "12345", "1234567", "abcdef", None, 123456, "１２３４５６"):
            self.assertIsNone(host_auth.totp_accept(secret, bad, now, None), bad)
        # Spaces as authenticator apps print them are fine.
        code = host_auth.totp_at(secret, now)
        self.assertEqual(
            host_auth.totp_accept(secret, f"{code[:3]} {code[3:]}", now, None), step)

    def test_a_step_is_accepted_once(self):
        secret = host_auth.new_totp_secret()
        now = 1_700_000_015.0
        step = host_auth.totp_step(now)
        code = host_auth.totp_at(secret, now)
        self.assertEqual(host_auth.totp_accept(secret, code, now, None), step)
        # The same step again, and any older one, is spent.
        self.assertIsNone(host_auth.totp_accept(secret, code, now, step))
        earlier = host_auth.totp_at(secret, now - 30)
        self.assertIsNone(host_auth.totp_accept(secret, earlier, now, step))
        # Still inside its window a moment later: still spent.
        self.assertIsNone(host_auth.totp_accept(secret, code, now + 29, step))
        # The next step's code is new.
        following = host_auth.totp_at(secret, now + 30)
        self.assertEqual(host_auth.totp_accept(secret, following, now + 30, step),
                         step + 1)

    def test_every_step_of_the_window_is_computed_whatever_matches(self):
        secret = host_auth.new_totp_secret()
        now = 1_700_000_015.0
        for code in (host_auth.totp_at(secret, now - 30), "000000"):
            with mock.patch.object(host_auth, "hotp", wraps=host_auth.hotp) as hotp:
                host_auth.totp_accept(secret, code, now, None)
            self.assertEqual(hotp.call_count, 3, code)

    def test_secret_and_uri(self):
        secret = host_auth.new_totp_secret()
        self.assertRegex(secret, r"^[A-Z2-7]{32}$")          # 160 bits, base32
        self.assertNotEqual(secret, host_auth.new_totp_secret())
        uri = urlparse(host_auth.otpauth_uri("Jo Operator", secret))
        self.assertEqual((uri.scheme, uri.netloc), ("otpauth", "totp"))
        self.assertEqual(unquote(uri.path), "/Subfloor:Jo Operator")
        self.assertEqual(parse_qs(uri.query), {
            "secret": [secret], "issuer": ["Subfloor"], "algorithm": ["SHA1"],
            "digits": ["6"], "period": ["30"]})

    def test_scrypt_hash_carries_its_parameters_and_verifies(self):
        with mock.patch.object(host_auth, "SCRYPT_N", 2 ** 14):
            record = host_auth.hash_secret(PASSWORD)
        self.assertEqual(
            {key: record[key] for key in ("algo", "n", "r", "p", "dklen")},
            {"algo": "scrypt", "n": 2 ** 14, "r": 8, "p": 1, "dklen": 32})
        self.assertEqual((len(record["salt"]), len(record["hash"])), (32, 64))
        self.assertNotIn(PASSWORD, json.dumps(record))
        # Verified with the parameters stored, not the current defaults.
        self.assertEqual(host_auth.SCRYPT_N, 2 ** 15)
        self.assertTrue(host_auth.verify_secret(PASSWORD, record))
        for wrong in (PASSWORD + " ", PASSWORD.upper(), "", None, 7, "x" * 5000):
            self.assertFalse(host_auth.verify_secret(wrong, record), wrong)
        # A salt per hash.
        with mock.patch.object(host_auth, "SCRYPT_N", 2 ** 14):
            self.assertNotEqual(host_auth.hash_secret(PASSWORD)["hash"], record["hash"])

    def test_no_stored_hash_costs_the_same_scrypt(self):
        """Unknown user or awaiting user: the same work, and the answer is no."""
        with mock.patch.object(host_auth, "SCRYPT_N", 2 ** 14):
            record = host_auth.hash_secret(PASSWORD)
            for stored in (record, None, {}, {"algo": "md5"},
                           {**record, "n": 3}, {**record, "salt": "zz"}):
                with mock.patch.object(host_auth, "_scrypt",
                                       wraps=host_auth._scrypt) as scrypt:
                    result = host_auth.verify_secret(PASSWORD, stored)
                self.assertEqual(result, stored is record)
                self.assertGreaterEqual(scrypt.call_count, 1)
                n, r, p = scrypt.call_args_list[-1].args[2:5]
                self.assertEqual((n, r, p), (2 ** 14, 8, 1))

    def test_device_labels_are_plain_bounded_text(self):
        label = host_auth._device_label
        self.assertEqual(label("  desk\x00\x1b[31m\n‮ top  ", None),
                         "desk[31m top")
        self.assertEqual(len(label("x" * 500, None)), host_auth.DEVICE_MAX)
        self.assertEqual(label("Büro – Rechner 2", None), "Büro – Rechner 2")
        for nothing in ("", "   ", "\x00\n", None, 7, ["x"]):
            self.assertEqual(label(nothing, None), "Browser", nothing)
        self.assertEqual(
            label(None, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15"),
            "Safari on macOS")
        self.assertEqual(
            label("", "Mozilla/5.0 (Windows NT 10.0) Chrome/126.0 Safari/537.36 Edg/126.0"),
            "Edge on Windows")

    def test_claim_code_is_short_and_unambiguous(self):
        code = host_auth.new_claim_code()
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{8}$")
        self.assertEqual(host_auth.show_claim_code(code), f"{code[:4]}-{code[4:]}")
        self.assertEqual(
            host_auth._claim_text(f" {code[:4].lower()}-{code[4:]} "), code)


class SignInTest(AuthFixture):
    """Acceptance 2 and 3, over HTTP."""

    def test_sign_in_issues_the_cookie_and_only_its_hash_is_stored(self):
        _first, secret = self.enrol()
        status, headers, out = self.sign_in(secret, device="laptop")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["signed_in"], True)
        self.assertEqual(out["user"], {"user_id": 1, "username": "Operator"})
        self.assertEqual(out["session"]["device"], "laptop")
        self.assertTrue(out["session"]["current"])

        # The cookie, attribute by attribute.
        name, _sep, rest = headers["Set-Cookie"].partition("=")
        token, *attributes = [part.strip() for part in rest.split(";")]
        self.assertEqual(name, host_auth.cookie_name())
        self.assertRegex(name, r"^sc_session_[0-9a-f]{8}$")
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")           # 256 bits
        self.assertEqual(len(base64.urlsafe_b64decode(token + "=")), 32)
        self.assertEqual(attributes, [
            f"Max-Age={400 * 24 * 3600}", "Path=/", "HttpOnly", "SameSite=Strict"])
        lowered = headers["Set-Cookie"].lower()
        for absent in ("secure", "domain=", "expires="):
            self.assertNotIn(absent, lowered)
        self.assertEqual(headers["Access-Control-Allow-Credentials"], "true")
        self.assertEqual(headers["Access-Control-Allow-Origin"], GUI)
        self.assertNotIn(token, json.dumps(out))

        # On disk: the hash, in the private store and the published list.
        digest = auth_sessions.token_hash(token)
        self.assert_nowhere_on_disk(token)
        self.assertIn(digest, [s["token_sha256"] for s in self.store()["sessions"]])
        self.assertIn(digest, [s["hash"] for s in self.published()["sessions"]])
        # Neither the password nor the TOTP secret leaves the private store.
        self.assert_nowhere_on_disk(PASSWORD)
        self.assert_nowhere_on_disk(secret, allow=(host_auth.STORE_NAME,))
        self.assertEqual(stat.S_IMODE((self.private / "auth.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.public.stat().st_mode), 0o700)
        self.assertEqual(
            sorted(self.published()),
            ["cookie", "enrolled", "sessions", "updated_at", "version"])
        self.assertEqual(sorted(self.published()["sessions"][0]), ["hash", "user_id"])
        self.assertNotIn("Operator", (self.public / "sessions.json").read_text())

        # And it works: the engine status and an operation.
        cookie = f"{name}={token}"
        status, _headers, out = self.get("/status", cookie=cookie)
        self.assertEqual(status, 200, out)
        with mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)}):
            status, _headers, out = self.post("/ops/update", cookie=cookie)
        self.assertEqual(status, 202, out)
        self.finished()

    def test_failures_cannot_be_told_apart(self):
        _cookie, secret = self.enrol()
        self.add_user("Second")                       # awaiting first sign-in
        answers = {}

        def attempt(label, **fields):
            host_auth.unlock()
            body = {"username": "Operator", "password": PASSWORD,
                    "code": self.code(secret), **fields}
            with mock.patch.object(host_auth, "_scrypt",
                                   wraps=host_auth._scrypt) as scrypt, \
                    mock.patch.object(host_auth, "totp_accept",
                                      wraps=host_auth.totp_accept) as totp:
                status, headers, out = self.post("/auth/sign-in", body)
            # Password and code are both evaluated, every time.
            self.assertEqual((scrypt.call_count, totp.call_count), (1, 1), label)
            headers = {key: value for key, value in headers.items() if key != "Date"}
            answers[label] = (status, headers, out)
            return status

        attempt("unknown user", username="nobody")
        attempt("unknown user, garbage", username="nobody", password="x", code="1")
        attempt("wrong password", password=PASSWORD + "x")
        attempt("wrong code", code="000000")
        attempt("wrong password and code", password="nope", code="000000")
        attempt("user awaiting first sign-in", username="Second")
        attempt("inactive engine user", username="Retired")
        attempt("empty", username="", password="", code="")
        attempt("not strings", username=7, password=None, code=["1"])
        attempt("a code two steps old", code=self.code(secret, -2))
        # A code that was just used: correct, and spent.
        used = self.code(secret)
        host_auth.unlock()
        self.assertEqual(self.sign_in(secret, code=used)[0], 200)
        attempt("replayed code", code=used)

        first = answers["unknown user"]
        self.assertEqual(first[0], 401)
        self.assertEqual(first[2]["error"]["code"], "invalid_credentials")
        self.assertNotIn("Set-Cookie", first[1])
        for label, answer in answers.items():
            self.assertEqual(answer, first, label)

    def test_code_window_and_single_use_over_http(self):
        _cookie, secret = self.enrol()
        used = self.code(secret)
        self.assertEqual(self.sign_in(secret, code=used)[0], 200)
        status, _headers, out = self.sign_in(secret, code=used)
        self.assertEqual(status, 401, out)
        # The previous step's code is inside the window but older than the
        # step already accepted.
        self.assertEqual(self.sign_in(secret, code=self.code(secret, -1))[0], 401)
        host_auth.unlock()
        self.tick(60)
        # One step behind and one ahead of the clock are accepted …
        self.assertEqual(self.sign_in(secret, code=self.code(secret, -1))[0], 200)
        self.tick(60)
        self.assertEqual(self.sign_in(secret, code=self.code(secret, 1))[0], 200)
        # … two are not (clock drift beyond one step).
        self.tick(120)
        self.assertEqual(self.sign_in(secret, code=self.code(secret, 2))[0], 401)
        self.assertEqual(self.sign_in(secret, code=self.code(secret, -2))[0], 401)
        self.assertIn("clock", self.sign_in(secret, code="000000")[2]["error"]["message"])

    def test_wrong_password_is_refused_and_the_step_is_not_spent(self):
        _cookie, secret = self.enrol()
        code = self.code(secret)
        status, _headers, out = self.sign_in(secret, password="wrong", code=code)
        self.assertEqual(status, 401, out)
        self.assertEqual(self.sign_in(secret, code=code)[0], 200)

    def test_cookie_is_renewed_on_use_and_last_seen_is_throttled(self):
        cookie, _secret = self.enrol()
        token = cookie.split("=", 1)[1]
        first_seen = self.store()["sessions"][0]["last_seen"]
        before = (self.private / "auth.json").stat().st_mtime_ns

        self.tick(60)
        status, headers, out = self.get("/auth/status", cookie=cookie)
        self.assertEqual(status, 200, out)
        self.assertTrue(out["signed_in"])
        self.assertEqual(out["user"], {"user_id": 1, "username": "Operator"})
        # The same token again, with the full lifetime from now.
        self.assertEqual(headers["Set-Cookie"],
                         auth_sessions.set_cookie(host_auth.cookie_name(), token))
        self.assertIn(f"Max-Age={400 * 24 * 3600}", headers["Set-Cookie"])
        # Use within the throttle window writes nothing.
        self.assertEqual(self.store()["sessions"][0]["last_seen"], first_seen)
        self.assertEqual((self.private / "auth.json").stat().st_mtime_ns, before)

        self.tick(host_auth.TOUCH_SECONDS)
        status, headers, out = self.get("/auth/status", cookie=cookie)
        self.assertIn("Set-Cookie", headers)
        self.assertEqual(self.store()["sessions"][0]["last_seen"], self.now)
        self.assertEqual(out["session"]["last_seen_at"],
                         host_auth._stamp(self.now))
        # The engine never expires a session: years later it still stands.
        self.tick(3 * 365 * 24 * 3600)
        self.assertEqual(self.get("/status", cookie=cookie)[0], 200)

        # No cookie, an unknown one, or no Origin: no renewal, not signed in.
        for kwargs in ({}, {"cookie": f"{host_auth.cookie_name()}={'A' * 43}"},
                       {"cookie": cookie, "origin": None}):
            status, headers, out = self.get("/auth/status", **kwargs)
            self.assertEqual(status, 200, out)
            self.assertFalse(out["signed_in"], kwargs)
            self.assertIsNone(out["user"])
            self.assertNotIn("Set-Cookie", headers)
            self.assertTrue(out["enrolled"])

    def test_sign_out_holds_from_the_next_request(self):
        cookie, _secret = self.enrol()
        self.assertEqual(self.get("/status", cookie=cookie)[0], 200)
        self.assertIsNotNone(auth_sessions.session_from_cookie_header(cookie))

        status, headers, out = self.post("/auth/sign-out", cookie=cookie)
        self.assertEqual(status, 200, out)
        self.assertEqual(out, {"signed_in": False})
        self.assertEqual(headers["Set-Cookie"],
                         f"{host_auth.cookie_name()}=; Max-Age=0; Path=/; "
                         "HttpOnly; SameSite=Strict")

        status, _headers, out = self.get("/status", cookie=cookie)
        self.assertEqual(status, 401, out)
        self.assertEqual(out["error"]["code"], "sign_in_required")
        with mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)}):
            self.assertEqual(self.post("/ops/update", cookie=cookie)[0], 401)
        self.assert_nothing_ran()
        self.assertEqual(self.store()["sessions"], [])
        self.assertEqual(self.published()["sessions"], [])
        # The review server's check, on its next request.
        self.assertIsNone(auth_sessions.session_from_cookie_header(cookie))
        # Signing out twice, or with no session at all, is the same answer.
        status, headers, out = self.post("/auth/sign-out", cookie=cookie)
        self.assertEqual((status, out), (200, {"signed_in": False}))
        self.assertEqual(self.post("/auth/sign-out")[0], 200)

    def test_devices_are_listed_and_revoked_from_the_next_request(self):
        laptop, secret = self.enrol(device="laptop")
        self.tick()
        status, headers, out = self.sign_in(
            secret, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                           "Gecko/20100101 Firefox/140.0"})
        self.assertEqual(status, 200, out)
        phone = headers["Set-Cookie"].split(";")[0]
        # With no device name given, a short one from the browser.
        self.assertEqual(out["session"]["device"], "Firefox on Linux")

        status, _headers, out = self.get("/auth/sessions", cookie=laptop)
        self.assertEqual(status, 200, out)
        self.assertEqual(
            [(s["device"], s["current"]) for s in out["sessions"]],
            [("Firefox on Linux", False), ("laptop", True)])
        self.assertEqual(sorted(out["sessions"][0]),
                         ["created_at", "current", "device", "id", "last_seen_at"])
        self.assertNotIn("token", json.dumps(out))
        other = out["sessions"][0]["id"]

        # Each needs a session; an id that is not this user's is not found.
        self.assertEqual(self.get("/auth/sessions")[0], 401)
        self.assertEqual(self.post("/auth/sessions/revoke", {"id": other})[0], 401)
        for missing in ("nope", None, 7, ""):
            status, _headers, out = self.post(
                "/auth/sessions/revoke", {"id": missing}, cookie=laptop)
            self.assertEqual(status, 404, out)
            self.assertEqual(out["error"]["code"], "session_not_found")

        self.assertEqual(self.get("/status", cookie=phone)[0], 200)
        status, headers, out = self.post(
            "/auth/sessions/revoke", {"id": other}, cookie=laptop)
        self.assertEqual(status, 200, out)
        self.assertEqual(out, {"revoked": other, "current": False})
        self.assertNotIn("Set-Cookie", headers)
        # The revoked device, on its very next request, to either server.
        self.assertEqual(self.get("/status", cookie=phone)[0], 401)
        self.assertIsNone(auth_sessions.session_from_cookie_header(phone))
        self.assertFalse(self.get("/auth/status", cookie=phone)[2]["signed_in"])
        # The other is untouched.
        self.assertEqual(self.get("/status", cookie=laptop)[0], 200)
        self.assertEqual(auth_sessions.session_from_cookie_header(laptop)["user_id"], 1)

        # Revoking this device is a sign-out.
        own = self.get("/auth/sessions", cookie=laptop)[2]["sessions"][0]["id"]
        status, headers, out = self.post(
            "/auth/sessions/revoke", {"id": own}, cookie=laptop)
        self.assertEqual(out, {"revoked": own, "current": True})
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.get("/status", cookie=laptop)[0], 401)

    def test_a_cookie_counts_only_from_the_pinned_gui_origin(self):
        cookie, _secret = self.enrol()
        self.assertEqual(self.get("/status", cookie=cookie)[0], 200)
        # No Origin (not a browser page of the GUI): the cookie is not read.
        status, _headers, out = self.get("/status", cookie=cookie, origin=None)
        self.assertEqual(status, 401, out)
        # Another loopback page is "same site" for the browser, so it would
        # send the cookie. Its Origin is what stops it.
        for origin in (f"http://127.0.0.1:{GUI_PORT + 1}", "http://localhost:5173",
                       "https://attacker.example"):
            status, headers, out = self.get("/status", cookie=cookie, origin=origin)
            self.assertEqual(status, 403, (origin, out))
            self.assertNotIn("Access-Control-Allow-Credentials", headers)
            status, _headers, out = self.post("/auth/sign-out", cookie=cookie,
                                              origin=origin)
            self.assertEqual(status, 403, (origin, out))
        self.assertEqual(len(self.store()["sessions"]), 1)
        # The GUI's other name is its origin too.
        status, headers, _out = self.get(
            "/status", cookie=cookie, origin=f"http://localhost:{GUI_PORT}",
            host=f"localhost:{self.port}")
        self.assertEqual(status, 200)

    def test_auth_posts_take_json_and_nothing_else(self):
        _cookie, secret = self.enrol()
        raw = {"username": "Operator", "password": PASSWORD, "code": self.code(secret)}
        # A form post needs no preflight from a foreign page; it is refused.
        status, _headers, out = base.http_call(
            self.port, "PUT", "/auth/sign-in", origin=GUI, body=raw)
        self.assertEqual(status, 405, out)
        conn_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("POST", "/auth/sign-in", body="username=Operator",
                         headers={**conn_headers, "Origin": GUI})
            response = conn.getresponse()
            self.assertEqual(response.status, 415)
            self.assertEqual(json.loads(response.read())["error"]["code"],
                             "json_required")
        finally:
            conn.close()
        for body in ([1, 2], "text", None):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            try:
                conn.request("POST", "/auth/sign-in", body=json.dumps(body),
                             headers={"Content-Type": "application/json",
                                      "Origin": GUI})
                self.assertEqual(conn.getresponse().status, 400, body)
            finally:
                conn.close()
        # A body announced as larger than any sign-in needs is not read.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("POST", "/auth/sign-in", body="{}",
                         headers={"Content-Type": "application/json", "Origin": GUI,
                                  "Content-Length": "20000"})
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()
        self.assertEqual(self.store()["rate"]["failures"], 0)
        for path in ("/auth/nope", "/auth/", "/auth/first", "/auth/sessions"):
            self.assertEqual(self.post(path, raw)[0], 404, path)
        self.assertEqual(self.get("/auth/sign-in")[0], 404)


class RateLimitTest(AuthFixture):
    """Failed attempts are slowed, per install, and the count is on disk."""

    def fail(self, secret: str):
        return self.sign_in(secret, password="wrong")

    def wait(self) -> float:
        rate = self.store()["rate"]
        return round((rate["blocked_until"] or self.now) - self.now, 3)

    def test_delay_grows_and_covers_password_code_and_claim(self):
        _cookie, secret = self.enrol()
        claim = self.add_user("Second")
        # The first failures cost nothing.
        free = (lambda: self.fail(secret),
                lambda: self.sign_in(secret, code="000000"),
                lambda: self.begin("Second", "WRONG-CODE"))
        self.assertEqual(len(free), host_auth.RATE_FREE)
        for attempt in free:
            status, _headers, out = attempt()
            self.assertEqual(status, 401, out)
            self.assertEqual(self.wait(), 0)
        self.assertEqual(self.get("/auth/status")[2]["retry_after"], 0)

        # From then on each failure doubles the wait, whatever kind it is.
        kinds = (
            lambda: self.fail(secret),
            lambda: self.sign_in(secret, code="000000"),
            lambda: self.begin("Second", "WRONG-CODE"),
            lambda: self.sign_in(secret, username="nobody"),
        )
        waits = []
        for attempt in range(12):
            status, _headers, out = kinds[attempt % len(kinds)]()
            self.assertEqual(status, 401, (attempt, out))
            waits.append(self.wait())

            # While waiting, nothing is looked at: not even correct details.
            with mock.patch.object(host_auth, "_scrypt") as scrypt:
                status, headers, out = self.sign_in(secret)
                self.assertEqual(status, 429, out)
                self.assertEqual(out["error"]["code"], "rate_limited")
                self.assertEqual(headers["Retry-After"],
                                 str(out["error"]["details"]["retry_after"]))
                self.assertEqual(out["error"]["details"]["unlock_cli"],
                                 "./sc auth unlock")
                self.assertEqual(self.begin("Second", claim)[0], 429)
                scrypt.assert_not_called()
            self.assertEqual(self.get("/auth/status")[2]["retry_after"],
                             math.ceil(waits[-1]))
            # A refused attempt does not lengthen the wait.
            self.assertEqual(self.wait(), waits[-1])
            self.tick(waits[-1] - 0.5)
            self.assertEqual(self.sign_in(secret)[0], 429)
            self.tick(0.75)
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300])
        self.assertEqual(max(waits), host_auth.RATE_MAX_SECONDS)

        # Once the wait is over, correct details sign in and clear the count.
        status, _headers, out = self.sign_in(secret)
        self.assertEqual(status, 200, out)
        self.assertEqual(self.store()["rate"],
                         {"failures": 0, "last_failure": None, "blocked_until": None})
        # The claim code was never used up by the wrong guesses.
        self.assertEqual(self.begin("Second", claim)[0], 200)

    def test_a_wrong_confirming_code_counts_too(self):
        claim = self.add_user()
        _status, _headers, begun = self.begin("Operator", claim)
        for _ in range(host_auth.RATE_FREE + 1):
            status, _headers, out = self.post("/auth/first/confirm", {
                "enrolment": begun["enrolment"], "password": PASSWORD,
                "code": "000000"})
            self.assertEqual(status, 401, out)
            self.assertEqual(out["error"]["code"], "invalid_code")
        self.assertEqual(self.store()["rate"]["failures"], host_auth.RATE_FREE + 1)
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": PASSWORD,
            "code": self.code(begun["secret"])})
        self.assertEqual(status, 429, out)
        self.assertEqual(self.store()["users"][0]["state"], "awaiting")

    def test_quiet_time_and_a_clock_set_back_do_not_lock_the_operator_out(self):
        _cookie, secret = self.enrol()
        for _ in range(host_auth.RATE_FREE + 3):
            self.fail(secret)
            self.tick(self.wait() + 0.1)
        self.assertEqual(self.store()["rate"]["failures"], host_auth.RATE_FREE + 3)
        # A long quiet spell: the count starts again.
        self.tick(host_auth.RATE_QUIET_SECONDS + 1)
        self.assertEqual(self.fail(secret)[0], 401)
        self.assertEqual(self.store()["rate"]["failures"], 1)
        self.assertEqual(self.wait(), 0)
        # A wait recorded against a clock that was then set back reads as
        # longer than any wait can be; it is void, not a lockout.
        for _ in range(host_auth.RATE_FREE + 2):
            self.fail(secret)
            self.tick(self.wait() + 0.1)
        self.fail(secret)
        self.assertGreater(self.wait(), 0)
        with host_auth._locked():
            store = host_auth.load()
            store["rate"]["blocked_until"] = self.now + 7 * 24 * 3600
            store["rate"]["last_failure"] = self.now + 7 * 24 * 3600 - 300
            engine_ops.write_private(host_auth.store_path(), store)
        self.assertEqual(self.get("/auth/status")[2]["retry_after"], 0)
        self.assertEqual(self.sign_in(secret)[0], 200)

    def test_host_cli_clears_the_delay(self):
        _cookie, secret = self.enrol()
        for _ in range(host_auth.RATE_FREE + 6):
            self.fail(secret)
            self.tick(self.wait() + 0.1)
        self.fail(secret)
        self.assertEqual(self.sign_in(secret)[0], 429)
        code, out, _err = self.cli("unlock")
        self.assertEqual(code, 0)
        self.assertIn("cleared", out)
        self.assertEqual(self.sign_in(secret)[0], 200)

    def test_limit_survives_a_service_restart(self):
        """The count is in the store, so a NEW service process, started over
        the same private directory, still refuses."""
        # A little in the past, so that by the end this test's clock has
        # caught up with the real one the new process will run on, and the
        # step the first sign-in spent is well behind it.
        self.now = time.time() - 100
        _cookie, secret = self.enrol()
        for _ in range(host_auth.RATE_FREE + 7):
            self.fail(secret)
            self.tick(self.wait() + 0.01)
        self.fail(secret)
        rate = self.store()["rate"]
        self.assertEqual(rate["failures"], host_auth.RATE_FREE + 8)
        remaining = rate["blocked_until"] - time.time()
        self.assertTrue(60 < remaining <= host_auth.RATE_MAX_SECONDS, remaining)

        # This process's service goes away entirely.
        port = self.restart_as_a_new_process()

        def attempt():
            # That process runs on the real clock.
            return base.http_call(
                port, "POST", "/auth/sign-in", origin=GUI,
                body={"username": "Operator", "password": PASSWORD,
                      "code": host_auth.totp_at(secret, time.time())})

        status, headers, out = attempt()
        self.assertEqual(status, 429, out)
        self.assertEqual(out["error"]["code"], "rate_limited")
        self.assertGreater(int(headers["Retry-After"]), 25)
        # It was the limit and nothing else: cleared on the host, the same
        # request signs in.
        self.assertEqual(self.cli("unlock")[0], 0)
        status, headers, out = attempt()
        self.assertEqual(status, 200, out)
        self.assertIn("HttpOnly", headers["Set-Cookie"])


# ── acceptance 9: first sign-in ──────────────────────────────────────────────

class FirstSignInTest(AuthFixture):
    def test_user_add_creates_an_awaiting_user_with_only_a_claim_hash(self):
        self.assertEqual(self.get("/auth/status")[2]["enrolled"], False)
        # An install pinned before sign-in existed: `./sc launch` says what
        # is missing.
        self.assertIn("no GUI user yet", engine_ops.sign_in_summary())
        self.assertIn("./sc auth user add <username>", engine_ops.sign_in_summary())
        code, out, err = self.cli("user", "add", "operator", "--port", str(GUI_PORT))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(engine_ops.sign_in_summary(), "")
        claim = re.search(r"claim code:\s+(\S+)", out).group(1)
        self.assertRegex(claim, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertIn("awaiting first sign-in", out)
        self.assertIn(f"http://127.0.0.1:{GUI_PORT}", out)

        user = self.store()["users"][0]
        # Keyed by the engine's users.user_id, under the engine's spelling.
        self.assertEqual((user["user_id"], user["username"], user["state"]),
                         (1, "Operator", "awaiting"))
        self.assertIsNone(user["password"])
        self.assertIsNone(user["totp_secret"])
        self.assertIsNone(user["totp_last_step"])
        self.assertEqual(user["claim"]["algo"], "scrypt")
        self.assertEqual(self.store()["sessions"], [])
        # The code itself is nowhere, with or without its dash.
        self.assert_nowhere_on_disk(claim)
        self.assert_nowhere_on_disk(claim.replace("-", ""))
        # Nothing was written to the engine DB.
        con = sqlite3.connect(self.engine_db)
        self.assertEqual(
            con.execute("SELECT password_hash, password_salt FROM users "
                        "WHERE user_id=1").fetchone(), (None, None))
        con.close()

        status, _headers, out = self.get("/auth/status")
        self.assertEqual(
            (out["enrolled"], out["signed_in"], out["pinned"], out["gui_origin"]),
            (True, False, True, GUI))
        self.assertTrue(self.published()["enrolled"])
        self.assertTrue(auth_sessions.enrolled())

    def test_the_form_a_username_gets(self):
        self.add_user()
        for username, expected in (("Operator", True), ("operator", True),
                                   (" OPERATOR ", True), ("nobody", False),
                                   ("Second", False), ("", False), (None, False)):
            status, _headers, out = self.post("/auth/identify", {"username": username})
            self.assertEqual((status, out), (200, {"first_sign_in": expected}), username)
        self.assertEqual(self.store()["rate"]["failures"], 0)

    def test_first_sign_in_needs_the_claim_code_and_a_confirmed_code(self):
        claim = self.add_user()
        before = (self.private / "auth.json").read_bytes()

        # A wrong claim code, an unknown user, a user who is not awaiting.
        refusals = []
        for username, code in (("Operator", "AAAA-BBBB"), ("Operator", ""),
                               ("nobody", claim), ("Second", claim),
                               ("Operator", None)):
            status, headers, out = self.begin(username, code)
            self.assertEqual(status, 401, (username, out))
            self.assertEqual(out["error"]["code"], "invalid_claim")
            self.assertNotIn("secret", json.dumps(out))
            refusals.append(out)
            host_auth.unlock()
        self.assertTrue(all(out == refusals[0] for out in refusals))

        status, _headers, begun = self.begin("operator", claim.lower())
        self.assertEqual(status, 200, begun)
        self.assertEqual(sorted(begun), [
            "algorithm", "digits", "enrolment", "expires_in", "issuer",
            "otpauth_uri", "password_min", "period", "secret", "username"])
        self.assertEqual(begun["otpauth_uri"],
                         host_auth.otpauth_uri("Operator", begun["secret"]))
        uri = urlparse(begun["otpauth_uri"])
        self.assertEqual(parse_qs(uri.query)["secret"], [begun["secret"]])
        self.assertEqual(unquote(uri.path), "/Subfloor:Operator")

        # Nothing is saved before confirmation: the user is as the CLI left
        # it, and the secret exists in the service's memory only.
        self.assertEqual((self.private / "auth.json").read_bytes(), before)
        self.assert_nowhere_on_disk(begun["secret"])
        self.assertEqual(self.published()["sessions"], [])

        confirm = {"enrolment": begun["enrolment"], "password": PASSWORD,
                   "code": self.code(begun["secret"])}
        # Not without the authenticator's code …
        for code in ("000000", "", None, self.code(begun["secret"], 3)):
            status, _headers, out = self.post(
                "/auth/first/confirm", {**confirm, "code": code})
            self.assertEqual(status, 401, (code, out))
            self.assertEqual(out["error"]["code"], "invalid_code")
            self.assertIn("clock", out["error"]["message"])
            host_auth.unlock()
        # … nor with a password that is not one …
        for password in ("short", "", None, "x" * 300):
            status, _headers, out = self.post(
                "/auth/first/confirm", {**confirm, "password": password})
            self.assertEqual(status, 400, (password, out))
            self.assertEqual(out["error"]["code"], "password_unacceptable")
        # … nor without the enrolment the claim code opened.
        for enrolment in ("guess", "", None, begun["enrolment"] + "x"):
            status, _headers, out = self.post(
                "/auth/first/confirm", {**confirm, "enrolment": enrolment})
            self.assertEqual(status, 409, (enrolment, out))
            self.assertEqual(out["error"]["code"], "enrolment_expired")
        user = self.store()["users"][0]
        self.assertEqual((user["state"], user["password"], user["totp_secret"]),
                         ("awaiting", None, None))
        self.assertEqual(self.store()["sessions"], [])

        # With all three: saved, and the first session issued.
        status, headers, out = self.post(
            "/auth/first/confirm", {**confirm, "device": "desk"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["user"], {"user_id": 1, "username": "Operator"})
        self.assertEqual(out["session"]["device"], "desk")
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        user = self.store()["users"][0]
        self.assertEqual(user["state"], "active")
        self.assertEqual(user["totp_secret"], begun["secret"])
        self.assertEqual(user["password"]["algo"], "scrypt")
        self.assertIsNone(user["claim"])
        self.assert_nowhere_on_disk(PASSWORD)
        self.assertEqual(self.get("/status", cookie=cookie)[0], 200)

        # The confirming code is spent: it does not also sign in.
        status, _headers, out = self.sign_in(begun["secret"], code=confirm["code"])
        self.assertEqual(status, 401, out)
        host_auth.unlock()
        self.tick()
        self.assertEqual(self.sign_in(begun["secret"])[0], 200)
        self.assertEqual(self.post("/auth/identify", {"username": "Operator"})[2],
                         {"first_sign_in": False})

    def test_a_claim_code_works_once_and_a_completed_user_is_not_reclaimed(self):
        claim = self.add_user()
        _status, _headers, begun = self.begin("Operator", claim)
        # A second browser that also holds the claim code, before the first
        # confirms: the newer enrolment replaces the older.
        _status, _headers, second = self.begin("Operator", claim)
        self.assertNotEqual(second["secret"], begun["secret"])
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": PASSWORD,
            "code": self.code(begun["secret"])})
        self.assertEqual(status, 409, out)
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": second["enrolment"], "password": PASSWORD,
            "code": self.code(second["secret"])})
        self.assertEqual(status, 200, out)
        saved = self.store()["users"][0]

        # Reused: the same code, the same username.
        status, _headers, out = self.begin("Operator", claim)
        self.assertEqual(status, 401, out)
        self.assertEqual(out["error"]["code"], "invalid_claim")
        host_auth.unlock()
        # The finished enrolment cannot be confirmed again either.
        self.tick()
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": second["enrolment"], "password": "another password",
            "code": self.code(second["secret"])})
        self.assertEqual(status, 409, out)
        # And `user add` refuses a user that exists.
        code, _out, err = self.cli("user", "add", "Operator", "--port", str(GUI_PORT))
        self.assertEqual(code, 1)
        self.assertIn("already has a GUI user", err)
        self.assertEqual(self.store()["users"][0], saved)
        self.assertEqual(self.sign_in(second["secret"])[0], 200)

    def test_an_enrolment_does_not_outlive_its_time_or_the_service(self):
        claim = self.add_user()
        _status, _headers, begun = self.begin("Operator", claim)
        confirm = {"enrolment": begun["enrolment"], "password": PASSWORD}
        self.tick(host_auth.ENROLMENT_SECONDS + 1)
        status, _headers, out = self.post(
            "/auth/first/confirm", {**confirm, "code": self.code(begun["secret"])})
        self.assertEqual(status, 409, out)
        # A restarted service remembers no enrolment: nothing was saved.
        _status, _headers, begun = self.begin("Operator", claim)
        host_auth._pending.clear()
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": PASSWORD,
            "code": self.code(begun["secret"])})
        self.assertEqual(status, 409, out)
        self.assertEqual(self.store()["users"][0]["state"], "awaiting")
        # The claim code still works: it is used up only by a confirmation.
        self.assertEqual(self.begin("Operator", claim)[0], 200)

    def test_reset_returns_the_user_to_awaiting_and_ends_every_session(self):
        cookie, secret = self.enrol()
        self.tick()
        status, headers, _out = self.sign_in(secret)
        second = headers["Set-Cookie"].split(";")[0]
        for held in (cookie, second):
            self.assertEqual(self.get("/status", cookie=held)[0], 200)
        code, out, err = self.cli("user", "reset", "operator")
        self.assertEqual(code, 0, out + err)
        claim = re.search(r"claim code:\s+(\S+)", out).group(1)
        self.assertIn("every session cleared", out)

        user = self.store()["users"][0]
        self.assertEqual(
            (user["state"], user["password"], user["totp_secret"],
             user["totp_last_step"]), ("awaiting", None, None, None))
        self.assertEqual(user["claim"]["algo"], "scrypt")
        self.assertEqual(self.store()["sessions"], [])
        self.assertEqual(self.published()["sessions"], [])
        self.assert_nowhere_on_disk(secret)
        # Every session, from the next request, on both servers.
        for held in (cookie, second):
            self.assertEqual(self.get("/status", cookie=held)[0], 401)
            self.assertIsNone(auth_sessions.session_from_cookie_header(held))
        # The old password and authenticator no longer sign in.
        self.tick()
        self.assertEqual(self.sign_in(secret)[0], 401)
        host_auth.unlock()
        self.assertEqual(self.post("/auth/identify", {"username": "Operator"})[2],
                         {"first_sign_in": True})

        # First sign-in again, with the new code; a reset in the middle of it
        # ends that enrolment and replaces the code.
        _status, _headers, begun = self.begin("Operator", claim)
        code, out, _err = self.cli("user", "reset", "Operator")
        newer = re.search(r"claim code:\s+(\S+)", out).group(1)
        self.assertNotEqual(newer, claim)
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": PASSWORD,
            "code": self.code(begun["secret"])})
        self.assertEqual(status, 409, out)
        self.assertEqual(self.begin("Operator", claim)[0], 401)
        host_auth.unlock()
        _status, _headers, begun = self.begin("Operator", newer)
        status, _headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": "a new password",
            "code": self.code(begun["secret"])})
        self.assertEqual(status, 200, out)
        self.assertNotEqual(begun["secret"], secret)

        code, _out, err = self.cli("user", "reset", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("no GUI user", err)


class UnpinnedAuthTest(AuthFixture):
    """Before `./sc auth user add`: nobody can sign in, and the page can say why."""

    pinned = False

    def test_nothing_is_reachable_until_a_user_is_created(self):
        for origin in (GUI, "http://127.0.0.1:8813", None):
            status, headers, out = self.get("/auth/status", origin=origin)
            self.assertEqual(status, 200, out)
            self.assertEqual(
                (out["pinned"], out["enrolled"], out["signed_in"], out["gui_origin"]),
                (False, False, False, None))
            self.assertEqual(out["setup_cli"], "./sc auth user add <username>")
            self.assertIn("./sc auth user add <username>", out["reason"])
            self.assertNotIn("Access-Control-Allow-Credentials", headers)
            self.assertNotIn("Set-Cookie", headers)
        for path in ("/auth/sign-in", "/auth/first/begin", "/auth/first/confirm",
                     "/auth/identify", "/auth/sign-out", "/auth/sessions/revoke"):
            for origin in (GUI, "http://127.0.0.1:8813"):
                status, headers, out = self.post(
                    path, {"username": "Operator", "password": PASSWORD,
                           "code": "000000"}, origin=origin)
                self.assertEqual(status, 409, (path, out))
                self.assertEqual(out["error"]["code"], "gui_origin_unpinned")
                self.assertEqual(out["error"]["details"]["setup_cli"],
                                 "./sc auth user add <username>")
                self.assertNotIn("Set-Cookie", headers)
        self.assertFalse((self.private / "auth.json").exists())
        self.assertEqual(self.get("/auth/status", origin="https://attacker.example")[0], 403)

    def test_a_session_left_behind_without_a_pin_is_not_honoured(self):
        token = base.seed_session()
        cookie = base.cookie_for(self.root, token)
        out = self.get("/auth/status", cookie=cookie)[2]
        self.assertEqual((out["pinned"], out["signed_in"], out["enrolled"]),
                         (False, False, True))
        self.assertEqual(self.post("/ops/update", cookie=cookie)[0], 409)
        self.assertEqual(self.get("/auth/sessions", cookie=cookie)[0], 401)
        self.assert_nothing_ran()


# ── acceptance 5, and the finding carried from the review of #1772 ───────────

class EngineOpsNeedASessionTest(AuthFixture):
    """A process that can reach the port can write any Host and Origin it
    likes (a sandbox launched with `SC_NET=host` shares the host's network
    namespace). What it cannot write is a session token: it has only ever
    seen hashes."""

    def setUp(self) -> None:
        super().setUp()
        self.cookie_header, self.secret = self.enrol()
        patcher = mock.patch.dict(engine_ops.VERBS, {"update": ("noop",)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def forged(self, method: str, path: str, cookie: str | None):
        """Exactly what the reviewer sent: the service's own loopback Host,
        the pinned GUI origin, no Authorization — and whatever cookie."""
        return base.http_call(
            self.port, method, path, host=f"127.0.0.1:{self.port}", origin=GUI,
            cookie=cookie, body={} if method == "POST" else None)

    def presented(self) -> dict[str, str | None]:
        name = host_auth.cookie_name()
        published = self.published()["sessions"]
        self.assertEqual(len(published), 1)
        digest = published[0]["hash"]
        return {
            "no cookie": None,
            "a well-formed token nobody issued": f"{name}={'A' * 43}",
            "a random well-formed token":
                f"{name}={base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')}",
            # What a sandbox can read from the mounted list, presented as if
            # it were a token: as published, and re-encoded to look like one.
            "the published hash": f"{name}={digest}",
            "the published hash, shaped like a token":
                f"{name}={base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip('=')}",
            "the stored hash from another cookie name": f"sc_session_00000000={digest}",
            "an empty cookie": f"{name}=",
        }

    def test_a_forged_origin_without_the_session_starts_nothing(self):
        for label, cookie in self.presented().items():
            for op in ("update", "restart", "update-harnesses"):
                status, headers, out = self.forged("POST", f"/ops/{op}", cookie)
                self.assertEqual(status, 401, (label, op, out))
                self.assertEqual(out["error"]["code"], "sign_in_required")
                self.assertEqual(out["error"]["details"],
                                 {"auth_status": "/auth/status"})
                self.assertNotIn("Set-Cookie", headers)
            # Not even whether an operation exists.
            status, _headers, out = self.forged("POST", "/ops/no-such-op", cookie)
            self.assertEqual(status, 401, (label, out))
            status, _headers, out = self.forged("GET", "/status", cookie)
            self.assertEqual(status, 401, (label, out))
            self.assertEqual(out["error"]["code"], "sign_in_required")
            self.assertNotIn("runtime", out)
            self.assertNotIn("log_tail", out)
            self.assertEqual(self.forged("GET", "/auth/sessions", cookie)[0], 401, label)
            self.assertEqual(
                self.forged("POST", "/auth/sessions/revoke", cookie)[0], 401, label)
            status, _headers, out = self.forged("GET", "/auth/status", cookie)
            self.assertEqual((status, out["signed_in"], out["user"]),
                             (200, False, None), label)
        time.sleep(0.5)
        # The stub `sc` was never run and no operation was recorded.
        self.assert_nothing_ran()
        # The operator's own session was not disturbed.
        self.assertEqual(len(self.store()["sessions"]), 1)

    def test_the_same_request_with_the_session_is_served(self):
        status, _headers, out = self.forged("GET", "/status", self.cookie_header)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["runtime"], "host")
        status, _headers, out = self.forged("POST", "/ops/update", self.cookie_header)
        self.assertEqual(status, 202, out)
        self.finished()
        self.assertEqual(self.stub_argv(), ["noop"])

    def test_a_shell_credential_does_not_stand_in_for_the_session(self):
        for headers in ({"Authorization": "Bearer shell-token"},
                        {"Authorization": f"Bearer {self.cookie_header.split('=')[1]}"}):
            status, _headers, out = base.http_call(
                self.port, "POST", "/ops/update", origin=GUI, headers=headers,
                cookie=self.cookie_header)
            self.assertEqual(status, 403, out)
            self.assertEqual(out["error"]["code"], "fnb_operator_required")
        self.assert_nothing_ran()

    def test_the_published_list_signs_nobody_in(self):
        """Everything the sandbox can read, tried every way against sign-in."""
        published = self.published()
        digest = published["sessions"][0]["hash"]
        self.assertEqual(
            sorted(published), ["cookie", "enrolled", "sessions", "updated_at", "version"])
        for body in ({"username": "Operator", "password": digest, "code": digest[:6]},
                     {"username": "Operator", "password": PASSWORD, "code": digest[:6]},
                     {"username": digest, "password": digest, "code": "000000"}):
            host_auth.unlock()
            self.assertEqual(self.post("/auth/sign-in", body)[0], 401)
        # The review server's own check refuses the hash too.
        name = published["cookie"]
        self.assertIsNone(auth_sessions.session_from_cookie_header(f"{name}={digest}"))
        self.assertIsNone(auth_sessions.verify_token(digest))
        self.assertIsNotNone(auth_sessions.session_from_cookie_header(self.cookie_header))

    def test_hammering_sign_in_slows_the_operator_and_does_not_end_sessions(self):
        """What `--network host` still buys a sandbox: it can keep the failure
        delay at its ceiling. It gains no session, the operator's existing
        sessions keep working, and the host CLI clears the delay."""
        for _ in range(40):
            status, _headers, _out = self.forged("POST", "/auth/sign-in", None)
            self.assertEqual(status, 401)
            until = self.store()["rate"]["blocked_until"]
            self.tick(until - self.now + 0.25 if until else 0)
        self.forged("POST", "/auth/sign-in", None)
        rate = self.store()["rate"]
        self.assertAlmostEqual(rate["blocked_until"] - self.now,
                               host_auth.RATE_MAX_SECONDS, places=3)
        # However long it goes on, the wait never passes the ceiling.
        for _ in range(5):
            self.assertEqual(self.forged("POST", "/auth/sign-in", None)[0], 429)
        self.assertEqual(self.store()["rate"], rate)
        # The operator signing in on a new device is refused for now …
        status, _headers, out = self.sign_in(self.secret)
        self.assertEqual(status, 429, out)
        self.assertLessEqual(out["error"]["details"]["retry_after"],
                             host_auth.RATE_MAX_SECONDS + 1)
        # … the browser already signed in is not affected at all …
        self.assertEqual(self.get("/status", cookie=self.cookie_header)[0], 200)
        status, _headers, out = self.post("/ops/update", cookie=self.cookie_header)
        self.assertEqual(status, 202, out)
        self.finished()
        self.assertEqual(len(self.store()["sessions"]), 1)
        # … and one host command lets the operator in.
        self.assertEqual(self.cli("unlock")[0], 0)
        self.assertEqual(self.sign_in(self.secret)[0], 200)


# ── two files, one rule: stopped anywhere, it fails closed ───────────────────

class InterruptedCommitTest(AuthFixture):
    """A change is two or three atomic writes. A session exists only while
    its hash is in both the store and the published list, so a write that
    fails (or a crash) between them leaves a removed session removed for both
    servers, after a restart too, and a new session accepted by neither."""

    def fresh(self):
        """An active user with two sessions. Returns (victim, keeper, secret)."""
        host_auth.unlock()
        if self.store_users():
            _user, claim = host_auth.reset_user("Operator")
        else:
            claim = self.add_user()
        _status, _headers, begun = self.begin("Operator", claim)
        status, headers, out = self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": PASSWORD,
            "code": self.code(begun["secret"]), "device": "victim"})
        self.assertEqual(status, 200, out)
        victim = headers["Set-Cookie"].split(";")[0]
        self.tick()
        status, headers, out = self.sign_in(begun["secret"], device="keeper")
        self.assertEqual(status, 200, out)
        self.tick()
        return victim, headers["Set-Cookie"].split(";")[0], begun["secret"]

    def store_users(self) -> list:
        path = self.private / host_auth.STORE_NAME
        return json.loads(path.read_text())["users"] if path.exists() else []

    def session_id(self, device: str) -> str:
        return next(s["id"] for s in host_auth.all_sessions() if s["device"] == device)

    def operations(self):
        """name -> (run, cookies it must end). `run` returns True on success."""
        def http(path, body, cookie):
            return lambda: self.post(path, body(), cookie=cookie())[0] == 200

        def cli(*argv):
            return lambda: self.cli(*(a() if callable(a) else a for a in argv))[0] == 0

        victim_id = lambda: self.session_id("victim")       # noqa: E731
        return {
            "revoke from another device": (
                http("/auth/sessions/revoke", lambda: {"id": victim_id()},
                     lambda: self.keeper), ("victim",)),
            "sign-out": (
                http("/auth/sign-out", dict, lambda: self.victim), ("victim",)),
            "./sc auth sessions revoke <id>": (
                cli("sessions", "revoke", victim_id), ("victim",)),
            "./sc auth sessions revoke --all": (
                cli("sessions", "revoke", "--all"), ("victim", "keeper")),
            "./sc auth user reset": (
                cli("user", "reset", "Operator"), ("victim", "keeper")),
        }

    def test_an_interrupted_revocation_stays_revoked(self):
        for name, (run, ended) in self.operations().items():
            interrupted_after_a_write = 0
            for index in range(8):
                self.victim, self.keeper, _secret = self.fresh()
                cookies = {"victim": self.victim, "keeper": self.keeper}
                for cookie in cookies.values():
                    self.assertEqual(self.accepted_by(cookie), (True, True))
                with self.write_fails(index) as (seen, state):
                    succeeded = run()
                label = f"{name}, write {index} fails after {seen}"
                if not state["hit"]:
                    # Every write point has been tried; this run was whole.
                    self.assertTrue(succeeded, label)
                    for who in ended:
                        self.assertEqual(self.accepted_by(cookies[who]),
                                         (False, False), label)
                    break
                # It did not finish, and said so.
                self.assertFalse(succeeded, label)
                # If nothing was written, nothing changed. Otherwise the
                # sessions it was ending are ended, for BOTH servers.
                expected = (False, False) if seen else (True, True)
                for who in ended:
                    self.assertEqual(self.accepted_by(cookies[who]), expected, label)
                # A restart reads the same two files by the same rule. It does
                # not bring a session back.
                host_auth.republish()
                for who in ended:
                    self.assertEqual(self.accepted_by(cookies[who]), expected,
                                     label + ", after a restart")
                if seen:
                    interrupted_after_a_write += 1
                    digests = {auth_sessions.token_hash(cookies[who].split("=")[1])
                               for who in ended}
                    self.assertFalse(digests & {
                        s["hash"] for s in self.published()["sessions"]}, label)
                    self.assertFalse(digests & {
                        s["token_sha256"] for s in self.store()["sessions"]}, label)
                # A session it was not ending is untouched throughout.
                for who in set(cookies) - set(ended):
                    self.assertEqual(self.accepted_by(cookies[who]), (True, True), label)
                # And the operation, run again, completes (unless it names a
                # session by id, and that session is already gone).
                by_id = name in ("revoke from another device",
                                 "./sc auth sessions revoke <id>")
                if not (seen and by_id):
                    self.assertTrue(run(), label + ", run again")
                    for who in ended:
                        self.assertEqual(self.accepted_by(cookies[who]),
                                         (False, False), label + ", run again")
            else:
                self.fail(f"{name} never completed")
            # The interesting case was really exercised: a failure after the
            # list changed and before the store did, and after both.
            self.assertGreaterEqual(interrupted_after_a_write, 2, name)

    def test_the_cli_says_what_an_interrupted_command_left(self):
        self.victim, self.keeper, _secret = self.fresh()
        with self.write_fails(1):
            code, out, err = self.cli("user", "reset", "Operator")
        self.assertEqual(code, 1)
        self.assertIn("a write failed", err)
        self.assertIn("already signed out", err)
        self.assertNotIn("claim code", out)
        self.assertEqual(self.accepted_by(self.victim), (False, False))

    def test_a_new_session_is_accepted_nowhere_until_both_writes_land(self):
        _first, secret = self.enrol()
        hits = 0
        for index in range(6):
            host_auth.unlock()
            self.tick()
            with self.write_fails(index) as (seen, state):
                status, headers, out = self.sign_in(secret, device="new")
            label = f"sign-in, write {index} fails after {seen}"
            if not state["hit"]:
                self.assertEqual(status, 200, label)
                self.assertEqual(self.accepted_by(headers["Set-Cookie"].split(";")[0]),
                                 (True, True))
                break
            hits += 1
            self.assertEqual(status, 500, label)
            # No token was handed out, and no session exists for either
            # server: not now, and not after a restart.
            self.assertNotIn("Set-Cookie", headers)
            self.assertNotIn("session", out)
            for moment in ("now", "after a restart"):
                self.assertEqual(len(self.published()["sessions"]), 1, label + moment)
                self.assertEqual([s["device"] for s in host_auth.all_sessions()],
                                 ["first browser"], label + moment)
                host_auth.republish()
            self.assertEqual(len(self.store()["sessions"]), 1, label)
        else:
            self.fail("sign-in never completed")
        self.assertEqual(hits, 2)          # the store, then the list

    def test_an_interrupted_first_sign_in_issues_nothing(self):
        claim = self.add_user()
        _status, _headers, begun = self.begin("Operator", claim)
        confirm = {"enrolment": begun["enrolment"], "password": PASSWORD,
                   "code": self.code(begun["secret"])}
        # The store write fails: nothing was saved, and it can be tried again.
        with self.write_fails(0):
            status, headers, out = self.post("/auth/first/confirm", confirm)
        self.assertEqual(status, 500, out)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.store()["users"][0]["state"], "awaiting")
        self.assertIsNone(self.store()["users"][0]["password"])
        # The list write fails: the password and authenticator are saved, but
        # no session exists and no token was handed out. The operator signs
        # in with the next code.
        with self.write_fails(1):
            status, headers, out = self.post("/auth/first/confirm", confirm)
        self.assertEqual(status, 500, out)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.published()["sessions"], [])
        self.assertEqual(host_auth.all_sessions(), [])
        host_auth.republish()
        self.assertEqual(self.store()["sessions"], [])
        self.assertEqual(self.store()["users"][0]["state"], "active")
        self.tick()
        self.assertEqual(self.sign_in(begun["secret"])[0], 200)

    def test_a_restart_never_restores_a_session_the_list_lacks(self):
        """The reviewer's probe, directly: the list lost a hash, the store
        still has it. Neither accepts it, and starting the service again does
        not publish it back."""
        victim, keeper, _secret = self.fresh()
        digest = auth_sessions.token_hash(victim.split("=")[1])
        published = self.published()
        published["sessions"] = [s for s in published["sessions"]
                                 if s["hash"] != digest]
        engine_ops.write_private(self.public / "sessions.json", published)
        self.assertIn(digest, [s["token_sha256"] for s in self.store()["sessions"]])
        self.assertEqual(self.accepted_by(victim), (False, False))
        self.assertEqual(self.get("/auth/status", cookie=victim)[2]["signed_in"], False)
        self.assertEqual(self.post("/auth/sessions/revoke", {"id": "x"},
                                   cookie=victim)[0], 401)
        host_auth.republish()                       # what service start does
        self.assertEqual(self.accepted_by(victim), (False, False))
        self.assertNotIn(digest, [s["hash"] for s in self.published()["sessions"]])
        self.assertNotIn(digest, [s["token_sha256"] for s in self.store()["sessions"]])
        self.assertEqual(self.accepted_by(keeper), (True, True))
        # With no list at all, nobody is signed in to either server.
        (self.public / "sessions.json").unlink()
        self.assertEqual(self.accepted_by(keeper), (False, False))
        host_auth.republish()
        self.assertEqual(self.accepted_by(keeper), (False, False))
        self.assertEqual(self.store()["users"][0]["state"], "active")


class RestartAndRaceTest(AuthFixture):
    """What must hold across a service restart and under simultaneous requests."""

    def test_a_spent_step_stays_spent_across_a_service_restart(self):
        # Enrol a little in the past, then sign in on the real clock, which
        # is the one the new process runs on.
        self.now = time.time() - 90
        _cookie, secret = self.enrol()
        self.now = time.time()
        used = self.code(secret)
        self.assertEqual(self.sign_in(secret, code=used)[0], 200)
        step = host_auth.totp_step(self.now)
        self.assertEqual(self.store()["users"][0]["totp_last_step"], step)

        port = self.restart_as_a_new_process()

        def attempt(code):
            return base.http_call(
                port, "POST", "/auth/sign-in", origin=GUI,
                body={"username": "Operator", "password": PASSWORD, "code": code})

        # The code that signed in before the restart is still inside its
        # window, and is still refused: the step is remembered on disk.
        self.assertLess(time.time() - self.now, 25)
        status, headers, out = attempt(used)
        self.assertEqual(status, 401, out)
        self.assertEqual(out["error"]["code"], "invalid_credentials")
        self.assertNotIn("Set-Cookie", headers)
        # So is the step before it.
        self.assertEqual(attempt(host_auth.totp_at(secret, (step - 1) * 30))[0], 401)
        # It was the replay and nothing else: the next step's code signs in.
        status, headers, out = attempt(host_auth.totp_at(secret, (step + 1) * 30))
        self.assertEqual(status, 200, out)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.store()["users"][0]["totp_last_step"], step + 1)

    def simultaneously(self, count: int, request):
        barrier = threading.Barrier(count)
        results: list = [None] * count

        def run(slot: int) -> None:
            barrier.wait(30)
            results[slot] = request(slot)

        threads = [threading.Thread(target=run, args=(slot,)) for slot in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertNotIn(None, results)
        return results

    def test_simultaneous_attempts_cannot_exceed_the_rate_limit(self):
        _cookie, secret = self.enrol()
        code = self.code(secret)
        allowed = host_auth.RATE_FREE + 1        # the free ones, and the one
        count = 16                               # that starts the wait
        with mock.patch.object(host_auth, "_scrypt", wraps=host_auth._scrypt) as scrypt, \
                mock.patch.object(host_auth, "totp_accept",
                                  wraps=host_auth.totp_accept) as totp:
            results = self.simultaneously(count, lambda _slot: self.sign_in(
                secret, password="wrong", code=code))
        statuses = sorted(status for status, _headers, _out in results)
        self.assertEqual(statuses, [401] * allowed + [429] * (count - allowed))
        # Only that many were ever evaluated, however they arrived.
        self.assertEqual((scrypt.call_count, totp.call_count), (allowed, allowed))
        self.assertEqual(self.store()["rate"]["failures"], allowed)
        self.assertEqual(self.store()["rate"]["blocked_until"], self.now + 1)
        self.assertEqual(len(self.store()["sessions"]), 1)

        # The same with a claim code being guessed.
        host_auth.unlock()
        claim = self.add_user("Second")
        results = self.simultaneously(count, lambda slot: self.begin(
            "Second", f"AAAA-BBB{slot % 8 + 2}"))
        statuses = sorted(status for status, _headers, _out in results)
        self.assertEqual(statuses, [401] * allowed + [429] * (count - allowed))
        host_auth.unlock()
        self.assertEqual(self.begin("Second", claim)[0], 200)

    def test_one_code_signs_in_once_however_many_present_it_together(self):
        _cookie, secret = self.enrol()
        code = self.code(secret)
        results = self.simultaneously(8, lambda _slot: self.sign_in(secret, code=code))
        statuses = sorted(status for status, _headers, _out in results)
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertTrue(set(statuses) <= {200, 401, 429}, statuses)
        self.assertEqual(
            sum(1 for _status, headers, _out in results if "Set-Cookie" in headers), 1)
        self.assertEqual(len(self.store()["sessions"]), 2)
        self.assertEqual(len(self.published()["sessions"]), 2)

    def test_two_simultaneous_confirms_complete_once(self):
        claim = self.add_user()
        _status, _headers, begun = self.begin("Operator", claim)
        passwords = [PASSWORD, "another password entirely"]
        results = self.simultaneously(2, lambda slot: self.post("/auth/first/confirm", {
            "enrolment": begun["enrolment"], "password": passwords[slot],
            "code": self.code(begun["secret"]), "device": f"browser {slot}"}))
        statuses = sorted(status for status, _headers, _out in results)
        self.assertEqual(statuses, [200, 409])
        winner = next(slot for slot, result in enumerate(results) if result[0] == 200)
        loser = results[1 - winner]
        self.assertEqual(loser[2]["error"]["code"], "enrolment_expired")
        self.assertNotIn("Set-Cookie", loser[1])
        # One user, one session, and the winner's password.
        store = self.store()
        self.assertEqual([u["state"] for u in store["users"]], ["active"])
        self.assertEqual([s["device"] for s in store["sessions"]], [f"browser {winner}"])
        self.assertEqual(len(self.published()["sessions"]), 1)
        cookie = results[winner][1]["Set-Cookie"].split(";")[0]
        self.assertEqual(self.accepted_by(cookie), (True, True))
        self.tick()
        self.assertEqual(
            self.sign_in(begun["secret"], password=passwords[1 - winner])[0], 401)
        self.assertEqual(
            self.sign_in(begun["secret"], password=passwords[winner])[0], 200)

    def test_confirm_and_reset_at_once_never_leave_a_claimed_user_or_a_session(self):
        """Both orders, each forced: one is held inside the lock while the
        other arrives, and the other must wait for it."""
        real = host_auth.hash_secret
        for leader in ("confirm", "reset"):
            host_auth.unlock()
            if self.store()["users"]:
                _user, claim = host_auth.reset_user("Operator")
            else:
                claim = self.add_user()
            _status, _headers, begun = self.begin("Operator", claim)
            inside, release = threading.Event(), threading.Event()
            outcome: dict = {}

            def slow(secret):
                inside.set()
                release.wait(30)
                return real(secret)

            def confirm():
                outcome["confirm"] = self.post("/auth/first/confirm", {
                    "enrolment": begun["enrolment"], "password": PASSWORD,
                    "code": self.code(begun["secret"])})

            def reset():
                outcome["reset"] = host_auth.reset_user("Operator")

            jobs = {"confirm": confirm, "reset": reset}
            follower = "reset" if leader == "confirm" else "confirm"
            with mock.patch.object(host_auth, "hash_secret", side_effect=slow):
                first = threading.Thread(target=jobs[leader])
                first.start()
                self.assertTrue(inside.wait(30), leader)
                second = threading.Thread(target=jobs[follower])
                second.start()
                time.sleep(0.4)
                # The second is waiting for the first: nothing interleaves.
                self.assertTrue(second.is_alive(), leader)
                self.assertNotIn(follower, outcome)
                release.set()
                first.join(60)
                second.join(60)
            self.assertEqual(set(outcome), {"confirm", "reset"}, leader)
            status, headers, out = outcome["confirm"]
            _user, new_claim = outcome["reset"]
            if leader == "confirm":
                # It completed, and the reset that followed undid all of it.
                self.assertEqual(status, 200, out)
                cookie = headers["Set-Cookie"].split(";")[0]
                self.assertEqual(self.accepted_by(cookie), (False, False))
            else:
                # The reset ended the enrolment before it could complete.
                self.assertEqual(status, 409, out)
                self.assertEqual(out["error"]["code"], "enrolment_expired")
                self.assertNotIn("Set-Cookie", headers)
            # Either way: awaiting, nothing saved, no session anywhere, the
            # old claim dead, and the reset's claim code the only way in.
            user = self.store()["users"][0]
            self.assertEqual((user["state"], user["password"], user["totp_secret"]),
                             ("awaiting", None, None), leader)
            self.assertEqual(self.store()["sessions"], [], leader)
            self.assertEqual(self.published()["sessions"], [], leader)
            self.assertEqual(self.begin("Operator", claim)[0], 401, leader)
            host_auth.unlock()
            self.tick()
            self.assertEqual(self.sign_in(begun["secret"])[0], 401, leader)
            host_auth.unlock()
            self.assertEqual(
                self.begin("Operator", host_auth.show_claim_code(new_claim))[0], 200)


# ── the review server's side ─────────────────────────────────────────────────

class VerifyTest(AuthFixture):
    """`auth_sessions.session_from_cookie_header`, the one call a verifier makes."""

    def test_absent_or_broken_list_means_nobody_is_signed_in(self):
        token = "A" * 43
        self.assertFalse((self.public / "sessions.json").exists())
        self.assertIsNone(auth_sessions.cookie_name())
        self.assertFalse(auth_sessions.enrolled())
        self.assertIsNone(auth_sessions.verify_token(token))
        self.assertIsNone(
            auth_sessions.session_from_cookie_header(f"sc_session_00000000={token}"))
        self.public.mkdir(mode=0o700, exist_ok=True)
        path = self.public / "sessions.json"
        digest = auth_sessions.token_hash(token)
        good = {"version": 1, "cookie": "sc_session_00000000", "enrolled": True,
                "sessions": [{"hash": digest, "user_id": 1}]}
        for content in ("", "not json", "[]", json.dumps({**good, "version": 2}),
                        json.dumps({**good, "sessions": "x"}),
                        json.dumps({**good, "sessions": [{"hash": digest.upper()}]}),
                        json.dumps({**good, "cookie": "session"})):
            path.write_text(content)
            self.assertIsNone(auth_sessions.session_from_cookie_header(
                f"sc_session_00000000={token}"), content)
        path.write_text(json.dumps(good))
        self.assertEqual(
            auth_sessions.session_from_cookie_header(f"sc_session_00000000={token}"),
            {"hash": digest, "user_id": 1})
        # A link in its place is never followed.
        elsewhere = self.base / "elsewhere.json"
        elsewhere.write_text(json.dumps(good))
        path.unlink()
        path.symlink_to(elsewhere)
        self.assertIsNone(auth_sessions.session_from_cookie_header(
            f"sc_session_00000000={token}"))

    def test_only_this_installs_cookie_and_a_real_token_match(self):
        cookie, _secret = self.enrol()
        name, token = cookie.split("=", 1)
        self.assertEqual(auth_sessions.cookie_name(), name)
        session = auth_sessions.session_from_cookie_header(cookie)
        self.assertEqual(session, {"hash": auth_sessions.token_hash(token),
                                   "user_id": 1})
        # Among other cookies, as a browser sends them.
        self.assertEqual(auth_sessions.session_from_cookie_header(
            f"theme=dark; sc_session_ffffffff={'B' * 43}; {cookie}; other=1"), session)
        for header in (None, "", "garbage", f"{name}=", f"{name}={token[:-1]}",
                       f"{name}={token}x", f"sc_session_ffffffff={token}",
                       f"x{name}={token}", f"{name}={token.swapcase()}", 7):
            self.assertIsNone(auth_sessions.session_from_cookie_header(header), header)
        # Every published hash is compared, each in constant time.
        self.tick()
        with mock.patch.object(auth_sessions.hmac, "compare_digest",
                               wraps=auth_sessions.hmac.compare_digest) as compare:
            auth_sessions.verify_token(token)
        self.assertEqual(compare.call_count, len(self.published()["sessions"]))

    def test_the_sandbox_reads_the_fixed_mount_and_the_host_the_derived_path(self):
        self.assertEqual(str(auth_sessions.SANDBOX_DIR),
                         sandbox_devkit.AUTH_SESSIONS_TARGET)
        with mock.patch.object(auth_sessions, "DIR", None):
            with mock.patch.object(auth_sessions, "in_sandbox", return_value=True):
                self.assertEqual(auth_sessions.sessions_dir(),
                                 Path("/run/super-coder/auth-sessions"))
            with mock.patch.object(auth_sessions, "in_sandbox", return_value=False):
                self.assertEqual(
                    auth_sessions.sessions_dir(),
                    sandbox_devkit.auth_sessions_dir(auth_sessions.REPO_ROOT))

    def test_published_list_is_never_inside_the_private_state(self):
        with mock.patch.object(host_auth, "PUBLIC_DIR", self.private / "public"):
            with self.assertRaises(engine_ops.StateError):
                host_auth.publish(host_auth._empty_store())
        link = self.base / "link"
        link.symlink_to(self.private, target_is_directory=True)
        with mock.patch.object(host_auth, "PUBLIC_DIR", link):
            with self.assertRaises(engine_ops.StateError):
                host_auth.publish(host_auth._empty_store())

    def test_a_store_that_cannot_be_read_is_an_error_not_an_empty_install(self):
        self.enrol()
        path = self.private / "auth.json"
        saved = path.read_bytes()
        path.write_text("{ truncated")
        code, _out, err = self.cli("user", "list")
        self.assertEqual(code, 1)
        self.assertIn("cannot be read", err)
        self.assertEqual(self.get("/auth/status")[0], 500)
        self.assertEqual(path.read_text(), "{ truncated")
        path.write_bytes(saved)
        self.assertEqual(self.cli("user", "list")[0], 0)


# ── the launcher's mount of the published list ───────────────────────────────

class SessionsMountGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "fork"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}
        self.state = sandbox_devkit.engine_ops_state_dir(self.root, self.env)
        self.sessions = sandbox_devkit.auth_sessions_dir(self.root, self.env)
        self.target = sandbox_devkit.AUTH_SESSIONS_TARGET

    def guard(self, *arguments: str) -> None:
        with mock.patch.object(os, "getcwd", return_value=str(self.base)):
            sandbox_devkit.guard_engine_ops_state(
                ["docker", "run", "-d", *arguments, "image", "./sc", "serve"],
                self.root, self.env)

    def refused(self, *arguments: str) -> str:
        with self.assertRaises(sandbox_devkit.SandboxImageError, msg=arguments) as raised:
            self.guard(*arguments)
        self.assertIn("refusing to launch", str(raised.exception))
        return str(raised.exception)

    def test_the_two_directories_are_siblings_named_alike(self):
        collection = self.home / ".local" / "state" / "subfloor"
        self.assertEqual(self.state.parent, collection / "engine-ops")
        self.assertEqual(self.sessions.parent, collection / "auth-sessions")
        self.assertEqual(self.sessions.name, self.state.name)
        self.assertEqual(self.sessions.name, sandbox_devkit.install_key(self.root))
        self.assertFalse(sandbox_devkit.paths_overlap(self.state, self.sessions))
        custom = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.base / "x")}
        self.assertEqual(sandbox_devkit.auth_sessions_dir(self.root, custom).parent,
                         self.base / "x" / "subfloor" / "auth-sessions")
        # Pure paths: nothing was created by asking.
        self.assertFalse(self.sessions.exists())

    def test_the_launchers_own_mount_is_the_only_way_in(self):
        with mock.patch.dict(os.environ, self.env):
            os.environ.pop("XDG_STATE_HOME", None)
            mount = sandbox_devkit.auth_sessions_mount(self.root)
        self.assertEqual(mount, [
            "--mount", f"type=bind,src={self.sessions},dst={self.target},readonly"])
        self.assertEqual(stat.S_IMODE(self.sessions.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.sessions.parent.stat().st_mode), 0o700)
        self.assertEqual(
            mount[1], sandbox_devkit.auth_sessions_mount_value(self.root, self.env))
        self.guard(*mount)
        self.assertEqual(
            sandbox_devkit.bind_mounts(["-d", *mount, "image"]),
            [(str(self.sessions), True, self.target)])

        (self.sessions / "sessions.json").write_text("{}")
        link = self.home / "harness"
        link.symlink_to(self.sessions, target_is_directory=True)
        cases = {
            "writable": ["-v", f"{self.sessions}:{self.target}"],
            "writable, said so": ["-v", f"{self.sessions}:{self.target}:rw"],
            "writable bind": ["--mount", f"type=bind,src={self.sessions},dst={self.target}"],
            "read-only denied by value":
                ["--mount", f"type=bind,src={self.sessions},dst={self.target},readonly=false"],
            "somewhere else in the sandbox":
                ["-v", f"{self.sessions}:/tmp/sessions:ro"],
            "the collection of every install's list":
                ["-v", f"{self.sessions.parent}:{self.target}:ro"],
            "the state home": ["-v", f"{self.home}/.local/state:/s"],
            "one of its files": ["-v", f"{self.sessions}/sessions.json:/s.json:ro"],
            "writable through a link": ["-v", f"{link}:/h"],
            "the whole home": ["--mount", f"type=bind,src={self.home},dst=/home"],
        }
        for label, arguments in cases.items():
            message = self.refused(*arguments)
            self.assertIn("overlaps", message, label)
        # The launcher's mount beside a second, writable one: still refused.
        self.refused(*mount, "-v", f"{self.sessions}:/rw")

    def test_only_the_launchers_exact_spelling_reaches_the_list(self):
        """The directory is allowed as one argument, character for character.
        Another spelling that Docker would read as the same read-only mount is
        refused too: nothing rests on reading a mount the way Docker does."""
        with mock.patch.dict(os.environ, self.env):
            os.environ.pop("XDG_STATE_HOME", None)
            mount = sandbox_devkit.auth_sessions_mount(self.root)
        link = self.home / "harness"
        link.symlink_to(self.sessions, target_is_directory=True)
        s, t = self.sessions, self.target
        equivalent = {
            "the short form": ["-v", f"{s}:{t}:ro"],
            "the other read-only word": ["--mount", f"type=bind,src={s},dst={t},ro"],
            "the long field names":
                ["--mount", f"type=bind,source={s},target={t},readonly"],
            "another field order": ["--mount", f"type=bind,dst={t},src={s},readonly"],
            "read-only by value": ["--mount", f"type=bind,src={s},dst={t},readonly=true"],
            "through a link": ["--mount", f"type=bind,src={link},dst={t},readonly"],
            "a trailing slash": ["--mount", f"type=bind,src={s}/,dst={t},readonly"],
            "padding": ["--mount", f" type=bind,src={s},dst={t},readonly"],
        }
        for label, arguments in equivalent.items():
            self.assertIn("overlaps", self.refused(*arguments), label)
        # Twice is not once.
        self.assertIn("overlaps", self.refused(*mount, *mount))
        self.guard(*mount)

    def test_a_mount_value_says_each_thing_once(self):
        """Docker reads `--mount` fields in order, a later one overriding an
        earlier one, and takes several names for one field. A value that uses
        either is refused, so the guard and Docker cannot read it differently.
        The first case is the one from the review of PR #1775: read with
        "the first target, and read-only if any field says so", it looked like
        the launcher's mount, while Docker mounts the list WRITABLE at
        /tmp/writable-sessions."""
        with mock.patch.dict(os.environ, self.env):
            os.environ.pop("XDG_STATE_HOME", None)
            mount = sandbox_devkit.auth_sessions_mount(self.root)
        s, t = self.sessions, self.target
        other = self.base / "elsewhere"
        ambiguous = {
            "two targets and read-only taken back": (
                f"type=bind,src={s},target={t},dst=/tmp/writable-sessions,"
                "readonly,readonly=false"),
            "two targets": f"type=bind,src={s},target={t},dst=/tmp/writable-sessions,readonly",
            "the same target key twice": f"type=bind,src={s},dst={t},dst=/tmp/w,readonly",
            "three names for the target":
                f"type=bind,src={s},dst={t},destination=/tmp/w,readonly",
            "read-only taken back": f"type=bind,src={s},dst={t},readonly,readonly=false",
            "read-only taken back, other word": f"type=bind,src={s},dst={t},readonly,ro=false",
            "read-only said twice": f"type=bind,src={s},dst={t},readonly,readonly",
            "two sources, the list last": f"type=bind,src={other},src={s},dst={t},readonly",
            "two sources, the list first": f"type=bind,src={s},source={other},dst={t},readonly",
            "type said twice": f"type=volume,src={s},dst={t},readonly,type=bind",
            "type said twice, bind first": f"type=bind,src={s},dst={t},type=volume",
            "a field a launcher bind never has":
                f"type=bind,src={s},dst={t},readonly,bind-propagation=rshared",
            "an unknown field": f"type=bind,src={s},dst={t},readonly,volume-opt=o=bind",
        }
        for label, value in ambiguous.items():
            for arguments in (["--mount", value], [*mount, "--mount", value]):
                message = self.refused(*arguments)
                self.assertIn("cannot be checked", message, label)
            with self.assertRaises(sandbox_devkit.SandboxImageError, msg=label):
                sandbox_devkit.bind_mounts(["-d", "--mount", value, "image"])
        # The same rule wherever the mount points: the guard never has to
        # know what such a value would mean.
        for value in (f"type=bind,src={other},dst=/a,dst=/b",
                      f"type=bind,src={other},dst=/a,readonly,ro",
                      f"type=bind,src={other},dst=/a,consistency=cached"):
            self.assertIn("cannot be checked", self.refused("--mount", value), value)
        # `-v`: SRC:DST and at most one of ro or rw, nothing more.
        for value in (f"{s}:{t}:ro,rw", f"{s}:{t}:rw,ro", f"{s}:{t}:ro,ro",
                      f"{s}:{t}:ro:rw", f"{s}:{t}:ro,z", f"{s}:{t}:",
                      f"{other}:/a:ro,rw", f"{other}:/a:rw:ro", f"{other}:/a:Z",
                      f"{other}:relative:ro"):
            self.assertIn("cannot be checked", self.refused("-v", value), value)
        # What the launcher writes is untouched.
        for arguments in (["-v", f"{other}:{other}"], ["-v", f"{other}:/py:ro"],
                          ["-v", f"{other}:/data:rw"],
                          ["--mount", f"type=bind,src={other},dst=/run/agent,readonly"],
                          ["--mount", "type=volume,src=sc-devkit-abc,dst=/opt/devkit"],
                          ["--mount", 'type=volume,src=sc-devkit-def,dst="/opt/with,comma"']):
            self.guard(*mount, *arguments)

    def test_a_list_directory_that_resolves_into_the_private_state_is_refused(self):
        self.state.mkdir(parents=True)
        self.sessions.parent.mkdir(parents=True)
        self.sessions.symlink_to(self.state, target_is_directory=True)
        message = self.refused()
        self.assertIn("session-hash directory", message)
        with mock.patch.dict(os.environ, self.env):
            os.environ.pop("XDG_STATE_HOME", None)
            with self.assertRaises(sandbox_devkit.SandboxImageError):
                sandbox_devkit.auth_sessions_mount(self.root)

    def test_the_service_counts_the_mounted_list_among_what_a_sandbox_sees(self):
        with mock.patch.object(engine_ops, "REPO_ROOT", self.root), \
                mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.dict(os.environ, self.env):
            os.environ.pop("XDG_STATE_HOME", None)
            engine_ops._host_private_checked.clear()
            self.assertIn(Path(os.path.realpath(self.sessions)),
                          engine_ops.sandbox_visible_roots())
            with self.assertRaises(engine_ops.StateError):
                engine_ops.private_dir(self.sessions / "private")
            engine_ops._host_private_checked.clear()


# ── `./sc auth …` ────────────────────────────────────────────────────────────

class CliTest(AuthFixture):
    pinned = False

    def test_user_add_confirms_the_origin_in_the_terminal(self):
        self.assertIsNone(engine_ops.pinned_gui_port())
        # No terminal and no port: nothing is pinned and no user is created.
        code, _out, err = self.cli("user", "add", "Operator")
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", err)
        self.assertIn("nothing was created", err)
        # At a terminal the suggestion is displayed; only a typed port counts.
        with mock.patch.object(engine_ops, "suggested_gui_port", return_value=8812):
            for answer in ("", "yes", "y"):
                code, out, err = self.cli("user", "add", "Operator", tty=True,
                                          answer=answer)
                self.assertEqual(code, 1, answer)
                self.assertIn("http://127.0.0.1:8812", out)
                self.assertIn("writable from the sandbox", out)
                self.assertIn("aborted", out)
            self.assertIsNone(engine_ops.pinned_gui_port())
            self.assertFalse((self.private / "auth.json").exists())

            code, out, err = self.cli("user", "add", "Operator", tty=True,
                                      answer=str(GUI_PORT))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(engine_ops.pinned_gui_port(), GUI_PORT)
        self.assertIn(f"the host accepts the GUI at {GUI}", out)
        self.assertRegex(out, r"claim code:\s+[A-Z2-9]{4}-[A-Z2-9]{4}")
        self.assertEqual(self.store()["users"][0]["state"], "awaiting")

        # A second user keeps the pin on Enter, and with no terminal.
        with mock.patch.object(engine_ops, "suggested_gui_port", return_value=8812):
            code, out, err = self.cli("user", "add", "Second", tty=True, answer="")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(engine_ops.pinned_gui_port(), GUI_PORT)
        self.assertEqual([u["user_id"] for u in self.store()["users"]], [1, 2])

    def test_user_add_takes_engine_users_only(self):
        for name in ("nobody", "Retired"):
            code, out, err = self.cli("user", "add", name, "--port", str(GUI_PORT))
            self.assertEqual(code, 1, name)
            self.assertIn("is not an active engine user", err)
            self.assertIn("Operator, Second", err)
        # Refused before the origin is touched.
        self.assertIsNone(engine_ops.pinned_gui_port())
        self.assertFalse((self.private / "auth.json").exists())
        for argv in (("user", "add"), ("user", "add", "a", "b"),
                     ("user", "add", "Operator", "--force"), ("user",),
                     ("user", "remove", "Operator"), ("nonsense",), ()):
            self.assertEqual(self.cli(*argv)[0], 2, argv)
        for bad in ("abc", "0", "70000"):
            self.assertEqual(
                self.cli("user", "add", "Operator", "--port", bad)[0], 1, bad)
        self.assertFalse((self.private / "auth.json").exists())
        # An engine DB that is not there is said plainly.
        with mock.patch.object(host_auth, "ENGINE_DB", self.base / "absent.db"):
            code, _out, err = self.cli("user", "add", "Operator", "--port", "8800")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertEqual(self.cli("--help")[0], 0)

    def test_the_engine_users_are_read_without_writing_the_database(self):
        before = self.engine_db.read_bytes()
        self.assertEqual(host_auth.engine_users(), [
            {"user_id": 1, "username": "Operator"},
            {"user_id": 2, "username": "Second"}])
        self.assertEqual(self.engine_db.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.engine_db.parent.glob("engine.db*")),
            ["engine.db"])
        # A WAL database, as the live engine's is.
        con = sqlite3.connect(self.engine_db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("UPDATE users SET email='x' WHERE user_id=2")
        con.commit()
        self.assertEqual(len(host_auth.engine_users()), 2)
        con.close()
        self.assertEqual(len(host_auth.engine_users()), 2)

    def test_list_sessions_revoke_and_origin(self):
        code, out, _err = self.cli("user", "list")
        self.assertEqual(code, 0)
        self.assertIn("./sc auth user add <username>", out)
        self.assertIn("no device is signed in", self.cli("sessions")[1])

        cookie, secret = self.enrol(device="desk")
        self.tick()
        self.sign_in(secret, device="phone")
        code, out, _err = self.cli("user", "list")
        self.assertEqual(out.strip().split("\t"),
                         ["1", "Operator", "active", "2 session(s)"])
        code, out, _err = self.cli("sessions", "list")
        rows = [line.split("\t") for line in out.strip().splitlines()]
        self.assertEqual([(row[1], row[2]) for row in rows],
                         [("Operator", "phone"), ("Operator", "desk")])
        self.assertNotIn(cookie.split("=")[1], out)

        code, _out, err = self.cli("sessions", "revoke", "nope")
        self.assertEqual(code, 1)
        code, out, _err = self.cli("sessions", "revoke", rows[1][0])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get("/status", cookie=cookie)[0], 401)
        self.assertEqual(len(self.published()["sessions"]), 1)
        code, out, _err = self.cli("sessions", "revoke", "--all")
        self.assertIn("1 session(s) revoked", out)
        self.assertEqual(self.published()["sessions"], [])
        self.assertEqual(self.store()["users"][0]["state"], "active")

        # `auth origin` is the pin, alone.
        code, out, _err = self.cli("origin", "--show")
        self.assertEqual(code, 0)
        self.assertIn(f"accepts the GUI at {GUI}", out)
        self.assertEqual(self.cli("origin", "8830")[0], 0)
        self.assertEqual(engine_ops.pinned_gui_port(), 8830)
        self.assertEqual(self.cli("origin", "abc")[0], 2)
        self.assertEqual(self.cli("origin")[0], 1)            # no terminal
        self.assertEqual(engine_ops.pinned_gui_port(), 8830)

    def test_every_verb_refuses_inside_the_sandbox(self):
        with mock.patch.object(engine_ops, "in_sandbox", return_value=True):
            for argv in (("user", "add", "Operator", "--port", "8800"),
                         ("user", "reset", "Operator"), ("user", "list"),
                         ("sessions",), ("sessions", "revoke", "--all"),
                         ("unlock",), ("origin", "8800"), ("origin", "--show")):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 1, argv)
                self.assertEqual(out, "")
                self.assertIn("this is the sandbox", err)
        self.assertEqual(list(self.private.iterdir()), [])
        self.assertFalse(self.public.exists())
        self.assertIsNone(engine_ops.pinned_gui_port())

    def test_the_real_cli_refuses_with_the_sandbox_marker_set(self):
        """The script itself, as the dispatcher runs it inside a container."""
        state = self.base / "state-home"
        env = {**os.environ, "SC_SANDBOX": "1", "HOME": str(self.base / "home"),
               "XDG_STATE_HOME": str(state)}
        for argv in (["user", "add", "Operator", "--port", "8800"], ["user", "list"],
                     ["unlock"]):
            done = subprocess.run(
                [sys.executable, str(ENGINE / "api" / "host_auth.py"), *argv],
                env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertEqual(done.stdout, "")
            self.assertIn("this is the sandbox", done.stderr)
        self.assertFalse(state.exists())


class ConcurrencyTest(AuthFixture):
    def test_the_cli_and_the_service_share_one_lock(self):
        """Two writers at once (as the service and `./sc auth` are): every
        write lands, and the store is never torn."""
        _cookie, secret = self.enrol()
        errors: list[Exception] = []

        def churn():
            try:
                for _ in range(15):
                    base.seed_session("Operator")
                    host_auth.unlock()
            except Exception as exc:  # noqa: BLE001 — reported by the assertion
                errors.append(exc)

        threads = [threading.Thread(target=churn) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        store = self.store()
        self.assertEqual(len(store["sessions"]),
                         min(1 + 4 * 15, host_auth.SESSIONS_MAX))
        self.assertEqual(len(self.published()["sessions"]), len(store["sessions"]))
        lock = self.private / "auth.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertEqual(self.sign_in(secret)[0], 200)

        # Across processes it is the lock file: while this process writes,
        # another cannot take it.
        probe = [sys.executable, "-c",
                 "import fcntl, os, sys\n"
                 "fd = os.open(sys.argv[1], os.O_RDWR)\n"
                 "try:\n"
                 "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                 "except OSError:\n"
                 "    sys.exit(3)\n", str(lock)]
        with host_auth._locked():
            self.assertEqual(subprocess.run(probe, check=False).returncode, 3)
        self.assertEqual(subprocess.run(probe, check=False).returncode, 0)


# ── the dispatcher ───────────────────────────────────────────────────────────

class DispatcherTest(unittest.TestCase):
    """`./sc auth …` through the real bootstrap and dispatcher, over a fake
    engine whose `api/host_auth.py` only records what it was asked."""

    def setUp(self) -> None:
        self.fx = HostRuntimeFixture()
        self.addCleanup(self.fx.close)
        (self.fx.api / "host_auth.py").write_text(
            "import os, sys\n"
            "with open(os.environ['SC_TEST_LOG'], 'a') as log:\n"
            "    log.write('host_auth ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            "print('host_auth ran')\n")
        git = ["git", "-C", str(self.fx.root), "-c", "user.name=t",
               "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        subprocess.run([*git, "add", "sc"], check=True)
        subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
        self.linked = self.fx.root.parent / "linked-worktree"
        subprocess.run([*git, "worktree", "add", "-q", str(self.linked)], check=True)

    def run_sc(self, where: Path, *args: str, **env: str):
        return subprocess.run(
            [str(where / "sc"), *args], cwd=where, env={**self.fx.env, **env},
            text=True, capture_output=True, timeout=60, check=False)

    def auth_calls(self) -> list[str]:
        return [line for line in self.fx.calls() if line.startswith("host_auth")]

    def test_auth_runs_on_the_host_in_the_main_checkout(self):
        done = self.run_sc(self.fx.root, "auth", "user", "add", "Operator",
                           "--port", "8812")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("host_auth ran", done.stdout)
        self.assertEqual(self.auth_calls(),
                         ["host_auth user add Operator --port 8812"])
        self.run_sc(self.fx.root, "auth", "sessions", "revoke", "--all")
        # `engine-ops-pin` is `auth origin` under its first name.
        self.run_sc(self.fx.root, "engine-ops-pin", "8812")
        self.assertEqual(self.auth_calls()[1:], [
            "host_auth sessions revoke --all", "host_auth origin 8812"])

    def test_auth_is_refused_from_a_linked_worktree(self):
        for args in (("auth", "user", "add", "Operator"),
                     ("auth", "user", "reset", "Operator"),
                     ("auth", "user", "list"), ("auth", "sessions"),
                     ("auth", "unlock"), ("auth", "origin", "8812"),
                     ("engine-ops-pin", "8812")):
            done = self.run_sc(self.linked, *args)
            self.assertEqual(done.returncode, 1, (args, done.stdout, done.stderr))
            self.assertEqual(done.stdout, "")
            first = done.stderr.splitlines()[0]
            self.assertIn(f"./sc {args[0]} refused", first)
            self.assertIn("a linked worktree has no instance of its own", first)
            self.assertIn(f"live instance   : {self.fx.root}", done.stderr)
            self.assertIn("Nothing was opened, written or deleted.", done.stderr)
        self.assertEqual(self.auth_calls(), [])

    def test_auth_is_refused_inside_the_sandbox(self):
        for args in (("auth", "user", "add", "Operator"), ("auth", "user", "list"),
                     ("auth", "unlock"), ("engine-ops-pin", "8812")):
            done = self.run_sc(self.fx.root, *args, SC_SANDBOX="1")
            self.assertEqual(done.returncode, 1, (args, done.stdout, done.stderr))
            self.assertEqual(done.stdout, "")
            self.assertIn(f"./sc {args[0]} refused: this is the sandbox", done.stderr)
            self.assertIn("Nothing was read or changed.", done.stderr)
        self.assertEqual(self.auth_calls(), [])

    def test_the_real_dispatcher_help_names_the_verbs(self):
        text = (ENGINE / "scripts" / "dispatch.sh").read_text()
        for verb in ("./sc auth user add <username>", "./sc auth user reset <username>",
                     "./sc auth user list", "./sc auth sessions", "./sc auth unlock",
                     "./sc auth origin"):
            self.assertIn(verb, text)
        self.assertEqual(host_auth.SETUP_CLI, engine_ops.SETUP_CLI)


if __name__ == "__main__":
    unittest.main()
