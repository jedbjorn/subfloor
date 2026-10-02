"""Durable cold-check HTTP intent/reconciliation; no native processes."""
import json
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import conversation_native_checks as checks
from conversation_runtime_contract import RuntimeContractError
from test_native_chat_ownership import database  # noqa: F401


@pytest.fixture
def workflow(request,monkeypatch):
    path,con=request.getfixturevalue('database')
    calls=[]
    report={'state':'running','fingerprint':'fp','grades':{},'probe':None,'diagnostic':None}
    operation=SimpleNamespace(database=path,future=Future(),
        seat=SimpleNamespace(root=path.parent,candidate_fingerprint=lambda **_:SimpleNamespace(key='fp')),
        service=SimpleNamespace(resolve_route=lambda **_:calls.append('resolve')),
        cache=SimpleNamespace(admission=lambda _: {'submission':'compatible'}),
        owner=SimpleNamespace(lock=threading.RLock(),allocating=set(),closing=set()),
        status=lambda:dict(report))
    def begin(*,on_candidate):
        assert con.execute("SELECT COUNT(*) FROM conversation_runtime_check_requests WHERE status='accepted'").fetchone()[0]==1
        on_candidate('fp')
        calls.append('begin')
    operation.begin=begin
    class InlineThread:
        def __init__(self,*,target,args,**_):self.target,self.args=target,args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(checks.threading,'Thread',InlineThread)
    value=checks.NativeChecks(operation)
    return value,operation,report,calls,con


def test_committed_intent_exact_key_readback_and_duplicate_do_not_restart(workflow):
    value,_,_,calls,con=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    assert first['state']=='running' and not first['admissible']
    duplicate=value.create(1,'stable',checks.CODEX_SELECTION)
    assert duplicate['check_id']==first['check_id'] and calls==['begin']
    assert value.get(1,request_key='stable')['check_id']==first['check_id']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0]==1
    with pytest.raises(RuntimeContractError,match='outside operator'):
        value.get(2,check_id=first['check_id'])
    with pytest.raises(RuntimeContractError) as exc:value.create(1,'replacement',checks.CODEX_SELECTION)
    assert exc.value.code=='CLEANUP_PENDING' and calls==['begin']


def test_restart_retained_request_never_discovers_or_replaces(workflow):
    value,operation,_,calls,_=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    restarted=checks.NativeChecks(operation)
    retained=restarted.get(1,request_key='stable')
    assert retained['state']=='retained' and not retained['retry_allowed']
    assert retained['diagnostics'][0]['code']=='CHECK_CONSUMER_RESTARTED'
    assert restarted.create(1,'stable',checks.CODEX_SELECTION)['check_id']==first['check_id']
    with pytest.raises(RuntimeContractError) as exc:restarted.create(1,'new',checks.CODEX_SELECTION)
    assert exc.value.code=='CLEANUP_PENDING' and calls==['begin']


def test_restart_can_read_exact_bound_probe_after_http_ack_is_lost(workflow):
    value,operation,_,calls,con=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    runtime={'role':'probe','generation_id':'owned-generation','state':'needs_consent','setup':{'setup_id':'owned-phase'}}
    con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=\'cv\'',(json.dumps(runtime),))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('fp','cv','owned-generation','preparing',100,1)")
    con.commit()
    result=checks.NativeChecks(operation).get(1,request_key='stable')
    assert result['check_id']==first['check_id']
    assert result['probe']['generation_id']=='owned-generation' and result['probe']['setup']['setup_id']=='owned-phase'
    assert not result['admissible'] and calls==['begin']


@pytest.mark.parametrize('failure',['native','allocating','closing','capacity','variant','changed','resolver'])
def test_start_and_new_check_require_matching_cleanup_and_owner_admission(workflow,failure):
    value,operation,report,_,_=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete','unresolved_work':[],'unresolved_definitions':[]},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    if failure=='native':report['cleanup']['native_outcome']='inconclusive'
    elif failure=='allocating':report['resources']['owner_allocating']=True
    elif failure=='closing':report['resources']['owner_closing']=True
    elif failure=='capacity':report['resources']['owned_capacity_released']=False
    elif failure=='variant':report['grades']['submission']='inconclusive'
    elif failure=='changed':operation.seat.candidate_fingerprint=lambda **_:SimpleNamespace(key='replacement')
    elif failure=='resolver':
        def unavailable(**_):raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','missing ordinary route proof')
        operation.service.resolve_route=unavailable
    result=value.get(1,check_id=first['check_id'])
    assert not result['admissible']
    assert result['retry_allowed']==(failure in {'variant','changed','resolver'})


def test_completed_scoped_cleanup_allows_fresh_explicit_intent_and_old_result_is_stable(workflow):
    value,_,report,calls,con=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    completed=value.get(1,check_id=first['check_id'])
    assert completed['admissible'] and completed['retry_allowed']
    second=value.create(1,'second',checks.CODEX_SELECTION)
    assert second['check_id']!=first['check_id'] and calls.count('begin')==2
    before=con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?',(first['check_id'],)).fetchone()[0]
    value.refresh(first['check_id'])
    after=con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?',(first['check_id'],)).fetchone()[0]
    assert json.loads(before)==json.loads(after)


def test_historical_admission_is_rechecked_after_installed_identity_changes(workflow):
    value,operation,report,_,_=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    assert value.get(1,check_id=first['check_id'])['admissible']
    operation.seat.candidate_fingerprint=lambda **_:SimpleNamespace(key='replacement')
    readback=checks.NativeChecks(operation).get(1,request_key='first')
    assert not readback['admissible'] and readback['retry_allowed']


def test_failed_discovery_only_releases_intent_after_empty_completed_begin(workflow):
    value,operation,_,_,_=workflow
    operation.future=None
    def unavailable(**_):raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','missing native identity')
    operation.begin=unavailable
    first=value.create(1,'first',checks.CODEX_SELECTION)
    assert first['state']=='complete' and first['retry_allowed'] and not first['admissible']
    operation.owner.allocating.add('pending')
    second=value.create(1,'second',checks.CODEX_SELECTION)
    assert second['state']=='retained' and not second['retry_allowed']
