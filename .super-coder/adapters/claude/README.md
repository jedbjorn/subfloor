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

A `.claude/settings.json` template could live here later (permissions, hooks);
not needed for v1.
