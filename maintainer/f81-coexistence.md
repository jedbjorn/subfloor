# F81 / spec #231 implementation and acceptance handoff

Candidate: the PR containing this file, based on `ec81eea5ff85ec19e38aea388b6b66ba0e68872e`.
Record the final PR head in native acceptance evidence; these source observations
are not native smoke results. Decision #364 governs the directory boundary.
Frozen F37/spec #101 and doc #103 remain historical records.

## Ownership audit

All paths below are relative to the managed working directory unless explicitly
prefixed with HOME. Dispositions: **removed**, **launch-scoped**,
**preserved-user-owned**, **retained-repository-policy**. A native session in
an installed repository can still discover the retained project artifacts.

| Adapter | Instructions and skills (writer → consumer) | Hooks/plugins and permissions (writer → consumer) | MCP (writer → consumer) | Homes, credentials/history, environment and discovery |
| --- | --- | --- | --- | --- |
| Claude | **removed**: HOME `.claude/CLAUDE.md`; legacy `global_pointer.write_global_pointers` retired. **retained-repository-policy**: `run.main/prepare_launch` and `conversation_boot` → `CLAUDE.md`; `run.render_harness_skills` / `skill_projection` → `.claude/skills` → Claude discovery. | **retained-repository-policy**: `run.apply_merge_json` → `.claude/settings.local.json` PreToolUse branch guard and SessionEnd telemetry → Claude hook loader. **launch-scoped**: `run.launch_mode_flags`, conversation adapter → skip-permissions / bypassPermissions. | **launch-scoped**: `run.managed_mcp_injection` / conversation adapter → native `--mcp-config` for enabled browser / windows-mcp servers. | **preserved-user-owned**: native login, `.claude.json`, `.claude` history and `CLAUDE_CONFIG_DIR`; no new home override. `install.check_auth` and quota probe read credentials. **launch-scoped**: copied child environment, including sandbox `IS_SANDBOX`. `token_parsers.claude` uses configured `projects` JSONL and recorded cwd filtering; `live_model.claude` uses the requested worktree; native session store supports exact resume. |
| Codex | **removed**: HOME `.codex/AGENTS.md` and effective `CODEX_HOME/AGENTS.md`. **retained-repository-policy**: both local boot artifacts; `.claude/skills` and `.agents/skills` via the shared boot/skill writers → Codex discovery. | **retained-repository-policy**: local `.codex/hooks.json` / `branch_guard` → Codex hook loader; `run.trust_codex_worktree` appends only `[projects."<exact-worktree>"]` to native config.toml. **launch-scoped**: native launch flags and app-server thread policy carry sandbox/approval policy and hook trust bypass. | **launch-scoped**: shared MCP recipes → native `-c mcp_servers...` / conversation adapter. No universal MCP write. | **preserved-user-owned**: effective `CODEX_HOME`, auth.json, config defaults, native history; credential/model discovery reads only. `token_parsers.codex` reads configured `sessions` rollouts, excludes off-repo metadata before attribution. Native app-server thread IDs bind browser resume. **launch-scoped**: copied environment; no new CODEX_HOME or global permission default. |
| OpenCode | **removed**: HOME `.config/opencode/AGENTS.md`. **retained-repository-policy**: AGENTS.md, `.claude/skills`, `.opencode/skills`; `run.emit_adapter` / `opencode_config` → `opencode.json` instructions including tool-discipline.md → OpenCode loader. | **retained-repository-policy**: `run.resolve_opencode_plugins` / `opencode_config` → local protect-default-branch and enforce-model-route plugins, edit/webfetch/bash policy. **launch-scoped**: conversation session permission rules; sandbox bash allowance remains local config. | **retained-repository-policy**: `run.apply_managed_mcp` merges enabled servers into local `opencode.json` through `opencode_config`; native project consumer. | **preserved-user-owned**: native config, auth/history in XDG data locations; no universal settings writes. **launch-scoped**: `OPENCODE_DISABLE_CLAUDE_CODE=1` and enforced model route only in child env. `token_parsers.opencode` filters SQLite session.directory by repository; `live_model.opencode` opens read-only and matches exact worktree. Native server session IDs bind resume. |
| Kimi | No legacy global pointer declaration. **retained-repository-policy**: local AGENTS.md and default `.claude/skills` projections via shared boot writers; discovery compatibility requires native acceptance. | No emitted global/local hook or plugin configuration. **launch-scoped**: sandbox `--yolo`; native prompt mode auto permissions. | No managed MCP injection declared. | **preserved-user-owned**: `.kimi-code`, credentials/config/history and caller-selected KIMI_CODE_HOME; native browser adapter resolves its session root from copied env and actual worktree. **launch-scoped**: KIMI_MODEL_THINKING_EFFORT. `token_parsers.kimi` checks state.json workDir; `live_model.kimi` matches exact worktree and main wire. Native exact session binding remains unchanged. |
| Vibe | No legacy global pointer declaration. **retained-repository-policy**: local AGENTS.md and default `.claude/skills` projections; discovery compatibility requires native acceptance. | No emitted hook/plugin config. **launch-scoped**: terminal `--trust`, sandbox `--agent auto-approve`. One-shot/browser are unsupported, unchanged. | No managed MCP injection declared. | **preserved-user-owned**: `.vibe` native login/config/history; MISTRAL_API_KEY forwarded only to the launched child/container when already supplied. `token_parsers.vibe` filters native meta.json environment.working_directory. No new home override or session attribution mechanism. |

### Shared surfaces and evidence

- `global_pointer.reconcile`: **removed** global ownership. The module name is
  retained for lifecycle imports, with no writer function. The cleanup-only
  asset records the three complete templates and actual target declarations
  from `dab98db0`, `894967f3`, and `52c86168`; LF and CRLF are accepted.
  `tests/test_global_pointer.py` covers defaults, effective/explicit roots,
  absent/user/edited content, backup ambiguity, permissions, symlinks, concurrent
  processes, user changes, interruption, CLI dispatch and repeat lifecycle calls.
- `install.ensure_harnesses`, `update_harnesses`, install main, `run.main` and
  `prepare_launch` call reconciliation. `dispatch.sh::sc_harness_cleanup` also
  runs on host launch/enter before host or Docker handoff; sandbox cleanup
  refuses to inspect host mounts. A cleanup failure warns and remains advisory
  for lifecycle commands, while explicit apply/check exits nonzero if unresolved.
- `map_setup.run_update_compat` launches newly materialized `update_compat.py`,
  which invokes cleanup regardless of old bridge markers. This is the old
  updater adoption seam. `test_update_legacy_compat` exercises the real new bridge
  in a fresh subprocess and restores a legacy pointer's user backup. No cleanup
  marker suppresses a later retry or a different installation's reconciliation.
- Directory `flock` coordinates updated installations sharing a config root,
  without creating a config directory or lock file. Ancestors are opened with
  O_PATH/O_NOFOLLOW; the final directory remains pinned. Target and backup
  identity/content are rechecked before unlink or same-directory atomic rename.
  Backup bytes are never rewritten; restored modes never widen backup access.
  SIGKILL can leave a private staging file, but retries still converge without
  using that file as ownership evidence. Arbitrary uncoordinated writers can
  race after revalidation; this is deliberately not a stronger locking claim.
- `run.main/prepare_launch`, `conversation_launch`, `conversation_adapters.base`:
  **launch-scoped** copied child env carries API token/base, shell ID/name,
  worktree, harness and route; no startup or universal config exports.
  Admin engine-down authority and local boot composition are unchanged.
  `conversation_boot` restores conversation snapshots at exact resume.
- `shell_alias.install`: **launch-scoped** explicit `subfloor` command in bash
  startup and fish function/completion files. Merely sourcing the bash block
  makes no subprocess/API call, exports no identity and leaves native command
  resolution intact (`test_agent_coexistence`). Fish contains only a function
  definition and completions; native fish startup evidence is pending.
  `sc_wrapper.register_install` owns the explicit `sc` command and registry,
  not any native harness command.
- `map_setup.wire_hooks`: **retained-repository-policy** per-clone
  `git -C <root> config core.hooksPath`, without `--global`; native operator
  commits remain guarded. `test_branch_guard` verifies this retained policy.
- `dispatch.sh::dcreds`: **preserved-user-owned** native mount roots. It creates
  missing config directories and a missing `.claude.json` containing `{}` for
  Docker bind types; it does not populate instructions or override existing
  native settings. Native credential mounts persist login/history. The native
  binary installers remain explicit install/update operations on shared tools.
- `analytics._shell_for_cwd` and every `token_parsers.*::sweep`:
  **retained-repository-policy** recorded path boundaries; remote URL/application
  name never enrolls a session. `test_analytics` and `test_agent_coexistence`
  cover off-repo rejection, prefix collisions, repository root and worktrees.
  Existing native history is not rewritten or deleted by these readers.
- Unknown global files/hooks/plugins/MCP settings remain **preserved-user-owned**.
  There is no substring-based global sweep. A known target with an edited
  sentinel or ambiguous backup is reported by exact path for operator handling.

## Acceptance status and remaining gate

Local host evidence on 2026-09-12: Claude `2.1.269 (Claude Code)`, Codex
`codex-cli 0.153.4`, OpenCode `1.18.30`, Vibe `vibe 2.22.0`, Kimi `0.39.1`.
These are installed-version observations, not smoke results. The source's
verified versions are respectively 2.1.223, 0.147.0, 1.18.9, 2.22.0 and 0.33.0;
installed Codex and Kimi exceed supported source ranges. Flag #639 blocks native
acceptance until a qualified isolated seat is supplied. No harness was updated,
provider session started, service stopped or operator HOME cleaned for this work.

The initial affected regression run passed 307 tests and 163 subtests, with one
failure: `test_recovery_view_masks_private_state_and_parent_root_alias` reports
`restricted_shell_view_unavailable`. The same test fails on a disposable archive
of unchanged origin/main `ec81eea5`, so the required execution-view proof needs
CI/a qualified seat. The first full CI run passed that execution-view test,
plus Docker lifecycle, boot verification, host-contract and render checks. Its
four failures identified missing manifest/CLI integration and dispatcher
registration expectations; those are corrected in the follow-up commit. The
guard was not relaxed. Cleanup lint and type checking
pass. The subsequent lifecycle/MCP/config run passed 112 tests and 38 subtests;
two branch-guard checks were invalidated by placing the disposable HOME under
/tmp, which is deliberately guard-exempt. Re-running the branch-guard module
with disposable HOME under ignored workspace state passed all 12 tests and
7 subtests. The PR records authoritative CI status.

Before release, independent acceptance must record one exact candidate head,
native versions, launch surface, discovered boot/config, a harmless command
result, exact native session identity on resume, and observed Subfloor API/MCP
requests. Use disposable homes/config/repositories and independently supervised
services: never stop the operator's live API to simulate failure.

| Required native evidence | Status |
| --- | --- |
| Fresh unrelated Claude/Codex/OpenCode sessions with API up and down, no Subfloor prompt/contact/identity/permission injection | Pending qualified seat |
| Concurrent unrelated bare + explicit managed session; native login, user hooks/settings/history continuity | Pending qualified seat |
| Terminal, one-shot, browser and exact resume, host and sandbox; correct boot/skills/permissions/guards on supported surfaces | Pending qualified seat; Vibe terminal only |
| Unrelated checkout sharing remote/name excluded; ordinary local user instructions still apply | Automated path boundary passes; native discovery pending |
| Kimi/Vibe native ambient/discovery audit, fish startup | Source audit complete; native probes pending |

## Operator rollout handoff

After release, inventory installations and config roots sharing the OS user.
Update or stop using every old writer before claiming durable coexistence.
From an updated installed/source checkout on the host, without needing an API:

```sh
./sc harness-cleanup --check
./sc harness-cleanup --apply
./sc harness-cleanup --check
# Repeat the option for additional known nondefault legacy roots:
./sc harness-cleanup --check --config-root codex=/absolute/old-codex-home
```

Check defaults to read-only, one result per path. `removable`/`restorable` predict
apply; `unresolved` requires the named cause to be corrected by the operator.
Ambiguous user content is never chosen automatically. Absent targets remain
absent even with a backup. Config roots and the current environment's overrides
are bounded inputs, not a recursive HOME search. Backups are retained after
restoration. A partial result exits nonzero; repeat after resolving its cause.

Start a fresh native conversation from a normal terminal in an unrelated
directory after cleanup. Already loaded conversation context is not erased.
Installed repository roots, their subdirectories and shell worktrees retain
project behavior, even on direct native invocation; children of an active
Subfloor shell can inherit that shell's environment. Rolling back to an old
engine reintroduces global pointer writing. Live Dev rollout and final frozen
F81 how-it-works documentation remain the operator/Planner handoff after merge.
