"""Owner-checked run links and read-only receipt/ancestry projections.

attached_pr stores the engine-wide subscription identity, never a bare GitHub
number. Git probes happen only during projection, outside write transactions.
"""
from __future__ import annotations

import json
import re
import subprocess

import db_driver
from runs import TERMINAL, RunStore

PR_LIMIT = 20
RECEIPT_LIMIT = 5


def ancestry(cwd: str, commit: str | None, head: str | None) -> str:
    if not isinstance(commit, str) or not isinstance(head, str):
        return 'unknown'
    if not all(re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', v) for v in (commit, head)):
        return 'unknown'
    try:
        result = subprocess.run(['git', '-C', cwd, 'merge-base', '--is-ancestor', commit, head],
                                capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    return {0: 'current', 1: 'stale'}.get(result.returncode, 'unknown')


class RunAttachments:
    def __init__(self, con):
        self.con = con
        self.store = RunStore(con)

    def targets(self, owner: int) -> list[dict]:
        prs = self.con.execute('SELECT subscription_id,repository,pr_number FROM pr_subscriptions '
                               'WHERE owner_shell_id=? ORDER BY subscription_id DESC', (owner,)).fetchall()
        units = self.con.execute('SELECT work_unit_id,title FROM sprint_work_units '
                                 'WHERE assigned_shell_id=? ORDER BY work_unit_id DESC', (owner,)).fetchall()
        return [{'kind': 'pr', 'id': r['pr_number'], 'repository': r['repository'],
                     'label': f"{r['repository']}#{r['pr_number']}"} for r in prs] + [
            {'kind': 'work_unit', 'id': r['work_unit_id'], 'label': f"U{r['work_unit_id']} {r['title']}"} for r in units]

    def _target(self, row: dict, data: dict) -> tuple[str, int]:
        owner = row['owner_shell_id']
        if not isinstance(data, dict) or set(data) - {'pr', 'work_unit', 'repository'}:
            raise ValueError('attach accepts a target id only, never receipt text')
        kind = 'pr' if 'pr' in data else 'work_unit'
        if ('pr' in data) == ('work_unit' in data) or type(data.get(kind)) is not int or data[kind] <= 0:
            raise ValueError('choose exactly one positive pr or work_unit id')
        if 'repository' in data and (kind != 'pr' or not isinstance(data['repository'], str)):
            raise ValueError('repository requires a PR target')
        if row['state'] not in TERMINAL:
            raise ValueError('only terminal runs can be attached')
        if kind == 'pr':
            targets = self.con.execute('SELECT * FROM pr_subscriptions WHERE owner_shell_id=? '
                                       'AND pr_number=? AND (? IS NULL OR repository=?)',
                                       (owner, data['pr'], data.get('repository'), data.get('repository'))).fetchall()
            if not targets:
                raise PermissionError('PR is not registered to the run owner')
            if len(targets) != 1:
                raise ValueError('ambiguous PR number; specify repository')
            target = targets[0]['subscription_id']
            other = row['attached_work_unit']
            registered = targets[0]['sprint_registered_pr_id']
        else:
            target = data['work_unit']
            unit = self.con.execute('SELECT assigned_shell_id FROM sprint_work_units WHERE work_unit_id=?', (target,)).fetchone()
            if not unit or unit[0] != owner:
                raise PermissionError('work unit is not assigned to the run owner')
            other = target if row['attached_pr'] else None
            pr = self.con.execute('SELECT sprint_registered_pr_id FROM pr_subscriptions WHERE subscription_id=?',
                                  (row['attached_pr'],)).fetchone()
            registered = pr[0] if pr else None
        if other and not self.con.execute('SELECT 1 FROM sprint_pr_work_units WHERE registered_pr_id=? AND work_unit_id=?',
                                          (registered, other)).fetchone():
            raise ValueError('PR and work-unit attachment conflict')
        column = 'attached_pr' if kind == 'pr' else 'attached_work_unit'
        if row[column] is not None and row[column] != target:
            raise ValueError('attachment conflict: run already linked to a different target')
        return column, target

    def attach(self, run_id: int, actor: int | None, data: dict) -> dict:
        # The GUI acts for the run's owner; it cannot cross the owner's target boundary.
        refusal = None
        with db_driver.write_transaction(self.con, 'runs.attach'):
            row = self.store.get(run_id)
            try:
                if actor is not None and actor != row['owner_shell_id']:
                    raise PermissionError('run belongs to another shell')
                column, target = self._target(row, data)
                if row[column] == target:
                    return row
                self.con.execute(f'UPDATE runs SET {column}=? WHERE run_id=?', (target, run_id))
            except (PermissionError, ValueError) as exc:
                refusal = exc
            # Log target ids, never rejected pasted evidence or arbitrary caller text.
            audit = {k: v for k, v in data.items() if k in {'pr', 'work_unit'} and type(v) is int} if isinstance(data, dict) else {}
            self.con.execute('INSERT INTO run_attachment_events(run_id,actor_shell_id,target_json,outcome,reason) VALUES(?,?,?,?,?)',
                             (run_id, actor, json.dumps(audit), 'refused' if refusal else 'attached', str(refusal) if refusal else None))
        if refusal:
            raise refusal
        return self.store.get(run_id, actor)

    def receipts(self, *, subscription: int | None = None, work_unit: int | None = None,
                 head: str | None = None) -> list[dict]:
        return self._receipts('attached_pr=? OR attached_work_unit=?', (subscription, work_unit), head)

    def _receipts(self, where: str, params: tuple, head: str | None) -> list[dict]:
        # Newest RECEIPT_LIMIT only: each receipt costs a git ancestry probe.
        rows = self.con.execute(f'SELECT run_id FROM runs WHERE {where} ORDER BY run_id DESC LIMIT ?',
                                (*params, RECEIPT_LIMIT)).fetchall()
        result = []
        for item in rows:
            row = self.store.get(item[0])
            receipt = row['receipt_json'] or {}
            result.append({'run_id': row['run_id'], 'kind': row['kind'], 'hook': receipt.get('hook'),
                           'commit': row['commit'], 'state': row['state'], 'exit_code': row['exit_code'],
                           'summary': receipt.get('summary'), 'evidence_pruned': bool(row['evidence_pruned']),
                           'log_url': f"/api/runs/{row['run_id']}/tail",
                           'ancestry': ancestry(row['cwd'], row['commit'], head), 'head_sha': head})
        return result

    def pr_receipts(self, repository: str, number: int, head: str | None) -> list[dict]:
        pr = self.con.execute('SELECT subscription_id FROM pr_subscriptions WHERE repository=? AND pr_number=?',
                              (repository, number)).fetchone()
        if not pr:
            return []
        return self._receipts('attached_pr=? OR attached_work_unit IN (SELECT link.work_unit_id FROM sprint_pr_work_units link '
                              'JOIN pr_subscriptions p ON p.sprint_registered_pr_id=link.registered_pr_id '
                              'WHERE p.subscription_id=?)', (pr[0], pr[0]), head)

    def prs(self, owner: int) -> dict:
        """Open PRs first, then newest; capped at PR_LIMIT with the owner's full total."""
        rows = self.con.execute('SELECT p.*,t.normalized_state,t.observed_head_sha,t.observed_at FROM pr_subscriptions p '
                               'LEFT JOIN pr_subscription_transitions t ON t.transition_id=(SELECT MAX(transition_id) '
                               'FROM pr_subscription_transitions WHERE subscription_id=p.subscription_id) '
                               "WHERE p.owner_shell_id=? ORDER BY COALESCE(t.normalized_state IN ('merged','closed'),0), "
                               'p.subscription_id DESC LIMIT ?', (owner, PR_LIMIT)).fetchall()
        total = self.con.execute('SELECT count(*) FROM pr_subscriptions WHERE owner_shell_id=?', (owner,)).fetchone()[0]
        return {'prs': [dict(r, receipts=self.pr_receipts(r['repository'], r['pr_number'], r['observed_head_sha']))
                        for r in rows], 'total': total}
