"""Fixture-only normal Chats installation and checked native route ownership.

The resolver consumes completed synthetic probe observations and matching
behavior coverage. Installed identity diagnostics never replace a running
generation's captured capabilities or immutable context.
"""
from __future__ import annotations

import json
import threading
import time

import conversation_events
import conversation_native_chats
import conversation_native_history
import db_driver
import route_bindings
from conversation_runtime_contract import RuntimeContractError, payload_digest
from conversation_runtime_controller import encoded, observed_native_route
from gui_experiment_probe_owner import NativeProbeOwner


class FixtureChats:
    def __init__(self,operation):
        self.operation,self.seat,self.service=operation,operation.seat,operation.service
        self.database=operation.database
        self.pending={}
        self.last_scan=0.0
        self.lock=threading.Lock()
        self.stopped=threading.Event()
        self.wake=threading.Event()
        self.thread=threading.Thread(target=self.publish_changes,name='fixture-native-installation',daemon=True)

    def resolve_route(self,harness,model,effort):
        self.seat.ensure_codegen_clean()
        fp=self.seat.candidate_fingerprint(harness,model,effort)
        guard=getattr(self.seat,'require_loaded_source',None)
        if guard is not None:guard(fp)
        evidence=self.operation.cache.get(fp,'submission')
        if evidence is None or evidence.grade!='compatible' or self.operation.cache.admission(fp).get('submission')!='compatible':
            code='CAPABILITY_INCOMPATIBLE' if evidence and evidence.grade=='incompatible' else 'CAPABILITY_INCONCLUSIVE'
            raise RuntimeContractError(code,'selected installed identity has no cleanup-bound submission coverage')
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute("SELECT c.*,s.user_id AS shell_owner,j.status AS job_status,j.generation_id AS job_generation,g.state AS generation_state,g.cleanup_json,g.binding_json FROM conversation_runtime_probe_jobs j JOIN conversations c USING(conversation_id) JOIN shells s USING(shell_id) JOIN conversation_runtime_generations g ON g.generation_id=j.generation_id AND g.conversation_id=c.conversation_id AND g.owner_user_id=c.owner_user_id AND g.shell_id=c.shell_id WHERE j.fingerprint_key=?",(fp.key,)).fetchone()
        finally:
            con.close()
        runtime=json.loads(row['runtime_projection']) if row else {}
        cleanup=json.loads(row['cleanup_json']) if row else {}
        captured=json.loads(row['binding_json']) if row else {}
        context=captured.get('context',{})
        selected={'harness':fp.harness,'provider':fp.provider,'model':fp.model,'effort':fp.effort,
                  'driver_revision':fp.driver_revision,'contract_revision':fp.contract_revision,
                  'policy_digest':fp.policy_digest,'permission_mode':'unrestricted',
                  'executable':{'path':str(fp.executable.path),'sha256':fp.executable.sha256,'version':fp.executable.version}}
        if (row is None or row['owner_user_id']!=1 or row['shell_owner']!=1
                or row['runtime_mode']!='native_experiment' or runtime.get('role')!='probe'
                or row['job_status']!='complete' or row['generation_state']!='closed'
                or runtime.get('generation_id')!=row['job_generation'] or runtime.get('state')!='closed'
                or row['creation_request_hash']!=fp.key
                or captured.get('supervision',{}).get('fingerprint')!=fp.key
                or captured.get('supervision',{}).get('implementation_digest')!=fp.implementation_digest
                or any(context.get(key)!=value for key,value in selected.items())
                or context.get('generation_id')!=row['job_generation']
                or context.get('conversation_id')!=row['conversation_id']
                or context.get('owner_user_id')!=row['owner_user_id'] or context.get('shell_id')!=row['shell_id']
                or context.get('worktree')!=row['worktree']
                or (row['harness'],row['provider'],row['model'],row['effort'])!=(fp.harness,fp.provider,fp.model,fp.effort)
                or cleanup.get('unit_verified_exited') is not True or cleanup.get('outcome')!='complete'
                or cleanup.get('native_outcome')!='complete'
                or cleanup.get('unresolved_work') or cleanup.get('unresolved_definitions')):
            raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','completed owned selected-route observation is unavailable')
        route=observed_native_route(runtime.get('native_route'))
        if (route['account_type']!=('chatgpt' if harness=='codex' else 'claude.ai')
                or route['model']!=model or effort not in route['efforts']):
            raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','native selected account/model/effort observation does not match')
        binding=NativeProbeOwner.candidate_binding(fp)
        binding['selector_binding'].update(proof_state='checked_native_selection',
            model_observed=True,effort_observed=True,catalogue_observed=route.get('catalogue_observed',False),
            native_fingerprint=fp.key,native_executable_version=fp.executable.version)
        binding['evidence_digest']=payload_digest({'fingerprint':fp.key,'selected_native_route':route,
            'submission_coverage':sorted(evidence.coverage),'observed_at':evidence.observed_at})
        route_bindings.validate_v2_binding(binding)
        return binding,route_bindings.digest_json(binding)

    def prepare(self,cid,generation):
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute("SELECT * FROM conversations WHERE conversation_id=? AND owner_user_id=1 AND runtime_mode='native_experiment'",(cid,)).fetchone()
        finally:
            con.close()
        runtime=json.loads(row['runtime_projection']) if row else {}
        if (row is None or runtime.get('role')=='probe' or row['state']=='closed' or runtime.get('state')=='closing'
                or runtime.get('generation_id')!=generation):
            raise RuntimeContractError('RUNTIME_NOT_OWNED','ordinary fixture preparation requires an open non-probe chat')
        binding=json.loads(row['route_binding'])
        expected,digest=self.resolve_route(row['harness'],row['model'],row['effort'])
        if binding!=expected or route_bindings.digest_json(binding)!=digest:
            raise RuntimeContractError('NATIVE_ROUTE_CHANGED','installed route/evidence changed before canonical preparation')
        checked=self.seat.candidate_fingerprint(row['harness'],row['model'],row['effort'])
        if binding['catalogue_generation']!=checked.key[:32]:
            raise RuntimeContractError('NATIVE_ROUTE_CHANGED','checked candidate changed before canonical preparation')
        history_proof=None
        con=db_driver.connect(str(self.database))
        try:
            link=conversation_native_history.association(con,cid,row['owner_user_id'])
            if link is not None:
                _,history,_=conversation_native_history.source_history(con,link['source_conversation_id'],row['owner_user_id'])
                history_proof=self.history_admission(history)
                conversation_native_history.prepared_history(con,row,generation,history_proof)
        finally:
            con.close()
        if history_proof is not None and conversation_native_chats._SERVICE is not self.service:
            raise RuntimeContractError('NATIVE_HISTORY_UNAVAILABLE','current experimental owner was withdrawn before preparation')
        options={'history_proof':history_proof} if history_proof is not None else {}
        context,fp,native=self.seat.prepare(cid,generation,checked_fingerprint=checked,**options)
        self.service.require_capability(context.capability_evidence,'submission')
        return context,fp,native

    def history_admission(self,history):
        binding,digest=self.resolve_route(history.harness,history.model,history.effort)
        fp=self.seat.candidate_fingerprint(history.harness,history.model,history.effort)
        evidence=self.operation.cache.get(fp,'history_resume')
        if evidence is None:
            raise RuntimeContractError('NATIVE_HISTORY_UNAVAILABLE','history-specific consumed interface and native behavior have not been proved')
        return conversation_native_history.checked_proof({
            'grade':evidence.grade,'coverage':sorted(evidence.coverage),'fingerprint':fp.key,
            'binding':binding,'binding_digest':digest},history)

    def changed(self,harness,observation):
        # The observer callback only enqueues; publication has no native launch
        # or inference and uses its own bounded API consumer worker.
        with self.lock:
            self.pending[harness]=observation
        self.wake.set()

    def reconcile_automatic(self) -> None:
        """Existing publication worker observes relevant content, not accounts."""
        if conversation_native_chats._SERVICE is not self.service:return
        from conversation_native_checks import configured_selection
        con=db_driver.connect(str(self.database))
        try:
            selections=set()
            for row in con.execute("SELECT c.harness,c.model,c.effort FROM conversations c JOIN shells s USING(shell_id) WHERE c.runtime_mode='native_experiment' AND c.owner_user_id=1 AND s.user_id=1 AND c.state!='closed'"):
                selections.add((row['harness'],row['model'],row['effort']))
            for row in con.execute('SELECT selection_json FROM conversation_runtime_check_requests WHERE owner_user_id=1'):
                selection=json.loads(row[0]);selections.add((selection.get('harness'),selection.get('model'),selection.get('effort')))
        finally:con.close()
        for harness,model,effort in selections:
            selection={'harness':harness,'model':model,'effort':effort}
            try:
                configured_selection(selection)
                fp=self.seat.candidate_fingerprint(**selection)
                grade='inconclusive'
                try:
                    self.resolve_route(harness,model,effort)
                    grade='compatible'
                except RuntimeContractError:
                    self.operation.workflow.enqueue_automatic(selection,fp.key)
                    evidence=self.operation.cache.get(fp,'submission')
                    if evidence and evidence.grade=='incompatible':grade='incompatible'
                ref=self.operation.workflow.automatic_reference(selection,fingerprint=fp.key)
                self.publish_check_reference(selection,ref,fp,grade)
            except RuntimeContractError as exc:
                if exc.code=='NATIVE_EXECUTABLE_INCONCLUSIVE':self.operation.workflow.note_unavailable(harness)
            except (OSError,ValueError):
                continue # Source observation failure cannot prove a credential/setup availability transition.
        self.operation.workflow.dispatch_automatic()

    def publish_check_reference(self,selection: dict,ref: dict | None,fp,grade: str) -> None:
        if conversation_native_chats._SERVICE is not self.service:return
        if self.seat.candidate_fingerprint(**selection)!=fp:return
        con=db_driver.connect(str(self.database));changed=[]
        try:
            with db_driver.write_transaction(con,'native_chat.automatic_check_ref'):
                if conversation_native_chats._SERVICE is not self.service:return
                rows=con.execute("SELECT c.conversation_id,c.runtime_projection FROM conversations c JOIN shells s USING(shell_id) WHERE c.runtime_mode='native_experiment' AND c.owner_user_id=1 AND s.user_id=1 AND c.state!='closed' AND c.harness=? AND c.model=? AND c.effort=?",(selection['harness'],selection['model'],selection['effort'])).fetchall()
                for row in rows:
                    runtime=json.loads(row['runtime_projection']);installed=runtime.get('latest_installed_identity') or {}
                    if installed.get('check_ref')==ref and installed.get('fingerprint')==fp.key and installed.get('capability_grade')==grade:continue
                    installed.update(check_ref=ref,fingerprint=fp.key,capability_grade=grade)
                    runtime['latest_installed_identity']=installed
                    con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),row['conversation_id']))
                    conversation_native_chats.append_event(con,row['conversation_id'],'capability.observed',
                        {'source':'system','scope':'latest_installation','check_ref':ref,'fingerprint':fp.key,'capability_grade':grade})
                    changed.append(row['conversation_id'])
        finally:con.close()
        for cid in changed:conversation_events.notify(cid)

    def publish_changes(self):
        while not self.stopped.is_set():
            with self.lock:
                pending,self.pending=self.pending,{}
            for harness,observation in pending.items():
                installed={'observed_at':time.time(),'identity_grade':observation.grade,
                           'capability_grade':'inconclusive','diagnostic':observation.code or 'MATCHING_CHECK_REQUIRED',
                           'executable':{'path':str(observation.binding.path),'sha256':observation.binding.sha256,'version':observation.binding.version} if observation.binding else None}
                con=db_driver.connect(str(self.database))
                rows=[]
                try:
                    with db_driver.write_transaction(con,'native_chat.latest_install'):
                        rows=con.execute("SELECT conversation_id,runtime_projection FROM conversations WHERE runtime_mode='native_experiment' AND owner_user_id=1 AND harness=? AND state!='closed'",(harness,)).fetchall()
                        for row in rows:
                            runtime=json.loads(row['runtime_projection'])
                            prior=runtime.get('latest_installed_identity') or {}
                            runtime['latest_installed_identity']={**installed,'check_ref':prior.get('check_ref')}
                            con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),row['conversation_id']))
                            conversation_native_chats.append_event(con,row['conversation_id'],'capability.observed',{'source':'system','scope':'latest_installation',**installed})
                except Exception: # noqa: BLE001 - failed metadata publication never starts native work
                    with self.lock:
                        self.pending.setdefault(harness,observation)
                finally:
                    con.close()
                for row in rows:
                    conversation_events.notify(row['conversation_id'])
            if time.monotonic()-self.last_scan>=5:
                self.last_scan=time.monotonic()
                try:self.reconcile_automatic()
                except Exception: # noqa: BLE001 - retained intent, no unknown native replay
                    self.last_scan=time.monotonic()
            self.wake.clear()
            self.wake.wait(1)

    def start(self):
        if conversation_native_chats._SERVICE is not None:
            raise RuntimeContractError('FIXTURE_INVALID','another experimental consumer is installed')
        self.service.prepare_context,self.service.route_resolver=self.prepare,self.resolve_route
        self.service.history_resolver=self.history_admission
        conversation_native_chats._SERVICE=self.service
        try:
            self.thread.start()
            for harness,observer in self.seat.observers.items():
                observer.start(lambda observation,harness=harness:self.changed(harness,observation),interval=5)
            self.service.start()
        except Exception:
            self.close()
            raise

    def close(self):
        self.stopped.set();self.wake.set()
        for observer in self.seat.observers.values():
            observer.close()
        if self.thread.is_alive():self.thread.join(4)
        self.service.shutdown()
        if conversation_native_chats._SERVICE is self.service:
            conversation_native_chats._SERVICE=None
