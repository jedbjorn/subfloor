#!/usr/bin/env python3
"""Tests for the map extractor plug-in mechanism + the reference extractors.

Stdlib `unittest`, no pytest (engine style). Each test builds a throwaway map db
from map_schema.sql, lays a tiny source tree in a temp repo root, registers the
files in dr_filepath the way map_repo does, then runs an extractor and asserts
the dr_* rows. The reference extractors (FastAPI, SvelteKit) aren't live in this
repo's own stack, so this is where they get exercised.

Run:
    python3 tests/test_map_extractors.py
"""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MAP_SCHEMA = ENGINE / "map_schema.sql"
TEMPLATES = ENGINE / "templates" / "map_extractors"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_t_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def map_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(MAP_SCHEMA.read_text())
    return con


class _Fixture:
    """A temp repo root with files registered in a fresh map db's dr_filepath."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.con = map_db()

    def add(self, rel: str, body: str, *, lang=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        self.con.execute(
            "INSERT INTO dr_filepath (path, lang, role) VALUES (?,?,?)",
            (rel, lang, "code"))
        self.con.commit()

    def close(self):
        self.con.close()
        self.tmp.cleanup()


class FastapiExtractorTest(unittest.TestCase):
    def test_decorator_and_route_styles(self):
        fx = _Fixture()
        self.addCleanup(fx.close)
        mod = load_module(TEMPLATES / "fastapi_endpoints.py")
        fx.add("api/routes.py", lang="Python", body=(
            'from fastapi import APIRouter\n'
            'router = APIRouter()\n'
            '@router.get("/shells")\n'
            'def list_shells(): ...\n'
            '@app.post("/shells/{id}")\n'
            'def create(): ...\n'
            '@app.route("/legacy", methods=["GET", "POST"])\n'
            'def legacy(): ...\n'
            'x = obj.get("not-a-route")  # not a decorator → ignored\n'
        ))
        mod.extract(fx.con, fx.root, {})
        rows = {(r["method"], r["path"]) for r in
                fx.con.execute("SELECT method, path FROM dr_endpoint")}
        self.assertIn(("GET", "/shells"), rows)
        self.assertIn(("POST", "/shells/{id}"), rows)
        self.assertIn(("GET,POST", "/legacy"), rows)
        self.assertNotIn(("GET", "not-a-route"), rows)
        # handler carries file:line; framework labeled
        r = fx.con.execute(
            "SELECT handler, framework FROM dr_endpoint WHERE path='/shells'").fetchone()
        self.assertTrue(r["handler"].startswith("api/routes.py:"))
        self.assertEqual(r["framework"], "fastapi")

    def test_rerun_is_idempotent(self):
        fx = _Fixture()
        self.addCleanup(fx.close)
        mod = load_module(TEMPLATES / "fastapi_endpoints.py")
        fx.add("a.py", lang="Python", body='@app.get("/x")\ndef f(): ...\n')
        mod.extract(fx.con, fx.root, {})
        mod.extract(fx.con, fx.root, {})  # second run must not duplicate
        n = fx.con.execute("SELECT COUNT(*) FROM dr_endpoint").fetchone()[0]
        self.assertEqual(n, 1)


class SqliteSchemaExtractorTest(unittest.TestCase):
    def test_tables_columns_and_comments(self):
        fx = _Fixture()
        self.addCleanup(fx.close)
        mod = load_module(TEMPLATES / "sqlite_schema.py")
        fx.add("db/schema.sql", lang="SQL", body=(
            "CREATE TABLE IF NOT EXISTS users (\n"
            "  id INTEGER PRIMARY KEY,  -- the pk, not a column comment trap\n"
            "  email TEXT NOT NULL,\n"
            "  name TEXT,\n"
            "  FOREIGN KEY (org_id) REFERENCES orgs(id)\n"  # constraint → skipped
            ");\n"
            "CREATE VIEW active_users AS SELECT * FROM users;\n"
        ))
        mod.extract(fx.con, fx.root, {})
        tables = {(r["name"], r["kind"]) for r in
                  fx.con.execute("SELECT name, kind FROM dr_db_table")}
        self.assertIn(("users", "table"), tables)
        self.assertIn(("active_users", "view"), tables)
        cols = {r["name"]: r for r in fx.con.execute(
            "SELECT name, type, pk, not_null FROM dr_db_column WHERE table_name='users'")}
        self.assertEqual(set(cols), {"id", "email", "name"})  # FK constraint excluded
        self.assertEqual(cols["id"]["pk"], 1)
        self.assertEqual(cols["email"]["not_null"], 1)
        self.assertEqual(cols["email"]["type"], "TEXT")
        # the inline comment must not have become a column
        self.assertNotIn("--", cols)


class SveltekitExtractorTest(unittest.TestCase):
    def test_routes_and_components(self):
        fx = _Fixture()
        self.addCleanup(fx.close)
        mod = load_module(TEMPLATES / "sveltekit_routes.py")
        fx.add("src/routes/+page.svelte", "x")
        fx.add("src/routes/shells/[id]/+page.svelte", "x")
        fx.add("src/routes/api/flags/+server.ts", "x")
        fx.add("src/routes/(admin)/users/+page.svelte", "x")
        fx.add("src/lib/Card.svelte", "x")
        mod.extract(fx.con, fx.root, {})
        routes = {(r["path"], r["kind"]) for r in
                  fx.con.execute("SELECT path, kind FROM dr_route")}
        self.assertIn(("/", "page"), routes)
        self.assertIn(("/shells/:id", "page"), routes)
        self.assertIn(("/api/flags", "endpoint"), routes)
        self.assertIn(("/users", "page"), routes)  # (admin) group dropped from URL
        comps = {r["name"] for r in fx.con.execute("SELECT name FROM dr_component")}
        self.assertEqual(comps, {"Card"})  # +page.svelte files are routes, not components


class ExtractorDiscoveryTest(unittest.TestCase):
    """map_repo.run_extractors discovers + runs .sc-state/map_extractors/*.py."""

    def test_discovery_runs_and_guards(self):
        import sys
        sys.path.insert(0, str(ENGINE / "scripts"))
        import map_repo

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ext = root / ".sc-state" / "map_extractors"
            ext.mkdir(parents=True)
            (ext / "good.py").write_text(
                "def extract(con, repo_root, cfg):\n"
                "    con.execute(\"INSERT INTO dr_route (path) VALUES ('/hit')\")\n"
                "    return '1 route'\n")
            (ext / "broken.py").write_text(
                "def extract(con, repo_root, cfg):\n    raise RuntimeError('boom')\n")
            (ext / "_helper.py").write_text("# underscore = ignored\n")
            con = map_db()
            summaries = map_repo.run_extractors(con, root, {})
            # good ran and wrote; broken was guarded (logged, not raised); _helper skipped
            self.assertTrue(any("good: 1 route" in s for s in summaries))
            self.assertTrue(any("broken: FAILED" in s for s in summaries))
            self.assertFalse(any("_helper" in s for s in summaries))
            self.assertEqual(
                con.execute("SELECT path FROM dr_route").fetchone()["path"], "/hit")
            con.close()


if __name__ == "__main__":
    unittest.main()
