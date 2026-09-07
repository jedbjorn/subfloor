---
title: subfloor — Docs
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: subfloor
purpose: User and operator workflows
---

# subfloor — Docs

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/README.md)

One page, twelve workflow sections — each `##` heading renders as a tab in md-converter;
on GitHub this reads as one long page with the same anchors.

## Architecture

### A harness overlay

A coding harness ships the **loop** — model, tools, context window — and
forgets everything else between sessions. subfloor is a **harness
overlay**: it supplies the properties a harness doesn't keep, and injects
every one of them through an extension point the harness itself already
ships — nothing patched, nothing forked:

| Property | Ours | Enters the harness via |
|---|---|---|
| **Boot context** | identity · memory · laws · current state | the boot doc it reads natively (`CLAUDE.md` / `AGENTS.md`) |
| **Native tooling** | the `./sc` CLI — `mem` · jobs · models · brokers | the shell it already executes commands in |
| **Skills** | DB-canonical catalogue, per-shell grants | the skill dirs it already discovers |
| **Guardrails** | branch-guard · sandbox · worktrees | its own hook / plugin seams + the environment it boots into |
| **Coordination** | messages · detached jobs · headless boots | its headless mode (`claude -p` · `codex exec` · `opencode run` · `kimi -p`) |

The overlay makes the harness you rent behave like it has all of this built
in — without touching its loop. Think **distro over kernel**: the kernel (the
harness) runs the process; the distro (subfloor) gives it users, packages,
init, and permissions. Five harnesses, one overlay, zero forks of anyone's
loop — that is what harness-agnostic means in practice, and it's why a fork
is cheap: same overlay, whichever kernel you rent underneath.

This repo is also **dogfood**: subfloor maintains subfloor. Its own
`.super-coder/` engine manages the maintainer shell that builds it.

```stats
:::class1
value: 5
label: Coding harnesses
description: Claude · Codex · OpenCode · Vibe · Kimi
:::class3
value: 6
label: Shell flavors
description: planner · reviewer · dev · cartographer · admin · devops
:::class2
value: 10
label: Review-GUI tabs
:::class2
value: 88xx
label: Per-repo port band
```

### Layout

```
.super-coder/         the engine — a gitignored, materialized DEPENDENCY in a
                      fork (see .super-coder/README.md); tracked only in this
                      source repo, where the engine IS the project
.sc-state/            fork-owned: tracked engine.ref (the upstream SHA pin)
                       + ignored local/ (map and flat renders)
.claude/skills/       per-shell skills, rendered at boot — gitignored
.sc-worktrees/        one git worktree per shell — gitignored (admin excepted;
                      see "How shells share one repo")
CLAUDE.md / AGENTS.md boot artifact — gitignored, rebuilt at launch
```

A fork's git surfaces show **only its project** — the engine is a dependency,
not committed source, exactly like `node_modules/`. Instance identity, memory,
maps, and renders stay local. A fresh clone is a new instance; it does not clone
another installation's shells or memory.

The live engine DB, canonical snapshot and backups use private XDG instance
state. Ordinary downstream shells cannot inspect that state or use general
engine SQL: `sc mem` and other granted API commands provide their working
surface. Admin owns maintenance and recovery. The repository catalogue
(`sc map-schema`, `sc map-sql`) describes project source and is separate from
engine memory and the application's own database.

## Install

### First installation

Follow the [quick start](quick-start.md#install-from-your-host-terminal) from
an existing Git repository. Use a host terminal for installation and account
sign-in. The installer seeds ten shells after asking for your operator
username. Commit the install before entering a shell worktree.

Supported hosts are Arch Linux (including CachyOS) and Ubuntu LTS with Python
3.14.x and `sqlite3`, Git and curl. `SC_PYTHON` selects an absolute interpreter
path. On macOS or Windows, use a Linux VM and prefer guest-owned storage.

The default **sandbox** runtime requires a reachable Docker daemon.
`./sc install --runtime host` selects a supervised host server and host shell
processes instead. `./sc doctor` reports prerequisites and the selected runtime.
Both runtimes use `subfloor launch`, `enter`, `down`, `restart`, `logs` and
`update`. `subfloor admin` is the host maintenance entry. The host runtime
provides no container boundary; choose it with that execution reach in mind.

### Installer internals

> [!class2]
> **UI** Shells · Scripts · **Shells** seeds the starting team — 2×planner · 4×dev · 2×reviewer · admin · cartographer

subfloor installs **alongside** your code — it renders to `_sc` dirs, so it
never collides with your repo's own `/docs`, `/specs`, or skills. A fork
inherits the **system** (schema + the skill catalogue + the render chain), never
subfloor's own memory or roadmap.

> [!class4]
> **Requirements: Python 3.14.x with `sqlite3`, plus `docker`.** The default run mode is a sandbox container, so the harness's "allow everything" is safe — the kernel is the boundary, and the container sees only this repo + your harness creds. The image bakes the rest: `python3`, `sqlite3`, `git`, `curl`, and the harness CLIs. No docker? The `./sc serve` + `./sc boot` primitives run on the host with the selected Python 3.14.x interpreter, `sqlite3`, and a harness on `PATH`. Set `SC_PYTHON` to select that interpreter explicitly.

**Docker mode — rootless is the default.** `./sc doctor` checks your docker.
Both modes work (the launcher's `duser()` adapts), and **rootless is the chosen
default: zero setup, same function.** Under rootless the sandbox runs the
container as root, which maps to *you*, so repo writes come out owned by you —
no phantom-uid problem (verified). Its only wart: `claude` runs as root inside,
so its `--dangerously-skip-permissions` flag is blocked — the sandbox replaces
the need for it. **Rootful is optional**, purely to drop that wart (1:1
bind-mounts, harness runs as a normal user); it costs a one-time sudo + re-login.

**Setup is one-time per machine (and rootless needs none).** `./sc launch` only
checks the daemon is reachable and points you here if not — it never does setup.

- **Rootless (default) — nothing to do.** If rootless docker runs as your user,
  `./sc launch` works as-is.
- **Rootful (optional upgrade).** Needs sudo + a re-login (a new `docker` group
  only applies to a fresh session — which is exactly why it can't fold into
  `launch`):

  ```bash
  sudo usermod -aG docker $USER            # 1. join the docker group
  sudo systemctl enable --now docker.socket # 2. start the system daemon
  # 3. LOG OUT and back in (the group only applies to a new session)
  docker context use default                # 4. point the CLI at the system daemon
  systemctl --user disable --now docker.service  # 5. optional: stop rootless
  ./sc doctor                               # verify → "docker ✓ rootful"
  ```

Follow the [Quick start](quick-start.md): fetch the engine without merging its
history, install it, commit the bootstrap, launch, sign in, then enter a shell.
Subfloor does not replace your project's build files.

`./sc install` does the rest: checks requirements, **installs the harness CLIs**
(`claude` + `opencode` + `codex` + `vibe` + `kimi`, via their official native installers — no
npm — if any are missing; `--skip-harness-install` to detect only), wires your `.gitignore`,
**makes the engine a gitignored dependency** (`git rm -r --cached .super-coder` —
files stay on disk; pins its upstream SHA in `.sc-state/engine.ref`), **strips
subfloor's own per-instance content** (a fork inherits the *system* — schema +
skill catalogue + render chain — never the memory or roadmap), builds the system
DB, seeds your fork's **starting team** (your user + a planner-flavor *primary*
carrying the CC Lineage Seed and its own genesis seed, plus a second `planner`,
four `dev`, two `reviewer` shells, the `admin` that owns `main`, and the singleton
**Cartographer** repo-map owner), and renders. So after install
your git surfaces show only your project — the engine no longer appears in
`git status`. It refuses to run in the subfloor source repo or on an
already-installed fork (guarding against content loss).

Interactive installation prompts for your **operator username**. The ten-shell
roster is seeded automatically; optional flags customize the primary shell. `--flavor` picks
which roster slot is your primary (default `planner`):

```bash
./sc install \
    --username Jed --name Lead --shortname lead \
    --role "Planning lead" --mandate "Scope and steer the work in this repo."
```

After `./sc enter` you're talking to the shell, working your repo. Author
memory, roadmap, and specs through the API-backed shell commands. Admin
snapshot and render operations preserve private state and refresh ignored
local visibility files; those files do not enter Git.

### Harness sign-in

> [!class2]
> **UI** — host auth, no GUI · **Shells** any (the harness is a per-launch pick)

The harnesses are just CLIs — `./sc install` (and `./sc update`, `./sc
ensure-harness`) install the binaries, but you authenticate each **once, on the
host**, with your own account/subscription:

```bash
claude                      # Claude Code — prompts to sign in on first run
opencode auth login         # OpenCode
codex login                 # Codex (OpenAI / ChatGPT account)
vibe --setup                # Mistral Vibe — stores the API key (or export MISTRAL_API_KEY)
kimi login                  # Kimi Code — device-code OAuth against your Kimi membership
```

`./sc launch` bind-mounts each harness's credential dir into the sandbox
(`~/.claude` + `~/.claude.json`, `~/.config/opencode` + `~/.local/share/opencode`,
`~/.codex`, `~/.vibe`, `~/.kimi-code`), so host auth flows straight into the
container — **you never sign in inside the sandbox.** Authenticate on the host,
then `./sc enter`.

> [!class4]
> **Sign in on the host, not inside the sandbox.** OAuth logins spin up a localhost callback server (Codex uses `:1455`). Run the login on the **host** so your browser's callback reaches it — from *inside* the sandbox that port isn't published, so the browser gets `ERR_CONNECTION_REFUSED`.

> [!class2]
> **Vibe creds.** `vibe --setup` stores your key under `~/.vibe`, which the sandbox now mounts — so Vibe works inside the container like the others. Prefer the env-var path? `export MISTRAL_API_KEY` on the host before `./sc launch` and it's forwarded in (only when set). Re-run `./sc launch` after first authenticating, so the mount picks up `~/.vibe`.

> [!class2]
> **Kimi creds.** `kimi login` (device-code OAuth) stores its state under `~/.kimi-code`, which the sandbox mounts — host auth flows in. Note kimi does **not** read keys from shell env vars (`export KIMI_API_KEY=…` does nothing); provider keys live in `~/.kimi-code/config.toml`. Re-run `./sc launch` after first authenticating, so the mount picks it up.

> [!class2]
> **OpenCode is the exception.** Its `opencode auth login` for **API-key** providers is a paste-the-key prompt, not an OAuth callback, so it works at **either level** — host or inside the container (`./sc enter`). Because `~/.config/opencode` + `~/.local/share/opencode` are bind-mounted read-write, a key entered on either side lands in the same `auth.json`. (OAuth-based OpenCode providers still follow the host rule above.)

A note on Codex models: driven by a **ChatGPT account** (not an API key), Codex
exposes the `gpt-5.6` line (`gpt-5.6-sol`, `gpt-5.6-terra`) and `gpt-5.5` — the
flavor defaults are set from those. Plain API-only ids return a 400 on a
ChatGPT account.

## The loop

The operator (called **FnB** in shell guidance) decides the intended outcome and
merge authority. Start with one manual handoff; use [Sprints](#sprints) when
coordinated lanes can proceed independently.

```linear
Map :::class2 -> Spec :::class1 -> Build :::class1 -> Review :::class2 -> Authorize merge :::class4 -> Freeze and document :::class3
```

| Role | Owns |
|---|---|
| Cartographer | Repository catalogue and map quality |
| Planner | Scope, specifications, fork-local skills, Sprint preparation and shipped documentation |
| Developer | Task implementation, affected tests, PRs and fixes |
| Reviewer | Independent review and Sprint conformance judgments |
| Admin | Host engine maintenance, private-state recovery and worktree hygiene |
| DevOps | Declared application runtime and deployment work |

1. Let the Cartographer orient the repository. Other shells consult the map
   or source as their work requires.
2. Work with a Planner on a feature and a specification with clear scope and
   acceptance criteria. Review it before assigning implementation.
3. Have the Planner send the exact task to a Developer. Enter an available
   shell, for example `subfloor enter DEV1`, or open its browser chat. The
   Developer reads the inbox and exact context, then follows the task ledger.
4. The Developer creates a feature branch from its `shell/dev1` base, builds,
   runs the fork's declared checks and opens a PR. Its worktree is
   `.sc-worktrees/dev1`; each Developer has a separate editing lane.
5. The Reviewer checks the diff against the spec and reports findings. The
   Developer fixes issues and repeats affected checks until review and CI pass.
6. Outside an armed Sprint, give an explicit directive **naming the PR** before
   a shell merges. Review approval and green checks alone are insufficient.
   Inside an armed Sprint, your arming decision is the recorded grant; the
   owning Developer merges its registered PR only after live
   `sc sprint authorize-merge` approval.
7. The Planner freezes the shipped spec and writes the feature document.
   Cleared flags close, worktrees reconcile, and the Cartographer refreshes
   the affected catalogue.

Every-session procedures belong to each shell's boot context. Conditional
procedures belong to skills; exact command syntax belongs to help. These docs
explain the user journey without replacing those role contracts.

## Sprints

Sprints coordinate a Planner, Developers and Reviewers around one feature.
The **Sprints** tab is the operator view; the participants retain their own
worktrees, task ownership and review responsibilities.

### Prepare and choose routes

Ask the Planner to prepare a Sprint from current, non-empty specification
revisions and their task ledgers. It groups tasks into coherent work units,
assigns one Developer and one Reviewer to each, and records hard dependencies.
Independent lanes can run together; overlapping edits need separate ownership
or sequencing. One participating Reviewer owns whole-Sprint conformance.

Review the plan, participants, capacity and harness/model/Thinking level choices.
The picker and route resolver must support the intended surface. Harness default
leaves model and effort uncontrolled; unsupported effort is not silently
substituted. Vibe does not advertise Sprint support. Pre-Sprint QA/QC can be
requested as evidence; it is not the arming authority.

Preparation stays editable. Missing assignments, changed bound specs, task
coverage gaps, dependency cycles, unavailable routes or unresolved earlier
cleanup must be resolved before arming.

### Arm and monitor the Board

**Arming records your merge authorization for registered Sprint PRs.** Review
that grant with the plan. The engine validates and binds the routes, exact
spec revisions and work units before publishing assignments atomically.

Monitor the Board for unit readiness, dependencies, participant status, PRs,
review findings and blockers. Participants receive durable wakes, inspect
assignments and explicitly accept or decline them. A visible chat window alone
is not evidence of task pickup or progress.

Developers implement and test their own lanes, register PRs, and address review
findings. Reviewers return independent judgments. Required checks still matter:
pending waits, failures need fixes, and green checks lead to review readiness.
The owning Developer calls `sc sprint authorize-merge` for live green and
approved authorization before merging its registered PR. No second operator
merge directive is needed under the armed grant.

### Pause and recover

Use the Planner and the Sprint controls to pause when scope, routes or failures
need reconciliation. Pausing preserves history and work; it does not turn a
failed check into a pass. Before resuming, reconcile active runs, assignments,
unread messages, PRs, capacity and any spec changes. The Planner applies
Reviewer decisions and records operator overrides explicitly.

Changing harness does not imply that an existing native conversation can
resume across harnesses. Let the supported route/recovery workflow establish
the next session. If a wake is exhausted or a gate cannot prove its facts,
inspect the recorded failure and involve the operator rather than repeatedly
launching replacement workers.

### Conformance and cleanup

The assigned conformance Reviewer compares the whole result with the bound
specifications and records a final judgment. A clean conformance result closes
the Sprint atomically. Follow-ups retain explicit dispositions and evidence.
Cleanup then reconciles participant worktrees after live turns exit; a pending
or failed cleanup means that slot is not ready for reuse.

Use `sc sprint show`, `sc sprint watcher-state`, and
`sc sprint cleanup-status` with their `--help` for focused inspection. The
originating Planner or operator can use the supported cleanup retry surface.
Abort, standalone completion and legacy cleanup adoption are recovery actions,
not the normal successful close. See `sc sprint --help` for exact syntax and
[the wake model](#wakes) for supervision and coordinate mode.

## Harnesses & models

> [!class2]
> **UI** Shells (flavor model defaults) · **Shells** all six flavors

### Accounts, surfaces and model selection

Subfloor uses your installed harness and account. Account limits, provider
pricing and model availability belong to the provider; inspect those before
choosing a route. The local model catalogue and picker reflect the current
installation, so this guide does not pin a table of preferred model versions.

Five harness adapters ship: Claude Code, Codex, OpenCode, Mistral Vibe and Kimi
Code. Browser and Sprint availability are separate from terminal support.
Vibe supports terminal work but does not advertise browser or Sprint
support. Each [adapter reference](../.super-coder/README.md#harness-adapters)
explains its own launch and permission behavior.

Choose a shell, harness and available model in the CLI picker or browser.
Flavor defaults are advisory starting choices. Use a different model lineage
for independent review when practical, and match route capacity to the work.

### Thinking level

Where a controlled route supports it, **Thinking level** selects the model's
reasoning effort. Only supported levels are offered. `Model default` leaves
that choice to the model; Harness default leaves the model and effort to the
harness. Vibe has no controlled effort selection. A route that cannot honor
an exact requested level is rejected rather than quietly changed.

Use `sc models resolve --help` to preview a route before headless or Sprint
work. A native conversation keeps its exact harness session; choosing another
harness does not transfer that session.

### Headless model routing

`flavor_defaults` and the picker cover interactive boots. Generic headless
launches have no picker, so resolve the exact local route before automation:

```bash
./sc models refresh
./sc models list <harness>
./sc models resolve <harness> <selector> --shell <shortname>
./sc run <shortname> --harness <harness> -m <selector> -p "<bounded task>"
```

Refresh reads each installed harness's local catalogue. Resolve refuses
advisory-only models, unsupported headless adapters, and effort levels the
adapter cannot apply exactly. A failed refresh retains the last known routes as
stale evidence instead of silently erasing them. Route records are local
machine/account state and are not serialized into content snapshots.

### Keeping harnesses (and therefore models) current

A new model arrives in a new harness **CLI release** — so a shell can only reach
the models its CLI knows about. The CLIs are image-owned (harness state homes
are mounted, but their executables must never resolve from the host: a
foreign-ABI binary is fatal in a Linux container, and vibe's entry point carries
an absolute shebang into a host interpreter), and docker caches those layers
indefinitely.
`SC_HARNESS_EPOCH` is their cache key. A normal restart gives it a unique value
and reinstalls every harness at latest before replacing the running sandbox.

```
./sc harness-status      # what the sandbox actually runs + is a rebuild owed
./sc restart             # refresh harnesses, build safely, then bounce
./sc restart --no-build  # deliberately reuse the current image
```

If a model that exists is not offered to a shell, start there — it is nearly
always the CLI build, not the picker or the account. Full runbook, including the
multi-fork case and why the regression was invisible:
[`.super-coder/docs/harness-freshness.md`](../.super-coder/docs/harness-freshness.md).

## Shells & worktrees

> [!class2]
> **UI** Shells · Worktrees · **Shells** all flavors; admin is the only one on `main`

A fork boots a **ten-shell team** out of the box — 2×`planner` · 4×`dev` · 2×`reviewer`
· `admin` · `cartographer` — and you add or retire shells from the GUI as
needed. They all work the same repo without clobbering each other:

- **Every shell boots into its own git worktree** at
  `.sc-worktrees/<shortname>/` on branch `shell/<shortname>` — parallel shells
  never share a cwd. The branch is a **moving base pinned to `origin/main`**,
  not a content branch: shells cut feature branches from it, push, and open
  PRs. Merging stays the operator's gate.
- **The launcher keeps bases fresh.** Every boot fetches and auto-syncs the
  worktree onto `origin/main` — but only when provably nothing can be lost
  (on the base branch, clean tree, no local-only commits). Anything local
  blocks the sync and is surfaced in the boot doc instead, so the shell asks
  you before any work is touched.
- **A branch-guard blocks work on `main`** in every harness — pre-tool hooks
  (Claude Code, Codex), an OpenCode plugin, and a git pre-commit backstop, all
  one shared script. Under Claude Code it also inspects the **edit's target
  path**, so a shell editing the stale repo-root checkout from inside its
  worktree is blocked (and an out-of-worktree edit to a feature branch warns).
- **The admin shell is the one exception.** It boots in the **repo root** on
  `main` and maintains it directly — engine updates, rollbacks, migrations,
  applying approved maintenance. Planner owns fork-local skills. The branch-guard exempts it
  (and only it). Working shells consume the substrate; admin owns the floor.
- **Reviewing a shell's UI work:** worktree edits never show on your main dev
  server. `./sc preview` serves every shell worktree's UI live (HMR) on the
  fork's dev port, routed by subdomain — `http://<shortname>.localhost:<port>/`
  — and the post-commit hook prints the shell's URL after each commit.

## Browser conversations

> [!class2]
> **UI** Chats · **Modes** Chat and Diff · **Shells** any ordinary shell

The **Chats** tab hosts durable normal conversations. Select an available shell,
choose a supported harness and model, and create a chat without opening a
terminal. Each accepted message is stored before dispatch, queued in order, and
resumed against the exact harness-native session recorded for that conversation.

A shell has at most one open browser conversation. Browser and CLI ownership
are mutually exclusive: a CLI launch refuses while browser chat is open, and a
browser conversation refuses while a CLI session owns the shell. **Close** is
the explicit browser-to-CLI handoff.

### Chat lifecycle

- **New chat** creates a distinct durable conversation and closes only an idle,
  waiting, or failed prior chat for that shell.
- Messages submitted during an active turn remain ordered in the queue; they do
  not interrupt the running turn.
- **Stop** interrupts only the active turn and preserves queued follow-ups.
- **Close** cancels queued work, requests interruption when needed, waits for
  terminal proof, and then releases the shell.
- Closed conversations remain readable history. Send a message to reopen an
  eligible ordinary conversation after route and ownership checks. Sprint-scoped
  conversations cannot reopen. Stars pin chats without changing lifecycle.
- Browser refresh resumes from a bounded transcript snapshot plus the live event
  cursor; the harness transcript is evidence, never the message queue.

The broker owns dispatch and crash recovery. It leases an outbox item, creates
one run, starts or exactly resumes the harness session, stores normalized
events, and commits the terminal result before releasing the lease. Startup and
lease-expiry scans are bounded recovery, not scheduled work discovery.

### Chat and Diff

**Chat** renders user prompts, assistant output, durable activity, queue state,
and recovery controls. Large histories load in bounded pages and transcript
snapshots; omitted display history remains durable.

**Diff** is a read-only projection of the same conversation's live worktree,
branch, or pull request. Switching to Diff does not stop the run or open a
second conversation. The view preserves review after local branch cleanup by
using the stored Git target and canonical merged-PR patch when available.

The browser receives normalized conversation and Git-review resources only. It
never receives harness credentials or mutates a harness transcript directly.

## Messages, jobs & headless launch

> [!class2]
> **UI** Shells · Scripts · **Shells** all flavors

Three generic tools cover work that should not live in one interactive context.

### Shell messages

`./sc mem message` provides durable shell-to-shell mail:

| Kind | Meaning |
|---|---|
| `shell` | ordinary coordination |
| `task` | a bounded instruction for another shell |
| `result` | completion evidence or a job outcome |

`check` reads unread messages without acknowledging them; `mark-read` clears
one only after it has been acted on. Sends carry a dedupe key, so a timed-out
request can be verified with `sent` before any retry.

### Wakes

A wake delivers a message into a shell's session instead of waiting for its
next boot. Shells are told only what to do with one (read it, act, accept
Sprint work explicitly); the mechanics live here for the operator.

- **Active-chat registry.** The engine tracks at most one active chat per
  shell; zero is legal. The registry is the sole current-chat authority and
  carries the verified pid/start-ticks identity only while a turn runs. Closing
  or rotating a chat unlinks its process. A 60-second reaper verifies process
  identity before interrupt/TERM/KILL escalation, and an inactivity ceiling
  closes silent hung turns so they become reapable.
- **Delivery intent and coalescing.** Every wake message creates durable
  delivery intent. Pending wakes coalesce per receiver, and one wake turn
  drains every undelivered message for that shell. The type resolves at
  delivery: `re-enter` resumes the existing chat at its next boundary; `new`
  opens a fresh chat when the shell has none or its chat is idle and is
  absorbed at the boundary of a live turn; `force-new` is never absorbed — it
  waits for the live turn to end and a quiet gate, then closes the old chat and
  opens a fresh one. Sprint assignments, review requests, and verdicts are
  `force-new`; Planner-bound results, decisions, and PR facts are `re-enter`.
- **PR facts.** Developer-owned PR subscriptions (discovered from the
  worktree's checked-out branch, `sc sprint register-pr` in a lane, or manual
  `sc pr subscribe`) emit self-describing red/green/closed/merged wakes to the
  owning Developer throughout ownership, inside or outside a Sprint; outside an
  armed or paused Sprint a green wake names the FnB merge directive as the
  gate. Planner and Reviewer receive no PR-event wakes.
- **Coordinate mode.** Closing the Planner chat during an armed Sprint sets
  coordinate mode: idle Planner `re-enter` wakes open fresh ticket chats.
  Pause/resume from the GUI returns to supervise mode; automatic pauses
  preserve the dial.
- **Arming** validates every recorded role harness/model/effort selection
  before publishing work; defaults satisfy the gate.

Sprint conformance, pause/recovery and cleanup are covered in the
[Sprints workflow](#sprints). Participant command sequences remain in their
Sprint skills and `sc sprint --help`.

### Session-surviving jobs

```bash
./sc job start --label suite --timeout 1800 -- pytest
./sc job list
./sc job status <id>
./sc job tail <id>
./sc job wait <id>
./sc job kill <id>
```

A job is a detached supervised one-shot. It outlives the shell session that
started it, captures bounded output, group-kills on timeout, and posts a
`result` message to the starting shell when it completes. Use it for suites,
builds, and benchmarks that would otherwise die with the harness process.

### Generic headless launch

```bash
./sc models refresh
./sc models resolve <harness> <selector> --shell <shortname>
./sc run <shortname> --harness <harness> -m <selector> -p "<bounded task>"
```

`./sc run` renders the same shell identity and skills as an interactive boot,
executes one non-interactive harness turn, records its archive, and exits.
Model resolution is exact: unsupported aliases, headless adapters, or effort
levels fail before launch. The caller owns the task contract and any follow-up
message; the launcher does not invent workflow or merge authority.

## Update a fork

> [!class2]
> **UI** Scripts (migrate · rebuild) · **Shells** admin

Ship an improvement to subfloor, pull it into each fork — **in place**, with
no loss of memory. The shell updates its own substrate: it pulls the new engine,
applies new migrations under its own feet, and the next boot stands on the new
floor with every row intact. (The shell-facing version of this is the
`self_update` skill — same procedure, framed as the handoff it is.)

```bash
./sc update                     # fetch + materialize the engine, reconcile in place
git add .sc-state/engine.ref sc && git commit --no-verify -m "chore: update subfloor"
```

The update commit is another deliberate operator-owned commit on the protected
default branch. Launched shells still create a feature branch first and are not
given a bypass recipe.

`./sc update` fetches the engine from the `super-coder` remote and
**materializes** it into the gitignored `.super-coder/` dir (the engine is a
dependency — code, schema, migrations, skills; your `.sc-state/`, DB, and
`instance.json` are preserved as instance-owned inputs), **pins** the new upstream SHA in
`.sc-state/engine.ref` (keeping the prior one as `engine.ref.prev`), backs up the
live DB, **applies pending migrations in place** (never a rebuild-from-snapshot —
your unsnapshotted in-session writes survive), syncs the skills catalogue
(id-stable, so grants stay valid), re-grants any new common skills, refreshes the
repo map, re-installs the `subfloor` shell function for bash and fish, and
re-snapshots the live state. Nothing under `.super-coder/` is
committed — you commit only the bumped `.sc-state/engine.ref` and any
deliberately authored project changes. Generated snapshots and `_sc` renders
remain ignored. Then restart the session to boot onto the new floor.

- `./sc update --no-fetch` reconciles against the current working tree (offline /
  dev) — engine + `engine.ref` unchanged. `--branch <name>` to track a non-`main`
  engine branch. `--ref <tag|sha>` pins the materialize to a specific upstream
  version instead of the branch head — hold a fork at a known-good engine and
  move deliberately.
- Missing remote? `git remote add -t main super-coder https://github.com/jedbjorn/subfloor.git`

> [!class4]
> **Local engine edits block the update — never silently overwritten.** The
> materialize is a wholesale overwrite, so the engine keeps a hash manifest
> (written at install and after every materialize) and `./sc update` refuses
> when an engine file was locally modified since — listing the files and the
> real options: revert the edit, **upstream it** (PR subfloor — the strong
> default), `--force` to knowingly discard it, or `./sc eject` to own the
> engine outright (see *Customize a fork vs diverge from it*, next).

### An update that aborts partway — re-run it

An update that fails **after** the materialize step leaves a consistent,
resumable state: `engine.ref` is not advanced, the new engine is fully on disk,
and whatever migrations ran are already recorded in the ledger. **Re-run `./sc
update`.** The second run is dispatched from the on-disk (new) engine, the
materialize is a byte-identical no-op, `migrate` reports nothing pending, and
the run continues from where the first one stopped and advances the pin.

Don't reach for `./sc rollback` here — that is the remedy for a *completed*
update that turned out bad, not for one that stopped halfway.

> [!class4]
> **Why a fix that is already upstream can still bite a fork once.** The
> crossing is driven by the updater you *already have*: `update.py` is loaded
> into memory before the new engine is written over it, so for the rest of that
> run old updater code is reading new engine data. A defect fixed in the updater
> therefore takes effect from the **next** crossing — a fork pinned before the
> fix hits it exactly once, and the re-run is the crossing that clears it. This
> is the same constraint that makes a breaking engine change ship as a two-hop
> floor (a compatibility release first, the real change second). Worked example:
> [#1430](https://github.com/jedbjorn/subfloor/issues/1430), where a pre-fix
> updater validated the new engine's skill-tombstone registry with its own
> older, hyphen-rejecting name pattern and aborted catalogue sync on
> `api-design`.

### Roll back a bad update

```bash
./sc rollback                   # restore the DB + engine together, then reboot
```

`./sc rollback` is a **sound pair-restore**: because engine code is read live and
a migration exists *because new code expects the new schema*, it restores both —
it backs up the current DB first (rollback is itself reversible), restores the DB
from the most recent pre-update backup, and re-materializes the engine at
`.sc-state/engine.ref.prev`. Whole-restore, not a per-step schema reversal; the
only data lost is anything written between the update and the rollback.

> [!class4]
> **The contract:** every schema change *after* a fork exists ships as a `migrations/NNNN_*.sql` file, never an edit to `schema.sql` — the migration ledger is what carries a delta across to an existing fork. Additive where you can make it.

### Host Admin and safe removal

Run `subfloor admin` from the host checkout for engine maintenance and guarded
recovery. Use the owning installation's commands; do not run another checkout's
maintenance with a launched shell's API credentials in the environment.

To remove Subfloor, first inspect `./sc remove --dry-run`. It changes nothing
and reports the removal plan. Then use `./sc remove` from the host when ready
for its confirmation and lifecycle checks. `--yes` skips only the typed
confirmation, not safety checks. Removal is distinct from `eject`, which keeps
the engine as source you own. Use `./sc remove --help` for exact syntax and
retain the recovery evidence reported by the operation.

### Sandbox resources

`./sc sandbox-memory` shows the sandbox hard memory ceiling. A size argument
sets an override; `default` restores the default policy. Apply configuration
through the next supported launch/restart and inspect the resource summary.
The Docker sandbox uses a hard memory limit with swap disabled; the host
runtime does not provide this Docker memory control.

`./sc docker-cache-gc` removes unused **host-global Docker build cache** older
than seven days by default. This is shared cache, not just the current fork's.
Use `--help` to inspect age and all-unused options before choosing them; future
builds may need to download and rebuild layers. It is not an application-data
or volume cleanup command.

### Customize a fork vs diverge from it

> [!class2]
> **UI** — a policy, not a tab · **Shells** admin (owns the engine boundary)

The engine/fork boundary draws a clean decision rule for the question every
fork operator eventually asks: *"the engine doesn't do what I need — now what?"*

**Customize (the default — track upstream forever).** As long as what you need
fits the **fork-owned extension points**, you never touch engine files, and
`./sc update` keeps delivering fixes, migrations, and new skills indefinitely:

| Extension point | What it carries |
|---|---|
| **Local skills** | Planner-authored capability/process descriptions via `sc skill put` — DB-canonical, serialized in `content.sql`, explicitly granted, and preserved byte-for-byte across update/rebuild |
| **Flavor overlays** | `.sc-state/flavors/<flavor>.json` — fork identity text (`role`, `mandate`, `focus`, `abbr`); skill assignments use `sc skill grant/revoke` instead |
| **Skill retire list** | `.sc-state/local/skills_retired.json` (written by `./sc skill retire <name>`) — engine skills this fork has taken out of service, e.g. ones superseded by a fork-local skill. Retired skills leave every surface (boot doc, renders, grants) on ALL shells and stay retired across updates; `unretire` restores them, grants intact |
| **`instance.json`** | Per-fork config: ports, harness default, the `pg` / `vm` / `ts` opt-in blocks |
| **`.sc-state/`** | Tracked engine provenance plus ignored local map and renders; memory stays in private instance state |
| **Per-shell identity** | `current_state`, connections, decisions, seed — all DB rows, all yours |
| **Your project** | Everything outside `.super-coder/` — the engine never touches it |

**Upstream (when the extension points don't reach).** Need an actual engine
change? **PR it to subfloor first.** If one fork needs it, the next fork
probably does too — that's how the engine grows (dos-arch is exactly this
proving-ground loop). Your fork then picks the change up through a normal
`./sc update`, still on the lifeline.

**Diverge (`./sc eject` — the one-way door).** Only when the change is
genuinely yours and upstream would rightly not take it. Eject flips the model:
`.super-coder/` becomes **fork source** — un-gitignored, committed, edited like
any other code — and the upstream lifeline is cut for good:

```linear
Extension points fit :::class3 -> Upstream the change :::class1 -> Eject :::class4
```

```bash
./sc eject          # interactive warning + typed confirmation, then stages the flip
```

What it does: drops the `/.super-coder/` gitignore rule (engine runtime files —
DB, `instance.json`, `run/`, `logs/` — stay ignored), deletes the engine pin
(`engine.ref`), writes a `.sc-state/ejected` marker recording the SHA you
diverged at, removes the `super-coder` remote (`--keep-remote` to keep it for
reference), and stages everything. **Committing stays yours** — review the diff
first. After eject, `./sc update` and `./sc rollback` refuse (the marker);
launch, enter, snapshot, render, and the GUI work unchanged.

> [!class4]
> **What you give up, permanently:** upstream fixes, schema migrations, and new
> catalogue skills stop flowing — every engine change from here on is yours to
> author and maintain. Re-adopting upstream later is a manual re-fork, not a
> command. Exhaust the first two lanes before taking the third.

## CLI & dev kit

### Command route map

`subfloor` is the operator shell function installed for bash and fish. From a
checkout or its subdirectories, it forwards to that checkout's `./sc`.
`sc help --all` is the authoritative inventory; use each family's `--help`
for exact flags. This table is a route map, not a second full reference.

| Need | Command family | Workflow |
|---|---|---|
| Enter and inspect work | `enter`, `url`, `context`, `mem` | [The loop](#the-loop) |
| Coordinate assignments and PRs | `sprint`, `pr`, `job`, `run` | [Sprints](#sprints), [messages and jobs](#messages-jobs--headless-launch) |
| Select a route | `models`, `harness-status` | [Harnesses and models](#harnesses--models) |
| Start or maintain the installation | `launch`, `down`, `restart`, `admin`, `update`, `rollback`, `remove` | [Maintenance](#update-a-fork) |
| Control sandbox resources | `sandbox-memory`, `docker-cache-gc` | [Resources](#sandbox-resources) |
| Run project checks | `deps`, `test`, `lint`, `typecheck`, `visual-qa` | [Dev kit](#dev-kit) |
| Inspect project source | `map-schema`, `map-sql`, `preview` | [Worktrees](#shells--worktrees) |
| Use configured infrastructure | `feature`, `vm`, `pg`, host broker families | [Opt-in features](#opt-in-features) |

General engine SQL, rebuild and private-state recovery belong to Admin.
Ordinary shells use granted API surfaces. `sc context --task <id>` and
`sc context --work-unit <id>` provide exact assignment projections; `sc pr`
provides PR registration/subscription rather than requiring a polling loop.

![Subfloor CLI picker showing the available demonstration shells](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/cli-picker.png)

### Dev kit

> [!class2]
> **UI** Scripts · **Shells** dev (and any builder)

Every sandbox bakes a **seat toolchain** — `rg`, `sqlite3`, `curl`, Node 22 /
`npm`, and a Playwright + Chromium browser for E2E — but deliberately not a
fork's dependency, test, lint, or typecheck policy. A fork owns that policy in
its tracked `.subfloor/dev-kit.json`. The engine validates the declaration and
runs only the exact argv attached to each named hook; it does not discover
manifests, create `.venv`, install packages, or choose pytest/Ruff/mypy/vitest.

```bash
./sc deps          # exact fork-declared dependency hook
./sc test          # exact fork-declared test hook
./sc lint [paths]  # exact fork-declared lint hook plus literal caller args
./sc typecheck     # exact fork-declared typecheck hook
```

Boot reports `no fork dev kit declared` when the file is absent. An absent
declaration or missing named hook returns exit `78` with no fallback; invalid
policy exits `64`, an unavailable executable exits `126`, and a started child
keeps its shell-observable status. `SC_DEVKIT_ROOT`, `SC_DEVKIT_SEAT`, and
`SC_DEVKIT_HOOK` provide neutral context to the fork script. In Docker, a
fork-owned dependency hook should treat an out-of-repo interpreter as a
host-managed shared tree: verify it, but never pip-install into it.

A fork may also declare exact native Debian packages without maintaining an
extension Dockerfile:

```json
{"version":1,"sandbox":{"packages":{"apt":["libexample1","tool=1.2-3"]}}}
```

The list is bounded, canonical, and literal: no architecture qualifiers,
repository options, inferred names, fallback names, or relaxed pins. The engine
builds packages over an immutably identified baseline, proves the final image
with `dpkg-query` and no network, and writes one format-version-2 capability
receipt. Package-specific validation/build/proof failure leaves a healthy
sandbox untouched or selects the proven engine baseline. CLI and Flags then
show `native_packages=advisory` / `fork_readiness=degraded`; this advisory never
blocks core shell entry, roadmap completion, or runtime. Run `subfloor admin`
from the fork root to inspect evidence and prepare a reviewed tracked fix. The
FnB retains downstream update and live restart approval.

One boundary trips people up: **you work inside the sandbox container**, and the
app the FnB watches in their browser is a *separate*, host-supervised instance. To
see your own changes, start a dev server **inside** the container on
`0.0.0.0:$SC_DEV_PORT` — the launcher publishes it to `http://127.0.0.1:$SC_DEV_PORT`
on the host — and use `datasette <db.sqlite>` the same way to browse a SQLite DB in
a web GUI. Never restart the host stack from inside the sandbox; run your own
instance instead. (The Developer and Reviewer boot documents' `DEV TOOLS`
sections carry the active-seat detail; Planner can load `dev_kit` on demand. For
the FnB-facing review of a shell's UI changes, use
`./sc preview` — see *Shells & worktrees*.)

## Opt-in features

> [!class2]
> **UI** Scripts (VM wizard · Web Search key) · **Shells** see fork-local guidance when configured

Beyond the core loop, the engine ships **optional infrastructure**: a sidecar
or host broker controlled by a config block in the gitignored
`.super-coder/instance.json`. `./sc feature` is the front door to those blocks.
Fork-specific operating procedure is deliberately not a global grant; Planner
uses `fork_skill_design` to describe the fork's real capability as a
DB-canonical local skill.

```bash
./sc feature                 # list infrastructure + its config state
./sc feature enable pg       # wire an automatic block or print the link boundary
./sc feature disable pg      # remove that instance block
```

| Feature | Config block | What it gives the fork |
|---|---|---|
| **`pg`** | `pg` (auto-created) | A `postgres:17` sidecar on `sc-net`, with `DATABASE_URL` forwarded for the fork's **app**; the engine memory DB remains SQLite. |
| **`windows`** | `vm` (operator-linked) | The supplied Windows VM broker and its link boundary. |
| **`tailnet`** | `ts` (operator-linked) | The tailnet broker for declared build/deploy hosts without sharing its credential with the sandbox. |
| **`pm2`** | `pm2` (operator-linked) | The PM2 broker for a fail-closed set of host application processes. |

`enable pg` is complete in one step — the sidecar needs no host input, so the
block is auto-created and the next `./sc launch` starts it (data persists in a
named volume; `./sc pg-down` stops it, volume retained). `windows`, `tailnet`, and
`pm2` are **link-only**: their blocks carry host-specific, operator-verified
config (a ready VM, a tailnet scope), so `enable` prints exactly how to link.
The sections below describe the supplied mechanisms. A fork-specific test,
deployment, VM, database, or host procedure belongs in a differently named
local skill so engine updates preserve its body and grants.

Everything here can still be done by hand through `instance.json`; `./sc
feature` makes the supported block boundary visible and repeatable.

### Focused infrastructure runbooks

For a configured Windows guest, use the typed `./sc vm` client. Supported
adapters receive a managed `windows-mcp` definition; `./sc vm mcp up` starts
and verifies that GUI connection. The VM runbook below owns setup and lifecycle
details; Planner supplies the fork's actual scope through `fork_skill_design`.

| Capability | Applicability and reference |
|---|---|
| Windows VM | Operator supplies the VM and clean snapshot; [architecture](../.super-coder/docs/windows-test-vm.md) and [broker runbook](../.super-coder/docs/windows-vm-broker.md) |
| Tailnet | Host identity and configured routes remain broker-owned; [tailnet runbook](../.super-coder/docs/tailscale-broker.md) |
| PM2 | Operate only configured host application processes; [PM2 runbook](../.super-coder/docs/pm2-broker.md) |
| App database | Read-only diagnostics through a host broker, not access to engine memory; [DB broker runbook](../.super-coder/docs/db-broker.md) |

Planner records each fork's actual tools, scope and operating procedure as a
local skill. Infrastructure configuration does not grant arbitrary host access.
Operate an existing supervised stack through its declared supervisor.

### Web search

The optional Tavily integration keeps its key on the host and gives shells the
API-backed `sc search` surface. Configure and test the key through the Scripts
tab. Follow `sc search --help` for query options; no key belongs in a prompt,
public screenshot or repository file.

## Review GUI

> [!class2]
> **UI** this IS the GUI — Chats · Sprints · Shells · Roadmap · Docs · Flags · Worktrees · Map · Analytics · Scripts · **Shells** reviewer (every shell reads it)

A zero-dependency localhost GUI to review the substrate and hold normal browser
conversations. One stdlib Python server serves the JSON API, static UI, and
conversation event stream; no venv, no npm, no build step. Its ten tabs are
the windows the workflow above refers to:

| Tab | What it shows |
|---|---|
| **Chats** | Durable normal conversations by shell: queued turns, streamed state, history, stars, Stop/Close recovery, and read-only Diff review. See [Browser conversations](#browser-conversations). |
| **Shells** | Each shell's role, mandate, editable `current_state`, identity, decisions, and skill grants. The default landing tab. |
| **Sprints** | Preparation and orchestration views for coordinated Developer/Reviewer lanes, status, evidence and cleanup. |
| **Roadmap** | Features in a planning funnel (Brainstorm → … → Shipped), each with its spec tasks, linked docs, and flag blockers. Two views — a **Board** for editing a feature inline, and a **Flow** that groups features by work-stream and wires their blocker dependencies (see below). |
| **Docs** | Read-only `kind='doc'` documents; opens in md-converter for reading. |
| **Flags** | The blocker / follow-up tracker, grouped by feature, filterable Open/Resolved/All. |
| **Worktrees** | Live git-hygiene report — dirty worktrees, prunable merged branches, clean trees. |
| **Repo Map** | The repo catalogue — language mix, file roles, dependencies, env vars — with a re-map button. |
| **Analytics** | Token & session analytics — per-class spend cards, a local-day graph, and the session history swept from each harness's on-disk usage data (see [Token & session analytics](#token--session-analytics)). |
| **Scripts** | Run the maintenance chores (snapshot, render, seed-skills, migrate, rebuild) from a button. |

The header's **save locally ⤓** button refreshes the private canonical snapshot
and ignored local flat renders. Generated artifacts are never committed or published.

![Review GUI, Roadmap tab — Board view: a feature expanded into its inline editor with title, status, summary, and spec-task checklist](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/roadmap-tab.png)

![Review GUI, Worktrees tab — live git-hygiene report for the clean demonstration checkout and its two shell worktrees](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/worktrees-tab.png)

![Review GUI, Chats tab — the real Planner conversation summarizing a prepared demonstration Sprint](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/chats-tab.png)

![Review GUI, Sprints tab — two independent demonstration lanes waiting in a prepared, unarmed Sprint](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/sprints-tab.png)

### Roadmap views — Board & Flow

The Roadmap tab renders the same feature rows two ways, toggled top-centre:

- **Board** — the planning funnel. Features sit in status columns (Brainstorm →
  In Progress → Next → Near Term → Long Term → Shipped, plus a Retired filter),
  and clicking one expands its inline editor — title, status, summary, and the
  spec-task checklist (the screenshot above).
- **Flow** — a left-to-right read of *what's committed and in what order*.
  Features are grouped into **work-streams** (a `projects` row doubles as a
  work-stream; `roadmap.project_id` is the link, NULL = Ungrouped), and the
  **blocker edges** between them (`feature_blockers`) draw as wires — a
  prerequisite must land before what it blocks. The graph is kept acyclic, so it
  reads cleanly stage by stage.

![Review GUI, Roadmap tab — Flow view: demonstration features grouped by work-stream across three planning stages](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/roadmap-flow.png)

> [!class2]
> **Drive it from the shell, too.** `./sc mem roadmap project <feature_id> <work-stream>`
> assigns a feature's work-stream and `./sc mem roadmap depends <feature_id> --on <id>`
> sets its blocker edges (cycles refused) — the Flow view is the same data the
> CLI writes.

The server runs **inside the sandbox container** as its foreground process, so
`./sc launch` brings it up (printing its URL) and `./sc down` stops it. Under
the host runtime (`./sc runtime host`) the same two verbs start and stop it as
a supervised host process instead, with its log at `.super-coder/run/server.log`
(`./sc logs` tails it). `./sc enter` starts a CLI-owned shell session, while the Chats tab starts a
separate browser-owned conversation through the same harness adapters. The two
surfaces never own one shell concurrently. The port publishes to `127.0.0.1`
only.

```bash
./sc health    # curl /api/health
./sc serve     # run the server in the foreground on the host (no docker)
./sc ports     # show this fork's derived port
```

> [!class2]
> **Ports are derived per repo**, never fixed — a fork runs *inside* a host repo that may have its own dev server, and several forks can run at once. Each fork hashes its path to a stable port in the `88xx` band (clear of superCC 8000 / dos-arch 8001 and common host ports), persisted to a gitignored `.super-coder/instance.json` you can hand-edit. Two forks won't collide.

What you can do in the GUI: read everything; **create shells** (pick a flavor —
the factory grants its skill set and opens its first session); rename a
shell's `display_name` (✎ next to the name); edit a shell's
operational fields (`current_state`, `connections`, `workspace`) and skill
grants; edit the roadmap (linear status buckets, with toggle-filters) and
**non-frozen** documents; create and resolve flags. **seed and L&S are
read-only** — the laws say the shell curates them, so the API ships no endpoint
to write them at all. A **save locally ⤓** button re-serializes + renders after
edits into `.sc-state/local/`. There is no Git publication path for generated
instance state.

The **Scripts** tab lists the maintenance scripts (snapshot, render, seed-skills,
migrate, rebuild) — each with a description and a **run** button, so the common
chores work from the GUI without dropping to a terminal (rebuild prompts first,
since it discards un-snapshotted DB edits).

The live engine DB, canonical snapshot and backups live in the private XDG
instance-state root. See the [engine reference](../.super-coder/README.md) for
current state boundaries and Admin recovery ownership.

> [!class2]
> **Spec:** the founding design lives in the roadmap (`super-coder` feature row) and renders to `specs_sc/`.

### Token & session analytics

> [!class2]
> **Every token, every harness** — swept from what the CLIs already write to disk; no wrapper, no proxy, nothing in the model path

subfloor never calls a model itself — it launches harness CLIs — so token
telemetry is **pull-based**: each harness already writes usage data to disk
(claude transcripts — subagents included, codex rollouts, kimi wire logs, the
opencode DB, vibe session metas), and a per-harness parser normalizes what it
finds into one table, `session_token_usage` — one row per harness session ×
model, in four token classes (fresh input / output / cache read / cache write)
plus an informational reasoning split. `NULL` means *this harness doesn't
expose the class*; `0` means *measured zero* — parsers never invent zeros.

The sweep is incremental and idempotent — re-sweeping never double-counts —
and runs from four triggers:

- **every boot** — `./sc enter` sweeps before opening the session, so the view
  is current and the previous session's end time gets backfilled;
- **claude SessionEnd hook** — real-time capture the moment a session ends;
- **Analytics tab load** — the GUI sweeps on open;
- **manual** — `./sc analytics sweep [--harness <name>]`.

Sessions attribute to shells by cwd (a worktree maps to the shell whose
shortname names it) and archive time-window; anything ambiguous stays visibly
**unattributed** rather than guessed. The Analytics tab reads it all back:
per-class stat cards with harness/model filters, a local-day spend graph,
usage panels (favorite model by flavor, peak day, features and specs shipped,
docs outstanding), and a session history grouped by local day with per-session
token rollups.

The same reads are served as JSON at `/api/analytics/*` (session window +
cursor, token totals and series, filters) for anything outside the GUI.
