-- 0072 — reseed: issue_reporting URLs after the super-coder → subfloor rename.
--
-- The repo renamed to jedbjorn/subfloor; the skill's issue/PR links move off
-- the redirect. Source asset updated in the same commit; this trailing
-- forward reseed (UPSERT by name; skill_id + grants preserved) carries it to
-- installed forks and fresh builds alike.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'issue_reporting',
  'Report engine defects upstream — the moment a ./sc command fails or lies, a skill contradicts your reality, the API blocks a documented workflow, or you work around the engine to proceed. File a GitHub issue on super-coder; your repo''s app bugs stay in the fork.',
  'substrate',
  NULL,
  1,
  '# issue_reporting — the backwards flow

An engine defect fixed upstream reaches every fork via `./sc update`; worked
around silently, every fork re-derives the workaround. File the issue while
the failure is on screen — NEVER batch to session end.

A workaround IS a report: deviating from a skill''s steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; file it now.

## Boundary — engine vs fork

| Where | What |
|---|---|
| **Upstream — file it** | anything the engine materializes/owns: `.super-coder/`, `sc` + every subcommand, engine skills (this catalogue), the boot doc render, the sandbox / dev kit, `./sc update` + migrations, the `_sc` API + `sc mem` |
| **Fork — don''t** | the repo''s app code, fork-local skills (see `local_skill_management`), operator-owned host config |

Unsure -> "would the same problem hit any other fork?" yes = upstream.

## Triggers

Each row = a real engine defect filed by a fork shell doing ordinary work.
Match the left column -> file.

| You hit | Real case |
|---|---|
| A `./sc` command fails out of the box | `./sc verify` always aborted — its own render step needed `SC_ADMIN` it never set (#227) |
| A command exits green without doing the work | `./sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `./sc lint` said "run `./sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths your seat doesn''t have | `configure_winbox` drove raw `ssh`/`virsh` — neither exists in the broker-only sandbox (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400''d standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404''d advancing its status (#224) |
| Every write suddenly 401s | rebuild didn''t re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `./sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229); update aborted crossing a commit that deleted an engine file (#209) |
| A structural foot-gun keeps re-biting you | the cwd trap — `cd` to root for `./sc`, then bare git hit the wrong tree, "my edits vanished" (#225) |
| The sandbox can reach something it shouldn''t | `do_push` src/dest weren''t contained — sandbox→host escape (#228) |

Stale guidance (skill says X, engine does Y) files the same as a crash.

## Capture — while the failure is on screen

- **engine ref** = `cat .sc-state/engine.ref` — first line of every report
- **fork + seat**: repo name, shell flavor, sandbox/host
- **ran / followed**: the exact command, or skill name + step
- **expected vs actual**: exact output, trimmed to the failing lines
- **workaround**: what unblocked you, or "blocked, none found"

The issue is public: NEVER paste api keys, tokens, secrets, or private paths.

## File it

```bash
# 1. dedup — someone may have hit it first
gh issue list --repo jedbjorn/subfloor --search "<symptom keywords>" --state all

# 2. file — title: [<fork>] <area>: <one-line symptom>
gh issue create --repo jedbjorn/subfloor \
  --title "[<fork>] <area>: <symptom>" \
  --body "$(cat <<''EOF''
- engine ref: <sha from .sc-state/engine.ref>
- fork/seat: <repo> · <shell flavor> · <sandbox|host>

**Ran / followed:** <command or skill+step>
**Expected:** <what the docs/skill promise>
**Actual:** <exact trimmed output>
**Workaround:** <what unblocked you, or "blocked">
EOF
)"
```

`jedbjorn/subfloor` = engine upstream; confirm: `git remote get-url super-coder`.

Dedup hit -> comment your engine ref + repro on the existing issue; do NOT
file a duplicate.

No `gh` / no network from your seat -> save the identical body as a fork flag:
`sc mem flag open "[Engine] <symptom> | Blocker for: <x>" --name UP-###`, then
message the **admin** shell to relay it upstream (see `messaging`).

## Rules

- One defect per issue. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to your FnB first.
- Filing ≠ unblocked: defect blocks work -> also open a fork flag linking the
  issue URL.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
