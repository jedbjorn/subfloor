"""Owned IPC/race and finite-scenario fixtures; no native inference or services."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_runtime_checks import (
    CleanupProof,
    CompatibilityChecker,
    EvidenceCache,
    Fingerprint,
)
from conversation_runtime_contract import (
    ExecutableBinding,
    NativeReference,
    NativeSubmission,
    ProcessIdentity,
    RuntimeContext,
    RuntimeContractError,
    RuntimeEvent,
    StartupConsent,
    payload_digest,
)
from conversation_runtime_native_probes import (
    ControllerProbeDriver,
    NativeProbeFactory,
    OwnedProbe,
)


class Store:
    def __init__(self):
        self.commands, self.receipts = {}, {}
    def attach(self, generation, owner, shell, consumer, *, lifetime):
        return {'consumer': consumer, 'fence': 1, 'expires': time.time()+lifetime}
    def intent(self, generation, owner, shell, lease, cid, kind, payload):
        if cid not in self.commands:
            key = 'request_id' if kind == 'submit' else 'control_id'
            command = dict(payload) | {key:cid, 'request_sequence':len(self.commands)+1}
            self.commands[cid] = command | {'payload_digest':payload_digest(command)}
        return self.commands[cid]
    def receipt(self, generation, owner, shell, cid, result):
        self.receipts[cid] = result
    def ingest(self, generation, owner, shell, lease, replay):
        return replay['sequence']

class Client:
    consumer = 'probe-only-consumer'
    def __init__(self, context):
        self.context, self.lease = context, {}
        self.events, self.calls, self.work = [], [], []
        self.primary, self.marker = None, None
        self.pids = {77:ProcessIdentity(77,100)}
        self.memory = True
        self.lost_submit = self.break_reply = self.break_child = False
        self.lock = threading.Lock()
    def event(self, kind, ref, request=None, **data):
        event = RuntimeEvent(kind,ref,request_id=request,data=data)
        self.events.append({'sequence':len(self.events)+1,'event':dataclasses.asdict(event)})
    def identity(self):
        return {'root_id':'root','process':{'pid':77,'start_ticks':100},'protocol':{'native_route':{'account_type':'chatgpt','model':self.context.model,'efforts':['high']},'memory_policy':{'generate_memories':False,'use_memories':False,'feature_enabled':False,'root_mode':'disabled'} if self.memory else {}}}
    def request(self, op, *, timeout, **fields):
        assert 0<timeout<=120
        self.calls.append((op,fields,timeout))
        with self.lock:
            if op in {'attach','ack'}: return {}
            if op=='open': return {'state':'ready','identity':self.identity()}
            if op=='subscribe': return {'events':[e for e in self.events if e['sequence']>fields['after']],'sequence':len(self.events),'primary':self.primary}
            if op=='snapshot':
                return {'identity':self.identity(),'primary':dataclasses.asdict(NativeReference('root','root',activity_id=self.primary)) if self.primary else None,'work':list(self.work),'freshness':'current','partial':False,'primary_state':'active' if self.primary else 'idle'}
            if op=='submit':
                command=fields['command']; request=command['request_id']; turn='turn-'+str(command['request_sequence'])
                ref=NativeReference('root','root',activity_id=turn);self.primary=turn
                self.event('activity.started',ref,request);self.event('activity.processed',ref,request)
                text=command['text'];marker=re.search(r'fixture_state with marker ([a-z0-9]+)',text)
                if marker:
                    self.marker=marker[1];output='READY '+self.marker
                    labels=re.findall(r'F89_(?:ROOT|CHILD)_[a-z0-9]+',text)
                    if labels:
                        self.pids[200]=ProcessIdentity(200,2000);output+=' '+labels[0]+'=200'
                        root_terminal=NativeReference('root','root',activity_id=turn,item_id='root-item',work_id='root-item',native_process_id='opaque-root')
                        self.work.append({'reference':dataclasses.asdict(root_terminal),'kind':'terminal','state':'running','provenance':'fixture','observed_at':time.time(),'freshness':'current'})
                        self.pids[201]=ProcessIdentity(201,2001);output+=' '+labels[-1]+'=201'
                        child='Spawn exactly one native child' in text
                        sibling=NativeReference('root','child' if child else 'root','root' if child else None,'child-turn' if child else turn,'sibling-item','sibling-item','opaque-sibling')
                        self.work.append({'reference':dataclasses.asdict(sibling),'kind':'terminal','state':'running','provenance':'fixture','observed_at':time.time(),'freshness':'current'})
                        if child:
                            child_ref=NativeReference('root','child','root','child-turn',work_id='child')
                            self.work.append({'reference':dataclasses.asdict(child_ref),'kind':'child','state':'active','provenance':'fixture','observed_at':time.time(),'freshness':'current'})
                    self.event('output.final',replace(ref,item_id='reply'),request,text=output)
                elif 'remembered nonce' in text:
                    self.event('output.final',replace(ref,item_id='reply'),request,text=self.marker or 'unknown')
                if '1000 numbered' not in text:
                    self.event('activity.terminal',ref,request,status='completed');self.primary=None
                if self.lost_submit: raise OSError('lost after owned native write')
                return {'state':'written','acknowledged':True,'native_activity_id':turn}
            if op=='control':
                command=fields['command'];ref=NativeReference(**command['target'])
                if command['action']=='stop_reply':
                    assert command['expected_activity_id']==self.primary
                    request=next(e['event']['request_id'] for e in reversed(self.events) if e['event']['kind']=='activity.started' and e['event']['reference']['activity_id']==self.primary)
                    self.event('activity.terminal',ref,request,status='interrupted');self.primary=None
                    if self.break_reply: self.pids.pop(200,None)
                elif ref.native_process_id:
                    self.work=[w for w in self.work if w['reference']['native_process_id']!=ref.native_process_id];self.pids.pop(200,None)
                else:
                    self.work=[w for w in self.work if w['reference']['thread_id']!='child'];self.pids.pop(201,None)
                    self.event('activity.terminal',ref,status='interrupted')
                    if self.break_child:self.pids.pop(77,None)
                self.event('control.acknowledged',ref,action=command['action'])
                self.events[-1]['event']['control_id']=command['control_id']
                self.event('control.outcome',ref,outcome='complete')
                self.events[-1]['event']['control_id']=command['control_id']
                return {'state':'written','acknowledged':True}
            if op=='close':
                self.pids.clear();self.work.clear()
                return {'outcome':'complete','unresolved_work':[],'unresolved_definitions':[]}
            raise AssertionError(op)

@pytest.fixture
def owned(tmp_path):
    isolation=tmp_path/'probe';worktree= isolation/'worktree';state=isolation/'state'
    worktree.mkdir(parents=True);state.mkdir();executable=tmp_path/'native';executable.write_text('not executed')
    context=RuntimeContext('owned-generation','synthetic-conversation',1,1,'codex',state,worktree,ExecutableBinding(executable,hashlib.sha256(executable.read_bytes()).hexdigest(),'fixture'),'driver-content-bound',hashlib.sha256(b'boot').hexdigest(),'policy','unrestricted',model='gpt-6.1-sol',effort='high',provider='openai',boot_content='boot',probe_capabilities=('submission','stop_reply','stop_work','stop_work_terminal','stop_work_child'))
    client,store=Client(context),Store()
    return OwnedProbe(context,client,store,1,1,'already-registered-unit',isolation,True,lambda marker,end:client.marker==marker,lambda pid:client.pids.get(pid))

def fingerprint(owned):
    return Fingerprint.capture(owned.context,settings_digest='settings',implementation_digest='actual-content')

def start(owned):
    driver=ControllerProbeDriver(owned)
    assert driver.start(owned.context,lambda _:None,deadline=time.monotonic()+3).state=='ready'
    return driver

def test_unknown_write_retains_intent_never_replayed_and_close_fences_later_input(owned):
    driver=start(owned);owned.client.lost_submit=True
    receipt=driver.submit(NativeSubmission('once',1,'digest','finite'),deadline=time.monotonic()+1)
    assert receipt.state=='unknown' and owned.store.receipts['once']['state']=='unknown'
    assert len([c for c in owned.client.calls if c[0]=='submit'])==1
    driver.cleanup(deadline=time.monotonic()+1)
    late=driver.submit(NativeSubmission('later',2,'digest','finite'),deadline=time.monotonic()+1)
    assert late.state=='not_written' and 'later' not in owned.store.commands


@pytest.mark.parametrize('owner_still_verified',[True,False])
def test_repeated_cleanup_retains_native_close_and_rechecks_owner_os_proof(owned,owner_still_verified):
    owner_calls=[]
    def cleanup(fp,end):
        owner_calls.append(fp.key)
        return CleanupProof(True if len(owner_calls)==1 else owner_still_verified,'complete')
    factory=NativeProbeFactory(lambda *args:owned,cleanup);fp=fingerprint(owned)
    session=factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
    session.driver.start(owned.context,lambda e:None,deadline=time.monotonic()+2)
    assert factory.cleanup(fp,deadline=time.monotonic()+1).complete
    original=owned.client.request
    def exited(op,**fields):
        if op=='close':raise OSError('owned controller already exited')
        return original(op,**fields)
    owned.client.request=exited
    second=factory.cleanup(fp,deadline=time.monotonic()+1)
    assert second.complete is owner_still_verified and second.native_outcome=='complete'
    assert len(owner_calls)==2 and sum(op=='close' for op,_,_ in owned.client.calls)==1


def test_concurrent_cleanup_uses_single_proved_native_close(owned):
    driver=start(owned);entered,release=threading.Event(),threading.Event()
    original=owned.client.request;close_calls=[];results=[]
    def blocked(op,**fields):
        if op=='close':
            close_calls.append(op);entered.set();assert release.wait(1)
        return original(op,**fields)
    owned.client.request=blocked
    first=threading.Thread(target=lambda:results.append(driver.cleanup(deadline=time.monotonic()+2)))
    second=threading.Thread(target=lambda:results.append(driver.cleanup(deadline=time.monotonic()+2)))
    first.start();assert entered.wait(1);second.start();release.set();first.join(2);second.join(2)
    assert len(close_calls)==1 and len(results)==2 and all(r.outcome=='complete' for r in results)


@pytest.mark.parametrize('missing',['memory','marker'])
def test_native_prompts_require_actual_memory_and_physical_marker_observation(owned,missing):
    if missing=='memory': owned.client.memory=False
    else:owned=replace(owned,observe_marker=None)
    driver=start(owned);result=NativeProbeFactory._exercise(driver,frozenset({'submission'}),time.monotonic()+1)
    assert result['submission'].grade=='inconclusive' and not any(c[0]=='submit' for c in owned.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('change',[
    None, {'evidence_level':'effective_telemetry'}, {'effective_telemetry':True},
    {'auto_memory_disabled':1}, {'auto_memory_enabled_setting':0},
    {'inherited_disable_flag':None}, {'generation_id':'other'},
    {'executable_sha256':'e'*64}, {'configuration_sha256':'e'*64},
    {'hook_sha256':None}, {'source_condition_sha256':'unknown'},
    {'observation_origin':'model narration'}, {'missing_policy':True},
])
def test_claude_probe_input_requires_qualified_captured_memory_inference(owned,change):
    context=replace(owned.context,harness='claude',provider='anthropic',model='actual-selected-model')
    owned.client.context=context
    policy={'evidence_level':'configuration_source_flag_inference',
        'observation_origin':'claude:documented-settings+captured-executable+owned-SessionStart-hook',
        'auto_memory_disabled':True,'effective_telemetry':False,'generation_id':context.generation_id,
        'executable_sha256':context.executable.sha256,'configuration_sha256':'b'*64,
        'hook_sha256':'c'*64,'source_condition_sha256':'d'*64,'inherited_disable_flag':'1',
        'auto_memory_enabled_setting':False}
    if change is not None:policy.update(change)
    def identity():
        return {'root_id':'root','process':{'pid':77,'start_ticks':100},'protocol':{
            'configuration_sha256':'b'*64,'native_route':{'account_type':'claude.ai',
            'model':context.model,'efforts':['high']},
            'memory_policy':{} if policy.get('missing_policy') else policy}}
    owned.client.identity=identity
    actual=replace(owned,context=context)
    driver=start(actual)
    result=NativeProbeFactory._exercise(driver,frozenset({'submission'}),time.monotonic()+2)
    assert result['submission'].grade==('compatible' if change is None else 'inconclusive')
    assert sum(op=='submit' for op,_,_ in owned.client.calls)==(2 if change is None else 0)
    assert ('memory_disabled' in result['submission'].coverage)==(change is None)
    if change is None:
        assert driver.identity.protocol['memory_policy']['effective_telemetry'] is False
    driver.cleanup(deadline=time.monotonic()+1)


def test_factory_witness_exports_only_correlated_semantic_counts_and_cleanup(owned):
    factory=NativeProbeFactory(lambda *args:owned,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(owned)
    assert factory.witness(fp)=={'waiting_stage':'allocation','first_close_observed':False}
    session=factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
    session.driver.start(owned.context,lambda e:None,deadline=time.monotonic()+2)
    session.exercise(session.driver,time.monotonic()+2)
    witness=factory.witness(fp)
    assert witness['waiting_stage']=='finished' and witness['first_root_processed']
    assert witness['first_root_terminal_counts']=={'completed':1,'failed':0,'interrupted':0,'other':0}
    assert witness['first_successful_reply'] and witness['second_final_nonce_matches']
    private=owned.client.marker
    assert private and private not in json.dumps(witness) and 'root' not in witness
    assert factory.cleanup(fp,deadline=time.monotonic()+1).complete
    witness=factory.witness(fp)
    assert witness['first_close_outcome']=='complete' and witness['native_complete_retained']
    assert witness['cleanup_fenced'] and witness['first_close_unresolved_work']==0


def test_factory_witness_does_not_promote_partial_foreign_or_unknown_terminal_data(owned):
    factory=NativeProbeFactory(lambda *args:owned,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(owned)
    session=factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
    driver=session.driver;driver.start(owned.context,lambda e:None,deadline=time.monotonic()+2)
    session.exercise(driver,time.monotonic()+2)
    scenario=driver._scenario
    ref=NativeReference('root','root',activity_id=scenario.first_activity)
    with driver._lock:
        driver.events.append(RuntimeEvent('activity.terminal',ref,request_id=scenario.first_request,
                            data={'status':'private arbitrary native data'}))
        driver.events.append(RuntimeEvent('activity.terminal',ref,request_id='foreign',data={'status':'failed'}))
        driver.events.append(RuntimeEvent('activity.terminal',ref,request_id=scenario.first_request,
                            partial=True,data={'status':'failed'}))
        driver.events.append(RuntimeEvent('activity.terminal',NativeReference('root','child','root',activity_id='child-turn'),
                            data={'status':'completed'}))
    witness=factory.witness(fp)
    assert witness['first_root_terminal_counts']=={'completed':1,'failed':0,'interrupted':0,'other':1}
    assert witness['child_terminal_counts']['completed']==1
    assert 'private arbitrary native data' not in json.dumps(witness)
    driver.cleanup(deadline=time.monotonic()+1)


def test_factory_witness_retains_the_predicate_that_timed_out(owned):
    original=owned.client.event
    def failed(kind,ref,request=None,**data):
        if kind=='activity.terminal':data['status']='failed'
        return original(kind,ref,request,**data)
    owned.client.event=failed
    factory=NativeProbeFactory(lambda *args:owned,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(owned);session=factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
    session.driver.start(owned.context,lambda e:None,deadline=time.monotonic()+2)
    assert session.exercise(session.driver,time.monotonic()+1)['submission'].grade=='inconclusive'
    witness=factory.witness(fp)
    assert witness['waiting_stage']=='first_reply' and witness['first_root_processed']
    assert witness['first_root_terminal_counts']['failed']==1 and not witness['first_successful_reply']
    session.driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('outcome',[None,{'private':'arbitrary native data'}])
def test_factory_witness_does_not_label_missing_native_close_outcome_observed(owned,outcome):
    original=owned.client.request
    def missing(op,**fields):
        if op=='close':return {'outcome':outcome}
        return original(op,**fields)
    owned.client.request=missing
    factory=NativeProbeFactory(lambda *args:owned,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(owned);session=factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
    session.driver.start(owned.context,lambda e:None,deadline=time.monotonic()+2)
    assert not factory.cleanup(fp,deadline=time.monotonic()+1).complete
    witness=factory.witness(fp)
    assert not witness['first_close_observed'] and not witness['native_complete_retained']
    assert witness['first_close_outcome']=='inconclusive' and 'arbitrary native data' not in json.dumps(witness)


@pytest.mark.parametrize('case',['missing_tag','unowned_candidate'])
def test_inventory_witness_distinguishes_missing_tag_from_failed_owned_identity_match(owned,case):
    original=owned.client.event
    def output(kind,ref,request=None,**data):
        if case=='missing_tag' and isinstance(data.get('text'),str):
            data['text']=re.sub(r'F89_ROOT_[a-z0-9]+=200','untagged root output',data['text'])
        return original(kind,ref,request,**data)
    owned.client.event=output
    actual=replace(owned,process_identity=lambda pid:None if pid==200 else owned.client.pids.get(pid))
    factory=NativeProbeFactory(lambda *args:actual,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(actual);session=factory.reserve(fp,frozenset({'submission','stop_work'}),deadline=time.monotonic()+3)
    session.driver.start(actual.context,lambda e:None,deadline=time.monotonic()+3)
    result=session.exercise(session.driver,time.monotonic()+1)
    assert result['submission'].grade=='compatible' and result['stop_work'].grade=='inconclusive'
    witness=factory.witness(fp)
    assert witness['waiting_stage']=='root_tagged_pid' and witness['first_successful_reply']
    assert witness['initial_snapshot_observed'] and witness['initial_snapshot_current']
    assert not witness['initial_snapshot_partial'] and witness['initial_root_terminal_current']
    assert witness['initial_child_ancestry_current'] and witness['initial_child_active_turn_present']
    assert witness['root_tagged_pid_candidates']==(0 if case=='missing_tag' else 1)
    assert witness['root_owned_pid_matches']==0 and witness['child_owned_pid_matches']==0
    assert actual.client.marker not in json.dumps(witness) and '200' not in json.dumps(witness)
    session.driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('change',[{'partial':True},{'freshness':'last_observed'}])
def test_inventory_witness_preserves_partial_and_stale_snapshot_failure(owned,change):
    original=owned.client.request
    def snapshot(op,**fields):
        result=original(op,**fields)
        return result|change if op=='snapshot' else result
    owned.client.request=snapshot
    factory=NativeProbeFactory(lambda *args:owned,lambda *args:CleanupProof(True,'complete'))
    fp=fingerprint(owned);session=factory.reserve(fp,frozenset({'submission','stop_work'}),deadline=time.monotonic()+3)
    session.driver.start(owned.context,lambda e:None,deadline=time.monotonic()+3)
    result=session.exercise(session.driver,time.monotonic()+2)
    assert result['submission'].grade==('compatible' if change.get('partial') else 'inconclusive')
    assert len([c for c in owned.client.calls if c[0]=='submit'])==2
    if not change.get('partial'):
        assert factory.witness(fp)['waiting_stage']=='nonce_recall'
        session.driver.cleanup(deadline=time.monotonic()+1)
        return
    witness=factory.witness(fp)
    assert witness['waiting_stage']=='initial_snapshot' and witness['initial_snapshot_observed']
    assert witness['initial_snapshot_partial']==change.get('partial',False)
    assert witness['initial_snapshot_current']==(change.get('freshness','current')=='current')
    assert not witness['initial_root_terminal_current'] and witness['root_tagged_pid_candidates']==0
    session.driver.cleanup(deadline=time.monotonic()+1)

def test_finite_scenario_earns_observed_targets_without_early_os_cleanup_pass(owned):
    driver=start(owned);result=NativeProbeFactory._exercise(driver,frozenset({'submission','stop_reply','stop_work'}),time.monotonic()+3)
    assert all(item.grade=='compatible' for item in result.values())
    assert {'target:terminal','target:child'}<=result['stop_work'].coverage
    assert 'owned_unit_cleanup' not in result['stop_work'].coverage
    assert 200 not in owned.client.pids and 201 not in owned.client.pids and 77 in owned.client.pids
    assert len([c for c in owned.client.calls if c[0]=='submit'])==3
    witness=driver._scenario.witness()
    assert witness['initial_root_terminal_current'] and witness['observed_child_terminal_current']
    assert witness['root_tagged_pid_candidates']==witness['root_owned_pid_matches']==1
    assert witness['child_tagged_pid_candidates']==witness['child_owned_pid_matches']==1
    driver.cleanup(deadline=time.monotonic()+1)

def test_terminal_only_grant_does_not_certify_child(owned):
    owned=replace(owned,context=replace(owned.context,probe_capabilities=('submission','stop_work','stop_work_terminal')))
    driver=start(owned);result=NativeProbeFactory._exercise(driver,frozenset({'stop_work'}),time.monotonic()+3)
    assert result['stop_work'].grade=='compatible' and 'target:terminal' in result['stop_work'].coverage
    assert 'target:child' not in result['stop_work'].coverage and 201 in owned.client.pids
    driver.cleanup(deadline=time.monotonic()+1)

def test_child_break_scopes_diagnostic_preserving_proved_terminal_and_submission(owned):
    owned.client.break_child=True;driver=start(owned)
    result=NativeProbeFactory._exercise(driver,frozenset({'submission','stop_work'}),time.monotonic()+3)
    item=result['stop_work'];assert item.grade=='compatible' and result['submission'].grade=='compatible'
    assert any(d.capability=='stop_work_child' and d.grade=='incompatible' for d in item.diagnostics)
    cache=EvidenceCache();cache.put(fingerprint(owned),replace(item,coverage=item.coverage|{'owned_unit_cleanup'}))
    grades=cache.admission(fingerprint(owned));assert grades['stop_work_terminal']=='compatible' and grades['stop_work_child']=='incompatible'
    driver.cleanup(deadline=time.monotonic()+1)

def test_automation_only_does_not_run_prompts_or_create_definitions(owned):
    driver=start(owned);result=NativeProbeFactory._exercise(driver,frozenset({'automation'}),time.monotonic()+1)
    assert result['automation'].grade=='inconclusive' and not any(c[0]=='submit' for c in owned.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)

def test_cleanup_fences_late_allocation_until_identity_resolved(owned):
    begun,release=threading.Event(),threading.Event();cleanup_calls,errors=[],[]
    def allocate(fp,caps,end):begun.set();release.wait(1);return owned
    def cleanup(fp,end):cleanup_calls.append(fp.key);return CleanupProof(True,'complete')
    factory=NativeProbeFactory(allocate,cleanup);fp=fingerprint(owned)
    def reserve():
        try:factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
        except RuntimeContractError as exc:errors.append(exc.code)
    thread=threading.Thread(target=reserve);thread.start();assert begun.wait(1)
    assert not factory.cleanup(fp,deadline=time.monotonic()+1).complete
    release.set();thread.join(1)
    assert errors==['PROBE_ALLOCATION_FENCED'] and not any(c[0]=='open' for c in owned.client.calls)
    assert factory.cleanup(fp,deadline=time.monotonic()+1).complete and cleanup_calls==[fp.key,fp.key]

def test_native_close_ack_cannot_replace_independent_os_exit(owned):
    factory=NativeProbeFactory(lambda fp,caps,end:owned,lambda fp,end:CleanupProof(False,'complete'));fp=fingerprint(owned)
    session=factory.reserve(fp,frozenset({'submission'}),deadline=time.monotonic()+2)
    session.driver.start(owned.context,lambda _:None,deadline=time.monotonic()+2)
    assert not factory.cleanup(fp,deadline=time.monotonic()+1).complete


def test_identity_observer_receives_captured_metadata_without_mutating_probe_guard(owned):
    observed = []
    def observe(identity):
        observed.append(identity)
        identity.protocol["memory_policy"]["root_mode"] = "changed-by-observer"
    driver = start(replace(owned, on_identity=observe))
    assert observed[0].root_id == "root"
    assert driver.identity.protocol["memory_policy"]["root_mode"] == "disabled"
    assert "env" not in observed[0].protocol
    driver.cleanup(deadline=time.monotonic()+1)


def test_expired_submission_cannot_create_intent_or_rpc(owned):
    driver = start(owned)
    with pytest.raises(TimeoutError):
        driver.submit(NativeSubmission("expired", 1, "digest", "finite"), deadline=time.monotonic()-1)
    assert "expired" not in owned.store.commands
    assert not any(c[0] == "submit" for c in owned.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)


def test_reader_tolerates_additions_but_records_consumed_text_break_and_keeps_draining(owned):
    driver = start(owned)
    with owned.client.lock:
        owned.client.events.extend([
            {"sequence": 1, "event": {"kind": "future.event", "harmless": True}},
            {"sequence": 2, "event": {"kind": "output.final", "reference": {"root_id": "root", "thread_id": "root", "activity_id": "turn", "item_id": "reply", "addition": True}, "data": {"text": 42}}},
            {"sequence": 3, "event": dataclasses.asdict(RuntimeEvent("output.final", NativeReference("root", "root", activity_id="turn", item_id="later"), data={"text": "later valid"}))}])
    until = time.monotonic()+1
    while time.monotonic() < until and not any(e.data.get("text") == "later valid" for e in driver.events):
        time.sleep(.01)
    assert any(e.data.get("text") == "later valid" for e in driver.events)
    assert any(d.capability == "submission" and d.grade == "incompatible" for d in driver.diagnostics)
    assert driver._reader.is_alive()
    driver.cleanup(deadline=time.monotonic()+1)


def test_wrong_scope_printed_pid_never_certifies_terminal_stop(owned):
    scope = replace(owned, process_identity=lambda pid: owned.client.pids.get(pid) if pid == 77 else None)
    driver = start(scope)
    result = NativeProbeFactory._exercise(driver, frozenset({"stop_work"}), time.monotonic()+.3)
    assert result["stop_work"].grade == "inconclusive"
    assert not any(c[0] == "control" for c in owned.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)


def test_root_interrupt_break_preserves_finished_submission_grade(owned):
    owned.client.break_reply = True
    driver = start(owned)
    result = NativeProbeFactory._exercise(driver, frozenset({"submission", "stop_reply"}), time.monotonic()+3)
    assert result["submission"].grade == "compatible"
    assert result["stop_reply"].grade == "incompatible"
    driver.cleanup(deadline=time.monotonic()+1)


class SetupClient(Client):
    def __init__(self, context):
        super().__init__(context)
        self.confirmed = threading.Event()
        self.setup = dataclasses.asdict(StartupConsent(context.generation_id, 'observed-phase',
            context.executable.sha256, context.driver_revision, 'a'*64, time.time()))
        self.status_generation = context.generation_id
        self.closed = False

    def request(self, op, *, timeout, **fields):
        if op in {'open', 'status'}:
            self.calls.append((op, fields, timeout))
            identity = self.identity()
            if op == 'open':
                return {'state': 'needs_consent', 'identity': identity, 'setup': self.setup}
            identity['protocol']['native_route'] = {'account_type': 'claude.ai',
                'model': self.context.model, 'efforts': [self.context.effort], 'catalogue_observed': False}
            return {'generation': self.status_generation, 'ready': self.confirmed.is_set(),
                'lost': False, 'identity': identity,
                'setup': None if self.confirmed.is_set() else self.setup,
                # Acknowledged confirmation alone must not admit inference.
                'setup_confirmation': {'setup_id': self.setup['setup_id'], 'control_id': 'external-owner'}}
        if op == 'close':
            self.closed = True
        return super().request(op, timeout=timeout, **fields)


def setup_owned(owned, **callbacks):
    context = replace(owned.context, harness='claude', model='selected-native', provider='anthropic')
    return replace(owned, context=context, client=SetupClient(context), **callbacks)


def test_setup_wait_drains_journal_without_input_and_reports_actual_ready_metadata(owned):
    updates, identities, result = [], [], []
    scope = setup_owned(owned, on_setup=updates.append, on_identity=identities.append)
    driver = ControllerProbeDriver(scope)
    thread = threading.Thread(target=lambda: result.append(driver.start(scope.context, lambda _: None,
        deadline=time.monotonic()+2)))
    thread.start()
    until = time.monotonic()+1
    while not updates and time.monotonic()<until: time.sleep(.01)
    assert updates and updates[0].generation_id == scope.context.generation_id
    scope.client.event('output.final', NativeReference('root', 'root', activity_id='setup-turn', item_id='reply'), text='observed')
    while not driver.events and time.monotonic()<until: time.sleep(.01)
    assert driver.events and driver._reader.is_alive()
    assert driver.submit(NativeSubmission('too-early', 1, 'digest', 'ordinary input'),
        deadline=time.monotonic()+1).state == 'not_written'
    assert not scope.store.commands and not any(c[0] == 'control' for c in scope.client.calls)
    scope.client.confirmed.set()  # Owner's independently scoped operator action.
    thread.join(1)
    assert result[0].state == 'ready' and updates[-1] is None
    assert identities[0].protocol['native_route']['account_type'] == 'claude.ai'
    assert len([c for c in scope.client.calls if c[0] == 'open']) == 1
    driver.cleanup(deadline=time.monotonic()+1)


def test_setup_confirmation_without_readiness_expires_and_withdraws_without_reopen(owned):
    updates = []
    scope = setup_owned(owned, on_setup=updates.append)
    driver = ControllerProbeDriver(scope)
    result = driver.start(scope.context, lambda _: None, deadline=time.monotonic()+.25)
    assert result.state == 'unknown' and updates[0] is not None and updates[-1] is None
    assert driver.start(scope.context, lambda _: None, deadline=time.monotonic()+1).state == 'unavailable'
    assert len([c for c in scope.client.calls if c[0] == 'open']) == 1
    assert not any(c[0] in {'submit', 'control'} for c in scope.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('field,value', [('generation_id', 'other'), ('executable_sha256', 'b'*64),
    ('driver_revision', 'other'), ('phase', 'trust_workspace')])
def test_setup_wrong_binding_or_unsupported_phase_never_published(owned, field, value):
    updates = []
    scope = setup_owned(owned, on_setup=updates.append)
    scope.client.setup[field] = value
    driver = ControllerProbeDriver(scope)
    with pytest.raises(RuntimeContractError):
        driver.start(scope.context, lambda _: None, deadline=time.monotonic()+1)
    assert not updates and not any(c[0] in {'submit', 'control'} for c in scope.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)


def test_close_withdraws_setup_wakes_startup_and_blocks_late_ready(owned):
    updates, identities, result = [], [], []
    scope = setup_owned(owned, on_setup=updates.append, on_identity=identities.append)
    driver = ControllerProbeDriver(scope)
    thread = threading.Thread(target=lambda: result.append(driver.start(scope.context, lambda _: None,
        deadline=time.monotonic()+2)))
    thread.start()
    until = time.monotonic()+1
    while not updates and time.monotonic()<until: time.sleep(.01)
    driver.cleanup(deadline=time.monotonic()+1)
    scope.client.confirmed.set()
    thread.join(1)
    assert result[0].state == 'unknown' and updates[-1] is None and not identities
    assert scope.client.closed and not any(c[0] in {'submit', 'control'} for c in scope.client.calls)


def test_close_before_open_prevents_native_start_and_intent(owned):
    driver = ControllerProbeDriver(owned)
    driver.cleanup(deadline=time.monotonic()+1)
    assert driver.start(owned.context, lambda _: None, deadline=time.monotonic()+1).state == 'unavailable'
    assert not any(c[0] == 'open' for c in owned.client.calls)


def test_wrong_generation_status_cannot_publish_ready_identity(owned):
    identities = []
    scope = setup_owned(owned, on_identity=identities.append)
    scope.client.status_generation = 'unrelated-generation'
    scope.client.confirmed.set()
    driver = ControllerProbeDriver(scope)
    with pytest.raises(RuntimeContractError, match='private status generation changed'):
        driver.start(scope.context, lambda _: None, deadline=time.monotonic()+1)
    assert not identities
    driver.cleanup(deadline=time.monotonic()+1)


def test_close_during_open_fences_late_return_without_setup_or_ready_publication(owned):
    updates, identities, result = [], [], []
    scope = setup_owned(owned, on_setup=updates.append, on_identity=identities.append)
    entered, release = threading.Event(), threading.Event()
    request = scope.client.request
    def blocked_open(op, *, timeout, **fields):
        if op == 'open':
            entered.set()
            assert release.wait(1)
        return request(op, timeout=timeout, **fields)
    scope.client.request = blocked_open
    driver = ControllerProbeDriver(scope)
    thread = threading.Thread(target=lambda: result.append(driver.start(scope.context, lambda _: None,
        deadline=time.monotonic()+2)))
    thread.start()
    assert entered.wait(1)
    driver.cleanup(deadline=time.monotonic()+1)
    release.set()
    thread.join(1)
    assert result[0].state == 'unknown' and not updates and not identities
    assert driver._reader is None


def test_claude_invented_disabled_flag_cannot_admit_inference_before_evidence_choice(owned):
    scope = setup_owned(owned)
    scope.client.confirmed.set()
    scope.client.identity = lambda: {'root_id': 'root', 'process': {'pid': 77, 'start_ticks': 100},
        'protocol': {'memory_policy': {'disabled': True}}}
    driver = start(scope)
    result = NativeProbeFactory._exercise(driver, frozenset({'submission'}), time.monotonic()+1)
    assert result['submission'].grade == 'inconclusive'
    assert not any(c[0] == 'submit' for c in scope.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('missing', ['processing', 'reply', 'failed', 'wrong_nonce', 'partial_reply', 'wrong_activity', 'tool_reply'])
def test_submission_requires_successful_correlated_processing_and_nonce_recall(owned, missing):
    event = owned.client.event
    def observed(kind, ref, request=None, **data):
        if missing == 'processing' and kind == 'activity.processed': return
        if missing == 'reply' and kind.startswith('output.'): return
        if missing == 'failed' and kind == 'activity.terminal': data['status'] = 'failed'
        if missing == 'wrong_nonce' and kind == 'output.final' and not data.get('text', '').startswith('READY'):
            data['text'] = 'not the prior nonce'
        if missing == 'wrong_activity' and kind == 'output.final': ref = replace(ref, activity_id='other-turn')
        if missing == 'tool_reply' and kind == 'output.final': ref = replace(ref, work_id='native-command-item')
        event(kind, ref, request, **data)
        if missing == 'partial_reply' and kind == 'output.final': owned.client.events[-1]['event']['partial'] = True
    owned.client.event = observed
    driver = start(owned)
    result = NativeProbeFactory._exercise(driver, frozenset({'submission'}), time.monotonic()+.6)
    assert result['submission'].grade == 'inconclusive' and not result['submission'].coverage
    driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('break_root', ['os_exit', 'native_identity'])
def test_terminal_stop_cannot_certify_after_losing_captured_root(owned, break_root):
    scope = replace(owned, context=replace(owned.context,
        probe_capabilities=('submission', 'stop_work', 'stop_work_terminal')))
    request = scope.client.request
    stopped = False
    def collateral(op, *, timeout, **fields):
        nonlocal stopped
        result = request(op, timeout=timeout, **fields)
        if op == 'control' and fields['command']['action'] == 'stop_work':
            stopped = True
            if break_root == 'os_exit': scope.client.pids.pop(77, None)
        if op == 'snapshot' and stopped and break_root == 'native_identity':
            result['identity']['root_id'] = 'replacement-root'
        return result
    scope.client.request = collateral
    driver = start(scope)
    result = NativeProbeFactory._exercise(driver, frozenset({'stop_work'}), time.monotonic()+2)
    evidence = result['stop_work']
    assert 'target:terminal' not in evidence.coverage
    assert any(d.capability == 'stop_work_terminal' and d.grade == 'incompatible' for d in evidence.diagnostics)
    assert 201 in scope.client.pids
    driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('missing', ['absent', 'pending', 'partial', 'wrong_control'])
def test_physical_exit_and_snapshot_absence_require_matched_native_control_success(owned, missing):
    scope = replace(owned, context=replace(owned.context,
        probe_capabilities=('submission', 'stop_work', 'stop_work_terminal')))
    request = scope.client.request
    def unconfirmed(op, *, timeout, **fields):
        result = request(op, timeout=timeout, **fields)
        if op == 'control' and fields['command']['action'] == 'stop_work':
            if missing == 'absent': scope.client.events = [e for e in scope.client.events if not e['event']['kind'].startswith('control.')]
            else:
                event = scope.client.events[-1]['event']
                if missing == 'pending': event['data']['outcome'] = 'pending'
                if missing == 'partial': event['partial'] = True
                if missing == 'wrong_control': event['control_id'] = 'unrelated-control'
            return {'state': 'written', 'acknowledged': True, 'detail': 'unconfirmed opaque native result'}
        return result
    scope.client.request = unconfirmed
    driver = start(scope)
    result = NativeProbeFactory._exercise(driver, frozenset({'stop_work'}), time.monotonic()+.6)
    assert result['stop_work'].grade == 'inconclusive' and 'target:terminal' not in result['stop_work'].coverage
    assert 200 not in scope.client.pids and 77 in scope.client.pids and 201 in scope.client.pids
    driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('outcome', ['complete', 'inconclusive'])
def test_composite_unresolved_native_cleanup_retains_factory_allocation(owned, outcome):
    request = owned.client.request
    def unresolved(op, *, timeout, **fields):
        result = request(op, timeout=timeout, **fields)
        if op == 'close': return {'outcome': outcome, 'unresolved_definitions': ['owned-definition']}
        return result
    owned.client.request = unresolved
    factory = NativeProbeFactory(lambda fp, caps, end: owned, lambda fp, end: CleanupProof(True, 'complete'))
    fp = fingerprint(owned)
    session = factory.reserve(fp, frozenset({'submission'}), deadline=time.monotonic()+2)
    session.driver.start(owned.context, lambda _: None, deadline=time.monotonic()+2)
    proof = factory.cleanup(fp, deadline=time.monotonic()+1)
    assert not proof.complete and proof.unresolved_definitions == ('owned-definition',)
    with pytest.raises(RuntimeContractError) as error:
        factory.reserve(fp, frozenset({'submission'}), deadline=time.monotonic()+2)
    assert error.value.code == 'PROBE_ALLOCATION_RETAINED'


@pytest.mark.parametrize('freshness', ['stale', 'unknown'])
def test_current_snapshot_does_not_upgrade_work_witness_freshness(owned, freshness):
    original = owned.client.request
    def request(op, **fields):
        value = original(op, **fields)
        if op == 'snapshot':
            for row in value['work']: row['freshness'] = freshness
        return value
    owned.client.request = request
    factory = NativeProbeFactory(lambda *args: owned, lambda *args: CleanupProof(True, 'complete'))
    fp = fingerprint(owned)
    session = factory.reserve(fp, frozenset({'submission', 'stop_work'}), deadline=time.monotonic()+3)
    session.driver.start(owned.context, lambda _: None, deadline=time.monotonic()+3)
    try:
        session.exercise(session.driver, time.monotonic()+2)
        witness = factory.witness(fp)
        assert witness['initial_snapshot_current']
        assert not witness['initial_root_terminal_current']
        assert not witness['initial_child_ancestry_current']
        assert not witness['observed_child_terminal_current']
    finally:
        session.driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('gap', ['missing_pid_tag', 'unowned_pid', 'partial_work_snapshot'])
@pytest.mark.parametrize('cleanup', ['complete', 'native_unknown', 'os_unknown'])
def test_checker_submission_proof_survives_optional_target_gap_only_after_full_cleanup(owned, gap, cleanup):
    event = owned.client.event
    def output(kind, ref, request=None, **data):
        if gap == 'missing_pid_tag' and isinstance(data.get('text'), str):
            data['text'] = re.sub(r'F89_ROOT_[a-z0-9]+=200', 'untagged', data['text'])
        return event(kind, ref, request, **data)
    owned.client.event = output
    original = owned.client.request
    def request(op, **fields):
        result = original(op, **fields)
        if op == 'snapshot' and gap == 'partial_work_snapshot':
            result['partial'] = True
        if op == 'close' and cleanup == 'native_unknown':
            result['outcome'] = 'inconclusive'
        return result
    owned.client.request = request
    actual = replace(owned, process_identity=lambda pid:
                     None if gap == 'unowned_pid' and pid == 200 else owned.client.pids.get(pid))
    factory = NativeProbeFactory(lambda *args: actual, lambda *args:
                                 CleanupProof(cleanup != 'os_unknown', 'complete'))
    fp = fingerprint(actual)
    checker = CompatibilityChecker()
    result = checker.request(fp, observed_interface={'fixed': True},
        requirements={'submission': {'fixed': True}, 'stop_work': {'fixed': True}},
        factory=factory, seconds=2.4).result(timeout=4)
    assert len([c for c in actual.client.calls if c[0] == 'submit']) == 2
    assert not any(c[0] == 'control' for c in actual.client.calls)
    assert result.evidence['submission'].grade == ('compatible' if cleanup == 'complete' else 'inconclusive')
    assert result.evidence['stop_work'].grade == 'inconclusive'
    assert ('owned_unit_cleanup' in result.evidence['submission'].coverage) == (cleanup == 'complete')
    assert checker.cache.admission(fp)['submission'] == ('compatible' if cleanup == 'complete' else 'inconclusive')
    assert not result.evidence['stop_work'].coverage


@pytest.mark.parametrize('boundary', ['activity', 'item', 'digest', 'contiguous', 'replay', 'conflicting', 'missing_part'])
def test_pid_candidates_preserve_observed_text_record_and_part_boundaries(owned, boundary):
    from conversation_runtime_native_probes import _Scenarios
    driver = start(owned)
    ref = NativeReference('root', 'root', activity_id='turn')
    owned.client.pids[200] = ProcessIdentity(200, 2000)
    other = replace(ref, activity_id='other') if boundary == 'activity' else replace(ref, item_id='other') if boundary == 'item' else ref
    rows = [RuntimeEvent('output.final', ref, data={'text': 'TAG=200', 'text_digest': 'a'*64, 'part': 0, 'last': True}),
            RuntimeEvent('output.final', other, data={'text': '123', 'text_digest': 'b'*64, 'part': 0, 'last': True})]
    if boundary in {'contiguous', 'replay', 'conflicting', 'missing_part'}:
        rows = [RuntimeEvent('output.final', ref, data={'text': text, 'text_digest': 'a'*64, 'part': part, 'last': part == 1})
                for part, text in enumerate(['TAG=2', '00'])]
        if boundary == 'replay': rows.insert(1, rows[0])
        if boundary == 'conflicting': rows.append(replace(rows[0], data=rows[0].data | {'text': 'TAG=3'}))
        if boundary == 'missing_part': rows = [replace(rows[1], data=rows[1].data | {'text': 'TAG=200'})]
    with driver._lock: driver.events = rows
    scenario = _Scenarios(driver, frozenset({'submission'}), time.monotonic()+1)
    try:
        assert scenario._pid('TAG') == (None if boundary in {'conflicting', 'missing_part'} else ProcessIdentity(200, 2000))
    finally:
        driver.cleanup(deadline=time.monotonic()+1)
