"""Shared journal connection serialization and real bounded private RPC overlap."""
import dataclasses
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.super-coder/scripts'))
from conversation_runtime import RuntimeClient
from conversation_runtime_contract import (
    DriverStart,
    RuntimeContractError,
    RuntimeIdentity,
    payload_digest,
    runtime_context_wire,
)
from conversation_runtime_controller import (
    Controller,
    Journal,
    PrivateServer,
    start_ticks,
)
from conversation_runtime_native_probes import ControllerProbeDriver, OwnedProbe
from test_conversation_runtime_native_probes import Store
from test_private_history_baseline import Driver, baseline, context


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700);native=Driver();owner=Controller('g',tmp_path,native)
    server=PrivateServer(owner,tmp_path/'socket');thread=threading.Thread(target=server.serve);thread.start()
    client=RuntimeClient(server.endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()),consumer='api')
    client.lease={'consumer':'api','fence':1}
    deadline=time.monotonic()+1
    try:
        # Path existence precedes listen(). A bounded read-only protocol probe
        # establishes listener readiness without retrying attach/other writes.
        while True:
            try:client.request('status',timeout=.1)
            except RuntimeContractError as exc:
                assert exc.code=='LEASE_FENCED';break
            except (FileNotFoundError,ConnectionRefusedError):
                assert time.monotonic()<deadline;time.sleep(.005)
        client.request('attach',expires=time.time()+30,timeout=.1)
        yield client,owner,native
    finally:
        owner.shutdown.set();thread.join(1);assert not thread.is_alive()


@pytest.mark.parametrize('operation',['get','set','pending_submission_before'])
def test_all_direct_connection_operations_and_cursor_consumption_use_existing_lock(tmp_path,operation):
    tmp_path.chmod(0o700);journal=Journal(tmp_path/'journal');actual=journal.db
    class Cursor:
        def __init__(self,value):self.value=value
        def fetchone(self):
            assert journal.lock._is_owned()
            return self.value.fetchone()
    class Connection:
        def execute(self,*args):
            assert journal.lock._is_owned()
            return Cursor(actual.execute(*args))
    journal.db=Connection()
    try:
        if operation=='get':assert journal.get('sequence')==0
        elif operation=='set':journal.set('sequence',1);assert journal.get('sequence')==1
        else:assert journal.pending_submission_before(2) is False
    finally:
        journal.db=actual
        with journal.lock:actual.close()


def test_read_and_write_wait_for_cursor_consumption_before_sharing_connection(tmp_path):
    tmp_path.chmod(0o700);journal=Journal(tmp_path/'journal');actual=journal.db
    cursor_entered=threading.Event();release_cursor=threading.Event();second_sql=threading.Event();faults=[]
    class Cursor:
        def __init__(self,value,first):self.value,self.first=value,first
        def fetchone(self):
            if self.first:
                cursor_entered.set();assert release_cursor.wait(1)
            return self.value.fetchone()
    class Connection:
        def execute(self,*args):
            first=threading.current_thread().name=='first-read'
            if not first:second_sql.set()
            return Cursor(actual.execute(*args),first)
    journal.db=Connection()
    def read():
        try:assert journal.get('sequence')==0
        except (AssertionError, RuntimeError, OSError, sqlite3.Error) as exc:faults.append(type(exc).__name__)
    def write():
        try:journal.set('sequence',1)
        except (AssertionError, RuntimeError, OSError, sqlite3.Error) as exc:faults.append(type(exc).__name__)
    reader=threading.Thread(target=read,name='first-read');writer=threading.Thread(target=write,name='second-write')
    try:
        reader.start();assert cursor_entered.wait(1);writer.start()
        assert not second_sql.wait(.03)
        release_cursor.set();reader.join(1);writer.join(1)
        assert not reader.is_alive() and not writer.is_alive() and not faults
        assert second_sql.is_set() and actual.execute('SELECT 1').fetchone()[0]==1
    finally:
        release_cursor.set();reader.join(1);writer.join(1);journal.db=actual
        with journal.lock:actual.close()


@pytest.mark.parametrize('repeat',range(12))
def test_real_active_proxy_drain_duplicate_open_status_and_cleanup(private,tmp_path,repeat):
    client,owner,_native=private;bound=context(tmp_path,baseline())
    driver=ControllerProbeDriver(OwnedProbe(bound,client,Store(),1,1,'synthetic-unit',tmp_path,True))
    try:
        assert driver.start(bound,lambda _:None,deadline=time.monotonic()+2).state=='ready'
        assert driver._reader.is_alive()
        for _ in range(12):
            assert client.request('open',context=runtime_context_wire(bound),timeout=.1)['ready'] is True
            assert client.request('status',timeout=.1)['ready'] is True
    finally:
        # Cleanup is exercised while the reader is still owned, then joins it;
        # fixture shutdown cannot overlap a surviving replay actor.
        assert driver.cleanup(deadline=time.monotonic()+1).outcome=='complete'
        assert not driver._reader.is_alive()
        with owner.journal.lock:assert owner.journal.db.execute('SELECT 1').fetchone()[0]==1


def test_static_journal_error_response_retains_close_without_private_sql(private,tmp_path):
    client,owner,_native=private
    client.request('open',context=runtime_context_wire(context(tmp_path,baseline())),timeout=.1)
    original=owner.journal.get
    def unavailable(key):
        if key=='setup':raise sqlite3.InterfaceError('PRIVATE_SQL_SENTINEL')
        return original(key)
    owner.journal.get=unavailable
    try:
        with pytest.raises(RuntimeContractError) as caught:client.request('status',timeout=.1)
        assert caught.value.code=='JOURNAL_UNAVAILABLE'
        assert 'PRIVATE_SQL_SENTINEL' not in str(caught.value)
    finally:owner.journal.get=original
    command={'control_id':'close','request_sequence':1,'action':'close'};command['payload_digest']=payload_digest(command)
    assert client.request('close',command=command,timeout=.1)['outcome']=='complete'


def test_native_inventory_wait_does_not_hold_journal_lock_or_block_status(private,tmp_path):
    client,owner,native=private
    bound=dataclasses.replace(context(tmp_path),history=None,workspace=None,capability_evidence={})
    native.start=lambda *args,**kwargs:DriverStart('ready',RuntimeIdentity('old-root'))
    entered=threading.Event();release=threading.Event();failures=[]
    original=native.inventory
    def wait_inventory(*,deadline):
        assert owner.journal.lock.acquire(blocking=False)
        owner.journal.lock.release();entered.set()
        assert release.wait(max(0,deadline-time.monotonic()))
        return original(deadline=deadline)
    native.inventory=wait_inventory
    client.request('open',context=runtime_context_wire(bound),timeout=.1)
    def snapshot():
        try:client.request('snapshot',timeout=.5)
        except (AssertionError, RuntimeError, OSError, sqlite3.Error) as exc:failures.append(type(exc).__name__)
    worker=threading.Thread(target=snapshot)
    try:
        worker.start();assert entered.wait(1)
        assert client.request('status',timeout=.1)['ready'] is True
        release.set();worker.join(1);assert not worker.is_alive() and not failures
    finally:release.set();worker.join(1)
