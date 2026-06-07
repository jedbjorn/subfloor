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
| `launch` | argv exec'd to start the harness (`codex`) |
| `boot_artifact` | the context file this harness reads (`AGENTS.md`, informational) |
| `emit` | files copied to the repo root at launch (none — codex reads `~/.codex` + `AGENTS.md`) |
| `env` | extra env merged into the launch environment |
| `model` | `{ "flag": "--model" }` — run.py appends `--model <id>` for the flavor's codex model |
| `sandbox.launch_flags` | flags appended ONLY inside the docker sandbox (`SC_SANDBOX`) |

`--dangerously-bypass-approvals-and-sandbox` is appended only in the container,
where the container itself is the safety boundary (matches how claude/opencode
get allow-all permissions in-sandbox). On the no-docker host path (`./sc boot`),
codex keeps its normal approval prompts.

**Host setup (one-time):** the binary is baked into the sandbox image, but auth is
mounted from the host — so `codex` must be installed + logged in on the host once:
`curl -fsSL https://chatgpt.com/codex/install.sh | sh` then `codex` and sign in
with ChatGPT. That writes `~/.codex/auth.json`, which `./sc launch` mounts in.
