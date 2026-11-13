# adapters/opencode — OpenCode

**Posture: current adapter reference.** The adjacent [manifest](adapter.json)
is authoritative for supported surfaces, compatibility bounds and launch flags.
Historical live-probe versions below are evidence, not the current support pin.


OpenCode reads `AGENTS.md` at the repo root and discovers Agent Skills from its
native `.opencode/skills/<name>/SKILL.md` tree. The render chain emits each
shell's exact grants there for OpenCode while retaining the
`.claude/skills/<name>/SKILL.md` mirror used by Claude-compatible harnesses.
The adapter adds the harness-specific config file and launch command.

- **`opencode.json`** (emitted to the repo root at launch, gitignored like the
  boot artifact) — points `instructions` at `AGENTS.md`, sets default tool
  permissions (edit allow / webfetch allow / bash ask), and leaves an `mcp` slot.
  Source maintainers edit this tracked template through a PR; installed forks
  receive it through update. The live file is regenerated each launch. The
  template leaves `model` unset, but the launch is not: the manifest declares
  `"model": { "file": "opencode.json", "key": "model" }`, so when the booting
  shell's flavor names an opencode model, `run.py` merges it into the emitted
  file before exec. With no flavor model the key stays absent and the harness is
  rented — pick it in OpenCode (`-m provider/model`) or add `"model"` here.
- **`"lsp": true`** — enabled by default. OpenCode's own default is LSP *off*; we
  turn it on so the model gets language-server diagnostics as a feedback loop
  (it sees the type error / unresolved import it just introduced and fixes it
  before handing back). Servers start lazily, per detected file extension, so
  languages absent from a fork cost nothing. Built-in servers cover the common
  languages (Pyright, tsserver, gopls, rust-analyzer, …); switch `true` → `{}`
  to keep built-ins while adding custom servers. **Offline/airgapped forks:** set
  `OPENCODE_DISABLE_LSP_DOWNLOAD=true` in the environment — OpenCode auto-fetches
  server binaries on first use, and this is the only knob for it (env-only, no
  JSON key), so we leave it to the env rather than forcing it off here.
- **`skill_dirs`** — renders exact shell grants to both `.claude/skills` and
  OpenCode's native `.opencode/skills`. Native delivery avoids depending on
  version-sensitive project-local Claude compatibility.
- **`env.OPENCODE_DISABLE_CLAUDE_CODE=1`** — disables Claude prompt and skill
  compatibility so OpenCode sees only its native, exact-grant skill tree rather
  than ambient global `~/.claude/skills`.
- **`tool-discipline.md`** + its entry in `instructions` — a static, harness-level
  steer (not part of the render-chain-generated `AGENTS.md`, so it survives
  regeneration). It tells the model never to emit tool calls as text/XML and never
  to call the synthetic `invalid`/`unknown` tools. This breaks a **compounding
  failure loop** seen with text-tool-call models (Qwen/Hermes): one malformed call
  makes OpenCode inject its own `invalid`-tool error into context, which the model
  then parrots back as more malformed calls. Referenced in place under
  `.super-coder/adapters/opencode/` (path is relative to the repo root where
  OpenCode runs); not emitted, just read.
- **`provider.openrouter.options.extraBody.parallel_tool_calls: false`** — caps the
  model to one tool call per turn over OpenRouter. Fewer batched calls means fewer
  chances for a single malformed entry to poison the turn; a known stabilizer for
  Qwen tool-calling. `extraBody` is merged straight into the request body by
  OpenCode's bundled AI SDK (verified in the binary), so it reliably overrides the
  default. Provider-scoped (all OpenRouter models); harmless for non-Qwen models.
  `reasoning` is intentionally left untouched — the next dial if noise persists.
- **`protect-default-branch.js`** + its entry in `opencode.json` `plugin` — a
  `tool.execute.before` hook that blocks `write`/`edit`/`patch` while a protected
  default branch is in play (forcing a feature branch before work lands). It
  extracts the edited path from the tool args (`output.args.filePath`) and passes
  it to the guard, so it blocks an edit aimed at the **stale main root** (or any
  protected-branch checkout), not just one whose cwd is on a protected branch —
  the same target-file check claude gets. `run.py` rewrites the `plugin` entry to
  an **absolute** engine path at emit: the template's repo-relative
  `./.super-coder/...` does not exist in a fork's shell worktree (the engine is
  gitignored), so opencode would silently load no plugin and the guard would
  never run. It shells out to the shared `.super-coder/scripts/branch-guard.sh`
  (one branch-decision source across all harnesses; honors
  `SC_PROTECTED_BRANCHES`). Throwing in the hook aborts that one tool call and
  surfaces the reason to the model. The git pre-commit backstop
  (`.super-coder/hooks/pre-commit`) catches shell-driven writes that route around
  the tool path.
- **`enforce-model-route.js`** + its entry in `opencode.json` `plugin` — a
  `chat.params` hook that refuses a mismatched model route *before* provider
  dispatch. `run.py` sets `SC_OPENCODE_ENFORCED_MODEL` (a `{requested, selector}`
  JSON contract) only for an interactive **host Admin** launch that asked for an
  explicit OpenCode model; the hook compares the resolved runtime
  `providerID/modelID` against that selector and throws when they differ, so the
  harness never responds or requests a tool on the wrong route. Absent the env
  var the hook is a no-op, so an ordinary shell launch is unaffected. The same
  `plugin`-path rewrite to an absolute engine path applies. The selector is also
  preflighted (`opencode models <provider>`) before any durable launch state is
  created.
- **`mcp.streamable_http`** — managed MCP servers arrive as an `opencode.json`
  `mcp` merge (type `remote`), not launch args: `windows-mcp` only with a linked
  VM, `browser` only with the browser port configured and never inside the
  sandbox.
- **`sandbox.merge_json`** — inside the docker sandbox only (`SC_SANDBOX`),
  `permission.bash` is merged to `allow`, where the container is the safety
  boundary. On the no-docker host path the template's `bash: ask` stands.

**Host setup (one-time):** the binary is baked into the sandbox image, but auth
is mounted from the host — so `opencode` must be installed + logged in on the
host once: `curl -fsSL https://opencode.ai/install | bash` (binary →
`~/.opencode/bin/opencode`), then `opencode auth login`. That writes
`~/.local/share/opencode/auth.json`, which `./sc launch` mounts in.
`./sc install` / `./sc update` / `./sc ensure-harness` install the binary
automatically; auth stays manual.

## Conversation capability

Feature #24 selected `opencode serve` as the conversation driver. The broker
creates a session through the local authenticated HTTP API, stores the returned
opaque `id`, submits later turns to that exact session, consumes `/event` SSE,
interrupts through the session abort resource, and inspects the session
resource during recovery. The server stays bound to `127.0.0.1`; credentials
remain engine-side.

The contract was live-probed on 1.18.9. Two sessions in `/tmp` retained
different nonce values, and resuming the first by `sessionID` after the second
ran returned only the first nonce. An asynchronous prompt emitted
`session.status=busy`; `POST /session/:id/abort` returned `true`, emitted a
`MessageAbortedError`, and returned the session to `idle`.

`maximum_cli_version_exclusive` was raised from `1.19.0` to `1.20.0` on
2026-11-12 to cover the installed `1.19.5`, by operator ruling (flag SC-1610,
Feature #95 spec #272): build against the installed version and assume its
behaviour holds. `verified_cli_version` stays `1.18.9` because the probe above
was not repeated; an installed 1.19.5 reports `supported`. CI still installs
`opencode-ai@1.18.9`.

### Compaction and the context meter (Feature #95, spec #272)

- `session.compacted` is mapped to one `context.compacted`. OpenCode names the
  session and nothing else, so the trigger is `unknown` and the size after
  stays empty until the next request reports it. The size before is the
  chat's last request before the summarising one: the latest this stream saw,
  or the chat's last recorded reading when it compacts before any request of
  the turn. The summarising request itself (an assistant message with
  `summary: true`) runs under the compaction agent's own prompt and is never
  read as the chat's context, for the marker or for the meter: on 1.19.5 it
  reported 449 input tokens for a session whose last request was about
  13,600.
- **No summary text is stored.** The compaction agent's reply is an assistant
  message carrying `summary: true`, `mode: "compaction"` and
  `agent: "compaction"`; in the event stream recorded on 1.19.5 its
  `message.updated` arrives before any of its text parts. The adapter passes
  on none of that message's text, on an automatic compaction as on a manual
  one: the chat shows the compaction marker instead. Either `summary: true`
  or `mode: "compaction"` marks it. An operator message's `summary` is an
  object and marks nothing, and ordinary replies before and after the
  compaction stream as usual. A manual summarise on 1.19.5 emitted it (below);
  **no automatic one has been seen**, and whether OpenCode auto-compacts under
  the engine is the open verification (spec #272 criterion 12).
- **Manual compaction.** `POST /session/{id}/summarize` with
  `{"providerID": …, "modelID": …}`; both are required (the server answers
  `400` without them, and `404` for an unknown session). Run once on 1.19.5,
  2026-11-12, against a private scratch `opencode serve` with a free model
  (two model requests): the session went `busy`, an assistant message with
  `summary: true` and `mode: compaction` streamed, then `session.compacted`,
  `session.status` idle and `session.idle`; the request answered `true` as
  the compaction finished. The adapter names the chat's own provider and
  model, records the compaction as `manual`, and passes on neither the
  summary text nor the summarising request's usage. The size before follows
  the rule above. A chat with no named
  model (a harness-default route) uses the model its session has been using,
  read from `GET /session/{id}/message`: the newest reply's `providerID` and
  `modelID` (the compaction agent's own `summary` reply is skipped), or
  failing that the newest operator message's `model`. That stored shape was
  read from a scratch session on 1.19.5; the lookup itself is covered by test
  only. If neither names a model the chat gets the one-line error, which says
  that the chat names no model and its session records none. Both waits are
  bounded. If the request has not answered within 600 s it is abandoned and
  aborted, and the chat shows the error line. If it answers and the session
  does not report idle within 15 s, the run ends there; with no
  `session.compacted` the chat says the harness did not report a compaction.
  The summarising request's tokens are passed on as the run's usage, with no
  context size. **Not checked:** the engine-owned
  shared server (the check used a private one), and a large session.
- The meter reads the assistant message's `tokens`
  (`input` + `cache.read` + `cache.write`). OpenCode states no window there.
  The meter's percentage is of the OpenCode trigger in effect, so a chat
  shows the size alone while OpenCode's auto compaction is off (the default)
  and a percentage once it is turned on.
- **Engine compaction (spec #275).** OpenCode takes no token-count trigger at
  launch, so nothing is passed to it. Its settings are on the OpenCode card in
  **Shells → Harness**, one setting for every model provider behind OpenCode
  (an `anthropic/` or `openai/` model included), and both start off: Auto
  compaction off, Idle compaction Off. Turned on, the engine compacts an
  OpenCode chat after a turn that ends at or above the trigger, and while
  idle, through the manual trigger above. Its cache life is not measured, so
  the idle wait is capped at 55 minutes. No environment variable applies.

## Subagent keys (`headless.agent`, Feature #96, spec #273)

`scripts/job_agent.py` builds an `sc job` subagent command from the `headless`
block and these optional keys; no harness name appears in the job code. An
adapter without them is refused for a subagent by the name of the missing key
and is unaffected otherwise.

| Key | Value | Meaning |
|---|---|---|
| `brief.delivery` | `stdin` | the message positional is omitted; the brief is read from standard input |
| `result` | `--format json`, `stdout_jsonl`, `type: text`, field `part.text` | the last text event is the final message |
| `session` | `event`, field `sessionID` | every event carries the session id |
| `resume` | `-s` | `-s <id>` continues the session; the id is unchanged |

Checked on OpenCode 1.19.5, 2026-11-12, model
`ollama-cloud/deepseek-v4.1-flash`, two requests: one launch and one resume
that recalled the first reply. Observed without a request: SIGTERM while
waiting on standard input ends the process by the signal (status -15); `-s`
with an unknown id exits 1 with "Session not found".

### What a subagent on this harness is

`sc job agent --harness opencode --model <selector> [--effort <level>]` runs OpenCode
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
  it travels as `--variant <level>`; when omitted, no effort setting is sent and the CLI's own default
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
