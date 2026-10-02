"""Durable operator intents for the fixture-owned finite native check.

This is an HTTP workflow over the existing bounded owner/factory, not a second
native process manager. A restarted consumer reads retained intent and probe
ownership; it never starts a replacement check from a lost response.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

import db_driver
from conversation_runtime_contract import RuntimeContractError, payload_digest

# Requested candidates are not claims of native availability or catalogue.
CODEX_SELECTION = {'harness':'codex','model':'gpt-6.1-sol','effort':'high'}
CLAUDE_SELECTION = {'harness':'claude','model':'claude-sonnet-5-5','effort':'high'}
REQUESTED_CANDIDATES = (
    ('codex','gpt-6.1-sol','high','Codex · gpt-6.1-sol · high'),
    ('claude','claude-sonnet-5-5','high','Claude · claude-sonnet-5-5 · high'),
)


def configured_selection(body: dict) -> dict:
    """Exact configured request only; never observed native availability."""
    for harness,model,effort,_label in REQUESTED_CANDIDATES:
        candidate={'harness':harness,'model':model,'effort':effort}
        if body==candidate:
            return candidate
    raise RuntimeContractError('CHECK_SELECTION_INVALID','exact configured native candidate required')


class NativeChecks:
    def __init__(self, operation):
        self.operation=operation
        self.database=operation.database
        self.lock=threading.RLock()
        self.current: str | None=None
        self.beginning=False
        self.failed: str | None=None

    def config(self) -> dict:
        import conversation_native_chats
        return {'enabled':conversation_native_chats._SERVICE is self.operation.service,
                'candidates':[{'harness':harness,'model':model,'effort':effort,'label':label,
                'proof_state':'requested_candidate','grades':{},'diagnostics':[]}
                for harness,model,effort,label in REQUESTED_CANDIDATES],
                'onboarding':{'canonical_main_root':str(self.operation.seat.root.resolve()),
                              'scope':'linked_worktrees','initial_setup':'native_tui',
                              'local_channel_setup':'scoped_gui_action'}}

    def create(self,owner: int,key: str,body: dict) -> dict:
        if owner!=1:
            raise RuntimeContractError('CHECK_SELECTION_INVALID','named operator and exact configured native candidate required')
        body=configured_selection(body)
        request_hash=payload_digest(body)
        with self.lock:
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_check.http_intent'):
                    previous=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=? AND request_key=?',(owner,key)).fetchone()
                    if previous:
                        if previous['request_hash']!=request_hash:
                            raise RuntimeContractError('CHECK_IDEMPOTENCY_CONFLICT','check key was reused with different selection')
                        return self._projection(previous)
                    if con.execute("SELECT 1 FROM conversation_runtime_check_requests WHERE status!='complete' LIMIT 1").fetchone():
                        raise RuntimeContractError('CLEANUP_PENDING','retained check must reconcile before another check')
                    if con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone():
                        raise RuntimeContractError('CLEANUP_PENDING','retained probe must finish owned cleanup before checking')
                    check_id='nc_'+uuid.uuid4().hex
                    now=time.time()
                    con.execute('INSERT INTO conversation_runtime_check_requests VALUES(?,?,?,?,?,?,?,?,?)',
                        (check_id,owner,key,request_hash,json.dumps(body),'accepted','{}',now,now))
            finally:
                con.close()
            self.current,self.beginning,self.failed=check_id,True,None
            # Intent is committed before any discovery, checker, boot or native
            # allocation. Browser loss cannot invent a second native intent.
            threading.Thread(target=self._begin,args=(check_id,),name='native-check-intent',daemon=True).start()
        return self.get(owner,check_id=check_id)

    def _begin(self,check_id: str) -> None:
        try:
            con=db_driver.connect(str(self.database))
            try:
                row=con.execute("SELECT selection_json FROM conversation_runtime_check_requests WHERE check_id=? AND owner_user_id=1 AND status='accepted'",(check_id,)).fetchone()
                if row is None:
                    raise RuntimeContractError('CHECK_INTENT_INVALID','captured selected intent is unavailable')
                selection=configured_selection(json.loads(row['selection_json']))
            finally:
                con.close()
            self.operation.begin(selection=selection,on_candidate=lambda key:self._bind(check_id,key))
            with self.lock:
                self.beginning=False
            future=self.operation.future
            if future is not None:
                future.add_done_callback(lambda _:self.refresh(check_id))
            self.refresh(check_id)
        except Exception: # noqa: BLE001 - private native/config errors stay private
            with self.lock:
                self.beginning=False
                self.failed=check_id
                con=db_driver.connect(str(self.database))
                try:
                    retained=con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone()
                finally:
                    con.close()
                with self.operation.owner.lock:
                    empty=((self.operation.future is None or self.operation.future.done()) and not retained
                           and not self.operation.owner.allocating and not self.operation.owner.closing)
                result: dict={'diagnostics':[{'code':'NATIVE_CHECK_INCONCLUSIVE','grade':'inconclusive'}],
                        'admissible':False,'retry_allowed':empty,'probe':None,'grades':{}}
                self._save(check_id,'complete' if empty else 'retained',result)

    def _save(self,check_id: str,state: str,result: dict) -> None:
        con=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_check.http_result'):
                row=con.execute('SELECT result_json,status FROM conversation_runtime_check_requests WHERE check_id=?',(check_id,)).fetchone()
                if row is None:
                    raise RuntimeContractError('CHECK_INTENT_INVALID','check intent no longer exists')
                previous=json.loads(row[0])
                bound=previous.get('fingerprint')
                if bound is not None and result.get('fingerprint',bound)!=bound:
                    raise RuntimeContractError('CHECK_CANDIDATE_CHANGED','check result belongs to another captured candidate')
                if row['status']=='complete':
                    return # Old consumers cannot regress retained terminal cleanup truth.
                if bound is not None:
                    result['fingerprint']=bound
                if previous.get('probe_binding'):
                    if result.get('probe_binding',previous['probe_binding'])!=previous['probe_binding']:
                        raise RuntimeContractError('CHECK_PROBE_CHANGED','check result belongs to another probe generation')
                    result['probe_binding']=previous['probe_binding']
                con.execute('UPDATE conversation_runtime_check_requests SET status=?,result_json=?,updated_at=? WHERE check_id=?',
                            (state,json.dumps(result),time.time(),check_id))
        finally:
            con.close()

    def _bind(self,check_id: str,fingerprint_key: str) -> None:
        # Do not acquire the workflow lock beneath the operation lock: a GET
        # reads operation status in the other order. Durable request status
        # prevents any replacement while this fixed candidate is being bound.
        con=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_check.http_binding'):
                updated=con.execute("UPDATE conversation_runtime_check_requests SET result_json=?,updated_at=? WHERE check_id=? AND status='accepted' AND json_extract(result_json,'$.fingerprint') IS NULL",
                                    (json.dumps({'fingerprint':fingerprint_key}),time.time(),check_id))
                if updated.rowcount!=1:
                    raise RuntimeContractError('CHECK_INTENT_INVALID','candidate no longer matches retained check intent')
        finally:
            con.close()

    def _admissible(self,result: dict,selection: dict) -> bool:
        if result.get('grades',{}).get('submission')!='compatible':
            return False
        try:
            selection=configured_selection(selection)
            fp=self.operation.seat.candidate_fingerprint(**selection)
            self.operation.service.resolve_route(**selection)
            return fp.key==result.get('fingerprint') and self.operation.cache.admission(fp).get('submission')=='compatible'
        except (RuntimeContractError,OSError,ValueError):
            return False

    def refresh(self,check_id: str) -> None:
        with self.lock:
            if self.current!=check_id or self.beginning or self.failed==check_id:
                return
            con=db_driver.connect(str(self.database))
            try:
                row=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE check_id=?',(check_id,)).fetchone()
                original=json.loads(row['result_json'])
                bound=original.get('fingerprint')
            finally:
                con.close()
            observed=self.operation.status()
            if not bound or observed.get('fingerprint')!=bound:
                # A newer operation/cache cannot satisfy an older durable
                # intent. Reconcile the captured ownership before a new check.
                self._save(check_id,'retained',{'fingerprint':bound,'probe':None,'grades':{},
                    'admissible':False,'retry_allowed':False,
                    'diagnostics':[{'code':'CHECK_CANDIDATE_CHANGED','grade':'inconclusive'}]})
                return
            resources=observed.get('resources') or {}
            cleanup=observed.get('cleanup') or {}
            # Owner capacity and native cleanup are independent from OS exit.
            # A broad compatible label or an exited PID alone cannot admit.
            cleaned=(cleanup.get('unit_verified_exited') is True and cleanup.get('native_outcome')=='complete'
                     and not cleanup.get('unresolved_work') and not cleanup.get('unresolved_definitions')
                     and resources.get('owned_capacity_released') is True
                     and resources.get('owner_allocating') is False and resources.get('owner_closing') is False)
            terminal=observed.get('state')=='complete'
            admissible=bool(terminal and cleaned and self._admissible(observed,json.loads(row['selection_json'])))
            result={name:observed.get(name) for name in ('probe','fingerprint','grades','evidence','cleanup','observations','resources')}
            con=db_driver.connect(str(self.database))
            try:
                probe,binding=self._owned_probe(con,row,original)
            finally:
                con.close()
            if observed.get('probe') is not None and (probe is None or any(observed['probe'].get(key)!=probe[key] for key in ('conversation_id','generation_id'))):
                result={name:None for name in ('probe','evidence','cleanup','observations','resources','behavior_witness')}
                result.update(fingerprint=bound,grades={})
                admissible=False
                cleaned=False
            else:
                result['probe']=probe
            # Optional diagnostic evidence is scoped to the same captured
            # candidate/check/probe. It never contributes to grades/admission.
            witness=observed.get('behavior_witness')
            observed_probe=observed.get('probe')
            if (binding and probe and isinstance(observed_probe,dict)
                    and all(observed_probe.get(key)==probe[key] for key in ('conversation_id','generation_id'))
                    and isinstance(witness,dict)):
                from gui_experiment_runtime import semantic_witness
                result['behavior_witness']=semantic_witness(witness)
            else:
                result['behavior_witness']=None
            if binding:
                result['probe_binding']=binding
            result.update(admissible=admissible,retry_allowed=bool(terminal and cleaned),
                          diagnostics=[{'code':observed['diagnostic'],'grade':'inconclusive'}] if observed.get('diagnostic') else [])
            self._save(check_id,'complete' if terminal and cleaned else 'retained' if terminal else 'running',result)

    @staticmethod
    def _projection(row) -> dict:
        result=json.loads(row['result_json'])
        result.pop('probe_binding',None)
        if isinstance(result.get('behavior_witness'),dict):
            from gui_experiment_runtime import semantic_witness
            result['behavior_witness']=semantic_witness(result['behavior_witness'])
            result['behavior_witness_observation']='historical_check' if row['status']=='complete' else 'recorded_check'
        else:
            if 'behavior_witness' in result:
                result['behavior_witness']=None
            result.pop('behavior_witness_observation',None)
        return {'check_id':row['check_id'],'request_key':row['request_key'],'state':row['status'],
                'selection':json.loads(row['selection_json']),'admissible':False,'retry_allowed':False,
                'grades':{},'diagnostics':[],'probe':None,**result}

    def get(self,owner: int, *, check_id: str | None=None,request_key: str | None=None) -> dict:
        if (check_id is None)==(request_key is None):
            raise RuntimeContractError('CHECK_REQUEST_INVALID','one check id or request key required')
        with self.lock:
            con=db_driver.connect(str(self.database))
            try:
                field,value=('check_id',check_id) if check_id is not None else ('request_key',request_key)
                row=con.execute(f'SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=? AND {field}=?',(owner,value)).fetchone()
                if row is None:
                    raise RuntimeContractError('CHECK_NOT_FOUND','check is outside operator tenancy or not recorded')
                target=row['check_id']
            finally:
                con.close()
            self.refresh(target)
            con=db_driver.connect(str(self.database))
            try:
                row=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE check_id=?',(target,)).fetchone()
                result=self._projection(row)
                if result.get('behavior_witness') is not None:
                    observed_probe,binding=self._owned_probe(con,row,json.loads(row['result_json']))
                    if observed_probe is None or not binding:
                        result['behavior_witness']=None
                        result['behavior_witness_observation']='unavailable'
                if row['status']=='complete':
                    # A retained historical pass is not a claim about the
                    # latest installed source/binary or ordinary resolver.
                    result['admissible']=bool(result.get('admissible') and self._admissible(result,json.loads(row['selection_json'])))
                    if con.execute("SELECT 1 FROM conversation_runtime_check_requests WHERE check_id!=? AND status!='complete' LIMIT 1",(target,)).fetchone() or con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone():
                        result['retry_allowed']=False
                if self.current!=target and row['status']!='complete':
                    result['probe'],binding=self._owned_probe(con,row,json.loads(row['result_json']))
                    probe=result['probe']
                    cleanup=probe.get('cleanup') or {} if probe else {}
                    with self.operation.owner.lock:
                        released=(result.get('fingerprint') not in self.operation.owner.allocating
                                  and result.get('fingerprint') not in self.operation.owner.closing)
                    if (probe and probe['status']=='complete' and probe['phase']=='closed' and released
                            and cleanup.get('unit_verified_exited') is True
                            and (cleanup.get('native_outcome')=='complete' or cleanup.get('never_launched') is True)
                            and not cleanup.get('unresolved_work') and not cleanup.get('unresolved_definitions')):
                        # Cleanup recovery is not a recovered behavior pass.
                        # No prompt, check, or ordinary admission is replayed.
                        result.update(state='complete',admissible=False,retry_allowed=True,grades={},
                            diagnostics=[{'code':'CHECK_CLEANUP_RECONCILED','grade':'inconclusive'}])
                        persisted={key:item for key,item in result.items() if key not in {'check_id','request_key','selection','state'}}
                        persisted['probe_binding']=binding
                        self._save(target,'complete',persisted)
                        return result
                    result.update(state='retained',admissible=False,retry_allowed=False,
                                  diagnostics=[{'code':'CHECK_CONSUMER_RESTARTED','grade':'inconclusive'}])
                return result
            finally:
                con.close()

    @staticmethod
    def _owned_probe(con,row,result: dict) -> tuple[dict|None,dict|None]:
        key=result.get('fingerprint')
        job=con.execute('SELECT * FROM conversation_runtime_probe_jobs WHERE fingerprint_key=?',(key,)).fetchone() if key else None
        chat=con.execute('SELECT c.*,s.user_id AS shell_owner FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(job['conversation_id'],)).fetchone() if job else None
        if chat is None or job is None:
            return None,None
        runtime=json.loads(chat['runtime_projection'])
        selection=json.loads(row['selection_json'])
        binding={'conversation_id':job['conversation_id'],'generation_id':job['generation_id'],'shell_id':chat['shell_id']}
        if (chat['owner_user_id']!=row['owner_user_id'] or chat['shell_owner']!=row['owner_user_id']
                or chat['runtime_mode']!='native_experiment' or runtime.get('role')!='probe'
                or runtime.get('check_id')!=row['check_id']
                or runtime.get('generation_id')!=job['generation_id'] or chat['creation_request_hash']!=key
                or any(chat[field]!=selection[field] for field in ('harness','model','effort'))
                or result.get('probe_binding',binding)!=binding):
            return None,None
        captured=con.execute('SELECT conversation_id,owner_user_id,shell_id FROM conversation_runtime_generations WHERE generation_id=?',(job['generation_id'],)).fetchone()
        if captured and (captured['conversation_id']!=job['conversation_id'] or captured['owner_user_id']!=row['owner_user_id'] or captured['shell_id']!=chat['shell_id']):
            return None,None
        return {'conversation_id':job['conversation_id'],'generation_id':job['generation_id'],
            'status':job['status'],'deadline':job['deadline'],'phase':runtime.get('state'),
            'setup':runtime.get('setup'),'cleanup':runtime.get('cleanup') or runtime.get('preparation_cleanup')},binding
