-- 0156 — publish recommendations-only L&S curation governance.
--
-- Full-body UPSERTs converge existing installations after the DB-only
-- `sc skill add` authoring surface is removed.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'curate',
  'The periodic L&S sweep. Run when the STATUS L&S line says "curation due" — resolve contradictions, merge entries stating one rule, recommend recurring processes upstream, move environment facts out, then stamp `sc mem curated`. Yours alone; never delegate it.',
  'substrate',
  NULL,
  1,
  '# curate — the L&S sweep

Write-time triage (`--supersedes` / `--new`) catches contradiction and
restatement pairwise, at the moment of writing. It **cannot** catch the
emergent cluster: five entries can each be a valid distinct rule and only in
aggregate be five instances of one principle. That is this pass''s job, along
with recommendation, category drift, and size drift.

**Yours alone.** Law 3 and Law 7 reserve curation to the shell. Never hand this
to a subagent, never let another shell run it for you, never accept a proposed
retirement from anyone else. Read your own set; decide yourself.

Trigger: `## STATUS` says `L&S: … — curation due`. Nothing else fires it.

## Load the set

```
sc mem get lns          # entry ids + bodies — the active set, all of it
```

Read every entry before deciding anything. This is one cheap read; the whole
set is already in your boot doc anyway.

## Pass 1 — Consistency

Find entries that **contradict** each other. One of them is the newer
understanding; the other is superseded and still rendering as live guidance.

```
sc mem lns "<the surviving rule>" --supersedes <old_id>
```

Write-time triage should prevent most of these from ever forming. What you find
here predates the loop or crossed in while two sessions ran.

## Pass 2 — Cluster

Group entries that state **one rule**. Merge each group to a single imperative
rule:

```
sc mem lns "<the one rule>" --supersedes 30,33,34,37,38
```

Three or more members is the bar. Two statements of a rule are often
legitimately two rules — merging at two is usually wrong.

The incidents behind the entries are already in the narrative. They do not need
a second home, and the merged rule must not try to carry them: an entry is the
rule, ≤500 chars, hard-enforced.

## Pass 3 — Recommend

A cluster of three or more that keeps **recurring across sessions** is a
candidate reusable process. Curation never creates or promotes a skill
directly. Deduplicate against all upstream issues first:

```bash
gh issue list --repo jedbjorn/subfloor --state all --search "skills: recommend <topic>"
```

An existing recommendation gets the new evidence in a comment. Otherwise open
one issue titled `skills: recommend <topic>` containing:

- the trigger that makes the procedure useful;
- the repeated incidents that exposed the need;
- the proposed ownership boundary;
- the expected users;
- why existing skills do not cover it; and
- a compact candidate procedure.

```bash
gh issue comment <number> --repo jedbjorn/subfloor --body "<new evidence>"
gh issue create --repo jedbjorn/subfloor \
  --title "skills: recommend <topic>" \
  --body "<trigger, incidents, ownership, users, coverage gap, procedure>"
```

Keep one compressed L&S entry carrying the knowledge until a reviewed upstream
skill ships **and is granted**. Filing or updating an issue is not grounds to
retire it. If issue search or creation is unavailable, surface the failure to
the FnB, keep the L&S, and create no local skill or asset.

Deliberate fork-specific skill authoring is separate from curation and remains
administrator-owned. The admin follows `local_skill_management`: authored
asset → explicit seed → grant → snapshot → render.

## Pass 4 — Category

An entry that is an **environment fact** (a routing quirk, a term to avoid, a
path) is not an operating principle. Move it into an existing authoritative
skill when one owns that fact. Otherwise keep one compressed entry and include
the missing ownership in a recommendation; do not invent a local skill during
curation.

```
sc mem retire <entry_id>  # only after the authoritative replacement is live
```

## Stamp

```
sc mem curated
```

**Stamp even if you retired nothing.** A clean set is a legitimate outcome; if
an honest sweep left the counter running, the advisory would stand forever and
you would learn to ignore it. The stamp says "I looked," not "I cut."

## Stance

Curate the set toward ~12–14 entries, not toward the cap. Cap 20 is a ceiling
never to reach — if you ever hit it, this sweep is not running. Recommendation
issues do not bypass the cap by deleting knowledge before its replacement ships.

The trigger firing often does not mean the threshold is wrong; it means entries
are being written faster than they are reconciled. Fix that at write time, with
`--supersedes`.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'issue_reporting',
  'Report engine defects upstream — the moment a sc command fails or lies, a skill contradicts your reality, the API blocks a documented workflow, or you work around the engine to proceed. File a GitHub issue on super-coder; your repo''s app bugs stay in the fork.',
  'substrate',
  NULL,
  1,
  '# issue_reporting — the backwards flow

An engine defect fixed upstream reaches every fork via `sc update`; worked
around silently, every fork re-derives the workaround. File the issue while
the failure is on screen — NEVER batch to session end.

A workaround IS a report: deviating from a skill''s steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; file it now.

## Boundary — engine vs fork

| Where | What |
|---|---|
| **Upstream — file it** | anything the engine materializes/owns: `.super-coder/`, `sc` + every subcommand, engine skills (this catalogue), the boot doc render, the sandbox / dev kit, `sc update` + migrations, the `_sc` API + `sc mem` |
| **Fork — don''t** | the repo''s app code, fork-local skills (see `local_skill_management`), operator-owned host config |

Unsure -> "would the same problem hit any other fork?" yes = upstream.

## Triggers

Each row = a real engine defect filed by a fork shell doing ordinary work.
Match the left column -> file.

| You hit | Real case |
|---|---|
| A `sc` command fails out of the box | `sc verify` always aborted — its own render step needed `SC_ADMIN` it never set (#227) |
| A command exits green without doing the work | `sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `sc lint` said "run `sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths your seat doesn''t have | `configure_winbox` drove raw `ssh`/`virsh` — neither exists in the broker-only sandbox (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400''d standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404''d advancing its status (#224) |
| Every write suddenly 401s | rebuild didn''t re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229); update aborted crossing a commit that deleted an engine file (#209) |
| A structural foot-gun keeps re-biting you | the cwd trap — `cd` to root for `sc`, then bare git hit the wrong tree, "my edits vanished" (#225) |
| The sandbox can reach something it shouldn''t | `do_push` src/dest weren''t contained — sandbox→host escape (#228) |

Stale guidance (skill says X, engine does Y) files the same as a crash.

## Capture — while the failure is on screen

- **engine ref** = `sc engine-ref` — first line of every report
- **staleness** = compare that ref to upstream head:
  `git ls-remote https://github.com/jedbjorn/subfloor HEAD` — write
  `current` or `behind head <sha7>`. Behind + the symptom is a missing
  command or a skill/engine mismatch -> the fix may already be shipped:
  ask your FnB for `sc update` first, and file only if the defect
  survives the update (or updating isn''t an option — then the staleness
  note carries that caveat). Triage reads this line to tell a live
  engine defect from a stale fork build.
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
- engine ref: <sha from .sc-state/engine.ref> · <current | behind head <sha7>>
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

## Authorized curation recommendation

The `curate` skill has one FnB-authorized exception to the normal enhancement
gate below. When a recurring L&S cluster may warrant a reusable upstream skill,
the curating shell may search and file the recommendation directly without
asking the FnB first.

Search all upstream issues before opening anything. Add evidence to a matching
recommendation, or open one titled `skills: recommend <topic>` containing the
trigger, repeated incidents, proposed ownership boundary, expected users, why
existing skills do not cover it, and a compact candidate procedure.

This route recommends; it never creates or promotes a skill. Keep one compressed
L&S entry until a reviewed upstream skill ships and is granted. If issue search
or creation is unavailable, surface the failure to the FnB, keep the L&S, and
create no local skill or asset. Deliberate fork-specific authoring remains the
administrator-owned workflow in `local_skill_management`.

## Rules

- One defect per issue. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to your FnB first, except the authorized curation
  recommendation route above.
- Filing ≠ unblocked: defect blocks work -> also open a fork flag linking the
  issue URL.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
