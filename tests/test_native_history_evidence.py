"""History evidence contract fixtures; no installed/native compatibility proof."""
import copy
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.super-coder/scripts'))
from conversation_native_history import HISTORY_COVERAGE as ASSOCIATION_COVERAGE
from conversation_runtime_checks import (
    REQUIRED_COVERAGE,
    CapabilityEvidence,
    CompatibilityChecker,
    EvidenceCache,
    HistoryEligibility,
)
from conversation_runtime_contract import (
    CAP_HISTORY_RESUME,
    CAP_SUBMISSION,
    GUI_CAPABILITIES,
    HISTORY_COVERAGE,
    HistorySourceIdentity,
    RuntimeContractError,
    payload_digest,
)
from test_conversation_runtime_checks import (
    INTERFACE,
    REQUIREMENT,
    Factory,
    complete_evidence,
    fingerprint,
)
from test_conversation_runtime_checks import (
    context as _context,
)

context = _context


def source(subject='root-a',**changes):
    # Simulated server-owned immutable subject: identical policy on root B
    # must never reuse root A's actual restoration eligibility.
    subject_binding={'conversation':'closed-a','generation':'old-a','root':subject,
        'owner':1,'shell':1,'role':'ordinary','boot':'b'*64,'policy':'c'*64,
        'creation':'d'*64,'all_generation_cleanup':'e'*64}
    value={'harness': 'codex','executable_sha256': 'a'*64,'executable_version': 'fixture',
        'driver_revision': 'old-driver','implementation_digest': 'b'*64,'policy_digest': 'c'*64,
        'provider': 'openai','model': 'native-model','effort': 'high','creation_provenance_digest': 'd'*64,
        'source_binding_digest': payload_digest(subject_binding)}
    value.update(changes)
    return HistorySourceIdentity(**value)


def qualifier(fp,identity=None,**changes):
    value={'fingerprint_key': fp.key,'source': identity or source(),'coverage': HISTORY_COVERAGE,
        'creation_provenance': ('fixture:retained-owned-creation',),
        'native_policy_provenance': ('fixture:correlated-native-policy-and-definitions',)}
    value.update(changes)
    return HistoryEligibility(**value)


def evidence(fp,identity=None,**changes):
    value={'capability': CAP_HISTORY_RESUME,'grade': 'compatible','coverage': HISTORY_COVERAGE,
        'provenance': ('fixture:history-recall-repeat-and-composite-cleanup',),
        'history_eligibility': (qualifier(fp,identity),)}
    value.update(changes)
    return CapabilityEvidence(**value)


def test_shared_named_capability_uses_exact_association_coverage():
    assert CAP_HISTORY_RESUME in GUI_CAPABILITIES
    assert REQUIRED_COVERAGE[CAP_HISTORY_RESUME] == ASSOCIATION_COVERAGE == HISTORY_COVERAGE
    assert 'pre_resume_goal_tool_policy' in HISTORY_COVERAGE


def test_reusable_history_grade_never_admits_an_unqualified_source(context):
    fp=fingerprint(context);cache=EvidenceCache()
    cache.put(fp,evidence(fp,history_eligibility=()))
    assert cache._reusable(fp,CAP_HISTORY_RESUME).grade=='compatible'
    assert cache.get(fp,CAP_HISTORY_RESUME) is None
    assert cache.admission(fp)[CAP_HISTORY_RESUME]=='unverified'
    assert cache.history_evidence(fp,source()) is None
    assert cache.history_evidence(fp,None) is None


def test_cache_roundtrip_qualifies_only_exact_source_not_equal_policy_root(context):
    fp=fingerprint(context);cache=EvidenceCache();a=source();b=source('root-b')
    assert replace(a,source_binding_digest=b.source_binding_digest)==b
    cache.put(fp,evidence(fp,a));cache.put(fp,complete_evidence())
    restored=EvidenceCache.restore(json.loads(json.dumps(cache.export())))
    assert restored.history_evidence(fp,a).grade=='compatible'
    assert restored.history_evidence(fp,b) is None
    assert restored.get(fp,CAP_HISTORY_RESUME) is None
    assert restored.get(fp,CAP_SUBMISSION).grade=='compatible'
    assert restored.admission(fp)[CAP_HISTORY_RESUME]=='unverified'
    exported=json.dumps(restored.export())
    assert 'root-a' not in exported and 'closed-a' not in exported


@pytest.mark.parametrize('missing',sorted(HISTORY_COVERAGE))
def test_any_history_global_coverage_omission_refuses_compatibility(context,missing):
    fp=fingerprint(context);cache=EvidenceCache()
    cache.put(fp,evidence(fp,coverage=HISTORY_COVERAGE-{missing}))
    assert cache.history_evidence(fp,source()) is None
    assert cache.get(fp,CAP_HISTORY_RESUME) is None


@pytest.mark.parametrize('change',[
    {'creation_provenance':()},{'native_policy_provenance':()},
    {'coverage':HISTORY_COVERAGE-{'pre_resume_goal_tool_policy'}},
    {'coverage':HISTORY_COVERAGE-{'no_restored_work_definitions'}},
    {'coverage':HISTORY_COVERAGE-{'owned_unit_cleanup'}},
])
def test_remembered_creation_cannot_substitute_for_native_policy_or_cleanup(context,change):
    fp=fingerprint(context);cache=EvidenceCache()
    cache.put(fp,evidence(fp,history_eligibility=(qualifier(fp,**change),)))
    assert cache._reusable(fp,CAP_HISTORY_RESUME).grade=='compatible'
    assert cache.history_evidence(fp,source()) is None


@pytest.mark.parametrize('field',[
    'executable_sha256','executable_version','driver_revision','implementation_digest',
    'policy_digest','creation_provenance_digest','source_binding_digest','contract_revision',
])
def test_exact_retained_source_mutations_do_not_reuse_eligibility(context,field):
    fp=fingerprint(context);cache=EvidenceCache();a=source();cache.put(fp,evidence(fp,a))
    new='f'*64 if field.endswith(('digest','sha256')) else 'changed'
    assert cache.history_evidence(fp,replace(a,**{field:new})) is None


@pytest.mark.parametrize('change',[
    {'implementation_digest':'changed-current-assets'},
    {'executable':None},{'policy_digest':'changed-current-policy'},
])
def test_current_fingerprint_mutation_does_not_reuse_either_grade(context,change):
    fp=fingerprint(context);cache=EvidenceCache();cache.put(fp,evidence(fp))
    if change.get('executable','present') is None:
        change={'executable':replace(fp.executable,sha256='f'*64)}
    changed=replace(fp,**change)
    assert cache._reusable(changed,CAP_HISTORY_RESUME) is None
    assert cache.history_evidence(changed,source()) is None


def test_eligibility_transplant_to_new_fingerprint_is_rejected(context):
    fp=fingerprint(context);changed=replace(fp,implementation_digest='changed')
    with pytest.raises(ValueError):EvidenceCache().put(changed,evidence(fp))
    cache=EvidenceCache();cache.put(fp,evidence(fp));raw=cache.export()
    raw['records'][0]['evidence'][CAP_HISTORY_RESUME]['history_eligibility'][0]['fingerprint_key']='f'*64
    with pytest.raises(ValueError):EvidenceCache.restore(raw)


@pytest.mark.parametrize('change',[
    {'harness':'claude'},{'provider':'other'},{'model':'other'},{'effort':'low'},
])
def test_source_selected_route_cannot_be_relabelled_as_current_history(context,change):
    fp=fingerprint(context)
    with pytest.raises(ValueError):EvidenceCache().put(fp,evidence(fp,source(**change)))


def test_existing_normal_owner_cannot_use_reusable_history_grade(context):
    from gui_experiment_chats import FixtureChats
    fp=fingerprint(context);cache=EvidenceCache();cache.put(fp,evidence(fp))
    owner=FixtureChats.__new__(FixtureChats)
    owner.operation=SimpleNamespace(cache=cache)
    owner.seat=SimpleNamespace(candidate_fingerprint=lambda *_:fp)
    owner.resolve_route=lambda *_:({},'unused-binding')
    with pytest.raises(RuntimeContractError,match='history-specific') as error:
        owner.history_admission(SimpleNamespace(harness='codex',model='native-model',effort='high'))
    assert error.value.code=='NATIVE_HISTORY_UNAVAILABLE'


@pytest.mark.parametrize('field,value',[
    ('source_binding_digest',''),('source_binding_digest',None),
    ('creation_provenance_digest','unknown'),('executable_sha256',True),
    ('executable_version',''),('driver_revision',None),('implementation_digest',''),
    ('policy_digest',''),('harness',[]),('provider',''),('model',''),('effort',''),
])
def test_unknown_retained_source_identity_cannot_be_filled_from_current_fp(field,value):
    with pytest.raises(ValueError):source(**{field:value})


@pytest.mark.parametrize('mutation',[
    lambda q:q.update(raw_native_payload={'goal':'private'}),
    lambda q:q.update(coverage='pre_resume_goal_tool_policy'),
    lambda q:q.update(creation_provenance='source'),
    lambda q:q.update(native_policy_provenance=True),
    lambda q:q['source'].update(native_root_id='private'),
    lambda q:q['source'].update(source_binding_digest=''),
])
def test_cache_restoration_rejects_private_or_malformed_eligibility(context,mutation):
    fp=fingerprint(context);cache=EvidenceCache();cache.put(fp,evidence(fp))
    raw=copy.deepcopy(cache.export());mutation(raw['records'][0]['evidence'][CAP_HISTORY_RESUME]['history_eligibility'][0])
    with pytest.raises(ValueError):EvidenceCache.restore(raw)


def test_older_evidence_without_history_fields_preserves_ordinary_grade(context):
    fp=fingerprint(context);cache=EvidenceCache();cache.put(fp,complete_evidence())
    payload=cache.export();del payload['records'][0]['evidence'][CAP_SUBMISSION]['history_eligibility']
    restored=EvidenceCache.restore(payload)
    assert restored.get(fp,CAP_SUBMISSION).grade=='compatible'
    assert restored.history_evidence(fp,source()) is None


def test_static_interface_with_no_history_behavior_cannot_create_history_grade(context):
    class NoHistoryFactory(Factory):
        def reserve(self,*args,**kwargs):
            session=super().reserve(*args,**kwargs)
            return replace(session,exercise=lambda *_:{})
    factory=NoHistoryFactory(context);checker=CompatibilityChecker()
    result=checker.request(fingerprint(context),observed_interface=INTERFACE,
        requirements={CAP_HISTORY_RESUME:REQUIREMENT},factory=factory,seconds=1).result(timeout=2)
    assert result.evidence[CAP_HISTORY_RESUME].grade=='inconclusive'
    assert result.evidence[CAP_HISTORY_RESUME].diagnostics[0].code=='BEHAVIOR_COVERAGE_MISSING'
    assert checker.cache.history_evidence(fingerprint(context),source()) is None
    assert result.cleanup.complete


def test_cleanup_ambiguity_prevents_global_history_reuse_and_exact_subject(context):
    from conversation_runtime_checks import CleanupProof
    fp=fingerprint(context)
    class HistoryFixtureFactory(Factory):
        def reserve(self,*args,**kwargs):
            session=super().reserve(*args,**kwargs)
            return replace(session,exercise=lambda *_:{CAP_HISTORY_RESUME:evidence(fp)})
    factory=HistoryFixtureFactory(context,cleanup=CleanupProof(False,'inconclusive'))
    checker=CompatibilityChecker()
    result=checker.request(fp,observed_interface=INTERFACE,requirements={CAP_HISTORY_RESUME:REQUIREMENT},
        factory=factory,seconds=1).result(timeout=2)
    assert result.evidence[CAP_HISTORY_RESUME].grade=='inconclusive'
    assert checker.cache.history_evidence(fp,source()) is None
    assert not result.cleanup.complete


def test_history_eligibility_does_not_attach_to_ordinary_submission(context):
    fp=fingerprint(context)
    with pytest.raises(ValueError):
        replace(complete_evidence(),history_eligibility=(qualifier(fp),))
    assert asdict(source())['source_binding_digest']
