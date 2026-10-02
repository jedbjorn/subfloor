"""Fixed finite native-check operation inside the disposable API only.

This seam exercises actual owner/factory behavior, not ordinary Chats admission.
Production runtime startup is unchanged. The fixture ledger retains ownership
through API death and verifies all registered units before deleting state.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import conversation_native_chats
import db_driver
from conversation_runtime_checks import (
    CheckResult,
    CompatibilityChecker,
    EvidenceCache,
    Fingerprint,
)
from conversation_runtime_contract import RuntimeContractError
from conversation_runtime_native_probes import NativeProbeFactory
from gui_experiment_native_seat import NativeFixtureSeat
from gui_experiment_probe_owner import NativeProbeOwner

_FIXTURE = None
CHECK_PATH = '/api/experiment-native-check'


def adapter_interface(driver) -> tuple[dict,dict]:
    """Observe consumed Python signatures; never a native-wire certificate."""
    parameters={name:{key:{'kind':value.kind.name} for key,value in inspect.signature(getattr(driver,name)).parameters.items()}
                for name in ('start','submit','inventory','control','cleanup')}
    required={name:{key:{'kind':kind} for key,kind in fields.items()} for name,fields in {
        'start':{'context':'POSITIONAL_OR_KEYWORD','emit':'POSITIONAL_OR_KEYWORD','deadline':'KEYWORD_ONLY'},
        'submit':{'command':'POSITIONAL_OR_KEYWORD','deadline':'KEYWORD_ONLY'},
        'inventory':{'deadline':'KEYWORD_ONLY'},
        'control':{'command':'POSITIONAL_OR_KEYWORD','deadline':'KEYWORD_ONLY'},
        'cleanup':{'deadline':'KEYWORD_ONLY'}}.items()}
    return {'adapter_methods':parameters},{
        cap:{'adapter_methods':{name:required[name] for name in names}}
        for cap,names in {'submission':('start','submit','inventory','cleanup'),
                          'stop_reply':('start','inventory','control','cleanup'),
                          'stop_work':('start','inventory','control','cleanup')}.items()}


class FixtureNativeCheck:
    def __init__(self, *, database: Path, root: Path, fixture_id: str, supervisor, native_bindings):
        if database.resolve()!=root.resolve()/'.super-coder/shell_db.db' or not fixture_id:
            raise ValueError('synthetic fixture identity required')
        # Ownership is proved before even bounded native identity discovery.
        supervisor.preparation_identity()
        self.database,self.fixture_id,self.supervisor=database,fixture_id,supervisor
        con=db_driver.connect(str(database))
        try:
            row=con.execute("SELECT evidence_json FROM conversation_runtime_capability_cache WHERE cache_key='fixture-native'").fetchone()
            self.cache=EvidenceCache.restore(json.loads(row[0])) if row else EvidenceCache()
        finally:
            con.close()
        self.seat=NativeFixtureSeat(database=database,root=root,supervisor=supervisor,
                                  native_bindings=native_bindings,cache=self.cache)
        self.service=conversation_native_chats.NativeChatsService(database,root,supervisor)
        self.owner=NativeProbeOwner(self.seat,self.service)
        self.factory=NativeProbeFactory(self.owner.allocate,self.owner.cleanup)
        self.checker=CompatibilityChecker(cache=self.cache)
        self.lock=threading.RLock()
        self.future: Future[CheckResult] | None=None
        self.fingerprint: Fingerprint | None=None
        self.failure: str | None=None
        self.stopped=False
        self.persisted=False
        self.cancelling=False
        from conversation_native_checks import NativeChecks
        self.workflow=NativeChecks(self)

    def begin(self, *, on_candidate=None) -> dict:
        with self.lock:
            if self.stopped:
                raise RuntimeContractError('FIXTURE_STOPPED','API consumer is stopping')
            if self.future is not None and not self.future.done():
                return self.status()
            con=db_driver.connect(str(self.database))
            try:
                retained=con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone()
            finally:
                con.close()
            if retained:
                raise RuntimeContractError('CLEANUP_PENDING','retained finite probe must finish owned cleanup before new checking')
            fp=self.seat.candidate_fingerprint('codex','gpt-6.1-sol','high')
            from conversation_adapters.codex_runtime import create_driver
            shape,requirements=adapter_interface(create_driver())
            self.fingerprint,self.failure,self.persisted=fp,None,False
            if on_candidate is not None:
                on_candidate(fp.key) # Commit HTTP binding before dispatch.
            self.future=self.checker.request(fp,observed_interface=shape,requirements=requirements,
                                             factory=self.factory,seconds=177)
            self.future.add_done_callback(self.finished)
            return self.status()

    def finished(self,future) -> None:
        try:
            future.result()
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_check.persist_cache'):
                    # Snapshot at the committed write edge. Merge persisted
                    # records so a delayed old callback cannot overwrite newer
                    # fingerprint/capability evidence from another completion.
                    row=con.execute("SELECT evidence_json FROM conversation_runtime_capability_cache WHERE cache_key='fixture-native'").fetchone()
                    previous=EvidenceCache.restore(json.loads(row[0])).export() if row else {'records':[]}
                    payload=self.cache.export()
                    records={record['key']:record for record in previous['records']}
                    for record in payload['records']:
                        prior=records.get(record['key'])
                        if prior:
                            for cap,item in prior['evidence'].items():
                                if cap not in record['evidence'] or record['evidence'][cap]['observed_at']<item['observed_at']:
                                    record['evidence'][cap]=item
                        records[record['key']]=record
                    payload['records']=sorted(records.values(),key=lambda record:max((item['observed_at'] for item in record['evidence'].values()),default=0))[-128:]
                    con.execute('INSERT OR REPLACE INTO conversation_runtime_capability_cache VALUES(?,?,?)',
                                ('fixture-native',json.dumps(payload,separators=(',',':')),time.time()))
            finally:
                con.close()
            with self.lock:
                if self.future is future:
                    self.persisted=True
        except Exception: # noqa: BLE001 - private transport errors never become diagnostics
            with self.lock:
                if self.future is future:
                    self.failure='CHECK_RESULT_INCONCLUSIVE'

    def cancel(self) -> dict:
        with self.lock:
            if self.fingerprint is None:
                raise RuntimeContractError('CLEANUP_PENDING','retained probe requires fixture-owned cleanup')
            if not self.cancelling:
                self.cancelling=True
                fp=self.fingerprint
                def close():
                    try:
                        self.factory.cleanup(fp,deadline=time.monotonic()+20)
                    except Exception: # noqa: BLE001 - retain unknown scoped cleanup
                        self.failure='OWNED_CLEANUP_INCONCLUSIVE'
                    finally:
                        with self.lock:
                            self.cancelling=False
                threading.Thread(target=close,name='fixture-native-check-close',daemon=True).start()
            return self.status()

    def status(self) -> dict:
        with self.lock:
            fp=self.fingerprint
            con=db_driver.connect(str(self.database))
            try:
                job=con.execute('SELECT * FROM conversation_runtime_probe_jobs WHERE fingerprint_key=?',(fp.key,)).fetchone() if fp else None
                if job is None:
                    # Restart exposes retained ownership; never replacement
                    # allocation, inferred grades, or replay of a lost probe.
                    job=con.execute("SELECT * FROM conversation_runtime_probe_jobs WHERE status!='complete' ORDER BY updated_at DESC LIMIT 1").fetchone()
                chat=con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(job['conversation_id'],)).fetchone() if job else None
                events=con.execute('SELECT event_json FROM conversation_runtime_events WHERE generation_id=? ORDER BY sequence',(job['generation_id'],)).fetchall() if job else []
            finally:
                con.close()
            result: dict[str,Any]={'fixture_id':self.fixture_id,'operation':'finite-native-check','harness':'codex',
                    'model':'gpt-6.1-sol','effort':'high','state':'running' if self.future and not self.future.done() else 'retained' if job and job['status']!='complete' else 'idle',
                    'fingerprint':fp.key if fp else None,'probe':None,'grades':{},'evidence':{},
                    'diagnostic':self.failure,'ordinary_chats_admitted':False,'structural_observation':'python-adapter-signatures',
                    'cache_persisted':self.persisted,'close_pending':self.cancelling}
            if job:
                if chat is None:
                    raise RuntimeContractError('PROBE_INVALID','retained probe chat is unavailable')
                projection=json.loads(chat[0])
                result['probe']={'conversation_id':job['conversation_id'],'generation_id':job['generation_id'],
                                 'status':job['status'],'deadline':job['deadline'],'phase':projection.get('state'),
                                 'setup':projection.get('setup'),'cleanup':projection.get('cleanup') or projection.get('preparation_cleanup')}
                root_activities: set[tuple[str | None,str | None,str]]=set()
                child_activities: set[tuple[str | None,str | None,str]]=set()
                first_activity=None
                for raw in events:
                    event=json.loads(raw[0]);ref=event.get('reference') or {}
                    if event['kind'] in {'activity.started','activity.processed'} and ref.get('activity_id'):
                        first_activity=event['observed_at'] if first_activity is None else min(first_activity,event['observed_at'])
                        target=root_activities if ref.get('thread_id') in {None,ref.get('root_id')} else child_activities
                        target.add((ref.get('root_id'),ref.get('thread_id'),ref['activity_id']))
                native=next((item for item in self.supervisor.inventory() if item['generation_id']==job['generation_id']),None)
                os_cleanup=native.get('os_cleanup',{}) if native else {}
                with self.owner.lock:
                    allocating=job['fingerprint_key'] in self.owner.allocating
                    closing=job['fingerprint_key'] in self.owner.closing
                result['observations']={'native_route':projection.get('native_route'),'memory_policy':projection.get('memory_policy'),
                    'ready_observation_at':projection.get('ready_observation_at'),'native_process':projection.get('native_process'),
                    'root_activities_observed':len(root_activities),'child_activities_observed':len(child_activities),
                    'controller_events_retained':len(events),'first_activity_observed_at':first_activity}
                result['resources']={'owner_allocating':allocating,'owner_closing':closing,
                    'owned_capacity_released':job['status']=='complete' and not allocating and not closing,
                    'owned_unit_cleanup_verified':os_cleanup.get('complete') is True,
                    'owned_cgroup_empty':os_cleanup.get('cgroup_empty') is True,
                    'owned_recorded_process_exited':os_cleanup.get('recorded_process_exited') is True,
                    'private_journal_retained_until_fixture_stop':native is not None}
            if fp and self.future and self.future.done() and self.persisted and self.failure is None:
                measured=self.future.result()
                result.update(state='complete',grades=self.cache.admission(fp),
                              cleanup=dataclasses.asdict(measured.cleanup) if measured.cleanup else None,
                              evidence={cap:{'grade':item.grade,'coverage':sorted(item.coverage),
                                             'diagnostics':[{'code':d.code,'grade':d.grade,'capability':d.capability} for d in item.diagnostics]}
                                        for cap,item in measured.evidence.items()})
            return result

    def shutdown(self) -> None:
        # Releasing an API consumer never signals captured native units.
        with self.lock:
            self.stopped=True
        self.service.shutdown()


def handle_check(method: str,headers_raw: str,body: bytes) -> tuple:
    import conversation_routes as routes
    if _FIXTURE is None:
        return routes._err(503,'EXPERIMENT_UNAVAILABLE','named fixture runtime is not active')
    headers=routes._parse_headers(headers_raw)
    if not routes._host_ok(headers):
        return routes._err(403,'HOST_NOT_ALLOWED','fixture API serves loopback only')
    con=db_driver.connect(str(_FIXTURE.database))
    try:
        operator=routes._operator(con,headers)
        if operator['user_id']!=1:
            raise routes.ApiError(403,'OPERATOR_REQUIRED','named synthetic operator required')
        if method=='POST':
            if not routes._mutation_site_ok(headers):
                raise routes.ApiError(403,'NOT_SAME_ORIGIN','cross-site fixture mutation rejected')
            request=routes._body(body)
            if request=={'action':'close'}:
                return routes._json(202,_FIXTURE.cancel())
            if request!={}:
                raise routes.ApiError(422,'CHECK_REQUEST_INVALID','fixed native check takes no options')
            return routes._json(202,_FIXTURE.begin())
        if method=='GET':
            return routes._json(200,_FIXTURE.status())
        return routes._err(405,'METHOD_NOT_ALLOWED','GET or POST required')
    except routes.ApiError as exc:
        return routes._err(exc.status,exc.code,exc.message)
    except RuntimeContractError as exc:
        return routes._err(422,exc.code,'native fixture prerequisite remains inconclusive')
    finally:
        con.close()


def start_fixture(*,database: Path,root: Path,fixture_id: str,supervisor,native_bindings=None):
    global _FIXTURE
    if _FIXTURE is not None:
        raise ValueError('one fixture API owner required')
    _FIXTURE=FixtureNativeCheck(database=database,root=root,fixture_id=fixture_id,
                              supervisor=supervisor,native_bindings=native_bindings or {})
    def shutdown():
        global _FIXTURE
        _FIXTURE.shutdown()
        _FIXTURE=None
    return shutdown
