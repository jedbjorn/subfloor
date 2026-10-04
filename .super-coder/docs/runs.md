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
A broker run id alone is not consumption evidence.

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
completion wakes.

## API and integration boundary

Shell routes require a bearer token and derive ownership from it. Another
shell's run id cannot be read, signalled, or finalized. Loopback operator
routes expose the fleet and may request cancellation; they do not register or
submit execution outcomes. Cancellation verifies boot id, pid and start ticks.

| Route | Contract |
| --- | --- |
| `POST /_sc/runs` | `{registration_key,kind,label,argv,cwd,commit}`; returns registered run with engine-allocated evidence path; retries require identical input |
| `GET /_sc/runs` | Owner's runs, newest first |
| `GET /_sc/runs/<id>` | Owner's authoritative state and wake receipt |
| `POST /_sc/runs/<id>/running` | Supervisor/child pid, start ticks, boot id, started timestamp |
| `POST /_sc/runs/<id>/terminal` | `{state,exit_code,finished_at,spawn_error?}`; one immutable terminal payload |
| `GET /_sc/runs/<id>/tail` | Bounded log text; absent evidence is identified |
| `POST /_sc/runs/<id>/kill` | Owner cancellation request; supervisor retains authoritative exit |
| `GET /api/runs[/<id>[/tail]]` | Operator projection |
| `POST /api/runs/<id>/kill` | Operator cancellation, guarded browser origin |

`kind` registration accepts `job`, `devkit`, and `probe`. Native process
observations are reserved for the process-tree lane. Receipt and attachment
columns are reserved for their downstream lanes. The snapshot includes runs
and delivery evidence after their message/conversation parents.
