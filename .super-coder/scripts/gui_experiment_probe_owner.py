"""Fixed synthetic allocation/cleanup for the fixture's finite native checks.

Only the copied supervised API constructs this owner. It never imports a
previous receipt as grades, invokes a host CLI, copies authentication, or
prepares an ordinary chat using a probe's consent. The checker/factory own
the finite behavior; this owner owns its canonical synthetic preparation.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path

import conversation_events
import db_driver
import route_bindings
from conversation_native_chats import NativeChatsService, append_event
from conversation_runtime_checks import CleanupProof, Fingerprint
from conversation_runtime_contract import (
    ProcessIdentity,
    RuntimeContractError,
    RuntimeIdentity,
    StartupConsent,
)
from conversation_runtime_controller import encoded, start_ticks
from conversation_runtime_native_probes import OwnedProbe


class NativeProbeOwner:
    def __init__(self,seat,service: NativeChatsService):
        self.seat,self.service=seat,service
        self.database,self.root=seat.database,seat.root
        self.lock=threading.RLock()
        self.closing: set[str]=set()
        self.allocating: set[str]=set()

    @staticmethod
    def candidate_binding(fingerprint: Fingerprint) -> dict:
        """Pending provenance is usable only by a named finite probe.

        Requested options are candidates, not an advertised catalogue. Actual
        driver account/model/effort observation precedes useful probe input.
        """
        if fingerprint.harness not in {'codex','claude'} or not fingerprint.model or not fingerprint.effort:
            raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','selected native candidate required')
        binding={'contract_version':2,'control_state':'controlled','harness':fingerprint.harness,
                 'requested_model':fingerprint.model,'provider_model':fingerprint.model,
                 'requested_effort':fingerprint.effort,'effective_effort':fingerprint.effort,
                 'native_variant_id':None,'transport':'codex-reasoning-config' if fingerprint.harness=='codex' else 'claude-effort-argument',
                 'catalogue_generation':fingerprint.key[:32],'evidence_digest':fingerprint.key,
                 'selector_binding':{'experimental_native':True,'proof_state':'pending_finite_probe',
                                     'catalogue_observed':False,'model_observed':False,'effort_observed':False},
                 'adapter_metadata':{}}
        route_bindings.validate_v2_binding(binding)
        return binding

    def allocate(self,fingerprint: Fingerprint,capabilities: frozenset[str],deadline: float) -> OwnedProbe:
        with self.lock:
            if fingerprint.key in self.allocating or fingerprint.key in self.closing:
                raise RuntimeContractError('PROBE_ALLOCATION_FENCED','earlier allocation/cleanup remains in progress')
            self.allocating.add(fingerprint.key)
        try:
            return self._allocate(fingerprint,capabilities,deadline)
        finally:
            with self.lock:
                self.allocating.discard(fingerprint.key)

    def _allocate(self,fingerprint: Fingerprint,capabilities: frozenset[str],deadline: float) -> OwnedProbe:
        # Claude's readiness challenge itself performs inference. Pending
        # workspace/no-memory authority is enforced before Driver.start, not
        # by the factory's later post-ready observation check.
        if fingerprint.harness=='claude':
            raise RuntimeContractError('CLAUDE_STARTUP_EVIDENCE_INCONCLUSIVE','workspace and startup evidence prerequisites remain unresolved')
        allowed={'submission','stop_reply','stop_work','stop_work_terminal','stop_work_child'}
        if not capabilities<=allowed or time.monotonic()>=deadline:
            raise RuntimeContractError('PROBE_INVALID','finite supported native probe scope required')
        preparation_owner=self.seat.supervisor.preparation_identity()
        binding=self.candidate_binding(fingerprint)
        generation=uuid.uuid4().hex
        cid='cv_'+generation
        short='fxp'+generation[:20]
        worktree=self.root/'.sc-worktrees'/short
        with self.lock:
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_probe.reserve_chat'):
                    previous=con.execute('SELECT status FROM conversation_runtime_probe_jobs WHERE fingerprint_key=?',(fingerprint.key,)).fetchone()
                    if previous and previous['status']!='complete':
                        raise RuntimeContractError('CLEANUP_PENDING','earlier finite probe ownership remains retained')
                    if fingerprint.key in self.closing:
                        raise RuntimeContractError('PROBE_ALLOCATION_FENCED','cleanup fenced this probe allocation')
                    shell=con.execute("INSERT INTO shells(display_name,shortname,flavor,system_prompt,user_id,api_key) VALUES(?,?,'dev','Synthetic native compatibility probe',1,?)",(short,short,secrets.token_hex(32))).lastrowid
                    projection={'role':'probe','generation_id':generation,'state':'preparing','capabilities':{},
                                'setup':None,'partial':True,'freshness':'unknown','preparation_owner':preparation_owner,
                                'check_deadline':time.time()+max(0,deadline-time.monotonic())}
                    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,route_contract_version,route_binding,runtime_projection) VALUES(?,?,1,?,?,?,?,?,?,?,'native_experiment',2,?,?)",
                        (cid,shell,fingerprint.harness,fingerprint.provider,fingerprint.model,fingerprint.effort,str(worktree),cid,fingerprint.key,encoded(binding),encoded(projection)))
                    con.execute('INSERT OR REPLACE INTO conversation_runtime_probe_jobs VALUES(?,?,?,\'preparing\',?,?)',
                        (fingerprint.key,cid,generation,projection['check_deadline'],time.time()))
                    append_event(con,cid,'conversation.created',{'mode':'native_experiment','role':'probe','generation_id':generation})
            finally:
                con.close()
        # This is a new canonical synthetic slot; it cannot overwrite an
        # existing directory/branch or invoke host-global installation hooks.
        subprocess.run(['git','-C',str(self.root),'worktree','add','--quiet','-b','shell/'+short,str(worktree)],
                       check=True,capture_output=True,timeout=max(.001,min(15,deadline-time.monotonic())))
        granted=set(capabilities)|{'submission'}
        if 'stop_work' in capabilities:
            granted.update({'stop_work_terminal','stop_work_child'})
        grants=tuple(sorted(granted))
        context,actual,native=self.seat.prepare(cid,generation,probe_capabilities=grants)
        with self.lock:
            if fingerprint.key in self.closing or time.monotonic()>=deadline or actual!=fingerprint:
                raise RuntimeContractError('PROBE_ALLOCATION_FENCED','cleanup/deadline/installed identity fenced captured preparation')
            con=db_driver.connect(str(self.database))
            try:
                job=con.execute('SELECT * FROM conversation_runtime_probe_jobs WHERE fingerprint_key=?',(fingerprint.key,)).fetchone()
                chat=con.execute('SELECT state,runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                projection=json.loads(chat['runtime_projection'])
                if (job is None or job['generation_id']!=generation or job['conversation_id']!=cid
                        or job['status']!='preparing' or job['deadline']<=time.time()
                        or chat['state']=='closed' or projection.get('generation_id')!=generation
                        or projection.get('state')!='preparing' or projection.get('role')!='probe'):
                    raise RuntimeContractError('PROBE_ALLOCATION_FENCED','Close/stale job fenced prepared allocation')
            finally:
                con.close()
            self.service.store.reserve(context,native|{'fingerprint':fingerprint.key,'implementation_digest':fingerprint.implementation_digest,'purpose':'finite_probe'})
            self._update(cid,generation,state='starting',preparation_owner=None,partial=False)
            con=db_driver.connect(str(self.database))
            try:
                runtime=json.loads(con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0])
                if runtime.get('state')=='closing':
                    # Transfer a Close accepted immediately before reserve to
                    # the now-canonical generation; no launch follows.
                    version=con.execute('SELECT version FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0]
                    self.service.request_close(con,cid,1,version)
                    raise RuntimeContractError('PROBE_ALLOCATION_FENCED','Close fenced reserved probe before launch')
                if self.service.store.status(generation,1,shell)['close_intent']:
                    raise RuntimeContractError('PROBE_ALLOCATION_FENCED','Close fenced probe before launch')
            finally:
                con.close()
            self.seat.supervisor.launch(generation)
        client,owner,shell_id=self.service.attach(generation)
        # Canonical worktree and private state are siblings beneath the marked
        # synthetic fixture. The boundary is that verified shared seat root;
        # per-probe shell/chat/generation/unit remain independently owned.
        return OwnedProbe(context,client,self.service.store,owner,shell_id,native['unit'],self.root,True,
            observe_marker=lambda marker,end:self.marker(shell_id,marker,end),
            process_identity=lambda pid:self.process(generation,pid),
            on_identity=lambda identity:self.identity(cid,generation,identity),
            on_setup=lambda setup:self.setup(cid,generation,setup))

    def _update(self,cid: str,generation: str,**fields) -> None:
        con=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_probe.projection'):
                row=con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                projection=json.loads(row['runtime_projection'])
                if projection.get('generation_id')!=generation:
                    raise RuntimeContractError('GENERATION_INVALID','probe projection no longer matches captured generation')
                if projection.get('state') in {'closing','closed'} and fields.get('state') not in {'closing','closed'}:
                    return
                projection.update(fields)
                con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(projection),cid))
                append_event(con,cid,'capability.observed',{'generation_id':generation,'role':'probe','grade':'inconclusive',
                    'phase':projection.get('state'),'setup':projection.get('setup')})
        finally:
            con.close()
        conversation_events.notify(cid)

    def setup(self,cid: str,generation: str,setup: StartupConsent|None) -> None:
        import dataclasses
        if setup is not None and setup.generation_id!=generation:
            raise RuntimeContractError('SETUP_INVALID','startup choice belongs to another probe')
        self._update(cid,generation,state='needs_consent' if setup else 'checking',setup=dataclasses.asdict(setup) if setup else None)

    def identity(self,cid: str,generation: str,identity: RuntimeIdentity) -> None:
        # Native observations remain probe diagnostics, never cache admission.
        memory=identity.protocol.get('memory_policy') or {}
        self._update(cid,generation,state='checking',root_id=identity.root_id,native_route=identity.protocol.get('native_route'),
                     memory_policy={key:memory[key] for key in ('generate_memories','use_memories','feature_enabled','root_mode') if key in memory},
                     ready_observation_at=time.time(),native_process={'pid':identity.process.pid,'start_ticks':identity.process.start_ticks} if identity.process else None,
                     setup=None)

    def marker(self,shell: int,marker: str,deadline: float) -> bool:
        if time.monotonic()>=deadline:
            return False
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute('SELECT current_state FROM shells WHERE shell_id=? AND user_id=1',(shell,)).fetchone()
            return row is not None and row['current_state']==marker
        finally:
            con.close()

    def process(self,generation: str,pid: int) -> ProcessIdentity|None:
        if isinstance(pid,bool) or not isinstance(pid,int) or pid<=0:
            return None
        native=next((n for n in self.seat.supervisor.inventory() if n['generation_id']==generation),None)
        group=native.get('control_group') if native else None
        if not group:
            return None
        try:
            before=start_ticks(pid)
            groups=Path(f'/proc/{pid}/cgroup').read_text().splitlines()
            if not any(line.split(':',2)[-1]==group or line.split(':',2)[-1].startswith(group+'/') for line in groups):
                return None
            if start_ticks(pid)!=before:
                return None
            return ProcessIdentity(pid,before)
        except (OSError,ValueError):
            return None

    def cleanup(self,fingerprint: Fingerprint,deadline: float) -> CleanupProof:
        with self.lock:
            self.closing.add(fingerprint.key)
            con=db_driver.connect(str(self.database))
            try:
                row=con.execute('SELECT * FROM conversation_runtime_probe_jobs WHERE fingerprint_key=?',(fingerprint.key,)).fetchone()
            finally:
                con.close()
        if row is None:
            with self.lock:
                if fingerprint.key in self.allocating:
                    return CleanupProof(False,'inconclusive')
                self.closing.discard(fingerprint.key)
            return CleanupProof(True,'complete')  # Allocation finished without creating a synthetic owner.
        generation,cid=row['generation_id'],row['conversation_id']
        con=db_driver.connect(str(self.database))
        try:
            owner=con.execute('SELECT owner_user_id,shell_id FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
            captured=con.execute('SELECT 1 FROM conversation_runtime_generations WHERE generation_id=?',(generation,)).fetchone()
        finally:
            con.close()
        if captured is None:
            self._update(cid,generation,state='closing',setup=None)
            with self.lock:
                if fingerprint.key in self.allocating:
                    # Registration/boot may still finish after this Close.
                    # Only a completed allocation plus scoped cleanup can
                    # certify never-launched resources or release the job.
                    return CleanupProof(False,'inconclusive')
            self.service.finish_preparation(cid,generation)
            con=db_driver.connect(str(self.database))
            try:
                projection=json.loads(con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0])
            finally:
                con.close()
            cleanup=projection.get('preparation_cleanup') or {}
            proof=CleanupProof(cleanup.get('unit_verified_exited') is True,
                'complete' if cleanup.get('never_launched') is True else 'inconclusive')
            if proof.complete and not self.completed(fingerprint,cid,generation):
                return CleanupProof(False,'inconclusive')
            return proof
        try:
            receipt=self.service.store.command_status(generation,owner['owner_user_id'],owner['shell_id'],'probe-close')['receipt']
        except RuntimeContractError:
            receipt={}
        self.service.finish_cleanup(generation,cid,owner['owner_user_id'],owner['shell_id'],receipt)
        cleanup=self.service.store.status(generation,owner['owner_user_id'],owner['shell_id'])['cleanup']
        proof=CleanupProof(cleanup.get('unit_verified_exited') is True,cleanup.get('native_outcome','inconclusive'),
                           tuple(cleanup.get('unresolved_work',())),tuple(cleanup.get('unresolved_definitions',())))
        if proof.complete and not self.completed(fingerprint,cid,generation):
            return CleanupProof(False,'inconclusive')
        return proof

    def completed(self,fingerprint: Fingerprint,cid: str,generation: str) -> bool:
        with self.lock:
            if fingerprint.key in self.allocating:
                return False
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_probe.completed'):
                    updated=con.execute("UPDATE conversation_runtime_probe_jobs SET status='complete',updated_at=? WHERE fingerprint_key=? AND conversation_id=? AND generation_id=?",
                                        (time.time(),fingerprint.key,cid,generation))
                    if updated.rowcount!=1:
                        return False
            finally:
                con.close()
            self.closing.discard(fingerprint.key)
            return True
