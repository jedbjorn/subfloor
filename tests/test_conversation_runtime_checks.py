"""Owned temporary files and bounded fake probes; no account, unit or inference."""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_runtime_checks import (
    REQUIRED_COVERAGE,
    CapabilityEvidence,
    CleanupProof,
    CompatibilityChecker,
    EvidenceCache,
    ExecutableObserver,
    Fingerprint,
    ProbeSession,
    RuntimeValidator,
    required_subset,
)
from conversation_runtime_contract import (
    CAP_STOP_REPLY,
    CAP_STOP_WORK,
    CAP_SUBMISSION,
    DriverStart,
    ExecutableBinding,
    NativeCleanup,
    NativeReference,
    NativeSnapshot,
    RuntimeContext,
    RuntimeDriver,
    RuntimeEvent,
    RuntimeIdentity,
    WriteReceipt,
)


@pytest.fixture
def context(tmp_path):
    work=tmp_path/'work';work.mkdir()
    state=tmp_path/'state';state.mkdir()
    executable=tmp_path/'native';executable.write_bytes(b'finite source fixture; never executed')
    return RuntimeContext('probe-generation','probe-chat',1,1,'codex',state,work,
        ExecutableBinding(executable,hashlib.sha256(executable.read_bytes()).hexdigest(),'native-test'),
        'driver-v1','boot-sha','prepared-policy','unrestricted',provider='openai',model='native-model',effort='high')


def fingerprint(context):
    return Fingerprint.capture(context,settings_digest='canonical-route-tool-shape',implementation_digest='actual-source-assets-sha')


class Driver(RuntimeDriver):
    harness='codex'
    revision='driver-v1'

    def __init__(self, *, state='ready'):
        self.state=state;self.starts=0;self.events=None

    def start(self, context, emit, *, deadline):
        self.starts+=1;self.events=emit
        return DriverStart(self.state,RuntimeIdentity('root'))

    def submit(self, command, *, deadline):
        return WriteReceipt('unknown')

    def inventory(self, *, deadline):
        return NativeSnapshot(RuntimeIdentity('root'),None)

    def control(self, command, *, deadline):
        return WriteReceipt('unsupported')

    def cleanup(self, *, deadline):
        return NativeCleanup('complete')


class Factory:
    def __init__(self, context, *, driver=None, coverage=True, cleanup=None):
        self.context=context;self.driver=driver or Driver();self.coverage=coverage
        self.proof=cleanup or CleanupProof(True,'complete')
        self.allocations=0;self.cleaned=0;self.entered=threading.Event();self.release=None
        self.registered=True;self.active=False;self.emit_extra=None
        self.isolation_root=context.worktree.parent

    def reserve(self, fingerprint, capabilities, *, deadline):
        self.allocations+=1;self.entered.set()
        if self.release is not None:
            self.release.wait(max(0,deadline-time.monotonic()))
        def exercise(driver, deadline):
            if self.emit_extra:driver.events(self.emit_extra)
            return {cap:CapabilityEvidence(cap,'compatible',REQUIRED_COVERAGE[cap] if self.coverage else frozenset(),('fixture:recorded-native-contract',)) for cap in capabilities}
        return ProbeSession(self.context,self.driver,'registered-owned-unit',self.isolation_root,self.registered,exercise)

    def cleanup(self, fingerprint, *, deadline):
        self.cleaned+=1
        self.driver.cleanup(deadline=deadline)
        return self.proof


INTERFACE={'methods':{'turn/start':{'input':['text'],'result':{'id':'string'}}},'extra':'harmless'}
REQUIREMENT={'methods':{'turn/start':{'result':{'id':'string'}}}}


def check(checker, context, factory, **kwargs):
    return checker.request(fingerprint(context),observed_interface=INTERFACE,
        requirements={CAP_SUBMISSION:REQUIREMENT},factory=factory,**kwargs)


def test_observer_preserves_captured_binary_when_installed_symlink_changes(tmp_path):
    one=tmp_path/'native-one';one.write_bytes(b'one')
    two=tmp_path/'native-two';two.write_bytes(b'two')
    installed=tmp_path/'native';installed.symlink_to(one)
    reads=[]
    observer=ExecutableObserver(installed,version_reader=lambda path,deadline: reads.append(path) or path.name)
    first=observer.observe();assert first.changed and first.binding.path==one
    assert not observer.observe().changed and reads==[one]
    installed.unlink();installed.symlink_to(two)
    second=observer.observe()
    assert second.changed and second.binding.path==two and first.binding.path==one
    assert second.binding.sha256!=first.binding.sha256


def test_service_observer_notices_replacement_without_new_chat(tmp_path):
    path=tmp_path/'native';path.write_bytes(b'old')
    changed=threading.Event();observations=[]
    observer=ExecutableObserver(path,version_reader=lambda *_:'native-test')
    observer.observe()
    observer.start(lambda result:(observations.append(result),changed.set()),interval=.01)
    try:
        path.write_bytes(b'new binary')
        assert changed.wait(1) and observations[-1].changed
    finally:
        assert observer.close()


def test_unavailable_or_racing_installed_identity_is_inconclusive(tmp_path):
    missing=ExecutableObserver(tmp_path/'missing')
    assert missing.observe().grade=='inconclusive'
    path=tmp_path/'native';path.write_bytes(b'old')
    def changing_version(path,deadline):
        path.write_bytes(b'new');return 'version'
    result=ExecutableObserver(path,version_reader=changing_version).observe()
    assert result.grade=='inconclusive' and result.binding is None


def test_fingerprint_excludes_chat_boot_ids_but_includes_actual_source_and_policy(context):
    original=fingerprint(context)
    another=replace(context,generation_id='other-generation',conversation_id='other-chat',boot_digest='dynamic-boot')
    assert fingerprint(another).key==original.key
    for mutation in ({'policy_digest':'changed-policy'},{'model':'changed-model'},{'executable':replace(context.executable,sha256='changed-binary')}):
        assert fingerprint(replace(context,**mutation)).key!=original.key
    assert replace(original,implementation_digest='changed-source-assets').key!=original.key
    assert replace(original,settings_digest='changed-managed-mcp-route-shape').key!=original.key


@pytest.mark.parametrize('observed',[
    {'type':'string','enum':['text','image'],'unknown':True},
    {'type':'string','enum':['image','text','new-future-item']},
])
def test_required_subset_accepts_harmless_properties_and_enum_additions(observed):
    assert required_subset(observed,{'type':'string','enum':['text']}) is None


@pytest.mark.parametrize('observed,required,path',[
    ({},{'method':{}},'method'),({'value':True},{'value':1},'value'),
    ({'type':'integer'},{'type':'string'},'type'),({'enum':['new']},{'enum':['text']},'enum'),
])
def test_consumed_required_fields_and_types_report_exact_path(observed,required,path):
    assert required_subset(observed,required)==path


def complete_evidence(cap=CAP_SUBMISSION):
    return CapabilityEvidence(cap,'compatible',REQUIRED_COVERAGE[cap]|{'owned_unit_cleanup'},('owned:normal-events+behavior','owned:verified-cgroup-exit'))


def test_cache_roundtrip_prevents_duplicate_probe_after_api_restart(context):
    cache=EvidenceCache();fp=fingerprint(context)
    cache.put(fp,complete_evidence())
    restored=EvidenceCache.restore(cache.export())
    factory=Factory(context)
    result=check(CompatibilityChecker(cache=restored),context,factory).result(timeout=1)
    assert result.evidence[CAP_SUBMISSION].grade=='compatible' and factory.allocations==0
    assert restored.get(replace(fp,implementation_digest='new-source'),CAP_SUBMISSION) is None


def test_schema_only_or_incomplete_cleanup_evidence_cannot_certify(context):
    cache=EvidenceCache();fp=fingerprint(context)
    cache.put(fp,CapabilityEvidence(CAP_SUBMISSION,'compatible',REQUIRED_COVERAGE[CAP_SUBMISSION],('schema-only',)))
    assert cache.get(fp,CAP_SUBMISSION) is None
    cache.put(fp,CapabilityEvidence(CAP_SUBMISSION,'compatible',REQUIRED_COVERAGE[CAP_SUBMISSION]|{'owned_unit_cleanup'}))
    assert cache.get(fp,CAP_SUBMISSION) is None


def test_cache_rejects_wrong_key_and_older_contract_cannot_certify(context):
    cache=EvidenceCache();cache.put(fingerprint(context),complete_evidence())
    payload=cache.export();payload['records'][0]['key']='wrong-key'
    with pytest.raises(ValueError):EvidenceCache.restore(payload)
    assert EvidenceCache.restore({'revision':'older-contract'}).get(fingerprint(context),CAP_SUBMISSION) is None


def test_unavailable_credentials_evidence_expires_without_becoming_incompatible(context):
    cache=EvidenceCache();fp=fingerprint(context)
    cache.put(fp,CapabilityEvidence(CAP_SUBMISSION,'inconclusive',observed_at=time.time()-61))
    assert cache.get(fp,CAP_SUBMISSION) is None
    cache.put(fp,CapabilityEvidence(CAP_SUBMISSION,'incompatible'))
    assert cache.get(fp,CAP_SUBMISSION).grade=='incompatible'


def test_unknown_events_and_added_fields_do_not_disable_operations():
    validator=RuntimeValidator('root')
    assert not validator.frame({'kind':'future/native/addition','unrecognized':object()})
    assert not validator.frame({'kind':'output.delta','reference':{'root_id':'root','thread_id':'root','activity_id':'turn','item_id':'item','new_native_field':True},'data':{'text':'visible'},'new_field':'addition'})


def test_malformed_consumed_fields_degrade_only_affected_capability():
    validator=RuntimeValidator('root')
    diagnostics=validator.frame({'kind':'output.delta','reference':{'root_id':'root','thread_id':'root','activity_id':'turn'},'data':{'text':'partial'}})
    assert {d.capability for d in diagnostics}=={CAP_SUBMISSION}
    assert diagnostics[0].grade=='inconclusive'
    bad=validator.frame({'kind':'activity.started','reference':{'root_id':'root','thread_id':'root'}})
    assert {d.capability for d in bad}=={CAP_SUBMISSION,CAP_STOP_REPLY}
    assert not validator.frame({'kind':'output.final','reference':{'root_id':'root','thread_id':'root','activity_id':'turn','item_id':'item'},'data':{'text':'reader continues'}})


def test_child_turn_alias_and_claude_implicit_root_are_supported():
    validator=RuntimeValidator('root')
    assert not validator.validate(RuntimeEvent('activity.terminal',NativeReference('root','child',activity_id='turn')))
    assert not validator.validate(RuntimeEvent('activity.started',NativeReference('root','root',activity_id='turn')))
    assert validator.validate(RuntimeEvent('activity.started',NativeReference('root','child',activity_id='turn')))
    claude=RuntimeValidator('root',harness='claude')
    assert not claude.validate(RuntimeEvent('activity.started',NativeReference('root',activity_id='prompt')))
    assert not claude.validate(RuntimeEvent('output.final',NativeReference('root',activity_id='prompt'),data={'text':'channel reply'}))


def test_unproved_stop_success_degrades_stop_and_supervision_is_independent():
    validator=RuntimeValidator('root')
    diagnostics=validator.validate(RuntimeEvent('control.outcome',NativeReference('root','root',activity_id='turn'),control_id='control',partial=True,data={'outcome':'complete'}))
    assert {d.capability for d in diagnostics}=={CAP_STOP_REPLY}
    assert not validator.frame({'kind':'future/cleanup'})


def test_singleflight_bounded_behavior_and_cleanup_then_cache(context):
    checker=CompatibilityChecker();factory=Factory(context);factory.release=threading.Event()
    future=check(checker,context,factory)
    assert factory.entered.wait(1)
    assert check(checker,context,factory) is future
    factory.release.set();result=future.result(timeout=1)
    assert result.cleanup.complete and result.evidence[CAP_SUBMISSION].grade=='compatible'
    assert factory.allocations==factory.cleaned==factory.driver.starts==1
    assert 'owned_unit_cleanup' in result.evidence[CAP_SUBMISSION].coverage
    assert check(checker,context,factory).result(timeout=1).evidence[CAP_SUBMISSION].grade=='compatible'
    assert factory.allocations==1


@pytest.mark.parametrize('state',['unavailable','needs_consent'])
def test_missing_account_or_explicit_consent_remains_inconclusive(context,state):
    factory=Factory(context,driver=Driver(state=state))
    result=check(CompatibilityChecker(),context,factory).result(timeout=1)
    assert result.evidence[CAP_SUBMISSION].grade=='inconclusive' and factory.cleaned==1


def test_behavior_missing_required_coverage_is_inconclusive(context):
    factory=Factory(context,coverage=False)
    result=check(CompatibilityChecker(),context,factory).result(timeout=1)
    assert result.evidence[CAP_SUBMISSION].grade=='inconclusive'


@pytest.mark.parametrize('boundary',['active','unregistered','wrong_policy','escaped_worktree'])
def test_owner_factory_cannot_use_active_or_unregistered_context(context,tmp_path,boundary):
    factory=Factory(context);options={}
    if boundary=='active':options['active_generations']=frozenset({context.generation_id})
    elif boundary=='unregistered':factory.registered=False
    elif boundary=='wrong_policy':factory.context=replace(context,policy_digest='wrong-policy')
    else:
        escape=tmp_path.parent/'outside-fixture'
        factory.context=replace(context,worktree=escape)
    result=check(CompatibilityChecker(),context,factory,**options).result(timeout=1)
    assert result.evidence[CAP_SUBMISSION].grade=='inconclusive'
    assert factory.driver.starts==0 and factory.cleaned==1


def test_required_interface_mismatch_does_not_allocate_native_runtime(context):
    factory=Factory(context)
    result=CompatibilityChecker().request(fingerprint(context),observed_interface={},requirements={CAP_STOP_WORK:REQUIREMENT},factory=factory).result(timeout=1)
    assert result.evidence[CAP_STOP_WORK].grade=='incompatible' and factory.allocations==factory.cleaned==0


def test_timeout_factory_cleanup_fences_late_reserve_before_start(context):
    class SlowFactory(Factory):
        def reserve(self, fingerprint, capabilities, *, deadline):
            self.entered.set();self.release.wait(1)
            if self.cleaned:raise TimeoutError('owner allocation fenced by cleanup')
            return super().reserve(fingerprint,capabilities,deadline=deadline)
    factory=SlowFactory(context);factory.release=threading.Event()
    future=check(CompatibilityChecker(),context,factory,seconds=.09)
    assert factory.entered.wait(1)
    result=future.result(timeout=1);factory.release.set()
    assert result.evidence[CAP_SUBMISSION].grade=='inconclusive'
    assert factory.cleaned==1 and factory.driver.starts==0


def test_unverified_cleanup_retains_capacity_until_independent_proof(context):
    checker=CompatibilityChecker();fp=fingerprint(context)
    factory=Factory(context,cleanup=CleanupProof(False,'complete'))
    result=check(checker,context,factory).result(timeout=1)
    assert result.evidence[CAP_SUBMISSION].grade=='inconclusive'
    assert not checker.resolve_cleanup(fp,CleanupProof(False,'complete'))
    assert checker.resolve_cleanup(fp,CleanupProof(True,'complete'))
    assert not checker.resolve_cleanup(fp,CleanupProof(True,'complete'))


def test_normal_observed_break_changes_only_captured_generation_fingerprint(context):
    checker=CompatibilityChecker();old=fingerprint(context);new=replace(old,implementation_digest='new-install-source')
    checker.cache.put(old,complete_evidence());checker.cache.put(new,complete_evidence())
    checker.validate_runtime(old,RuntimeValidator('root'),RuntimeEvent('output.delta',NativeReference('root','root',activity_id='turn'),partial=True,data={'text':'partial scoped output'}))
    assert checker.cache.get(old,CAP_SUBMISSION).grade=='inconclusive'
    assert checker.cache.get(new,CAP_SUBMISSION).grade=='compatible'


def test_partial_normal_events_cannot_certify_behavior(context):
    factory=Factory(context)
    factory.emit_extra=RuntimeEvent('output.delta',NativeReference('root','root',activity_id='turn'),partial=True,data={'text':'partial scoped output'})
    result=check(CompatibilityChecker(),context,factory).result(timeout=1)
    assert result.evidence[CAP_SUBMISSION].grade=='inconclusive'


def test_terminal_stop_coverage_never_admits_unproved_child_stop(context):
    from conversation_runtime_checks import TARGET_COVERAGE
    cache=EvidenceCache();fp=fingerprint(context)
    coverage=REQUIRED_COVERAGE[CAP_STOP_WORK]|{'owned_unit_cleanup'}|TARGET_COVERAGE['codex']['stop_work_terminal']
    cache.put(fp,CapabilityEvidence(CAP_STOP_WORK,'compatible',coverage,('owned:terminal-stop-and-sibling-observed',)))
    assert cache.admission(fp)['stop_work_terminal']=='compatible'
    assert cache.admission(fp)['stop_work_child']=='inconclusive'
    assert cache.admission(fp)[CAP_STOP_WORK]=='compatible'
    both=coverage|TARGET_COVERAGE['codex']['stop_work_child']
    cache.put(fp,CapabilityEvidence(CAP_STOP_WORK,'compatible',both,('owned:terminal-and-child-stops-observed',)))
    assert cache.admission(fp)['stop_work_child']=='compatible'


def test_claude_taskstop_coverage_never_admits_user_agent_control(context):
    from conversation_runtime_checks import TARGET_COVERAGE
    cache=EvidenceCache();fp=replace(fingerprint(context),harness='claude')
    coverage=REQUIRED_COVERAGE[CAP_STOP_WORK]|{'owned_unit_cleanup'}|TARGET_COVERAGE['claude']['stop_work_terminal']
    cache.put(fp,CapabilityEvidence(CAP_STOP_WORK,'compatible',coverage,('owned:TaskStop-result-and-later-snapshot',)))
    assert cache.admission(fp)['stop_work_terminal']=='compatible'
    assert cache.admission(fp)['stop_work_child']=='inconclusive'


def test_observer_retries_failed_owner_publication_without_losing_replacement(tmp_path):
    path=tmp_path/'native';path.write_bytes(b'finite-observer')
    done=threading.Event();calls=[]
    def publish(result):
        calls.append(result)
        if len(calls)==1:raise RuntimeError('temporary owner store unavailable')
        done.set()
    observer=ExecutableObserver(path,version_reader=lambda *_:'native-test')
    observer.start(publish,interval=.01)
    try:
        assert done.wait(1) and len(calls)==2 and calls[0].binding==calls[1].binding
        assert calls[1].code=='OWNER_PUBLICATION_RETRY'
    finally:
        assert observer.close()


def test_public_cache_export_scrubs_known_private_values(context):
    cache=EvidenceCache();fp=fingerprint(context)
    cache.put(fp,replace(complete_evidence(),provenance=('owned:credential-sensitive-value appeared in diagnostic',)))
    assert 'credential-sensitive-value' not in str(cache.export(sensitive_values=('credential-sensitive-value',)))


def test_unverified_owned_units_keep_global_two_root_budget(context):
    checker=CompatibilityChecker();contexts=[replace(context,model='route-'+str(index)) for index in range(3)]
    factories=[Factory(item,cleanup=CleanupProof(False,'complete')) for item in contexts]
    for item,factory in zip(contexts,factories):
        assert check(checker,item,factory).result(timeout=1).evidence[CAP_SUBMISSION].grade=='inconclusive'
    assert [factory.allocations for factory in factories]==[1,1,0]
    assert checker.resolve_cleanup(fingerprint(contexts[0]),CleanupProof(True,'complete'))
    assert checker.resolve_cleanup(fingerprint(contexts[1]),CleanupProof(True,'complete'))


def test_demonstrated_child_break_restricts_only_child_action(context):
    from conversation_runtime_checks import TARGET_COVERAGE
    checker=CompatibilityChecker();fp=fingerprint(context)
    coverage=REQUIRED_COVERAGE[CAP_STOP_WORK]|{'owned_unit_cleanup'}|TARGET_COVERAGE['codex']['stop_work_terminal']|TARGET_COVERAGE['codex']['stop_work_child']
    checker.cache.put(fp,CapabilityEvidence(CAP_STOP_WORK,'compatible',coverage,('owned:both-target-behaviors',)))
    event=RuntimeEvent('control.outcome',NativeReference('root','child',activity_id='child-turn',work_id='child'),control_id='stop-child',partial=True,data={'outcome':'complete'})
    checker.validate_runtime(fp,RuntimeValidator('root'),event)
    assert checker.cache.admission(fp)['stop_work_child']=='incompatible'
    assert checker.cache.admission(fp)['stop_work_terminal']=='compatible'


def test_same_installed_binding_recovery_is_published_after_unavailability(tmp_path):
    target=tmp_path/'binary';target.write_bytes(b'unchanged executable')
    installed=tmp_path/'native';installed.symlink_to(target)
    observer=ExecutableObserver(installed,version_reader=lambda *_:'native-test')
    captured=observer.observe().binding
    installed.unlink()
    assert observer.observe().changed
    assert not observer.observe().changed
    installed.symlink_to(target)
    recovery=observer.observe()
    assert recovery.changed and recovery.grade=='compatible' and recovery.binding==captured
    assert not observer.observe().changed


@pytest.mark.parametrize('data',[{}, {'text':42}, {'text':None}, {'text':False}])
def test_consumed_output_requires_string_text_but_reader_validation_continues(data):
    validator=RuntimeValidator('root')
    diagnostics=validator.frame({'kind':'output.final','reference':{'root_id':'root','thread_id':'root','activity_id':'turn','item_id':'item'},'data':data})
    assert {d.capability for d in diagnostics}=={CAP_SUBMISSION}
    assert diagnostics[0].grade=='incompatible' and diagnostics[0].code=='OUTPUT_TEXT_INVALID'
    assert not validator.frame({'kind':'output.final','reference':{'root_id':'root','thread_id':'root','activity_id':'turn','item_id':'next'},'data':{'text':'reader continues','extra':'harmless'}})
