"""Spec #222 metrics against real captured transcripts, not authored events."""
import importlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder/scripts'))
from bench_metrics import TOKEN_KEYS, derive_metrics  # noqa: E402

FIXTURES = ROOT / 'tests/fixtures'
CODEX = FIXTURES / 'bench_metrics/codex'
CLAUDE = FIXTURES / 'live_model/claude/projects'
OPENCODE = FIXTURES / 'live_model/opencode/opencode.db'
CASES = [
    ('codex', CODEX, '/tmp', next(CODEX.rglob('*.jsonl')),
     '2026-09-07T19:51:30.000Z', '2026-09-07T19:51:44.000Z',
     8013, [3740, 25472, None, 93, 22], [2, 1, 1, 1]),
    ('claude', CLAUDE, '/home/j3d1/dos-arch', next(CLAUDE.glob('*pln1/*.jsonl')),
     '2026-07-06T00:00:00Z', '2026-07-07T00:00:00Z',
     52733406, [6, 129109, 24703, 1813, None], [3, 2, 2, 2]),
    ('opencode', OPENCODE, '/tmp/lm-capture/oc-sub', 'ses_066f3adacffeX6QmjGUik7v26I',
     '2026-07-25T00:00:00Z', '2026-07-26T00:00:00Z',
     41549815, [23801, 0, 0, 406, 0], [3, 1, 1, 0]),
]


def project(case, **overrides):
    harness, source, repo, ref, start, end, *_ = case
    kw = dict(repo_root=repo, session_ref=ref, dispatched_at=start, ended_at=end)
    kw.update(overrides)
    return derive_metrics(harness, source, **kw)


def assert_metric(actual, value, fidelity):
    assert actual['value'] == value
    assert actual['fidelity'] == fidelity
    assert actual['note']


@pytest.mark.parametrize('case', CASES, ids=['codex', 'claude', 'opencode'])
def test_real_capture_values_fidelity_and_analytics_parity(case):
    harness, source, repo, ref, _, _, latency, tokens, activity = case
    receipt = project(case)
    assert_metric(receipt['timing']['wall_ms'], 14000 if harness == 'codex' else 86400000, 'full')
    assert_metric(receipt['timing']['first_token_ms'], latency, 'derived')
    assert_metric(receipt['timeout'], False, 'full')
    for key, value in zip(TOKEN_KEYS, tokens):
        assert_metric(receipt['tokens'][key], value, 'null' if value is None else 'full')
    for key, value in zip(('turns', 'tool_batches', 'tool_calls', 'shell_execs'), activity):
        assert_metric(receipt['tools'][key], value, 'derived')
    parser = importlib.import_module('token_parsers.' + harness)
    rows = [r for r in parser.sweep(Path(repo), lambda _: None, print, source=source)
            if r['harness_session_ref'] == str(ref)]
    assert rows
    for key in TOKEN_KEYS:
        values = [r[key] for r in rows]
        total = sum(values) if all(v is not None for v in values) else None
        assert receipt['tokens'][key]['value'] == total


def test_claude_subagents_and_content_blocks_are_counted_once():
    ref = next(CLAUDE.glob('*claude-sub/*.jsonl'))
    receipt = derive_metrics('claude', CLAUDE, repo_root='/tmp/lm-capture/claude-sub',
                             session_ref=ref, dispatched_at='2026-07-25T00:00:00Z',
                             ended_at='2026-07-26T00:00:00Z')
    # Three parent records are two messages; two child blocks share one id.
    assert_metric(receipt['tools']['turns'], 3, 'derived')
    assert_metric(receipt['tools']['tool_calls'], 1, 'derived')
    assert_metric(receipt['tools']['tool_batches'], 1, 'derived')
    assert_metric(receipt['tools']['shell_execs'], 0, 'derived')
    parser = importlib.import_module('token_parsers.claude')
    rows = parser.sweep(Path('/tmp/lm-capture/claude-sub'), lambda _: None, print, source=CLAUDE)
    assert len(rows) == 2  # sonnet parent + haiku subagent, same analytics ref
    for key in TOKEN_KEYS:
        if key != 'reasoning_tokens':
            assert receipt['tokens'][key]['value'] == sum(r[key] for r in rows)


def test_real_quota_failure_has_null_usage_and_latency_but_measured_zero_activity():
    source = FIXTURES / 'live_model/codex/sessions'
    ref = next(source.rglob('*.jsonl'))
    meta = json.loads(ref.read_text().splitlines()[0])['payload']
    result = derive_metrics('codex', source, session_ref=ref, repo_root=meta['cwd'],
                            dispatched_at='2026-07-25T00:00:00Z', ended_at='2026-07-26T00:00:00Z',
                            timeout=True)
    assert_metric(result['timeout'], True, 'full')
    assert_metric(result['timing']['first_token_ms'], None, 'null')
    for actual in result['tokens'].values():
        assert_metric(actual, None, 'null')
    for actual in result['tools'].values():
        assert_metric(actual, 0, 'derived')


def test_opencode_missing_detail_tables_downgrades_to_null(tmp_path):
    db = tmp_path / 'capture.db'
    shutil.copyfile(OPENCODE, db)
    with sqlite3.connect(db) as con:
        con.execute('DROP TABLE part')
    case = list(CASES[2])
    case[1] = db
    result = project(case)
    assert_metric(result['tokens']['cache_write_tokens'], 0, 'full')
    assert_metric(result['timing']['first_token_ms'], None, 'null')
    for actual in result['tools'].values():
        assert_metric(actual, None, 'null')


@pytest.mark.parametrize('overrides', [
    {'dispatched_at': '2027-01-01T00:00:00Z'},
    {'dispatched_at': '2026-09-07T19:51:40Z'},
    {'ended_at': '2026-09-07T19:51:35Z'},
    {'dispatched_at': '2026-09-07T19:51:30'},
    {'timeout': 'false'},
    {'repo_root': '/unrelated'},
    {'session_ref': '/etc/passwd'},
])
def test_invalid_clock_or_wrong_session_rejected(overrides):
    with pytest.raises(ValueError):
        project(CASES[0], **overrides)


def test_corrupted_capture_is_not_reported_as_zero(tmp_path):
    shutil.copytree(CODEX, tmp_path / 'sessions')
    ref = next((tmp_path / 'sessions').rglob('*.jsonl'))
    with ref.open('a') as stream:
        stream.write('{truncated')
    case = list(CASES[0])
    case[1], case[3] = tmp_path / 'sessions', ref
    with pytest.raises(ValueError):
        project(case)


def test_unknown_harness_and_unknown_opencode_session_rejected():
    case = list(CASES[0])
    case[0] = 'unknown'
    with pytest.raises(ValueError):
        project(case)
    with pytest.raises(ValueError):
        project(CASES[2], session_ref='not-the-captured-session')
