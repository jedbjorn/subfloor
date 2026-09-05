#!/usr/bin/env python3
"""Web search (Tavily) — key store, client, API routes, CLI (spec doc #215).

The contract under test:
  • the key lives in ONE 0600 file and never appears in status, API responses,
    error text, or instance.json;
  • rotation replaces the file atomically and the next search reads the new key;
  • browser-operator routes refuse shell credentials and cross-origin mutations;
  • /_sc/search is shell-token scoped and forwards secret-free failures at the
    status web_search chose;
  • `./sc search` renders the payload and is registered in the dispatcher.

Run:
    python3 tests/test_web_search.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))
import server
import web_search

KEY = "tvly-secret-key-1234abcd"
NEW_KEY = "tvly-rotated-key-9999wxyz"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def opener_returning(payload: dict):
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps(payload).encode())

    opener.calls = calls
    return opener


def opener_failing(code: int, body: bytes = b""):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body))
    return opener


TAVILY_OK = {
    "query": "q",
    "answer": "An answer.",
    "response_time": 1.2,
    "results": [
        {"title": "One", "url": "https://one.example", "content": "first hit",
         "score": 0.9, "published_date": "2026-01-01"},
        {"title": "Two", "url": "https://two.example", "content": "second hit",
         "score": 0.5},
    ],
}


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "web_search.json"

    def test_unconfigured_status(self):
        self.assertEqual(web_search.status(self.path), {
            "configured": False, "provider": "tavily",
            "key_hint": None, "updated_at": None,
        })

    def test_write_is_0600_and_status_hides_the_key(self):
        st = web_search.write(KEY, self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(st["configured"])
        self.assertEqual(st["key_hint"], "…abcd")
        self.assertIsNotNone(st["updated_at"])
        self.assertNotIn(KEY, json.dumps(st))
        self.assertEqual(json.loads(self.path.read_text())["api_key"], KEY)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["web_search.json"],
                         "no temp inode left behind")

    def test_rotate_replaces_the_key(self):
        web_search.write(KEY, self.path)
        st = web_search.write(NEW_KEY, self.path)
        raw = self.path.read_text()
        self.assertIn(NEW_KEY, raw)
        self.assertNotIn(KEY, raw)
        self.assertEqual(st["key_hint"], "…wxyz")

    def test_clear_removes_the_file(self):
        web_search.write(KEY, self.path)
        st = web_search.clear(self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(st["configured"])
        # idempotent
        self.assertFalse(web_search.clear(self.path)["configured"])

    def test_write_rejects_blank_and_whitespace(self):
        for bad in ("", "   ", "tvly abc", "tvly\nabc"):
            with self.assertRaises(ValueError):
                web_search.write(bad, self.path)
        self.assertFalse(self.path.exists())

    def test_symlink_store_is_refused(self):
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaises(web_search.WebSearchError):
            web_search.status(self.path)
        with self.assertRaises(web_search.WebSearchError):
            web_search.write(KEY, self.path)
        with self.assertRaises(web_search.WebSearchError):
            web_search.clear(self.path)
        self.assertTrue(self.path.is_symlink(), "clear must not follow a symlink")
        self.assertTrue(target.exists())

    def test_store_path_lives_under_private_state_not_instance_json(self):
        path = web_search.store_path()
        self.assertEqual(path.name, "web_search.json")
        self.assertNotEqual(path.parent, ENGINE)
        self.assertNotIn("instance.json", str(path))


class ClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "web_search.json"
        web_search.write(KEY, self.path)

    def test_search_sends_bearer_and_normalizes(self):
        opener = opener_returning(TAVILY_OK)
        out = web_search.search("q", max_results=2, depth="advanced",
                                path=self.path, opener=opener)
        req = opener.calls[0]
        self.assertEqual(req.full_url, web_search.TAVILY_URL)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        self.assertEqual(json.loads(req.data), {
            "query": "q", "max_results": 2, "search_depth": "advanced",
            "include_answer": True,
        })
        self.assertEqual(out["answer"], "An answer.")
        self.assertEqual([r["url"] for r in out["results"]],
                         ["https://one.example", "https://two.example"])
        self.assertEqual(out["results"][0]["snippet"], "first hit")
        self.assertEqual(out["results"][0]["published"], "2026-01-01")
        self.assertIsNone(out["results"][1]["published"])
        self.assertNotIn(KEY, json.dumps(out))

    def test_candidate_key_wins_over_stored(self):
        opener = opener_returning(TAVILY_OK)
        web_search.search("q", api_key=NEW_KEY, path=self.path, opener=opener)
        self.assertEqual(opener.calls[0].get_header("Authorization"),
                         f"Bearer {NEW_KEY}")

    def test_unconfigured_is_409_naming_the_gui(self):
        web_search.clear(self.path)
        with self.assertRaises(web_search.WebSearchError) as cm:
            web_search.search("q", path=self.path, opener=opener_returning({}))
        self.assertEqual((cm.exception.status, cm.exception.code),
                         (409, "unconfigured"))
        self.assertIn("Scripts → Web Search", cm.exception.message)

    def test_rejected_key_is_502_and_redacted(self):
        body = json.dumps({"detail": {"error": f"bad key {KEY}"}}).encode()
        with self.assertRaises(web_search.WebSearchError) as cm:
            web_search.search("q", path=self.path, opener=opener_failing(401, body))
        self.assertEqual((cm.exception.status, cm.exception.code),
                         (502, "key_rejected"))
        self.assertIn("rotate", cm.exception.message)
        self.assertNotIn(KEY, cm.exception.message)

    def test_limits_map_to_429(self):
        for code, expect in ((429, "rate_limited"), (432, "usage_limit")):
            with self.assertRaises(web_search.WebSearchError) as cm:
                web_search.search("q", path=self.path,
                                  opener=opener_failing(code, b"limit"))
            self.assertEqual((cm.exception.status, cm.exception.code),
                             (429, expect))

    def test_network_failure_is_502_unreachable(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError(f"dns down for {KEY}")
        with self.assertRaises(web_search.WebSearchError) as cm:
            web_search.search("q", path=self.path, opener=opener)
        self.assertEqual((cm.exception.status, cm.exception.code),
                         (502, "unreachable"))
        self.assertNotIn(KEY, cm.exception.message)

    def test_argument_validation(self):
        opener = opener_returning(TAVILY_OK)
        for kwargs in ({"max_results": 0}, {"max_results": 21},
                       {"max_results": True}, {"depth": "deep"}):
            with self.assertRaises(ValueError):
                web_search.search("q", path=self.path, opener=opener, **kwargs)
        with self.assertRaises(ValueError):
            web_search.search("   ", path=self.path, opener=opener)
        self.assertEqual(opener.calls, [])

    def test_validate_reports_ok_and_failure_without_raising(self):
        ok = web_search.validate(path=self.path, opener=opener_returning(TAVILY_OK))
        self.assertTrue(ok["ok"])
        self.assertIn("https://one.example", ok["output"])
        bad = web_search.validate(api_key="tvly-nope-00000000", path=self.path,
                                  opener=opener_failing(401, b"{}"))
        self.assertFalse(bad["ok"])
        self.assertIn("rejected", bad["output"])
        web_search.clear(self.path)
        none = web_search.validate(path=self.path, opener=opener_returning({}))
        self.assertFalse(none["ok"])
        self.assertIn("not configured", none["output"])


def _build_file_db(path: Path) -> None:
    source = sqlite3.connect(":memory:")
    source.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        source.executescript(migration.read_text())
    source.execute("INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)")
    source.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname, api_key) "
        "VALUES ('Dev', 'x', 'dev', 'dev', 'shell-token')")
    source.commit()
    target = sqlite3.connect(path)
    source.backup(target)
    target.close()
    source.close()


class ApiRouteTest(unittest.TestCase):
    """Routes through server.dispatch_http with the Tavily call mocked."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "engine.db"
        _build_file_db(self.db_path)
        self.store = Path(self.tmp.name) / "web_search.json"
        patcher = mock.patch.object(web_search, "store_path", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def request(self, method, path, *, token=None, body=None, origin=None,
                opener=None):
        raw = json.dumps(body).encode() if body is not None else b""
        lines = ["Host: 127.0.0.1:8800", f"Content-Length: {len(raw)}"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        if origin:
            lines.append(f"Origin: {origin}")
            lines.append("Sec-Fetch-Site: same-origin")
        patches = [mock.patch.object(server, "db", side_effect=self.connect)]
        if opener is not None:
            patches.append(mock.patch.object(
                web_search.urllib.request, "urlopen", side_effect=opener))
        for p in patches:
            p.start()
        try:
            status, _headers, out = server.dispatch_http(
                method, path, "\r\n".join(lines), raw)
        finally:
            for p in patches:
                p.stop()
        return status, json.loads(out)

    SAME = "http://127.0.0.1:8800"

    def test_status_unconfigured_then_set_rotate_clear(self):
        status, body = self.request("GET", "/api/web-search")
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])

        status, body = self.request("PUT", "/api/web-search",
                                    body={"api_key": KEY}, origin=self.SAME)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["key_hint"], "…abcd")
        self.assertNotIn(KEY, json.dumps(body))
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)

        status, body = self.request("PUT", "/api/web-search",
                                    body={"api_key": NEW_KEY}, origin=self.SAME)
        self.assertEqual(status, 200)
        self.assertEqual(body["key_hint"], "…wxyz")
        self.assertNotIn(KEY, self.store.read_text())

        status, body = self.request("GET", "/api/web-search")
        self.assertTrue(body["configured"])
        self.assertNotIn(NEW_KEY, json.dumps(body))

        status, body = self.request("DELETE", "/api/web-search", origin=self.SAME)
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])
        self.assertFalse(self.store.exists())

    def test_put_validation(self):
        status, _body = self.request("PUT", "/api/web-search",
                                     body={"api_key": "  "}, origin=self.SAME)
        self.assertEqual(status, 400)
        status, _body = self.request("PUT", "/api/web-search",
                                     body={"api_key": 5}, origin=self.SAME)
        self.assertEqual(status, 400)
        self.assertFalse(self.store.exists())

    def test_shell_credentials_are_refused_on_config_routes(self):
        for method, body in (("GET", None), ("PUT", {"api_key": KEY}),
                             ("DELETE", None)):
            status, out = self.request(method, "/api/web-search", token="shell-token",
                                       body=body, origin=self.SAME)
            self.assertEqual(status, 403, (method, out))
            self.assertEqual(out["error"]["code"], "fnb_operator_required")
            self.assertIn("web search config", out["error"]["message"])
        status, out = self.request("POST", "/api/web-search/validate",
                                   token="shell-token", body={})
        self.assertEqual(status, 403)
        self.assertFalse(self.store.exists())

    def test_cross_origin_mutation_is_refused(self):
        status, out = self.request("PUT", "/api/web-search", body={"api_key": KEY},
                                   origin="https://attacker.example")
        self.assertEqual(status, 403)
        self.assertEqual(out["error"]["code"], "same_origin_required")
        self.assertFalse(self.store.exists())
        status, out = self.request("DELETE", "/api/web-search",
                                   origin="https://attacker.example")
        self.assertEqual(status, 403)

    def test_validate_probes_candidate_then_stored_key(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            return _Resp(json.dumps(TAVILY_OK).encode())

        status, out = self.request("POST", "/api/web-search/validate",
                                   body={"api_key": NEW_KEY}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(seen, [f"Bearer {NEW_KEY}"])
        self.assertFalse(self.store.exists(), "test never saves")

        web_search.write(KEY, self.store)
        status, out = self.request("POST", "/api/web-search/validate", body={},
                                   opener=opener)
        self.assertTrue(out["ok"])
        self.assertEqual(seen[-1], f"Bearer {KEY}")

        def bad(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b"{}"))
        status, out = self.request("POST", "/api/web-search/validate", body={},
                                   opener=bad)
        self.assertEqual(status, 200)
        self.assertFalse(out["ok"])
        self.assertNotIn(KEY, out["output"])

    def test_shell_search_requires_token(self):
        status, _out = self.request("POST", "/_sc/search", body={"query": "q"})
        self.assertEqual(status, 401)
        status, _out = self.request("POST", "/_sc/search", token="wrong",
                                    body={"query": "q"})
        self.assertEqual(status, 401)

    def test_shell_search_unconfigured_is_409(self):
        status, out = self.request("POST", "/_sc/search", token="shell-token",
                                   body={"query": "q"})
        self.assertEqual(status, 409)
        self.assertEqual(out["code"], "unconfigured")
        self.assertIn("Scripts → Web Search", out["error"])

    def test_shell_search_returns_results_and_forwards_failures(self):
        web_search.write(KEY, self.store)
        status, out = self.request(
            "POST", "/_sc/search", token="shell-token",
            body={"query": "q", "max_results": 2, "depth": "basic"},
            opener=opener_returning(TAVILY_OK))
        self.assertEqual(status, 200)
        self.assertEqual(len(out["results"]), 2)
        self.assertNotIn(KEY, json.dumps(out))

        status, out = self.request("POST", "/_sc/search", token="shell-token",
                                   body={"query": "q", "max_results": 99})
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "bad_request")

        status, out = self.request("POST", "/_sc/search", token="shell-token",
                                   body={"query": "q"},
                                   opener=opener_failing(401, b"{}"))
        self.assertEqual(status, 502)
        self.assertEqual(out["code"], "key_rejected")
        self.assertNotIn(KEY, out["error"])

    def test_config_status_needs_an_active_operator(self):
        with self.connect() as con:
            con.execute("UPDATE users SET is_active=0")
        status, _out = self.request("GET", "/api/web-search")
        self.assertEqual(status, 401)


class CliTest(unittest.TestCase):
    def test_render_lists_answer_and_results(self):
        text = web_search.render(web_search._normalize(TAVILY_OK, "q"))
        self.assertTrue(text.startswith("An answer."))
        self.assertIn("1. One\n   https://one.example\n   first hit", text)
        self.assertIn("2. Two", text)
        self.assertEqual(web_search.render({"results": []}), "(no results)")

    def test_main_posts_through_the_api_lane(self):
        import mem
        calls = []

        def fake_api(method, path, payload=None, **kw):
            calls.append((method, path, payload, kw))
            return web_search._normalize(TAVILY_OK, "two words")

        buf = io.StringIO()
        with (
            mock.patch.object(mem, "_require_api", return_value=None),
            mock.patch.object(mem, "_api", side_effect=fake_api),
            redirect_stdout(buf),
        ):
            rc = web_search.main(["two", "words", "--max", "7", "--depth", "advanced"])
        self.assertEqual(rc, 0)
        method, path, payload, kw = calls[0]
        self.assertEqual((method, path), ("POST", "/_sc/search"))
        self.assertEqual(payload, {"query": "two words", "max_results": 7,
                                   "depth": "advanced"})
        self.assertTrue(kw.get("idempotent"))
        self.assertIn("1. One", buf.getvalue())

        buf = io.StringIO()
        with (
            mock.patch.object(mem, "_require_api", return_value=None),
            mock.patch.object(mem, "_api", side_effect=fake_api),
            redirect_stdout(buf),
        ):
            web_search.main(["--json", "q"])
        self.assertEqual(json.loads(buf.getvalue())["answer"], "An answer.")

    def test_dispatcher_registers_the_verb(self):
        text = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertIn('search)       exec "$PY" "$S/web_search.py" "$@" ;;', text)
        gate = next(line for line in text.splitlines()
                    if line.strip().startswith("install|ensure-harness|doctor|"))
        self.assertIn("|search|", gate)

    def test_skill_asset_is_common_and_names_the_verb(self):
        text = (ENGINE / "assets" / "skills" / "web_search" / "SKILL.md").read_text()
        self.assertIn("command: sc search", text)
        self.assertIn("common: true", text)
        self.assertIn("Scripts → Web Search", text)
        seed = (ENGINE / "migrations" / "0001_seed_skills.sql").read_text()
        self.assertIn("'web_search',", seed)


if __name__ == "__main__":
    unittest.main()
