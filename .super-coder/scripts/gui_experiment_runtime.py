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


def schema_summary(fingerprint,observation,receipt) -> dict:
    """Structural evidence only, with no generated documents or private ledger."""
    from conversation_runtime_codex_schema import CAPABILITIES, RESPONSE_FILES
    names={'ClientRequest.json','ServerNotification.json',*RESPONSE_FILES.values()}
    valid_hash=lambda value:isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
    result={'fingerprint':fingerprint.key,'observation':'installed-native-generated-schema',
            'generation_completed':observation.generation_completed is True,
            'behavior_admitted':False,'structural_grades':{},'diagnostics':[],
            'schema_files_sha256':{},'owned_codegen':{}}
    result['structural_grades']={cap:grade for cap,grade in observation.structural_grades.items()
                                if cap in CAPABILITIES and grade in {'compatible','incompatible','inconclusive'}}
    result['diagnostics']=[{'capability':item.capability,'grade':item.grade,'code':item.code}
                           for item in observation.diagnostics if item.capability in CAPABILITIES
                           and item.grade in {'incompatible','inconclusive'}
                           and item.code in {'NATIVE_SCHEMA_UNAVAILABLE','REQUIRED_NATIVE_INTERFACE_MISMATCH'}]
    result['schema_files_sha256']={name:value for name,value in observation.schema_files_sha256.items()
                                  if name in names and valid_hash(value)}
    for name in ('account_access','child_started','child_reaped','process_group_exited',
                 'files_removed','gate_released','child_registered','cleanup_complete'):
        if type(receipt.get(name)) is bool:result['owned_codegen'][name]=receipt[name]
    if receipt.get('inference_count')==0 and type(receipt.get('inference_count')) is int:
        result['owned_codegen']['inference_count']=0
    if valid_hash(receipt.get('wrapper_sha256')):
        result['owned_codegen']['wrapper_sha256']=receipt['wrapper_sha256']
    return result


def semantic_witness(raw: dict) -> dict:
    """Fixed diagnostic scalars only; never journal/native unknown fields."""
    stages={'allocation','startup','first_processing','first_reply','initial_work_inventory',
            'initial_snapshot','root_tagged_pid','child_ancestry','child_terminal','child_tagged_pid',
            'nonce_recall','stop_reply','stop_terminal','stop_child','sibling_tagged_pid','finished'}
    stage=raw.get('waiting_stage')
    result: dict[str,Any]={'waiting_stage':stage if isinstance(stage,str) and stage in stages else 'unknown'}
    for name in ('first_root_processed','first_final_nonce_matches','first_successful_reply',
                 'second_final_nonce_matches','first_close_observed','native_complete_retained','cleanup_fenced',
                 'initial_snapshot_observed','initial_snapshot_current','initial_snapshot_partial',
                 'initial_root_terminal_current','initial_child_ancestry_current',
                 'initial_child_active_turn_present','observed_child_terminal_current'):
        if type(raw.get(name)) is bool:
            result[name]=raw[name]
    outcome=raw.get('first_close_outcome')
    if isinstance(outcome,str) and outcome in {'complete','pending','failed','inconclusive'}:
        result['first_close_outcome']=outcome
    for name in ('first_close_unresolved_work','first_close_unresolved_definitions'):
        if type(raw.get(name)) is int and 0<=raw[name]<=4096:
            result[name]=raw[name]
    for role in ('root','sibling','child'):
        for suffix in ('tagged_pid_candidates','owned_pid_matches','rejected_pid_output_records'):
            name=role+'_'+suffix
            if type(raw.get(name)) is int and 0<=raw[name]<=128:
                result[name]=raw[name]
        name=role+'_pid_observation'
        if isinstance(raw.get(name),str) and raw[name] in {'unobserved','matched','ambiguous','no_owned_match','missing_terminal_output'}:
            result[name]=raw[name]
    for name in ('first_root_terminal_counts','child_terminal_counts'):
        values=raw.get(name)
        if isinstance(values,dict):
            result[name]={key:values[key] for key in ('completed','failed','interrupted','other')
                          if type(values.get(key)) is int and 0<=values[key]<=4096}
    return result


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
        self.service.probe_delegate=self.consume_probe
        self.checker=CompatibilityChecker(cache=self.cache)
        self.lock=threading.RLock()
        self.future: Future[CheckResult] | None=None
        self.fingerprint: Fingerprint | None=None
        self.failure: str | None=None
        self.stopped=False
        self.persisted=False
        self.cancelling=False
        self.schema_future: Future | None=None
        self.schema_fingerprint: Fingerprint | None=None
        self.schema_observation=None
        self.schema_public: dict | None=None
        self.schema_cleanup_pending=False
        from conversation_native_checks import NativeChecks
        self.workflow=NativeChecks(self)
        self.chats: Any=None

    def consume_probe(self,cid: str,generation: str) -> bool:
        """The current factory retains its reader; restart only reconciles ownership."""
        con=db_driver.connect(str(self.database))
        try:
            job=con.execute('SELECT * FROM conversation_runtime_probe_jobs WHERE conversation_id=? AND generation_id=?',(cid,generation)).fetchone()
            chat=con.execute('SELECT c.*,s.user_id AS shell_owner FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(cid,)).fetchone()
            runtime=json.loads(chat['runtime_projection']) if chat else {}
            if (job is None or chat is None or chat['owner_user_id']!=1 or chat['shell_owner']!=1
                    or chat['runtime_mode']!='native_experiment' or runtime.get('role')!='probe'
                    or runtime.get('generation_id')!=generation):
                raise RuntimeContractError('PROBE_INVALID','probe reconciliation is outside captured tenancy')
            captured=con.execute('SELECT conversation_id,owner_user_id,shell_id,state,cleanup_json FROM conversation_runtime_generations WHERE generation_id=?',(generation,)).fetchone()
            if captured and (captured['conversation_id']!=cid or captured['owner_user_id']!=1 or captured['shell_id']!=chat['shell_id']):
                raise RuntimeContractError('PROBE_INVALID','captured generation conflicts with the retained probe owner')
        finally:
            con.close()
        with self.lock:
            active=(self.fingerprint is not None and self.fingerprint.key==job['fingerprint_key']
                    and (self.future is not None and not self.future.done() or self.cancelling))
            if active:
                if runtime.get('state')=='closing':
                    self.cancel()
                return True
        if runtime.get('state')=='closing' and captured is None and runtime.get('preparation_owner'):
            self.service.recover_preparation(cid,runtime)
            return True
        native_cleanup=json.loads(captured['cleanup_json']) if captured else {}
        preparation=runtime.get('preparation_cleanup') or {}
        complete=(runtime.get('state')=='closed' and chat['state']=='closed'
                  and (captured is not None and captured['state']=='closed'
                       and native_cleanup.get('outcome')=='complete' and native_cleanup.get('unit_verified_exited') is True
                       and not native_cleanup.get('unresolved_work') and not native_cleanup.get('unresolved_definitions')
                       or captured is None and preparation.get('unit_verified_exited') is True and preparation.get('never_launched') is True))
        if complete:
            self.owner.completed_key(job['fingerprint_key'],cid,generation)
        return False

    def retained_guard(self) -> None:
        if self.schema_cleanup_pending:
            raise RuntimeContractError('CLEANUP_PENDING','previous schema cleanup remains unresolved')
        self.seat.ensure_codegen_clean()
        con=db_driver.connect(str(self.database))
        try:
            retained=con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone()
        finally:
            con.close()
        if retained:
            raise RuntimeContractError('CLEANUP_PENDING','retained finite probe must finish owned cleanup before new checking')

    def native_schema(self,fp,deadline):
        # RAM reuse is exact captured identity + completed child cleanup only.
        # Re-capturing is required even when the previous operation succeeded.
        if self.schema_cleanup_pending:
            raise RuntimeContractError('CLEANUP_PENDING','previous schema cleanup remains unresolved')
        self.seat.ensure_codegen_clean()
        if self.seat.candidate_fingerprint('codex','gpt-6.1-sol','high')!=fp or time.monotonic()>=deadline:
            raise RuntimeContractError('NATIVE_SCHEMA_INCONCLUSIVE','schema identity changed')
        if (self.schema_fingerprint==fp and self.schema_observation is not None
                and self.schema_public is not None
                and self.schema_public['owned_codegen'].get('cleanup_complete') is True):
            return self.schema_observation
        try:
            observation,receipt=self.seat.observe_native_schema(fp,deadline)
        except Exception as exc:
            if not isinstance(exc,RuntimeContractError) or exc.code!='NATIVE_SCHEMA_UNALLOCATED':
                self.schema_cleanup_pending=True
            raise
        self.schema_public=schema_summary(fp,observation,receipt)
        if not all(receipt.get(name) is True for name in ('cleanup_complete','child_registered',
                    'gate_released','child_reaped','process_group_exited','files_removed')):
            self.schema_observation=None
            self.schema_fingerprint=None
            self.schema_cleanup_pending=True
            raise RuntimeContractError('NATIVE_SCHEMA_INCONCLUSIVE','schema child cleanup is not proved')
        self.schema_fingerprint=fp
        self.schema_observation=observation if receipt.get('cleanup_complete') is True and observation.generation_completed else None
        return observation

    def begin_schema(self) -> dict:
        with self.lock:
            if self.stopped:
                raise RuntimeContractError('FIXTURE_STOPPED','API consumer is stopping')
            if self.future is not None and not self.future.done():
                raise RuntimeContractError('CHECK_BUSY','native behavior check is active')
            if self.schema_future is not None and not self.schema_future.done():
                return self.status()
            self.retained_guard() # Before candidate observation or codegen.
            deadline=time.monotonic()+23
            future: Future=Future()
            self.schema_future=future
            self.schema_public={'state':'running','observation':'installed-native-generated-schema','behavior_admitted':False}
            def observe():
                try:
                    fp=self.seat.candidate_fingerprint('codex','gpt-6.1-sol','high')
                    with self.lock:
                        if self.stopped:raise RuntimeContractError('FIXTURE_STOPPED','API consumer is stopping')
                    self.native_schema(fp,deadline)
                    with self.lock:
                        self.schema_public['state']='complete' if self.schema_observation is not None else 'inconclusive'
                    future.set_result(None)
                except Exception: # noqa: BLE001 - unknown native payload never becomes a public diagnostic
                    with self.lock:
                        self.schema_observation=None
                        self.schema_public={'state':'inconclusive','observation':'installed-native-generated-schema',
                                            'behavior_admitted':False,'diagnostic':'NATIVE_SCHEMA_INCONCLUSIVE'}
                    future.set_result(None)
            threading.Thread(target=observe,name='fixture-native-schema',daemon=True).start()
            return self.status()

    def begin(self, *, selection=None,on_candidate=None,deadline=None,expected_fingerprint=None,validate_intent=None) -> dict:
        from conversation_native_checks import CODEX_SELECTION, configured_selection
        selection=configured_selection(CODEX_SELECTION if selection is None else selection)
        with self.lock:
            if self.stopped:
                raise RuntimeContractError('FIXTURE_STOPPED','API consumer is stopping')
            if self.future is not None and not self.future.done():
                if validate_intent is not None:
                    raise RuntimeContractError('CHECK_BUSY','automatic intent cannot adopt another active operation')
                if self.fingerprint and selection!={'harness':self.fingerprint.harness,'model':self.fingerprint.model,'effort':self.fingerprint.effort}:
                    raise RuntimeContractError('CHECK_BUSY','another selected native check is active')
                return self.status()
            if self.schema_future is not None and not self.schema_future.done():
                raise RuntimeContractError('CHECK_BUSY','native schema observation is active')
            self.retained_guard()
            deadline=min(time.monotonic()+177,deadline) if deadline is not None else time.monotonic()+177
            if time.monotonic()+20>=deadline:
                raise RuntimeContractError('CHECK_DEADLINE','finite check has no cleanup reserve')
            if validate_intent is not None:validate_intent()
            fp=self.seat.candidate_fingerprint(**selection)
            if expected_fingerprint is not None and fp.key!=expected_fingerprint:
                raise RuntimeContractError('CHECK_CANDIDATE_CHANGED','automatic captured candidate changed before checking')
            loaded_guard=getattr(self.seat,'require_loaded_source',None)
            if loaded_guard is not None:loaded_guard(fp)
            capacity=getattr(self.seat,'probe_capacity',None)
            if capacity is not None and capacity(fp.harness)<1:
                raise RuntimeContractError('PROBE_CAPACITY_PENDING','retained roots leave no probe slot')
            if validate_intent is not None:validate_intent()
            if (selection!={'harness':fp.harness,'model':fp.model,'effort':fp.effort}
                    or fp.provider!=('openai' if fp.harness=='codex' else 'anthropic')):
                raise RuntimeContractError('CHECK_CANDIDATE_CHANGED','captured fingerprint does not match the selected native request')
            if fp.harness=='codex':
                observation=self.native_schema(fp,deadline-20)
                if not observation.generation_completed:
                    raise RuntimeContractError('NATIVE_SCHEMA_INCONCLUSIVE','required native submission interface is unavailable')
                shape=observation.observed_interface
                requirements={cap:observation.requirements[cap] for cap in ('submission','stop_reply','stop_work')}
            else:
                from conversation_adapters.claude_runtime import create_driver
                # This is source signature checking only. Native hook/channel,
                # route and qualified memory observations are required by the
                # owned driver/factory before useful inference and cache proof.
                shape,requirements=adapter_interface(create_driver())
            remaining=deadline-time.monotonic()
            if remaining<60:
                raise RuntimeContractError('CHECK_DEADLINE','source preparation consumed the finite check budget')
            if self.seat.candidate_fingerprint(**selection)!=fp:
                raise RuntimeContractError('CHECK_CANDIDATE_CHANGED','captured candidate changed during source observation')
            if validate_intent is not None:validate_intent()
            self.fingerprint,self.failure,self.persisted=fp,None,False
            if on_candidate is not None:
                on_candidate(fp.key) # Commit HTTP binding before dispatch.
            if validate_intent is not None:validate_intent()
            remaining=deadline-time.monotonic()
            if remaining<60:raise RuntimeContractError('CHECK_DEADLINE','finite dispatch has insufficient remaining budget')
            self.future=self.checker.request(fp,observed_interface=shape,requirements=requirements,
                                             factory=self.factory,seconds=min(177,remaining))
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
                chat=con.execute('SELECT runtime_projection,harness,model,effort FROM conversations WHERE conversation_id=?',(job['conversation_id'],)).fetchone() if job else None
                events=con.execute('SELECT event_json FROM conversation_runtime_events WHERE generation_id=? ORDER BY sequence',(job['generation_id'],)).fetchall() if job else []
            finally:
                con.close()
            selected={'harness':fp.harness,'model':fp.model,'effort':fp.effort} if fp else {
                'harness':chat['harness'],'model':chat['model'],'effort':chat['effort']} if chat else {
                'harness':'codex','model':'gpt-6.1-sol','effort':'high'}
            result: dict[str,Any]={'fixture_id':self.fixture_id,'operation':'finite-native-check',**selected,
                    'state':'running' if self.future and not self.future.done() else 'retained' if job and job['status']!='complete' else 'idle',
                    'fingerprint':fp.key if fp else None,'probe':None,'grades':{},'evidence':{},
                    'diagnostic':self.failure,'ordinary_chats_admitted':False,'structural_observation':
                    'installed-native-generated-schema' if selected['harness']=='codex' else 'source-adapter-signatures-plus-native-runtime-validators',
                    'cache_persisted':self.persisted,'close_pending':self.cancelling}
            if self.schema_public is not None and selected['harness']=='codex':
                result['native_schema']=self.schema_public
            if self.schema_cleanup_pending:
                result['native_schema']={'state':'cleanup_pending','behavior_admitted':False,
                                         'observation':'installed-native-generated-schema'}
            if fp:
                result['behavior_witness']=semantic_witness(self.factory.witness(fp))
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
        if self.chats is not None:
            self.chats.close()
        else:
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
            if request=={'action':'schema'}:
                return routes._json(202,_FIXTURE.begin_schema())
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
    from gui_experiment_chats import FixtureChats
    _FIXTURE.chats=FixtureChats(_FIXTURE)
    _FIXTURE.chats.start()
    def shutdown():
        global _FIXTURE
        _FIXTURE.shutdown()
        _FIXTURE=None
    return shutdown
