"""Late journal observations retain independently verified terminal cleanup."""
import dataclasses
import json
import sys
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.super-coder/scripts'))
from conversation_native_chats import NativeChatsService, project_event
from conversation_runtime_contract import NativeReference, RuntimeEvent
from test_native_chat_ownership import database  # noqa: F401


@pytest.mark.parametrize('complete',[True,False])
def test_late_child_event_preserves_terminal_cleanup_but_unresolved_close_stays_pending(request,complete):
    path,con=request.getfixturevalue('database')
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'g','state':'closing'}),))
    con.execute("UPDATE conversation_runtime_generations SET close_intent=1,state='closing'");con.commit()
    service=NativeChatsService(path,path.parent,None)
    cleanup={'outcome':'complete' if complete else 'pending','unit_verified_exited':complete,
             'unresolved_work':[],'unresolved_definitions':[]}
    service._commit_cleanup('g','cv',1,1,cleanup)
    event=RuntimeEvent('activity.terminal',reference=NativeReference('root',thread_id='child',parent_thread_id='root',activity_id='child-turn'),data={'status':'completed'})
    project_event(con,'cv',5,dataclasses.asdict(event),primary={'root_id':'root','activity_id':'late-primary'})
    con.commit()
    runtime=json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])
    assert runtime['state']==('closed' if complete else 'closing')
    if complete:
        assert runtime['primary'] is None and runtime['cleanup']==cleanup
        assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
        assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0]=='closed'
    assert con.execute("SELECT COUNT(*) FROM conversation_events WHERE event_type='activity.terminal'").fetchone()[0]==1
