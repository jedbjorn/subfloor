"""Bounded projection of the installed Codex-generated native interface.

This structural observation cannot certify behavior or cleanup. An owner runs
only the fixed generation command inside its registered isolated boundary;
this module never reads account data, selects credentials, or launches a turn.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conversation_runtime_checks import Diagnostic, required_subset
from conversation_runtime_contract import ExecutableBinding, Grade

SCHEMA_SECONDS = 20.0
MAX_SCHEMA_BYTES = 8 * 1024 * 1024
RESPONSE_FILES = {
    'initialize': 'v1/InitializeResponse.json', 'account/read': 'v2/GetAccountResponse.json',
    'model/list': 'v2/ModelListResponse.json', 'config/read': 'v2/ConfigReadResponse.json',
    'thread/start': 'v2/ThreadStartResponse.json', 'turn/start': 'v2/TurnStartResponse.json',
    'thread/read': 'v2/ThreadReadResponse.json', 'thread/list': 'v2/ThreadListResponse.json',
    'thread/backgroundTerminals/list': 'v2/ThreadBackgroundTerminalsListResponse.json',
    'thread/backgroundTerminals/terminate': 'v2/ThreadBackgroundTerminalsTerminateResponse.json',
}
# Only fields actually supplied/consumed by the current experimental driver.
REQUEST_FIELDS = {
    'initialize': {'clientInfo.name': 'string', 'clientInfo.version': 'string', 'capabilities.experimentalApi': 'boolean'},
    'account/read': {'refreshToken': 'boolean'},
    'model/list': {'cursor': 'string', 'limit': 'integer', 'includeHidden': 'boolean'},
    'config/read': {'cwd': 'string', 'includeLayers': 'boolean'},
    'thread/start': {'model': 'string', 'cwd': 'string', 'developerInstructions': 'string',
                     'approvalPolicy': 'string', 'sandbox': 'string', 'config': 'object',
                     'modelProvider': 'string', 'allowProviderModelFallback': 'boolean'},
    'thread/memoryMode/set': {'threadId': 'string', 'mode': 'string'},
    'turn/start': {'threadId': 'string', 'input': 'array', 'input.*.@text.text': 'string',
                   'input.*.@text.text_elements': 'array', 'input.*.@text.type': 'string', 'model': 'string', 'effort': 'string'},
    'thread/read': {'threadId': 'string', 'includeTurns': 'boolean'},
    'thread/list': {'ancestorThreadId': 'string', 'sourceKinds': 'array', 'useStateDbOnly': 'boolean',
                    'limit': 'integer', 'cursor': 'string'},
    'turn/interrupt': {'threadId': 'string', 'turnId': 'string'},
    'thread/backgroundTerminals/list': {'threadId': 'string', 'limit': 'integer', 'cursor': 'string'},
    'thread/backgroundTerminals/terminate': {'threadId': 'string', 'processId': 'string'},
    'thread/backgroundTerminals/clean': {'threadId': 'string'},
}
PROVIDED_INPUTS = {method: {path.split('.')[0] for path in fields} for method, fields in REQUEST_FIELDS.items()}
PROVIDED_INPUTS['initialize'] = {'clientInfo', 'capabilities'}
RESULT_FIELDS = {
    'initialize': {'userAgent': 'string'}, 'account/read': {'account.@chatgpt.type': 'string'},
    'model/list': {'data': 'array', 'data.*.id': 'string', 'data.*.supportedReasoningEfforts': 'array',
                   'data.*.supportedReasoningEfforts.*.reasoningEffort': 'string'},
    'config/read': {'config': 'object'},  # memory flags are an opaque config map: behavior checks consume them.
    'thread/start': {'thread.id': 'string', 'thread.cwd': 'string'},
    'turn/start': {'turn.id': 'string'},
    'thread/read': {'thread.id': 'string', 'thread.turns': 'array', 'thread.turns.*.id': 'string',
                    'thread.turns.*.status': 'string'},
    'thread/list': {'data': 'array', 'data.*.id': 'string'},
    'thread/backgroundTerminals/list': {'data': 'array', 'data.*.itemId': 'string',
        'data.*.processId': 'string', 'data.*.command': 'string', 'data.*.cwd': 'string'},
    'thread/backgroundTerminals/terminate': {'terminated': 'boolean'},
}
EVENT_FIELDS = {
    'turn/started': {'threadId': 'string', 'turn.id': 'string'},
    'turn/completed': {'threadId': 'string', 'turn.id': 'string', 'turn.status': 'string'},
    'item/agentMessage/delta': {'threadId': 'string', 'turnId': 'string', 'itemId': 'string', 'delta': 'string'},
    'item/commandExecution/outputDelta': {'threadId': 'string', 'turnId': 'string', 'itemId': 'string', 'delta': 'string'},
    'item/completed': {'threadId': 'string', 'turnId': 'string', 'item.@agentMessage.id': 'string',
        'item.@agentMessage.text': 'string', 'item.@commandExecution.id': 'string',
        'item.@commandExecution.processId': 'string', 'item.@commandExecution.status': 'string',
        'item.@commandExecution.aggregatedOutput': 'string', 'item.@subAgentActivity.id': 'string',
        'item.@subAgentActivity.agentThreadId': 'string'},
}
START_METHODS = {'initialize', 'account/read', 'model/list', 'config/read', 'thread/start', 'thread/memoryMode/set'}
METHODS_BY_CAP = {
    'submission': START_METHODS | {'turn/start', 'thread/read'},
    'stop_reply': START_METHODS | {'turn/interrupt', 'thread/read'},
    'stop_work': START_METHODS | {'thread/read', 'thread/list', 'thread/backgroundTerminals/list',
                                'thread/backgroundTerminals/terminate', 'thread/backgroundTerminals/clean'},
}
CHILD_FIELDS = {'data.*.parentThreadId': 'string', 'data.*.canAcceptDirectInput': 'boolean'}
CAPABILITIES = (*METHODS_BY_CAP, 'stop_work_terminal', 'stop_work_child')
ASSISTANT_FIELDS = {key: value for key, value in EVENT_FIELDS['item/completed'].items()
                    if key in {'threadId', 'turnId'} or '@agentMessage' in key}
TERMINAL_FIELDS = {key: value for key, value in EVENT_FIELDS['item/completed'].items()
                   if key in {'threadId', 'turnId'} or '@commandExecution' in key}
CHILD_ITEM_FIELDS = {key: value for key, value in EVENT_FIELDS['item/completed'].items()
                     if key in {'threadId', 'turnId'} or '@subAgentActivity' in key}


class SchemaUnavailable(ValueError):
    pass


def _options(node: Any, root: Mapping[str, Any], depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 24 or not isinstance(node, Mapping):
        raise SchemaUnavailable('unsupported native schema layout')
    if '$ref' in node:
        ref = node['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/'):
            raise SchemaUnavailable('nonlocal native schema reference')
        target: Any = root
        for part in ref[2:].split('/'):
            if not isinstance(target, Mapping):
                raise SchemaUnavailable('unresolved native schema reference')
            target = target.get(part.replace('~1', '/').replace('~0', '~'))
        return _options(target, root, depth+1)
    if 'allOf' in node:
        branches = node['allOf']
        # Schemars wraps annotated local references in a one-element allOf.
        if (not isinstance(branches, list) or len(branches) != 1
                or set(node) - {'allOf', 'description', 'default', 'title'}):
            raise SchemaUnavailable('unsupported native schema composition')
        return _options(branches[0], root, depth+1)
    union = node.get('oneOf', node.get('anyOf'))
    if union is not None:
        if not isinstance(union, list) or len(union) > 256:
            raise SchemaUnavailable('unbounded native schema union')
        return [option for branch in union for option in _options(branch, root, depth+1)]
    return [node]


def _nodes(node: Any, path: str, root: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nodes = _options(node, root)
    for part in path.split('.') if path else ():
        found = []
        for item in nodes:
            if part == '*':
                child = item.get('items')
            elif part.startswith('@'):
                discriminator = item.get('properties', {}).get('type', {})
                child = item if part[1:] in discriminator.get('enum', []) else None
            else:
                properties = item.get('properties', {})
                if not isinstance(properties, Mapping):
                    raise SchemaUnavailable('invalid native schema properties')
                child = properties.get(part)
            if child is not None:
                found.extend(_options(child, root))
        nodes = found
    return nodes


def _field(node: Any, path: str, root: Mapping[str, Any]) -> dict[str, Any]:
    nodes = _nodes(node, path, root)
    types: set[str] = set()
    enums: set[str] = set()
    for item in nodes:
        kind = item.get('type', 'object' if 'properties' in item else None)
        for value in kind if isinstance(kind, list) else [kind]:
            if value in {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}:
                types.add(value)
        enum = item.get('enum', [])
        if isinstance(enum, list):
            enums.update(value for value in enum if isinstance(value, str))
    result: dict[str, Any] = {'types': sorted(types)}
    if enums:
        result['enum'] = sorted(enums)
    return result


def _methods(document: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    options = document.get('oneOf')
    if not isinstance(options, list) or len(options) > 512:
        raise SchemaUnavailable('unsupported native method envelope')
    for item in options:
        for branch in _options(item, document):
            properties = branch.get('properties', {})
            if not isinstance(properties, Mapping):
                raise SchemaUnavailable('invalid native method envelope')
            method = properties.get('method', {})
            values = method.get('enum', []) if isinstance(method, Mapping) else []
            for name in values:
                if isinstance(name, str) and name in REQUEST_FIELDS.keys() | EVENT_FIELDS.keys():
                    result[name] = properties.get('params', {})
    return result


def _supplied_inputs(params: Any, method: str, root: Mapping[str, Any]) -> bool:
    objects: dict[str, set[str]] = {'': set(PROVIDED_INPUTS[method])}
    for path in REQUEST_FIELDS[method]:
        parts = path.split('.')
        for index, part in enumerate(parts):
            if part == '*' or part.startswith('@'):
                continue
            prefix = '.'.join(parts[:index])
            objects.setdefault(prefix, set()).add(part)
    if method == 'thread/start':
        objects['config'] = {'features.memories', 'memories.generate_memories', 'memories.use_memories'}
    for path, provided in objects.items():
        for item in _nodes(params, path, root):
            names = item.get('required', [])
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise SchemaUnavailable('invalid native required fields')
            if not set(names) <= provided:
                return False
    return True


def _project_field(node: Any, path: str, root: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _field(node, path, root)
    except SchemaUnavailable:
        return {'unavailable': True}


def _unavailable_consumed(observed: Any, required: Any) -> bool:
    if not isinstance(observed, Mapping) or not isinstance(required, Mapping):
        return False
    return observed.get('unavailable') is True or any(
        _unavailable_consumed(observed.get(key), value) for key, value in required.items())


def _required(fields: Mapping[str, str]) -> dict[str, Any]:
    return {path: {'types': [kind]} for path, kind in fields.items()}


@dataclass(frozen=True)
class NativeSchemaObservation:
    executable: ExecutableBinding
    observed_interface: Mapping[str, Any] = field(default_factory=dict)
    requirements: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()
    schema_files_sha256: Mapping[str, str] = field(default_factory=dict)
    generation_completed: bool = False

    @property
    def structural_grades(self) -> dict[str, Grade]:
        grades: dict[str, Grade] = {cap: 'compatible' for cap in CAPABILITIES}
        for diagnostic in self.diagnostics:
            grades[diagnostic.capability] = diagnostic.grade
        return grades


def project_codex_schema(directory: Path, executable: ExecutableBinding, *, effort: str | None = None) -> NativeSchemaObservation:
    """Project generated annotations; this is not a behavior certificate."""
    consumed = 0
    hashes = {}
    def read(name: str) -> dict:
        nonlocal consumed
        path = directory / name
        if any(parent.is_symlink() for parent in (directory, *(directory / parent for parent in path.relative_to(directory).parents if parent != Path('.')))):
            raise SchemaUnavailable('generated native schema directory unavailable')
        if path.is_symlink() or not path.is_file():
            raise SchemaUnavailable('generated native schema unavailable')
        size = path.stat().st_size
        if size > MAX_SCHEMA_BYTES or consumed+size > MAX_SCHEMA_BYTES:
            raise SchemaUnavailable('generated native schema exceeded bound')
        raw = path.read_bytes();consumed += len(raw)
        if len(raw) != size:
            raise SchemaUnavailable('generated native schema changed while reading')
        hashes[name] = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise SchemaUnavailable('invalid native schema object')
        return value
    try:
        requests, notifications = read('ClientRequest.json'), read('ServerNotification.json')
        request_methods, event_methods = _methods(requests), _methods(notifications)
        observed: dict[str, Any] = {'requests': {}, 'results': {}, 'events': {}}
        for method, fields in REQUEST_FIELDS.items():
            if method not in request_methods:
                continue
            params = request_methods[method]
            try:
                supplied = _supplied_inputs(params, method, requests)
                observed['requests'][method] = {'fields': {path: _project_field(params, path, requests) for path in fields},
                    'required_inputs_satisfied': supplied}
            except SchemaUnavailable:
                observed['requests'][method] = {'unavailable': True}
        for method, name in RESPONSE_FILES.items():
            document = read(name)
            fields = RESULT_FIELDS[method] | (CHILD_FIELDS if method == 'thread/list' else {})
            if method == 'thread/read':
                fields |= {'thread.status.type': 'string', 'thread.canAcceptDirectInput': 'boolean'}
            observed['results'][method] = {path: _project_field(document, path, document) for path in fields}
        for method, fields in EVENT_FIELDS.items():
            if method in event_methods:
                observed['events'][method] = {path: _project_field(event_methods[method], path, notifications) for path in fields}
        requirements = {}
        for cap, methods in METHODS_BY_CAP.items():
            requirement: dict[str, Any] = {'requests': {m: {'fields': _required(REQUEST_FIELDS[m]),
                'required_inputs_satisfied': True} for m in methods},
                'results': {m: _required(RESULT_FIELDS[m]) for m in methods if m in RESULT_FIELDS},
                'events': {m: _required(EVENT_FIELDS[m]) for m in ('turn/started', 'turn/completed')}}
            if cap == 'submission':
                requirement['events'].update({'item/agentMessage/delta': _required(EVENT_FIELDS['item/agentMessage/delta']),
                                               'item/completed': _required(ASSISTANT_FIELDS)})
                if (effort not in {None, 'default'}
                        and 'enum' in observed.get('requests', {}).get('turn/start', {}).get('fields', {}).get('effort', {})):
                    requirement['requests']['turn/start']['fields']['effort']['enum'] = [effort]
            requirement['requests']['thread/memoryMode/set']['fields']['mode']['enum'] = ['disabled']
            requirement['requests']['thread/start']['fields']['approvalPolicy']['enum'] = ['never']
            requirement['requests']['thread/start']['fields']['sandbox']['enum'] = ['danger-full-access']
            if cap == 'stop_work':
                requirement['events'].update({'item/commandExecution/outputDelta': _required(EVENT_FIELDS['item/commandExecution/outputDelta']),
                                               'item/completed': _required(TERMINAL_FIELDS)})
            requirements[cap] = requirement
        requirements['stop_work_terminal'] = requirements['stop_work']
        child_requirement = {'requests': {m: {'fields': _required(REQUEST_FIELDS[m]), 'required_inputs_satisfied': True}
                                         for m in START_METHODS | {'thread/list', 'thread/read', 'turn/interrupt'}},
                             'results': {'thread/list': _required(CHILD_FIELDS),
                                         'thread/read': _required({'thread.status.type': 'string', 'thread.canAcceptDirectInput': 'boolean'})},
                             'events': {'item/completed': _required(CHILD_ITEM_FIELDS)}}
        child_requirement['requests']['thread/memoryMode/set']['fields']['mode']['enum'] = ['disabled']
        child_requirement['requests']['thread/start']['fields']['approvalPolicy']['enum'] = ['never']
        child_requirement['requests']['thread/start']['fields']['sandbox']['enum'] = ['danger-full-access']
        child_requirement['results'].update({m: _required(RESULT_FIELDS[m]) for m in START_METHODS if m in RESULT_FIELDS})
        requirements['stop_work_child'] = child_requirement
        diagnostics = tuple(Diagnostic(cap, 'inconclusive' if _unavailable_consumed(observed, required) else 'incompatible',
                                       'NATIVE_SCHEMA_UNAVAILABLE' if _unavailable_consumed(observed, required) else 'REQUIRED_NATIVE_INTERFACE_MISMATCH')
                            for cap, required in requirements.items() if required_subset(observed, required))
        return NativeSchemaObservation(executable, observed, requirements, diagnostics, hashes, True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        diagnostics = tuple(Diagnostic(cap, 'inconclusive', 'NATIVE_SCHEMA_UNAVAILABLE')
                            for cap in CAPABILITIES)
        return NativeSchemaObservation(executable, diagnostics=diagnostics)


def observe_codex_schema(executable: ExecutableBinding, directory: Path, *, effort: str | None,
                         run_owned: Callable[[tuple[str, ...], float], bool], deadline: float) -> NativeSchemaObservation:
    """Owner runner enforces its registered process/unit and output ownership.

    It receives this fixed argv, never browser input, and must wait/reap within
    deadline. Generation is codegen only: no account RPC, model or fallback.
    """
    deadline = min(deadline, time.monotonic()+SCHEMA_SECONDS)
    try:
        before = hashlib.sha256(executable.path.read_bytes()).hexdigest()
        if before != executable.sha256 or time.monotonic() >= deadline:
            raise SchemaUnavailable('captured executable differs')
        command = (str(executable.path), 'app-server', 'generate-json-schema', '--experimental', '--out', str(directory))
        if not run_owned(command, deadline) or time.monotonic() >= deadline:
            raise SchemaUnavailable('bounded generation unavailable')
        if hashlib.sha256(executable.path.read_bytes()).hexdigest() != before:
            raise SchemaUnavailable('installed executable changed during generation')
        result = project_codex_schema(directory, executable, effort=effort)
        if (time.monotonic() >= deadline
                or hashlib.sha256(executable.path.read_bytes()).hexdigest() != before):
            raise SchemaUnavailable('captured executable changed while reading schema')
        return result
    except (OSError, ValueError, RuntimeError):
        return NativeSchemaObservation(executable, diagnostics=tuple(
            Diagnostic(cap, 'inconclusive', 'NATIVE_SCHEMA_UNAVAILABLE') for cap in CAPABILITIES))
