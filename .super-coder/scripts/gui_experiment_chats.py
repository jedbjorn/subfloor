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
        self.lock=threading.Lock()
        self.stopped=threading.Event()
        self.wake=threading.Event()
        self.thread=threading.Thread(target=self.publish_changes,name='fixture-native-installation',daemon=True)

    def resolve_route(self,harness,model,effort):
        self.seat.ensure_codegen_clean()
        fp=self.seat.candidate_fingerprint(harness,model,effort)
        evidence=self.operation.cache.get(fp,'submission')
        if evidence is None or evidence.grade!='compatible':
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
            model_observed=True,effort_observed=True,catalogue_observed=route.get('catalogue_observed',False))
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
        context,fp,native=self.seat.prepare(cid,generation,checked_fingerprint=checked)
        self.service.require_capability(context.capability_evidence,'submission')
        return context,fp,native

    def changed(self,harness,observation):
        # The observer callback only enqueues; publication has no native launch
        # or inference and uses its own bounded API consumer worker.
        with self.lock:
            self.pending[harness]=observation
        self.wake.set()

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
                            runtime['latest_installed_identity']=installed
                            con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),row['conversation_id']))
                            conversation_native_chats.append_event(con,row['conversation_id'],'capability.observed',{'source':'system','scope':'latest_installation',**installed})
                except Exception: # noqa: BLE001 - failed metadata publication never starts native work
                    with self.lock:
                        self.pending.setdefault(harness,observation)
                finally:
                    con.close()
                for row in rows:
                    conversation_events.notify(row['conversation_id'])
            self.wake.clear()
            self.wake.wait(1)

    def start(self):
        if conversation_native_chats._SERVICE is not None:
            raise RuntimeContractError('FIXTURE_INVALID','another experimental consumer is installed')
        self.service.prepare_context,self.service.route_resolver=self.prepare,self.resolve_route
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
