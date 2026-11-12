# adapters/codex — OpenAI Codex CLI

**Posture: current adapter reference.** The adjacent [manifest](adapter.json)
is authoritative for supported surfaces, compatibility bounds and launch flags.
Historical live-probe versions below are evidence, not the current support pin.


Codex reads the boot artifact (`AGENTS.md`) and project `AGENTS.md` conventions
natively. The render chain also emits each shell's exact grants to Codex's
native `.agents/skills/<name>/SKILL.md` tree while retaining the cross-harness
`.claude/skills/<name>/SKILL.md` mirror. The adapter carries those skill targets,
the launch command, and how Codex takes a model and sandbox flag.

This adapter connects Subfloor to the codex CLI. Account entitlement and
billing are provider-owned; they are not part of the adapter contract.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness (`codex --dangerously-bypass-hook-trust`) |
| `surfaces` | which lanes this harness may serve (`terminal`, `one_shot`, `browser`, `sprint` — all true) |
| `boot_artifact` | the context file this harness reads (`AGENTS.md`, informational) |
| `emit` | files copied to the repo root at launch (`.codex/hooks.json` — the branch-guard hook) |
| `skill_dirs` | exact shell grants rendered to `.claude/skills` and the generic native `.agents/skills` (which Kimi Code also reads) |
| `env` | extra env merged into the launch environment |
| `mcp.streamable_http` | managed MCP recipes injected as `-c mcp_servers.<name>.url=…` launch args — `windows-mcp` (only with a linked VM) and `browser` (only with the browser port configured, and never inside the sandbox) |
| `model` | `{ "flag": "--model" }` — run.py appends `--model <id>` for the flavor's codex model |
| `headless.effort` | maps requested effort to `-c model_reasoning_effort="<level>"` |
| `launch_flags` / `headless_flags` | always-on argv appended to the interactive / headless launch — `--sandbox danger-full-access` (plus `--ask-for-approval never` interactively; `codex exec` has no approval flag and never prompts) |

## Branch-guard hook

Codex reads project-local hooks from `<repo>/.codex/hooks.json`. The hook is a
`PreToolUse` matcher on `^apply_patch$` (codex's file-edit tool) that runs the
shared `.super-coder/scripts/branch-guard.sh` and denies the edit (exit 2) while a
protected default branch is in play. `.codex/hooks.json` is emitted (gitignored)
each launch. Codex delivers the apply_patch text in `tool_input.command`; the
guard extracts Add/Update/Delete targets from its patch headers before applying
the same target-file check used for other harnesses. Scratch-only patches under
`/tmp`, `/var/tmp`, `/dev/shm`, or `$TMPDIR` remain available even when the cwd
is a protected Admin checkout. A payload without a usable target falls back to
the cwd-branch check.

**Two things gate whether this hook actually enforces — both verified empirically:**

1. **Trust (LOADING).** Codex loads project-local hooks only when the project's
   `.codex/` layer is *trusted*, keyed per-directory. A shell runs in a worktree
   (`.sc-worktrees/<name>`), which is NOT the trusted main root — so without help
   the hook never loads. `run.py` (`trust_codex_worktree`) marks the worktree
   trusted in `$CODEX_HOME/config.toml` at boot. This is the one place the engine
   writes under the codex home — additive project-trust only, never auth/history.
   (`--dangerously-bypass-hook-trust` only skips the per-hook hash review, NOT this
   layer-load trust. And `codex exec` runs no hooks at all — interactive only.)

2. **No YOLO flag (ENFORCING).** `--dangerously-bypass-approvals-and-sandbox`
   **ignores the hook's exit-2 deny** — verified: the hook fires and returns 2,
   but the edit proceeds. The always-on set `--sandbox danger-full-access
   --ask-for-approval never` elevates the *sandbox policy* without taking the
   bypass branch, so the exit-2 deny IS honored and the edit-time guard blocks —
   on the host and in the container alike. The adapter carries no
   container-only bypass: codex rejects it alongside `--ask-for-approval`
   (`the argument '--ask-for-approval <APPROVAL_POLICY>' cannot be used with
   '--dangerously-bypass-approvals-and-sandbox'`, first seen on codex-cli
   0.154.0, still verbatim on 0.155.1), and the
   always-on set already grants everything it did. Never swap the set for the
   YOLO flag as a shortcut; it is the bypass branch that loses the guard, not
   the policy.

**Host setup (one-time):** the binary is baked into the sandbox image, but auth is
mounted from the host — so `codex` must be installed + logged in on the host once:
`curl -fsSL https://chatgpt.com/codex/install.sh | sh` then `codex` and sign in
with ChatGPT. That writes `~/.codex/auth.json`, which `./sc launch` mounts in.

## Permission stance

Every launched shell runs with `--sandbox danger-full-access` (interactive
launches add `--ask-for-approval never`), on the host as well as in the
sandbox — the same policy the browser-chat lane has always sent through the
app-server (`approvalPolicy=never`, `sandbox=danger-full-access`). It is an
always-on launch flag rather than an Admin-only grant because:

1. **Subfloor's host seat already grants host authority.** The rendered
   EXECUTION CONTEXT tells every host shell that "the host toolchain, network,
   creds, services, and files available to your user are in reach". Codex was
   the only harness whose terminal launch contradicted that — Claude shells
   have carried `--dangerously-skip-permissions` on the host since the
   permission stance was set.
2. **Codex's own Linux sandbox is not a portable safety boundary.** Since
   codex-cli started shelling out to a bundled **bubblewrap** for
   `read-only`/`workspace-write`, a host that denies unprivileged mount
   propagation changes fails every single command before it starts:

   ```
   $ codex exec -s workspace-write 'run pwd'
   bwrap: Failed to make / slave: Operation not permitted
   ```

   That is not a degraded seat, it is a dead one: `pwd` fails, so the shell can
   read no file, run no `sc` command, and load no skill — and the subagents it
   spawns inherit the same dead policy, which reads from inside the session as
   "agents are broken". Verified on CachyOS (kernel 7.2.2) with codex-cli
   0.153.4: `-s workspace-write` fails as above, `-s danger-full-access`
   succeeds. Still true on 0.155.1, where `codex sandbox -- pwd` (the
   model-free sandbox runner) reproduces the same `bwrap` refusal.

The safety boundary is unchanged and is the same one every other harness
relies on: the branch-guard `PreToolUse` hook at edit time, and the git
**pre-commit** hook, which refuses protected-branch commits regardless of
harness flags. Elevating the sandbox policy does not elevate a shell past
either guard.

## Conversation capability

Feature #24 selected `codex app-server`, not transcript mutation and not
`exec resume --last`. The broker records `thread/start`'s `thread.id`, later
opens it with `thread/resume`, starts one turn at a time, consumes JSONL-RPC
notifications, interrupts with `turn/interrupt`, and inspects persisted state
with `thread/read`.

The contract was live-probed on 0.145.0. Exact `codex exec resume <thread-id>`
kept two same-cwd conversations isolated. The app-server probe ran with
`approvalPolicy=never` and `sandbox=danger-full-access`, interrupted a live
harmless command, observed terminal status `interrupted`, stopped the server,
started a new server, resumed the exact thread, and recovered the original
nonce. Permission policy is the requirement; `--sandbox` is not part of the
shared contract. The app-server payload takes kebab-case `danger-full-access`.

### Supported range (re-probed 2026-09-21 on 0.155.1; ceiling raised 2026-11-12)

`conversation.verified_cli_version` is `0.155.1` and
`maximum_cli_version_exclusive` is `0.168.0`.

**The ceiling and the probe are no longer the same statement.** On 2026-11-12
the ceiling was raised from `0.156.0` to `0.168.0` to cover the installed
`codex-cli 0.167.1`, by operator ruling (flag SC-1610, Feature #95 spec #272):
build against the installed version and assume its behaviour holds; a later
break is handled when it shows. `verified_cli_version` did not move, because
the whole-contract probe below was not repeated on 0.167.1. So an installed
0.167.1 reports `supported`, not `verified` and not `newer-unverified`. What
was checked on 0.167.1 is narrower and is listed under "Compaction and the
context meter" below.

The original window was authored from evidence, not from version arithmetic:

- **`0.155.1` is what was probed.** It is the installed host binary and, at the
  time of the probe, the newest stable `rust-v0.155.1` release — everything
  above it on the release list is a `0.156.0-alpha.*` pre-release.
- **The probe replayed the whole contract live** against `codex app-server
  --stdio`: `thread/start` with `approvalPolicy=never` +
  `sandbox=danger-full-access`, a streamed turn (`item/agentMessage/delta`), a
  second thread in the same cwd that neither shared nor leaked the first
  thread's nonce, a `sleep 60` command execution actually started under the
  unrestricted policy and then ended by `turn/interrupt` with terminal status
  `interrupted`, a full server stop/restart followed by `thread/resume` on the
  exact thread id that recovered the original nonce, and `thread/read
  includeTurns` reporting the same id with both turns.
- **`codex app-server generate-json-schema` corroborates the shapes.**
  `thread/start`, `thread/resume`, `thread/read`, `turn/start` and
  `turn/interrupt` all exist with the params the broker sends, `SandboxMode`
  is still the kebab-case enum, and `TurnStatus` still carries
  `completed | interrupted | failed | inProgress`.
- **`0.156.0` is excluded because nothing was probed there.** Its only
  published builds are alphas; raise the ceiling when a stable `0.156.x` has
  been probed the same way, not before.

One receive-side branch is deliberately wider than the probe: the broker maps
both `thread/tokenUsage/updated` and `turn/tokenUsage/updated` to `usage`, and
only the former exists in 0.155.1's schema. The `turn/` spelling was kept for
older builds still inside the window, so that one branch is carried by
compatibility judgement rather than by this probe. Everything else the broker
sends or consumes was exercised live.

Release notes from `0.148.0` through `0.155.1` change no method the broker
calls; the app-server work in that span is daemon lifecycle, Guardian approval
review, plugin reconciliation and Windows sandbox provisioning. The one entry
worth naming is `0.149.0`'s "Reject obsolete app-server permission profile
fields" — the probe confirms the fields this adapter sends are not among them.

The launch surface was re-checked against `codex --help` / `codex exec --help`
on the same binary: `--dangerously-bypass-hook-trust`, `-s/--sandbox` (with
`danger-full-access` still a valid value) and `-c` exist on both; top-level
`-a/--ask-for-approval` still accepts `never`; `codex exec` still has no
`--ask-for-approval`, though it does carry `--approve-for-me`, which routes
approval requests through automatic review **using the workspace-write
sandbox** — deliberately unused here, because workspace-write is exactly the
bwrap-backed policy that cannot start on this host (see Permission stance).
`--dangerously-bypass-approvals-and-sandbox` is still
refused alongside `--ask-for-approval`, verbatim on 0.155.1. `codex sandbox --
pwd` still fails with `bwrap: Failed to make / slave: Operation not permitted`
on this host, so `danger-full-access` remains the only runnable policy here.

## Runs inventory (0.160.0)

The Runs lane inspected installed `codex exec` help, config/features and native
scratch transcripts on 2026-10-04. Unified execution exposes `exec_command`
and `write_stdin`; a command can yield a session handle while the model
continues. The exposed tool inventory also includes timer and multi-agent
operations. Availability alone establishes no lifetime guarantee.

A measured native exec probe started `sleep 120`, yielded handle `57449`, and
finished the CLI after 11.08 seconds. Its observed child process incarnation
was gone at CLI exit. This proves no durable detachment for that native exec
session. Multi-agent lifetime and explicit OS detachment were not asserted.
GUI long work therefore continues through `sc job`; no Codex conversion hook
is introduced by this lane.

## Compaction and the context meter (Feature #95, spec #272)

Checked on `codex-cli 0.167.1`, 2026-11-12, with one small model request.

- **Earlier compact window.** A browser chat is given
  `model_auto_compact_token_limit` (default 250,000; `SC_CHAT_COMPACT_WINDOW_CODEX`
  sets it, `off` restores Codex's default). It is stated twice, and both forms
  were accepted: as `-c model_auto_compact_token_limit=<n>` on the
  `codex … app-server --stdio` launch, where `config/read` then reports the
  value with origin `sessionFlags`; and in the `config` object of
  `thread/start` and `thread/resume`, where it is parsed as thread
  configuration (a non-integer value is refused with
  `failed to load configuration: invalid type … expected i64`). A resumed
  thread therefore gets it by either route. **Not checked:** that Codex then
  compacts at that figure, and the accepted range; that is the post-merge
  live check (spec #272 criterion 10). It is skipped for a chat whose recorded
  `modelContextWindow` is smaller than the setting. Terminal seats are untouched.
- **Signal.** The `contextCompaction` item is the one signal. It is recorded
  once as `context.compacted` and no longer surfaces as a tool event; the
  deprecated `thread/compacted` notification is ignored. The item carries no
  trigger and no counts: the trigger is `auto` unless the engine asked for the
  compaction (`CodexAdapter.compaction_requested`), and the counts are read
  from the `thread/tokenUsage/updated` values around the item. The app-server
  order, from 308 engine-captured compactions, is: the last request's usage
  (for example input 215,900, total 216,150), `item/started`, a usage update
  with `inputTokens: 0` whose `totalTokens` is the reduced context (for
  example 18,692; sometimes sent twice), then `item/completed`. The size
  before is the last reading at `item/started`; the size after is that
  zero-input update's total. A repeat of it adds no second marker. Item ids
  were distinct across all 308, so a replayed item is recorded once.
- **Meter.** `tokenUsage.last.inputTokens` (which already counts the cached
  part) is the context size of the latest request, and `modelContextWindow`
  is kept as the window, exactly as Codex reports it (258,400 for a 272,000
  model: Codex states 95%). The zero-input update after a compaction reads as
  its total, never as 0. The meter's percentage is of the compact window, not
  of `modelContextWindow` (decision #451); the model window decides only
  whether the compact window applies, and the tooltip states it.
- **Manual compaction.** `thread/compact/start` with `{"threadId": …}`, sent
  after `thread/resume` in a new app-server (the engine's process-per-message
  shape). Run once on 0.167.1, 2026-11-12, on a one-turn scratch thread with
  the default model (two model requests): the request answered `{}` at once;
  the app-server then replayed the last turn's `thread/tokenUsage/updated`
  under its old turn id, and ran the compaction as a turn of its own:
  `turn/started` with a new turn id, `item/started` (`contextCompaction`), a
  usage update with `inputTokens: 0` and the new total, `item/completed`,
  `turn/completed` (`status: completed`). So the thread must be resumed
  first, and the request does not return the turn id: the adapter adopts it
  from `turn/started` and applies its exact-turn checks from there. The
  request sets `CodexAdapter.compaction_requested`, which is what tags the
  compaction `manual`. Both waits are bounded: 30 s for the turn to start,
  300 s for it to finish; a request that is accepted and then shows no
  compaction in that time ends the run without one, and the chat says the
  harness did not report a compaction. A refused request, or a compaction
  turn that ends `failed`, is the chat's one-line error. The adopted turn id
  replaces the placeholder on the run row as soon as it is known, so Stop and
  recovery after a broker restart name the real turn. Stop before it is
  known cannot be sent to Codex (there is no turn to name): it ends the wait
  at once, the run is interrupted, the chat shows "Compaction stopped", and
  the app-server is closed with the run. Codex states no token usage for the
  compaction request (the updates around the item are the last turn's,
  replayed, and its estimate of the new context), so none is recorded for the
  run. **Not checked live:** the request on a thread with a turn in progress
  (the engine never sends it then), Stop during a compaction, and recovery of
  a compaction after a broker restart.

## Subagent keys (`headless.agent`, Feature #96, spec #273)

`scripts/job_agent.py` builds an `sc job` subagent command from the `headless`
block and these optional keys; no harness name appears in the job code. An
adapter without them is refused for a subagent by the name of the missing key
and is unaffected otherwise.

| Key | Value | Meaning |
|---|---|---|
| `brief.delivery` | `stdin`, argument `-` | the prompt positional is `-`, so the prompt is read from standard input |
| `result` | `--json`, `file`, `-o` | the final message is written to the `-o` file; events go to stdout as JSONL |
| `session` | `event`, `thread.started`, field `thread_id` | the session reference is read from the event stream |
| `resume` | subcommand `resume`, `-c sandbox_mode="danger-full-access"` | `codex exec resume <id> -`; that subcommand has no `--sandbox` or `-C` option, so the policy travels as config |

Checked on codex-cli 0.167.1, 2026-11-12, model `gpt-6-luna` at effort `low`,
two requests: one launch and one `codex exec resume <id> -` that recalled the
first reply. The `--json` stream is `thread.started` (with `thread_id`),
`turn.started`, `item.completed` (an `agent_message` item) and
`turn.completed`; the `-o` file holds the final message alone; the resumed run
reports the same `thread_id`, and the `-c sandbox_mode=…` form was accepted.
Both runs were made outside a repository and so also carried
`--skip-git-repo-check`, which a subagent's worktree does not need: on the same
date and version, `sc job agent` and `sc job continue` ran the built commands
(without that flag) in a subagent worktree of a scratch repository, two
requests on `gpt-6-luna` at `low`. The launch ended `done`; the worktree was
removed as unchanged and recreated at the same path; the resume ran from that
directory, reported the same `thread_id`, and recalled the first reply. Observed
without a request: SIGTERM while `codex exec -` waits on standard input ends
the process by the signal (status -15).

### What a subagent on this harness is

`sc job agent --harness codex --model <selector> [--effort <level>]` runs Codex
once, headless, in a worktree the engine creates
(`.sc-worktrees/agent-<run_id>`), and wakes the launching shell when the run
ends. `sc job continue <id>` resumes the same session in that worktree as a
new run. Any shell may launch it, whatever harness that shell itself runs on.

- **Identity.** The run has the launching shell's environment: its API token,
  its shell identity and its git identity. It holds exactly that shell's
  authority, no more and no less, and that shell is accountable for what it
  does. The engine adds no refusal for what it writes. The worktree carries no
  boot file, so the CLI starts plain and learns its task from the brief.
- **Model and effort.** The model is required. Effort is optional: when given,
  it travels as `-c model_reasoning_effort="<level>"`; when omitted, no effort setting is sent and the CLI's own default
  for that model applies.
- **Limits.** One shell may have 3 subagents live and the install 6. A run is
  stopped after 2 hours by default, or after `--timeout` seconds, at most 8
  hours. A subagent cannot launch or continue a subagent. A brief is at most
  64 KiB.
- **Watches.** `sc job start --until`, `sc job watch pr` and
  `sc job watch timer` are engine jobs, not harness runs, and behave the same
  on every harness. One shell may have 10 live.

The verbs, the worktree rule and the wake are in
[`docs/runs.md`](../../docs/runs.md).
