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
from pathlib import Path

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
        self.consumer_token=uuid.uuid4().hex

    def config(self) -> dict:
        import conversation_native_chats
        preview=getattr(self.operation.seat,'behavioral_profile','full')=='submission-preview'
        captured=getattr(self.operation.seat,'native_bindings',{})
        path=captured.get('CODEX') if isinstance(captured,dict) else None
        bound_codex=(type(path) is str and Path(path).is_absolute()
                     and not any(char in path for char in '\n\r\0'))
        candidates=[row for row in REQUESTED_CANDIDATES
                    if not preview or row[0]=='codex' and bound_codex]
        answer={'enabled':conversation_native_chats._SERVICE is self.operation.service,
                'candidates':[{'harness':harness,'model':model,'effort':effort,'label':label,
                'proof_state':'requested_candidate','grades':{},'diagnostics':[],
                'automatic_check':self.automatic_reference({'harness':harness,'model':model,'effort':effort}),
                **self.retained_references({'harness':harness,'model':model,'effort':effort})}
                for harness,model,effort,label in candidates],
                'onboarding':{'canonical_main_root':str(self.operation.seat.root.resolve()),
                              'scope':'linked_worktrees','initial_setup':'native_tui',
                              'local_channel_setup':'scoped_gui_action'}}
        if preview:answer['preview_profile']='submission-preview'
        return answer

    def _current_owner(self) -> bool:
        import conversation_native_chats
        service=self.operation.service
        return (service is not None and conversation_native_chats._SERVICE is service
                and not service.stopped.is_set()
                and service.database.resolve()==self.database.resolve())

    @staticmethod
    def _automatic(row) -> dict | None:
        value=json.loads(row['result_json']).get('_automatic')
        if not isinstance(value,dict) or set(value)!={'origin','expected_fingerprint','phase','attempt','consumer','unavailable'}:
            return None
        if (value['origin']!='installed_change' or value['phase'] not in {'queued','claimed','settled'}
                or type(value['attempt']) is not int or not 0<=value['attempt']<=32
                or type(value['unavailable']) is not bool
                or not isinstance(value['expected_fingerprint'],str) or len(value['expected_fingerprint'])!=64
                or any(c not in '0123456789abcdef' for c in value['expected_fingerprint'])
                or not isinstance(value['consumer'],str) or len(value['consumer']) not in {0,32}
                or any(c not in '0123456789abcdef' for c in value['consumer'])):
            return None
        if (json.loads(row['result_json']).get('fingerprint')!=value['expected_fingerprint']
                or value['phase']=='queued' and value['consumer']!=''
                or value['phase']=='claimed' and len(value['consumer'])!=32):return None
        return value

    def automatic_reference(self,selection: dict, *, fingerprint: str | None=None) -> dict | None:
        if not self._current_owner():return None
        try:
            selection=configured_selection(selection)
        except (RuntimeContractError,OSError,ValueError):return None
        con=db_driver.connect(str(self.database))
        try:
            rows=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=1 ORDER BY created_at DESC,check_id DESC').fetchall()
            rows=[row for row in rows if json.loads(row['selection_json'])==selection]
            if not rows:return None
            try:fingerprint=fingerprint or self.operation.seat.candidate_fingerprint(**selection).key
            except (RuntimeContractError,OSError,ValueError):return None
            if not self._current_owner():return None
            for row in rows:
                if json.loads(row['selection_json'])!=selection:continue
                result=json.loads(row['result_json'])
                if result.get('fingerprint')!=fingerprint:continue
                if len(row['check_id'])!=35 or not row['check_id'].startswith('nc_') or any(c not in '0123456789abcdef' for c in row['check_id'][3:]):continue
                auto=self._automatic(row)
                if '_automatic' in result and auto is None:continue
                # Exact GET owns all probe/cleanup/admission details. A ref
                # alone never supplies a capability or a consent descriptor.
                code=result.get('diagnostics') or []
                diagnostic=code[0].get('code') if code and isinstance(code[0],dict) else None
                if not isinstance(diagnostic,str) or not 1<=len(diagnostic)<=128 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for c in diagnostic):diagnostic=None
                return {'check_id':row['check_id'],'origin':'installed_change' if auto else 'operator',
                        'state':row['status'],'diagnostic':diagnostic}
        finally:con.close()
        return None

    def retained_references(self,selection: dict) -> dict:
        """Old captured probes remain discoverable while a new identity waits."""
        refs: list[dict]=[]
        answer: dict={'retained_checks':refs,'retained_checks_partial':False}
        if not self._current_owner():return answer
        con=db_driver.connect(str(self.database))
        try:
            rows=con.execute("SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=1 AND status!='complete' ORDER BY created_at,check_id").fetchall()
            for row in rows:
                if json.loads(row['selection_json'])!=selection:continue
                cid=row['check_id']
                if not isinstance(cid,str) or len(cid)!=35 or not cid.startswith('nc_') or any(c not in '0123456789abcdef' for c in cid[3:]):continue
                if len(refs)==16:
                    answer['retained_checks_partial']=True;break
                meta=self._automatic(row)
                refs.append({'check_id':cid,'origin':'installed_change' if meta else 'operator',
                    'state':row['status'],'scope':'retained_check'})
        finally:con.close()
        return answer

    def note_unavailable(self,harness: str) -> None:
        """An observed availability transition allows one later safe retry."""
        if not self._current_owner():return
        con=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_check.unavailable'):
                for row in con.execute("SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=1 AND status='complete'").fetchall():
                    meta=self._automatic(row)
                    if meta and json.loads(row['selection_json']).get('harness')==harness and not meta['unavailable']:
                        result=json.loads(row['result_json']);meta['unavailable']=True;result['_automatic']=meta
                        con.execute('UPDATE conversation_runtime_check_requests SET result_json=? WHERE check_id=?',(json.dumps(result),row['check_id']))
        finally:con.close()

    def enqueue_automatic(self,selection: dict,fingerprint: str) -> dict | None:
        selection=configured_selection(selection)
        if not self._current_owner():return None
        if not isinstance(fingerprint,str) or len(fingerprint)!=64 or any(c not in '0123456789abcdef' for c in fingerprint):
            raise RuntimeContractError('CHECK_CANDIDATE_CHANGED','exact automatic fingerprint required')
        with self.lock:
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_check.automatic_intent'):
                    if not self._current_owner():return None
                    if self.operation.seat.candidate_fingerprint(**selection).key!=fingerprint:
                        raise RuntimeContractError('CHECK_CANDIDATE_CHANGED','automatic observation is no longer current')
                    if not self._current_owner():return None
                    rows=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=1 ORDER BY created_at DESC,check_id DESC').fetchall()
                    # Source observation has not bound the manual candidate
                    # yet. Defer, without claiming a fingerprint match: a
                    # queued same-candidate row would later make owned probe
                    # allocation ambiguous when the manual check binds.
                    for row in rows:
                        if (self.beginning and self.current==row['check_id'] and row['status']=='accepted'
                                and json.loads(row['selection_json'])==selection
                                and row['request_hash']==payload_digest(selection)):
                            result=json.loads(row['result_json'])
                            if '_automatic' not in result and result.get('fingerprint') is None:
                                return None
                    attempt=0
                    # A later observation supersedes only positively queued
                    # work. Claimed/unknown ownership is retained for Close.
                    for prior in rows:
                        old=self._automatic(prior)
                        if (old and prior['status']=='accepted' and old['phase']=='queued'
                                and json.loads(prior['selection_json'])==selection
                                and old['expected_fingerprint']!=fingerprint):
                            result=json.loads(prior['result_json']);result['_automatic']=dict(old,phase='settled')
                            result.update(admissible=False,retry_allowed=True,
                                diagnostics=[{'code':'CHECK_CANDIDATE_CHANGED','grade':'inconclusive'}])
                            con.execute("UPDATE conversation_runtime_check_requests SET status='complete',result_json=?,updated_at=? WHERE check_id=? AND status='accepted'",
                                        (json.dumps(result),time.time(),prior['check_id']))
                    for row in rows:
                        result=json.loads(row['result_json'])
                        if json.loads(row['selection_json'])!=selection or result.get('fingerprint')!=fingerprint:continue
                        meta=self._automatic(row)
                        if '_automatic' in result and meta is None:
                            raise RuntimeContractError('CHECK_INTENT_INVALID','automatic metadata is inconclusive')
                        if (meta and row['status']=='complete' and meta['unavailable']
                                and result.get('retry_allowed') is True and meta['attempt']<32):
                            attempt=meta['attempt']+1
                            break
                        return self._projection(row)
                    key='auto_'+payload_digest({'owner':1,'selection':selection,'fingerprint':fingerprint,'attempt':attempt})
                    previous=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE owner_user_id=1 AND request_key=?',(key,)).fetchone()
                    if previous:return self._projection(previous)
                    meta={'origin':'installed_change','expected_fingerprint':fingerprint,'phase':'queued',
                          'attempt':attempt,'consumer':'','unavailable':False}
                    result={'fingerprint':fingerprint,'_automatic':meta,'admissible':False,'retry_allowed':False,
                            'diagnostics':[{'code':'AUTOMATIC_CHECK_QUEUED','grade':'inconclusive'}]}
                    cid='nc_'+uuid.uuid4().hex;now=time.time()
                    con.execute('INSERT INTO conversation_runtime_check_requests VALUES(?,?,?,?,?,?,?,?,?)',
                        (cid,1,key,payload_digest(selection),json.dumps(selection),'accepted',json.dumps(result),now,now))
                    return {'check_id':cid,'state':'accepted'}
            finally:con.close()

    def _automatic_edge(self,check_id: str,fp: str,deadline: float,selection: dict) -> None:
        if time.monotonic()>=deadline or not self._current_owner():
            raise RuntimeContractError('CHECK_OWNER_CHANGED','automatic dispatch owner/deadline changed')
        self.operation.supervisor.preparation_identity(deadline=deadline)
        if time.monotonic()>=deadline or not self._current_owner():
            raise RuntimeContractError('CHECK_OWNER_CHANGED','marked API owner changed during observation')
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE check_id=?',(check_id,)).fetchone()
            meta=self._automatic(row) if row else None
            if (row is None or row['owner_user_id']!=1 or row['status'] not in {'accepted','running'}
                    or meta is None or meta['phase']!='claimed' or meta['consumer']!=self.consumer_token
                    or json.loads(row['selection_json'])!=selection or row['request_hash']!=payload_digest(selection)
                    or meta['expected_fingerprint']!=fp or json.loads(row['result_json']).get('fingerprint')!=fp):
                raise RuntimeContractError('CHECK_INTENT_INVALID','automatic dispatch no longer owns its captured intent')
        finally:con.close()

    def dispatch_automatic(self) -> None:
        """Claim at most one durable queued intent; never restart a claim."""
        with self.lock:
            if not self._current_owner() or self.beginning:return
            if self.current is not None:self.refresh(self.current)
            if self.operation.future is not None and not self.operation.future.done():return
            con=db_driver.connect(str(self.database))
            try:
                rows=con.execute("SELECT * FROM conversation_runtime_check_requests WHERE status!='complete' ORDER BY created_at,check_id").fetchall()
                # Conflicting/manual/claimed records require readback and owned
                # cleanup first. Accepted alone is not never-dispatched proof.
                queued=[]
                for row in rows:
                    meta=self._automatic(row)
                    if row['owner_user_id']!=1 or row['status']!='accepted' or meta is None or meta['phase']!='queued':return
                    queued.append(row)
                if not queued:return
                row=queued[0];meta=self._automatic(row)
                assert meta is not None
                selection=configured_selection(json.loads(row['selection_json']))
                if con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone():return
            finally:con.close()
            try:
                self.operation.retained_guard()
                fp=self.operation.seat.candidate_fingerprint(**selection)
                if fp.key!=meta['expected_fingerprint']:
                    self._save(row['check_id'],'complete',{'admissible':False,'retry_allowed':True,
                        'diagnostics':[{'code':'CHECK_CANDIDATE_CHANGED','grade':'inconclusive'}]})
                    return
                self.operation.seat.require_loaded_source(fp)
                if self.operation.seat.probe_capacity(fp.harness)<1:return
            except RuntimeContractError as exc:
                if exc.code in {'CLEANUP_PENDING','PROBE_CAPACITY_PENDING'}:return
                if exc.code=='LOADED_SOURCE_CHANGED':
                    self._save(row['check_id'],'accepted',{'admissible':False,'retry_allowed':False,
                        'diagnostics':[{'code':'LOADED_SOURCE_CHANGED','grade':'inconclusive'}]})
                    return # No attempt began; a matching new API may claim it.
                self._save(row['check_id'],'complete',{'admissible':False,'retry_allowed':True,
                    'diagnostics':[{'code':'AUTOMATIC_SOURCE_INCONCLUSIVE','grade':'inconclusive'}]})
                return
            deadline=time.monotonic()+177
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_check.automatic_claim'):
                    fresh=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE check_id=?',(row['check_id'],)).fetchone()
                    current=self._automatic(fresh) if fresh else None
                    if (not self._current_owner() or current!=meta or fresh['status']!='accepted'
                            or self.operation.seat.candidate_fingerprint(**selection)!=fp):return
                    # Observation callbacks can replace this consumer; recheck
                    # after them, immediately before durable claim publication.
                    if not self._current_owner():return
                    result=json.loads(fresh['result_json']);meta=dict(meta,phase='claimed',consumer=self.consumer_token)
                    result['_automatic']=meta
                    con.execute("UPDATE conversation_runtime_check_requests SET result_json=?,updated_at=? WHERE check_id=? AND status='accepted'",
                                (json.dumps(result),time.time(),row['check_id']))
            finally:con.close()
            self.current,self.beginning,self.failed=row['check_id'],True,None
            threading.Thread(target=self._begin,args=(row['check_id'],deadline,selection),name='native-check-intent',daemon=True).start()

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
                        replay=self._projection(previous)
                        if replay.get('behavior_witness') is not None:
                            probe,binding=self._owned_probe(con,previous,json.loads(previous['result_json']))
                            if probe is None or not binding:
                                replay['behavior_witness']=None
                                replay['behavior_witness_observation']='unavailable'
                        return replay
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

    def _begin(self,check_id: str,deadline: float | None=None,claimed_selection: dict | None=None) -> None:
        try:
            con=db_driver.connect(str(self.database))
            try:
                row=con.execute("SELECT * FROM conversation_runtime_check_requests WHERE check_id=? AND owner_user_id=1 AND status='accepted'",(check_id,)).fetchone()
                if row is None:
                    raise RuntimeContractError('CHECK_INTENT_INVALID','captured selected intent is unavailable')
                selection=configured_selection(json.loads(row['selection_json']))
            finally:
                con.close()
            meta=self._automatic(row)
            options={}
            if meta is not None:
                if (deadline is None or claimed_selection!=selection or row['request_hash']!=payload_digest(selection)):
                    raise RuntimeContractError('CHECK_INTENT_INVALID','claimed check has no current selected intent')
                options={'deadline':deadline,'expected_fingerprint':meta['expected_fingerprint'],
                         'validate_intent':lambda:self._automatic_edge(check_id,meta['expected_fingerprint'],deadline,selection)}
            self.operation.begin(selection=selection,on_candidate=lambda key:self._bind(check_id,key),**options)
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
                retained_guard=False
                try:self.operation.retained_guard()
                except Exception:retained_guard=True # noqa: BLE001 - unknown codegen cleanup is not safe retry
                with self.operation.owner.lock:
                    empty=((self.operation.future is None or self.operation.future.done()) and not retained and not retained_guard
                           and not self.operation.owner.allocating and not self.operation.owner.closing)
                result: dict={'diagnostics':[{'code':'CLEANUP_PENDING' if retained_guard else 'NATIVE_CHECK_INCONCLUSIVE','grade':'inconclusive'}],
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
                meta=self._automatic(row)
                if meta is not None:
                    result['_automatic']=dict(meta,phase='settled' if state=='complete' else meta['phase'])
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
                row=con.execute('SELECT * FROM conversation_runtime_check_requests WHERE check_id=?',(check_id,)).fetchone()
                meta=self._automatic(row) if row else None
                if meta is not None:
                    if (row['status']!='accepted' or meta['phase']!='claimed' or meta['consumer']!=self.consumer_token
                            or meta['expected_fingerprint']!=fingerprint_key or not self._current_owner()):
                        raise RuntimeContractError('CHECK_INTENT_INVALID','automatic candidate differs from durable dispatch')
                    return
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
            meta=self._automatic(row)
            if meta is not None and (not self._current_owner() or meta['consumer']!=self.consumer_token):
                self._save(check_id,'retained',{'fingerprint':bound,'admissible':False,'retry_allowed':False,
                    'diagnostics':[{'code':'CHECK_CONSUMER_RESTARTED','grade':'inconclusive'}]})
                return
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
            # Diagnostics belong to this exact captured check/probe. They
            # cannot contribute to admission, grades or cleanup decisions.
            observed_probe=observed.get('probe')
            witness=observed.get('behavior_witness')
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
            if 'behavior_witness' in result:result['behavior_witness']=None
            result.pop('behavior_witness_observation',None)
        meta=result.pop('_automatic',None)
        if meta is not None:result['origin']='installed_change'
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
            pending_cleanup=False
            try:self.operation.retained_guard()
            except Exception:pending_cleanup=True # noqa: BLE001 - recovery cannot assert unknown schema exit
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
                    if pending_cleanup:result.update(admissible=False,retry_allowed=False,
                        diagnostics=[{'code':'CLEANUP_PENDING','grade':'inconclusive'}])
                    if con.execute("SELECT 1 FROM conversation_runtime_check_requests WHERE check_id!=? AND status!='complete' LIMIT 1",(target,)).fetchone() or con.execute("SELECT 1 FROM conversation_runtime_probe_jobs WHERE status!='complete' LIMIT 1").fetchone():
                        result['retry_allowed']=False
                meta=self._automatic(row)
                if meta is not None and meta['phase']=='queued' and row['status']=='accepted':
                    result.update(state='accepted',admissible=False,retry_allowed=False,probe=None)
                    return result
                if self.current!=target and row['status']!='complete':
                    result['probe'],binding=self._owned_probe(con,row,json.loads(row['result_json']))
                    probe=result['probe']
                    cleanup=probe.get('cleanup') or {} if probe else {}
                    with self.operation.owner.lock:
                        released=(result.get('fingerprint') not in self.operation.owner.allocating
                                  and result.get('fingerprint') not in self.operation.owner.closing)
                    if (probe and probe['status']=='complete' and probe['phase']=='closed' and released and not pending_cleanup
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
                                  diagnostics=[{'code':'CLEANUP_PENDING' if pending_cleanup else 'CHECK_CONSUMER_RESTARTED','grade':'inconclusive'}])
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
