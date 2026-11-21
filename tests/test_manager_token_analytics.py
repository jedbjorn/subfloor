"""Local cross-project collection, dashboard and reference parity on disposable installs."""
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path[:0] = [str(ENGINE / "manager"), str(ENGINE / "scripts")]
from operator_password import make_verifier  # type: ignore[import-not-found]
from token_analytics import INTERVAL, RETRY, Analytics

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


def write_pin(repo, ref, when):
    path = repo / ".sc-state" / "engine.ref"
    path.parent.mkdir(exist_ok=True)
    path.write_text(ref + "\n")
    epoch = datetime.fromisoformat(when).timestamp()
    os.utime(path, (epoch, epoch))


def pin(repo, ref, when):
    write_pin(repo, ref, when)
    commit_pin(repo, when)


def commit_pin(repo, when):
    git(repo, "add", ".sc-state/engine.ref")
    git(repo, "commit", "-qm", "Pin engine", date=when)


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
        (engine / "api").mkdir(parents=True)
        (engine / "api/server.py").touch()
        (repo / "sc").touch()
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
    pin(h.repos[1], h.refs[1], h.times[2])
    manager.repositories = lambda: reversed(h.repos)  # collect the failing project first
    h.dbs[1].rename(h.dbs[1].with_suffix(".offline"))
    with closing(sqlite3.connect(h.dbs[0])) as con, con:
        con.execute("UPDATE session_token_usage SET input_tokens=12")
    assert not manager.refresh()["ok"]
    assert RETRY - 3 <= manager.seconds_until_due() <= RETRY
    restarted = Analytics(h.cache, lambda: h.repos, engine_repo=h.upstream)
    assert restarted.read("sessions", query)["sessions"][0]["input_tokens"] == 20
    beta = restarted.read("status")["projects"][1]
    assert "missing" in beta["error"]
    assert beta["engine_ref"] == h.refs[1]
    assert restarted.read("engine-updates", query)["updates"][0]["landed_at"] == h.times[2]
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
    pin(h.repos[1], h.refs[1], h.times[2])
    receipt.write_text(json.dumps({"instance_id": "b" * 32, "status": "publishing"}))
    assert not h.manager.refresh()["ok"]
    status = h.manager.read("status")["projects"]
    assert "activity agent" in status[0]["warnings"]
    assert "relocation_incomplete" in status[1]["error"]
    assert status[1]["engine_ref"] == h.refs[1]
    assert h.manager.read("engine-updates", {"project": ["beta"]})["updates"][0]["ref"] == h.refs[1]
    assert len(h.manager.read("activity")["activity"]) == 4
    assert [s["usage_rows"] for s in status] == [1, 1]


def test_unreadable_database_keeps_sessions_and_collects_new_pin(history):
    h = history
    beta = {"project": ["beta"]}
    assert h.manager.refresh()["ok"]
    sessions = h.manager.read("sessions", beta)
    pin(h.repos[1], h.refs[1], h.times[2])
    h.dbs[1].write_bytes(b"corrupt fixture")
    assert not h.manager.refresh()["ok"]
    assert "not a database" in h.manager.read("status")["projects"][1]["error"]
    assert h.manager.read("sessions", beta) == sessions
    update = h.manager.read("engine-updates", beta)["updates"][0]
    assert (update["ref"], update["landed_at"]) == (h.refs[1], h.times[2])


def test_working_rollback(history):
    h = history
    query = {"project": ["alpha"]}
    assert h.manager.refresh()["ok"]
    write_pin(h.repos[0], h.refs[0], h.times[2])
    expected = [(h.refs[0], h.times[2]), (h.refs[1], h.times[1]), (h.refs[0], h.times[0])]
    for _ in range(2):
        assert h.manager.refresh()["ok"]
        updates = h.manager.read("engine-updates", query)["updates"]
        assert [(row["ref"], row["landed_at"]) for row in updates] == expected
        assert h.manager.read("status")["projects"][0]["engine_ref"] == h.refs[0]
    commit_pin(h.repos[0], datetime.now(timezone.utc).isoformat())
    assert h.manager.refresh()["ok"]
    assert h.manager.read("engine-updates", query)["updates"] == updates


def publish_beta(h):
    pinfile = h.repos[1] / ".sc-state" / "engine.ref"
    shutil.copy2(pinfile, pinfile.with_name("engine.ref.prev"))
    write_pin(h.repos[1], h.refs[1], h.times[1])


def test_delayed_repin(history, tmp_path):
    h = history
    publish_beta(h)
    assert h.manager.refresh()["ok"]
    beta = {"project": ["beta"]}
    before = h.manager.read("engine-updates", beta)
    assert [(r["ref"], r["landed_at"]) for r in before["updates"]] == [
        (h.refs[1], h.times[1]), (h.refs[0], h.times[0])]
    commit_pin(h.repos[1], h.times[2])
    for manager in (h.manager, Analytics(tmp_path / "fresh", lambda: h.repos, engine_repo=h.upstream)):
        assert manager.refresh()["ok"]
        assert manager.read("engine-updates", beta) == before
        assert len(manager.read("engine-updates")["updates"]) == 4
        assert manager.read("engine-updates", {**beta, "from": [h.times[2]]}) == {"updates": []}


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


@pytest.fixture
def reference(history, tmp_path, monkeypatch):
    """Run the original extractor and store only against the disposable installations."""
    if not os.environ.get("HALO_TOKENS_REFERENCE"):
        pytest.skip("optional read-only reference checkout")
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

    return source, ref


@pytest.mark.parametrize("warm_cache", [False, True], ids=["fresh", "previously-refreshed"])
def test_delayed_repin_reference_parity(history, reference, warm_cache):
    h = history
    source, ref = reference
    publish_beta(h)

    def compare(con):
        payload = json.loads(subprocess.check_output([sys.executable, "-B", source / "remote_extract.py"]))
        with con:
            ref.ingest(con, payload)
        assert h.manager.refresh()["ok"]
        for query in ({}, {"project": ["alpha"]}, {"project": ["beta"]}):
            assert h.manager.read("engine-updates", query) == ref.engine_updates(con, query)

    with closing(ref.connect()) as con:
        if warm_cache:
            compare(con)
        commit_pin(h.repos[1], h.times[2])
        compare(con)


def test_reference_parity(history, reference):
    h = history
    source, ref = reference
    with closing(ref.connect()) as con:
        for fail_beta in (False, True):
            if fail_beta:
                write_pin(h.repos[0], h.refs[0], h.times[2])
                pin(h.repos[1], h.refs[1], h.times[2])
                h.dbs[1].rename(h.dbs[1].with_suffix(".offline"))
                with closing(sqlite3.connect(h.dbs[0])) as alpha, alpha:
                    alpha.execute("UPDATE session_token_usage SET input_tokens=12")
            payload = json.loads(subprocess.check_output([sys.executable, "-B", source / "remote_extract.py"]))
            with con:
                ref.ingest(con, payload)
            assert h.manager.refresh()["ok"] != fail_beta
            for q in ({}, {"project": ["alpha"]}, {"project": ["beta"]}):
                for route in ("sessions", "usage", "activity", "filters"):
                    expected = getattr(ref, route.replace("-", "_"))(con, q)
                    assert h.manager.read(route, q) == expected
                # The reference extractor retains dated rollbacks to old SHAs.
                # Its cache keeps only the first marker it sees for each SHA.
                markers = h.manager.read("engine-updates", q)
                if not fail_beta or q.get("project") == ["beta"]:
                    assert markers == ref.engine_updates(con, q)
                events = {(p["project"], r["ref"], r["landed_at"])
                          for p in payload["projects"] if not q or p["project"] in q["project"]
                          for r in p["engine_refs"]}
                assert sorted((r["project"], r["ref"], r["landed_at"]) for r in markers["updates"]) == sorted(events)
            expected = ref.status(con)["projects"]
            actual = h.manager.read("status")["projects"]
            assert [(p["project"], p["usage_rows"], bool(p["error"])) for p in expected] == [
                (p["project"], p["usage_rows"], bool(p["error"])) for p in actual]
            assert {p["project"]: p["engine_ref"] for p in actual} == {
                p["project"]: r["ref"] for p in payload["projects"] for r in p["engine_refs"] if r["current"]}


def test_dashboard_project_switch_and_refresh(history, tmp_path):
    """Navigate from inventory to the real packaged dashboard in a clean browser."""
    playwright = ENGINE / "browser/node_modules/playwright"
    chromium = shutil.which("chromium")
    if not (playwright.is_dir() and chromium and shutil.which("node")):
        pytest.skip("optional local Chromium seat")
    h = history
    assert h.manager.refresh()["ok"]
    release = tmp_path / "release"
    shutil.copytree(ENGINE / "manager", release)
    shutil.copy2(ENGINE / "scripts/instance_state.py", release / "instance_state.py")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = h.cache / "settings.json"
    settings.write_text(json.dumps({
        "root": str(h.repos[0].parent), "port": port, "listing": str(h.cache / "urls.json"),
        "operator_verifier": make_verifier("disposable"), "service_id": "fixture",
    }))
    script = """
    const assert = require('node:assert/strict');
    const {chromium} = require(process.argv[1]);
    (async () => {
      const browser = await chromium.launch({executablePath:process.argv[2], headless:true});
      try {
        const page = await browser.newPage({httpCredentials:{username:'operator', password:'disposable'}});
        await page.goto(`http://127.0.0.1:${process.argv[3]}/`);
        await page.getByRole('link', {name:'Token Analytics'}).click();
        await page.locator('.sess').first().waitFor();
        assert.equal(await page.locator('.sess').count(), 2);
        assert(await page.locator('.sess-head .pill').getByText('alpha', {exact:true}).isVisible());
        assert.match(await page.locator('.an-card').first().innerText(), /30/);
        assert.match(await page.locator('.ht-mark-hit title').first().textContent(), /Engine/);
        await page.getByLabel('Project', {exact:true}).selectOption('beta');
        await page.waitForFunction(() => document.querySelectorAll('.sess').length === 1);
        assert.match(await page.locator('.an-card').first().innerText(), /20/);
        assert.equal(await page.locator('.ht-mark-hit title').textContent(),
          'engine update\\nbeta: ' + process.argv[4] + ' Engine one');
        await page.getByRole('button', {name:'Shell Hours', exact:true}).click();
        await page.waitForFunction(() => document.querySelector('.an-chart-title').textContent.includes('per shell hour'));
        await page.getByRole('button', {name:'Refresh', exact:true}).click();
        await page.waitForFunction(() => document.querySelector('#collection-errors').textContent.includes(
          'beta: engine database missing'));
        assert.equal(await page.locator('.sess').count(), 1);
        assert.equal(await page.getByLabel('Project', {exact:true}).inputValue(), 'beta');
      } finally { await browser.close(); }
    })().catch(error => {console.error(error); process.exitCode = 1;});
    """
    process = subprocess.Popen(
        [sys.executable, "-E", "-s", str(release / "server.py"), str(settings), str(h.upstream)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _, stderr = process.communicate()
                pytest.fail(f"Manager exited before the browser check: {stderr}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.2) as response:
                    if response.read() == b"fixture":
                        break
            except OSError:
                time.sleep(.05)
        else:
            pytest.fail("Manager did not become ready")
        h.dbs[1].rename(h.dbs[1].with_suffix(".offline"))
        result = subprocess.run(
            ["node", "-e", script, str(playwright), chromium, str(port), h.refs[0]],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


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
