---
name: issue_reporting
description: Report engine defects upstream — the moment a ./sc command fails or lies, a skill contradicts your reality, the API blocks a documented workflow, or you work around the engine to proceed. File a GitHub issue on super-coder; your repo's app bugs stay in the fork.
category: substrate
common: true
---

# issue_reporting — the backwards flow

The engine improves through what forks report. Every fork runs super-coder
harder than its authors can; when it breaks under you, that observation is
engine input, not fork trivia. Fixed upstream once, the fix reaches every fork
via `./sc update`. Worked around silently, every fork re-derives the same
workaround forever.

**A workaround is a report.** If you had to deviate from a skill's
instructions, wrap a command, or hand-patch state to proceed — you are holding
the exact repro and the exact fix-shaped evidence upstream needs. File it
*while it's in front of you*; don't batch to session end.

## Boundary — engine vs fork

**Upstream (file it):** anything the engine materializes or owns —
`.super-coder/`, the `sc` command and every subcommand, engine skills (this
catalogue), the boot doc render, the sandbox / dev kit, `./sc update` and
migrations, the `_sc` API and `sc mem`.

**Fork (don't):** your repo's app code, fork-local skills (yours — see
`local_skill_management`), host config the operator owns.

Unsure? Ask: *would the same problem hit any other fork?* Yes → upstream.

## Triggers

Every row below is a real engine defect found and reported by a fork shell
doing ordinary work. Recognize yourself in the left column → file.

| You hit | Real case |
|---|---|
| A `./sc` command fails out of the box | `./sc verify` always aborted — its own render step needed `SC_ADMIN` it never set (#227) |
| A command exits green without doing the work | `./sc test` silently fell back to unittest when pytest was missing — spurious failures, green-washed suites (#219) |
| The documented remedy is a closed loop | `./sc lint` said "run `./sc deps` first," but deps skips pip in the sandbox — the tool was unobtainable from inside the box (#246) |
| A skill instructs tools or paths your seat doesn't have | `configure_winbox` drove raw `ssh`/`virsh` — neither exists in the sandbox, which is broker-only by design (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400'd standalone docs that the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404'd advancing its status — the handoff was walled off (#224) |
| Every write suddenly 401s | rebuild didn't re-mint api_keys, so all live shells were locked out until an API bounce (#214) |
| `./sc update` / migrate wedges or half-applies | a migration failed partway, and the retry died on `duplicate column name` — chain wedged (#229); update aborted crossing a commit that deleted an engine file (#209) |
| A structural foot-gun keeps re-biting you | the cwd trap — `cd` to root for `./sc`, then bare git hit the wrong tree and "my edits vanished" (#225) |
| The sandbox can reach something it shouldn't | `do_push` src/dest weren't contained — a sandbox→host escape (#228) |

Stale-guidance reports (skill says X, engine does Y) are as valuable as
crashes — they cost the next shell a session of confusion each.

## Capture — while the failure is on screen

- **engine ref:** `cat .sc-state/engine.ref` — first line of every report.
- **fork + seat:** repo name, which shell (flavor), sandbox or host.
- **what you ran / followed:** the exact command, or skill name + step.
- **expected vs actual:** with the exact output, trimmed to the failing lines.
- **workaround:** what you did instead — or "blocked, none found."

Sanitize before it leaves the fork: no api keys, tokens, secrets, or private
paths in the body. The issue is public.

## File it

```bash
# 1. dedup — someone may have hit it first
gh issue list --repo jedbjorn/super-coder --search "<symptom keywords>" --state all

# 2. file — title: [<fork>] <area>: <one-line symptom>
gh issue create --repo jedbjorn/super-coder \
  --title "[<fork>] <area>: <symptom>" \
  --body "$(cat <<'EOF'
- engine ref: <sha from .sc-state/engine.ref>
- fork/seat: <repo> · <shell flavor> · <sandbox|host>

**Ran / followed:** <command or skill+step>
**Expected:** <what the docs/skill promise>
**Actual:** <exact trimmed output>
**Workaround:** <what unblocked you, or "blocked">
EOF
)"
```

(`jedbjorn/super-coder` is the engine upstream — confirm with
`git remote get-url super-coder` if in doubt.)

If a match exists: comment your engine ref + repro on it instead of filing a
duplicate — a second fork confirming is signal, not noise.

**No `gh` / no network from your seat:** save the identical body as a fork
flag — `sc mem flag open "[Engine] <symptom> | Blocker for: <x>" --name UP-###`
— and message the **admin** shell to relay it upstream (see `messaging`).

## Rules

- One defect per issue. Batch nothing.
- Defects file directly; *enhancement* ideas ("the engine should…") go to your
  FnB first — an observed failure is the bar for filing unasked.
- Filing does not close your loop: if the defect blocks work, also open a fork
  flag linking the issue URL so the blocker is tracked where you work.
