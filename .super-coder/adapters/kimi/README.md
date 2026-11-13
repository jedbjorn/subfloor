# adapters/kimi — Kimi Code CLI

**Posture: current adapter reference.** The adjacent [manifest](adapter.json)
is authoritative for supported surfaces, compatibility bounds and launch flags.
Historical live-probe versions below are evidence, not the current support pin.


Kimi Code (`kimi`, Moonshot AI's coding CLI — repo `MoonshotAI/kimi-code`, the
TypeScript successor to the legacy Python `kimi-cli`) reads the boot artifact
(`AGENTS.md`) natively — already emitted by the render chain — so the adapter
carries the launch command, its native skill root, a headless block, and a
sandbox approval flag.

This adapter connects Subfloor to the kimi CLI. Account entitlement and
billing are provider-owned; they are not part of the adapter contract.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness (`kimi` — cwd is the workspace; no dir flag exists) |
| `comm_aliases` | extra `/proc/<pid>/comm` values a live kimi presents (`kimi-code` — see below) |
| `surfaces` | which lanes this harness may serve (`terminal`, `one_shot`, `browser`, `sprint` — all true) |
| `boot_artifact` | the context file this harness reads (`AGENTS.md`, informational) |
| `emit` | files copied to the repo root at launch (none — kimi reads `~/.kimi-code` + `AGENTS.md`) |
| `skill_dirs` | extra skill roots this harness reads natively (`.agents/skills` — see Skills); `.claude/skills` is always rendered as the cross-harness default |
| `mcp.streamable_http` | `supported: false` — no managed streamable-HTTP MCP injection, so the browser and windows-mcp servers claude/codex/opencode get are not wired here |
| `headless.launch` | non-interactive base argv (`kimi`) |
| `headless.prompt_flag` | `-p`; run.py emits it immediately before the prompt value |
| `headless.model_flag` | `-m`; the selector must be an alias discovered from `~/.kimi-code/config.toml` |
| `headless.effort.env` | `KIMI_MODEL_THINKING_EFFORT`; applies an explicitly requested headless effort level |
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
default remains unsafe. Headless boots are different: Refresh models
discovers the exact local aliases (for example `kimi-code/k3`), the resolver
accepts only a locally available high-effort route, and the adapter emits
`kimi -m <alias> -p <prompt>`. A requested selector that cannot be applied fails
before the worker session opens.

**`comm_aliases: ["kimi-code"]` — the liveness seam:** the shell-liveness scan
identifies a live harness by `/proc/<pid>/comm`, which is the process's RUNTIME
name, not its launch binary. kimi execs `/usr/local/bin/kimi` (a single ELF; no
wrapper, no child) and then renames ITSELF to `kimi-code` via `PR_SET_NAME`
roughly 0.7s in — measured on a real headless worker, `exe → /usr/local/bin/kimi`,
no child processes, 11 threads in the one tgid. Without the alias a live kimi
worker matched nothing, so its shell projected as **available** and the operator
was invited to double-book it. Two consequences worth knowing: during that ~0.7s
startup window comm reads `MainThread` (the bun runtime's initial thread name)
and the worker is briefly invisible — deliberately NOT aliased, because
`MainThread` would match every unrelated bun/node process on the machine, trading
a 0.7s false-available for a permanent false-busy. And a finished-but-unreaped
kimi keeps `comm=kimi-code` while its `cwd` link becomes unreadable, so it lands
in the scan's existing `indeterminate` bucket rather than holding a shell.

**Skills:** kimi does not read `.claude/skills/`. Its project skill roots are
`.kimi-code/skills/` (brand) and `.agents/skills/` (the generic cross-harness
path), both resolved against the project root it finds by walking up for
`.git`. The adapter therefore declares `skill_dirs: [".agents/skills"]` and,
like codex, gets the booting shell's exact grants delivered natively; the boot
doc's `## SKILLS` block remains the harness-agnostic backstop rather than the
only channel. `.claude/skills` is still rendered because `render_harness_skills`
always prepends it — harmless here, since kimi ignores it.

`.agents/skills` needed no new plumbing: it is already the codex-native path,
already gitignored (`/.agents/skills/`, written by the installer and kept by
the gitignore sync), already listed in `engine_paths` as a generated install
path so `./sc remove` deletes it, and already pruned per boot by
`skill_projection` so a retired grant's folder does not survive into the next
session.

Verified live on the installed CLI (`kimi 2.0.1`, 2026-09-21): a skill dropped
at `<root>/.agents/skills/<name>/SKILL.md` is listed by `kimi -p` as an
available session skill, both in an ordinary clone and in a linked git worktree
whose `.git` is a file — the exact shape of a shell seat at
`.sc-worktrees/<shortname>`. The binary's own root table confirms it:
`PROJECT_BRAND_DIRS = [".kimi-code/skills"]`,
`PROJECT_GENERIC_DIRS = [".agents/skills"]`.

**That probe was run OUTSIDE the conversation window declared at the time.**
`conversation` then pinned `[0.30.0, 0.34.0)` with `0.33.0` verified; 2.0.1 was
far above it. On 2026-11-12 the ceiling was raised to `2.6.0` to cover the
installed `2.5.3`, by operator ruling (flag SC-1610, Feature #95 spec #272):
build against the installed version and assume its behaviour holds.
`verified_cli_version` stays `0.33.0`, so an installed 2.5.3 reports
`supported`, never `verified`:
nothing here re-probes the `kimi-print` driver contract. Native skill delivery
does not depend on that window — `skill_dirs` is not version-gated — and the
failure mode is benign in both directions: an in-window `0.3x` build that does
not read `.agents/skills` simply ignores a rendered directory, and the boot
doc's `## SKILLS` block remains the backstop that actually carries the grants.
Treat native delivery as proven on 2.0.1 and assumed elsewhere.

**User-level roots are not ours.** kimi also reads `~/.kimi-code/skills` and
`~/.agents/skills`. The engine never writes, prunes, or reconciles either one:
they are the operator's, they are shared across every project on the host, and
a skill living there will appear in a shell's session without being one of its
grants. The only engine-managed roots are the two this adapter puts in the
worktree — `.claude/skills` and `.agents/skills`; the project-level
`.kimi-code/skills` is undeclared and therefore unmanaged too.

Only the generic path is declared. `.kimi-code/skills` would be a second,
kimi-only mirror of the same grants needing its own ignore and cleanup
coverage, and it buys nothing kimi does not already read from `.agents/skills`.

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
in the Dockerfile) — otherwise the mount would shadow the baked binary with an
incompatible host binary inside the Linux container.
Same reason `ensure_harness_path()` is a no-op under `SC_SANDBOX`.

## Conversation capability

The `kimi-print` driver records a native session-directory reference, streams
normalized events from print mode, resumes with `-S`, and interrupts with
`SIGINT`. Model and effort selections must resolve to supported local routes;
`headless.effort.env` supplies `KIMI_MODEL_THINKING_EFFORT`. Session inspection
uses native stored evidence. The manifest does not advertise interactive
permission responses. See `scripts/conversation_adapters/kimi.py` in the engine.

### Context meter, compaction and the compact trigger (Feature #95, spec #275)

Checked on Kimi Code `2.5.3` with `kimi-code/k3.1-pro`, 2026-11-13, live on
the delivery host. The adapter reads the main wire (`agents/main/wire.jsonl`)
after the run ends, as it always has:

- **Meter.** Each `usage.record` with `usageScope: turn` is one model request
  of the chat and becomes one `usage` event with scope `request`:
  `context_tokens` = `inputOther` + `inputCacheRead` + `inputCacheCreation`,
  `context_window` = the model's `max_context_size` read (never written) from
  the `config.toml` beside the session store, left out when unreadable.
  Kimi events are ingested after the run, so `created_at` is ingest time;
  each reading carries **`requested_at`, the record's own `time` in epoch
  milliseconds (UTC, integer)**, for gap and cache figures. `usageScope:
  session` records (a compaction's own request among them) give no reading.
  The whole-turn total is kept as before, now labelled scope `turn` with no
  `context_tokens`.
- **Compaction event.** `context.apply_compaction` becomes `context.compacted`
  with `tokens_before` / `tokens_after`; the trigger is the `source` of the
  preceding `full_compaction.begin` (`manual`, `auto`, otherwise `unknown`)
  and `compaction_ref` is `<session>@<time>`, so a replayed slice is not
  recorded twice. `summary` and `contextSummary` are never read into an event.
- **Trigger (the spec's gate).** `/compact` as the prompt of a resumed
  `kimi -S <id> -p` run does **not** compact: it reaches the model as plain
  text. `/compact` as ACP `session/prompt` (`kimi acp`, after `initialize`
  and `session/load`) does: it answers `end_turn` at once and compacts in
  the background, writing `full_compaction.begin` (`source: manual`),
  `context.apply_compaction` and `full_compaction.complete`. The connection
  must stay open until then; closing it cancels the compaction
  (`full_compaction.cancel`). The first `session/load` of a session appends a
  project-context injection just after it answers, and a compaction begun
  before that lands is cancelled, so the adapter waits for the wire to go
  quiet before it sends `/compact`. The manifest's `conversation.compact`
  block records the mechanism; `manual_compaction` is true.
- **Run identity.** A compaction run writes no `turn.prompt` and no `end_turn`
  step, so it is found by the `full_compaction.begin` it appends after the
  wire boundary taken just before `/compact` is sent (anything the load
  wrote, a compaction of Kimi's own included, is never claimed; run ref
  `kimi-<time>-<offset>`, like a turn's) and ends on `full_compaction.complete`
  (`run.completed`, with the marker and the request's own usage under scope
  `compaction`) or `full_compaction.cancel` (`run.failed`,
  `HARNESS_COMPACT_REFUSED`). No end within 15 minutes ends the run with no
  marker, which the chat shows as not reported. A recovered compaction run
  reconciles from the same records and records the marker and the cost the
  live stream did not; the cost names its record (`usage_ref`), so the store
  keeps one copy of each.

Kimi takes no token-count trigger at launch (`compactionTriggerRatio` is a
ratio of the window in the user's own `config.toml`, which the engine never
edits), so `compact_window_native` is false: the engine compacts Kimi chats
between turns when the meter reaches the harness's trigger, and while idle.

## Subagent keys (`headless.agent`, Feature #96, spec #273)

`scripts/job_agent.py` builds an `sc job` subagent command from the `headless`
block and these optional keys; no harness name appears in the job code. An
adapter without them is refused for a subagent by the name of the missing key
and is unaffected otherwise.

| Key | Value | Meaning |
|---|---|---|
| `brief.delivery` | `argument` | the brief is the value of `-p`; the ledger records the token `{brief}`, never the text |
| `result` | `--output-format stream-json`, `stdout_jsonl`, `role: assistant`, field `content` | the last assistant record is the final message |
| `session` | `event`, `session.resume_hint`, field `session_id` | the final meta record names the session |
| `resume` | `-S` | `-S <id>` continues the session; the id is unchanged |

Checked on Kimi Code 2.5.3, 2026-11-12, model alias
`kimi-code/kimi-for-coding-highspeed`, four requests: a text-mode launch, a
`stream-json` resume with `-S` that recalled the first reply, and two fresh
`stream-json` launches whose briefs began with `---` and with `-h`. Both were
accepted as the value of `-p`, so a brief may start with dashes. Text mode
prints the reply on stdout behind a bullet and the resume hint on stderr, so
the structured stream is used. `kimi session list --cwd <dir> --json` also lists
the session without a request. Exit codes on stop and timeout were not
observed.

### What a subagent on this harness is

`sc job agent --harness kimi --model <selector> [--effort <level>]` runs Kimi Code
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
  it travels in the environment as `KIMI_MODEL_THINKING_EFFORT`; when omitted, no effort setting is sent and the CLI's own default
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
