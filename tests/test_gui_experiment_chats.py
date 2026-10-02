"""Fixture installation/admission uses synthetic source observations only."""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import conversation_native_chats
from conversation_runtime_checks import (
    REQUIRED_COVERAGE,
    CapabilityEvidence,
    InstalledObservation,
)
from conversation_runtime_contract import (
    ExecutableBinding,
    RuntimeContext,
    RuntimeContractError,
)
from gui_experiment_chats import FixtureChats
from test_gui_experiment_runtime import operation  # noqa: F401
from test_native_chat_ownership import database  # noqa: F401


def completed_probe(value,fp,con):
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'P','Probe','dev','synthetic',1)")
    cleanup={'outcome':'complete','native_outcome':'complete','unit_verified_exited':True}
    projection={'role':'probe','generation_id':'proof-gen','state':'closed',
        'native_route':{'account_type':'chatgpt' if fp.harness=='codex' else 'claude.ai','model':fp.model,'efforts':[fp.effort],'catalogue_observed':False},'cleanup':cleanup}
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('proof-cv',2,1,?,?,?,?,'/synthetic/probe','probe-key',?,'native_experiment',?)",
        (fp.harness,fp.provider,fp.model,fp.effort,fp.key,json.dumps(projection)))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES(?,'proof-cv','proof-gen','complete',100,1)",(fp.key,));con.commit()
    context=RuntimeContext('proof-gen','proof-cv',2,1,fp.harness,value.seat.root/'runtime',Path('/synthetic/probe'),
        fp.executable,fp.driver_revision,'d'*64,fp.policy_digest,'unrestricted',provider=fp.provider,model=fp.model,effort=fp.effort)
    value.service.store.reserve(context,{'fingerprint':fp.key,'implementation_digest':fp.implementation_digest,'purpose':'finite_probe'})
    con.execute("UPDATE conversation_runtime_generations SET state='closed',cleanup_json=? WHERE generation_id='proof-gen'",(json.dumps(cleanup),));con.commit()
    con.execute("UPDATE conversations SET state='closed',closed_at=datetime('now') WHERE conversation_id='proof-cv'");con.commit()
    return projection


def test_grade_alone_cannot_issue_normal_native_route(request):
    value,fp,_,_=request.getfixturevalue('operation')
    value.cache.put(fp,CapabilityEvidence('submission','compatible',REQUIRED_COVERAGE['submission']|{'owned_unit_cleanup'},('synthetic source fixture',)))
    with pytest.raises(RuntimeContractError) as exc:FixtureChats(value).resolve_route('codex',fp.model,fp.effort)
    assert exc.value.code=='NATIVE_ROUTE_INCONCLUSIVE'


@pytest.mark.parametrize('field,value',[
    ('executable',{'path':'/bin/false','sha256':'f'*64,'version':'other'}),
    ('driver_revision','other'),('contract_revision','other'),('policy_digest','other'),
    ('permission_mode','interactive'),('model','other'),('effort','low'),
    ('provider','other'),('harness','claude'),('conversation_id','other'),('shell_id',9)])
def test_checked_context_cannot_disagree_with_proof_identity(request,field,value):
    owner,fp,con,_=request.getfixturevalue('operation')
    completed_probe(owner,fp,con)
    owner.cache.put(fp,CapabilityEvidence('submission','compatible',REQUIRED_COVERAGE['submission']|{'owned_unit_cleanup'},('synthetic source fixture',)))
    captured=json.loads(con.execute("SELECT binding_json FROM conversation_runtime_generations WHERE generation_id='proof-gen'").fetchone()[0])
    captured['context'][field]=value
    con.execute("UPDATE conversation_runtime_generations SET binding_json=? WHERE generation_id='proof-gen'",(json.dumps(captured),));con.commit()
    with pytest.raises(RuntimeContractError) as exc:FixtureChats(owner).resolve_route('codex',fp.model,fp.effort)
    assert exc.value.code=='NATIVE_ROUTE_INCONCLUSIVE'


@pytest.mark.parametrize('gap',['none','coverage','cleanup','model','effort','account','source'])
def test_route_requires_exact_selected_observation_coverage_and_cleanup(request,gap):
    value,fp,con,_=request.getfixturevalue('operation')
    projection=completed_probe(value,fp,con)
    coverage=REQUIRED_COVERAGE['submission']|{'owned_unit_cleanup'}
    if gap=='coverage':coverage-= {'repeated_input'}
    value.cache.put(fp,CapabilityEvidence('submission','compatible',coverage,('synthetic source fixture',)))
    if gap=='cleanup':
        con.execute("UPDATE conversation_runtime_generations SET cleanup_json='{}' WHERE generation_id='proof-gen'")
    elif gap=='source':
        binding=json.loads(con.execute("SELECT binding_json FROM conversation_runtime_generations WHERE generation_id='proof-gen'").fetchone()[0])
        binding['supervision']['implementation_digest']='other'
        con.execute("UPDATE conversation_runtime_generations SET binding_json=? WHERE generation_id='proof-gen'",(json.dumps(binding),))
    elif gap in {'model','effort','account'}:
        field={'model':'model','effort':'efforts','account':'account_type'}[gap]
        projection['native_route'][field]=['low'] if gap=='effort' else 'other'
        con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='proof-cv'",(json.dumps(projection),))
    con.commit()
    chats=FixtureChats(value)
    if gap=='none':
        binding,digest=chats.resolve_route('codex',fp.model,fp.effort)
        assert binding['selector_binding']['proof_state']=='checked_native_selection' and digest
        assert binding['selector_binding']['model_observed'] and not binding['selector_binding']['catalogue_observed']
    else:
        with pytest.raises(RuntimeContractError):chats.resolve_route('codex',fp.model,fp.effort)


def test_installed_update_publication_keeps_captured_generation_and_capabilities(request,monkeypatch):
    value,_,con,_=request.getfixturevalue('operation')
    old={'generation_id':'g','state':'ready','role':'ordinary','capabilities':{'submission':'compatible'},
         'captured_identity':{'executable':{'sha256':'old'}},'primary':{'activity_id':'old-turn'}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(old),));con.commit()
    class Observer:
        callback=None
        closed=False
        def start(self,callback,*,interval):self.callback=callback;assert interval==5
        def close(self):self.closed=True
    observer=Observer()
    value.seat.observers={'codex':observer}
    monkeypatch.setattr(conversation_native_chats,'_SERVICE',None)
    chats=FixtureChats(value)
    chats.start()
    try:
        assert value.workflow.config()['enabled']
        assert observer.callback is not None
        observer.callback(InstalledObservation(ExecutableBinding(Path('/bin/true'),'b'*64,'new'),True,'compatible'))
        end=time.monotonic()+2
        while time.monotonic()<end:
            runtime=json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])
            if runtime.get('latest_installed_identity'):break
            time.sleep(.01)
        assert runtime['capabilities']==old['capabilities'] and runtime['captured_identity']==old['captured_identity']
        assert runtime['primary']==old['primary']
        assert runtime['latest_installed_identity']['executable']['sha256']=='b'*64
        assert runtime['latest_installed_identity']['capability_grade']=='inconclusive'
        assert runtime['latest_installed_identity']['identity_grade']=='compatible'
    finally:
        chats.close()
    assert observer.closed and conversation_native_chats._SERVICE is None
    assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0]=='ready'


def test_replaced_generation_is_refused_before_normal_preparation(request):
    value,_,con,_=request.getfixturevalue('operation')
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'other','state':'preparing'}),));con.commit()
    calls=[]
    value.seat.prepare=lambda *args:calls.append(args)
    with pytest.raises(RuntimeContractError):FixtureChats(value).prepare('cv','g')
    assert not calls


@pytest.mark.parametrize('gap',['none','grade','coverage','account','effort','model','source','cleanup'])
def test_claude_normal_admission_requires_selected_native_proof_not_codex_cache(request,gap):
    import dataclasses

    from conversation_native_checks import CLAUDE_SELECTION
    value,codex,con,_=request.getfixturevalue('operation')
    fp=dataclasses.replace(codex,harness='claude',provider='anthropic',model=CLAUDE_SELECTION['model'])
    def candidate(harness,model,effort):
        assert (harness,model,effort)==('claude',fp.model,fp.effort)
        return fp
    value.seat.candidate_fingerprint=candidate
    projection=completed_probe(value,fp,con)
    coverage=REQUIRED_COVERAGE['submission']|{'owned_unit_cleanup'}
    value.cache.put(codex,CapabilityEvidence('submission','compatible',coverage,('synthetic Codex source proof',)))
    if gap!='grade':
        value.cache.put(fp,CapabilityEvidence('submission','compatible',coverage-({'memory_disabled'} if gap=='coverage' else set()),('synthetic Claude source proof',)))
    if gap in {'account','effort','model'}:
        projection['native_route'][{'account':'account_type','effort':'efforts','model':'model'}[gap]]=['low'] if gap=='effort' else 'other'
        con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='proof-cv'",(json.dumps(projection),))
    elif gap=='source':
        binding=json.loads(con.execute("SELECT binding_json FROM conversation_runtime_generations WHERE generation_id='proof-gen'").fetchone()[0])
        binding['supervision']['implementation_digest']='other'
        con.execute("UPDATE conversation_runtime_generations SET binding_json=? WHERE generation_id='proof-gen'",(json.dumps(binding),))
    elif gap=='cleanup':con.execute("UPDATE conversation_runtime_generations SET cleanup_json='{}' WHERE generation_id='proof-gen'")
    con.commit()
    if gap=='none':
        binding,_=FixtureChats(value).resolve_route('claude',fp.model,fp.effort)
        assert binding['harness']=='claude' and binding['transport']=='claude-effort-argument'
        assert binding['selector_binding']['proof_state']=='checked_native_selection'
        assert binding['selector_binding']['model_observed'] and binding['selector_binding']['effort_observed']
        assert binding['selector_binding']['catalogue_observed'] is False
    else:
        with pytest.raises(RuntimeContractError):FixtureChats(value).resolve_route('claude',fp.model,fp.effort)
