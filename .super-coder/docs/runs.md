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
| `POST /_sc/runs` | `{registration_key,kind,label,argv,cwd,commit,foreground?}`; returns registered run with engine-allocated evidence path; retries require identical input |
| `GET /_sc/runs` | Owner's runs, newest first |
| `GET /_sc/runs/<id>` | Owner's authoritative state and wake receipt |
| `POST /_sc/runs/<id>/running` | Supervisor/child pid, start ticks, boot id, started timestamp |
| `POST /_sc/runs/<id>/terminal` | `{state,exit_code,finished_at,spawn_error?}`; one immutable terminal payload |
| `GET /_sc/runs/<id>/tail` | Bounded log text; absent evidence is identified |
| `POST /_sc/runs/<id>/receipt` | Owner-only immutable dev-kit receipt; duplicate identical submissions are idempotent |
| `POST /_sc/runs/<id>/prune-evidence` | Record pruned completed receipt evidence; preserve the row and parsed receipt |
| `POST /_sc/runs/<id>/kill` | Owner cancellation request; supervisor retains authoritative exit |
| `GET /api/runs[/<id>[/tail]]` | Operator projection |
| `POST /api/runs/<id>/kill` | Operator cancellation, guarded browser origin |

`kind` registration accepts `job`, `devkit`, and `probe`. Native process
observations are reserved for the process-tree lane. Attachment columns remain reserved for their downstream lane. The snapshot includes runs
and delivery evidence after their message/conversation parents.

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
