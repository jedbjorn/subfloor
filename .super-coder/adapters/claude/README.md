# adapters/claude — Claude Code

Claude Code reads the boot artifact (`CLAUDE.md`) and `.claude/skills/<name>/SKILL.md`
natively — both already emitted by the render chain — so the adapter only carries
the launch command. No extra config file to emit at v1.

`adapter.json` fields (the harness seam contract):

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness |
| `boot_artifact` | the context file this harness reads (informational) |
| `emit` | files in this dir copied to the repo root at launch (none for Claude) |
| `env` | extra env merged into the launch environment |
| `model` | `{ "flag": "--model" }` — run.py appends `--model <id>` for the flavor's claude model (alias: `sonnet`/`haiku`/`opus`) |
| `merge_json` | always-on: project-scoped JSON deep-merged every launch (preserves fork keys). Installs the branch-guard hook into `.claude/settings.local.json`. |
| `sandbox` | `merge_json`: project-scoped config patched in-sandbox only (allow-all permissions) |

## Branch-guard hook

`merge_json` deep-merges a `PreToolUse` hook into **`.claude/settings.local.json`**
(the gitignored personal layer — never the fork's tracked `.claude/settings.json`,
so fork-owned config is untouched). The hook runs `hooks/branch-guard.sh` before
every `Edit`/`Write`/`NotebookEdit`/`MultiEdit` and blocks the edit (exit 2) when
HEAD is a protected default branch — forcing a feature branch before any work
lands. Protected set defaults to `main master`; override per-fork with
`SC_PROTECTED_BRANCHES` (space-separated). This is the enforcement behind the
`git` skill and the boot-template VERSION CONTROL rule — a skill loads too late to
stop the first edit; the hook fires before it. Re-emitted (idempotently) each
launch, so it survives `./sc update`.
