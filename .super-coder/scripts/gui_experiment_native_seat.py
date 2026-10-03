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
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import conversation_native_chats
import conversation_native_history
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


def _dependency_process(pid: int) -> tuple[int,str,int]:
    fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return int(fields[19]),fields[0],int(fields[2])


def _dependency_cgroup(pid: int) -> str:
    return next(line[3:] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if line.startswith('0::/'))


def _dependency_group_live(group: int,cgroup: str) -> bool:
    payload=(Path('/sys/fs/cgroup')/cgroup.lstrip('/')/'cgroup.procs').read_bytes()
    if len(payload)>65536 or len(payload.split())>128:
        raise ValueError('owned dependency process observation exceeded bounds')
    for raw in payload.split():
        try:_,state,pgrp=_dependency_process(int(raw))
        except FileNotFoundError:continue
        if pgrp==group and state!='Z':return True
    return False


def _reap_dependency_group(process,ticks: int,cgroup: str,deadline: float) -> None:
    """Keep the unreaped root as the PID/group identity anchor through stop."""
    current,_,group=_dependency_process(process.pid)
    if current!=ticks or group!=process.pid or _dependency_cgroup(process.pid)!=cgroup:
        raise ValueError('owned dependency group identity changed')
    for sig,window in ((signal.SIGTERM,.3),(signal.SIGKILL,.4)):
        if not _dependency_group_live(process.pid,cgroup):break
        os.killpg(process.pid,sig)
        until=min(deadline,time.monotonic()+window)
        while time.monotonic()<until and _dependency_group_live(process.pid,cgroup):time.sleep(.01)
    if _dependency_group_live(process.pid,cgroup):
        raise ValueError('owned dependency group remains live')
    process.wait(timeout=max(.001,deadline-time.monotonic()))


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
        self.assets_lock=threading.Lock()
        self.claude_assets_ready=False
        self.schema_cleanup_pending=False
        for value in self.native_bindings.values():
            if value and (not Path(value).is_absolute() or any(c in value for c in '\n\r\0')):
                raise RuntimeContractError('CONTEXT_INVALID','absolute finite native paths required')
        self.source_lock=threading.RLock()
        self.source_cache: dict[str,tuple[list[tuple],str]]={}
        self.loaded_implementations: dict[str,str|None]={}
        self.observers={}
        for harness in ('codex','claude'):
            path=self.native_bindings.get(harness.upper())
            if path:
                self.observers[harness]=ExecutableObserver(Path(path),version_reader=self._version)
                # The copied source is immutable for this API lifetime. Disk
                # edits are observed, but are not an in-process driver reload.
                try:self.loaded_implementations[harness]=self.implementation_digest(harness)
                except (OSError,RuntimeContractError):self.loaded_implementations[harness]=None

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

    def implementation_files(self,harness: str) -> list[Path]:
        scripts=self.root/'.super-coder/scripts'
        paths=[scripts/name for name in ('conversation_runtime_contract.py','conversation_runtime_controller.py',
               'conversation_runtime.py','conversation_runtime_checks.py','gui_experiment_native_seat.py',
               'gui_experiment_probe_owner.py','gui_experiment_runtime.py','gui_experiment_chats.py','conversation_runtime_native_probes.py','conversation_native_chats.py','conversation_native_checks.py',
               'gui_experiment_readiness.py','conversation_boot.py','run.py','route_transport.py',
               'execution_view.py','execution_view_exec.py','conversation_native_history.py',
               'conversation_history_baseline.py')]
        if harness=='codex':paths.extend(scripts/name for name in (
            'conversation_runtime_codex_schema.py','conversation_runtime_codex_codegen.py'))
        paths.extend([scripts/'conversation_adapters'/f'{harness}_runtime.py',
                      self.root/'.super-coder/api/route_bindings.py',self.root/'fixture_bootstrap.py',
                      self.root/'.super-coder/adapters'/harness/'adapter.json'])
        if harness=='claude':
            assets=self.root/'.super-coder/assets/runtime/claude'
            paths.extend(p for p in assets.iterdir() if p.is_file() and not p.is_symlink())
        return sorted(paths)

    def implementation_digest(self,harness: str) -> str:
        with self.source_lock:
            paths=self.implementation_files(harness)
            metadata=[]
            for path in paths:
                if not path.is_file() or path.is_symlink():
                    raise RuntimeContractError('SOURCE_INVALID','captured implementation is unavailable')
                info=path.stat()
                metadata.append((str(path),info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns))
            cached=self.source_cache.get(harness)
            if cached is not None and cached[0]==metadata:return cached[1]
            content=[(str(path.relative_to(self.root)),hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths]
            if any((str(path),info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=before
                   for path,before in zip(paths,metadata,strict=True) for info in [path.stat()]):
                raise RuntimeContractError('SOURCE_CHANGED','implementation changed during observation')
            digest=payload_digest({'source_files':content})
            self.source_cache[harness]=(metadata,digest)
            return digest

    def require_loaded_source(self,fingerprint: Fingerprint) -> None:
        if (self.loaded_implementations.get(fingerprint.harness)!=fingerprint.implementation_digest
                or self.implementation_digest(fingerprint.harness)!=fingerprint.implementation_digest):
            raise RuntimeContractError('LOADED_SOURCE_CHANGED','observed content needs a new copied API source lifetime')

    def probe_capacity(self,harness: str, *, exclude: str | None=None) -> int:
        """Count retained roots and child reservations, never stop user work."""
        con=db_driver.connect(str(self.database))
        try:
            return self._available_roots(con,harness,exclude=exclude)
        except (ValueError,TypeError,AttributeError,KeyError) as exc:
            raise RuntimeContractError('PROBE_CAPACITY_PENDING','retained root/child occupancy is inconclusive') from exc
        finally:con.close()

    def _available_roots(self,con,harness: str, *, exclude: str | None=None) -> int:
        units={item['generation_id'] for item in self.supervisor.inventory()
               if item.get('harness')==harness and item.get('os_cleanup',{}).get('complete') is not True
               and item.get('generation_id')!=exclude}
        children=set()
        rows=con.execute("SELECT c.conversation_id,c.runtime_projection,g.generation_id,g.cleanup_json FROM conversations c LEFT JOIN conversation_runtime_generations g USING(conversation_id) WHERE c.runtime_mode='native_experiment' AND c.harness=?",(harness,)).fetchall()
        for row in rows:
            runtime=json.loads(row['runtime_projection'])
            gid=row['generation_id'] or runtime.get('generation_id')
            if not gid or gid==exclude:continue
            cleanup=json.loads(row['cleanup_json']) if row['cleanup_json'] else runtime.get('preparation_cleanup',{})
            clean=(cleanup.get('unit_verified_exited') is True and cleanup.get('outcome')=='complete'
                   and (cleanup.get('native_outcome')=='complete' or cleanup.get('never_launched') is True)
                   and cleanup.get('unresolved_work')==[] and cleanup.get('unresolved_definitions')==[])
            if clean:continue
            units.add(gid)
            if runtime.get('probe_child_reserved') is True:children.add((gid,'reserved'))
            for work in con.execute('SELECT projection_json FROM conversation_runtime_work WHERE generation_id=?',(gid,)):
                event=json.loads(work[0]);ref=event.get('reference') or {};data=event.get('data') or {}
                if data.get('kind')=='child':
                    children.add((gid,ref.get('thread_id') or 'unknown'))
        # A reserved child and its later actual observation are one slot.
        child_count=sum(max(1,len({thread for generation,thread in children if generation==gid and thread!='reserved'}))
                        for gid in {generation for generation,_ in children})
        return max(0,2-len(units)-child_count)

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

    def observe_native_schema(self,fingerprint: Fingerprint,deadline: float):
        """Fixed zero-account codegen, within the marked API's remaining budget."""
        from conversation_runtime_codex_codegen import make_owned_codegen_runner
        from conversation_runtime_codex_schema import observe_codex_schema
        deadline=min(deadline,time.monotonic()+20)
        self.ensure_codegen_clean()
        if (fingerprint.harness!='codex' or not isinstance(fingerprint.model,str)
                or not isinstance(fingerprint.effort,str) or time.monotonic()+2>=deadline):
            raise RuntimeContractError('NATIVE_SCHEMA_UNALLOCATED','bounded schema prerequisite unavailable before allocation')
        def guard():
            return self.supervisor.preparation_identity(deadline=deadline)
        try:
            owner=guard()
            if self.candidate_fingerprint('codex',fingerprint.model,fingerprint.effort)!=fingerprint or time.monotonic()+2>=deadline:
                raise RuntimeContractError('NATIVE_SCHEMA_UNALLOCATED','captured schema identity changed before allocation')
            parent=self.root/'runtime'
            guard()
            parent.mkdir(mode=0o700,exist_ok=True)
            info=parent.lstat()
            if (parent.resolve()!=parent or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700
                    or time.monotonic()+2>=deadline):
                raise RuntimeContractError('NATIVE_SCHEMA_UNALLOCATED','private schema boundary unavailable before allocation')
            allocation=uuid.uuid4().hex
            root=parent/f'codegen-{allocation}'
            guard()
            if time.monotonic()+2>=deadline:
                raise RuntimeContractError('NATIVE_SCHEMA_UNALLOCATED','schema budget expired before allocation')
        except (OSError,RuntimeError,ValueError) as exc:
            raise RuntimeContractError('NATIVE_SCHEMA_UNALLOCATED','schema prerequisite failed before allocation') from exc
        self.schema_cleanup_pending=True
        self.supervisor.begin_codegen(allocation,deadline=deadline)
        root.mkdir(mode=0o700)
        runner=make_owned_codegen_runner(fingerprint.executable,root,guard,
            record_child=lambda process,cgroup:self.supervisor.record_codegen_child(process,cgroup,deadline=deadline-2))
        clean=False
        try:
            observed=observe_codex_schema(fingerprint.executable,root/'schema',effort=fingerprint.effort,
                                         run_owned=runner,deadline=deadline-2)
        finally:
            clean=runner.cleanup(deadline)
        receipt=dict(runner.receipt)
        receipt['cleanup_complete']=clean
        if (not clean or not self.supervisor.finish_codegen(allocation,receipt,deadline=deadline)
                or guard()!=owner or time.monotonic()>=deadline
                or self.candidate_fingerprint('codex',fingerprint.model,fingerprint.effort)!=fingerprint):
            raise RuntimeContractError('NATIVE_SCHEMA_INCONCLUSIVE','schema ownership or cleanup remains inconclusive')
        self.schema_cleanup_pending=False
        return observed,receipt

    def ensure_codegen_clean(self) -> None:
        if self.schema_cleanup_pending or not self.supervisor.codegen_clean():
            raise RuntimeContractError('CLEANUP_PENDING','previous codegen ownership requires verified cleanup or fixture teardown')

    def selected_worktree(self,row, *, allow_missing: bool=False) -> Path:
        expected=run.shell_work_dir(row['shortname'],row['flavor'],root=self.root).absolute()
        selected=Path(row['worktree']).absolute()
        if selected!=expected or self.root.resolve() not in selected.resolve().parents or (not selected.is_dir() and not (allow_missing and not selected.exists())):
            raise RuntimeContractError('WORKTREE_INVALID','canonical selected worktree escaped the fixture')
        current=selected
        while current!=self.root.absolute():
            if current.is_symlink():
                raise RuntimeContractError('WORKTREE_INVALID','canonical worktree contains an aliased path')
            current=current.parent
        if selected.resolve()!=selected:
            raise RuntimeContractError('WORKTREE_INVALID','canonical worktree has stale or aliased containment')
        return selected

    def claude_startup_prerequisite(self,fingerprint: Fingerprint,deadline: float) -> dict:
        """Configured prerequisites only; the driver observes its owned hook.

        Source/configuration evidence is never an effective-memory observation
        or capability grade. Actual SessionStart/config/hook checks remain in
        the captured driver before its first readiness nonce and cached input.
        """
        self.supervisor.preparation_identity()
        if (fingerprint.harness!='claude' or fingerprint.provider!='anthropic'
                or not fingerprint.model or not fingerprint.effort or time.monotonic()>=deadline):
            raise RuntimeContractError('CLAUDE_STARTUP_EVIDENCE_INCONCLUSIVE','captured configured Claude candidate unavailable')
        if fingerprint!=self.candidate_fingerprint('claude',fingerprint.model,fingerprint.effort):
            raise RuntimeContractError('CLAUDE_STARTUP_EVIDENCE_INCONCLUSIVE','captured configured Claude candidate changed')
        if run.load_adapter('claude').get('launch_flags')!=['--dangerously-skip-permissions']:
            raise RuntimeContractError('PERMISSION_INCONCLUSIVE','canonical Claude permission policy changed')
        driver=importlib.import_module('conversation_adapters.claude_runtime')
        source=driver._memory_disable_source(fingerprint.executable.path,fingerprint.executable.sha256,deadline=deadline)
        self.prepare_claude_assets(deadline)
        if time.monotonic()>=deadline or fingerprint!=self.candidate_fingerprint('claude',fingerprint.model,fingerprint.effort):
            raise RuntimeContractError('CLAUDE_STARTUP_EVIDENCE_INCONCLUSIVE','captured implementation changed during prerequisites')
        return {'state':'configured_pending_owned_hook','evidence_level':'configuration_source_flag_inference',
                'effective_telemetry':False,'executable_sha256':fingerprint.executable.sha256,
                'implementation_digest':fingerprint.implementation_digest,'source_condition_sha256':source,
                'required_inherited_disable_flag':'1','required_auto_memory_enabled_setting':False}

    def prepare_claude_assets(self,deadline: float) -> None:
        """One fixed pinned dependency operation inside the marked API unit."""
        if time.monotonic()+1>=deadline:
            raise RuntimeContractError('CHANNEL_UNAVAILABLE','dependency deadline has no cleanup reserve')
        if not self.assets_lock.acquire(timeout=max(0,min(30,deadline-time.monotonic()))):
            raise RuntimeContractError('CHANNEL_UNAVAILABLE','pinned dependency preparation is busy')
        try:
            owner=self.supervisor.preparation_identity()
            assets=self.root/'.super-coder/assets/runtime/claude'
            if assets.is_symlink() or assets.resolve()!=assets or self.root.resolve() not in assets.resolve().parents:
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','copied channel assets escaped the fixture')
            if any((assets/name).is_symlink() for name in ('package.json','package-lock.json')):
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','captured channel manifests are aliased')
            package=json.loads((assets/'package.json').read_text())
            lock=json.loads((assets/'package-lock.json').read_text())
            if (package.get('dependencies')!={'@modelcontextprotocol/sdk':'1.31.0'}
                    or lock.get('packages',{}).get('node_modules/@modelcontextprotocol/sdk',{}).get('version')!='1.31.0'):
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','captured pinned SDK composition changed')
            node=shutil.which('node',path=os.defpath)
            npm=shutil.which('npm',path=os.defpath)
            home=self.root/'home'
            if node is None or npm is None or home.is_symlink() or home.resolve()!=home:
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','fixed fixture Node/npm paths unavailable')
            env={'HOME':str(home),'PATH':os.defpath,'npm_config_cache':str(home/'.npm-cache'),
                 'npm_config_userconfig':str(home/'no-npm-user-config'),
                 'npm_config_globalconfig':str(home/'no-npm-global-config')}
            if self.supervisor.preparation_identity()!=owner or time.monotonic()+1>=deadline:
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','owned dependency deadline/identity expired')
            version=subprocess.run([node,'--version'],env=env,capture_output=True,text=True,check=False,
                timeout=min(3,deadline-time.monotonic()-1))
            if version.returncode or len(version.stdout)>64 or int(version.stdout.strip().removeprefix('v').split('.')[0])<22:
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','pinned channel requires Node 22 or newer')
            installed=assets/'node_modules/@modelcontextprotocol/sdk/package.json'
            if any(path.is_symlink() for path in (installed,*installed.parents) if path!=assets and assets in path.parents):
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','copied dependencies are aliased')
            if self.supervisor.preparation_identity()!=owner or time.monotonic()+1>=deadline:
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','owned dependency deadline/identity expired')
            if self.claude_assets_ready and installed.is_file() and not installed.is_symlink() and json.loads(installed.read_text()).get('version')=='1.31.0':
                if self.supervisor.preparation_identity()!=owner or time.monotonic()+1>=deadline:
                    raise RuntimeContractError('CHANNEL_UNAVAILABLE','cached dependency ownership/deadline expired')
                return
            if (assets/'node_modules').is_symlink():
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','copied dependency root is aliased')
            # No lifecycle scripts, host npm configuration, credential values,
            # or global install. The child remains in this registered API unit.
            before=hashlib.sha256((assets/'package-lock.json').read_bytes()).hexdigest()
            if self.supervisor.preparation_identity()!=owner or time.monotonic()+1>=deadline:
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','owned dependency deadline/identity expired')
            process=subprocess.Popen([npm,'ci','--omit=dev','--ignore-scripts','--no-audit','--no-fund'],cwd=assets,env=env,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            ticks,_,group=_dependency_process(process.pid)
            cgroup=_dependency_cgroup(process.pid)
            status=None
            stop_deadline=min(deadline,time.monotonic()+30)
            try:
                if group!=process.pid or cgroup!=owner['control_group']:
                    raise ValueError('dependency child escaped the owned API unit')
                while time.monotonic()+1<stop_deadline:
                    exited=os.waitid(os.P_PID,process.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
                    if exited is not None:
                        status=exited.si_status if exited.si_code==os.CLD_EXITED else -1
                        break
                    time.sleep(.01)
            finally:
                _reap_dependency_group(process,ticks,cgroup,stop_deadline)
            if (status!=0 or time.monotonic()>=deadline or self.supervisor.preparation_identity()!=owner
                    or hashlib.sha256((assets/'package-lock.json').read_bytes()).hexdigest()!=before
                    or not installed.is_file() or installed.is_symlink()
                    or json.loads(installed.read_text()).get('version')!='1.31.0'):
                raise RuntimeContractError('CHANNEL_UNAVAILABLE','fixed pinned dependency preparation did not complete')
            self.claude_assets_ready=True
        except (OSError,ValueError,IndexError,StopIteration,subprocess.TimeoutExpired) as exc:
            raise RuntimeContractError('CHANNEL_UNAVAILABLE','bounded copied dependency prerequisite unavailable') from exc
        finally:
            self.assets_lock.release()

    def prepare(self,conversation_id: str,generation_id: str, *, probe_capabilities: tuple[str,...]=(),
                checked_fingerprint: Fingerprint|None=None,history_proof: dict|None=None) -> tuple[RuntimeContext,Fingerprint,dict]:
        self.ensure_codegen_clean()
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute('SELECT c.*,s.shortname,s.flavor,s.user_id AS shell_owner FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
            if (row is None or row['owner_user_id']!=1 or row['shell_owner']!=1
                    or row['state']=='closed' or row['runtime_mode']!='native_experiment'
                    or row['harness'] not in {'codex','claude'}):
                raise RuntimeContractError('RUNTIME_NOT_OWNED','opted-in synthetic chat required')
            # Check every component before the canonical start can repair Git
            # links, emit boot/configuration, or mutate the session archive.
            link=conversation_native_history.association(con,conversation_id,row['owner_user_id'])
            history=None
            history_service=conversation_native_chats._SERVICE
            if link is not None:
                if history_proof is None or probe_capabilities or checked_fingerprint is None:
                    raise RuntimeContractError('HISTORY_UNAVAILABLE','current history-specific ordinary admission is required before preparation')
                history=conversation_native_history.prepared_history(con,row,generation_id,history_proof)
                if (history_service is None or history_service.stopped.is_set()
                        or Path(history_service.database).resolve()!=self.database.resolve()
                        or history_service.history_admission(history)!=history_proof):
                    raise RuntimeContractError('HISTORY_UNAVAILABLE','current owned history preparation service is unavailable')
            elif history_proof is not None:
                raise RuntimeContractError('HISTORY_NOT_OWNED','history proof has no canonical continuation association')
            captured_worktree=self.selected_worktree(row,allow_missing=history is not None)
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
            else:
                runtime=json.loads(row['runtime_projection'])
                if runtime.get('role')=='probe' or runtime.get('generation_id')!=generation_id or runtime.get('state')!='preparing':
                    raise RuntimeContractError('RUNTIME_CLOSING','ordinary preparation no longer owns this preparing generation')
            digest=route_bindings.digest_json(binding)
            if (binding['harness'],binding['requested_model'],binding['requested_effort'])!=(row['harness'],row['model'],row['effort']):
                raise RuntimeContractError('ROUTE_INVALID','stored native route differs from captured chat')
            initial=con.execute('SELECT 1 FROM conversation_boot_snapshots WHERE conversation_id=?',(conversation_id,)).fetchone() is None
        finally:
            con.close()
        def recheck_history(con, *, observe_proof=True):
            if history is None:
                return
            if history_proof is None or history_service is None:
                raise RuntimeContractError('HISTORY_UNAVAILABLE','captured history owner/proof is required')
            # The proof owner can block. It cannot authorize writes using the
            # conversation or shell snapshot from before that callback.
            if observe_proof and history_service.history_admission(history)!=history_proof:
                raise RuntimeContractError('HISTORY_CHANGED','current history proof changed before preparation')
            fresh=con.execute('SELECT c.*,s.shortname,s.flavor,s.user_id AS shell_owner,s.is_deleted AS shell_deleted FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
            runtime=json.loads(fresh['runtime_projection']) if fresh else {}
            if (fresh is None or fresh['owner_user_id']!=1 or fresh['shell_owner']!=1 or fresh['shell_deleted']
                    or fresh['state']=='closed' or fresh['runtime_mode']!='native_experiment'
                    or runtime.get('generation_id')!=generation_id or runtime.get('state')!='preparing'
                    or runtime.get('role')!='ordinary'
                    or runtime.get('preparation_owner')!=json.loads(row['runtime_projection']).get('preparation_owner')
                    or any(fresh[key]!=row[key] for key in ('shell_id','harness','provider','model','effort','worktree','route_binding','shortname','flavor'))):
                raise RuntimeContractError('HISTORY_CHANGED','history preparing owner changed after proof observation')
            self.selected_worktree(fresh,allow_missing=True)
            if conversation_native_history.prepared_history(con,fresh,generation_id,history_proof)!=history:
                raise RuntimeContractError('HISTORY_CHANGED','owned continuation association changed at the mutation edge')
            if (conversation_native_chats._SERVICE is not history_service or history_service.stopped.is_set()
                    or Path(history_service.database).resolve()!=self.database.resolve()):
                raise RuntimeContractError('HISTORY_CHANGED','current history service changed at the mutation edge')
        harness=row['harness']
        observer=self.observers.get(harness)
        if observer is None:
            raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','native executable is not available in the owned seat')
        observed=observer.observe()
        if observed.binding is None:
            raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','installed native identity observation is unavailable')
        def executable_metadata():
            try:
                info=observed.binding.path.stat()
                return (str(observed.binding.path.resolve(strict=True)),info.st_dev,info.st_ino,
                        info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            except OSError as exc:
                raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','captured executable unavailable at preparation edge') from exc
        captured_executable_metadata=executable_metadata()
        if harness=='claude':
            startup_deadline=time.monotonic()+30
            if binding['selector_binding'].get('proof_state')=='pending_finite_probe':
                startup_deadline=min(startup_deadline,time.monotonic()+max(0,job['deadline']-time.time()))
            self.claude_startup_prerequisite(self.candidate_fingerprint(harness,row['model'],row['effort']),startup_deadline)
        if checked_fingerprint is not None:
            current_fingerprint=self.candidate_fingerprint(harness,row['model'],row['effort'])
            if (current_fingerprint!=checked_fingerprint or current_fingerprint.executable!=observed.binding
                    or binding['selector_binding'].get('proof_state')!='checked_native_selection'
                    or binding['catalogue_generation']!=current_fingerprint.key[:32]
                    or self.cache.admission(current_fingerprint).get('submission')!='compatible'):
                raise RuntimeContractError('NATIVE_ROUTE_CHANGED','captured checked identity/coverage changed before preparation')
        # Metadata/source observation can block. Recheck durable ownership at
        # the mutation edge; a Close/replacement during that work must precede
        # neither resource registration nor canonical start writes.
        con=db_driver.connect(str(self.database))
        try:
            fresh=con.execute('SELECT c.*,s.shortname,s.flavor,s.user_id AS shell_owner FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
            runtime=json.loads(fresh['runtime_projection']) if fresh else {}
            if (fresh is None or fresh['owner_user_id']!=1 or fresh['shell_owner']!=1
                    or fresh['state']=='closed' or fresh['runtime_mode']!='native_experiment'
                    or runtime.get('generation_id')!=generation_id or runtime.get('state')!='preparing'
                    or runtime.get('role')!=json.loads(row['runtime_projection']).get('role')
                    or runtime.get('preparation_owner')!=json.loads(row['runtime_projection']).get('preparation_owner')
                    or any(fresh[key]!=row[key] for key in ('shell_id','harness','provider','model','effort','worktree','route_binding'))):
                raise RuntimeContractError('RUNTIME_CLOSING','preparing generation was closed/replaced before canonical writes')
            self.selected_worktree(fresh,allow_missing=history is not None)
            if history is not None:
                recheck_history(con)
            if binding['selector_binding'].get('proof_state')=='pending_finite_probe':
                job=con.execute('SELECT status,deadline,fingerprint_key FROM conversation_runtime_probe_jobs WHERE conversation_id=? AND generation_id=?',(conversation_id,generation_id)).fetchone()
                if (runtime.get('role')!='probe' or not probe_capabilities or job is None
                        or job['status']!='preparing' or job['deadline']<=time.time()
                        or job['fingerprint_key']!=binding['evidence_digest']):
                    raise RuntimeContractError('PROBE_ROUTE_ONLY','finite probe ownership expired before canonical writes')
        finally:
            con.close()
        # Durable fixture resources precede canonical archive/boot/DB mutations.
        preparation_fingerprint=self.candidate_fingerprint(harness,row['model'],row['effort'])
        self.require_loaded_source(preparation_fingerprint)
        # Serialize the resource grant with all canonical preparation entries.
        # Reserve a possible child before register; other starts count it even
        # before the native controller/store row exists.
        capacity=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(capacity,'native_prepare.capacity'):
                free=self._available_roots(capacity,harness,exclude=generation_id)
                if free<1:
                    raise RuntimeContractError('PROBE_CAPACITY_PENDING','retained roots leave no native allocation capacity')
                final=capacity.execute('SELECT c.*,s.user_id AS shell_owner FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
                projection=json.loads(final['runtime_projection']) if final else {}
                if (final is None or final['owner_user_id']!=1 or final['shell_owner']!=1
                        or final['state']=='closed' or final['runtime_mode']!='native_experiment'
                        or projection.get('generation_id')!=generation_id or projection.get('state')!='preparing'
                        or projection.get('role')!=json.loads(row['runtime_projection']).get('role')
                        or projection.get('preparation_owner')!=json.loads(row['runtime_projection']).get('preparation_owner')
                        or any(final[key]!=row[key] for key in ('shell_id','harness','provider','model','effort','worktree','route_binding'))):
                    raise RuntimeContractError('RUNTIME_CLOSING','preparing ownership changed during capacity observation')
                if probe_capabilities:
                    if free<2:probe_capabilities=tuple(cap for cap in probe_capabilities if cap!='stop_work_child')
                    current=capacity.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(conversation_id,)).fetchone()
                    projection=json.loads(current[0]) if current else {}
                    if projection.get('generation_id')!=generation_id or projection.get('state')!='preparing':
                        raise RuntimeContractError('RUNTIME_CLOSING','allocation changed before capacity reservation')
                    projection['probe_child_reserved']='stop_work_child' in probe_capabilities
                    capacity.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',
                                     (json.dumps(projection),conversation_id))
        finally:capacity.close()
        # The preparing root and optional child are now durable reservations.
        # Supervisor I/O stays outside the DB writer so Close can commit.
        native=self.supervisor.register(generation_id,harness)
        if history is not None:
            con=db_driver.connect(str(self.database))
            try:
                recheck_history(con)
            finally:
                con.close()
        # Resource registration and proof callbacks can block. Retained
        # resource ownership does not authorize a now-closed boot mutation.
        con=db_driver.connect(str(self.database))
        try:
            first=con.execute('SELECT c.*,s.shortname,s.flavor,s.user_id AS shell_owner,s.is_deleted AS shell_deleted FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
            if first is None:raise RuntimeContractError('RUNTIME_CLOSING','registered conversation is no longer owned')
            self.selected_worktree(first,allow_missing=history is not None)
            fresh_fingerprint=self.candidate_fingerprint(harness,row['model'],row['effort'])
            self.require_loaded_source(fresh_fingerprint)
            if (fresh_fingerprint!=preparation_fingerprint or fresh_fingerprint.executable!=observed.binding
                    or executable_metadata()!=captured_executable_metadata):
                raise RuntimeContractError('NATIVE_ROUTE_CHANGED','registered captured executable/source changed before canonical writes')
            # Re-read after path/source observations; neither callback may
            # authorize writes using the earlier shell or preparer snapshot.
            final=con.execute('SELECT c.*,s.shortname,s.flavor,s.user_id AS shell_owner,s.is_deleted AS shell_deleted FROM conversations c JOIN shells s USING(shell_id) WHERE conversation_id=?',(conversation_id,)).fetchone()
            current=json.loads(final['runtime_projection']) if final else {}
            if (final is None or final['owner_user_id']!=1 or final['shell_owner']!=1 or final['shell_deleted']
                    or final['state']=='closed' or final['runtime_mode']!='native_experiment'
                    or current.get('generation_id')!=generation_id or current.get('state')!='preparing'
                    or current.get('role')!=json.loads(row['runtime_projection']).get('role')
                    or current.get('preparation_owner')!=json.loads(row['runtime_projection']).get('preparation_owner')
                    or any(final[key]!=row[key] for key in ('shell_id','harness','provider','model','effort','worktree','route_binding','shortname','flavor'))):
                raise RuntimeContractError('RUNTIME_CLOSING','registered generation changed before canonical writes')
            if binding['selector_binding'].get('proof_state')=='pending_finite_probe':
                current_job=con.execute('SELECT status,deadline,fingerprint_key FROM conversation_runtime_probe_jobs WHERE conversation_id=? AND generation_id=?',(conversation_id,generation_id)).fetchone()
                if (current.get('role')!='probe' or current_job is None or current_job['status']!='preparing'
                        or current_job['deadline']<=time.time() or current_job['fingerprint_key']!=binding['evidence_digest']):
                    raise RuntimeContractError('PROBE_ROUTE_ONLY','registered finite probe ownership changed before canonical writes')
            # Source/executable observations above may have changed the path
            # after its first validation. The final lexical row alone cannot
            # prove that the captured canonical checkout is still selected.
            if self.selected_worktree(final,allow_missing=history is not None)!=captured_worktree:
                raise RuntimeContractError('WORKTREE_INVALID','captured canonical worktree changed before canonical writes')
        finally:con.close()
        if initial:
            run.prepare_launch(shell_id=row['shell_id'],harness=harness,model=row['model'],effort=row['effort'],
                headless_prompt='native owned preparation; never dispatch',conversation_owned=True,
                route_binding=binding,binding_digest=digest,boot=BootDirective(conversation_id,'start'))
        plan=prepared_plan(database=self.database,root=self.root,conversation_id=conversation_id,
                           shell_id=row['shell_id'],harness=harness)
        if (plan.argv or plan.model!=row['model'] or plan.effort!=row['effort']
                or Path(plan.cwd).absolute()!=captured_worktree):
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
        caps=self.cache.admission(fingerprint)
        if history is not None:
            if history_proof is None or history_service is None:
                raise RuntimeContractError('HISTORY_UNAVAILABLE','captured history owner/proof is required')
            if fingerprint.key!=history_proof['fingerprint']:
                raise RuntimeContractError('HISTORY_CHANGED','prepared installed history identity changed')
            workspace=conversation_native_history.observe_workspace(Path(plan.cwd),self.root)
            con=db_driver.connect(str(self.database))
            try:
                # Observe outside the writer transaction, then CAS the fresh
                # state inside it without another blocking owner callback.
                recheck_history(con)
                with db_driver.write_transaction(con,'native_history.workspace'):
                    recheck_history(con,observe_proof=False)
                    con.execute('UPDATE conversation_native_history SET workspace_json=? WHERE conversation_id=? AND generation_id=?',
                        (json.dumps(dataclasses.asdict(workspace)|{'cwd':str(workspace.cwd),'git_common_dir':str(workspace.git_common_dir)}),conversation_id,generation_id))
            finally:
                con.close()
            caps=dict(caps)|{'history_resume':'compatible'}
            context=dataclasses.replace(context,history=history,workspace=workspace)
        context=dataclasses.replace(context,capability_evidence=caps)
        return context,fingerprint,native
