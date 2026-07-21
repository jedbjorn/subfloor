# adapters/codex — OpenAI Codex CLI

Codex reads the boot artifact (`AGENTS.md`) and project `AGENTS.md` conventions
natively — already emitted by the render chain — so the adapter only carries the
launch command plus how it takes a model and a sandbox flag.

**Why it exists:** the *subscription* path for OpenAI models. Signing into Codex
with a ChatGPT plan bills against that plan (flat, capped) instead of per-token
OpenAI API metering — which is the only way to run OpenAI models without an API
bill. (opencode can run OpenAI too, but only via a metered API key.) This makes
codex the OpenAI sibling of the claude harness: first-party CLI, subscription
billing. opencode stays as the universal metered catch-all.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness (`codex --dangerously-bypass-hook-trust`) |
| `boot_artifact` | the context file this harness reads (`AGENTS.md`, informational) |
| `emit` | files copied to the repo root at launch (`.codex/hooks.json` — the branch-guard hook) |
| `env` | extra env merged into the launch environment |
| `model` | `{ "flag": "--model" }` — run.py appends `--model <id>` for the flavor's codex model |
| `headless.effort` | maps sprint `high` to `-c model_reasoning_effort="high"` |
| `sandbox.launch_flags` | flags appended ONLY inside the docker sandbox (`SC_SANDBOX`) |

## Branch-guard hook

Codex reads project-local hooks from `<repo>/.codex/hooks.json`. The hook is a
`PreToolUse` matcher on `^apply_patch$` (codex's file-edit tool) that runs the
shared `.super-coder/scripts/branch-guard.sh` and denies the edit (exit 2) while a
protected default branch is in play. `.codex/hooks.json` is emitted (gitignored)
each launch. Codex only delivers the apply_patch *patch text* (`tool_input.command`),
not a file path, so the guard uses its cwd-branch check (not the target-file check
claude/opencode get).

**Two things gate whether this hook actually enforces — both verified empirically:**

1. **Trust (LOADING).** Codex loads project-local hooks only when the project's
   `.codex/` layer is *trusted*, keyed per-directory. A shell runs in a worktree
   (`.sc-worktrees/<name>`), which is NOT the trusted main root — so without help
   the hook never loads. `run.py` (`trust_codex_worktree`) marks the worktree
   trusted in `$CODEX_HOME/config.toml` at boot. This is the one place the engine
   writes under the codex home — additive project-trust only, never auth/history.
   (`--dangerously-bypass-hook-trust` only skips the per-hook hash review, NOT this
   layer-load trust. And `codex exec` runs no hooks at all — interactive only.)

2. **YOLO flag (ENFORCING).** `--dangerously-bypass-approvals-and-sandbox` is
   appended only in the container (the container is the safety boundary). That flag
   **ignores the hook's exit-2 deny** — verified: the hook fires and returns 2, but
   the edit proceeds. So **in-sandbox, codex's edit-time branch-guard cannot block**;
   the git **pre-commit backstop** is the real guard there (it blocks
   protected-branch *commits* regardless of harness flags). On the no-docker host
   path (`./sc boot`), the flag is absent, approvals are normal, and the exit-2 deny
   IS honored — so the host edit-time guard blocks.

**Host setup (one-time):** the binary is baked into the sandbox image, but auth is
mounted from the host — so `codex` must be installed + logged in on the host once:
`curl -fsSL https://chatgpt.com/codex/install.sh | sh` then `codex` and sign in
with ChatGPT. That writes `~/.codex/auth.json`, which `./sc launch` mounts in.
