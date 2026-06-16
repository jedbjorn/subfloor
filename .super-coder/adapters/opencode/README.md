# adapters/opencode — OpenCode

OpenCode consumes our Claude-format assets almost unchanged — it reads `AGENTS.md`
at the repo root and `.claude/skills/<name>/SKILL.md` natively (Agent Skills
format), both already emitted by the render chain. The adapter adds the one
harness-specific file OpenCode wants and the launch command.

- **`opencode.json`** (emitted to the repo root at launch, gitignored like the
  boot artifact) — points `instructions` at `AGENTS.md`, sets default tool
  permissions (edit allow / webfetch allow / bash ask), and leaves an `mcp` slot.
  Edit this **template** (tracked) to change a fork's OpenCode config; the live
  file is regenerated each launch. Model is intentionally unset — the harness is
  rented; pick it in OpenCode (`-m provider/model`) or add `"model"` here.
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
- **`env.OPENCODE_DISABLE_CLAUDE_CODE=1`** — best-effort: stop OpenCode from also
  loading `CLAUDE.md` (we dual-write both with identical content; this avoids a
  double-load). ⚠ The exact env name is a **research flag to verify on live
  OpenCode** — if wrong it's a harmless no-op (the content is identical anyway).
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

## Verify on live OpenCode (research flags from the spec)

- skills dir spelling consumed (`.claude/skills/` ✓ vs any `agent(s)/` variant)
- `OPENCODE_DISABLE_CLAUDE_CODE` env name (above)
- session-storage paths (doc 404'd at research time)

None block the contract; confirm during real-repo testing.
