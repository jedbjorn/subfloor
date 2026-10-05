"""Dev-kit provenance and the owner-authenticated run receipt lifecycle."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def provenance(checkout):
    def git(*args):
        result = subprocess.run(['git', '-C', str(checkout), *args],
                                capture_output=True, text=True, check=False)
        return result.stdout.strip() or None
    return {'commit': git('rev-parse', '--verify', 'HEAD'),
            'branch': git('branch', '--show-current')}


def pytest_summary(path):
    """Only a complete pytest terminal summary establishes numeric counts."""
    summary = None
    failing = []
    failing_bytes = 0
    truncated = False
    ansi = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')
    with path.open('rb') as stream:
        # Bound individual lines, not the number of lines in a large test log.
        while raw := stream.readline(65537):
            if len(raw) > 65536:
                while raw and not raw.endswith(b'\n'):
                    raw = stream.readline(65537)
                continue
            line = ansi.sub(b'', raw).decode(errors='replace').strip()
            match = re.match(r'^(?:FAILED|ERROR) (.+?)(?: - .*)?$', line)
            if match:
                size = len(json.dumps(match[1]).encode())
                if len(failing) < 1000 and failing_bytes + size < 131072:
                    failing.append(match[1])
                    failing_bytes += size
                else:
                    truncated = True
            terminal = re.fullmatch(r'=*\s*(.*?) in \d+(?:\.\d+)?s(?: \([^\n]*\))?\s*=*', line)
            if not terminal:
                continue
            counts = re.findall(r'(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?|subtests? passed)', terminal[1])
            # Do not mistake progress, arbitrary prose, or a truncated count for a summary.
            remainder = re.sub(r'\d+ (?:subtests? passed|passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)', '', terminal[1])
            if not counts or remainder.strip(' ,='):
                continue
            summary = {'framework': 'pytest', 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
            for number, category in counts:
                key = 'errors' if category in ('error', 'errors') else category
                if key in summary:
                    summary[key] = int(number)
    if summary is not None:
        summary['failing'] = list(dict.fromkeys(failing))
        if truncated:
            summary['failing_truncated'] = True
    return summary


def api(environment, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(environment['SC_API_BASE'].rstrip('/') + path,
                                    data=data, method=method,
                                    headers={'Authorization': 'Bearer ' + environment['SC_API_TOKEN'],
                                             'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def atomic_json(path, data):
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    with temporary.open('w') as stream:
        json.dump(data, stream, ensure_ascii=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class RunReceipt:
    def __init__(self, checkout, hook, argv, environment, source):
        self.environment = environment
        self.row = None
        self.meta = None
        self.wrapped = environment.get('SC_DEVKIT_RUN_ID')
        if environment.get('SC_SHELL_FLAVOR') == 'admin':
            return
        authenticated = environment.get('SC_API_TOKEN') and environment.get('SC_API_BASE')
        if bool(environment.get('SC_API_TOKEN')) != bool(environment.get('SC_API_BASE')):
            raise ValueError('incomplete shell API wiring; no hook launched')
        if self.wrapped and not authenticated:
            raise ValueError('wrapped dev-kit run requires shell authentication')
        if not authenticated:
            return  # standalone operator / CI: local receipt, no shell wake claim
        if self.wrapped:
            self.row = api(environment, 'GET', f'/_sc/runs/{int(self.wrapped)}')
            if self.row['kind'] != 'devkit' or self.row['state'] != 'running':
                raise ValueError('wrapped dev-kit run must be a running devkit run')
            if self.row['commit'] != source['commit']:
                raise ValueError('checkout changed since job registration; no hook launched')
            return
        self.row = api(environment, 'POST', '/_sc/runs', {
            'registration_key': uuid.uuid4().hex, 'kind': 'devkit',
            'label': f'devkit-{hook.name}', 'argv': list(argv),
            'cwd': str(hook.cwd), 'commit': source['commit']})
        import job
        directory = Path(self.row['evidence_path']).parent
        directory.mkdir(parents=True, exist_ok=False)
        self.meta = {'run_id': self.row['run_id'], 'job_id': str(self.row['run_id']),
                     'kind': 'devkit', 'cmd': list(argv), 'cwd': str(hook.cwd),
                     'label': f'devkit-{hook.name}', 'log': self.row['evidence_path'],
                     'supervisor_pid': os.getpid(), 'supervisor_start_ticks': job._start_ticks(os.getpid()),
                     'boot_id': job.boot_id(), 'started_at': job._now()}
        job.write_meta(directory, self.meta)
        api(environment, 'POST', f"/_sc/runs/{self.row['run_id']}/running", self.meta)

    def started(self, process, log):
        if self.meta is None:
            return
        import job
        self.meta.update(pid=process.pid, start_ticks=job._start_ticks(process.pid))
        job.write_meta(Path(self.row['evidence_path']).parent, self.meta)
        Path(self.row['evidence_path']).symlink_to(log)
        # Local identity already allows the reconciler to recover a failed check-in.
        try:
            api(self.environment, 'POST', f"/_sc/runs/{self.row['run_id']}/running", self.meta)
        except (OSError, urllib.error.URLError):
            print('dev-kit: process check-in deferred to reconciliation', file=sys.stderr)

    def finish(self, receipt, log):
        if not self.row:
            return
        import job
        directory = Path(self.row['evidence_path']).parent
        atomic_json(directory / 'receipt.json', receipt)
        if self.meta is not None:
            link = Path(self.row['evidence_path'])
            link.unlink(missing_ok=True)
            link.symlink_to(log)
            self.meta.update(exit_code=receipt['exit_status'], finished_at=job._now())
            job.write_meta(directory, self.meta)
        try:
            api(self.environment, 'POST', f"/_sc/runs/{self.row['run_id']}/receipt", receipt)
            if self.meta is not None:
                api(self.environment, 'POST', f"/_sc/runs/{self.row['run_id']}/terminal", job.terminal_payload(self.meta))
        except (OSError, urllib.error.URLError):
            print('dev-kit: receipt/outcome persisted locally; API submission pending reconciliation', file=sys.stderr)


def prune_pair(log, environment):
    receipt = log.with_suffix('.receipt.json')
    if receipt.exists():
        try:
            data = json.loads(receipt.read_text())
        except (OSError, ValueError):
            return  # a concurrent pruner or invalid receipt must not lose its pair
        if data.get('run_id'):
            try:
                api(environment, 'POST', f"/_sc/runs/{data['run_id']}/prune-evidence", {})
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                return  # retain both until the ledger durably acknowledges pruning
    log.unlink(missing_ok=True)
    receipt.unlink(missing_ok=True)
