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
