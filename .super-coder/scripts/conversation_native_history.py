"""Owned closed-history associations, never another native command authority.

The normal API issues the predecessor operand. A new conversation keeps its
own immutable boot and workspace. Providers still default to unavailable until
independent history-interface/behavior coverage exists.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from typing import Any, NoReturn

import active_chat_registry
import route_bindings
from conversation_runtime_contract import (
    NativeHistory,
    RuntimeContractError,
    WorkspaceIdentity,
    payload_digest,
)

HISTORY_COVERAGE = frozenset({
    'consumed_history_interface', 'owned_history_baseline', 'native_history_identity',
    'current_workspace', 'no_history_prompt_replay', 'no_restored_work_definitions',
    'pre_resume_goal_tool_policy', 'repeated_input', 'native_cleanup', 'owned_unit_cleanup',
})


def _fail(code: str, detail: str) -> NoReturn:
    raise RuntimeContractError(code, detail)


def source_history(con, cid: str, owner: int) -> tuple[Any, NativeHistory, str]:
    # Read without tenancy filtering first. Conflicting canonical rows must not
    # become absence/never-launched proof.
    row = con.execute('SELECT * FROM conversations WHERE conversation_id=?', (cid,)).fetchone()
    if row is None or row['owner_user_id'] != owner:
        _fail('HISTORY_NOT_FOUND', 'owned predecessor is unavailable')
    runtime = json.loads(row['runtime_projection'])
    if (row['runtime_mode'] != 'native_experiment' or row['state'] != 'closed'
            or runtime.get('role') not in {None, 'ordinary'} or runtime.get('state') != 'closed'
            or runtime.get('preparation_owner')):
        _fail('HISTORY_UNAVAILABLE', 'history requires a closed ordinary native predecessor')
    shell = con.execute('SELECT * FROM shells WHERE shell_id=?', (row['shell_id'],)).fetchone()
    if (shell is None or shell['user_id'] != owner or shell['is_deleted']
            or shell['flavor'] == 'admin'):
        _fail('HISTORY_NOT_FOUND', 'predecessor shell is no longer owned and launchable')
    generations = con.execute('SELECT * FROM conversation_runtime_generations WHERE conversation_id=? ORDER BY created_at,generation_id', (cid,)).fetchall()
    if not generations:
        _fail('HISTORY_UNAVAILABLE', 'no captured native history exists')
    captured = None
    proof = []
    for generation in generations:
        cleanup = json.loads(generation['cleanup_json'])
        if ((generation['owner_user_id'], generation['shell_id'], generation['harness'])
                != (owner, row['shell_id'], row['harness'])):
            _fail('HISTORY_NOT_OWNED', 'conflicting retained generation ownership')
        if (generation['state'] != 'closed' or cleanup.get('outcome') != 'complete'
                or cleanup.get('unit_verified_exited') is not True
                or cleanup.get('native_outcome') != 'complete'
                or cleanup.get('unresolved_work') or cleanup.get('unresolved_definitions')):
            _fail('CLEANUP_PENDING', 'every predecessor generation needs native, OS and definition cleanup')
        binding = json.loads(generation['binding_json'])
        context = binding.get('context', {})
        if (context.get('conversation_id'), context.get('generation_id'), context.get('owner_user_id'),
                context.get('shell_id'), context.get('harness'), context.get('provider'),
                context.get('model'), context.get('effort'), context.get('worktree')) != (
                cid, generation['generation_id'], owner, row['shell_id'], row['harness'],
                row['provider'], row['model'], row['effort'], row['worktree']):
            _fail('HISTORY_NOT_OWNED', 'captured predecessor context differs from its immutable conversation')
        commands = con.execute("SELECT command_id,state FROM conversation_runtime_commands WHERE generation_id=? AND kind='submit' ORDER BY command_sequence", (generation['generation_id'],)).fetchall()
        if any(command['state'] not in {'terminal', 'not_written'} for command in commands):
            _fail('HISTORY_INCONCLUSIVE', 'unresolved predecessor input cannot be replayed or presumed settled')
        proof.append({'generation': generation['generation_id'], 'binding': binding,
                      'cleanup': cleanup, 'commands': [dict(command) for command in commands]})
        if generation['generation_id'] == runtime.get('generation_id'):
            captured = (generation, context, cleanup)
    if captured is None or not runtime.get('root_id'):
        _fail('HISTORY_INCONCLUSIVE', 'exact owned predecessor native identity is unavailable')
    if con.execute("SELECT 1 FROM conversation_outbox WHERE conversation_id=? AND state='pending'", (cid,)).fetchone():
        _fail('HISTORY_INCONCLUSIVE', 'predecessor outbox is not resolved')
    if con.execute("SELECT 1 FROM conversation_runs WHERE conversation_id=? AND state IN ('queued','starting','running','unknown')", (cid,)).fetchone():
        _fail('HISTORY_INCONCLUSIVE', 'predecessor run outcome is not resolved')
    generation, context, cleanup = captured
    history = NativeHistory(cid, generation['generation_id'], runtime['root_id'], row['harness'],
                            row['model'], row['effort'], Path(row['worktree']),
                            context.get('boot_digest', ''), context.get('policy_digest', ''),
                            payload_digest(cleanup))
    digest = payload_digest({'source': cid, 'version': row['version'], 'route': row['route_binding'],
                             'worktree': row['worktree'], 'history': dataclasses.asdict(history) | {'source_worktree': str(history.source_worktree)},
                             'generations': proof})
    return row, history, digest


def require_slot(con, shell: int, *, destination: str | None = None) -> None:
    active = active_chat_registry.get(con, shell)
    if active is not None and active.chat_id != destination:
        _fail('WORKSPACE_BUSY', 'the canonical shell slot belongs to another conversation')
    for row in con.execute('SELECT * FROM conversations WHERE shell_id=?', (shell,)).fetchall():
        if row['conversation_id'] == destination:
            continue
        runtime = json.loads(row['runtime_projection'])
        if row['state'] != 'closed':
            _fail('WORKSPACE_BUSY', 'a current conversation already owns the canonical shell slot')
        if runtime.get('preparation_owner') or (runtime.get('generation_id')
                and not con.execute('SELECT 1 FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=?', (runtime['generation_id'], row['conversation_id'])).fetchone()
                and not ((runtime.get('preparation_cleanup') or {}).get('never_launched') is True
                         and (runtime.get('preparation_cleanup') or {}).get('unit_verified_exited') is True)):
            _fail('CLEANUP_PENDING', 'provisional preparation still owns the canonical shell slot')
    for generation in con.execute('SELECT * FROM conversation_runtime_generations WHERE shell_id=?', (shell,)).fetchall():
        if generation['conversation_id'] == destination:
            continue
        cleanup = json.loads(generation['cleanup_json'])
        if (generation['state'] != 'closed' or cleanup.get('outcome') != 'complete'
                or cleanup.get('unit_verified_exited') is not True
                or cleanup.get('native_outcome') != 'complete'
                or cleanup.get('unresolved_work') or cleanup.get('unresolved_definitions')):
            _fail('CLEANUP_PENDING', 'retained native ownership still occupies the canonical shell slot')


def checked_proof(proof: dict, history: NativeHistory) -> dict:
    binding = proof.get('binding')
    if not isinstance(binding, dict):
        _fail('HISTORY_UNAVAILABLE', 'history-specific current admission is unavailable')
    route_bindings.validate_v2_binding(binding)
    fp = proof.get('fingerprint')
    selector = binding['selector_binding']
    if (proof.get('grade') != 'compatible' or not HISTORY_COVERAGE <= set(proof.get('coverage', ()))
            or not isinstance(fp, str) or len(fp) != 64 or any(c not in '0123456789abcdef' for c in fp)
            or selector.get('proof_state') != 'checked_native_selection'
            or selector.get('native_fingerprint') != fp
            or (binding['harness'], binding['requested_model'], binding['requested_effort'])
                != (history.harness, history.model, history.effort)
            or proof.get('binding_digest') != route_bindings.digest_json(binding)):
        _fail('HISTORY_UNAVAILABLE', 'matching current history behavior and cleanup coverage is required')
    return proof


def association(con, cid: str, owner: int):
    row = con.execute('SELECT * FROM conversation_native_history WHERE conversation_id=?', (cid,)).fetchone()
    if row is not None and row['owner_user_id'] != owner:
        _fail('HISTORY_NOT_OWNED', 'continuation association is outside operator ownership')
    return row


def prepared_history(con, row, generation: str, proof: dict) -> NativeHistory | None:
    link = association(con, row['conversation_id'], row['owner_user_id'])
    if link is None:
        return None
    runtime = json.loads(row['runtime_projection'])
    if (link['generation_id'] != generation or runtime.get('generation_id') != generation
            or runtime.get('role') != 'ordinary' or runtime.get('state') != 'preparing'
            or row['state'] == 'closed' or link['shell_id'] != row['shell_id']
            or runtime.get('history')!={'source_conversation_id':link['source_conversation_id'],'source_generation_id':link['source_generation_id']}):
        _fail('HISTORY_NOT_OWNED', 'continuation preparing generation was closed or replaced')
    if con.execute('SELECT 1 FROM conversation_runtime_generations WHERE conversation_id=?',(row['conversation_id'],)).fetchone():
        _fail('HISTORY_ALREADY_ALLOCATED','captured continuation must reattach, never prepare history again')
    source, history, digest = source_history(con, link['source_conversation_id'], row['owner_user_id'])
    checked_proof(proof, history)
    if (history.source_generation_id != link['source_generation_id'] or digest != link['source_digest']
            or proof['fingerprint'] != link['fingerprint_key'] or payload_digest(proof) != link['proof_digest']
            or row['route_binding'] != route_bindings.canonical_json(proof['binding'])
            or (row['shell_id'], row['harness'], row['provider'], row['model'], row['effort'], row['worktree'])
                != (source['shell_id'], source['harness'], source['provider'], source['model'], source['effort'], source['worktree'])):
        _fail('HISTORY_CHANGED', 'predecessor, canonical workspace or checked history proof changed')
    require_slot(con, row['shell_id'], destination=row['conversation_id'])
    return history


def observe_workspace(worktree: Path, repository: Path) -> WorkspaceIdentity:
    """Bounded read of the newly prepared Git checkout; no credentials/hooks.

    This capture never restores a source branch/files or clears another slot.
    Providers must compare again before current inference after native resume.
    """
    if worktree.is_symlink() or worktree.resolve()!=worktree or repository.resolve() not in worktree.parents:
        _fail('HISTORY_WORKSPACE_INVALID','prepared checkout has aliased or foreign containment')
    values=[]
    for args in (('--show-toplevel',),('--git-common-dir',),('--abbrev-ref','HEAD'),('HEAD',)):
        result=subprocess.run(['git','-C',str(worktree),'rev-parse',*args],
            env={'PATH':os.defpath,'LC_ALL':'C','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull},
            capture_output=True,text=True,timeout=2,check=False)
        value=result.stdout.strip()
        if result.returncode or not value or len(value)>4096 or '\n' in value:
            _fail('HISTORY_WORKSPACE_INVALID','prepared Git identity observation is unavailable')
        values.append(value)
    common=Path(values[1])
    if not common.is_absolute():common=worktree/common
    common=common.resolve()
    if Path(values[0]).resolve()!=worktree or common!=(repository/'.git').resolve():
        _fail('HISTORY_WORKSPACE_INVALID','prepared Git checkout belongs to another repository')
    return WorkspaceIdentity(worktree,common,values[2],values[3])


def validate_context(con, context, binding) -> None:
    """Final canonical reservation fence; a dataclass is not owner issuance."""
    import conversation_native_chats
    service=conversation_native_chats._SERVICE
    if service is None or service.stopped.is_set() or Path(service.database).resolve()!=Path(con.execute('PRAGMA database_list').fetchone()[2]).resolve():
        _fail('HISTORY_UNAVAILABLE','current installed history owner is unavailable at reservation')
    row=con.execute('SELECT * FROM conversations WHERE conversation_id=?',(context.conversation_id,)).fetchone()
    if row is None or context.history is None or context.workspace is None:
        _fail('HISTORY_NOT_OWNED','canonical history context and fresh workspace observation required')
    proof=service.history_admission(context.history)
    history=prepared_history(con,row,context.generation_id,proof)
    link=association(con,context.conversation_id,context.owner_user_id)
    workspace=dataclasses.asdict(context.workspace)|{'cwd':str(context.workspace.cwd),'git_common_dir':str(context.workspace.git_common_dir)}
    if (history!=context.history or context.shell_id!=link['shell_id']
            or json.loads(link['workspace_json'])!=workspace or binding.get('fingerprint')!=link['fingerprint_key']
            or context.capability_evidence.get('history_resume')!='compatible'
            or conversation_native_chats._SERVICE is not service or service.stopped.is_set()):
        _fail('HISTORY_CHANGED','owned history selection/workspace/current proof changed before reservation')
