"""Local cross-project collection, dashboard and reference parity on disposable installs."""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path[:0] = [str(ENGINE / "manager"), str(ENGINE / "scripts")]
from token_analytics import INTERVAL, RETRY, STATIC_DIR, Analytics

del sys.path[:2]

SCHEMA = """
CREATE TABLE shells(shell_id, shortname, flavor);
CREATE TABLE shell_memory_archives(archive_id, session_id, shell_id);
CREATE TABLE session_token_usage(usage_id, harness, harness_session_ref, provider, model, title,
    started_at, ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    reasoning_tokens, status, captured_at, archive_id, shell_id);
CREATE TABLE roadmap(feature_id, title, updated_at, roadmap_status);
CREATE TABLE documents(document_id, feature_id, title, kind, frozen_date, frozen);
CREATE TABLE conversation_runs(shell_id, started_at, ended_at, heartbeat_at);
CREATE TABLE runs(owner_shell_id, kind, started_at, finished_at, state);
"""


def git(repo, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c",
                                   "commit.gpgsign=false", "-C", str(repo), *args],
                                  text=True, env=env).strip()


def pin(repo, ref, when):
    path = repo / ".sc-state" / "engine.ref"
    path.parent.mkdir(exist_ok=True)
    path.write_text(ref + "\n")
    git(repo, "add", ".sc-state/engine.ref")
    git(repo, "commit", "-qm", "Pin engine", date=when)
    epoch = datetime.fromisoformat(when).timestamp()
    os.utime(path, (epoch, epoch))


@pytest.fixture
def history(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    times = [(datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
             for d in (3, 2, 1)]
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q")
    refs = []
    for subject in ("Engine one", "Engine two"):
        git(upstream, "commit", "--allow-empty", "-qm", subject)
        refs.append(git(upstream, "rev-parse", "HEAD"))
    repos, databases = [], []
    for index, name in enumerate(("alpha", "beta"), 1):
        repo = tmp_path / "Repos" / name
        engine = repo / ".super-coder"
        engine.mkdir(parents=True)
        git(repo, "init", "-q")
        pin(repo, refs[0], times[0])
        config = engine / "instance.json"
        if name == "alpha":
            config.write_text("{}")
            database = engine / "shell_db.db"
            pin(repo, refs[1], times[1])
        else:
            iid = "b" * 32
            config.write_text(json.dumps({"instance_id": iid}))
            private = state / "subfloor" / "instances" / iid
            private.mkdir(parents=True)
            for path in (private, private.parent, private.parent.parent):
                path.chmod(0o700)
            (private / "owner.json").write_text(json.dumps({"instance_id": iid, "owner_uid": os.geteuid()}))
            database = private / "shell_db.db"
        with closing(sqlite3.connect(database)) as con, con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO shells VALUES (1,'DEV','dev')")
            con.execute("INSERT INTO shell_memory_archives VALUES (1,'001',1)")
            con.execute("INSERT INTO session_token_usage VALUES "
                        "(1,'codex','shared-session','local',?, 'Work',?,?,?,3,4,5,NULL,'ok',?,1,1)",
                        (name + '-model', times[1], times[2], 10 * index, times[2]))
            con.execute("INSERT INTO roadmap VALUES (1,'Released',?,'shipped')", (times[1],))
            con.execute("INSERT INTO documents VALUES (1,1,'Feature','spec',?,1)", (times[1],))
            con.execute("INSERT INTO conversation_runs VALUES (1,?,?,NULL)", times[1:])
            con.execute("INSERT INTO runs VALUES (1,'agent',?,?,'finished')", times[1:])
        repos.append(repo)
        databases.append(database)
    cache = tmp_path / "manager"
    manager = Analytics(cache, lambda: repos, engine_repo=upstream)
    return SimpleNamespace(manager=manager, repos=repos, dbs=databases, cache=cache, times=times,
                           upstream=upstream, refs=refs, state=state)


def test_project_history_and_retry(history):
    h = history
    manager = h.manager
    before = [db.read_bytes() for db in h.dbs]
    assert manager.seconds_until_due() == 0
    assert manager.refresh() == {"ok": True}
    assert [db.read_bytes() for db in h.dbs] == before
    assert INTERVAL - 3 <= manager.seconds_until_due() <= INTERVAL
    cards = manager.read("sessions")["sessions"]
    assert {row["project"]: row["input_tokens"] for row in cards} == {"alpha": 10, "beta": 20}
    query = {"project": ["beta"]}
    assert manager.read("filters", query)["models"] == ["beta-model"]
    assert manager.read("sessions", {**query, "model": ["alpha-model"]})["sessions"] == []
    assert [(r["source"], r["shell"]) for r in manager.read("activity", query)["activity"]] == [
        ("turn", "DEV"), ("agent", "DEV")]
    assert {r["subject"] for r in manager.read("engine-updates")["updates"]} == {"Engine one", "Engine two"}
    # Re-pinning a previously used revision is a separate dated event.
    pin(h.repos[0], h.refs[0], h.times[2])
    h.dbs[1].rename(h.dbs[1].with_suffix(".offline"))
    with closing(sqlite3.connect(h.dbs[0])) as con, con:
        con.execute("UPDATE session_token_usage SET input_tokens=12")
    assert not manager.refresh()["ok"]
    assert RETRY - 3 <= manager.seconds_until_due() <= RETRY
    restarted = Analytics(h.cache, lambda: h.repos, engine_repo=h.upstream)
    assert restarted.read("sessions", query)["sessions"][0]["input_tokens"] == 20
    assert "missing" in restarted.read("status")["projects"][1]["error"]
    assert len(restarted.read("engine-updates", {"project": ["alpha"]})["updates"]) == 3
    assert restarted.read("sessions", {"project": ["alpha"]})["sessions"][0]["input_tokens"] == 12
    h.dbs[1].with_suffix(".offline").rename(h.dbs[1])
    assert restarted.refresh()["ok"]
    assert restarted.read("status")["projects"][1]["error"] is None


def test_side_read_failure_and_relocation_hold(history):
    h = history
    assert h.manager.refresh()["ok"]
    with closing(sqlite3.connect(h.dbs[0])) as con, con:
        con.execute("DROP TABLE runs")
    receipt = h.dbs[1].parent / "relocation.json"
    receipt.write_text(json.dumps({"instance_id": "b" * 32, "status": "publishing"}))
    assert not h.manager.refresh()["ok"]
    status = h.manager.read("status")["projects"]
    assert "activity agent" in status[0]["warnings"]
    assert "relocation_incomplete" in status[1]["error"]
    assert len(h.manager.read("activity")["activity"]) == 4
    assert [s["usage_rows"] for s in status] == [1, 1]


def test_schedule_recovers_after_inventory_error(history):
    calls = []
    called = threading.Event()

    def unavailable():
        calls.append(1)
        called.set()
        raise OSError("inventory offline")

    service = Analytics(history.cache, unavailable)
    service.start()
    try:
        assert called.wait(3)
    finally:
        service.close()
    assert calls == [1]
    assert "inventory offline" in service.read("status")["last_pull"]["error"]
    service.start()
    service.close()
    assert calls == [1]
    assert history.manager.refresh()["ok"]
    assert INTERVAL - 3 <= service.seconds_until_due() <= INTERVAL


@pytest.mark.skipif(not os.environ.get("HALO_TOKENS_REFERENCE"), reason="optional read-only reference checkout")
def test_reference_parity(history, tmp_path, monkeypatch):
    """Run the original extractor and store only against this test's installations."""
    h = history
    source = Path(os.environ["HALO_TOKENS_REFERENCE"])
    registry = h.state / "super-coder" / "installs.json"
    registry.parent.mkdir()
    registry.write_text(json.dumps({"installs": list(map(str, h.repos))}))
    spec = importlib.util.spec_from_file_location("reference_store", source / "store.py")
    ref = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(ref)
    ref.DB = tmp_path / "reference.sqlite3"
    ref.MAIN_CHECKOUT = h.upstream

    with closing(ref.connect()) as con:
        for fail_beta in (False, True):
            if fail_beta:
                h.dbs[1].rename(h.dbs[1].with_suffix(".offline"))
            payload = json.loads(subprocess.check_output([sys.executable, "-B", source / "remote_extract.py"]))
            with con:
                ref.ingest(con, payload)
            assert h.manager.refresh()["ok"] != fail_beta
            for q in ({}, {"project": ["alpha"]}, {"project": ["beta"]}):
                for route in ("sessions", "usage", "activity", "filters", "engine-updates"):
                    expected = getattr(ref, route.replace("-", "_"))(con, q)
                    assert h.manager.read(route, q) == expected
            expected = ref.status(con)["projects"]
            actual = h.manager.read("status")["projects"]
            assert [(p["project"], p["usage_rows"], bool(p["error"])) for p in expected] == [
                (p["project"], p["usage_rows"], bool(p["error"])) for p in actual]


def test_dashboard_project_switch_and_refresh(history):
    """Exercise the packaged page and its JSON boundary in a disposable browser."""
    from urllib.parse import parse_qs, urlsplit

    playwright = pytest.importorskip("playwright.sync_api")

    h = history
    assert h.manager.refresh()["ok"]
    refreshes = []

    def dispatch(route):
        url = urlsplit(route.request.url)
        if url.path.startswith("/analytics/"):
            name = url.path.removeprefix("/analytics/") or "index.html"
            assert name in {"index.html", "theme.js", "style.css", "app.js"}
            route.fulfill(path=STATIC_DIR / name)
            return
        name = url.path.removeprefix("/api/analytics/")
        if route.request.method == "POST":
            assert name == "refresh"
            refreshes.append(name)
            body = h.manager.refresh()
        else:
            body = h.manager.read(name, parse_qs(url.query))
        route.fulfill(json=body)

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=shutil.which("chromium"))
        page = browser.new_page()
        try:
            page.route("http://manager.test/**", dispatch)
            page.goto("http://manager.test/analytics/")
            expect = playwright.expect
            expect(page.locator(".sess")).to_have_count(2)
            expect(page.locator(".sess-head .pill").get_by_text("alpha", exact=True)).to_be_visible()
            expect(page.locator(".an-card").first).to_contain_text("30")
            expect(page.locator(".ht-mark-hit title").first).to_contain_text("Engine")
            page.get_by_label("Project", exact=True).select_option("beta")
            expect(page.locator(".sess")).to_have_count(1)
            expect(page.locator(".an-card").first).to_contain_text("20")
            expect(page.locator(".ht-mark-hit title")).to_have_text("engine update\nbeta: " + h.refs[0] + " Engine one")
            page.get_by_role("button", name="Shell Hours", exact=True).click()
            expect(page.locator(".an-chart-title")).to_contain_text("per shell hour")
            h.dbs[1].rename(h.dbs[1].with_suffix(".offline"))
            page.get_by_role("button", name="Refresh").click()
            expect(page.locator("#collection-errors")).to_contain_text("beta: engine database missing")
            expect(page.locator(".sess")).to_have_count(1)
            expect(page.get_by_label("Project", exact=True)).to_have_value("beta")
            assert refreshes == ["refresh"]
        finally:
            browser.close()


def test_stopped_and_live_wal_collection(history):
    h = history
    db = h.dbs[0]
    # A clean shutdown checkpoints the DB and removes both journal files.
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    names = set(db.parent.iterdir())
    assert h.manager.refresh()["ok"]
    assert set(db.parent.iterdir()) == names
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("UPDATE session_token_usage SET input_tokens=17")
        writer.commit()
        assert db.with_name(db.name + "-wal").is_file()
        assert h.manager.refresh()["ok"]
        rows = h.manager.read("sessions", {"project": ["alpha"]})["sessions"]
        assert rows[0]["input_tokens"] == 17
