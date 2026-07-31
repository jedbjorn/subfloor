-- 0145 — reseed current generic guidance after historical skill migrations.
-- Asset-backed system content is authoritative; this forward reseed ensures
-- fresh rebuilds and existing installations converge on the post-removal
-- engine_surgery, memory, messaging, and test_authoring bodies.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'engine_surgery',
  'Procedure for changing the engine you are running — pull/reconcile/restart cadence and their costs, three-artifact engine-skill commits with a hermetic mirror render, migrating the live DB safely, and verifying claims about engine code against the remote rather than a possibly-stale checkout. SOURCE-REPO ONLY; a fork consumes the engine as a pinned dependency and never edits it. Load before touching .super-coder/ in the repo that owns it.',
  'craft',
  NULL,
  0,
  '# engine_surgery — changing the engine you are running

This repo IS the engine. Every shell here runs on the code it edits, reads a DB
it migrates, and is served by a process started from the tree it commits to.
That is surgery on a moving car, and it has one characteristic failure mode:
**a command answers confidently from a target you did not mean.**

**Fork shells never load this.** A fork consumes `.super-coder/` as a gitignored
dependency pinned by `engine.ref` and updates it with `./sc update`; it never
authors engine changes. Granted only in the source repo.

## The four trees, and which one bites

| tree | what it is | who keeps it current |
|---|---|---|
| your worktree | `.sc-worktrees/<shortname>` — your cwd, your branch | you; boot reports it as `sync:` |
| the main checkout | resolves your `./sc`, hosts the live DB, runs the server | admin / the FnB; boot reports it as `floor:` |
| the running process | code already imported — changes only on restart | the FnB |
| `origin/main` | the truth | whoever merged last |

`sc:11-21` derives the engine root from git''s **common dir**, so `./sc` from any
worktree reads the MAIN CHECKOUT. Being current in your own tree tells you
nothing about it. Read the `floor:` line in ACTIVE SESSION.

**Verify any claim about engine code against the remote:**

```
git show origin/main:<path>     # correct
./sc help | grep <thing>        # answers from the main checkout — may be stale
```

Three wrong answers in one session came from skipping that: a help query, a
pending-migration check that came back empty because it globbed a stale
migrations dir and nearly made a reconcile a silent no-op, and dormant PR
watches against a stale running floor.

## EDIT IN YOUR WORKTREE — scripted writes bypass the branch guard

`branch-guard.sh` blocks harness file-edit tools from writing to a
default-branch checkout. **It does not see writes made by a script.** A
`cd /home/j3d1/super-coder && python3 -c "...patch..."` lands on `main`,
uncommitted, with no warning — and your worktree stays clean, so `git status`
there reassures you nothing happened.

Recovery, if you find edits on the wrong tree:

```
git -C <main-checkout> diff > /tmp/x.patch
git apply /tmp/x.patch                                  # in your worktree
git -C <main-checkout> checkout -- <files>
```

Prefer the harness edit tools, which are guarded. If you script an edit, `cd` to
your worktree or use absolute worktree paths, and check `git status` in **both**
trees afterwards.

## Cadence — pull often, restart rarely

| action | cost | fixes |
|---|---|---|
| pull the main checkout | cheap, safe, no session impact | stale reads |
| apply pending migrations | low; back up first | stale DB rows |
| `./sc update` + restart | **restart kills live sessions** | stale running process |

Pull after every merge. Reconcile and restart only at a coordinated idle
boundary: a restart kills working shells, and swapping the floor under active
work is its own hazard. The restart is the FnB''s call.

## Migrating the live DB

The DB you migrate is the one every shell is using and the server has open.

1. **Fast-forward the main checkout first.** Pending-migration checks glob its
   `migrations/` dir, so a stale tree reports nothing pending and the reconcile
   silently does nothing.
2. **Name the DB path explicitly.** `./sc migrate` from a worktree resolves to
   the main checkout''s DB and says so nowhere (issue #569). Prefer
   `python3 .super-coder/scripts/migrate.py <explicit-path>`.
3. **Back up first**, WAL-safe, via SQLite''s online backup rather than a file
   copy:

   ```python
   src = sqlite3.connect(LIVE); dst = sqlite3.connect(BACKUP)
   with dst: src.backup(dst)
   ```

4. **Data-only migrations are safe under a running server** (row updates, no
   DDL). Schema changes want the restart window.
5. **Verify by read-back**, not by the migrate command''s own output.

## Engine skill edits are a three-artifact commit

All three, or CI goes red even when tests pass:

1. the source asset at `.super-coder/assets/skills/<name>/SKILL.md`;
2. a **trailing reseed migration** so existing installations converge — full-body
   upsert, `INSERT … ON CONFLICT(name) DO UPDATE SET`, patterned on the most
   recent `*_reseed_*.sql`. Generate it FROM the asset rather than hand-writing
   it, and store the body exactly as the guards read it
   (`split("---", 2)[2].strip()`) — an unstripped body fails three freshness
   guards;
3. the re-rendered mirror.

**Render the mirror through the guard''s own hermetic path**, never from the live
DB, so it cannot drift from what CI rebuilds:

```python
import render_check as rc, flat
rc._build_tracked_db(db)                  # schema → migrations → content.sql
flat.render_visibility(con, root=rc.ACTIVE_ROOT)
```

In **local artifact mode** the mirror lives under the ignored `.sc-state/local/`
and is not in the diff — the commit is then two artifacts, and `render-check`
still proves the migration.

Run the guard **from your own worktree** — `./sc render-check` resolves to the
main checkout and will judge code you are not committing (flag #47):

```
python3 .super-coder/scripts/render_check.py
```

## Adding a source-repo-only skill

Seeds carry **skills, not grants**: `0001` inserts catalogue rows. Standard
shells resolve one shared `flavor_skills` pack; Bespoke shells resolve their own
`shell_skills` rows. Newly-added common skills join every flavor/Bespoke pack
on update. Opt-ins remain ungranted until assigned.

A skill with **`common: false`** seeds into a fork''s catalogue without entering
any pack. Grant it here by naming one shell:

```
./sc skill grant <name> <shell> [<shell>…]
```

Naming a standard shell changes its whole flavor pack. Naming a Bespoke shell
changes only that shell.

## Stance

- A command reporting success against a target you did not intend is the house
  defect. Name the target; verify the effect.
- Never `SC_ADMIN=1` past a gate to save a step — the publish path is gated on
  purpose.
- Never auto-sync the main checkout from a shell. `sync_worktree` may
  `reset --hard` a shell base; the main checkout is the running server''s tree and
  a reset discards whatever the operator has in flight.
- Assert before you replace; read back after you write. A non-matching string
  replace does nothing and reports success.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'memory',
  'When + how this shell persists memory — current_state (≤300), session narrative, seed (cap 10), L&S (cap 20, ≤500/entry, --supersedes|--new), decisions — all via sc mem, written as it happens, not at close.',
  'substrate',
  NULL,
  1,
  '# memory — write as you go

All memory = DB rows; no flat files. Write at the moment it matters, never in a
close ritual.

Every write goes through `sc mem` -> lands in the live shared engine DB, visible
to all shells on commit. It always targets your own shell (the engine resolves
API identity for you) — never name a shell.

## current_state — rolling status, NOT a log

Present focus + what''s next. Replace in place; NEVER append. **300 chars, hard
— the write is rejected over it.** Rewrite when focus shifts.
```
sc mem state "…"
```

**Point, do not reproduce.** The overrun is never verbosity, it is restatement:
a decision''s reasoning, a spec''s gate, a flag''s argument all pasted inline when
each is a live row one query away. Name what is in flight and carry the id:

```
Feature #29 task #171 gate — see doc #44.
Blocked on flag #200. Next: task #172 after the blocker clears.
```

Not the argument, the ruling, or the rationale — those have rows, and a reader
who needs them runs `sc mem get`. Same principle the boot doc already applies
to decisions: carry the pointer, lazy-load the payload.

## Session narrative — append at inflection points

One row per session, appended progressively. Append a `[HH:MM]` line (time is
stamped for you) when: a decision lands / an approach changes or is rejected /
the FnB says something that shapes the work / an assumption breaks / before a
big change.
```
sc mem narrative "…"
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. Add new entries only; NEVER edit
a body — curate by retiring. The genesis + lineage seed are already yours.
```
sc mem seed "…"            # add
sc mem retire <entry_id>   # curate out (frees a cap slot)
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. An entry is **the RULE** — **≤500 chars,
hard**. The incident that taught it goes in the narrative, where you already
wrote it; if the text opens with an incident timestamp, it is a narrative entry.

**Exactly one of `--supersedes` / `--new` is required.** Your active set is
already rendered in your boot doc, so checking a new rule against it costs no
extra read — and this flag is where that check lands:
```
sc mem lns "…" --supersedes 29,36   # contradicts or refines those — retires them, adds this
sc mem lns "…" --new                # checked against the set, genuinely unrelated
```
`--supersedes` works at 20/20: it frees the slot it uses.

Caps are trigger-enforced (seed 10, L&S 20, L&S body 500, current_state 300) —
a rejected write is the feedback, and the message routes the fix.

Periodic sweep: when `## STATUS` says `L&S: … — curation due`, run the `curate`
skill, then `sc mem curated` to stamp it — even if you retired nothing. Cap 20
is a ceiling never to reach, not a target; curation holds the set near 12–14.

## Decisions — Major only

Record a Major decision (architecture, approach, a path chosen over another).
NEVER rewrite one — supersede via `--parent <decision_id>`. Mirror the headline
into the narrative.
```
sc mem decision "…" --rationale "…" [--parent <id>]
```

Link the why to the what — attach the feature/spec the decision shapes, so the
roadmap carries why it was built that way:
```
sc mem decision "…" --feature <feature_id>   # ties it to a roadmap feature
sc mem decision "…" --doc <document_id>       # ties it to a spec/doc (implies the feature)
```
Both optional — a decision unrelated to any feature stays unlinked. `--doc`
implies `--feature`: pass the doc alone -> feature derived from it. The link
surfaces on `sc mem get decisions <id>`.

## Stance

Write-as-you-go beats batch-at-close: nothing per write, zero at session end.
Curate seed/L&S (revise the set); never rewrite history (decisions, narrative,
seed bodies). Full command reference + table map: the `db_map` skill.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'messaging',
  'Shell-to-shell inbox — send a markdown message to another shell (typed: shell/task/result), check your unread inbox, verify delivery via the sent view, mark messages read. Driven by `sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.',
  'substrate',
  'sc mem message',
  1,
  '# messaging — the shell inbox

Shell-to-shell markdown messages, driven by `sc mem message`. Sender = you;
recipient addressed by `shortname`. Body = markdown, preserved verbatim.
Recipient discovers it on its next boot via the `## STATUS` `Inbox:` count.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | sent | mark-read <id>`

## Message kinds

Every message carries a `kind`, so ordinary mail, delegated tasks, and
completion evidence remain independently filterable:

- `shell` — ordinary shell-to-shell mail (the default; what `send` does
  unless told otherwise).
- `task` — a bounded instruction for another shell.
- `result` — worker → planner completion or transition report.

## check — your unread inbox

```
sc mem message check [N]      # N optional; default 50, max 200
```

Read-only — it does NOT auto-mark-read. Non-`shell` rows show their kind
inline. Surface the body to the operator (reply if warranted — a reply is
itself a `send`), then `mark-read` the inbound in the same turn.

## send — message another shell

```
sc mem message send <to-shortname> "<body>" [--kind shell|task|result]
```

- Multi-word body = one quoted argument; markdown preserved verbatim.
- Examples: `sc mem message send cartographer "map is stale — re-run sc map"`
  · `sc mem message send plan1 "feature 12 task 3 complete (PR #41)" --kind result`
- `cartographer` is a **role alias**: when no shell has that literal
  shortname, it resolves to the fork''s cartographer shell whatever its
  shortname (e.g. `CART1`). Address the map-keeper as `cartographer` — no
  shortname lookup needed. An exact shortname match always wins.
- Unknown / deleted recipient -> `mem: recipient shortname ''<x>'' unknown`;
  empty body -> `mem: body is empty`. Surface either to the operator plainly.
- Sends are idempotent under load: each invocation carries a dedupe key, so
  a timed-out send retries itself and can never write a duplicate. Do NOT
  re-run a timed-out send by hand — the retry already happened; if it still
  died, check `sent` first.

## sent — your outbound view

```
sc mem message sent           # latest 50 you sent, newest first, read receipts
```

Verify delivery after an ambiguous failure (a send that died after its
retries) before ever resending. A row present = delivered; absent = safe
to resend.

## mark-read — clear an inbox item (idempotent)

```
sc mem message mark-read <message_id>
```

Pass the `message_id` that `check` surfaced. Only messages addressed to you
clear — another shell''s message = no-op; re-marking a read message = no-op.

## Stance

- On boot, `Inbox:` non-zero -> run `--message check` and surface the first
  item before continuing.
- No threading: a reply = a new `send`; include `Re: <topic>` in the body if
  it matters.
- `mark-read` only after you have actually acted on the message.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'test_authoring',
  'Principles for stringent pytest tests — tests a realistic bug turns red. Pair with a granted stack-infra testing skill (test_authoring_sqlite / test_authoring_pg / a fork-local one) if the shell has one.',
  'craft',
  NULL,
  0,
  '# test_authoring — stringent pytest tests

Apply when writing a test or reviewing a diff that touches `tests/`.
Pass condition for any test: a realistic bug turns it red. A test no bug
can fail reads as coverage while guarding nothing — sharpen or cut it.

Stack infra (fixture setup, callers, DB access pattern) lives in the granted
stack skill — `test_authoring_sqlite` / `test_authoring_pg` / a fork-local
skill that supersedes this one. Load it alongside. None granted -> this skill
stands alone; do NOT hunt for one the fork doesn''t ship.

## Rules (the floor)

1. **Count + content + negative.** After a count assertion (`written == 1`),
   assert the content (the right row: fields + FKs) + the negative (the row
   that must NOT exist). Wrong body / wrong participant / stray contact must
   turn the test red. `>= 1` is banned where the exact count is knowable.

2. **No config-mirror tautologies.** NEVER assert output equals a constant
   the code under test imports in-process
   (`assert resp == list(THE_SAME_CONSTANT)`) — it catches hardcoding only,
   never a wrong value. Pin the literal expectation in the test, or derive it
   from independent behavior (e.g. the error classes a real
   `classify_error()` emits across sample failures).

3. **Round-trips assert the negative space.** Insert `new` -> assert `new`
   present + prior value gone + sibling fields untouched.
   `assert get() == put_value` alone passes against a stub that echoes input.

4. **Every error / edge branch gets its own case.** Failure path / reject
   path / NULL path / empty-input path -> one test each. `is not None` /
   bare truthiness banned where the exact value is knowable.

5. **Negative tests assert the effect is absent.** Denied / rejected / gated
   path -> assert the underlying action did not happen (no row written,
   resource still unreachable, no egress call) — a 4xx or a
   `permission_denied` string alone does not pass.

6. **Schema changes: test behavior, not `PRAGMA`.** To prove a column
   nullable, insert a NULL row -> assert accepted. The catalog flag can be
   right while a CHECK or trigger still rejects.

7. **Idempotency / migration tests run on a dirty fixture.** Seed the exact
   state the migration cleans (the rows it removes still present) -> run once
   and twice -> assert convergence. Idempotency-on-clean proves almost
   nothing.

8. **Reject silent-empty.** Bad filter / typo''d enum value -> assert 422
   explicitly, never a 200 reading as "nothing found."

9. **Cleanup lives in the fixture, never after the asserts.** A resource
   opened in a test body (connection, file handle, subprocess) and closed
   on the line after the assertions leaks exactly when the test FAILS —
   the worst possible correlation: the suite hangs or exhausts a pool
   only when it is catching bugs (a close-after-assert probe connection
   deadlocked three concurrent pytest runs of one file for four hours in
   a release gate). Open in a fixture (`yield` + teardown /
   `addfinalizer`) or a `with` block; teardown must run on the red path.
   Audit pattern: AST-scan the suite for opens whose close sits after an
   `assert` in the same body.

## Review lens (tests/ diff)

- Read the assertions, not the test name.
- Per `assert`: name a one-line code change that would still pass it. That
  change is a real bug -> the assertion is too weak; demand the fix.
- Count-only / substring-only / `is not None` -> demand the exact value.
- Output compared to a constant the code imports -> flag rule 2.
- Only the success branch tested -> name the missing edge + require it.

## Mechanizable subset (enforce in CI)

Grep-able; wire into a `.github` workflow that fails the build so the floor
holds when this skill isn''t loaded. Point the CI failure message back at
this skill.

- `assert .* (==|!=) (list|set)\(<KNOWN_CONSTANT>\)` — config-mirror shape.
- `assert .* >= 1` / bare `assert .* is not None` in a new test diff —
  demand an exact value.
- Count assertion with no content assertion in the following N lines.

## Never

- Close a resource on the line after an assert — a failing assert skips
  it; fixtures own teardown (rule 9).
- Mock the function under test, then assert the mock returned what you set.
- Assert a key exists without asserting its value.
- Let a count or status code stand in for "the right thing happened."
- Test only the happy path of code that has error branches.
- Ship a test whose assertions no realistic bug could violate.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
