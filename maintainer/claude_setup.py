"""Fixed fixture Claude TUI setup mechanics; never a conversation driver.

The caller supplies the canonical main-root context from the fixture's named
preparation seam. No public command/path/environment API is exposed here.
No input is synthesized. Source success never certifies zero native inference.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib
import io
import json
import math
import os
import pty
import re
import secrets
import selectors
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import termios
import time
import tty
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / '.super-coder/scripts'
sys.path[:0]=[str(Path(__file__).resolve().parent),str(SCRIPTS),str(SCRIPTS.parent/'api'),str(SCRIPTS.parents[1])]
from conversation_adapters.claude_runtime import _memory_disable_source
from conversation_runtime_contract import RuntimeContext, RuntimeContractError

MAX_FRAME = 256 * 1024
MAX_TRANSCRIPT = 2 * 1024 * 1024
SETUP_PHASES = ('ownership', 'preparation', 'native_start', 'observation')
SETUP_DIAGNOSTIC_STATES = {'attempted', 'failed', 'returned'}
SETUP_ERRORS = {'NONE', 'CONTRACT_REFUSED', 'DATABASE_UNAVAILABLE', 'IO_UNAVAILABLE',
                'SHAPE_INVALID', 'OBSERVATION_UNAVAILABLE'}


def service_bus() -> dict[str, str]:
    """Existing current-user service transport only; never a fallback bus."""
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    expected = f'/run/user/{os.getuid()}'
    if (runtime != expected or not isinstance(address, str) or len(address) > 256
            or not re.fullmatch(re.escape('unix:path=' + expected + '/bus')
                                + r'(?:,guid=[a-f0-9]{32})?', address)):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'current user service transport unavailable')
    directory, socket = Path(expected).lstat(), (Path(expected) / 'bus').lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != os.getuid()):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'current user service transport ownership differs')
    return {'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': address}


def setup_phase(supervisor, generation: str, phase: str, state: str, code: str) -> None:
    """Optional immutable private diagnostic; never blocks setup cleanup."""
    try:
        if phase not in SETUP_PHASES or state not in SETUP_DIAGNOSTIC_STATES or code not in SETUP_ERRORS:
            return
        if not setup_owned(supervisor, generation):
            return
        fixture = fixture_module()
        record = fixture.verify_receipt(supervisor.receipt)
        fixture.verify_root(record)
        native = next(n for n in record['native_units'] if n['generation_id'] == generation)
        fixture.verify_native(record, native)
        budget(native['setup_deadline'])
        write_private(Path(native['root']) / f'claude-setup-phase-{phase}-{state}.json',
                      {'generation': generation, 'helper_sha256': digest(Path(__file__).resolve()),
                       'phase': phase, 'state': state, 'code': code})
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, StopIteration):
        pass


def setup_diagnostic(state_root: Path, generation: str, helper_sha256: str, *, deadline: float) -> dict[str, str]:
    """Bounded same-generation private records; output only fixed enums."""
    result = {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}
    try:
        budget(deadline)
        root_info = state_root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
            return result
        for phase in SETUP_PHASES:
            for state in ('attempted', 'returned', 'failed'):
                budget(deadline)
                path = state_root / f'claude-setup-phase-{phase}-{state}.json'
                if not path.exists() and not path.is_symlink():
                    continue
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 512:
                        return {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}
                    raw = stream.read(513)
                    after = os.fstat(stream.fileno())
                current = path.lstat()
                if len(raw) != before.st_size or len(raw) > 512 or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    return {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}
                value = json.loads(raw)
                if (not isinstance(value, dict) or set(value) != {'generation', 'helper_sha256', 'phase', 'state', 'code'}
                        or value['generation'] != generation or value['helper_sha256'] != helper_sha256
                        or value['phase'] != phase or value['state'] != state or value['code'] not in SETUP_ERRORS):
                    return {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}
                result = {key: value[key] for key in ('phase', 'state', 'code')}
        budget(deadline)
        current_root = state_root.lstat()
        if (root_info.st_dev, root_info.st_ino) != (current_root.st_dev, current_root.st_ino):
            return {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}
        return result
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}

def fixture_module():
    # A copied fixture has the hash-bound maintainer bootstrap, never a host
    # checkout import. Development tests use the sibling source module.
    name='fixture_bootstrap' if (SCRIPTS.parents[1]/'fixture_bootstrap.py').is_file() else 'gui_experiment'
    return importlib.import_module(name)


def digest(path: Path) -> str:
    if path.resolve()!=path or path.is_symlink() or not path.is_file():
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'captured setup file unavailable')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_private(path: Path, value: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def process(pid: int) -> tuple[int, str]:
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[19]), next(line[3:] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                               if line.startswith('0::/'))


def budget(deadline: float) -> float:
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'finite setup deadline required')
    remaining = deadline - time.monotonic()
    if remaining <= 0 or remaining > 30:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup budget unavailable')
    return remaining


def validate(context: RuntimeContext, main_root: Path, deadline: float) -> str:
    """Check configured evidence only; the owned hook remains separate."""
    budget(deadline)
    if (context.harness != 'claude' or context.provider != 'anthropic'
            or context.worktree != main_root or main_root.resolve() != main_root
            or context.controller_endpoint is not None or context.capability_evidence
            or context.probe_capabilities or context.managed_mcp_args
            or context.permission_mode != 'bypassPermissions'
            or not context.model or not context.effort
            or context.executable.path.resolve() != context.executable.path):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'canonical setup context differs')
    common = main_root / '.git'
    if common.is_symlink() or not common.is_dir():
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'canonical Git main root required')
    for option,expected in (('--show-toplevel',main_root),('--git-common-dir',common)):
        observed=subprocess.run(['/usr/bin/git','-C',str(main_root),'rev-parse',option],
            env={'PATH':os.defpath},capture_output=True,text=True,timeout=min(1,budget(deadline)),check=False)
        value=observed.stdout.strip()
        if observed.returncode or len(value)>4096 or (main_root/Path(value)).resolve()!=expected:
            raise RuntimeContractError('SETUP_INCONCLUSIVE','actual canonical Git MAIN identity differs')
    if (digest(main_root / 'CLAUDE.md') != context.boot_digest
            or hashlib.sha256(context.boot_content.encode()).hexdigest() != context.boot_digest
            or digest(main_root / 'AGENTS.md') != context.boot_digest
            or digest(context.executable.path) != context.executable.sha256):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'captured boot or executable changed')
    info = context.state_root.lstat()
    if (context.state_root.resolve() != context.state_root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or context.state_root != main_root / 'runtime' / context.generation_id):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'registered private setup root differs')
    # The fixed preparer returns only synthetic fixture routing. Check it again
    # before any native process; credentials are compared in place, never logged.
    db = main_root / '.super-coder/shell_db.db'
    if db.resolve() != db or not db.is_file():
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'copied synthetic database unavailable')
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT user_id,flavor,api_key FROM shells WHERE shell_id=?", (context.shell_id,)).fetchone()
    port = json.loads((main_root / '.super-coder/instance.json').read_text())['port']
    if (row is None or row[0] != 1 or row[1] != 'admin' or context.owner_user_id != 1
            or context.env.get('SC_SHELL_ID') != str(context.shell_id)
            or context.env.get('SC_API_TOKEN') != row[2]
            or context.env.get('SC_API_BASE') != f'http://127.0.0.1:{port}'
            or context.env.get('SC_ENGINE_DIR') != str(main_root / '.super-coder')
            or context.env.get('SC_ROOT') != str(main_root)):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'synthetic setup identity differs')
    adapter = json.loads((main_root / '.super-coder/adapters/claude/adapter.json').read_text())
    if adapter.get('launch_flags') != ['--dangerously-skip-permissions']:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'canonical permission policy differs')
    for path in context.managed_mcp_files:
        if path.resolve() != path or not path.is_relative_to(context.state_root):
            raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup MCP file escaped generation')
        value = json.loads(path.read_text())
        servers = value.get('mcpServers')
        if not isinstance(servers, dict) or set(servers) != {'browser'}:
            raise RuntimeContractError('SETUP_INCONCLUSIVE', 'canonical setup MCP shape differs')
        server = servers['browser']
        if server != {'type':'http','url':f'http://127.0.0.1:{port}/mcp/{context.env.get("SC_SHELL_SHORTNAME", "").upper()}'}:
            raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup MCP routing differs')
    if len(context.managed_mcp_files) != 1:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'canonical managed MCP required')
    source = _memory_disable_source(context.executable.path, context.executable.sha256, deadline=deadline)
    budget(deadline)
    return source


def fixed_launch(context: RuntimeContext, main_root: Path, deadline: float) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    source = validate(context, main_root, deadline)
    observer = Path(__file__).resolve()
    hook = shlex.join([sys.executable, '-I', str(observer), '_hook', '--state-root', str(context.state_root)])
    settings = context.state_root / 'claude-setup-settings.json'
    write_private(settings, {'autoMemoryEnabled': False, 'permissions': {'deny': ['CronCreate']},
                            'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 3}]}]}})
    files = [main_root / 'CLAUDE.md', main_root / 'AGENTS.md', settings, observer,
             main_root / '.super-coder/adapters/claude/adapter.json', *context.managed_mcp_files]
    # Canonical permission and shell hooks remain discovered. Their bytes are
    # bound alongside the extra observer instead of replacing that discovery.
    discovered: dict[str,str|None]={}
    for name in ('.claude/settings.local.json','.claude/settings.json'):
        path=main_root/name
        present=path.exists() or path.is_symlink()
        discovered[str(path)]=digest(path) if present else None
        if present:files.append(path)
    binding: dict[str, Any] = {'generation_id': context.generation_id, 'session_id': str(uuid.UUID(hex=context.generation_id)),
               'worktree': str(main_root), 'native_home':context.env['HOME'], 'receipt':context.env['SC_F89_SETUP_RECEIPT'], 'executable': str(context.executable.path),
               'executable_sha256': context.executable.sha256, 'source_condition_sha256': source,
               'files': {str(p): digest(p) for p in files},'discovered_settings':discovered}
    binding['configuration_sha256']=hashlib.sha256(json.dumps(
        {'files':binding['files'],'discovered_settings':discovered},sort_keys=True).encode()).hexdigest()
    write_private(context.state_root / 'claude-setup-binding.json', binding)
    env = {k: v for k, v in context.env.items() if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'SC_F89_'))}
    env.update({'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1', 'CLAUDE_CODE_DISABLE_CRON': '1',
                'CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF': '1', 'DISABLE_AUTOUPDATER': '1',
                'SC_F89_SETUP_GENERATION': context.generation_id})
    # No positional prompt, resume, channel, controller or automatic readiness.
    assert isinstance(context.model,str) and isinstance(context.effort,str)
    argv = [*context.execution_prefix, str(context.executable.path), '--session-id', binding['session_id'],
            '--settings', str(settings), '--strict-mcp-config', '--mcp-config',
            *map(str, context.managed_mcp_files), '--dangerously-skip-permissions',
            '--disallowedTools', 'CronCreate', '--model', context.model, '--effort', context.effort]
    return argv, env, binding


def revalidate(binding: Mapping[str, Any], deadline: float) -> None:
    budget(deadline)
    for name,expected in binding.get('discovered_settings',{}).items():
        path=Path(name)
        current=digest(path) if path.exists() or path.is_symlink() else None
        if current!=expected:
            raise RuntimeContractError('SETUP_INCONCLUSIVE','discovered project settings changed')
    if digest(Path(binding['executable'])) != binding['executable_sha256'] or any(
            digest(Path(path)) != expected for path, expected in binding['files'].items()):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'captured setup configuration changed')
    budget(deadline)


def observe_hook(state: Path, raw: bytes) -> dict[str, Any]:
    """Only an actual owned descendant can emit the qualified setup flag."""
    if state.resolve() != state or len(raw) > MAX_FRAME:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup observation unavailable')
    binding = json.loads((state / 'claude-setup-binding.json').read_text())
    child = json.loads((state / 'claude-setup-child.json').read_text())
    event = json.loads(raw)
    if (not isinstance(event, dict) or event.get('hook_event_name') != 'SessionStart'
            or event.get('source') != 'startup' or event.get('session_id') != binding['session_id']
            or event.get('cwd') != binding['worktree']
            or os.environ.get('SC_F89_SETUP_GENERATION') != binding['generation_id']
            or os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY') != '1'):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'owned startup flag unavailable')
    revalidate(binding, time.monotonic() + 2)
    ticks, group = process(child['pid'])
    if ticks != child['start_ticks'] or group != child['cgroup'] or process(os.getpid())[1] != group:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup child identity differs')
    pid = os.getpid()
    for _ in range(12):
        if pid == child['pid']:
            break
        pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
    else:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'hook is not an owned setup descendant')
    if Path(f'/proc/{pid}/exe').resolve() != Path(binding['executable']):
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'actual native executable differs')
    # This pointer is private evidence from the already verified owned hook.
    # It is never searched for, exported, or treated as evidence of zero turns.
    path=event.get('transcript_path')
    if isinstance(path,str):
        try:
            pointer=transcript_pointer(binding,Path(path))
            write_private(state/'claude-setup-transcript.json',pointer)
        except (OSError,ValueError,RuntimeContractError):
            pass # Memory evidence is independent; unavailable turn evidence stays inconclusive.
    result = {'state': 'owned_startup_observed', 'generation_id':binding['generation_id'],'observed_at':time.time(),
              'configuration_sha256':binding['configuration_sha256'],
              'auto_memory_enabled_setting':False, 'evidence_level': 'configuration_source_flag_inference',
              'effective_telemetry': False, 'inherited_disable_flag': '1',
              'executable_sha256': binding['executable_sha256'], 'source_condition_sha256': binding['source_condition_sha256'],
              'hook_sha256': digest(Path(__file__).resolve())}
    write_private(state / 'claude-setup-observation.json', result)
    return result


def _owned_file_identity(path: Path, *, directory: bool=False) -> list[int]:
    # lstat every component, including the captured native-home namespace.
    if not path.is_absolute() or path.resolve()!=path:
        raise RuntimeContractError('SETUP_INCONCLUSIVE','transcript identity unavailable')
    for component in (*reversed(path.parents),path):
        if component.is_symlink():
            raise RuntimeContractError('SETUP_INCONCLUSIVE','transcript alias unavailable')
    info=path.lstat()
    if info.st_uid!=os.getuid() or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise RuntimeContractError('SETUP_INCONCLUSIVE','transcript ownership unavailable')
    return [info.st_dev,info.st_ino]


def transcript_pointer(binding: Mapping[str,Any], path: Path) -> dict[str,Any]:
    """Accept only the exact hook-supplied session in its native namespace."""
    projects=Path(binding['native_home'])/'.claude/projects'
    if (path.name!=binding['session_id']+'.jsonl' or path.parent.parent!=projects
            or path.resolve()!=path or path.parent.name in {'','subagents'}):
        raise RuntimeContractError('SETUP_INCONCLUSIVE','owned transcript namespace differs')
    parent=_owned_file_identity(path.parent,directory=True)
    file_identity=_owned_file_identity(path) if path.exists() or path.is_symlink() else None
    return {'path':str(path),'parent_identity':parent,'file_identity':file_identity,
            'minimum_size':path.stat().st_size if file_identity is not None else 0,
            'session_id':binding['session_id'],'generation_id':binding['generation_id'],
            'worktree':binding['worktree']}


def transcript_turn_evidence(state: Path, *, deadline: float) -> dict[str,Any]:
    """Fixed post-exit reader, bounded exact-session evidence, no telemetry claim.

    Its only caller runs after supervisor-verified whole-unit exit. A missing,
    empty, unfamiliar or changed file cannot establish zero native turns.
    """
    failure={'state':'inconclusive','evidence':'exact_session_transcript_turn_records'}
    try:
        budget(deadline)
        binding=json.loads((state/'claude-setup-binding.json').read_text())
        pointer=json.loads((state/'claude-setup-transcript.json').read_text())
        path=Path(pointer['path'])
        current=transcript_pointer(binding,path)
        if any(current[key]!=pointer[key] for key in ('parent_identity','session_id','generation_id','worktree')):
            return failure
        if pointer['file_identity'] is not None and current['file_identity']!=pointer['file_identity']:
            return failure
        if current['file_identity'] is None:
            # Distinguish only a qualified pointer that never had a file.
            # This is not evidence of zero turns or successful setup. Unknown
            # pointers, removed files and all other read failures stay opaque.
            revalidate(binding, deadline)
            budget(deadline)
            if transcript_pointer(binding,path)!=current:return failure
            return failure | {'reason':'qualified_transcript_missing'}
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if ([before.st_dev,before.st_ino]!=current['file_identity'] or not stat.S_ISREG(before.st_mode)
                    or not max(1,pointer['minimum_size'])<=before.st_size<=MAX_TRANSCRIPT):return failure
            chunks=[];remaining=before.st_size
            while remaining:
                budget(deadline)
                chunk=stream.read(min(65536,remaining))
                if not chunk:return failure
                chunks.append(chunk);remaining-=len(chunk)
            after=os.fstat(stream.fileno())
        data=b''.join(chunks)
        final=path.lstat()
        if ((before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                or (after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(final.st_size,final.st_mtime_ns,final.st_ctime_ns)
                or _owned_file_identity(path)!=current['file_identity'] or not data.endswith(b'\n')):return failure
        user=assistant=records=attributable=0
        known={'user','assistant','system','file-history-snapshot'}
        for line in data.splitlines():
            budget(deadline)
            if not line or len(line)>MAX_FRAME:return failure
            row=json.loads(line)
            if not isinstance(row,dict) or row.get('type') not in known:return failure
            if row['type']=='system' and row.get('subtype') not in {'init','local_command','turn_duration'}:return failure
            if 'sessionId' in row and row['sessionId']!=binding['session_id']:return failure
            if 'cwd' in row and row['cwd']!=binding['worktree']:return failure
            if 'isSidechain' in row and (type(row['isSidechain']) is not bool or row['isSidechain']):return failure
            bound=row.get('sessionId')==binding['session_id'] and row.get('cwd')==binding['worktree']
            if row['type'] in {'user','assistant'}:
                if not bound or not isinstance(row.get('message'),dict):return failure
                user+=row['type']=='user';assistant+=row['type']=='assistant'
            if row['type']=='system' and row.get('subtype')=='turn_duration' and not (user or assistant):return failure
            records+=1;attributable+=bound
        if not attributable:return failure
        return {'state':'observed','evidence':'exact_session_transcript_turn_records','effective_telemetry':False,
                'user_records':user,'assistant_records':assistant,'record_count':records,
                'zero_turn_records':user==assistant==0,'transcript_sha256':hashlib.sha256(data).hexdigest()}
    except (OSError,ValueError,KeyError,TypeError,RecursionError,RuntimeContractError):
        return failure



def child_owner_current(binding: Mapping[str, Any], deadline: float) -> bool:
    """Recheck durable parent+child ownership at the actual native exec edge."""
    fixture=fixture_module()
    budget(deadline)
    record=fixture.verify_receipt(Path(binding['receipt']))
    root=fixture.verify_root(record)
    native=next((n for n in record.get('native_units',[]) if n['generation_id']==binding['generation_id']),None)
    if native is None or native.get('purpose')!='claude_setup' or native.get('status')!='active':return False
    fixture.verify_native(record,native)
    state=fixture.unit_state(native,timeout=min(1,deadline-time.monotonic()))
    child=native.get('setup_child',{})
    ticks,group=process(os.getpid())
    parent=int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[1])
    return (str(root)==binding['worktree'] and record['status'] in {'serving','preparing'}
        and child.get('pid')==os.getpid() and child.get('start_ticks')==ticks and child.get('control_group')==group
        and native.get('main_pid')==parent and process(parent)==(native.get('main_pid_start_ticks'),group)
        and state.get('Description')==fixture.native_description(record,native)
        and state.get('ActiveState')=='active' and int(state.get('MainPID','0'))==parent
        and state.get('ControlGroup')==group and native.get('setup_executable_sha256')==binding['executable_sha256']
        and native.get('setup_helper_sha256')==digest(Path(__file__).resolve()))


def gated_child(fd: int) -> None:
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(MAX_FRAME + 1)
    if len(raw) > MAX_FRAME:
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'exec gate exceeded bounds')
    frame = json.loads(raw)
    revalidate(frame['binding'], frame['deadline'])
    if not child_owner_current(frame['binding'],frame['deadline']):
        raise RuntimeContractError('SETUP_INCONCLUSIVE','setup owner changed at native exec edge')
    budget(frame['deadline'])
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir(frame['binding']['worktree'])
    os.execvpe(frame['argv'][0], frame['argv'], frame['env'])


def run_setup(context: RuntimeContext, main_root: Path, *, verify_owned: Callable[[], bool],
              record_child: Callable[[int, int, str], bool], deadline: float,
              input_fd: int = 0, output_fd: int = 1) -> dict[str, Any]:
    """One bounded operator TUI, under the caller's registered native unit.

    All keyboard bytes come from the operator TTY. The caller owns whole-unit
    cleanup; this function never reports composite cleanup or a capability.
    """
    budget(deadline)
    if not os.isatty(input_fd) or not os.isatty(output_fd) or not verify_owned():
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'owned operator terminal required')
    argv, env, binding = fixed_launch(context, main_root, deadline)
    revalidate(binding, deadline)
    if not verify_owned():
        raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup owner changed')
    # Owner observation may block. Its earlier true result cannot authorize
    # a new PTY or child after the shared budget has expired.
    budget(deadline)
    master, slave = pty.openpty()
    read_fd, write_fd = os.pipe()
    child = None
    previous = termios.tcgetattr(input_fd)
    descriptor_flags: dict[int,int]={}
    try:
        budget(deadline)
        child = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '_child', '--gate-fd', str(read_fd)],
                                 stdin=slave, stdout=slave, stderr=slave, pass_fds=(read_fd,),
                                 env={'PATH': os.defpath, **service_bus()}, start_new_session=True)
        os.close(read_fd); read_fd = -1
        os.close(slave); slave = -1
        ticks, group = process(child.pid)
        if not verify_owned() or not record_child(child.pid, ticks, group):
            raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup child not durably registered')
        budget(deadline)
        write_private(context.state_root / 'claude-setup-child.json', {'pid': child.pid, 'start_ticks': ticks, 'cgroup': group})
        revalidate(binding, deadline)
        if not verify_owned():
            raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup owner changed before exec')
        budget(deadline)
        frame = json.dumps({'argv': argv, 'env': env, 'binding': binding, 'deadline': deadline}).encode()
        if len(frame) > MAX_FRAME:
            raise RuntimeContractError('SETUP_INCONCLUSIVE', 'private exec frame exceeded bounds')
        # The child waits for EOF and validates the complete frame. Partial
        # writes cannot execute the native CLI, and no gate is ever replayed.
        os.set_blocking(write_fd,False)
        with selectors.DefaultSelector() as gate:
            gate.register(write_fd,selectors.EVENT_WRITE)
            offset=0
            while offset<len(frame):
                budget(deadline)
                if not verify_owned():
                    raise RuntimeContractError('SETUP_INCONCLUSIVE','setup owner withdrawn before exec release')
                budget(deadline)
                if gate.select(min(.05,deadline-time.monotonic())):
                    offset+=os.write(write_fd,frame[offset:])
        os.close(write_fd);write_fd=-1
        tty.setraw(input_fd)
        descriptor_flags={fd:fcntl.fcntl(fd,fcntl.F_GETFL) for fd in {master,input_fd,output_fd}}
        for fd in descriptor_flags:os.set_blocking(fd,False)
        native_pending=bytearray();operator_pending=bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(input_fd, selectors.EVENT_READ)
            selector.register(master, selectors.EVENT_READ)
            while child.poll() is None and time.monotonic() < deadline:
                if not verify_owned():
                    raise RuntimeContractError('SETUP_INCONCLUSIVE', 'setup owner withdrawn')
                for key, _ in selector.select(min(.05, max(0, deadline - time.monotonic()))):
                    try:
                        data = os.read(key.fd, 8192)
                    except OSError:
                        data = b''
                    if not data:
                        selector.unregister(key.fd)
                        continue
                    pending=native_pending if key.fd==input_fd else operator_pending
                    if len(pending)+len(data)>MAX_FRAME:
                        raise RuntimeContractError('SETUP_INCONCLUSIVE','bounded terminal forwarding exceeded')
                    pending.extend(data)
                for fd,pending in ((master,native_pending),(output_fd,operator_pending)):
                    if pending:
                        try:written=os.write(fd,pending)
                        except BlockingIOError:written=0
                        del pending[:written]
        return {'state': 'setup_inconclusive', 'native_input_generated': False,
                'observation_present': (context.state_root / 'claude-setup-observation.json').is_file()}
    finally:
        termios.tcsetattr(input_fd, termios.TCSANOW, previous)
        for fd,flags in descriptor_flags.items():fcntl.fcntl(fd,fcntl.F_SETFL,flags)
        for fd in (master, slave, read_fd, write_fd):
            if fd >= 0:
                os.close(fd)
        if child is not None:
            # Anchor PID/group identity with the unreaped child until signals.
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=.5)
            else:
                child.wait(timeout=.5)



def prepare_claude_setup_context(supervisor, generation: str, executable: Path,
                                 executable_sha256: str, deadline: float) -> RuntimeContext:
    """Fixed setup purpose, genuinely canonical prepared at synthetic MAIN.

    Runs only in the registered setup unit. It never runs native CLI metadata
    commands, claims model availability or alters ordinary launch guards.
    """
    fixture=fixture_module()
    import run
    from conversation_adapters.claude_runtime import REVISION
    from conversation_boot import BootDirective
    from conversation_runtime_contract import ExecutableBinding

    budget(deadline)
    record = fixture.verify_receipt(supervisor.receipt)
    root = fixture.verify_root(record)
    if (run.ENGINE.resolve() != root / '.super-coder' or run.REPO_ROOT.resolve() != root
            or Path(run.DB_PATH).resolve() != root / '.super-coder/shell_db.db'):
        raise RuntimeContractError('SETUP_INCONCLUSIVE','copied synthetic engine required')
    native = next((n for n in record.get('native_units',[]) if n['generation_id']==generation),None)
    if native is None or native.get('purpose')!='claude_setup' or not setup_owned(supervisor,generation):
        raise RuntimeContractError('SETUP_INCONCLUSIVE','registered setup owner unavailable')
    # Preparation sees only the disposable HOME, never the native account home.
    if os.environ.get('HOME') != str(root/'home'):
        raise RuntimeContractError('SETUP_INCONCLUSIVE','masked canonical preparation home required')
    short = 'fx'+record['fixture_id'][:10]+'setup'
    cid = 'cv_fixture_setup_'+generation
    database = root/'.super-coder/shell_db.db'
    with sqlite3.connect(database) as con:
        con.row_factory=sqlite3.Row
        row=con.execute('SELECT * FROM shells WHERE shortname=?',(short,)).fetchone()
        if row is None:
            inserted=con.execute("INSERT INTO shells(display_name,shortname,flavor,system_prompt,user_id,api_key) VALUES(?,?,'admin','Native initial trust setup only',1,?)",
                            (short,short,secrets.token_hex(32))).lastrowid
            if inserted is None:raise RuntimeContractError('SETUP_INCONCLUSIVE','setup shell allocation failed')
            sid=int(inserted)
        else:
            if row['user_id']!=1 or row['flavor']!='admin':
                raise RuntimeContractError('SETUP_INCONCLUSIVE','fixed setup shell changed')
            sid=int(row['shell_id'])
        if con.execute('SELECT 1 FROM conversations WHERE conversation_id=?',(cid,)).fetchone():
            raise RuntimeContractError('SETUP_INCONCLUSIVE','setup generation is never restarted')
        con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_projection) VALUES(?,?,1,'claude','anthropic','claude-sonnet-5-5','high',?,?,?,?)",
                    (cid,sid,str(root),cid,hashlib.sha256(cid.encode()).hexdigest(),json.dumps({'role':'setup','generation_id':generation})))
    try:
        if not setup_owned(supervisor,generation):
            raise RuntimeContractError('SETUP_INCONCLUSIVE','setup owner changed before preparation')
        # Suppress boot status text, which can contain private fixture locators.
        class Discard(io.TextIOBase):
            def write(self,text: str) -> int:return len(text)
        with contextlib.redirect_stdout(Discard()):
            plan=run.prepare_launch(shell_id=sid,harness='claude',model='claude-sonnet-5-5',effort='high',
                                    headless_prompt='setup preparation; never dispatch',conversation_owned=True,
                                    boot=BootDirective(cid,'start'))
            resumed=run.prepare_launch(shell_id=sid,harness='claude',model='claude-sonnet-5-5',effort='high',
                                       headless_prompt='setup preparation; never dispatch',conversation_owned=True,
                                       boot=BootDirective(cid,'resume'))
        budget(deadline)
        if (not setup_owned(supervisor,generation) or plan.argv or resumed.argv
                or Path(plan.cwd)!=root or Path(resumed.cwd)!=root
                or plan.boot_content!=resumed.boot_content
                or plan.model!='claude-sonnet-5-5' or plan.effort!='high'
                or plan.env.get('SC_SHELL_ID')!=str(sid)):
            raise RuntimeContractError('SETUP_INCONCLUSIVE','canonical main-root preparation differs')
        adapter=run.load_adapter('claude')
        mcp=run.managed_mcp_injection(adapter,short)
        if not mcp or mcp['name']!='browser' or mcp['launch_args'][0]!='--mcp-config' or len(mcp['launch_args'])!=2:
            raise RuntimeContractError('SETUP_INCONCLUSIVE','canonical setup MCP unavailable')
        mcp_path=Path(native['root'])/'claude-setup-managed-mcp.json'
        write_private(mcp_path,json.loads(mcp['launch_args'][1]))
        import pwd
        env=dict(plan.env)
        env.update(HOME=pwd.getpwuid(os.getuid()).pw_dir,SC_F89_SETUP_RECEIPT=str(supervisor.receipt))
        return RuntimeContext(generation,cid,int(sid),1,'claude',Path(native['root']),root,
            ExecutableBinding(executable,executable_sha256,'captured-unobserved-version'),REVISION,
            hashlib.sha256(plan.boot_content.encode()).hexdigest(),hashlib.sha256(json.dumps(adapter,sort_keys=True).encode()).hexdigest(),
            'bypassPermissions',provider='anthropic',model=plan.model,effort=plan.effort,boot_content=plan.boot_content,
            execution_prefix=tuple(plan.execution_view.prefix),managed_mcp_files=(mcp_path,),env=env)
    except BaseException:
        close_setup_conversation(database,cid,root,generation)
        raise


def close_setup_conversation(database: Path,cid: str,root: Path,generation: str) -> None:
    """Finalize only this fixed setup row; never an ordinary or foreign chat."""
    if database!=root/'.super-coder/shell_db.db' or database.resolve()!=database or cid!='cv_fixture_setup_'+generation:
        raise RuntimeContractError('SETUP_INCONCLUSIVE','setup finalization identity differs')
    with sqlite3.connect(database,timeout=.1) as con:
        row=con.execute('SELECT owner_user_id,worktree,runtime_projection,shell_id FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
        if row is None:return
        shell=con.execute('SELECT user_id,flavor FROM shells WHERE shell_id=?',(row[3],)).fetchone()
        projection=json.loads(row[2])
        if row[0]!=1 or row[1]!=str(root) or projection!={'role':'setup','generation_id':generation} or shell!=(1,'admin'):
            raise RuntimeContractError('SETUP_INCONCLUSIVE','setup finalization ownership differs')
        con.execute("UPDATE conversations SET state='closed',closed_at=COALESCE(closed_at,datetime('now')) WHERE conversation_id=?",(cid,))


def setup_owned(supervisor,generation: str) -> bool:
    fixture=fixture_module()
    record=fixture.verify_receipt(supervisor.receipt)
    fixture.verify_root(record)
    native=next((n for n in record.get('native_units',[]) if n['generation_id']==generation),None)
    if native is None or native.get('purpose')!='claude_setup' or native.get('status')!='active':return False
    remaining=native.get('setup_deadline',0)-time.monotonic()
    if remaining<=0:return False
    state=fixture.unit_state(native,timeout=min(1,remaining))
    if (state.get('Description')!=fixture.native_description(record,native) or state.get('ActiveState')!='active'
            or int(state.get('MainPID','0'))!=os.getpid() or state.get('ControlGroup')!=native.get('control_group')):return False
    fixture.verify_native(record,native)
    ticks,group=process(os.getpid())
    return (native.get('main_pid')==os.getpid() and native.get('main_pid_start_ticks')==ticks
            and native.get('control_group')==group and native.get('setup_helper_sha256')==digest(Path(__file__).resolve()))


def record_setup_child(supervisor,generation: str,pid: int,ticks: int,group: str) -> bool:
    fixture=fixture_module()
    initial=fixture.read_json(supervisor.receipt)
    native=next(n for n in initial['native_units'] if n['generation_id']==generation)
    with fixture.ownership_lock(initial['fixture_id'],deadline=native['setup_deadline']):
        if not setup_owned(supervisor,generation) or process(pid)!=(ticks,group) or process(os.getpid())[1]!=group:return False
        record=fixture.verify_receipt(supervisor.receipt)
        native=next(n for n in record['native_units'] if n['generation_id']==generation)
        if native.get('setup_child'):return False
        native['setup_child']={'pid':pid,'start_ticks':ticks,'control_group':group}
        fixture.save(record,supervisor.receipt)
        return True


def setup_unit(receipt: Path,generation: str,executable: Path,sha256: str,deadline: float, *, pretrust: bool=False) -> dict[str,Any]:
    fixture=fixture_module()
    supervisor=fixture.NativeSupervisor(receipt)
    record=fixture.verify_receipt(receipt)
    root=fixture.verify_root(record)
    # Initial unit is inert until the outer supervisor records its OS identity.
    while not setup_owned(supervisor,generation):
        budget(deadline)
        time.sleep(.02)
    bus = service_bus()
    override = None
    if pretrust:
        override = importlib.import_module('claude_trust').config_override(os.environ.get('CLAUDE_CONFIG_DIR'))
    os.environ.clear()
    os.environ.update(HOME=str(root/'home'),PATH=os.defpath,PYTHONUNBUFFERED='1',SC_USER='fixture-operator', **bus)
    if override is not None:
        os.environ['CLAUDE_CONFIG_DIR'] = str(override)
    phase = 'preparation'
    context = None
    try:
        setup_phase(supervisor, generation, phase, 'attempted', 'NONE')
        context=prepare_claude_setup_context(supervisor,generation,executable,sha256,deadline-2)
        if pretrust:
            from dataclasses import replace

            claude_trust = importlib.import_module('claude_trust')
            native=next(n for n in record['native_units'] if n['generation_id']==generation)
            if not isinstance(claude_trust.__file__,str):
                raise RuntimeContractError('TRUST_INCONCLUSIVE','captured trust source unavailable')
            helper=Path(claude_trust.__file__).resolve()
            if digest(helper)!=native.get('trust_helper_sha256'):
                raise RuntimeContractError('TRUST_INCONCLUSIVE','captured trust source changed')
            if override is not None:
                context=replace(context,env={**context.env,'CLAUDE_CONFIG_DIR':str(override)})
            def trust_owned() -> bool:
                owned=setup_owned(supervisor,generation)
                budget(deadline-2)
                return owned and digest(helper)==native['trust_helper_sha256']
            result=dict(claude_trust.prepared_trust(context,root,deadline=deadline-2,
                verify_owned=trust_owned))
            write_private(context.state_root/'claude-trust-result.json',
                {**result,'generation':generation,'helper_sha256':digest(helper)})
            return result
        phase = 'native_start'
        setup_phase(supervisor, generation, phase, 'attempted', 'NONE')
        result = run_setup(context,root,verify_owned=lambda:setup_owned(supervisor,generation),
                         record_child=lambda pid,ticks,group:record_setup_child(supervisor,generation,pid,ticks,group),
                         deadline=deadline-2)
        setup_phase(supervisor, generation, 'observation', 'returned', 'NONE')
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as error:
        code = ('CONTRACT_REFUSED' if isinstance(error, RuntimeContractError)
                else 'DATABASE_UNAVAILABLE' if isinstance(error, sqlite3.Error)
                else 'IO_UNAVAILABLE' if isinstance(error, OSError)
                else 'SHAPE_INVALID' if isinstance(error, (ValueError, KeyError, TypeError))
                else 'OBSERVATION_UNAVAILABLE')
        setup_phase(supervisor, generation, phase, 'failed', code)
        raise
    finally:
        if context is not None:
            close_setup_conversation(root/'.super-coder/shell_db.db',context.conversation_id,root,generation)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    hook = sub.add_parser('_hook')
    hook.add_argument('--state-root', type=Path, required=True)
    child = sub.add_parser('_child')
    child.add_argument('--gate-fd', type=int, required=True)
    unit = sub.add_parser('_setup')
    unit.add_argument('--receipt', type=Path, required=True)
    unit.add_argument('--generation', required=True)
    unit.add_argument('--executable', type=Path, required=True)
    unit.add_argument('--sha256', required=True)
    unit.add_argument('--deadline', type=float, required=True)
    pretrust = sub.add_parser('_pretrust')
    pretrust.add_argument('--receipt', type=Path, required=True)
    pretrust.add_argument('--generation', required=True)
    pretrust.add_argument('--executable', type=Path, required=True)
    pretrust.add_argument('--sha256', required=True)
    pretrust.add_argument('--deadline', type=float, required=True)
    args = parser.parse_args()
    try:
        if args.action == '_hook':
            observe_hook(args.state_root, sys.stdin.buffer.read(MAX_FRAME + 1))
        elif args.action == '_child':
            gated_child(args.gate_fd)
        else:
            setup_unit(args.receipt,args.generation,args.executable,args.sha256,args.deadline,pretrust=args.action=='_pretrust')
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error):
        # Native command-hook errors can fail open. No turn is sent by setup;
        # later driver readiness separately requires its own actual owned hook.
        print('Experimental setup observation is inconclusive.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
