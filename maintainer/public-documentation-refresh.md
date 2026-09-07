# Public documentation refresh: preparation evidence

Feature #73, Spec #219, task #795. This is implementation evidence, not the
public user guide or a declaration that the refresh has shipped.

## Source baseline

Frozen source ref: `706eaa3da0ce2fe5e6a77644e0e520b43e92c374`.
Verified against remote `origin/main` on 2026-09-07. Paths below are relative
to the source repository and refer to that commit. Recheck changed owners
before final review; the running engine checkout is not evidence of remote
freshness.

| Contract | Source anchor | Documentation consequence |
|---|---|---|
| Five adapters | `.super-coder/adapters/{claude,codex,kimi,opencode,vibe}/adapter.json` | Name all five; read each manifest's `surfaces` before claiming browser or Sprint support. |
| Six flavors | `.super-coder/templates/shells/{admin,planner,dev,reviewer,devops,cartographer}.json`; `scripts/shell_factory.py` under the engine | Distinguish available flavors from the initial roster; Bespoke is untemplated. |
| Ten initial shells | `.super-coder/scripts/init_fork.py`, `TEAM_ROSTER` and separate Cartographer creation | Two Planners, four Developers, two Reviewers, one Admin, one Cartographer. DevOps is available but not initially seeded. |
| Installer interview | `.super-coder/scripts/init_fork.py`, `main`; `.super-coder/scripts/install.py`, `print_help` and seeding phase | Interactive input is the operator username; primary-shell customization is optional via flags. |
| Worktree examples | `.super-coder/scripts/init_fork.py`, `TEAM_ROSTER`; `.super-coder/assets/skills/git/SKILL.md` | Use a real roster shortname, such as `DEV1`, and `.sc-worktrees/dev1` / `shell/dev1`. Admin is the main-checkout exception. |
| Ten GUI tabs | `.super-coder/ui/index.html`, header navigation | Chats, Sprints, Shells, Roadmap, Docs, Flags, Worktrees, Repo Map, Analytics, Scripts. No standalone Skills tab. |
| Merge gate | `.super-coder/scripts/sprint_cli.py`, required `--merge-grant` and `authorize-merge`; decision #325 | Outside an armed Sprint, explicit operator direction names the PR. Arming records that direction for registered Sprint PRs; the owning Developer needs live authorization before merging. |
| Private state | `.super-coder/scripts/instance_state.py`, `InstanceState`, `_state_home`, `active_database_path`, `active_snapshot_path`, `active_backup_paths` | Fresh bound installs select private XDG instance state. Legacy and relocation handling are recovery concerns, not the current installation layout. Repo-local renders and configuration are separate. |
| Ordinary shell boundary | `.super-coder/scripts/execution_view.py`; decision #302 | Downstream ordinary shells use the API; general engine SQL and direct state inspection belong to Admin. Source code visibility does not grant live-state maintenance authority. |
| Skill ownership | `.super-coder/scripts/skill.py`, API lane; decision #313 | Planner owns fork-local skill operations; Admin owns engine maintenance. |
| CLI inventory | `.super-coder/scripts/dispatch.sh`; `sc help --all`; decision #326 | Prose is a workflow route map. Exact flags remain in command help. Validate commands without executing destructive verbs. |
| Effort and reopened chats | `.super-coder/ui/app.js`, `thinkingLevelLabel`, supported-effort selection, `reopenable`, `conversation.reopened` | Explain supported route-specific effort choices and sending a message to reopen eligible closed chats; Sprint-scoped chats are excluded. |
| Browser capture | `.super-coder/scripts/visual_qa.py`; `.super-coder/templates/fork/visual-qa.example.json` | Existing Playwright capture surface; local dev kit supplies Playwright and Chromium. Engine does not depend on Playwright. |
| Terminal capture | `docs/demo.tape` | VHS recipe needs current operator command, roster, picker and a real authenticated boot. Existing tape is not current acceptance evidence. |

Feature #28 package-manager distribution stays excluded, as Spec #219 requires.
The frozen tree has no package recipes or native release artifacts establishing
that installation path. A roadmap status alone cannot supply that evidence.

## Capture baseline

The operator-authorized dos-app checkout was clean on `main` at
`c5851784070ff92e0ae36ed093b80915cd3cf114`, retaining its existing install
commit. Its installed engine ref was
`1984c730af24b1c855101be0412d352969995e5d`.

Preparation preserved its refs in a Git bundle and created an independent
disposable clone from that exact dos-app commit. The clone's
`docs/capture-baseline` branch received `.super-coder` and `sc` from the frozen
source ref above. No reset, update, restart, or state mutation was performed
against the original dos-app checkout or runtime.

The disposable installation attempt was:

```sh
python3 .super-coder/scripts/install.py --force --skip-harness-install --runtime sandbox --username Demo
```

It exited 1 during installation identity creation because this launched Dev
seat cannot create a directory beneath the private instance-state root.
The installer raised `instance_state.InstanceStateError` with permission
denied. No state-root override or permission change was attempted.
See [issue #1541](https://github.com/jedbjorn/subfloor/issues/1541).

Admin provisioned the same clone at the frozen engine ref on 2026-09-07
(handoff #1807). Installation exited 0, applied 201 migrations, seeded the
10-shell Demo roster, and completed snapshot/render. Admin corrected an ambient
API-routing defect separately (flag #615); cross-checkout operations must not
inherit another instance's API environment. Flag #614 is closed.

The clean disposable Git baseline is
`09da493` (`docs/capture-baseline`), preserving the original dos-app commit
and pinning the capture engine. The original checkout and runtime remain
untouched. Recovery bundle and installation logs are retained in the local
handoff evidence directory. Sandbox launch and authenticated visual acceptance
belong to the capture/verification tasks; they are not implied by installation.

VHS, ttyd, ffmpeg, Docker, Node and Python 3.14.7 are available. Browser capture
dependencies, sanitized demonstration state and authenticated harness boot
still require verification. Do not substitute simulated terminal output or the
existing running fork's private data.

## Remaining delivery gates

Tasks #796–#800 remain pending. Once preparation passes, correct the four core
surfaces, then refresh workflows, audit all five adapter READMEs and the seven
top-level engine runbooks, and capture the settled GUI inventory. Preserve the
immutable checkpoints in `docs/deepseek-harness-removal.md` when labeling it.

The visuals must cover the CLI picker, Roadmap Board and Flow, Worktrees, Chats,
and Sprints. Review actual assets at rendered size for privacy and legibility.
Finish with command/link/anchor/fact drift checks, a quick-start sandbox walk,
themed rendering, configured CI, independent documentation review, and a final
link/render pass after review fixes. None of those gates is certified here.
