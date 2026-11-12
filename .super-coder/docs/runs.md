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
| `POST /_sc/runs/<id>/terminal` | `{state,exit_code,finished_at,spawn_error?}`; one immutable terminal payload. An `agent` run may add `agent: {head_commit?, uncommitted?}`, what the supervisor found in the worktree |
| `GET /_sc/runs/<id>/tail` | Bounded log text; absent evidence is identified |
| `POST /_sc/runs/<id>/receipt` | Owner-only immutable dev-kit receipt; duplicate identical submissions are idempotent |
| `POST /_sc/runs/<id>/prune-evidence` | Record pruned completed receipt evidence; preserve the row and parsed receipt |
| `POST /_sc/runs/<id>/facts` | Owner-only facts an agent run reports: `session_ref`, `provider_model`, `result_bytes`, `worktree_removed_at`; each is written once, an identical repeat is idempotent, a different value is refused |
| `GET /_sc/runs/<id>/result` | An agent run's final message, at most 256 KiB, with `truncated` and its recorded facts |
| `POST /_sc/runs/<id>/clean` | Owner-only, `{discard}`: records that an agent run's worktree is being removed by `sc job clean`; refused (400) while a run is live in that worktree |
| `POST /_sc/runs/<id>/kill` | Owner cancellation request; supervisor retains authoritative exit |
| `GET /api/runs[/<id>[/tail\|/result]]` | Operator projection |
| `POST /api/runs/<id>/kill` | Operator cancellation, guarded browser origin |

`kind` registration accepts `job`, `devkit`, `probe`, and `agent`. Native process
observations are reserved for the process-tree lane. Attachment columns remain reserved for their downstream lane. The snapshot includes runs
and delivery evidence after their message/conversation parents.

## Subagent ledger

Feature #96 (spec #273) adds `sc job` subagents. `sc job agent` registers an
`agent` run and detaches the job supervisor, which creates the worktree, runs
the harness, and ends the run (see Running a subagent). The GUI rows are not
shipped yet.

Every run may record `timeout_seconds`, its time limit, fixed at registration;
`sc job start` sends its `--timeout`. Rows registered before this column
exist carry none.

An `agent` run is one headless harness run. Its registration carries
`commit` (the base commit, a full object id) and an `agent` object:
`harness`, `requested_model`, `requested_effort`, `effective_effort`, `branch`,
`adapter_version`, `brief_bytes`, `launched_from_run`, `continues_run_id`. The
engine assigns its `cwd`, the worktree `.sc-worktrees/agent-<run_id>`; a
continue is given the worktree, base commit and branch of the run it
continues. The facts live in
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
running. A finished, stopped or lost run frees its slot. An unfinished run
holds its slot while its supervisor or its harness process is proven alive
(pid, start ticks and boot id all match). Otherwise it frees the slot when its
supervisor is proven gone, or when its reconciliation was blocked (`wake_state`
`blocked`, left for operator recovery) and its supervisor is not proven alive.
`sc job kill` stops only a run whose process is running.

The run is registered before its worktree exists. The supervisor checks in
first and then creates the worktree, so a slow creation is a running run and
never a registered row with no process. If the worktree cannot be created, for
example because `.sc-worktrees/agent-<run_id>` was left behind by an earlier
run of that number, the run ends `failed` with exit code 127 and the reason,
and the launching shell is told as for any finished run.

A subagent runs in its own worktree so that it is not counted as its launching
shell's live session and does not read that shell's boot file. Shell shortnames
beginning `agent-` are refused at creation. Its environment is the launching
shell's, with `SC_SEAT` cleared and `SC_JOB_AGENT_RUN` set: it acts under the
launching shell's identity with exactly that shell's authority, and the engine
adds no refusal for what it writes. `SC_JOB_AGENT_RUN` marks that one process
tree only: a shell session launch and the API server both clear it at start.

## Running a subagent

```bash
sc job agent --harness <name> --model <selector> [--effort <level>] \
  (--brief-file <path> | --brief "<text>") \
  [--base <ref>] [--branch <name>] [--label <text>] [--timeout <seconds>]
sc job result <id> [--json]
sc job continue <id> (--brief-file <path> | --brief "<text>") [--label <text>] [--timeout <seconds>]
sc job clean <id> [--discard]
sc job list [--kind agent|probe|job] [--all]
```

- **Launch.** Every refusal comes before registration; a refusal registers
  nothing, creates no worktree and starts no process. The brief is copied to
  `brief.md` in the run's evidence directory. The run's `meta.json` carries the
  recorded command and, beside it, the positions of the engine's `{brief}` and
  `{run_dir}` tokens; the supervisor fills those positions and no others.
- **Supervisor.** It attaches the brief (standard input, or the argument the
  adapter names) before the process starts, and runs the harness in the
  worktree as the leader of its own process group. Standard output is the
  harness's report and is the run's log; standard error is appended to the log
  when the run ends. Exit, the run-time limit, `sc job kill`, and a signal to
  the supervisor itself all end with a signal to the whole group, so no harness
  process outlives its run. A supervisor stopped by a signal records `failed`
  with that reason.
- **A supervisor that is killed** cannot do that. The runtime pulse then finds
  the run without a supervisor, kills the harness's process group if its leader
  is still the recorded incarnation, and records `lost`. A harness whose leader
  has already exited is not signalled.
- **Result and facts.** Before the terminal result is submitted the supervisor
  writes `result.md` (the final message, or a line saying there was none) and
  `facts.json` (`session_ref`, `result_bytes`, `worktree_removed_at`), and
  posts the facts. The terminal route reads `facts.json` if the post did not
  arrive, so the wake written in the terminal transaction states them either
  way. A `session_ref` that could not be an argument of a later resume (a
  leading dash, a quote, a control character) is refused when written.
- **Wake.** An agent's wake states the state and exit, harness, model and
  effort, time run against its limit, the worktree (or that it was removed
  unchanged), branch, HEAD, whether uncommitted changes exist, the result size,
  and the result, tail, continue and clean commands. It carries no part of the
  final message.
- **Worktree.** When a run ends with no uncommitted change and HEAD still at
  the base commit, the supervisor removes the worktree and records
  `worktree_removed_at`; a branch the run created is kept. Otherwise the
  worktree is kept until `sc job clean`, which refuses while a run is live in
  it, and refuses uncommitted changes or commits on no remote branch unless
  `--discard` is given. A discard is recorded (`worktree_discarded_at`).
- **Continue** registers a new run (`continues_run_id`) with its own slot and
  time limit, on the earlier run's harness and model, resuming its
  `session_ref` in its worktree. A removed worktree is recreated at the same
  path, on its branch when that exists, else detached at the base. It is
  refused while a run is live in that worktree, when no session reference was
  captured, for another harness or model, for an adapter with no resume form,
  and after a discard.
- **Usage.** `sc analytics sweep` gives a session whose working directory is
  `.sc-worktrees/agent-<run_id>` the launching shell's id and that run id
  (`session_token_usage.run_id`), and writes the totals to the run row's
  `usage_json`. A continued subagent works in the first run's worktree, so its
  usage is counted on that first run.

## Watchers

Every `probe` run is a watcher: `sc job start --until`, and two named forms on
the same supervisor. One shell may have 10 live at once; the eleventh
registration is refused (429) naming the runs that hold the slots.

- `sc job watch pr --pr <n> [--repository owner/name] [--every <s>] [--timeout <s>]`
  reads the PR every 60 seconds for at most 3,600 seconds, through the PR
  watcher's reader and state normalization, and ends when the state is green,
  red, merged or closed. Green means proven green at the head commit. A PR that
  cannot be read when the verb runs starts no watch. It creates no
  subscription and changes nothing in the PR watcher.
- `sc job watch timer --after <s>` ends `done` when the delay has passed.

Their ledger `argv` is `sc job watch <form> ...`, which is how the condition is
read back. The wake names the condition, the final state and, for a PR, the
head commit, and grants no merge authority.

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
