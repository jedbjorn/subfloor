# adapters/kimi — Kimi Code CLI

Kimi Code (`kimi`, Moonshot AI's coding CLI — repo `MoonshotAI/kimi-code`, the
TypeScript successor to the legacy Python `kimi-cli`) reads the boot artifact
(`AGENTS.md`) natively — already emitted by the render chain — so the adapter
carries only the launch command, a headless block, and a sandbox approval flag.

**Why it exists:** the Kimi-models sibling of the claude/codex/vibe harnesses —
a first-party CLI billed against a Kimi membership (Moderato / Allegretto /
Allegro / Vivace) or a Moonshot platform API key. K-series coding models
(k3, kimi-for-coding-highspeed). opencode stays the universal metered
catch-all; kimi is the native Moonshot path.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness (`kimi` — cwd is the workspace; no dir flag exists) |
| `boot_artifact` | the context file this harness reads (`AGENTS.md`, informational) |
| `emit` | files copied to the repo root at launch (none — kimi reads `~/.kimi-code` + `AGENTS.md`) |
| `headless.launch` | non-interactive base argv (`kimi`) |
| `headless.prompt_flag` | `-p`; run.py emits it immediately before the prompt value |
| `headless.model_flag` | `-m`; the selector must be an alias discovered from `~/.kimi-code/config.toml` |
| `headless.effort.env` | `KIMI_MODEL_THINKING_EFFORT`; sprint headless boots set it to `high` |
| `sandbox.launch_flags` | flags appended ONLY inside the docker sandbox, interactive launches only (`--yolo`) |

**`--yolo` (sandbox, interactive only):** auto-approves all actions inside the
container, where the container itself is the safety boundary (matches how
claude/opencode/codex/vibe get allow-all in-sandbox). It is a `launch_flags`
entry — NOT `headless_flags` — because `kimi -p` hard-errors on any
permission-mode flag (`Cannot combine --prompt with --yolo`, same for `--auto`):
prompt mode always runs in auto permission mode by design, so headless needs no
flag at all. This adapter is why the sandbox seam splits `launch_flags`
(interactive) from `headless_flags` (headless) instead of folding one into the
other. On the no-docker host path (`./sc boot`), kimi keeps its normal
per-action approval prompts.

**No interactive `model` field:** Kimi's `-m` selects a user-local alias from
`~/.kimi-code/config.toml`, not a portable provider id, so an interactive flavor
default remains unsafe. Headless sprint boots are different: Refresh models
discovers the exact local aliases (for example `kimi-code/k3`), the resolver
accepts only a locally available high-effort route, and the adapter emits
`kimi -m <alias> -p <prompt>`. A requested selector that cannot be applied fails
before the worker session opens.

**Skills:** kimi does not read `.claude/skills/` (its discovery dirs are
`.kimi-code/skills/` + `.agents/skills/`), so like codex/vibe it loads skills
through the canonical harness-agnostic path — the boot doc's `## SKILLS` block.

## Branch guard — no in-line block (v1)

Kimi Code has a hooks system, but wiring it is unverified here; for now kimi's
branch enforcement is the same two layers as vibe: the always-loaded
**VERSION CONTROL** rule in the boot artifact (advisory) plus the universal
**git pre-commit backstop** (`.super-coder/hooks/pre-commit` via
`core.hooksPath`) that refuses a commit on a protected default branch. If kimi's
hook config proves to support a pre-tool gate, wire it here to match the others.

**Host setup (one-time):** install + authenticate once on the host —
`curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash` (single binary →
`~/.kimi-code/bin/kimi`), then `kimi login` (device-code OAuth against a Kimi
membership) or write a provider key into `~/.kimi-code/config.toml`. Note
kimi does NOT read credentials from shell env vars (`export KIMI_API_KEY=…`
does nothing); keys live in config.toml — the exception is the `KIMI_MODEL_*`
env family, which synthesizes a temporary provider. `./sc install` /
`./sc update` / `./sc ensure-harness` install the binary automatically; auth
stays manual.

**Sandbox credentials + the split install dir:** `~/.kimi-code` is BOTH kimi's
binary home (`bin/`) and its config home — unlike the other harnesses, which
keep those apart. `./sc launch` bind-mounts host `~/.kimi-code` in (config,
sessions, login state flow straight to the container), so the sandbox image
deliberately bakes its own binary to `/usr/local/bin` (`KIMI_INSTALL_DIR=/usr/local`
in the Dockerfile) — otherwise the mount would shadow the baked binary with the
host's (fatal on a macOS host: a darwin binary inside the linux container).
Same reason `ensure_harness_path()` is a no-op under `SC_SANDBOX`.
