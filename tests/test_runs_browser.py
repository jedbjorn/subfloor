"""Optional Chromium proof of the Runs UI against real ledger/process evidence.

SC_RUNS_SCREENSHOTS retains developer captures outside Git. Unrelated chat and
harness metadata are fixtures; run HTTP, /proc, broker finish and transcript
projection are real. No managed browser or visual-QA service is involved.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder' / 'api'))
import conversation_routes
import server
from runs import RunStore
from signin_fixture import sign_in
from test_run_processes import (  # noqa: F401
    bind_process,
    broker_case,
    finish_native_turn,
    native_process,
)


def test_runs_drawer_fleet_process_and_turn_end(broker_case, native_process, monkeypatch):  # noqa: F811
    case = broker_case
    process, child_pid = native_process
    cid, _, rid, _ = bind_process(case, process)
    with case.connect() as con:
        store = RunStore(con, case.root / 'engine')
        for index, (state, wake, kind) in enumerate([
            ('running', 'none', 'job'), ('done', 'consumed', 'devkit'),
            ('failed', 'blocked', 'job'), ('timeout', 'pending', 'probe'),
            ('lost', 'enqueued', 'job'),
        ]):
            run = store.register(1 if index < 4 else 2, {
                'argv': ['test'], 'cwd': str(case.worktree), 'registration_key': f'fixture-{index}',
                'label': ['Long tests', 'Passing selection', 'Failing selection', 'Wait for service', 'Lost supervisor'][index],
                'kind': kind,
            })
            con.execute('UPDATE runs SET state=?,wake_state=?,exit_code=?,started_at=datetime(\'now\') WHERE run_id=?',
                        (state, wake, 0 if state == 'done' else 1 if state == 'failed' else None, run['run_id']))
            con.commit()
            log = Path(run['evidence_path'])
            log.parent.mkdir(parents=True)
            log.write_text('Fixture test output\n')
    monkeypatch.setattr(server, 'DB_PATH', str(case.db_path))
    monkeypatch.setattr(server, 'ENGINE', case.root / 'engine')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    shells = [{'shell_id': i, 'shortname': f'SH{i}', 'display_name': f'Developer {i}', 'flavor': 'dev', 'skills': []} for i in (1, 2)]
    current = {'conversation_id': cid, 'shell': shells[0], 'state': 'running', 'version': 1,
               'title': 'Runs acceptance · live process evidence', 'route': {'harness': 'codex', 'model': 'fixture'},
               'process': {'alive': True, 'pid': process.pid}, 'created_at': '2026-10-05 09:00:00'}
    verified = {'tier': 'deny-only', 'version': 'fixture version', 'verified_at': '2026-10-05T09:00:00Z'}
    requests = []

    def route_api(route):
        path = urlparse(route.request.url).path
        requests.append((route.request.method, path))
        if path.startswith('/api/runs'):
            return route.continue_()
        if path == '/api/health':
            data = {'repo': 'isolated Runs acceptance fixture'}
        elif path == '/api/shells':
            data = {'shells': shells, 'repo_root': str(case.worktree)}
        elif path.startswith('/api/shells/'):
            data = shells[0]
        elif path == '/api/shell-templates':
            data = {'templates': []}
        elif path == '/api/flavor-defaults':
            data = {'flavors': {}, 'harness_status': {
                'codex': {'installed': True, 'enabled': True, 'healthy': True, 'surfaces': {'browser': True}, 'conversion': None},
                'claude': {'installed': True, 'enabled': True, 'healthy': True, 'surfaces': {'browser': True}, 'conversion': verified}}}
        elif path == '/api/harnesses/claude/conversion/verify':
            verified.update(tier='rewrite')
            data = verified
        elif path == '/api/models':
            data = {'harnesses': {}}
        elif path == '/api/conversations':
            data = {'items': [current], 'next_cursor': None}
        elif path == f'/api/conversations/{cid}':
            data = current
        elif path == f'/api/conversations/{cid}/transcript':
            with case.connect() as con:
                data = conversation_routes._transcript_projection(con, cid, owner_user_id=1)
        else:
            data = {'items': []}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

    captures = Path(os.environ['SC_RUNS_SCREENSHOTS']) if os.environ.get('SC_RUNS_SCREENSHOTS') else None
    if captures:
        captures.mkdir(parents=True, exist_ok=True)
    errors = []
    # The operator routes answer only a signed-in browser (Feature #94).
    session = sign_in()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=shutil.which('chromium'))
            context = browser.new_context(viewport={'width': 1500, 'height': 1050})
            context.add_cookies([{'name': session.cookie_name, 'value': session.token, 'url': base}])
            page = context.new_page()
            page.add_init_script('''window.EventSource = class {
              constructor() { this.listeners = {}; window.fixtureEvents = this; queueMicrotask(() => this.onopen?.()); }
              addEventListener(type, fn) { this.listeners[type] = fn; }
              close() {}
            };''')
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)
            page.goto(base + f'/#interface/SH1/{cid}')
            page.locator('.runs-drawer summary').click()
            page.locator('.runs-drawer .run-row').first.wait_for()
            page.wait_for_function('(pid) => document.querySelector(".process-tile").textContent.includes(String(pid))', arg=child_pid)
            assert page.locator('.runs-drawer .run-row').count() == 4
            assert 'blocked' in page.locator('.runs-drawer').inner_text()
            if captures:
                page.screenshot(path=str(captures / '01-drawer-live-process.png'), full_page=True)
            finish_native_turn(case, rid, process)
            current['state'] = 'idle'
            current['process']['alive'] = False
            # Replay the real persisted event through the browser's SSE reducer.
            with case.connect() as con:
                events = [dict(row) for row in con.execute('SELECT * FROM conversation_events WHERE conversation_id=? ORDER BY sequence', (cid,))]
            for event in events:
                event['payload'] = json.loads(event['payload'])
                page.evaluate('(e) => window.fixtureEvents.listeners[e.event_type]?.({data: JSON.stringify(e), lastEventId: String(e.sequence)})', event)
            page.get_by_text('they were terminated with it.', exact=False).wait_for()
            page.wait_for_function('() => document.querySelector(".process-tile").textContent === ""')
            if captures:
                page.screenshot(path=str(captures / '02-turn-end-activity.png'), full_page=True)
            page.reload()
            page.get_by_text('they were terminated with it.', exact=False).wait_for()
            page.goto(base + '/#shells-runs')
            page.locator('.shell-pane .run-row').first.wait_for()
            assert page.locator('.shell-pane .run-row').count() == 5
            assert 'Runs (2)' in page.locator('.vtabs').text_content()
            if captures:
                page.screenshot(path=str(captures / '03-fleet-runs.png'), full_page=True)
            page.get_by_role('combobox', name='States').select_option('failed')
            page.wait_for_function('() => document.querySelectorAll(".shell-pane .run-row").length === 1')
            assert 'Failing selection' in page.locator('.shell-pane').inner_text()
            page.get_by_role('button', name='Tail', exact=True).click()
            page.get_by_text('Fixture test output').wait_for()
            page.get_by_role('button', name='Close', exact=True).click()
            page.goto(base + '/#shells')
            page.get_by_role('button', name='Re-verify').click()
            page.get_by_text('fixture version · rewrite').wait_for()
            assert ('POST', '/api/harnesses/claude/conversion/verify') in requests
            if captures:
                page.screenshot(path=str(captures / '04-harness-conversion.png'), full_page=True)
                (captures / 'process-evidence.json').write_text(json.dumps({'conversation_id': cid, 'turn_run_id': rid,
                    'root_pid': process.pid, 'child_pid': child_pid, 'events': events}, indent=2))
            assert errors == []
            browser.close()
    finally:
        session.restore()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def test_subagents_watches_limits_and_worktree_labels(broker_case, monkeypatch):  # noqa: F811
    """Feature #96 (spec #273 "GUI surface"): the drawer and the fleet view show
    subagents and watches from a real ledger over the real run routes. The
    chat, shell and worktree listings around them are fixtures."""
    from datetime import datetime, timedelta, timezone

    case = broker_case
    engine = case.root / 'engine'
    now = datetime.now(timezone.utc)
    ago = lambda **delta: (now - timedelta(**delta)).isoformat()  # noqa: E731
    note = ("harness process 4242 was running and was left running: "
            "it could not be tied to this run's worktree")
    with case.connect() as con:
        store = RunStore(con, engine)

        def agent(owner, key, label, harness, model, effort=None, continues=None, timeout=None):
            return store.register(owner, {
                'registration_key': key, 'kind': 'agent', 'label': label, 'argv': ['x'],
                'commit': 'a' * 40, 'timeout_seconds': timeout,
                'agent': {'harness': harness, 'requested_model': model, 'requested_effort': effort,
                          'effective_effort': effort, 'brief_bytes': 10, 'continues_run_id': continues}})

        def set_run(row, **values):
            con.execute(f"UPDATE runs SET {','.join(f'{k}=?' for k in values)} WHERE run_id=?",
                        (*values.values(), row['run_id']))
            con.commit()

        first = agent(1, 'a1', 'Check spec sites', 'codex', 'gpt-6-luna', 'low')
        set_run(first, state='done', exit_code=0, wake_state='consumed',
                started_at=ago(minutes=50), finished_at=ago(minutes=41))
        con.execute("UPDATE run_agents SET session_ref='thread-1', result_bytes=47, usage_json=? "
                    "WHERE run_id=?", (json.dumps({'input_tokens': 41200, 'output_tokens': 1800}),
                                       first['run_id']))
        con.commit()
        result = Path(first['evidence_path']).with_name('result.md')
        result.parent.mkdir(parents=True)
        result.write_text('Three sites moved on main: <b>not markup</b>.\n')
        second = agent(1, 'a2', 'Check spec sites', 'codex', 'gpt-6-luna', 'low',
                       continues=first['run_id'])
        set_run(second, state='running', started_at=ago(minutes=12))
        review = agent(1, 'a3', 'Review the diff', 'claude', 'haiku', timeout=3600)
        set_run(review, state='running', started_at=ago(minutes=40))
        agent(1, 'a4', 'Research the resume form', 'opencode', 'ollama-cloud/deepseek-v4.1-flash')
        lost = agent(2, 'a5', 'Lost reader', 'kimi', 'kimi-code/kimi-for-coding-highspeed')
        set_run(lost, state='lost', wake_state='pending', started_at=ago(minutes=30),
                finished_at=ago(minutes=20),
                terminal_json=json.dumps({'state': 'lost', 'spawn_error': note}))
        watch = store.register(1, {
            'registration_key': 'w1', 'kind': 'probe', 'label': 'PR #1786', 'cwd': str(case.worktree),
            'argv': ['sc', 'job', 'watch', 'pr', '--pr', '1786'], 'timeout_seconds': 3600})
        set_run(watch, state='running', started_at=ago(minutes=10))
        log = Path(watch['evidence_path'])
        log.parent.mkdir(parents=True)
        log.write_text('{"number": 1, "exit_code": 1, "excerpt": "pending at head 0a1b2c3"}\n'
                       '{"number": 2, "exit_code": 1, "excerpt": "pending at head 0a1b2c3"}\n')
        timer = store.register(2, {
            'registration_key': 'w2', 'kind': 'probe', 'label': 'timer 600s', 'cwd': str(case.worktree),
            'argv': ['sc', 'job', 'watch', 'timer', '--after', '600'], 'timeout_seconds': 600})
        set_run(timer, state='running', started_at=ago(minutes=2))
        until = store.register(1, {
            'registration_key': 'w3', 'kind': 'probe', 'label': 'wait for file', 'cwd': str(case.worktree),
            'argv': ['/bin/sh', '-c', 'test -f ready'], 'timeout_seconds': 3600})
        set_run(until, state='done', exit_code=0, wake_state='consumed', started_at=ago(minutes=9),
                finished_at=ago(minutes=5), terminal_json=json.dumps({
                    'state': 'done', 'exit_code': 0,
                    'probe_attempt': {'number': 4, 'exit_code': 0, 'excerpt': 'ready'}}))
    monkeypatch.setattr(server, 'DB_PATH', str(case.db_path))
    monkeypatch.setattr(server, 'ENGINE', engine)
    session = sign_in()
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    shells = [{'shell_id': i, 'shortname': f'SH{i}', 'display_name': f'Developer {i}', 'flavor': 'dev', 'skills': []} for i in (1, 2)]
    cid = 'cv_fixture'
    current = {'conversation_id': cid, 'shell': shells[0], 'state': 'idle', 'version': 1,
               'title': 'Subagents and watches', 'route': {'harness': 'claude', 'model': 'fixture'},
               'process': {'alive': False}, 'created_at': '2026-11-12 09:00:00'}
    worktree = {'dirty': 2, 'dirty_files': ['?? notes.md', ' M tracked.txt'], 'ahead': 0, 'behind': 0,
                'detached': True, 'branch': None, 'is_main': False}
    git_state = {
        'repo': {'name': 'fixture', 'default_branch': 'main'}, 'fetched': False, 'gh_available': True,
        'summary': {'dirty_worktrees': 1, 'stale_branches': 0, 'all_clean': False, 'worktrees': 3},
        'branches': [],
        'worktrees': [
            {**worktree, 'path': '.', 'abs': '/repo', 'branch': 'main', 'dirty': 0, 'dirty_files': [], 'is_main': True},
            {**worktree, 'path': f".sc-worktrees/agent-{first['run_id']}", 'abs': first['cwd']},
            {**worktree, 'path': '.sc-worktrees/agent-900', 'abs': str(Path(first['cwd']).with_name('agent-900')),
             'dirty': 0, 'dirty_files': []}]}
    with case.connect() as con:
        RunStore(con, engine).label_worktrees(git_state['worktrees'])

    def route_api(route):
        path = urlparse(route.request.url).path
        if path.startswith('/api/runs'):
            return route.continue_()
        if path == '/api/health':
            data = {'repo': 'isolated Runs acceptance fixture'}
        elif path == '/api/shells':
            data = {'shells': shells, 'repo_root': str(case.worktree)}
        elif path.startswith('/api/shells/'):
            data = shells[0]
        elif path == '/api/shell-templates':
            data = {'templates': []}
        elif path == '/api/flavor-defaults':
            data = {'flavors': {}, 'harness_status': {}}
        elif path == '/api/models':
            data = {'harnesses': {}}
        elif path == '/api/conversations':
            data = {'items': [current], 'next_cursor': None}
        elif path == f'/api/conversations/{cid}':
            data = current
        elif path == '/api/git-state':
            data = git_state
        else:
            data = {'items': []}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

    captures = Path(os.environ['SC_RUNS_SCREENSHOTS']) if os.environ.get('SC_RUNS_SCREENSHOTS') else None
    if captures:
        captures.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=shutil.which('chromium'))
            context = browser.new_context(viewport={'width': 1500, 'height': 1300})
            context.add_cookies([{'name': session.cookie_name, 'value': session.token, 'url': base}])
            page = context.new_page()
            page.add_init_script('''window.EventSource = class {
              constructor() { queueMicrotask(() => this.onopen?.()); }
              addEventListener() {}
              close() {}
            };''')
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)

            # The Runs drawer of one shell's chat.
            page.goto(base + f'/#interface/SH1/{cid}')
            page.locator('.runs-drawer summary').first.click()
            page.locator('.runs-drawer .run-row').first.wait_for()
            page.get_by_text('Last attempt 2: exit 1 · pending at head 0a1b2c3').wait_for()
            drawer = page.locator('.runs-drawer').inner_text()
            assert 'Subagents: 3 of 3 live for SH1 · 3 of 6 on this install.' in drawer
            assert 'Watchers: 1 of 10 live for SH1.' in drawer
            assert 'SH1 is at its limit of 3 live subagents. A further launch is refused until one ends.' in drawer
            assert f"codex · gpt-6-luna · effort low · continues run #{first['run_id']}" in drawer
            assert 'left of 2h00m' in drawer and 'left of 1h00m' in drawer
            assert 'claude · haiku · effort default' in drawer
            assert (f"Input + output tokens: 43000 for this thread, counted on run #{first['run_id']}"
                    in drawer)
            assert 'watch pr · PR #1786: checks settle (green or red), or it is merged or closed' in drawer
            assert 'watch until · test -f ready' in drawer and 'Last attempt 4: exit 0 · ready' in drawer
            assert 'Lost reader' not in drawer    # another shell's run
            # Three subagent threads, two watches; the first run is folded under its continue.
            assert page.locator('.runs-drawer .run-list > .run-row').count() == 5
            fold = page.locator('.runs-drawer .run-thread')
            assert fold.locator('summary').inner_text() == '1 earlier run of this subagent'
            # The clock of a live row moves without a refresh.
            live_clock = ('[...document.querySelectorAll(".runs-drawer .run-clock")]'
                          '.find((node) => node.textContent.startsWith("elapsed"))')
            before = page.evaluate(f'() => {live_clock}.textContent')
            assert ' left of ' in before
            page.wait_for_function(f'(text) => {live_clock}.textContent !== text', arg=before)
            fold.locator('summary').click()
            if captures:
                page.screenshot(path=str(captures / '10-drawer-subagents-watches.png'), full_page=True)
            fold.get_by_role('button', name='Result', exact=True).click()
            page.get_by_text('Three sites moved on main: <b>not markup</b>.').wait_for()
            assert page.get_by_text('done · exit 0 · codex · gpt-6-luna · effort low · 47 bytes').count() == 1
            if captures:
                page.screenshot(path=str(captures / '11-result.png'), full_page=True)
            page.get_by_role('dialog').get_by_role('button', name='Close', exact=True).click()
            # A live subagent has no result yet, and says so.
            page.locator(f".runs-drawer article[aria-label='Run {second['run_id']}']").get_by_role(
                'button', name='Result', exact=True).click()
            page.get_by_text('No result is recorded yet.').wait_for()
            page.get_by_role('dialog').get_by_role('button', name='Close', exact=True).click()

            # The fleet view.
            page.goto(base + '/#shells-runs')
            page.locator('.shell-pane .run-row').first.wait_for()
            fleet = page.locator('.shell-pane').inner_text()
            assert 'Subagents: 3 of 6 live on this install; one shell may run 3.' in fleet
            assert 'Watchers: 2 live on this install; one shell may run 10.' in fleet
            assert 'SH1 is at its limit of 3 live subagents.' in fleet
            assert f'When the run was marked lost: {note}' in fleet
            assert 'watch timer · 600 seconds pass' in fleet
            assert 'Runs (4)' in page.locator('.vtabs').text_content()
            if captures:
                page.screenshot(path=str(captures / '12-fleet.png'), full_page=True)
            page.get_by_role('combobox', name='Kinds').select_option('agent')
            page.wait_for_function('() => document.querySelectorAll(".shell-pane .run-list > .run-row").length === 4')
            assert 'watch pr' not in page.locator('.shell-pane').inner_text()
            page.get_by_role('combobox', name='Shells').select_option('2')
            page.get_by_text('Subagents: 0 of 3 live for SH2 · 3 of 6 on this install.').wait_for()
            if captures:
                page.screenshot(path=str(captures / '13-fleet-agents-of-one-shell.png'), full_page=True)
            # Nothing on the page launches a run: the only run writes are stop and attach.
            assert page.get_by_role('button', name='Launch').count() == 0

            # The Worktrees tab.
            page.goto(base + '/#worktrees')
            page.get_by_text(f"subagent run #{first['run_id']} · launched by SH1").wait_for()
            tab = page.locator('#view-worktrees').inner_text()
            assert f"codex · gpt-6-luna · latest run #{second['run_id']} running" in tab
            assert 'A subagent run is live in it.' in tab
            assert 'subagent run #900 · not in the run ledger' in tab
            if captures:
                page.screenshot(path=str(captures / '14-worktrees.png'), full_page=True)
            assert errors == []
            browser.close()
    finally:
        session.restore()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
