# adapters/claude — Claude Code

**Posture: current adapter reference.** The adjacent [manifest](adapter.json)
is authoritative for supported surfaces, compatibility bounds and launch flags.
Historical live-probe versions below are evidence, not the current support pin.


Claude Code reads the boot artifact (`CLAUDE.md`) and `.claude/skills/<name>/SKILL.md`
natively — both already emitted by the render chain — the adapter also declares model/effort routing, permission flags, the branch
guard and the conversation contract.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness |
| `surfaces` | which lanes this harness may serve (`terminal`, `one_shot`, `browser`, `sprint` — all true). An explicit `false` is refused before launch setup. |
| `boot_artifact` | the context file this harness reads (informational) |
| `emit` | files in this dir copied to the repo root at launch (none for Claude) |
| `env` | extra env merged into the launch environment |
| `mcp.streamable_http` | managed MCP recipes injected as `--mcp-config` launch args — `windows-mcp` (only with a linked VM) and `browser` (only with the browser port configured, and never inside the sandbox) |
| `model` | `{ "flag": "--model" }` — run.py appends `--model <id>` for the flavor's claude model (alias: `sonnet`/`haiku`/`opus`) |
| `name` | `{ "flag": "--name" }` — interactive launches append `--name <display_name>` so the session is labelled in the prompt box, resume picker and terminal title. Claude is the only adapter that declares it. |
| `headless.effort` | `{ "flag": "--effort" }` — applies an explicitly requested headless effort level |
| `merge_json` | always-on: project-scoped JSON deep-merged every launch (preserves fork keys). Installs the branch-guard `PreToolUse` hook and the `SessionEnd` telemetry hook (`scripts/telemetry-hook.sh`) into `.claude/settings.local.json`. |
| `launch_flags` / `headless_flags` | always-on argv appended to the interactive / headless launch — `--dangerously-skip-permissions` in both |
| `sandbox` | `env`: `IS_SANDBOX=1`, required because the rootless container runs claude as uid 0 and it refuses bypass as root without it |

## Permission stance

Every launched shell runs with `--dangerously-skip-permissions`, on the host as
well as in the sandbox — the same bypass browser chats already get from the
conversation adapter. This is a launch flag, not a settings merge, because
Claude Code 2.1.256 ignores a project-scoped `permissions.defaultMode` of
`bypassPermissions` (in `.claude/settings.json` or `.claude/settings.local.json`);
only user/managed settings or the CLI flag grant it, and the engine never edits
the operator's user settings. The branch-guard hook fires in bypass mode too,
so the default-branch protection is unchanged.

## Branch-guard hook

`merge_json` deep-merges a `PreToolUse` hook into **`.claude/settings.local.json`**
(the gitignored personal layer — never the fork's tracked `.claude/settings.json`,
so fork-owned config is untouched). The hook runs the shared branch-guard before
every `Edit`/`Write`/`NotebookEdit`/`MultiEdit`. It resolves the script
env-independently (walking to the main worktree root via `git rev-parse
--git-common-dir`, the same way `sc` does; `$SC_ENGINE_DIR` is an optional
fast-path override) — a fork gitignores `.super-coder/`, so a worktree-relative
path found nothing and the hook failed open in exactly the shell worktrees it
protects. It blocks
the edit (exit 2) when the **target file's** repo HEAD is a protected default
branch — so a worktree shell writing to the stale repo-root checkout is caught,
not just one whose own cwd is on `main`; an out-of-worktree edit to a feature
branch is allowed with a warning. With no stdin target it falls back to the cwd's
branch. Protected set defaults to `main master`; override per-fork with
`SC_PROTECTED_BRANCHES` (space-separated). Set `SC_SHARED_DIRS` (space-separated
absolute paths) to declare host-level shared directories that all shells may write
into without warnings — handoff folders, screenshot dirs, cross-shell scratch.
These are fully exempt from both the branch block and the out-of-worktree warning.
This is the enforcement behind the `git` skill and the boot-template VERSION
CONTROL rule — a skill loads too late to stop the first edit; the hook fires
before it. Re-emitted (idempotently) each launch, so it survives `./sc update`.

`scripts/branch-guard.sh` is the **one** branch-decision script used by the Claude and Codex hooks, the OpenCode plugin, and the universal
Git pre-commit backstop
— so `SC_PROTECTED_BRANCHES` and the message stay identical everywhere.

**Host setup (one-time):** the binary is baked into the sandbox image, but auth is
mounted from the host — so `claude` must be installed + logged in on the host once:
`curl -fsSL https://claude.ai/install.sh | bash` (binary → `~/.local/bin/claude`),
then run `claude` and `/login`. That writes `~/.claude.json`, which `./sc launch`
mounts in. `./sc install` / `./sc update` / `./sc ensure-harness` install the
binary automatically; auth stays manual.

## Conversation capability

Feature #24 selected process-per-turn print mode. A new conversation supplies a
broker-generated UUID through `--session-id`; every later process uses
`--resume <uuid>`. Structured streaming requires all of `--print --verbose
--output-format stream-json`; `--include-partial-messages` adds text deltas.
The `system.init.session_id` field is the captured native reference.

The contract was live-probed on 2.1.220. Two UUIDs in the same cwd retained
different nonce values and exact resume returned the requested conversation.
Sending `SIGINT` produced an `aborted_streaming` result with an explicit
interruption record; a fresh process resumed that UUID and retained the
interrupted prompt. Browser-mediated permission responses remain unproven, so
the manifest advertises that capability as false until the adapter has a typed
permission bridge.

**Current pin.** Minimum `2.1.304`, verified `2.1.322`, below `2.2.0`. The six
proofs (two-turn exact resume, same-worktree isolation, structured streaming,
interruption, resume after compute exit, session inspection) were re-run
through the adapter on `2.1.322 (Claude Code)`, 2026-11-12, with five small
requests, and all passed. Two shapes differ from the earlier builds and the
adapter reads both:

- **Interrupt result.** After `SIGINT` the result is `subtype:
  error_during_execution`, `is_error: true`, `terminal_reason:
  aborted_streaming`. An interruption is read from `subtype` or
  `terminal_reason`; an execution error naming an abort in neither is a
  failure.
- **Transcript tail.** The stored session ends with bookkeeping entries
  (`last-prompt`, `mode`, `cost-state`) after the reply. Inspection reads the
  last `user` or `assistant` message entry and ignores every other entry type,
  known or not.

The minimum is the first build known to accept `--autocompact` (see the
compatibility caution below). What happens below it is under "Below the
minimum".

## GUI background conversion

Non-Admin launches carry `SC_SEAT=gui|tui`. Only the GUI seat uses
`seat-convert-hook.sh`: verified background Bash calls become foreground
job-start bridges, background Agent calls become foreground, and native
scheduling tools are refused with the engine equivalent. TUI and Admin return
before reading hook input or evidence.

The bridge transports the original Bash program and cwd as encoded data,
then starts `sc job` through argv with `shell=False`. The original program is
one unchanged argument to Bash `-c`. Positive native millisecond timeouts round
up to seconds; invalid explicit timeouts refuse before starting a job. An
omitted timeout keeps the job default. The foreground bridge has its own
startup timeout and does not wait for the child job.

Conversion verification is keyed by observed version, binary identity and
runtime, beside the model evidence in `logs/seat_conversion`. A new install
gets one bounded native probe; repeated GUI launches read its result. Update,
harness update and `sc harness-status` also observe new identities.
`sc harness-status --reverify` explicitly retries. The tiers are `rewrite`,
`deny-only`, and `disarmed`; disarmed launches state the manual `sc job` rule.
The GUI status projection exposes the tier, version, timestamp and reason;
its operator-only reverify endpoint is
`POST /api/harnesses/claude/conversion/verify`.

## Compaction and the context meter (Feature #95, spec #272)

Checked on `2.1.322 (Claude Code)`, 2026-11-12, with three small requests.

- **Earlier compact window.** A browser chat is launched with
  `--autocompact <tokens>` (default 120,000; `SC_CHAT_COMPACT_WINDOW_CLAUDE`
  sets it, `off` restores Claude's default). The value is the window Claude
  compacts within, not the trigger: compaction fires somewhat below it. The
  flag is accepted on a new session and on `--resume … -p` (both exit 0 and
  complete the turn). **Not checked:** that a resumed run then compacts at
  that window; forcing it costs at least 100,000 tokens. The engine passes the
  value as set; Claude applies its own 100k minimum and clamps to the model's
  window. A chat whose recorded window is smaller than the setting is launched
  without the flag. Terminal seats are untouched.
- **The environment variable wins, so the launch clears it.** In the installed
  binary the window resolver returns `CLAUDE_CODE_AUTO_COMPACT_WINDOW` before
  it reads the configured window. Whenever the engine passes `--autocompact`
  it removes that variable from the browser chat's environment. With the
  setting `off` the variable is left alone. This is read from the binary, not
  observed in a compaction.
- **Signal.** Stream line `type: system`, `subtype: compact_boundary`, with
  `compact_metadata` (`trigger`, `pre_tokens`, optional `post_tokens`). The
  stored transcript carries the same boundary in camelCase
  (`compactMetadata`, `preTokens`, `postTokens`), which is what restart
  recovery reads. Both become one `context.compacted`; the boundary's `uuid`
  keeps a boundary seen on the pipe and again on replay to one marker.
- **Meter.** The context size is the input side of one model request
  (`input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens`
  from `stream_event` → `message_delta.usage`). The `result` usage is the sum
  of every request in the turn and is not shown as the context. A request made
  by a delegated agent (`parent_tool_use_id` set) is not the chat's context
  either. The window is `result.modelUsage[<model>].contextWindow` for the
  chat's own model, so it is known only after a chat's first completed turn.
- **Manual compaction.** A resumed headless run whose whole prompt is
  `/compact` (`claude -p /compact … --resume <session>`). Run once on
  2.1.322, 2026-11-12, on a two-message scratch session with Haiku (two small
  requests): the stream was `status: compacting`, `status` with
  `compact_result: success`, `system.init` (after the status lines),
  `compact_boundary` with `trigger: manual`, `pre_tokens` 27,888 and
  `post_tokens` 7,628, two replayed `user` lines (the summary and
  `<local-command-stdout>`), and a `result` with `subtype: success`,
  `num_turns: 0`, `stop_reason: null` and `local_command: "compact"`; exit 0.
  That `result` has the zero-turn shape of a resume preamble, so the adapter
  treats a `result` that names `local_command` as the terminal, never as a
  preamble. On a session with nothing to compact Claude makes no request and
  writes a locally authored assistant message (`model: "<synthetic>"`,
  "Error: No messages to compact") before the same successful `result`; the
  adapter ends that run `run.failed` with Claude's reason, as it does for a
  `status` line whose `compact_result` is `failed` (that shape is read from
  the binary's schema, not observed). A run with no boundary and no reason
  ends `run.completed`, which the chat shows as "not reported". `/compact`
  takes no instructions here. `--autocompact` is still passed on this run.
  **Not checked:** a manual compaction of a large session, and one
  interrupted part-way.

**Compatibility caution.** `--autocompact` is present in the 2.1.319, 2.1.321
and 2.1.322 binaries and named in the changelog at 2.1.304; nothing earlier is
established, so the manifest's minimum is 2.1.304.

**Below the minimum.** The minimum is reported, not enforced. Read from the
code, not run on an older build:

- Harness status shows `older-unverified · best-effort` with no error
  (`harness_versions.compatibility_status`).
- Route resolution still resolves the route and marks it `best-effort`; it
  refuses only on a probe error or a missing version, not on an old one
  (`route_bindings._require_runtime`, `_runtime_support_state`).
- The adapter's own `probe()` raises `HARNESS_VERSION_UNSUPPORTED`. Its only
  callers are the arm and reroute preflights in `sprint_domain.py`. With the
  route-binding schema they ignore that code. The legacy arm preflight, taken
  only when that schema is absent, turns any adapter error into a preflight
  failure, so there an older Claude cannot arm a run.

Nothing stops a browser-chat launch, and an older Claude fails every
browser-chat launch on the unknown `--autocompact` flag. The fix is to update
Claude Code to 2.1.304 or later. `SC_CHAT_COMPACT_WINDOW_CLAUDE=off` stops
the engine passing the flag and is a stopgap only.
