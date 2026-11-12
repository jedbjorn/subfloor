# Runs and durable jobs

`sc job start --label gate -- <command and args>` registers an authenticated
owner and monotonic run id before launching the detached supervisor. The
command argv is passed without another shell evaluation. Labels are display
text and never form paths. `--timeout` accepts positive integer seconds.
After the supervisor confirms command startup, the caller can end its turn.
Use `sc job status <id>` for the authoritative outcome and delivery receipt,
`sc job tail <id>` for bounded log output, and `sc job kill <id>` to request
cancellation. `wait --for N` remains an optional bounded foreground wait.

The command survives its launching harness. A service stop, reboot, or container
loss can interrupt computation; no interrupted command is automatically rerun.
The engine retains identity, evidence and notification intent. On startup and
five-second runtime pulses it reattaches a surviving supervisor incarnation or
records `lost` when it is proven gone without terminal evidence. `lost` means
unknown, never success. Unverifiable process identities are not signalled.

The supervisor writes its local terminal record before submitting it through
the owner-authenticated terminal route. API outages retry with exponential
backoff capped at five minutes; local attempt time and error remain in the
run's evidence directory. The runtime can recover that record without the
launching session's token. General shell tokens are never persisted in job
metadata. Duplicate terminal submissions return the original receipt;
conflicting outcomes are refused.

Terminal state, compatibility result inbox row, and engine-wide re-enter wake
intent commit atomically. The wake names the outcome and status/tail commands;
it conveys no review, merge or Sprint authority. Each run has its own message;
transport may coalesce wakes. Status distinguishes `pending`, `enqueued`,
`consumed` (the exact wake appears in the native transcript), and `blocked`.
A broker run id alone is not consumption evidence. Settled receipts leave the
active reconciliation set, including non-job engine wakes. A completed turn
without consumption evidence is blocked for operator recovery. Invalid run or
receipt evidence blocks only that row with a bounded reason; other delivery
and runtime heartbeats continue. The supervisor bounds spawn-error text in its
terminal submission while retaining full local evidence.

A non-Sprint re-enter wake refused before dispatch with `SHELL_BUSY` retries
at 15, 60, 180 and 300 seconds. It retains the failed conversation attempts and
uses stable keys for subsequent attempts of the same completion intent.
Exhaustion or an invalid/deleted owner is visibly blocked, without an automatic
boot storm. Ordinary native failures are not replayed. TUI-origin jobs still
write the result/wake; delivery waits out the CLI lock, and the inbox remains
readable at the next boot. The engine never injects into the CLI session.

`run/jobs/<legacy-id>/meta.json` and logs remain readable by the legacy status
and tail commands. New job evidence lives in engine-allocated
`run/runs/<run-id>/`. An upgrade does not invent legacy ownership or issue old
completion wakes. During an API outage, status, tail, wait and list read local
ledger or legacy evidence and label it unauthoritative. Status and list expose
submission errors and attempt timestamps; a local terminal result does not
prove wake delivery. Ledger cancellation requires the authenticated API.

## API and integration boundary

Shell routes require a bearer token and derive ownership from it. Another
shell's run id cannot be read, signalled, or finalized. Loopback operator
routes expose the fleet and may request cancellation; they do not register or
submit execution outcomes. Cancellation verifies boot id, pid and start ticks.

| Route | Contract |
| --- | --- |
| `POST /_sc/runs` | `{registration_key,kind,label,argv,cwd,commit,foreground?,timeout_seconds?}`; returns registered run with engine-allocated evidence path; retries require identical input. An `agent` run adds an `agent` object and omits `cwd` (see Subagent ledger) |
| `GET /_sc/runs` | Owner's runs, newest first |
| `GET /_sc/runs/<id>` | Owner's authoritative state and wake receipt |
| `POST /_sc/runs/<id>/running` | Supervisor/child pid, start ticks, boot id, started timestamp |
| `POST /_sc/runs/<id>/terminal` | `{state,exit_code,finished_at,spawn_error?}`; one immutable terminal payload |
| `GET /_sc/runs/<id>/tail` | Bounded log text; absent evidence is identified |
| `POST /_sc/runs/<id>/receipt` | Owner-only immutable dev-kit receipt; duplicate identical submissions are idempotent |
| `POST /_sc/runs/<id>/prune-evidence` | Record pruned completed receipt evidence; preserve the row and parsed receipt |
| `POST /_sc/runs/<id>/facts` | Owner-only facts an agent run reports: `session_ref`, `provider_model`, `result_bytes`, `worktree_removed_at`; each is written once, an identical repeat is idempotent, a different value is refused |
| `GET /_sc/runs/<id>/result` | An agent run's final message, at most 256 KiB, with `truncated` and its recorded facts |
| `POST /_sc/runs/<id>/kill` | Owner cancellation request; supervisor retains authoritative exit |
| `GET /api/runs[/<id>[/tail\|/result]]` | Operator projection |
| `POST /api/runs/<id>/kill` | Operator cancellation, guarded browser origin |

`kind` registration accepts `job`, `devkit`, `probe`, and `agent`. Native process
observations are reserved for the process-tree lane. Attachment columns remain reserved for their downstream lane. The snapshot includes runs
and delivery evidence after their message/conversation parents.

## Subagent ledger

Feature #96 (spec #273) adds the ledger half of `sc job` subagents. The
`sc job agent` verb, its supervisor path, the wake body and the GUI rows are
not shipped yet; until they are, nothing registers an `agent` run.

Every run may record `timeout_seconds`, its time limit, fixed at registration;
`sc job start` sends its `--timeout`. Rows registered before this column
exist carry none.

An `agent` run is one headless harness run. Its registration carries
`commit` (the base commit, a full object id) and an `agent` object:
`harness`, `requested_model`, `requested_effort`, `effective_effort`, `branch`,
`adapter_version`, `brief_bytes`, `launched_from_run`. The engine assigns its
`cwd`, the worktree `.sc-worktrees/agent-<run_id>`. The facts live in
`run_agents`, keyed by `run_id`, and appear as `agent` on the run row in
`GET /_sc/runs`, `GET /_sc/runs/<id>` and the operator routes; a run of any
other kind has no `agent` key. The result route reads `result.md` beside the
run's log.

Registration holds the limits, inside its write transaction:

| Limit | Value | Refusal |
| --- | --- | --- |
| Live subagents for one shell | 3 | 429, naming the count and the runs holding the slots |
| Live subagents on the install | 6 | 429, naming each run and its shell |
| Run time | default 7,200 s; at most 28,800 s | 400 |
| Brief size | 64 KiB | 400 |
| Nesting | depth 1 | 403: a registration carrying `launched_from_run` (the `SC_JOB_AGENT_RUN` of a subagent's environment) is refused. It prevents delegated work whose owner can no longer be traced. A command job or probe from the same environment registers and belongs to the launching shell |
| Reviewer of an armed or paused Sprint | may not launch | 403. It prevents a review verdict that rests on a reader nobody assigned. Developers and the Planner of that Sprint register, as does a Reviewer outside one |
| Admin | may not launch | 403. Every non-Admin flavor may launch; Admin's command jobs are unaffected |
| A shell named `agent-<number>` exists | no shell may launch | 403, naming the shell. Its worktree is the path a run of that number would be given. Such a shell can only predate the reserved prefix; rename it. A name such as `agent-smith` names no run's worktree and refuses nothing |
| Harness, model, effort shape | harness is a shipped harness name; model and effort carry no leading dash, quote, backslash or control character | 400 |

A slot is held by a run in state `registered` or `running` that can still be
running. A finished, stopped or lost run frees its slot. So does an unfinished
run with no process: one whose reconciliation was blocked (`wake_state`
`blocked`, left for operator recovery) and one whose supervisor is proven gone.
`sc job kill` stops only a run whose process is running.

The run is registered before its worktree exists. If the worktree cannot be
created, for example because `.sc-worktrees/agent-<run_id>` was left behind by
an earlier run of that number, the run ends `failed` with exit code 127 and the
reason, and the launching shell is told as for any finished run.

A subagent runs in its own worktree so that it is not counted as its launching
shell's live session and does not read that shell's boot file. Shell shortnames
beginning `agent-` are refused at creation. Its environment is the launching
shell's, with `SC_SEAT` cleared and `SC_JOB_AGENT_RUN` set: it acts under the
launching shell's identity with exactly that shell's authority, and the engine
adds no refusal for what it writes. `SC_JOB_AGENT_RUN` marks that one process
tree only: a shell session launch and the API server both clear it at start.

## Dev-kit receipts

GUI-seat shell `sc test`, `sc lint` and `sc typecheck` calls return a registered
`devkit` job id. The hook reuses that run, attaches its receipt before terminal
completion and produces one owner wake. TUI, Admin and `deps` calls stay
foreground without a completion wake. A nested hook consumes no outer run
marker and receives its own foreground run; a separate nesting marker prevents
GUI wrapping and preserves the nested exit status. Foreground registration sets
`foreground: true` (devkit only). This immutable registration attribute makes
terminal submission and reconciliation retain `wake_state=none`; terminal
callers cannot suppress a wrapped job wake.

Logs and adjacent `.receipt.json` files live in the invoking checkout's
`.sc-state/local/devkit-logs/<hook>/`. Receipts record the declared hook argv,
appended arguments, checkout, host/container seat, starting Git commit and
branch, status, duration and log. Pytest summaries include counts and failing
node ids; missing terminal summaries remain null. Full-output mode also retains
the streams and receipt. A standalone CI/operator invocation without shell API
credentials produces local evidence without promising a ledger row or wake.

Authenticated hooks register before execution; refusal launches no command.
Receipts persist beside the engine-allocated run evidence before completion,
so reconciliation can recover them after an API outage. Owner-only receipt
submission is idempotent and refuses conflicting data. The newest twenty
log/receipt pairs per hook are retained; pruning records `evidence_pruned` on
the ledger first. If the ledger cannot acknowledge pruning, both files remain.
The row and structured receipt outlive the files. These receipts describe local
execution; GitHub checks remain the merge-gate evidence.
