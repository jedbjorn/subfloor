"""Configured migrated check projections consumed by the actual native UI.

Chromium uses ephemeral synthetic HTTP, with no native process or account.
"""
# ruff: noqa: F811 - imported pytest fixtures are resolved by parameter name
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_conversation_native_ui import run_js
from test_native_automatic_updates import automatic, database, workflow  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_actual_configured_check_get_and_retained_prefix_feed_reference_predicates(automatic):
    import conversation_native_checks as checks
    value, _, _, calls, con, _ = automatic
    for i in range(18):
        ref = 'nc_'+f'{i:032x}'
        con.execute('INSERT INTO conversation_runtime_check_requests VALUES(?,?,?,?,?,?,?,?,?)',
            (ref, 1, 'old-'+str(i), 'digest', json.dumps(checks.CODEX_SELECTION), 'retained', '{}', i+1, i+1))
    con.commit()
    current = value.enqueue_automatic(checks.CODEX_SELECTION, 'a'*64)
    config = value.config()
    candidate = config['candidates'][0]
    readback = value.get(1, check_id=current['check_id'])
    assert candidate['automatic_check']['check_id'] == current['check_id']
    assert len(candidate['retained_checks']) == 16 and candidate['retained_checks_partial'] is True
    assert current['check_id'] not in [ref['check_id'] for ref in candidate['retained_checks']]
    assert calls == []  # GET references and result did not claim/dispatch.
    observed = run_js('''
const candidate='''+json.dumps(candidate)+''',result='''+json.dumps(readback)+''';
console.log(JSON.stringify({reference:chatNativeCheckReference(candidate.automatic_check),
  bound:chatNativeCheckMatches(result,candidate.automatic_check.check_id,candidate),
  grade:result.grades||{},admissible:result.admissible}));
''')
    assert observed['bound'] and observed['reference']['origin'] == 'installed_change'
    assert observed['admissible'] is False and observed['grade'] == {}


@pytest.mark.parametrize('bad', ['id','selection','state','null','foreign_generation','ordinary_role','ephemeral','route'])
def test_only_exact_check_and_probe_readback_can_navigate_setup(bad):
    result = {'check_id':'nc_'+'a'*32, 'state':'running', 'selection':{'harness':'codex','model':'sol','effort':'high'},
        'probe':{'conversation_id':'cv_owned','generation_id':'owned'}}
    chat = {'conversation_id':'cv_owned','runtime_mode':'native_experiment','route':dict(result['selection']),
        'runtime':{'generation_id':'owned','role':'probe'}}
    if bad == 'id': result['check_id'] = 'nc_'+'b'*32
    if bad == 'selection': result['selection']['model'] = 'foreign'
    if bad == 'state': result['state'] = 'invented'
    if bad == 'null': chat = None
    if bad == 'foreign_generation': chat['runtime']['generation_id'] = 'foreign'
    if bad == 'ordinary_role': chat['runtime']['role'] = 'ordinary'
    if bad == 'ephemeral': chat['runtime_mode'] = 'ephemeral'
    if bad == 'route': chat['route']['effort'] = 'low'
    checked = run_js('''
const result='''+json.dumps(result)+''', chat='''+json.dumps(chat)+''';
const selected={harness:'codex',model:'sol',effort:'high'};
console.log(JSON.stringify({check:chatNativeCheckMatches(result,'nc_'+'a'.repeat(32),selected),
  probe:chatNativeProbeMatches(chat,result)}));
''')
    assert not (checked['check'] and checked['probe'])


def test_actual_app_synthetic_browser_adopts_only_get_references():
    if not shutil.which('node'):
        pytest.skip('Node required')
    check = subprocess.run(['node','-e',"require(process.env.SC_PLAYWRIGHT_MODULE || 'playwright')"],
        capture_output=True,text=True,check=False)
    if check.returncode:
        pytest.skip('Named Playwright module required')
    result = subprocess.run(['node',str(ROOT/'tests/browser/native_check_adoption.cjs'),str(ROOT)],
        capture_output=True,text=True,check=False,timeout=60,env=os.environ.copy())
    assert result.returncode == 0, result.stdout+result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(receipt['cases']) == 16
    assert receipt['native_processes'] == receipt['inference_turns'] == 0


def test_pending_saved_and_latest_reads_keep_their_actual_admission_bounds():
    if not shutil.which('node'):
        pytest.skip('Node required')
    check = subprocess.run(['node','-e',"require(process.env.SC_PLAYWRIGHT_MODULE || 'playwright')"],
        capture_output=True,text=True,check=False)
    if check.returncode:
        pytest.skip('Named Playwright module required')
    result = subprocess.run(['node',str(ROOT/'tests/browser/native_check_pending.cjs'),str(ROOT)],
        capture_output=True,text=True,check=False,timeout=30,env=os.environ.copy())
    assert result.returncode == 0, result.stdout+result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(receipt['cases']) == 2 and receipt['posts'] == receipt['native'] == 0
    assert receipt['cases'][0]['maxActive'] <= 4
    assert receipt['cases'][1]['maxActive'] <= 64
