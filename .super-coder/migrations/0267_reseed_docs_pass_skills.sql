-- 0267 — reseed six substrate skills for the source-verified docs pass.
-- drive_browser: the proxy no longer sets an action deadline (PR #1624), so the
--   stop-and-escalate rule for a timed-out action taught a condition the proxy
--   cannot produce; it now says to wait for Playwright's own result.
-- self_update: rollback's write and restore paths are distinct. The backup of
--   the current DB is WRITTEN to an ordered, fail-closed destination
--   ($SC_DB_BACKUP_DIR, else ~/db_backups/<repo>, else the repo-local
--   .sc-state/db_backups) and pruned to the five newest per lifecycle prefix
--   per directory; the restore point is DISCOVERED across every candidate
--   directory, so a changed writable destination cannot hide it
--   (db_backup.select_backup_dir vs db_backup.latest_backup).
-- remote_seats: `./sc vm-bake` is the host-only, never-in-the-sandbox
--   host-direct escape hatch with no broker in the path, not an alias for the
--   brokered `vm bake`.
-- tailscale_diagnostics: the forbidden-character set also holds a carriage
--   return.
-- git / git_cleanup: complete the gitignored-artifact list and state the real
--   safe_to_clean_all predicate.
-- No schema change: full-body UPSERTs on name keep skill_id, so existing grants
-- survive, and converge upgraded installations on the same text a fresh seed
-- produces. Idempotent.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'drive_browser',
  'Drive the FnB''s dedicated Chromium Subfloor profile through the managed Playwright extension for a named browser task or logged-in dev-preview check. Bare metal with Claude, Codex, or OpenCode only; Admin uses the same surface for diagnosis and repair.',
  'substrate',
  'sc browser',
  0,
  '# drive_browser

Use only under an FnB directive naming the task, site/app, and allowed actions.
Restate that reach before the first browser call. If no directive exists, ask.
Do not expand it to other accounts or sites. Credential/account changes,
payments, public posting, and downloads outside the output directory require
explicit coverage in the directive. Never enter credentials when a site asks
to sign in; stop and report.

Run `sc browser status --json`. If absent, disarmed, unsupported, or failed,
report the returned state and stop. Use the managed MCP connection; never edit
harness configuration, start a substitute server, or arm the feature yourself.
The returned `proxy_url` is yours and the tab group is
`Playwright · Subfloor <SHORTNAME>`.

Run `sc browser open --json` to launch the linked, existing Subfloor profile.
No live window is required. The FnB still approves each new shell connection
in the Playwright Extension; never click that approval control yourself. Work
only in your tab group, without moving or touching other groups'' tabs.

Snapshot before acting, preferring accessibility snapshots over screenshots.
Save screenshots/downloads only in the returned output directory. Close the
tabs you opened unless directed to leave them. Report the result, tab group,
the session path returned by Playwright, and anything left open.

`extension not connected` means the extension is unavailable or the connection
is unapproved; it does not prove the profile window is closed. Report and stop;
never retry in a loop. The proxy sets no action deadline — it waits for
Playwright''s own action or navigation result and keeps the approved session
alive, so a slow action is still running: wait for its result rather than
replaying it. Actions are never retried, and overlapping requests on one
session refuse rather than queue. Confirmed transport loss ends the connection
and is reported as such. `disarmed` requires the FnB to arm it.

Admin diagnosis/repair stays under the operator''s named assignment. The FnB''s
host terminal can run `sc browser setup --json` to detect and link the existing
profile, or `doctor`, `up`, `down`, `arm`, and `disarm`. Launched shell credentials
permit `status` and (with this grant) `open`; they cannot set paths or arm. Doctor
checks actual package capabilities and repairs missing or incompatible private
packages without version pins. It reports `setup_ready` separately from
`connection_ready`; `ok` requires both and running, armed services. Creating profiles, installing the extension, logins,
and approval clicks remain human steps. See the engine''s
`.super-coder/docs/browser-driving.md` for the setup and live acceptance record.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git events for a Subfloor shell — GitHub capability recovery, merging a stack the FnB hands you, after-merge cleanup, and what never enters Git. The every-session rules (sync, branch, PR, merge gate, finish) live in your boot.',
  'substrate',
  NULL,
  0,
  '# git — the event procedures

Your boot''s VERSION CONTROL section carries the every-session rules: sync the
base, branch before you build, commit → push → PR → stop, the merge gate''s two
forms, the disposable `shell/<shortname>` base, and the finish gate. This skill
holds what fires on an event.

One repo at its root -> plain `git` (cwd = repo root) is safe. Project = this
repo minus `.super-coder/`. In a tracking fork the engine (`.super-coder/`) is
gitignored, materialized by `sc update`, and authored upstream in Subfloor —
NEVER commit or edit anything under it. In the Subfloor source repository
(`git ls-files --error-unmatch .super-coder/schema.sql` exits 0) `.super-coder/`
is tracked project source and `.sc-state/engine.ref` is not the delivery unit;
engine changes still land by branch and PR.

## GitHub capability boundary

`sc launch` and `sc restart` re-resolve Git transport and GitHub API
capabilities from the host on every invocation, including `--no-build` forms.
`build`, `enter`, and an already running sandbox do not refresh auth. Pass = the
lifecycle summary says `ready` for the operation you need; `unavailable` and
`unverified` are NEVER readiness claims.

Preserve the configured `origin` transport. For SSH, fix the host agent and
load an authorized GitHub identity; NEVER copy or mount private keys. For
HTTPS/API, fix a scoped host `SC_GH_TOKEN` or the host `gh` OAuth login. Then
run `sc launch` or `sc restart`; the running sandbox remains unchanged
until that refresh. NEVER rewrite the remote or start an interactive login
inside the sandbox to work around a missing capability.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## After a merge — clean up local

Only after the PR is merged:

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## Never commit the engine or derived files

- In a fork `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, the per-harness skill renders (`.claude/skills/`, `.agents/skills/`, `.opencode/skills/`), the engine-managed harness config (`.claude/settings.local.json`, `.codex/hooks.json`), and `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main`. Admin shell = the one exception: repo root on `main`, committing there by mandate.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git_cleanup',
  'Admin-only — triage and clean the repo''s git state across main + every worktree. The acting sibling of git_hygiene.py''s read pass: delete what''s provably merged, preserve (never discard) outstanding work, sync to remote. Use when the FnB asks to tidy/clean git, prune branches, or reconcile worktrees.',
  'substrate',
  NULL,
  0,
  '# git_cleanup — the act pass over git state

`git_hygiene.py` reads; this acts on its report. **Admin shell only** — the one vantage at the repo root on `main`, seeing every worktree, exempt from the branch-guard. A working shell NEVER runs this; it tidies only its own worktree.

Governing asymmetry: a MERGED PR = proof a branch is safe to delete; uncommitted work has NO proof it is disposable. Delete only on evidence; preserve by default; discard only on the FnB''s explicit per-item OK. Unsure -> surface, never guess destructively.

Expect the report to be quiet — still run it:

- Since #119, `git_prune.py` deletes the provably-merged branch set (Tier A.1''s `stale` set, repo-global) at every boot -> Tier A often already clear. This pass = backstop for what automation won''t touch: `gh`-down unprovable merges, dirty worktrees, unpushed work, `main` fast-forward, remote-ref pruning.
- Working shells self-finish (sync before build, land/surface before stop) -> Tier B/C should be rare. A full Tier B/C = a shell skipped its finish gate -> fix it AND send that shell a note, not a silent fix.

## Investigation order — scripts first, always

1. Scripts: `git_hygiene.py` (git state) + `shell_liveness.py` (who''s live). Read their output; never re-derive by hand what one pass gives you.
2. Git history — only when a verdict is ambiguous (`merged: null`, unexpected dirty file): `git -C <path> log` / `reflog` / `show`.
3. Working-tree contents — last, only when history doesn''t explain it.

## Step 1 — Read the state (never skip)

```bash
python3 .super-coder/scripts/git_hygiene.py --text       # git: dirty/stale/clean
python3 .super-coder/scripts/shell_liveness.py --text     # who has a live session
```
Drop `--text` for JSON when driving decisions programmatically.

- `git_hygiene` -> every worktree (path, branch, dirty count, sample files, ahead/behind) + every local branch''s staleness (`merged` = true / false / null-unknown, with PR number). `gh_available: false` in the JSON -> treat every `merged: null` as unknown, never safe.
- `shell_liveness` -> which shells have a live harness session right now (read from `/proc` cwd — instant, self-cleaning). Your OWN session shows as the repo-root `is_self` entry — expected, not a blocker; the gate is about OTHER shells (Tier C).

## Step 2 — Triage into three tiers, act top-down

Sort every report item into exactly one tier.

### Tier A — auto-safe (act without asking). Only these three:

1. Merged-PR branches — `merged: true` + `is_base: false` + `checked_out: false`:
   ```bash
   git branch -D <branch>
   ```
   Squash-merge is the project default -> `git branch -d` refuses; that refusal is expected, not a stop signal. What survives boot-time `git_prune.py` is residue: merged since the last boot, or merged during a `gh`-down boot.
2. Dead remote-tracking refs:
   ```bash
   git fetch --prune
   ```
3. `main` behind origin + clean tree (admin''s root tree only):
   ```bash
   git pull --ff-only          # never a plain pull/merge on main — no merge bubbles
   ```
   `--ff-only` refuses -> main diverged -> Tier B/C, not auto.

NEVER auto-delete: a `merged: null` branch, an `is_base` branch (`main` or any `shell/<shortname>` — long-lived moving bases), or a branch checked out in a worktree.

### Tier B — outstanding work (propose -> FnB OK -> act). Preserve, never discard.

- Unpushed commits (`ahead > 0`): show the FnB `git -C <path> log origin/<base>..HEAD --oneline` first -> propose push + PR.
- Admin''s OWN root tree dirty: show the diff + proposed message -> on OK, cut a feature branch off main, commit, push, PR. NEVER discard the admin tree''s dirt without explicit instruction.

### Tier C — other shells'' dirty worktrees (gated; preserve-only)

Any other shell''s worktree: `is_main: false` + `dirty > 0`.

1. **Liveness gate.** Committing files is non-destructive, but re-branching a worktree under a mid-session shell stomps that live session. Read the `shell_liveness` verdict:
   - `safe_to_clean_all: true` -> admin presence confirmed, every other worktree dormant, nothing indeterminate -> act on all.
   - shortname in `active_other_shells` -> that shell is LIVE -> surface only, do NOT touch its tree. The others remain safe.
   - `indeterminate > 0` -> a harness process whose cwd was unreadable (another OS user, say) -> do NOT assume all-clear -> surface.
2. **Attribution.** The commit carries THAT shell''s trailer, never the admin''s. Read the display name for `shell/<shortname>` from `sc mem get shells`, then export the identity on the commit so the tracked `prepare-commit-msg` hook writes the trailer for you:
   ```bash
   SC_SHELL_NAME="<display_name>" SC_SHELL_SHORTNAME="<SHORTNAME>" git -C $WT commit -m "<msg>"
   ```
3. **Preserve** (shell cleared as not live):
   ```bash
   WT=.sc-worktrees/<shortname>
   git -C $WT checkout -b <type>/<short-desc>           # feature branch off its HEAD
   git -C $WT add -A
   SC_SHELL_NAME="<display_name>" SC_SHELL_SHORTNAME="<SHORTNAME>" git -C $WT commit -m "<msg>"
   git -C $WT push -u origin <type>/<short-desc>
   gh pr create --repo <owner/repo> --head <type>/<short-desc> --fill   # open, never merge
   ```
4. **Message the owning shell** — it must never boot to a silently rearranged tree:
   ```bash
   sc mem message send <shortname> ''git_cleanup: your worktree had uncommitted work. I preserved it on branch `<type>/<short-desc>` and opened PR #<n>. Your tree now sits on that branch — `git checkout shell/<shortname>` to return to your base.''
   ```
   Report the same to the FnB. Worktree left untouched (live / indeterminate) -> no message.

## Hard nevers

- NEVER `git checkout -- `, `git reset --hard`, `git clean`, or `git stash drop` on uncommitted work without the FnB''s explicit per-item OK — preserve is reversible, discard is not.
- NEVER commit another shell''s work under the admin''s attribution.
- NEVER act on a worktree whose shell may be live — surface instead.
- NEVER merge a PR — opening is the default; merging is the FnB''s gate.
- NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.
- NEVER touch the engine — `.super-coder/` is a gitignored materialized dependency, not your code.

## Step 3 — Report

Close with: deleted (evidence — PR #), pushed/PR''d (links), surfaced and awaiting the FnB''s call, + a final `git_hygiene.py --text` as the after-state. Nothing outstanding -> say so and stop.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'remote_seats',
  'Drive a machine through the host brokers — posture 1 owns a VM''s lifecycle with sc vm; posture 2 runs broker-served SSH exec, push, and pull against named remotes with sc remote. Keys never enter the shell. Opt-in; Linux targets need nothing more, Windows adds windows_testing.',
  'substrate',
  'sc remote',
  0,
  '# remote_seats — two postures, one broker

A shell drives another machine only through the host-side vm-broker. It holds
the libvirt connection, every SSH key, and the known-hosts files; the shell
calls `./sc vm` and `./sc remote`, which speak to the broker''s unix socket, and
never runs `ssh`, `scp`, or `virsh` itself — on bare metal or in a sandbox.
That is the whole access model (decision #353). Two postures share it
(decision #355):

| Posture | Who owns lifecycle | Shell verbs |
|---|---|---|
| 1 · host to VM | this engine, through `./sc vm` | adopt, status, start, stop, restart, snapshot, bake, reset (`--running` or `--off`), push, pull, exec, capture |
| 2 · VM to VM | the FnB or the upstream instance | `./sc remote` status, exec, push, pull |

Under posture 2 lifecycle and snapshots are someone else''s job: never ask for
a start, reset, or snapshot on a named remote; report the need instead.

## Set up once per fork

The FnB links the targets; each command writes only its own block of the
gitignored `instance.json` and reports whether the broker is up plus the
command that starts it (`./sc vm-broker-up`).

```bash
./sc vm adopt --domain <libvirt-domain> --ssh-user <guest-account>
./sc vm init --domain <libvirt-domain> --snapshot <baseline> \
  --ssh-host <guest-address> --ssh-user <guest-user> --ssh-key-path </abs/host/key>
./sc remote add <name> --host <address> --user <user> --key-path </abs/host/key> [--port 22] [--known-hosts-path </abs/path>]
./sc ts init ...        # Tailscale diagnostics tier; see tailscale_diagnostics
```

`adopt` is the normal path for a Windows guest: the operator runs one bootstrap
line in the guest console, then `adopt` installs a host-held key, provisions the
guest, writes the block and takes the baseline snapshot. It is host-only and
idempotent. `init` is the hand-link path for a guest already prepared by other
means. Either way the key is generated and kept on the host and never enters a
shell (decision #353); `windows_testing` carries the Windows detail.

Remote names match `[a-z0-9][a-z0-9-]{0,31}`. `key_path` is an absolute,
host-owned, mode-0600 file; the broker refuses anything else. Without
`known_hosts_path` the broker pins the host key on first contact into its own
file under `.sc-state/local/remotes/`. `./sc remote list` and
`./sc remote remove <name>` manage the block; no output ever includes key
material.

## Posture 1 session

```bash
./sc vm status                       # broker, domain, SSH readiness, MCP tunnel
./sc vm start                        # only when off; waits for SSH
./sc vm push <local-file> [<dest>]   # scp to the guest; dest defaults under the workspace
./sc vm pull <guest-path> <dest>     # scp back into the repo or .sc-state/local/
./sc vm exec -- <command>            # or --command-file <utf-8 file>
./sc vm capture [--output <path>]    # console screenshot under .sc-state/local/vm-captures
./sc vm reset --off                  # back to the configured baseline, powered off
```

Lifecycle verbs beside those: `stop` (graceful; `--force` is the only route to
`virsh destroy`), `restart`, `snapshot list`, `snapshot create <name>` (allowed
in any state — a running domain gets a live snapshot, and a hypervisor that
refuses one answers `snapshot_live_unsupported`), `snapshot delete <name>` (the
configured baseline is refused; redefine it with `bake` instead), `bake [<name>]`
(graceful shutdown, then a replace-not-stack offline snapshot that becomes the
baseline; `./sc vm-bake` is the host-direct escape hatch that runs the same
operation against libvirt in-process with no broker in the path — host-only,
never in the sandbox, and only when the broker is down; `vm bake` is the
normal route), and `reset [<name>] --off|--running` for a named snapshot.
Exactly one of `--off` and `--running` is required on every reset.
`push` sources and `pull` destinations must sit inside the repo or
`.sc-state/local/`, and never inside `.sc-state/local/vm/` (the host''s key and
host-key pin live there); a pull destination inside `.super-coder/` or `.git/`
is refused too. Add `--json` to any verb for one result object.

Posture 1''s VM is a disposable test box, so the lifecycle verbs above are
genuinely yours to use — snapshot, bake and reset included (decision #372).

Sharing rule: the broker''s mutation lock is the only concurrency control, so
read `status` first, do not start, reset, or snapshot a VM another shell is
visibly using, and finish your own session with `reset --off` while you still
have control. The engine supervises nothing after you stop (decision #101).

## Posture 2 session

```bash
./sc remote status <name>
./sc remote exec <name> -- <command>          # or --command-file <utf-8 file>
./sc remote push <name> <repo-file> <remote-path>
./sc remote pull <name> <remote-path> <local-path>
```

`push` sources resolve inside the repo; `pull` destinations resolve inside the
repo or `.sc-state/local/`. Treat the remote as shared: leave it as you found
it and say what you changed.

## Structured errors

Every verb returns `{ok, operation, error: {code, message}}` on failure. Act on
the code; never repair the transport yourself.

| Code | Meaning | Do |
|---|---|---|
| `broker_unreachable` | vm-broker is not serving | report the printed start command to the FnB |
| `remote_not_found`, `remote_name_invalid` | name undeclared or malformed | check `./sc remote list`; ask the FnB to add it |
| `remote_key_invalid`, `remote_config_invalid` | key path, mode, or fields wrong on the host | report; the FnB owns keys |
| `remote_path_not_allowed` | push source or pull destination outside the allowed roots | move the file inside the repo or `.sc-state/local/` |
| `remote_unreachable`, `remote_exec_failed` | SSH failed or the command exited non-zero | read `stderr`; report, do not retry blindly |
| `snapshot_protected`, `snapshot_name_invalid` | a lifecycle guard tripped | the baseline is redefined with `bake`, never deleted |
| `snapshot_live_unsupported` | libvirt refused a live internal snapshot | `./sc vm stop`, snapshot, then start again |
| `stop_timeout`, `reset_result_unknown` | the final state was not confirmed | run `status`, report it, do not retry blindly |
| `adopt_sandboxed` | `adopt` is host-only; it needs `virsh`, `ssh-keygen`, `scp` and a TTY | ask the FnB to run it on the host |
| `adopt_guest_not_found` | no address resolved from DHCP, ARP or `--ssh-host` | report; the FnB passes `--ssh-host` |
| `adopt_ssh_timeout` | the guest never answered on TCP 22 in the wait window | the bootstrap line has not run in the guest yet |
| `adopt_key_install_failed` | the password-authenticated key install did not take | report; key material is the FnB''s |
| `adopt_harden_failed` | `PasswordAuthentication no` did not take, or sshd did not come back | report; the FnB owns the guest''s sshd |
| `adopt_host_key_changed` | the guest''s host key differs from the pinned one | report the message verbatim: it names the pin file and the `rm` that clears it. Never delete it yourself |
| `adopt_provision_failed`, `adopt_verify_failed` | a provisioning step or a declared check failed | read the named step; fix `.subfloor/winbox.json` or the guest, re-run adopt |
| `adopt_config_invalid` | a flag or the saved block is unusable, or there is no TTY for the password prompt | report; adopt is the FnB''s command |
| `adopt_block_write_failed`, `adopt_baseline_failed` | the block did not save, or the baseline snapshot was not taken | report |
| `adopt_broker_failed` | adopt could not bring the broker up. The `vm` block IS written | the resume is `./sc vm-broker-up` then adopt again; ask the FnB |
| `push_failed`, `pull_failed` | `scp` itself failed | read the output; report, do not retry blindly |
| `pull_source_invalid`, `pull_destination_invalid`, `push_source_invalid` | a transfer path was empty | pass both paths |
| `scp_unsupported` | the host''s `scp` predates OpenSSH 8.7 and rejects `-s` | report; the FnB upgrades the host''s OpenSSH client |
| `bake_config_write_failed` | the snapshot was baked, the block was not updated to name it | report; the snapshot exists, the baseline pointer does not |
| `<operation>_timeout` | the broker call exceeded that verb''s budget (`exec_timeout`, `push_timeout`, `bake_timeout`, …) | the work may still be running — read `status` before retrying |

Never hand-install keys, aliases, or known-hosts entries anywhere, and never
open a raw `ssh` to a target the broker serves — key material stays host-side
(decision #353). If a target needs something the brokers do not give you, stop
and report it.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'self_update',
  'Update this fork''s Subfloor engine in place — fetch + materialize new code + migrations, all memory intact; sound rollback. The shell hands off to its own next boot. Use when a Subfloor update is available.',
  'substrate',
  'sc update',
  0,
  '# self_update — laying a new floor under your own feet

The local shell updates its own substrate — no external rebuild. All state lives
in the DB and engine code is read live each session, so a code-only update
touches no data; a schema change applies as an in-place migration, never a
destructive rebuild. `current_state`, narrative, decisions, flags, seed, and
L&S all carry across. This is succession for the substrate: you handing off to
you.

## When

- An engine update is available and you choose the moment — no external race.
- The running prompt + schema were read at the old boot -> reboot after the
  update; they refresh only on the far side.

## Procedure

1. **Clean tree first.** `git -C <repo> status` -> clean. Commit, PR, or
   discard any prior update''s output BEFORE running again — a fresh `sc update`
   on top of a stranded one stacks two engine bumps into one diff. Glance at
   `current_state` + make it true for now (the snapshot captures it).

2. **Run.** `sc update` — fetches the Subfloor engine from its upstream remote
   (named `super-coder` in existing forks),
   materializes it into the gitignored `.super-coder/` dir (engine = dependency,
   not fork source), pins the new upstream SHA in `.sc-state/engine.ref`
   (prior saved as `engine.ref.prev`), backs up the live DB, applies pending
   migrations in place, syncs the skills catalogue, re-grants common skills,
   maps the repo, re-snapshots the live state.
   - `sc update --no-fetch` = reconcile against the current working tree
     (offline / dev); engine + `engine.ref` unchanged.
   - Missing-remote error -> `git remote add super-coder <subfloor-url>`.

3. **Verify.** `sc verify` — headless boot proof: shells, memory, granted
   skills intact + schema current. Wrong count -> `sc rollback` (below).
   - Then `sc render && sc render-check` before step 5. `sc update` re-renders
     from the live DB, which can skip a change the new engine shipped (e.g. a
     skill body) — only `render-check`''s hermetic rebuild surfaces it. A red
     render-check here = a local mirror to regenerate. Pipeline + guard details:
     `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Commit only the public update.**
   Stage `.sc-state/engine.ref` (the pin), the root `sc` dispatcher if it
   changed, and other deliberately authored public files. Snapshot SQL and
   `_sc` renders remain ignored beneath `.sc-state/local/`; never force-add
   them. `.super-coder/` and `engine.ref.prev` are also gitignored in forks.

6. **Reboot** the session -> boot onto the new floor.

## Rolling back a bad update

`sc rollback` = sound pair-restore. Engine code is read live and a migration
exists because new code expects the new schema — restoring only the DB strands
new code on the old schema, so rollback restores both:

1. backs up the current (post-bad-update) DB first, under its own
   `prerollback` prefix so it is never mistaken for a pre-update restore point
   — rollback is itself reversible. Where that copy is *written* is an ordered,
   fail-closed choice: `$SC_DB_BACKUP_DIR` when set and writable, else
   `~/db_backups/<repo-name>/` (keyed by this fork''s repo dir name — distinct
   from any `db_backups/` dir the fork''s app keeps at its repo root), else the
   gitignored repo-local `.sc-state/db_backups/`. Pruning keeps the five newest
   backups per lifecycle prefix per directory, so classes never evict each
   other;
2. restores the DB from the newest pre-update backup found across *every*
   candidate directory, not just the currently writable one — the writable
   destination can change between update and rollback, and discovery must not
   hide a restore point behind it;
3. re-materializes the engine at `.sc-state/engine.ref.prev` + restores
   `engine.ref`.

Whole-restore, not per-step schema reversal. Only data written between update
and rollback is lost (seconds, in practice). Reboot afterwards; commit the
restored `.sc-state/` if the rolled-back floor should persist.

## The contract you rely on

Every schema change AFTER a fork exists ships as a migration file
(`migrations/NNNN_*.sql`), never an edit to `schema.sql` — a baseline edit
reaches fresh clones but never an existing fork; the migration ledger carries
the delta. Authoring engine changes: structural change -> new migration file,
additive where possible.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'tailscale_diagnostics',
  'Observe declared tailnet hosts through the host ts-broker with sc ts — tailnet status and a fixed read-only diagnostic verb table on readonly_hosts. Opt-in; a readonly_refused answer is a stop, never something to work around.',
  'substrate',
  'sc ts',
  0,
  '# tailscale_diagnostics — look, do not touch

The host''s Tailscale identity stays on the host. The ts-broker runs there and
serves two verbs over a unix socket; `./sc ts` is the client. The fork''s `ts`
block names `allowed_hosts` (unrestricted exec as the configured `ssh_user`)
and `readonly_hosts` (diagnostic verbs only). A host in `readonly_hosts` is
implicitly allowed; a host in neither list is refused.

```bash
./sc ts status                        # tailnet backend, self, peers
./sc ts exec <host> -- <command>      # one policy-checked command
./sc ts init --ssh-user <user> [--allowed-host H]... [--readonly-host H]... [--tailscale-bin <path>]
```

`init` writes only the `ts` block and prints whether the broker is ready and
the command that starts it (`./sc ts-broker-up`). Every result is one JSON
object: `{ok, exit, stdout, stderr}` for `exec`.

## Read-only doctrine

A shell never reaches a read-only host with `tailscale` or `ssh` directly, on
bare metal or otherwise, even when the operator''s own tailnet identity or a
login would let it; the only path to a `readonly_hosts` entry is
`./sc ts exec`, and observation means observation — read state and logs,
report what you saw, and change nothing on that host by any route. The broker
allowlist plus this paragraph is the accepted boundary (decision #354);
device-side enforcement is deferred, not a gap you are free to use.

## What a read-only host accepts

The broker accepts a command on a `readonly_hosts` entry only when its first
tokens match this table, the string contains none of `; & | > < $ \` ( )` or a
newline or carriage return, and `sudo` appears nowhere. The table lives as data
in `ts.py` (`READONLY_COMMANDS`); trailing operands select a unit, container,
or file.

| Verb | Permitted forms |
|---|---|
| `systemctl` | `status`, `is-active`, `list-units`, `list-timers` |
| `journalctl` | any flags except `--vacuum*` and `--rotate` |
| `pm2` | `status`, `list`, `describe`, `logs --nostream` |
| `docker` | `ps`, `logs`, `inspect`, `stats --no-stream`, `images` |
| host facts | `df`, `free`, `uptime`, `ps`, `ss`, `ip`, `nproc`, `hostname`, `whoami`, `id` |
| files | `ls`, `stat`, `cat`, `head`, `tail`, `du` |

Pipes and redirections are refused, so ask for the raw output and filter it on
your side.

## On `readonly_refused`

The broker answers `{ok: false, error: "readonly_refused", token: "<what>"}`
and names the offending token. Stop. Report the host, the exact command, and
the token to the FnB or the assigning shell. Do not rephrase the command to
slip past the table, split it into permitted pieces that add up to a change,
run it from another host, or open your own session to the target. A host that
is only in `allowed_hosts` is unrestricted by the broker, which is why the
fork declares which hosts are which; do not treat that as permission to
mutate production.

Other refusals: `not in allowed_hosts or readonly_hosts` means the fork has
not declared the host — ask the FnB; `broker_unreachable` means the ts-broker
is down — report the printed start command.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
