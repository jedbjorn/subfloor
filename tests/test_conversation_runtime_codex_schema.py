"""Recorded native codegen subset and owned fake runner; zero auth/inference."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_runtime_codex_schema import (
    CAPABILITIES,
    observe_codex_schema,
    project_codex_schema,
)
from conversation_runtime_contract import ExecutableBinding

FIXTURE = Path(__file__).parent / 'fixtures/codex_native_schema'


@pytest.fixture
def recorded(tmp_path):
    directory = tmp_path / 'schema'
    shutil.copytree(FIXTURE, directory)
    binary = tmp_path / 'codex'; binary.write_bytes(b'owned codegen fake')
    return directory, ExecutableBinding(binary, hashlib.sha256(binary.read_bytes()).hexdigest(), 'recorded')


def edit(directory, file, change):
    path = directory / file; value = json.loads(path.read_text())
    change(value); path.write_text(json.dumps(value))


def params(document, method):
    node = next(v['properties']['params'] for v in document['oneOf']
                if method in v['properties']['method']['enum'])
    return document['definitions'][node['$ref'].split('/')[-1]] if '$ref' in node else node


def observe(recorded, effort='high'):
    return project_codex_schema(*recorded, effort=effort)


def test_recorded_generated_subset_and_provenance(recorded):
    result = observe(recorded)
    assert result.generation_completed
    assert set(result.structural_grades.values()) == {'compatible'}
    assert result.observed_interface['requests']['turn/start']['fields']['effort']['types'] == ['null', 'string']
    provenance = json.loads((FIXTURE / 'provenance.json').read_text())
    for name, hashes in provenance['files'].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == hashes['fixture_sha256']
        assert result.schema_files_sha256[name] == hashes['fixture_sha256']
    # Only structural grades: neither capability cache nor cleanup evidence.
    assert not hasattr(result, 'coverage')
    assert 'account' not in repr(result.observed_interface.get('private', {}))


def test_additive_methods_fields_enums_and_annotations(recorded):
    directory, _ = recorded
    def add(d):
        d['oneOf'].append({'type': 'object', 'properties': {'method': {'enum': ['future/new']},
                             'params': {'$ref': '#/definitions/UnknownFutureReference'}}})
        params(d, 'turn/start')['properties']['future'] = {'type': 'number'}
        d['definitions']['ThreadMemoryMode']['enum'].append('future')
    edit(directory, 'ClientRequest.json', add)
    assert set(observe(recorded).structural_grades.values()) == {'compatible'}


@pytest.mark.parametrize('method,caps', [
    ('turn/start', {'submission'}),
    ('turn/interrupt', {'stop_reply', 'stop_work_child'}),
    ('thread/backgroundTerminals/terminate', {'stop_work', 'stop_work_terminal'}),
])
def test_required_method_removal_is_scoped(recorded, method, caps):
    edit(recorded[0], 'ClientRequest.json', lambda d: d.__setitem__('oneOf', [v for v in d['oneOf']
         if method not in v['properties']['method']['enum']]))
    grades = observe(recorded).structural_grades
    assert {cap for cap, grade in grades.items() if grade == 'incompatible'} == caps


@pytest.mark.parametrize('mutation', ['missing', 'wrong_type'])
def test_consumed_field_type_or_removal(recorded, mutation):
    def change(d):
        p = params(d, 'item/agentMessage/delta')['properties']
        if mutation == 'missing': p.pop('turnId')
        else: p['turnId'] = {'type': 'integer'}
    edit(recorded[0], 'ServerNotification.json', change)
    assert observe(recorded).structural_grades['submission'] == 'incompatible'
    assert observe(recorded).structural_grades['stop_work_terminal'] == 'compatible'


def test_terminal_fields_dont_break_assistant_and_child_fields_are_distinct(recorded):
    edit(recorded[0], 'ServerNotification.json', lambda d:
         d['definitions']['ThreadItem']['oneOf'][next(i for i,v in enumerate(d['definitions']['ThreadItem']['oneOf'])
          if 'commandExecution' in v['properties']['type']['enum'])]['properties'].pop('processId'))
    assert observe(recorded).structural_grades['submission'] == 'compatible'
    assert observe(recorded).structural_grades['stop_work_terminal'] == 'incompatible'
    assert observe(recorded).structural_grades['stop_work_child'] == 'compatible'
    edit(recorded[0], 'v2/ThreadListResponse.json', lambda d: d['definitions']['Thread']['properties'].pop('parentThreadId'))
    assert observe(recorded).structural_grades['stop_work_child'] == 'incompatible'


def test_new_mandatory_input_and_removed_disabled_mode(recorded):
    edit(recorded[0], 'ClientRequest.json', lambda d: params(d, 'turn/start')['required'].append('new_required'))
    assert observe(recorded).structural_grades['submission'] == 'incompatible'
    edit(recorded[0], 'ClientRequest.json', lambda d: d['definitions']['ThreadMemoryMode']['enum'].remove('disabled'))
    assert set(observe(recorded).structural_grades.values()) == {'incompatible'}


@pytest.mark.parametrize('failure', ['missing_file', 'malformed', 'symlink', 'composition'])
def test_generation_layout_unavailable_is_inconclusive(recorded, failure):
    directory, _ = recorded
    file = directory / 'ClientRequest.json'
    if failure == 'missing_file': file.unlink()
    elif failure == 'malformed': file.write_text('not json')
    elif failure == 'symlink':
        moved = directory / 'private.json'; file.rename(moved); file.symlink_to(moved)
    else: edit(directory, 'ClientRequest.json', lambda d: params(d, 'turn/start').update(allOf=[{}, {}]))
    assert set(observe(recorded).structural_grades.values()) == {'inconclusive'}


def test_selected_effort_enum_addition_vs_removal(recorded):
    edit(recorded[0], 'ClientRequest.json', lambda d: d['definitions']['ReasoningEffort'].update(enum=['high', 'new']))
    assert observe(recorded).structural_grades['submission'] == 'compatible'
    assert observe(recorded, 'unsupported').structural_grades['submission'] == 'incompatible'


def test_fixed_bounded_owned_runner_and_identity(recorded):
    directory, binary = recorded; calls = []
    def run(argv, deadline):
        calls.append((argv, deadline)); return True
    before = time.monotonic()
    result = observe_codex_schema(binary, directory, effort='high', run_owned=run, deadline=before+100)
    assert result.generation_completed
    argv, deadline = calls[0]
    assert argv == (str(binary.path), 'app-server', 'generate-json-schema', '--experimental', '--out', str(directory))
    assert before < deadline <= before+20.1
    assert set(result.structural_grades) == set(CAPABILITIES)


@pytest.mark.parametrize('failure', ['expired', 'unavailable', 'changed_before', 'changed_after'])
def test_no_fallback_or_stale_generation(recorded, failure):
    directory, binary = recorded; calls = []
    if failure == 'changed_before': binary.path.write_bytes(b'replacement')
    def run(argv, deadline):
        calls.append(argv)
        if failure == 'changed_after': binary.path.write_bytes(b'replacement')
        return failure != 'unavailable'
    result = observe_codex_schema(binary, directory, effort='high', run_owned=run,
                                 deadline=time.monotonic()+(-1 if failure == 'expired' else 30))
    assert set(result.structural_grades.values()) == {'inconclusive'}
    assert not result.generation_completed
    assert len(calls) == (0 if failure in {'expired', 'changed_before'} else 1)


def test_unsupported_consumed_field_layout_only_degrades_affected_capability(recorded):
    edit(recorded[0], 'ServerNotification.json', lambda d:
         params(d, 'item/agentMessage/delta')['properties'].update(delta={'allOf': [{}, {}]}))
    grades = observe(recorded).structural_grades
    assert grades['submission'] == 'inconclusive'
    assert grades['stop_reply'] == grades['stop_work_terminal'] == grades['stop_work_child'] == 'compatible'


def test_executable_is_revalidated_after_projection(recorded, monkeypatch):
    import conversation_runtime_codex_schema as schema
    project = schema.project_codex_schema
    def replaced(*args, **kwargs):
        result = project(*args, **kwargs)
        recorded[1].path.write_bytes(b'replacement during schema read')
        return result
    monkeypatch.setattr(schema, 'project_codex_schema', replaced)
    result = schema.observe_codex_schema(recorded[1], recorded[0], effort='high',
                                       run_owned=lambda *args: True, deadline=time.monotonic()+10)
    assert set(result.structural_grades.values()) == {'inconclusive'}
    assert not result.generation_completed


def test_symlinked_schema_parent_is_unavailable(recorded):
    directory, _ = recorded
    target = directory / 'v2-private'; (directory / 'v2').rename(target)
    (directory / 'v2').symlink_to(target, target_is_directory=True)
    assert set(observe(recorded).structural_grades.values()) == {'inconclusive'}
