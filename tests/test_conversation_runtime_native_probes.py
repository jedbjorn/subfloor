"""Owned IPC/race and finite-scenario fixtures; no native inference or services."""
from __future__ import annotations

import dataclasses
import hashlib
import re
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_runtime_checks import CleanupProof, EvidenceCache, Fingerprint
from conversation_runtime_contract import (
    ExecutableBinding,
    NativeReference,
    NativeSubmission,
    ProcessIdentity,
    RuntimeContext,
    RuntimeContractError,
    RuntimeEvent,
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
                        self.work.append({'reference':dataclasses.asdict(root_terminal),'kind':'terminal','state':'running','provenance':'fixture','observed_at':time.time()})
                        self.pids[201]=ProcessIdentity(201,2001);output+=' '+labels[-1]+'=201'
                        child='Spawn exactly one native child' in text
                        sibling=NativeReference('root','child' if child else 'root','root' if child else None,'child-turn' if child else turn,'sibling-item','sibling-item','opaque-sibling')
                        self.work.append({'reference':dataclasses.asdict(sibling),'kind':'terminal','state':'running','provenance':'fixture','observed_at':time.time()})
                        if child:
                            child_ref=NativeReference('root','child','root','child-turn',work_id='child')
                            self.work.append({'reference':dataclasses.asdict(child_ref),'kind':'child','state':'active','provenance':'fixture','observed_at':time.time()})
                    self.event('output.final',replace(ref,item_id='reply'),request,text=output)
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

@pytest.mark.parametrize('missing',['memory','marker'])
def test_native_prompts_require_actual_memory_and_physical_marker_observation(owned,missing):
    if missing=='memory': owned.client.memory=False
    else:owned=replace(owned,observe_marker=None)
    driver=start(owned);result=NativeProbeFactory._exercise(driver,frozenset({'submission'}),time.monotonic()+1)
    assert result['submission'].grade=='inconclusive' and not any(c[0]=='submit' for c in owned.client.calls)
    driver.cleanup(deadline=time.monotonic()+1)

def test_finite_scenario_earns_observed_targets_without_early_os_cleanup_pass(owned):
    driver=start(owned);result=NativeProbeFactory._exercise(driver,frozenset({'submission','stop_reply','stop_work'}),time.monotonic()+3)
    assert all(item.grade=='compatible' for item in result.values())
    assert {'target:terminal','target:child'}<=result['stop_work'].coverage
    assert 'owned_unit_cleanup' not in result['stop_work'].coverage
    assert 200 not in owned.client.pids and 201 not in owned.client.pids and 77 in owned.client.pids
    assert len([c for c in owned.client.calls if c[0]=='submit'])==3
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
