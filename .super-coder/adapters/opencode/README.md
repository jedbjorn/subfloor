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

## Verify on live OpenCode (research flags from the spec)

- skills dir spelling consumed (`.claude/skills/` ✓ vs any `agent(s)/` variant)
- `OPENCODE_DISABLE_CLAUDE_CODE` env name (above)
- session-storage paths (doc 404'd at research time)

None block the contract; confirm during real-repo testing.
