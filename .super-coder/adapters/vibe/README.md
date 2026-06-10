# adapters/vibe — Mistral Vibe CLI

Vibe reads the boot artifact (`AGENTS.md`) and project `AGENTS.md` conventions
natively — already emitted by the render chain — so the adapter only carries the
launch command plus a trust flag and a sandbox approval flag.

**Why it exists:** the Mistral-models sibling of the claude/codex harnesses —
a first-party, open-source (Apache-2.0) CLI billed against a Mistral plan
(Free / Pro $14.99 / Team) or a self-managed API key. Devstral / Mistral-Medium
coding models. opencode stays the universal metered catch-all; vibe is the
native Mistral path.

## Branch guard — no in-line block (by harness limitation)

Vibe has **no pre-tool hook** and no programmatic, branch-aware tool gate — its
permissions are static (per-tool always/ask/deny, agent profiles), so unlike
claude/codex/opencode it cannot block an edit at the moment it happens based on
git state. Vibe's branch enforcement is therefore two-layered and honest about it:
the always-loaded **VERSION CONTROL** rule in the boot artifact (advisory), and
the universal **git pre-commit backstop** (`.super-coder/hooks/pre-commit`, wired
via `core.hooksPath`) that refuses the *commit* on a protected default branch — a
commit-time gate rather than an edit-time one, but a real gate. If Mistral adds a
pre-tool hook later, wire it here to match the others.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness (`vibe --trust`) |
| `boot_artifact` | the context file this harness reads (`AGENTS.md`, informational) |
| `emit` | files copied to the repo root at launch (none — vibe reads `~/.vibe` + `AGENTS.md`) |
| `env` | extra env merged into the launch environment (none) |
| `sandbox.launch_flags` | flags appended ONLY inside the docker sandbox (`SC_SANDBOX`) |

**`--trust` (base launch):** the launcher writes the `AGENTS.md` it wants Vibe to
load, then asks Vibe to trust the dir it just authored — so the trust prompt
("trust this folder?") never blocks a launch. `--trust` is per-invocation only
(not persisted to `~/.vibe/trusted_folders.toml`), so it leaves no global state
and re-trusts cleanly each launch. This is the trust gate, distinct from tool
*approval* below.

**`--agent auto-approve` (sandbox only):** appended only in the container
(`SC_SANDBOX`), where the container itself is the safety boundary — tools run
without per-action approval (matches how claude/opencode/codex get allow-all
permissions in-sandbox). On the no-docker host path (`./sc boot`), Vibe keeps its
normal `default_agent` approval behavior.

**No `model` field:** Vibe takes no model from the launch seam. It selects a model
via `active_model` in `~/.vibe/config.toml` (set at `vibe --setup`), overridable
by the `VIBE_ACTIVE_MODEL` env var — neither of which is the `--flag`/JSON-file
mechanism run.py's model seam drives. Vibe's model ids are Mistral-specific and
don't map to the claude/codex/opencode flavor models, so routing a flavor model
here would be meaningless; Vibe uses its configured default.

**Host setup (one-time):** install + authenticate once on the host —
`curl -LsSf https://mistral.ai/vibe/install.sh | bash` (installs via `uv tool
install mistral-vibe`; the `vibe` binary lands in `~/.local/bin`), then
`vibe --setup` to store the API key, or export `MISTRAL_API_KEY` in your shell.
`./sc install` / `./sc update` / `./sc ensure-harness` install the binary
automatically; auth stays manual.

**Sandbox credentials:** `./sc launch` bind-mounts the host `~/.vibe` into the
container (alongside the claude/codex/opencode cred dirs), so `vibe --setup`'s
stored key + `.env` flow straight in — no in-sandbox login. The env-var path is
also forwarded: if `MISTRAL_API_KEY` is set on the host at launch, it's passed
through (only when non-empty, so it can't shadow the mounted `~/.vibe` creds).
Authenticate on the host, then re-run `./sc launch` so the mount picks it up.
