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
so fork-owned config is untouched). The hook runs the shared branch-guard before
every `Edit`/`Write`/`NotebookEdit`/`MultiEdit`. It resolves the script
env-independently (walking to the main worktree root via `git rev-parse
--git-common-dir`, the same way `sc` does; `$SC_ENGINE_DIR` is an optional
fast-path override) — a fork gitignores `.super-coder/`, so a worktree-relative
path found nothing and the hook failed open in exactly the shell worktrees it
protects. It blocks
the edit (exit 2) when the **target file's** repo HEAD is a protected default
branch — so a worktree shell writing to the stale repo-root checkout is caught,
not just one whose own cwd is on `main`; an out-of-worktree edit to a feature
branch is allowed with a warning. With no stdin target it falls back to the cwd's
branch. Protected set defaults to `main master`; override per-fork with
`SC_PROTECTED_BRANCHES` (space-separated). Set `SC_SHARED_DIRS` (space-separated
absolute paths) to declare host-level shared directories that all shells may write
into without warnings — handoff folders, screenshot dirs, cross-shell scratch.
These are fully exempt from both the branch block and the out-of-worktree warning.
This is the enforcement behind the `git` skill and the boot-template VERSION
CONTROL rule — a skill loads too late to stop the first edit; the hook fires
before it. Re-emitted (idempotently) each launch, so it survives `./sc update`.

`scripts/branch-guard.sh` is the **one** branch-decision script shared by all four
harnesses (claude + codex hooks, the opencode plugin, the git pre-commit backstop)
— so `SC_PROTECTED_BRANCHES` and the message stay identical everywhere.
