"""Fixed fixture request boundary and evidence persistence; no native inference."""
import json
import sqlite3
import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
from test_native_chat_ownership import database  # noqa: F401

import gui_experiment_runtime as runtime
from conversation_runtime_checks import CheckResult, CleanupProof, EvidenceCache, Fingerprint
from conversation_runtime_contract import ExecutableBinding


@pytest.fixture
def operation(database,monkeypatch):
    original,original_con=database
    root=original.parent
    (root/'.super-coder').mkdir()
    path=root/'.super-coder/shell_db.db'
    con=sqlite3.connect(path);con.row_factory=sqlite3.Row
    original_con.backup(con)
    con.execute('UPDATE users SET is_active=1 WHERE user_id=1')
    con.execute("INSERT INTO users(user_id,username,is_active) VALUES(2,'other',0)");con.commit()
    fp=Fingerprint('codex',ExecutableBinding(Path('/bin/true'),'a'*64,'test'),'test','b'*64,
                   'openai','gpt-6.1-sol','high','b'*64,'c'*64)
    calls=[]
    supervisor=SimpleNamespace(preparation_identity=lambda:calls.append('owned-api'),inventory=lambda:[])
    class Seat:
        def __init__(self,**kwargs):
            self.database,self.root=kwargs['database'],kwargs['root']
            calls.append('seat')
        def candidate_fingerprint(self,*args):
            assert args==('codex','gpt-6.1-sol','high')
            calls.append('candidate');return fp
    monkeypatch.setattr(runtime,'NativeFixtureSeat',Seat)
    value=runtime.FixtureNativeCheck(database=path,root=root,fixture_id='test',
                                    supervisor=supervisor,native_bindings={})
    yield value,fp,con,calls
    value.shutdown()
    con.close()


def test_actual_source_signatures_are_required_subset_without_native_certificate():
    from conversation_adapters.codex_runtime import create_driver
    from conversation_runtime_checks import required_subset
    observed,required=runtime.adapter_interface(create_driver())
    assert set(required)=={'submission','stop_reply','stop_work'}
    assert all(required_subset(observed,shape) is None for shape in required.values())
    del observed['adapter_methods']['control']['deadline']
    assert required_subset(observed,required['submission']) is None
    assert required_subset(observed,required['stop_reply']) is not None


def test_fixed_check_singleflight_and_completed_empty_evidence_does_not_admit(operation):
    value,fp,con,calls=operation
    future=Future();requests=[]
    def request(fingerprint,**kwargs):
        requests.append((fingerprint,kwargs));return future
    value.checker=SimpleNamespace(request=request)
    assert value.begin()['state']=='running'
    assert value.begin()['state']=='running' and len(requests)==1
    assert requests[0][1]['seconds']==177
    assert requests[0][1]['factory'] is value.factory
    future.set_result(CheckResult(fp,{},CleanupProof(True,'complete')))
    report=value.status()
    assert report['state']=='complete' and not report['ordinary_chats_admitted']
    assert report['grades']['submission']=='unverified'
    row=con.execute("SELECT evidence_json FROM conversation_runtime_capability_cache WHERE cache_key='fixture-native'").fetchone()
    assert EvidenceCache.restore(json.loads(row[0])).admission(fp)['submission']=='unverified'
    assert calls[:2]==['owned-api','seat']


def test_restart_retains_incomplete_probe_reference_without_start_or_grade(operation):
    value,fp,con,calls=operation
    con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=\'cv\'',
                (json.dumps({'role':'probe','generation_id':'generation','state':'closing'}),))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES(?,'cv','generation','preparing',100,1)",(fp.key,));con.commit()
    report=value.status()
    assert report['state']=='retained' and report['probe']['generation_id']=='generation'
    assert report['probe']['phase']=='closing' and report['grades']=={}
    assert calls==['owned-api','seat']


def test_explicit_close_uses_only_current_fixed_fingerprint_and_bounded_cleanup(operation,monkeypatch):
    import threading
    import time
    value,fp,_,_=operation
    finished=threading.Event();calls=[]
    value.fingerprint=fp
    def cleanup(fingerprint,*,deadline):
        assert fingerprint==fp and 0<deadline-time.monotonic()<=20
        calls.append(fingerprint.key);finished.set()
    monkeypatch.setattr(value.factory,'cleanup',cleanup)
    monkeypatch.setattr(runtime,'_FIXTURE',value)
    assert runtime.handle_check('POST','Host: 127.0.0.1\r\n',b'{"action":"close"}')[0]==202
    assert finished.wait(1) and calls==[fp.key]


@pytest.mark.parametrize('method,headers,body,code',[
    ('POST','Host: 127.0.0.1\r\nOrigin: https://elsewhere.example\r\n',b'{}',403),
    ('POST','Host: elsewhere.example\r\n',b'{}',403),
    ('POST','Host: 127.0.0.1\r\n',b'{"harness":"claude"}',422),
    ('POST','Host: 127.0.0.1\r\n',b'[]',400),
    ('POST','Host: 127.0.0.1\r\n',b'not-json',400),
    ('GET','Host: 127.0.0.1\r\nAuthorization: Bearer unknown\r\n',b'',401),
    ('DELETE','Host: 127.0.0.1\r\n',b'',405),
])
def test_request_boundary_refuses_options_authority_and_cross_site(operation,monkeypatch,method,headers,body,code):
    value,_,_,_=operation
    monkeypatch.setattr(runtime,'_FIXTURE',value)
    monkeypatch.setattr(value,'begin',lambda:pytest.fail('invalid request cannot allocate'))
    assert runtime.handle_check(method,headers,body)[0]==code


def test_get_status_and_fixed_post_use_named_operator(operation,monkeypatch):
    value,_,con,_=operation
    monkeypatch.setattr(runtime,'_FIXTURE',value)
    monkeypatch.setattr(value,'begin',lambda:{'state':'source-test-transport'})
    assert runtime.handle_check('GET','Host: 127.0.0.1\r\n',b'')[0]==200
    code,_,body=runtime.handle_check('POST','Host: 127.0.0.1\r\n',b'{}')
    assert code==202 and json.loads(body)['state']=='source-test-transport'
    con.execute('UPDATE users SET is_active=0 WHERE user_id=1')
    con.execute('UPDATE users SET is_active=1 WHERE user_id=2');con.commit()
    assert runtime.handle_check('POST','Host: 127.0.0.1\r\n',b'{}')[0]==403
