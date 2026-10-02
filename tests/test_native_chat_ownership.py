"""Native ownership outlives foreground/API/registry projection state."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder/scripts'))
import active_chat_registry
import run
from conversation_broker import BrokerStore
from conversation_reaper import ReaperStore


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'synthetic.db'
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / '.super-coder/schema.sql').read_text())
    for migration in sorted((ROOT / '.super-coder/migrations').glob('*.sql')):
        con.executescript(migration.read_text())
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'fixture')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(1,'FX','Fixture','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,worktree,creation_idempotency_key,creation_request_hash) VALUES('cv',1,1,'codex',?,'key','hash')", (str(tmp_path),))
    con.execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'cv')")
    con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('g','cv',1,1,'codex','{}','ready',1,1)")
    con.commit()
    yield path, con
    con.close()


@pytest.mark.parametrize('state', ['ready', 'lost', 'closed'])
@pytest.mark.parametrize('cleanup', [{}, {'unit_verified_exited': True, 'outcome': 'pending'},
    {'unit_verified_exited': True, 'outcome': 'complete', 'unresolved_definitions': ['definition']},
    {'unit_verified_exited': True, 'outcome': 'complete', 'unresolved_work': ['child']}])
def test_idle_native_owner_cannot_be_displaced_or_claimed_by_cli(database, state, cleanup):
    _, con = database
    con.execute('UPDATE conversation_runtime_generations SET state=?,cleanup_json=?', (state,json.dumps(cleanup)))
    con.commit()
    assert run.browser_conversation_active(con, 1)
    assert 1 in run.browser_conversation_shell_ids(con)
    for operation in (active_chat_registry.close_active, active_chat_registry.close_for_inactivity):
        with pytest.raises(active_chat_registry.ActiveChatBusy, match='pending cleanup'):
            operation(con, 1)
    assert active_chat_registry.get(con, 1).chat_id == 'cv'
    # A lost registry link and closed chat also cannot release native ownership.
    con.execute("UPDATE conversations SET state='closed',closed_at=datetime('now') WHERE conversation_id='cv'")
    con.commit()
    assert run.browser_conversation_active(con, 1)
    with pytest.raises(active_chat_registry.ActiveChatBusy, match='pending cleanup'):
        active_chat_registry.close_active(con, 1)


def test_verified_cleanup_releases_slot_and_mode_defaults_ephemeral(database):
    _, con = database
    assert con.execute('SELECT runtime_mode FROM conversations').fetchone()[0] == 'ephemeral'
    con.execute('UPDATE conversation_runtime_generations SET cleanup_json=?', (json.dumps({'unit_verified_exited':True,'outcome':'complete'}),))
    active_chat_registry.close_active(con, 1)
    assert not run.browser_conversation_active(con, 1)
    assert not run.browser_conversation_shell_ids(con)


def test_legacy_broker_and_reaper_do_not_take_native_turns(database):
    path, con = database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',state='queued'")
    mid = con.execute("INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash,state) VALUES('cv','user','1','prompt','queued','m','h','queued')").lastrowid
    con.execute("INSERT INTO conversation_outbox(conversation_id,message_id) VALUES('cv',?)",(mid,))
    con.commit()
    assert BrokerStore(str(path)).claim_next('legacy') is None
    assert con.execute('SELECT state FROM conversation_messages').fetchone()[0] == 'queued'
    con.execute("INSERT INTO conversation_runs(conversation_id,shell_id,trigger_message_id,state,lease_owner,lease_expires_at,heartbeat_at,started_at,process_pid,process_start_ticks,process_group_id) VALUES('cv',1,?,'running','fixture','2030-01-01','2030-01-01','2030-01-01',999999,1,999999)",(mid,))
    con.execute('DELETE FROM active_shell_chats')
    con.commit()
    assert ReaperStore(str(path)).candidates() == []
