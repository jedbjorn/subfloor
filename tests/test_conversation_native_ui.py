"""Native Chats UI evidence uses synthetic server projections, no native root."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder/ui/app.js").read_text()
NATIVE = APP[APP.index("// Native experiment: consume"):APP.index("async function chatRenderNew(")]


def run_js(script: str):
    result = subprocess.run(["node", "-e", NATIVE + script], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_targets_require_exact_stored_keys_current_inventory_and_each_variant():
    result = run_js('''
const cv={conversation_id:'cv_ui',version:7,state:'idle',runtime:{generation_id:'gen',state:'ready',
  primary:{activity_id:'root-turn'},capabilities:{stop_reply:'compatible',stop_work:'compatible',stop_work_terminal:'compatible'}}};
const row={work_key:'opaque-server-key',kind:'work.observed',partial:false,freshness:'current',
 data:{kind:'terminal',status:'running'},reference:{work_id:'native-opaque-id'}};
const terminal=chatNativeControlBody(cv,'stop_work',row);
const refused=[{...row,work_key:null},{...row,partial:true},{...row,freshness:'stale'},
 {...row,data:{kind:'unknown'}},{...row,data:{kind:'child'},reference:{activity_id:'child-turn'}},
 {...row,kind:'work.terminal'}].map(r=>chatNativeControlBody(cv,'stop_work',r));
const reply=chatNativeControlBody(cv,'stop_reply');
cv.close_requested_at=true;const closed=chatNativeControlBody(cv,'stop_reply');
console.log(JSON.stringify({terminal,refused,reply,closed}));
''')
    assert result["terminal"] == {"version": 7, "generation_id": "gen", "action": "stop_work", "work_key": "opaque-server-key"}
    assert result["refused"] == [None] * 6
    assert result["reply"]["expected_activity_id"] == "root-turn"
    assert result["closed"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_generation_setup_and_child_activity_are_never_guessed():
    result = run_js('''
const cv={version:4,state:'idle',runtime:{generation_id:'gen',state:'needs_consent',
 setup:{generation_id:'gen',setup_id:'server-setup',phase:'local_channel_development_consent'},
 capabilities:{stop_work:'compatible',stop_work_child:'compatible'}}};
const enable=chatNativeControlBody(cv,'enable_local_channel');
cv.runtime.setup.generation_id='old';const stale=chatNativeControlBody(cv,'enable_local_channel');
cv.runtime.state='ready';
const work={work_key:'child-key',partial:false,freshness:'current',kind:'work.observed',data:{kind:'child',state:'inProgress'},reference:{activity_id:'exact-child-turn'}};
const child=chatNativeControlBody(cv,'stop_work',work);
work.reference={};const missing=chatNativeControlBody(cv,'stop_work',work);
console.log(JSON.stringify({enable,stale,child,missing}));
''')
    assert result["enable"] == {"version": 4, "generation_id": "gen", "action": "enable_local_channel", "setup_id": "server-setup"}
    assert result["stale"] is None and result["missing"] is None
    assert result["child"]["expected_activity_id"] == "exact-child-turn"


def test_fresh_node_chromium_mocked_server_workflow():
    if not shutil.which("node"):
        pytest.skip("Node required")
    check = subprocess.run(["node", "-e", "require(process.env.SC_PLAYWRIGHT_MODULE || 'playwright')"],
                           capture_output=True, text=True, check=False)
    if check.returncode:
        pytest.skip("Install Node Playwright or set SC_PLAYWRIGHT_MODULE for the named seat")
    result = subprocess.run(["node", str(ROOT / "tests/browser/native_chats.cjs"), str(ROOT)],
                            capture_output=True, text=True, timeout=60, env=os.environ.copy(), check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["native_processes"] == 0 and receipt["inference_turns"] == 0
    assert receipt["controls_unknown_no_replay"] and receipt["close_available"]
    assert receipt["cold_check_readback"] and receipt["default_ephemeral"]
    assert receipt["creation_readback_no_replay"]
    assert receipt["send_readback_no_replay"] and receipt["cleanup_bound_new_check"]
    assert receipt["separate_chat_consent"] and receipt["production_opt_in_hidden"]
    assert receipt["composer_reachable"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_only_explicit_same_item_mirroring_removes_native_output():
    result = run_js('''
const output=(sequence,thread,item,extra={})=>({generation_id:'gen',controller_sequence:sequence,
 kind:'output.delta',reference:{root_id:'root',thread_id:thread,item_id:item},engine_run_id:7,...extra});
const runtime={generation_id:'gen',activity:[
 output(1,'root','assistant'),output(2,'root','assistant',{kind:'output.final',engine_mirrored:true}),
 output(3,'child','assistant',{kind:'output.final',engine_mirrored:false}),
 output(4,'root','terminal',{kind:'output.final',engine_mirrored:false}),
 output(5,'root','unknown-mirror',{engine_mirrored:'true'}),
 output(6,'root','setup',{source:'system'}),output(7,'root','old',{generation_id:'old'}),
 {generation_id:'gen',controller_sequence:8,kind:'activity.terminal',engine_mirrored:true},
 output(9,'root','assistant',{kind:'output.final',engine_mirrored:false,partial:true})]};
console.log(JSON.stringify(chatNativeVisibleActivity(runtime).map(row=>row.controller_sequence)));
''')
    assert result == [3, 4, 5, 6, 8, 9]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_itemless_final_dedup_uses_only_exact_server_output_key_and_retains_partial():
    result = run_js('''
const final=(sequence,key,extra={})=>({generation_id:'gen',controller_sequence:sequence,
 kind:'output.final',partial:false,freshness:'current',reference:{root_id:'root',activity_id:'turn'},
 engine_output_key:key,data:{text:'Same text does not establish identity'},...extra});
const runtime={generation_id:'gen',activity:[final(1,'stored-part-A'),final(2,'stored-part-A'),
 final(3,'stored-part-B'),final(4,'stored-part-A',{partial:true}),final(5,null),final(6,null),
 final(7,'stored-mirrored',{engine_mirrored:true}),final(8,'stored-part-C',{freshness:'stale'})]};
console.log(JSON.stringify(chatNativeVisibleActivity(runtime).map(row=>row.controller_sequence)));
''')
    assert result == [1, 3, 4, 5, 6, 8]


def test_actual_codex_work_event_state_survives_public_projection_into_ui():
    """Use emitted driver bytes and the public sanitizer, without native start."""
    import sys
    from dataclasses import asdict
    sys.path.insert(0, str(ROOT / '.super-coder/scripts'))
    from conversation_adapters.codex_runtime import CodexRuntimeDriver
    from conversation_native_chats import projection

    driver = CodexRuntimeDriver()
    from conversation_runtime_contract import RuntimeIdentity
    driver._identity = RuntimeIdentity('root', 'root')
    emitted = []
    driver._emit = emitted.append
    import sqlite3
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.executescript("""
CREATE TABLE conversation_runtime_generations(generation_id TEXT,conversation_id TEXT,owner_user_id INTEGER,shell_id INTEGER);
CREATE TABLE conversation_runtime_work(generation_id TEXT,work_key TEXT,projection_json TEXT,last_sequence INTEGER);
CREATE TABLE conversation_runtime_commands(generation_id TEXT,command_id TEXT,state TEXT,intent_json TEXT,receipt_json TEXT,kind TEXT,command_sequence INTEGER);
CREATE TABLE conversation_runtime_events(generation_id TEXT,sequence INTEGER,event_json TEXT);
INSERT INTO conversation_runtime_generations VALUES('gen','cv_source',1,1);
""")
    conversation = {'runtime_mode':'native_experiment','runtime_projection':json.dumps({'generation_id':'gen'}),
                    'conversation_id':'cv_source','owner_user_id':1,'shell_id':1,'harness':'codex','worktree':str(ROOT)}
    observations = []
    for state, completed in [('inProgress', False), ('completed', True), ('terminated', False)]:
        driver._item('root', 'turn', {'type':'commandExecution','id':'item','processId':'native-process',
                                     'status':state,'command':'finite source fixture'},
                     completed=completed, provenance='codex:item/completed' if completed else 'codex:item/started')
        event = next(event for event in reversed(emitted) if event.kind.startswith('work.'))
        con.execute('DELETE FROM conversation_runtime_work')
        con.execute('INSERT INTO conversation_runtime_work VALUES(?,?,?,1)',
                    ('gen','stored-key',json.dumps(asdict(event))))
        row = projection(conversation, con=con)['work'][0]
        # terminated is deliberately partial at the native driver's consumed
        # item boundary; do not erase that uncertainty to authorize controls.
        observations.append(row)
    con.close()
    result = run_js('''
const cv={version:1,state:'idle',runtime:{generation_id:'gen',state:'ready',
 capabilities:{stop_work:'compatible',stop_work_terminal:'compatible'}}};
const rows=''' + json.dumps(observations) + ''';
console.log(JSON.stringify(rows.map(row=>({state:chatNativeWorkState(row),control:chatNativeControlBody(cv,'stop_work',row)}))));
''')
    assert result[0]['state'] == {'state':'inProgress','terminal':False,'partial':False}
    assert result[0]['control']['work_key'] == 'stored-key'
    assert result[1]['state'] == {'state':'completed','terminal':True,'partial':False}
    assert result[1]['control'] is None
    assert result[2]['state'] == {'state':'terminated','terminal':True,'partial':True}
    assert result[2]['control'] is None


def test_work_state_unknown_conflicting_and_terminal_observed_rows_cannot_stop():
    result = run_js('''
const cv={version:1,state:'idle',runtime:{generation_id:'gen',state:'ready',
 capabilities:{stop_work:'compatible',stop_work_terminal:'compatible'}}};
const base={work_key:'key',kind:'work.observed',freshness:'current',partial:false,reference:{work_id:'owned'},data:{kind:'terminal'}};
const data=[{state:42,status:'running'},{state:null,status:'running'},{state:'future-state'},
 {state:'inProgress',status:'completed'},{state:'completed'},{state:'terminated'},
 {status:'running'},{status:'completed'},{state:'running',status:'running'}];
console.log(JSON.stringify(data.map(fields=>{const row={...base,data:{...base.data,...fields}};
 return {state:chatNativeWorkState(row),control:chatNativeControlBody(cv,'stop_work',row)};})));
''')
    for row in result[:4]:
        assert row['state'] == {'state':'unknown','terminal':False,'partial':True}
        assert row['control'] is None
    for row in result[4:6]:
        assert row['state']['terminal'] is True
        assert row['control'] is None
    assert result[6]['state'] == {'state':'running','terminal':False,'partial':False}
    assert result[6]['control']['work_key'] == 'key'  # Legacy Claude status fallback.
    assert result[7]['control'] is None
    assert result[8]['control']['work_key'] == 'key'
    assert 'el("span", {}, workState.state)' in NATIVE
    assert '${workState.partial ? " · partial"' in NATIVE


def test_only_explicit_false_partial_can_display_complete_work():
    result = run_js("""
const rows=[{partial:false},{partial:true},{partial:'false'},{partial:null},{}];
console.log(JSON.stringify(rows.map(row=>chatNativeWorkState({...row,data:{kind:'terminal',state:'inProgress'}}))));
""")
    assert result[0] == {'state':'inProgress','terminal':False,'partial':False}
    assert all(row == {'state':'inProgress','terminal':False,'partial':True} for row in result[1:])


def test_fresh_chromium_same_intent_check_discovery():
    if not shutil.which('node'):
        pytest.skip('Node required')
    installed=subprocess.run(['node','-e',"require(process.env.SC_PLAYWRIGHT_MODULE || 'playwright')"],capture_output=True,text=True,check=False)
    if installed.returncode:
        pytest.skip('Pinned Playwright required for named source seat')
    result=subprocess.run(['node',str(ROOT/'tests/browser/native_check_discovery.cjs'),str(ROOT)],capture_output=True,text=True,timeout=60,env=os.environ.copy(),check=False)
    assert result.returncode==0,result.stdout+result.stderr
    receipt=json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['native_processes']==0 and receipt['inference_turns']==0
    assert receipt['cases']==['converge','immediate','error','expiry','selection','dispose','replacement','terminal_race']
