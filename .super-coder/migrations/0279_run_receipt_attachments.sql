-- 0279 — receipt attachment audit; attached_pr identifies pr_subscriptions.
BEGIN;
CREATE TABLE IF NOT EXISTS run_attachment_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    actor_shell_id INTEGER REFERENCES shells(shell_id),
    target_json TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('attached','refused')),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_run_attachment_events_run ON run_attachment_events(run_id,event_id);
CREATE INDEX IF NOT EXISTS idx_runs_attached_pr ON runs(attached_pr) WHERE attached_pr IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_attached_unit ON runs(attached_work_unit) WHERE attached_work_unit IS NOT NULL;
UPDATE shells SET system_prompt=replace(system_prompt, '## CODE CRAFT', '## RUN RECEIPTS

Attach completed local evidence by run id with `sc job attach <id> --pr <number>`
or `sc job attach <id> --work-unit <id>`. The run and target must share an owner;
a PR must already be registered. Use `--repository owner/name` if its number is
ambiguous. The Runs drawer and fleet offer the same attachment action.
Receipts show beside the observed GitHub check in Chats and the Sprint board.
A commit outside the PR head''s ancestry is stale; unavailable ancestry stays
unknown. Pruned logs retain their receipt. Receipts supplement CI evidence;
only GitHub checks and the existing merge grant authorize a merge.

## CODE CRAFT')
WHERE flavor='dev' AND instr(system_prompt, '## CODE CRAFT')>0 AND instr(system_prompt, '## RUN RECEIPTS')=0;
UPDATE skills SET content='# git — the event procedures

Your boot''s VERSION CONTROL section carries the every-session rules: sync the
base, branch before you build, commit → push → PR → stop, the merge gate''s two
forms, the disposable `shell/<shortname>` base, and the finish gate. This skill
holds what fires on an event.

One repo at its root -> plain `git` (cwd = repo root) is safe. Project = this
repo minus `.super-coder/`. In a tracking fork the engine (`.super-coder/`) is
gitignored, materialized by `sc update`, and authored upstream in Subfloor —
NEVER commit or edit anything under it. In the Subfloor source repository
(`git ls-files --error-unmatch .super-coder/schema.sql` exits 0) `.super-coder/`
is tracked project source and `.sc-state/engine.ref` is not the delivery unit;
engine changes still land by branch and PR.

## GitHub capability boundary

`sc launch` and `sc restart` re-resolve Git transport and GitHub API
capabilities from the host on every invocation, including `--no-build` forms.
`build`, `enter`, and an already running sandbox do not refresh auth. Pass = the
lifecycle summary says `ready` for the operation you need; `unavailable` and
`unverified` are NEVER readiness claims.

Preserve the configured `origin` transport. For SSH, fix the host agent and
load an authorized GitHub identity; NEVER copy or mount private keys. For
HTTPS/API, fix a scoped host `SC_GH_TOKEN` or the host `gh` OAuth login. Then
run `sc launch` or `sc restart`; the running sandbox remains unchanged
until that refresh. NEVER rewrite the remote or start an interactive login
inside the sandbox to work around a missing capability.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## After a merge — clean up local

Only after the PR is merged:

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## RUN RECEIPTS

Attach completed local evidence by run id with `sc job attach <id> --pr <number>`
or `sc job attach <id> --work-unit <id>`. The run and target must share an owner;
a PR must already be registered. Use `--repository owner/name` if its number is
ambiguous. The Runs drawer and fleet offer the same attachment action.
Receipts show beside the observed GitHub check in Chats and the Sprint board.
A commit outside the PR head''s ancestry is stale; unavailable ancestry stays
unknown. Pruned logs retain their receipt. Receipts supplement CI evidence;
only GitHub checks and the existing merge grant authorize a merge.

## Never commit the engine or derived files

- In a fork `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, the per-harness skill renders (`.claude/skills/`, `.agents/skills/`, `.opencode/skills/`), the engine-managed harness config (`.claude/settings.local.json`, `.codex/hooks.json`), and `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main`. Admin shell = the one exception: repo root on `main`, committing there by mandate.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.' WHERE name='git';
UPDATE skills SET content='# dev_kit — read the fork development contract

The fork owns `.subfloor/dev-kit.json`. Subfloor validates the declaration,
selects the invoking checkout and host/container seat, runs exact hook argv,
and retains readiness evidence under `.sc-state/local/dev-kit/`. The engine
does not infer project policy from manifests or install privileged host tools.

## Hook inventory

The supported hook names are `deps`, `test`, `lint`, and `typecheck`:

```bash
sc deps [args...]
sc test [args...]
sc lint [args...]
sc typecheck [args...]
```

Read the declaration and its executable before running a hook. A configured
hook reports the selected checkout, cwd, seat, executable, and child status.
An absent hook is `unavailable`; do not reconstruct one from package metadata.

## Canonical states

| State | Meaning | Supported recovery |
|---|---|---|
| `absent` | No declaration exists; engine-baseline tools remain mechanisms, not project policy. | Add a tracked declaration only when the fork needs one. |
| `declared` | The declaration is valid; hook configuration is known, but execution/receipt evidence decides readiness. | Run the exact configured hook. |
| `invalid` | Declaration, path, mount, image, or invocation validation failed. | Correct the named tracked input and retry. |
| `ready` | The hook can execute on the active seat or the exact Docker receipt is current. | Continue. |
| `failed` | A declared hook or provisioning attempt ran and failed. | Inspect retained logs/evidence; retry the same supported surface. |
| `stale` | Docker provisioning or package evidence no longer matches the declaration, checkout, image, or labels. | From the host, run `sc launch`; use repair only after a failed attempt. |
| `advisory` | Engine baseline is runnable while a declared native-package candidate is degraded. | Inspect the named advisory evidence and submit a reviewed tracked remediation. |
| `repair` | A retained-container repair session is open without readiness. | Exit to the host, rerun `sc launch`, and require `ready`. |

Unavailable executable = exit 126; missing hook = exit 78; invalid
configuration = exit 64. A started child preserves its own status.

## Seats and evidence

Host hooks use the host checkout and toolchain. Container hooks use the
bind-mounted checkout, engine-baseline tools, declared sandbox extension, and
current provisioning receipt. `$SC_DEV_PORT` is loopback-bound on the host and
published from `0.0.0.0` in the container. A configured `$DATABASE_URL` reaches
the fork application sidecar; it never points at the engine memory DB.

`SC_SEAT=gui` wraps shell `test`, `lint`, and `typecheck` invocations in one
registered job. After the job id is confirmed, end the turn; its completion
wake tells you to inspect `sc job status <id>` and `sc job tail <id>` before
continuing. TUI and Admin hooks remain foreground without a completion wake
and return the child status; `deps` remains foreground in every seat. The wrapper marker is consumed by the
runner and removed from the hook''s environment, so nested hooks get their own
foreground run and return their real status without a completion wake. A
separate nesting marker prevents GUI wrapping inside a hook.

Every executed hook retains a log and adjacent `.receipt.json` under
`.sc-state/local/devkit-logs/<hook>/`. The receipt records declared argv with
appended arguments, checkout, host/container seat, starting commit and branch,
exit status, duration and log path. Test receipts parse pytest''s terminal
summary and failing ids; an absent summary is `null`, never zero failures.
Authenticated shell runs register before execution; wrapped hooks attach to
the existing run. Standalone CI and Admin retain local receipts without
claiming a shell run or completion wake. Receipts are local test evidence;
GitHub checks still own the merge gate.

Pruning retains the newest twenty completed log/receipt pairs per hook and
keeps ledger receipts with `evidence_pruned` set. If that acknowledgment is
unavailable, the files remain for a later pruning attempt. Submission outages
retain the run''s receipt and outcome for reconciliation, without rerunning the
hook. `SC_DEVKIT_OUTPUT=full` streams stdout and stderr while retaining the same
log and receipt. Provisioning/readiness evidence lives under
`.sc-state/local/dev-kit/`. Planner
uses this skill for pinch-hit development and capability design. It describes
the available surface and boundaries, not the fork''s test assertions,
deployment ritual, database technique, or VM lifecycle.

## RUN RECEIPTS

Attach completed local evidence by run id with `sc job attach <id> --pr <number>`
or `sc job attach <id> --work-unit <id>`. The run and target must share an owner;
a PR must already be registered. Use `--repository owner/name` if its number is
ambiguous. The Runs drawer and fleet offer the same attachment action.
Receipts show beside the observed GitHub check in Chats and the Sprint board.
A commit outside the PR head''s ancestry is stale; unavailable ancestry stays
unknown. Pruned logs retain their receipt. Receipts supplement CI evidence;
only GitHub checks and the existing merge grant authorize a merge.' WHERE name='dev_kit' AND content='# dev_kit — read the fork development contract

The fork owns `.subfloor/dev-kit.json`. Subfloor validates the declaration,
selects the invoking checkout and host/container seat, runs exact hook argv,
and retains readiness evidence under `.sc-state/local/dev-kit/`. The engine
does not infer project policy from manifests or install privileged host tools.

## Hook inventory

The supported hook names are `deps`, `test`, `lint`, and `typecheck`:

```bash
sc deps [args...]
sc test [args...]
sc lint [args...]
sc typecheck [args...]
```

Read the declaration and its executable before running a hook. A configured
hook reports the selected checkout, cwd, seat, executable, and child status.
An absent hook is `unavailable`; do not reconstruct one from package metadata.

## Canonical states

| State | Meaning | Supported recovery |
|---|---|---|
| `absent` | No declaration exists; engine-baseline tools remain mechanisms, not project policy. | Add a tracked declaration only when the fork needs one. |
| `declared` | The declaration is valid; hook configuration is known, but execution/receipt evidence decides readiness. | Run the exact configured hook. |
| `invalid` | Declaration, path, mount, image, or invocation validation failed. | Correct the named tracked input and retry. |
| `ready` | The hook can execute on the active seat or the exact Docker receipt is current. | Continue. |
| `failed` | A declared hook or provisioning attempt ran and failed. | Inspect retained logs/evidence; retry the same supported surface. |
| `stale` | Docker provisioning or package evidence no longer matches the declaration, checkout, image, or labels. | From the host, run `sc launch`; use repair only after a failed attempt. |
| `advisory` | Engine baseline is runnable while a declared native-package candidate is degraded. | Inspect the named advisory evidence and submit a reviewed tracked remediation. |
| `repair` | A retained-container repair session is open without readiness. | Exit to the host, rerun `sc launch`, and require `ready`. |

Unavailable executable = exit 126; missing hook = exit 78; invalid
configuration = exit 64. A started child preserves its own status.

## Seats and evidence

Host hooks use the host checkout and toolchain. Container hooks use the
bind-mounted checkout, engine-baseline tools, declared sandbox extension, and
current provisioning receipt. `$SC_DEV_PORT` is loopback-bound on the host and
published from `0.0.0.0` in the container. A configured `$DATABASE_URL` reaches
the fork application sidecar; it never points at the engine memory DB.

`SC_SEAT=gui` wraps shell `test`, `lint`, and `typecheck` invocations in one
registered job. After the job id is confirmed, end the turn; its completion
wake tells you to inspect `sc job status <id>` and `sc job tail <id>` before
continuing. TUI and Admin hooks remain foreground without a completion wake
and return the child status; `deps` remains foreground in every seat. The wrapper marker is consumed by the
runner and removed from the hook''s environment, so nested hooks get their own
foreground run and return their real status without a completion wake. A
separate nesting marker prevents GUI wrapping inside a hook.

Every executed hook retains a log and adjacent `.receipt.json` under
`.sc-state/local/devkit-logs/<hook>/`. The receipt records declared argv with
appended arguments, checkout, host/container seat, starting commit and branch,
exit status, duration and log path. Test receipts parse pytest''s terminal
summary and failing ids; an absent summary is `null`, never zero failures.
Authenticated shell runs register before execution; wrapped hooks attach to
the existing run. Standalone CI and Admin retain local receipts without
claiming a shell run or completion wake. Receipts are local test evidence;
GitHub checks still own the merge gate.

Pruning retains the newest twenty completed log/receipt pairs per hook and
keeps ledger receipts with `evidence_pruned` set. If that acknowledgment is
unavailable, the files remain for a later pruning attempt. Submission outages
retain the run''s receipt and outcome for reconciliation, without rerunning the
hook. `SC_DEVKIT_OUTPUT=full` streams stdout and stderr while retaining the same
log and receipt. Provisioning/readiness evidence lives under
`.sc-state/local/dev-kit/`. Planner
uses this skill for pinch-hit development and capability design. It describes
the available surface and boundaries, not the fork''s test assertions,
deployment ritual, database technique, or VM lifecycle.';
COMMIT;
