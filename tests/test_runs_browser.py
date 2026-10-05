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
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=shutil.which('chromium'))
            page = browser.new_page(viewport={'width': 1500, 'height': 1050})
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
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
