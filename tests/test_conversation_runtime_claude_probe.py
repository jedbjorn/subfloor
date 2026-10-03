"""Actual Claude normalization through bounded controller-proxy/check/cache tests.

Native transport, account and OS callbacks are synthetic. No inference or units.
"""
# Imported pytest fixtures intentionally share names with arguments.
# ruff: noqa: F811
from __future__ import annotations

import dataclasses
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'.super-coder/scripts'), str(ROOT/'.super-coder/api')]
from conversation_adapters.claude_runtime import CHANNEL, ClaudeRuntimeDriver
from conversation_runtime_checks import CleanupProof, CompatibilityChecker
from conversation_runtime_claude_probe import ClaudeScenarios
from conversation_runtime_contract import (
    NativeControl,
    NativeReference,
    NativeSubmission,
    ProcessIdentity,
    RuntimeContractError,
    RuntimeIdentity,
)
from conversation_runtime_native_probes import NativeProbeFactory
from test_conversation_runtime_native_probes import (  # noqa: F401
    fingerprint,
    owned,
    start,
)
from test_native_check_workflow import database, workflow  # noqa: F401


def claude_owned(owned, mutation='positive'):
    context = replace(owned.context, harness='claude', provider='anthropic', model='claude-sonnet-5-5',
        probe_capabilities=('submission','stop_work','stop_work_terminal'))
    owned.client.context = context
    policy = {'evidence_level':'configuration_source_flag_inference',
        'observation_origin':'claude:documented-settings+captured-executable+owned-SessionStart-hook',
        'auto_memory_disabled':True, 'effective_telemetry':False, 'generation_id':context.generation_id,
        'executable_sha256':context.executable.sha256, 'configuration_sha256':'b'*64,
        'hook_sha256':'c'*64, 'source_condition_sha256':'d'*64, 'inherited_disable_flag':'1',
        'auto_memory_enabled_setting':False}
    owned.client.identity = lambda: {'root_id':'root', 'process':{'pid':77,'start_ticks':100}, 'protocol':{
        'configuration_sha256':'b'*64, 'memory_policy':policy,
        'native_route':{'account_type':'claude.ai','model':context.model,'efforts':['high']}}}
    parser = ClaudeRuntimeDriver()
    parser._context = context
    parser._identity = RuntimeIdentity('root', process=ProcessIdentity(77,100))
    parser._ready, parser._primary_state = True, 'idle'
    parser._emit = lambda e: owned.client.events.append({'sequence':len(owned.client.events)+1, 'event':dataclasses.asdict(e)})
    original = owned.client.request
    def hook(name, **fields):
        parser.asset({'kind':'hook','event':{'hook_event_name':name, 'session_id':'root',
            'cwd':str(context.worktree), **fields}}, peer=ProcessIdentity(999999999,1), deadline=time.monotonic()+1)
    def prompt(turn, attrs):
        text = '<channel '+ ' '.join(f'{k}="{v}"' for k,v in {'source':CHANNEL,
            'generation_id':context.generation_id,'conversation_id':context.conversation_id, **attrs}.items())+'></channel>'
        hook('UserPromptSubmit', prompt_id=turn, prompt=text)
    def request(op, *, timeout, **fields):
        if op not in {'submit','control','snapshot'}: return original(op,timeout=timeout,**fields)
        owned.client.calls.append((op,fields,timeout))
        with owned.client.lock:
            if op=='snapshot':
                result=dataclasses.asdict(parser.inventory(deadline=time.monotonic()+timeout))
                result['identity']=owned.client.identity()
                return result
            cmd=fields['command'];turn='turn-'+str(cmd['request_sequence'])
            with parser._condition:
                if op=='submit':
                    receipt=parser.submit(NativeSubmission(**cmd), deadline=time.monotonic()+timeout)
                    prompt(turn, {'request_id':cmd['request_id'],'payload_digest':cmd['payload_digest']})
                    marker=re.search(r'fixture_state with marker ([a-z0-9]+)',cmd['text'])
                    if marker:
                        owned.client.marker=marker[1]
                        if 'run_in_background=true' in cmd['text']:
                            for wid,label,seconds in [('target','TARGET',105),('sibling','SIBLING',110)]:
                                hook('PostToolUse',prompt_id=turn, tool_name='Bash',tool_use_id='bash-'+wid,
                                    tool_input={'command':f'sleep {seconds} # F89_{label}_'+marker[1]},
                                    tool_response={'backgroundTaskId':wid})
                        output='READY '+marker[1]
                    else:output=owned.client.marker or 'unknown'
                    parser._output(turn, output, 'claude:owned-transcript-text','reply-'+turn)
                    rows=[{'id':wid,'type':'shell','status':'running'} for wid in parser._work]
                    hook('Stop',prompt_id=turn,background_tasks=rows,session_crons=[])
                    parser._terminal(turn,'completed','claude:owned-transcript-turn_duration')
                    return dataclasses.asdict(receipt)
                target = cmd['target']
                cmd=dict(cmd)|{'target':NativeReference(**target)}
                receipt=parser.control(NativeControl(**cmd),deadline=time.monotonic()+timeout)
                if receipt.state not in {'written','unknown'}:return dataclasses.asdict(receipt)
                prompt(turn,{'control_id':cmd['control_id']})
                response={'task_id':'target','task_type':'local_bash'}
                if mutation=='message_only': response={'message':'stopped'}
                if mutation=='malformed': response['status']={'raw':'private'}
                if mutation=='error': response['error']='failed'
                if mutation=='foreign_result': response['task_id']='foreign'
                hook('PostToolUse',prompt_id=turn,tool_name='TaskStop',tool_use_id='native-stop',
                    tool_input={'task_id':'target'},tool_response=response)
                rows=[{'id':'sibling','type':'shell','status':'running'}]
                if mutation=='partial':rows.append({'id':'malformed','status':{}})
                if mutation=='sibling_absent':rows=[]
                if mutation=='completed_race':parser._work_terminal('target','completed','claude:native-completion')
                hook('SubagentStop' if mutation=='nonstop' else 'Stop',
                    prompt_id='foreign' if mutation=='prompt' else turn,background_tasks=rows,session_crons=[])
                parser._terminal(turn,'completed','claude:owned-transcript-turn_duration')
                return dataclasses.asdict(receipt)
    owned.client.request=request
    return replace(owned,context=context), parser


@pytest.mark.parametrize('mutation', ['positive','message_only','malformed','error','foreign_result','partial',
    'sibling_absent','completed_race','nonstop','prompt'])
def test_actual_claude_hook_scenario_retains_native_semantic_and_sibling_scope(owned, mutation):
    actual, parser=claude_owned(owned,mutation)
    driver=start(actual)
    try:
        measured=NativeProbeFactory._exercise(driver,frozenset({'submission','stop_work','stop_reply'}),time.monotonic()+.7)
        assert measured['submission'].grade=='compatible'
        assert measured['stop_work'].grade==('compatible' if mutation=='positive' else 'inconclusive')
        assert measured['stop_reply'].grade=='inconclusive'
        assert 'terminal_identity' not in measured['stop_work'].coverage
        assert sum(op=='submit' for op,_,_ in actual.client.calls)==2
        assert sum(op=='control' for op,_,_ in actual.client.calls)==1
        assert parser.inventory(deadline=time.monotonic()+1).freshness=='last_observed'
        if mutation=='positive':
            events=driver.events
            outcome=next(e for e in events if e.kind=='control.outcome')
            assert outcome.data['os_verified'] is False
            assert outcome.reference.activity_id and outcome.data['snapshot_complete'] is True
    finally:driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('cleanup_ok',[True,False])
def test_checker_requires_native_and_fresh_owner_os_cleanup_before_claude_variant(owned,cleanup_ok):
    actual,_=claude_owned(owned)
    factory=NativeProbeFactory(lambda *args:actual,lambda *args:CleanupProof(cleanup_ok,'complete'))
    fp=fingerprint(actual);checker=CompatibilityChecker()
    checker.request(fp,observed_interface={'fixed':True},requirements={cap:{'fixed':True} for cap in ('submission','stop_work')},
        factory=factory,seconds=2).result(timeout=4)
    grades=checker.cache.admission(fp)
    assert (grades['submission']=='compatible')==cleanup_ok
    assert (grades['stop_work_terminal']=='compatible')==cleanup_ok
    assert grades.get('stop_work_child','inconclusive')!='compatible'


@pytest.mark.parametrize('mutation',['positive','foreign_root','stale','partial','wrong_prompt','nonstop',
    'absent_tool_id','message_only','future_snapshot','duplicate_ids','foreign_process'])
def test_taskstop_predicate_consumes_actual_normalized_order_and_fields(owned,mutation):
    actual,_=claude_owned(owned)
    driver=start(actual)
    try:
        scenario=ClaudeScenarios(driver,frozenset({'submission','stop_work'}),time.monotonic()+1)
        # Run the actual source normalizer once, then challenge only consumed
        # fields on its immutable output, rather than inventing a positive tuple.
        assert scenario.run()['stop_work'].grade=='compatible'
        events=list(driver.events)
        ack=next(e for e in events if e.kind=='control.acknowledged')
        control=ack.control_id
        for index,event in enumerate(events):
            if mutation=='foreign_root' and event.control_id==control:
                events[index]=replace(event,reference=replace(event.reference,root_id='foreign'))
            elif mutation=='foreign_process' and event.control_id==control:
                events[index]=replace(event,reference=replace(event.reference,native_process_id='foreign'))
            elif mutation in {'stale','partial'} and event.control_id==control:
                events[index]=replace(event,**({'freshness':'stale'} if mutation=='stale' else {'partial':True}))
            elif event.kind=='snapshot.observed' and event.reference.activity_id==ack.reference.activity_id:
                if mutation=='wrong_prompt':events[index]=replace(event,reference=replace(event.reference,activity_id='other'))
                if mutation=='nonstop':events[index]=replace(event,provenance='claude:SubagentStop-snapshot')
                if mutation=='future_snapshot':events[index]=replace(event,data=dict(event.data)|{'observed_at':-1})
                if mutation=='duplicate_ids':events[index]=replace(event,data=dict(event.data)|{'work_ids':['sibling','sibling']})
            elif event.kind=='control.acknowledged':
                if mutation=='absent_tool_id':events[index]=replace(event,data={k:v for k,v in event.data.items() if k!='tool_use_id'})
                if mutation=='message_only':events[index]=replace(event,data=dict(event.data)|{'native_result':{'message':'stopped'}})
        with driver._lock:driver.events=events
        assert scenario._native_stopped(control,'target','sibling',0)==(mutation=='positive')
    finally:driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('bad', ['positive','partial','stale','foreign_root','missing_variant'])
def test_actual_controller_store_and_http_only_admit_scoped_last_observed_claude_work(owned,tmp_path,monkeypatch,bad):
    import conversation_native_chats
    from conversation_native_chats import NativeChatsService, project_event
    from conversation_runtime_controller import Controller
    from test_conversation_api import ConversationApiCase
    actual,parser=claude_owned(owned)
    driver=start(actual)
    measured=NativeProbeFactory._exercise(driver,frozenset({'submission','stop_work'}),time.monotonic()+1)
    assert measured['stop_work'].grade=='compatible'
    observed=[e for e in driver.events if e.kind=='work.observed' and e.provenance=='claude:Stop-snapshot'
        and e.reference.work_id=='sibling'][-1]
    if bad=='partial':observed=replace(observed,partial=True,data=dict(observed.data)|{'snapshot_complete':False})
    if bad=='stale':observed=replace(observed,freshness='stale')
    if bad=='foreign_root':observed=replace(observed,reference=replace(observed.reference,root_id='foreign'))
    case=ConversationApiCase();case.setUp()
    service=None;controller=None
    try:
        cid='cv_'+'7'*32
        with case.connect() as con:
            con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES(?,1,1,'claude','anthropic','claude-sonnet-5-5','high',?,'native-key','native-hash','native_experiment',?)",
                (cid,str(actual.context.worktree),json.dumps({'generation_id':'g','state':'ready','root_id':'root',
                'capabilities':{'submission':'compatible','stop_work':'compatible','stop_work_terminal':'inconclusive' if bad=='missing_variant' else 'compatible'}})))
            con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('g',?,1,1,'claude','{}','ready',1,1)",(cid,))
        (tmp_path/'journal').mkdir(mode=0o700)
        controller=Controller('g',tmp_path/'journal',parser)
        controller.identity=RuntimeIdentity('root')
        controller.context=replace(actual.context,generation_id='g',conversation_id=cid)
        service=NativeChatsService(case.db_path,case.root,None)
        writes=[]
        client=SimpleNamespace(request=lambda op,**fields:writes.append(fields) or {'state':'unknown','acknowledged':True})
        monkeypatch.setattr(service,'attach',lambda generation:(client,1,1))
        monkeypatch.setattr(conversation_native_chats,'_SERVICE',service)
        lease=service.store.attach('g',1,1,service.consumer)
        if bad=='foreign_root':
            with pytest.raises(RuntimeContractError,match='outside captured'):
                controller.emit(observed)
            replay=controller.journal.replay(0)
            assert not any(e['event']['kind']=='work.observed' for e in replay['events'])
            assert not writes
            return
        controller.emit(observed)
        replay=controller.journal.replay(0)
        service.store.ingest('g',1,1,lease,replay,project=project_event)
        with case.connect() as con:
            row=con.execute('SELECT work_key,projection_json FROM conversation_runtime_work').fetchone()
            stored=json.loads(row['projection_json'])
            assert stored['reference']['activity_id']==observed.reference.activity_id
            assert stored['freshness']==observed.freshness
            cv=con.execute('SELECT version FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
        body={'version':cv[0],'generation_id':'g','action':'stop_work','work_key':row['work_key']}
        status,_,response=case.request('POST',f'/api/conversations/{cid}/runtime-controls',body=body,key='one-request')
        assert status==(202 if bad=='positive' else 409),response
        assert len(writes)==(1 if bad=='positive' else 0)
        if bad=='positive':
            status,_,duplicate=case.request('POST',f'/api/conversations/{cid}/runtime-controls',body=body,key='one-request')
            assert status==202 and duplicate['duplicate'] and len(writes)==1
            assert response['state']=='unknown'
    finally:
        driver.cleanup(deadline=time.monotonic()+1)
        if service:service.shutdown()
        if controller:controller.journal.db.close()
        case.doCleanups()


@pytest.mark.parametrize('cleanup_ok',[True,False])
def test_claude_cache_variant_reaches_normal_check_projection_only_after_composite_cleanup(owned,request,cleanup_ok):
    import conversation_native_checks as checks
    actual,_=claude_owned(owned)
    factory=NativeProbeFactory(lambda *args:actual,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(actual);checker=CompatibilityChecker()
    checker.request(fp,observed_interface={'fixed':True},requirements={cap:{'fixed':True} for cap in ('submission','stop_work')},
        factory=factory,seconds=2).result(timeout=4)
    grades=checker.cache.admission(fp)
    value,operation,report,_,_=request.getfixturevalue('workflow')
    operation.cache=checker.cache
    operation.seat.candidate_fingerprint=lambda **_:fp
    operation.begin=lambda *,selection,on_candidate:on_candidate(fp.key)
    first=value.create(1,'claude-once',checks.CLAUDE_SELECTION)
    report.update(state='complete',fingerprint=fp.key,grades=grades,
        cleanup={'native_outcome':'complete','unit_verified_exited':cleanup_ok,'unresolved_work':[],'unresolved_definitions':[]},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    public=value.get(1,check_id=first['check_id'])
    assert public['admissible']==cleanup_ok
    # Previously cleanup-bound cache diagnostics remain visible; this new
    # check's missing OS proof still forbids ordinary admission.
    assert public['grades'].get('stop_work_terminal')=='compatible'
    assert public['grades'].get('stop_work_child','inconclusive')!='compatible'


@pytest.mark.parametrize('mutation',['stale','unknown','root','process','active','partial_background'])
def test_root_retention_does_not_promote_lost_snapshot_or_bg_partial_to_root_uncertainty(owned,mutation):
    actual,_=claude_owned(owned)
    driver=start(actual)
    try:
        scenario=ClaudeScenarios(driver,frozenset({'submission'}),time.monotonic()+1)
        snapshot=driver.inventory(deadline=time.monotonic()+1)
        if mutation in {'stale','unknown'}:snapshot=replace(snapshot,freshness=mutation)
        if mutation=='root':snapshot=replace(snapshot,identity=replace(snapshot.identity,root_id='foreign'))
        if mutation=='process':snapshot=replace(snapshot,identity=replace(snapshot.identity,process=ProcessIdentity(78,1)))
        if mutation=='active':snapshot=replace(snapshot,primary_state='active')
        if mutation=='partial_background':snapshot=replace(snapshot,partial=True)
        assert scenario._retained(driver.identity,snapshot)==(mutation=='partial_background')
    finally:driver.cleanup(deadline=time.monotonic()+1)


def test_unsupported_claude_probe_caps_do_not_issue_extra_native_turns(owned):
    actual,_=claude_owned(owned)
    driver=start(actual)
    try:
        measured=NativeProbeFactory._exercise(driver,frozenset({'stop_reply','automation','history_resume'}),time.monotonic()+1)
        assert all(value.grade=='inconclusive' and not value.coverage for value in measured.values())
        assert not [op for op,_,_ in actual.client.calls if op in {'submit','control'}]
    finally:driver.cleanup(deadline=time.monotonic()+1)
