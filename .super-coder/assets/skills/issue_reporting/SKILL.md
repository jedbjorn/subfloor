---
name: issue_reporting
description: Route engine defects the maintainer way — a subfloor defect is yours to triage and fix in ~/Repos/subfloor (or file on its tracker as backlog); a HOME-substrate defect becomes a flag for the FnB, never an in-place fix. Fires the moment a command fails or lies, a skill contradicts reality, or you work around anything to proceed.
category: substrate
common: true
---

# issue_reporting — defects land where they're fixed

You maintain subfloor: there is no upstream above you to report engine defects
to. A defect either lands in **your backlog** (subfloor) or in **the FnB's
hands** (home substrate). Route it while the failure is on screen — NEVER
batch to session end.

A workaround IS a signal: deviating from a skill's steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; route it now.

## Boundary — whose defect is it

| Where it lives | What you do |
|---|---|
| **subfloor engine** (`~/Repos/subfloor`: its `sc` + subcommands, `.super-coder/` code, migrations, adapters, boot render, engine skills, `sc mem` API) | You are the maintainer. In current scope -> fix it in subfloor (`git` skill flow). Out of scope -> file it on the tracker (below) so it survives your session. |
| **HOME substrate** (the engine your cwd runs on: its boot doc, its `sc mem`, its launcher) | NOT your work surface. Open a flag (`sc mem flag open "[Engine] <symptom> | Blocker for: <x>"`) + surface to the FnB. NEVER fix in place — home-engine edits are FnB-gated. |
| **Fork reports** (issues filed on subfloor by installed forks: dos-arch, md-converter, ami, rst-c) | Your intake queue — triage like your own findings. |

Unsure which install misbehaved -> check where the failing command ran:
your cwd = home substrate; `~/Repos/subfloor` or a fork = subfloor engine.

## Triggers

Each row = a real engine-defect shape (filed by fork shells doing ordinary
work). Match the left column -> route it.

| You hit | Real case |
|---|---|
| A `./sc` command fails out of the box | `./sc verify` always aborted — its own render step needed `SC_ADMIN` it never set (#227) |
| A command exits green without doing the work | `./sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `./sc lint` said "run `./sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths the seat doesn't have | `configure_winbox` drove raw `ssh`/`virsh` — neither exists in the broker-only sandbox (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400'd standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404'd advancing its status (#224) |
| Every write suddenly 401s | rebuild didn't re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `./sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229) |
| A structural foot-gun keeps re-biting | the cwd trap — bare git resolving to the wrong tree, "my edits vanished" (#225) |

Stale guidance (skill says X, engine does Y) routes the same as a crash.

## Capture — while the failure is on screen

- **where**: which install (subfloor / fork name / home), shell, host seat
- **ran / followed**: the exact command, or skill name + step
- **expected vs actual**: exact output, trimmed to the failing lines
- **workaround**: what unblocked you, or "blocked, none found"

The tracker is public: NEVER paste api keys, tokens, secrets, or private paths.

## Backlog it (subfloor defects out of current scope)

```bash
# 1. dedup — a fork may have hit it first
gh --repo jedbjorn/subfloor issue list --search "<symptom keywords>" --state all

# 2. file — title: <area>: <one-line symptom>
gh --repo jedbjorn/subfloor issue create \
  --title "<area>: <symptom>" \
  --body "<capture block above>"
```

Dedup hit -> comment your repro on the existing issue; do NOT file a duplicate.

## Rules

- One defect per issue/flag. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to the FnB first.
- Filing ≠ unblocked: defect blocks current work -> also open a flag linking
  the issue URL.
