# Sprint PR watcher

**Posture: current operator runbook.** Applies to every install whose review
server is up and whose host has an authenticated `gh`. Repository names, PR
numbers and shortnames below are illustrative. Exact CLI syntax remains in
`sc help --all` and verb help; the Sprint protocol itself belongs to the
`sprint_protocol` and role skills.


The engine's GitHub observation service. It reads pull requests the fork's
Developer shells own, turns each state change into a durable transition, and
delivers the fact to the owning Developer as a message and a wake — inside a
Sprint or outside one. **Shells never poll GitHub**; this is why.

It is read-only against GitHub: it never pushes, merges, closes, re-runs checks
or comments. The merge gate stays where the boot document puts it — an FnB
directive, or `sc sprint authorize-merge` inside an armed Sprint.

## What is watched

A subscription is `(repository, pr_number)` owned by exactly one **Developer**
shell — the store refuses any other flavor, and refuses an identity reused with
different ownership. Subscriptions arrive two ways:

- **explicitly** — `./sc pr subscribe --repository owner/name --pr 123`
  (`POST /_sc/pr/subscribe`), which registers and takes an immediate first
  observation;
- **by discovery** — the watcher lists this repo's managed worktrees
  (`.sc-worktrees/<shortname>`, attached and non-detached only) and subscribes
  the Developer of any worktree whose checked-out branch heads an unsubscribed
  PR, through the same receipt path. A shell never has to remember to subscribe.

A Sprint's registered PRs are the same subscriptions with
`sprint_registered_pr_id` linked, which adds Sprint events, work-unit resolution
and the review loop on top.

## Cadence

| Interval | Value | Source |
|---|---|---|
| pulse | 5 s, and immediately on `notify_commit()` | `PULSE_SECONDS` |
| discovery | at most every 60 s, forced on startup, skipped entirely when no feature branch is uncovered | `DISCOVERY_SECONDS` |
| heartbeat history | one bounded row per 60 s of scanning | `HEARTBEAT_HISTORY_SECONDS` |
| GitHub call timeout | 20 s per `gh` read | `GITHUB_TIMEOUT_SECONDS` |
| failure backoff | `min(300, 5 × 2^failures)` seconds, at least 60 s when the error mentions a rate limit | `MAX_BACKOFF_SECONDS`, `RATE_BACKOFF_SECONDS` |

Each pulse polls only subscriptions whose latest transition is not `merged` or
`closed`, and groups them by repository: two or more in one repo take a single
`gh pr list`, a lone one takes `gh pr view`, and a listed PR missing a base SHA
falls back to the exact read.

## CI rollup truth

`normalize_state()` projects one GitHub response onto the durable vocabulary, in
this order: **merged** (state `MERGED`, or a `mergedAt`, or a merge SHA) ·
**closed** · **red** (any failed check) · **green** (rollup `SUCCESS`) ·
**pending** · **created** (no checks reported at all).

What counts as failed, pending or successful is decided in
`scripts/github_pull_requests.py`, and it is deliberately conservative:

- failure states include `CANCELLED`, `STALE`, `TIMED_OUT`, `ACTION_REQUIRED`
  and `STARTUP_FAILURE`, not only `FAILURE`;
- a cancelled run is **ignored** when another run of the same check name exists —
  concurrency control leaves the superseded run in the rollup beside its
  replacement, and GitHub's own gate reads the replacement (#1376);
- a bare `COMPLETED` status, or any state outside the known-good set, reads as
  `PENDING`, never as success;
- an **open** PR that looks green is cross-checked against
  `gh run list --commit <head>`: a failed workflow flips it to red, and a pending
  one — or a full 100-item page, which cannot prove every current-head run was
  seen — flips it to pending.

So "green" means *proven green at this head SHA*; an unproven rollup is pending.

## What becomes a wake, and to whom

An observation is recorded only when `(normalized_state, head_sha)` differs from
the latest transition, so a steady PR is silent. A recorded transition gets a
chained `transition_key` (a hash over subscription, parent key, state and head),
a row in `pr_subscription_transitions`, a mirrored `sprint_pr_transitions` row
when the PR is registered, and a `pr.transition` Sprint event.

Four states message the **owning Developer shell** — and only that shell —
as a `notification` with `declared_type: "re-enter"` and an idempotency key of
`pr-transition:<transition_key>:shell:<id>`, so a redelivery is a no-op:

| State | Body carries | Instruction varies by |
|---|---|---|
| `red` | `head_sha`, `event` | armed · paused ("fix now, do not wait for resume") · outside a Sprint |
| `green` | `head_sha`, `event` | armed ("judge readiness, pass the baton") · paused ("wait for resume") · outside ("merge only under a standing FnB directive naming it") |
| `closed` | `head_sha`, `event` | armed/paused ("tell the Planner if this blocks the Sprint") · outside |
| `merged` | `head_sha`, `event`, `merge_sha` | armed/paused ("follow sprint_dev post-merge cleanup; do not wait for another PR fact") · outside (git-skill after-merge cleanup) |

`pending` and `created` never message; a registration observing `created` with
no checks at all also writes a `pr.no_checks_observed` Sprint event.

Two couplings reach the Sprint machinery in the same transaction: a `closed`
transition on a singly-linked work unit resolves that lane's live review
expectations (`registered_pr.closed_without_merge`), and a `merged` transition
on a lane not in `merge_ready` resolves them as
`registered_pr.merged_grant_bypassed` — a merge that bypassed the grant is
recorded, not hidden. `merged` additionally runs the review loop's merge
observation, whose pause receipts are signalled after the commit.

## Liveness expectations (`scripts/sprint_liveness.py`)

An expectation row is created by a database trigger the moment an actionable
Sprint message is read and accepted (migration 0149), with its first evaluation
due five minutes later. The module around those rows has two halves, and only
one of them is currently wired.

**Resolution runs.** The watcher holds a `SprintLivenessMonitor` and resolves a
lane's live review expectations on the `closed` and grant-bypassed `merged`
transitions above; the review loop and the Sprint domain use the same entry
points. That is what keeps a finished lane from carrying a stale expectation.

**Evaluation does not.** `SprintLivenessMonitor.evaluate()` — the entry point
that collects evidence, nudges, escalates and sends the CI-stalled backstop —
has no caller anywhere in the engine source; only the test suite drives it.
Treat the policy below as the module's design, not as behavior to expect during
a Sprint: nothing nudges a silent worker automatically. Raise it with the FnB if
you need it. When driven it is **armed-only**, with these windows:

| Window | Value |
|---|---|
| evaluation interval | 5 minutes |
| grace before one nudge | 10 minutes of ambiguous silence |
| further wait before one Planner escalation | 10 minutes |
| CI-stalled backstop | 90 minutes in `created`/`pending` |

Evidence comes from native conversation events (`session.started`,
`run.started`, `assistant.delta`, `tool.*`, `usage`, `run.completed`,
`run.interrupted`), outbound handoffs, terminal failures and a launch-process
probe, with provider quota state as a sanctioned-quiet suppressor. Delivery of
every nudge and escalation is the wake outbox's job; the module only commits
the facts.

## Failure handling

A `gh` read that fails raises a sanitized `GitHubReadError` (first stderr line,
240 characters). The watcher then:

1. writes or coalesces a row in `pr_subscription_poll_failures` — a repeat of
   the same trigger and detail within one streak bumps `repeat_count`;
2. computes the backoff above and stores it, never shrinking a streak's delay;
3. writes one `pr.poll_failed` Sprint event per distinct failure, when the
   subscription belongs to a Sprint;
4. holds that subscription out of the scan until `retry_at`.

A successful observation clears the in-memory backoff. A response whose PR
number does not match the subscription is a read failure, never an observation.
Failures are per-subscription — one unreachable repository does not stall the
others — and the pulse loop prints any unexpected error rather than dying.

## Running it — and telling whether it is

Nothing to start: the single process-wide `sprint-pr-watcher` thread is
installed when the review server binds, and a commit that creates a
subscription nudges it through `notify_commit()`.

```
./sc sprint watcher-state --sprint <id>    bounded durable evidence (JSON)
./sc pr subscribe --repository owner/name --pr <n>
./sc logs                                  server.log, UTC-stamped
```

`watcher-state` is the read to trust. Its `watcher.status` is derived from the
`sprint-pr-watcher` row in `daemon_heartbeats`:

- **`live`** — the last beat is within `3 × (interval + 20 s)`;
- **`stale`** — older: the thread is wedged, the server restarted without it, or
  every pulse is timing out on `gh`;
- **`never-started`** — no heartbeat row at all.

The same payload carries each registered PR's latest transition with its age,
the recent heartbeat history with `subscriptions_scanned`, and up to 20 poll
failures with their backoff and error detail — where an expired `gh` token or a
rate limit shows up in words. In `server.log`, look for
`sprint-pr-watcher: pulse failed (…)`, `: service failed (…)` and
`: PR discovery failed (…)`.

**A PR event that never arrived**: confirm the subscription exists and is not
already `merged`/`closed`; read `watcher-state` for a `stale` status or a
poll-failure streak on it; confirm the fact is one of the four that message (a
`pending` rollup is not); confirm the owner is the Developer you expect, since
the message goes to the subscription owner and nobody else.

## Limits

- **Developer-owned only**, and there is **no unsubscribe verb**: `./sc pr`
  offers `subscribe` alone, and a subscription leaves the scan by reaching
  `merged` or `closed`.
- **Discovery needs a managed worktree.** A branch checked out anywhere but
  `.sc-worktrees/<shortname>`, or a detached worktree, is invisible to it.
- **`gh` is the whole transport.** No webhooks, no GitHub App: losing the host's
  `gh` authentication turns every subscription into a poll-failure streak rather
  than an error anyone is told about directly.
- **`./sc sprint monitor` does not evaluate liveness.** It reconciles unread
  wake pickup once and returns the pickup, runtime and health projections with
  an empty `outcomes` list; its `--help` line still describes the retired
  evaluation behavior. Nothing else evaluates liveness either — see the
  liveness section above.
