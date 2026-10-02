"""Native controller/driver contract identity and scoped-data boundaries."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_runtime_contract import (
    DriverStart,
    NativeControl,
    NativeHistory,
    NativeReference,
    ProcessIdentity,
    RuntimeContractError,
    RuntimeEvent,
    StartupConsent,
    WriteReceipt,
    payload_digest,
    public_payload,
)


def test_transport_receipt_does_not_manufacture_processing_or_outcome():
    ack = WriteReceipt(state='written', acknowledged=True, native_activity_id='opaque-turn')
    assert ack.state == 'written'
    reference = NativeReference(root_id='root', activity_id=ack.native_activity_id,
                                native_process_id='1234', os_process=ProcessIdentity(42, 300))
    assert reference.native_process_id != str(reference.os_process.pid)
    # Processing/terminal evidence arrives on a separately attributable event.
    terminal = RuntimeEvent('activity.terminal', reference=reference, provenance='native:turn/completed')
    assert terminal.reference == reference


def test_activity_requires_native_reference_and_unknown_events_refuse():
    with pytest.raises(RuntimeContractError, match='attributable'):
        RuntimeEvent('activity.started')
    with pytest.raises(RuntimeContractError, match='unknown driver event'):
        RuntimeEvent('unlisted-native-event')


def test_event_data_removes_nested_credential_and_private_reasoning_fields():
    event = RuntimeEvent('runtime.ready', data={'token':'sentinel', 'items':[
        {'text':'scoped user output', 'thinking':'private', 'env':{'PATH':'private'}},
    ]})
    assert event.data == {'items':[{'text':'scoped user output'}]}
    assert payload_digest({'a':1,'b':2}) == payload_digest({'b':2,'a':1})


def test_invalid_process_identity_and_oversize_events_are_explicit():
    with pytest.raises(RuntimeContractError, match='OS process'):
        ProcessIdentity(0, 0)
    with pytest.raises(RuntimeContractError, match='bounded payload'):
        RuntimeEvent('runtime.ready', data={'text':'x'*(65*1024)})


def test_known_values_scrub_all_nested_strings_before_persistence():
    assert public_payload({'detail':'failed auth abc-secret', 'output':['abc-secret']},
                          sensitive_values=('abc-secret',)) == {
                              'detail':'failed auth [redacted]', 'output':['[redacted]']}


def test_redaction_cannot_silently_rewrite_native_identity_targets():
    with pytest.raises(RuntimeContractError,match='without rewriting'):
        public_payload({'root_id':'root-real-secret'},sensitive_values=('real-secret',))


def test_startup_consent_is_a_typed_finite_choice_separate_from_ready_and_capabilities():
    setup=StartupConsent('g','phase-epoch','a'*64,'driver-v1','b'*64,1)
    start=DriverStart('needs_consent',setup=setup)
    assert start.setup==setup and not start.capabilities
    RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup))
    with pytest.raises(RuntimeContractError,match='needs_consent requires'):
        DriverStart('needs_consent')
    with pytest.raises(RuntimeContractError,match='needs_consent requires'):
        DriverStart('ready',setup=setup)
    with pytest.raises(RuntimeContractError,match='finite startup consent'):
        dataclasses.replace(setup,phase='arbitrary_terminal_prompt')
    with pytest.raises(RuntimeContractError,match='typed finite startup'):
        RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup)|{'arbitrary_input':'text'})


def test_enable_local_channel_control_has_only_captured_phase_binding():
    control=NativeControl('c',1,'a'*64,'enable_local_channel',options={'setup_id':'epoch','configuration_sha256':'b'*64})
    assert control.target is None
    with pytest.raises(RuntimeContractError,match='exact finite startup choice'):
        dataclasses.replace(control,options=dict(control.options)|{'text':'arbitrary terminal input'})
    with pytest.raises(RuntimeContractError,match='exact finite startup choice'):
        dataclasses.replace(control,options={'setup_id':'epoch','configuration_sha256':'wrong'})



def test_history_selection_is_frozen_bounded_and_does_not_contain_prompt_or_consent():
    history=NativeHistory('old-cv','old-g','opaque-old-root','claude','selected-model','high',Path('/owned/worktree'),
                          'a'*64,'b'*64,'c'*64)
    assert set(dataclasses.asdict(history))=={'source_conversation_id','source_generation_id','native_root_id',
        'harness','model','effort','source_worktree','source_boot_digest','source_policy_digest','cleanup_digest'}
    with pytest.raises(dataclasses.FrozenInstanceError):
        history.native_root_id='replacement'


@pytest.mark.parametrize('field,value', [
    ('native_root_id',''),('source_generation_id',True),('source_conversation_id','unsafe\noperand'),
    ('model','x'*256),('source_worktree',Path('relative')),('source_worktree',Path('/owned/../outside')),
    ('harness','unsupported'),('cleanup_digest','c'*63),('source_boot_digest','z'*64),
])
def test_history_selection_refuses_invalid_source_identity(field,value):
    history=NativeHistory('old-cv','old-g','opaque-old-root','codex','selected-model','high',Path('/owned/worktree'),
                          'a'*64,'b'*64,'c'*64)
    with pytest.raises(RuntimeContractError,match='history'):
        dataclasses.replace(history,**{field:value})
