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
const work={work_key:'child-key',partial:false,freshness:'current',kind:'work.observed',data:{kind:'child'},reference:{activity_id:'exact-child-turn'}};
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
