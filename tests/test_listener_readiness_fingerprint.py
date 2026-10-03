"""Both providers bind the changed client and copied listener bootstrap."""
from pathlib import Path

import pytest
from test_gui_experiment_native_seat import seat as seat  # noqa: PLC0414
from test_private_baseline_integration import claude_sources


@pytest.mark.parametrize('harness',['codex','claude'])
@pytest.mark.parametrize('asset',['.super-coder/scripts/conversation_runtime.py','fixture_bootstrap.py'])
def test_listener_content_is_consumed_and_loaded_source_change_refuses(seat,harness,asset):
    value,_events,_=seat
    if harness=='claude':claude_sources(value)
    first=value.candidate_fingerprint(harness,'selected-model','high')
    value.loaded_implementations[harness]=first.implementation_digest
    value.require_loaded_source(first)
    path=value.root/asset
    assert path in value.implementation_files(harness)
    path.write_text(path.read_text()+'\n# changed listener source\n')
    changed=value.candidate_fingerprint(harness,'selected-model','high')
    assert changed.key!=first.key and changed.implementation_digest!=first.implementation_digest
    assert value.cache.admission(changed)['submission']!='compatible'
    from conversation_runtime_contract import RuntimeContractError
    with pytest.raises(RuntimeContractError) as exc:value.require_loaded_source(changed)
    assert exc.value.code=='LOADED_SOURCE_CHANGED'


@pytest.mark.parametrize('harness',['codex','claude'])
def test_missing_copied_bootstrap_never_reuses_source_or_admission(seat,harness):
    value,events,_=seat
    if harness=='claude':claude_sources(value)
    from conversation_runtime_contract import RuntimeContractError
    Path(value.root/'fixture_bootstrap.py').unlink()
    with pytest.raises(RuntimeContractError) as exc:value.candidate_fingerprint(harness,'selected-model','high')
    assert exc.value.code=='SOURCE_INVALID' and not events
