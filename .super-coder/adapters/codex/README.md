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
| `skill_dirs` | exact shell grants rendered to `.claude/skills` and Codex-native `.agents/skills` |
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

### Supported range (re-probed 2026-09-21 on 0.155.1)

`conversation.verified_cli_version` is `0.155.1` and
`maximum_cli_version_exclusive` is `0.156.0`. The window is authored from
evidence, not from version arithmetic:

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

Release notes from `0.148.0` through `0.155.1` change no method the broker
calls; the app-server work in that span is daemon lifecycle, Guardian approval
review, plugin reconciliation and Windows sandbox provisioning. The one entry
worth naming is `0.149.0`'s "Reject obsolete app-server permission profile
fields" — the probe confirms the fields this adapter sends are not among them.

The launch surface was re-checked against `codex --help` / `codex exec --help`
on the same binary: `--dangerously-bypass-hook-trust`, `-s/--sandbox` (with
`danger-full-access` still a valid value) and `-c` exist on both; top-level
`-a/--ask-for-approval` still accepts `never`; `codex exec` still has no
approval flag at all. `--dangerously-bypass-approvals-and-sandbox` is still
refused alongside `--ask-for-approval`, verbatim on 0.155.1. `codex sandbox --
pwd` still fails with `bwrap: Failed to make / slave: Operation not permitted`
on this host, so `danger-full-access` remains the only runnable policy here.
