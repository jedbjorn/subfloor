"""Fixed native preparation inside the disposable API's supervised boundary.

This owner never installs a host wrapper, changes global HOME/PATH, or selects
credentials. Canonical launch preparation continues to see the masked fixture
home. Only immutable native launch contexts receive the explicitly captured
login-home/executable paths, and all engine/MCP identities remain synthetic.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import db_driver
import route_bindings
import run
from conversation_boot import BootDirective
from conversation_runtime_checks import EvidenceCache, ExecutableObserver, Fingerprint
from conversation_runtime_contract import (
    RuntimeContext,
    RuntimeContractError,
    payload_digest,
)
from gui_experiment_readiness import prepared_plan


class NativeFixtureSeat:
    def __init__(self, *, database: Path, root: Path, supervisor,
                 native_bindings: Mapping[str,str], cache: EvidenceCache):
        self.database,self.root,self.supervisor,self.cache=database,root,supervisor,cache
        if (database.resolve()!=root.resolve()/'.super-coder/shell_db.db'
                or run.ENGINE.resolve()!=root.resolve()/'.super-coder'
                or Path(run.DB_PATH).resolve()!=database.resolve()):
            raise RuntimeContractError('FIXTURE_INVALID','copied synthetic preparation boundary required')
        if set(native_bindings)-{'HOME','CODEX_HOME','CODEX','CLAUDE'}:
            raise RuntimeContractError('CONTEXT_INVALID','unknown native path binding')
        self.native_bindings=dict(native_bindings)
        for value in self.native_bindings.values():
            if value and (not Path(value).is_absolute() or any(c in value for c in '\n\r\0')):
                raise RuntimeContractError('CONTEXT_INVALID','absolute finite native paths required')
        self.observers={}
        for harness in ('codex','claude'):
            path=self.native_bindings.get(harness.upper())
            if path:
                self.observers[harness]=ExecutableObserver(Path(path),version_reader=self._version)

    def native_environment(self,harness: str) -> dict[str,str]:
        home=self.native_bindings.get('HOME')
        executable=self.native_bindings.get(harness.upper())
        if not home or not executable:
            raise RuntimeContractError('NATIVE_LOGIN_INCONCLUSIVE','owner native login paths are unavailable')
        env={'HOME':home,'PATH':str(Path(executable).parent)+os.pathsep+os.defpath}
        if harness=='codex':
            env['CODEX_HOME']=self.native_bindings.get('CODEX_HOME') or str(Path(home)/'.codex')
        # Native Node/Python helpers are pinned to prepared source/tooling;
        # ordinary server catalogue reads never receive these environment values.
        env['SC_PYTHON']=sys.executable
        return env

    def _version(self,path: Path,deadline: float) -> str:
        harness=next((name for name,value in self.native_bindings.items()
                      if name in {'CODEX','CLAUDE'} and Path(value).resolve()==path),None)
        if harness is None:
            raise RuntimeContractError('EXECUTABLE_INVALID','native executable is outside captured bindings')
        result=subprocess.run([str(path),'--version'],env=self.native_environment(harness.lower()),
                              capture_output=True,text=True,timeout=max(.001,min(deadline-time.monotonic(),3)),check=False)
        if result.returncode or not result.stdout.strip() or len(result.stdout)>512:
            raise RuntimeContractError('NATIVE_VERSION_INCONCLUSIVE','bounded native version observation unavailable')
        return result.stdout.splitlines()[0].strip()

    def implementation_digest(self,harness: str) -> str:
        scripts=self.root/'.super-coder/scripts'
        paths=[scripts/name for name in ('conversation_runtime_contract.py','conversation_runtime_controller.py',
               'conversation_runtime.py','conversation_runtime_checks.py','gui_experiment_native_seat.py',
               'gui_experiment_probe_owner.py','gui_experiment_runtime.py','conversation_runtime_native_probes.py','conversation_native_chats.py',
               'gui_experiment_readiness.py','conversation_boot.py','run.py','route_transport.py',
               'execution_view.py','execution_view_exec.py')]
        paths.extend([scripts/'conversation_adapters'/f'{harness}_runtime.py',
                      self.root/'.super-coder/api/route_bindings.py',self.root/'fixture_bootstrap.py',
                      self.root/'.super-coder/adapters'/harness/'adapter.json'])
        if harness=='claude':
            assets=self.root/'.super-coder/assets/runtime/claude'
            paths.extend(p for p in assets.iterdir() if p.is_file() and not p.is_symlink())
        content=[]
        for path in sorted(paths):
            if not path.is_file() or path.is_symlink():
                raise RuntimeContractError('SOURCE_INVALID','captured implementation is unavailable')
            content.append((str(path.relative_to(self.root)),hashlib.sha256(path.read_bytes()).hexdigest()))
        return payload_digest({'source_files':content})

    def settings_digest(self,harness: str) -> str:
        adapter=run.load_adapter(harness)
        # Dynamic owner/chat/boot/port values are intentionally absent. The
        # canonical policy and fixed fixture tool schema remain part of identity.
        return payload_digest({'adapter':adapter,'fixture_mcp':{'revision':1,'tools':['fixture_identity','fixture_state']}})

    def candidate_fingerprint(self,harness: str,model: str,effort: str) -> Fingerprint:
        """Capture requested transport identity, without claiming availability."""
        if (harness not in {'codex','claude'} or not isinstance(model,str) or not 1<=len(model)<=255
                or not isinstance(effort,str) or not 1<=len(effort)<=255):
            raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','bounded selected candidate required')
        observer=self.observers.get(harness)
        observed=observer.observe() if observer else None
        if observed is None or observed.binding is None:
            raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','candidate installed identity unavailable')
        driver=importlib.import_module(f'conversation_adapters.{harness}_runtime')
        revision=driver.DRIVER_REVISION if harness=='codex' else driver.REVISION
        policy=self.settings_digest(harness)
        return Fingerprint(harness,observed.binding,revision,policy,'openai' if harness=='codex' else 'anthropic',
                           model,effort,policy,self.implementation_digest(harness))

    def prepare(self,conversation_id: str,generation_id: str, *, probe_capabilities: tuple[str,...]=()) -> tuple[RuntimeContext,Fingerprint,dict]:
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute('SELECT c.*,s.shortname,s.flavor,s.user_id AS shell_owner FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
            if (row is None or row['owner_user_id']!=1 or row['shell_owner']!=1
                    or row['state']=='closed' or row['runtime_mode']!='native_experiment'
                    or row['harness'] not in {'codex','claude'}):
                raise RuntimeContractError('RUNTIME_NOT_OWNED','opted-in synthetic chat required')
            expected=run.shell_work_dir(row['shortname'],row['flavor'],root=self.root).absolute()
            selected=Path(row['worktree']).absolute()
            if (selected!=expected or self.root.resolve() not in selected.resolve().parents
                    or not selected.is_dir()):
                raise RuntimeContractError('WORKTREE_INVALID','canonical selected worktree escaped the fixture')
            # Check every component before the canonical start can repair Git
            # links, emit boot/configuration, or mutate the session archive.
            current=selected
            while current!=self.root.absolute():
                if current.is_symlink():
                    raise RuntimeContractError('WORKTREE_INVALID','canonical worktree contains an aliased path')
                current=current.parent
            if selected.resolve()!=selected:
                raise RuntimeContractError('WORKTREE_INVALID','canonical worktree has stale or aliased containment')
            binding=json.loads(row['route_binding'])
            route_bindings.validate_v2_binding(binding)
            if binding['selector_binding'].get('proof_state')=='pending_finite_probe':
                job=con.execute('SELECT * FROM conversation_runtime_probe_jobs WHERE conversation_id=? AND generation_id=?',
                                (conversation_id,generation_id)).fetchone()
                if (not probe_capabilities or 'submission' not in probe_capabilities
                        or json.loads(row['runtime_projection']).get('role')!='probe'
                        or json.loads(row['runtime_projection']).get('generation_id')!=generation_id
                        or json.loads(row['runtime_projection']).get('state') in {'closing','closed','preparation_inconclusive'}
                        or job is None or job['status']!='preparing' or job['deadline']<=time.time()
                        or job['fingerprint_key']!=binding['evidence_digest']):
                    raise RuntimeContractError('PROBE_ROUTE_ONLY','pending native selection is exclusive to its registered finite probe')
            digest=route_bindings.digest_json(binding)
            if (binding['harness'],binding['requested_model'],binding['requested_effort'])!=(row['harness'],row['model'],row['effort']):
                raise RuntimeContractError('ROUTE_INVALID','stored native route differs from captured chat')
            initial=con.execute('SELECT 1 FROM conversation_boot_snapshots WHERE conversation_id=?',(conversation_id,)).fetchone() is None
        finally:
            con.close()
        harness=row['harness']
        observer=self.observers.get(harness)
        if observer is None:
            raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','native executable is not available in the owned seat')
        observed=observer.observe()
        if observed.binding is None:
            raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','installed native identity observation is unavailable')
        # Durable fixture resources precede canonical archive/boot/DB mutations.
        native=self.supervisor.register(generation_id,harness)
        if initial:
            run.prepare_launch(shell_id=row['shell_id'],harness=harness,model=row['model'],effort=row['effort'],
                headless_prompt='native owned preparation; never dispatch',conversation_owned=True,
                route_binding=binding,binding_digest=digest,boot=BootDirective(conversation_id,'start'))
        plan=prepared_plan(database=self.database,root=self.root,conversation_id=conversation_id,
                           shell_id=row['shell_id'],harness=harness)
        if plan.argv or plan.model!=row['model'] or plan.effort!=row['effort']:
            raise RuntimeContractError('PREPARATION_INVALID','canonical native route preparation differs')
        adapter=run.load_adapter(harness)
        flags=adapter.get('launch_flags',[])
        expected_flags=['--sandbox','danger-full-access','--ask-for-approval','never'] if harness=='codex' else ['--dangerously-skip-permissions']
        if flags!=expected_flags:
            raise RuntimeContractError('PERMISSION_INCONCLUSIVE','canonical prepared permission policy changed')
        mcp=run.managed_mcp_injection(adapter,row['shortname'])
        if not mcp or mcp['name']!='browser' or mcp['url']!=plan.env['SC_API_BASE']+'/mcp/'+row['shortname'].upper():
            raise RuntimeContractError('MCP_INVALID','canonical native injection is outside the synthetic fixture')
        args=tuple(mcp['launch_args']);files: tuple[Path,...]=()
        if harness=='claude':
            if len(args)!=2 or args[0]!='--mcp-config':
                raise RuntimeContractError('MCP_INVALID','canonical Claude inline MCP composition changed')
            config=json.loads(args[1])
            path=Path(native['root'])/'managed-mcp.json'
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:json.dump(config,stream)
            files=(path,);args=()
        driver=importlib.import_module(f'conversation_adapters.{harness}_runtime')
        revision=driver.DRIVER_REVISION if harness=='codex' else driver.REVISION
        policy=self.settings_digest(harness)
        native_env=self.native_environment(harness)
        native_env['PATH']=plan.env.get('PATH',os.defpath)+os.pathsep+native_env['PATH']
        context=RuntimeContext(generation_id,conversation_id,row['shell_id'],1,harness,Path(native['root']),Path(plan.cwd),
            observed.binding,revision,hashlib.sha256(plan.boot_content.encode()).hexdigest(),policy,'unrestricted',
            provider=row['provider'],model=plan.model,effort=plan.effort,boot_content=plan.boot_content,
            execution_prefix=tuple(plan.execution_view.prefix),managed_mcp_args=args,managed_mcp_files=files,
            env=plan.env|native_env,probe_capabilities=probe_capabilities,
            controller_endpoint=Path(native['endpoint']))
        fingerprint=Fingerprint.capture(context,settings_digest=policy,implementation_digest=self.implementation_digest(harness))
        context=dataclasses.replace(context,capability_evidence=self.cache.admission(fingerprint))
        return context,fingerprint,native
