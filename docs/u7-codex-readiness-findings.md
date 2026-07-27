# U7 — codex hook-chain repair (flag #303) · running findings

Durable notes. Appended as each fact is ESTABLISHED, not when the unit ends.
Rebooted 2026-07-27 after a provider 529 destroyed 26 min of context-only work.

## Unit
- Sprint 84, U7. DEV6. Reviewer REV1. Branch `fix/codex-hook-chain` (base `c66264c`).
- Gate: a codex session shows `provider_ready_at` stamped AND `turn_stop` observed,
  AND H-27's alert stays silent on a healthy codex seat.
- Measured defect: `provider_ready_at` stamped — claude 46/46, kimi 2/2, codex 2/9.
  All 9 codex sessions on cli `0.145.0`. All 7 failures predate the 18:24 floor
  restart; both successes follow it.

## Surface read so far
- `.super-coder/scripts/interface_hooks.py` (331 lines) — vocabulary, CAPABILITIES,
  per-harness installers. Codex installer is `_codex_merge(work_dir)`, writes
  `work_dir/.codex/hooks.json` (PROJECT layer).
- `.super-coder/scripts/interface_hook.py` (190 lines) — the emitter.
- `tests/test_interface_hooks.py` (332 lines) — adapter proofs, no harness binary.
- `.codex/hooks.json` + `.super-coder/adapters/codex/.codex/hooks.json` — rendered
  fork template (branch guard on apply_patch).

## Candidate mechanisms (from flag #303) — status
- [ ] EMIT — does codex fire lifecycle hooks at all
- [ ] TRUST — does it refuse unregistered/unapproved hooks
- [ ] CONFIG DISCOVERY — does it look where we install (`$CODEX_HOME` vs project)

## Established facts

### F1 — the 18:24 restart is NOT the discriminator. CWD is. (CONFIRMED)
Live DB `interface_sessions`, all 12 codex sessions (flag #303 measured 9; three
more have landed since):

| sess | shell | cwd | provider_ready_at |
|---|---|---|---|
| 26 | DEV6 | `.sc-worktrees/dev6` | — |
| 38 | DEV6 | `.sc-worktrees/dev6` | — |
| 40 | PLN3 | `.sc-worktrees/pln3` | — |
| 45 | PLN3 | `.sc-worktrees/pln3` | — |
| 46 | CC | `.sc-worktrees/cc` | — |
| 47 | CC | `.sc-worktrees/cc` | — |
| 48 | CC | `.sc-worktrees/cc` | — |
| **52** | **ADM1** | **`/home/j3d1/super-coder` (REPO ROOT)** | **18:24:52** |
| **54** | **ADM1** | **`/home/j3d1/super-coder` (REPO ROOT)** | **18:39:35** |
| 61 | DEV6 | `.sc-worktrees/dev6` | — |
| 63 | DEV6 | `.sc-worktrees/dev6` | — |
| 65 | DEV6 | `.sc-worktrees/dev6` | — |

Both successes are the **admin-flavor shell (ADM1), which boots at the repo root**
— the only two root-cwd codex sessions in the whole set. Sessions 61/63/65 ran
TODAY, well after the 18:24 floor restart, in a worktree, and all three failed.

**This refutes the flag's stated temporal explanation.** "Every failure predates
the 18:24 restart" was true only because the sample ended there; the correlation
was with shell flavor, not with the floor. Restart is confounded, not causal.
Predicate that actually fits all 12/12: `cwd == repo root` → hooks fire;
`cwd == a git worktree` → hooks never fire.

Confirmed independently in codex's own rollout logs
(`~/.codex/sessions/2026/07/{26,27}/rollout-*.jsonl` `session_meta.payload.cwd`):
successes `cwd=/home/j3d1/super-coder`, failures
`cwd=/home/j3d1/super-coder/.sc-worktrees/dev6`. All `originator=codex-tui`,
all `cli_version=0.145.0` — so CLI version and interactive-vs-exec are BOTH
controlled for and neither is the cause.

### F2 — config discovery WORKS in worktrees; install is not the defect. (CONFIRMED)
`.sc-worktrees/dev6/.codex/hooks.json` is 61 lines and CONTAINS all four merged
lifecycle groups (SessionStart/UserPromptSubmit/Stop/SessionEnd) alongside the
fork's PreToolUse branch guard. Same for `.sc-worktrees/pln3`. So
`interface_hooks._codex_merge()` ran correctly and wrote the right file in the
worktrees that failed. **Hook CONFIG DISCOVERY (the `$CODEX_HOME` hypothesis) is
ruled out as the cause of the worktree failures** — the config is present and
correct at the project layer in exactly the sessions that produced nothing.

Inverted from expectation: the repo ROOT's `.codex/hooks.json` is only 16 lines
today — branch guard ONLY, lifecycle groups absent (a later adapter emit
overwrote them). Yet the root is where hooks fired. So current file content is
not what gates delivery either.

### F3 — project trust is present for the failing worktrees. (CONFIRMED)
`~/.codex/config.toml` holds 93 `trust_level = "trusted"` stanzas, including
`[projects."/home/j3d1/super-coder"]` (root, line 134) AND
`[projects."/home/j3d1/super-coder/.sc-worktrees/dev6"]` (line 233) and
`.../pln3` (line 290). `run.py:trust_codex_worktree()` is doing its job.
**Project-layer trust is NOT the discriminator** — dev6 and pln3 were trusted
and still delivered nothing.

### F4 — per-hook hash approval (`[hooks.state]`) is the sharpest asymmetry. (CONFIRMED as asymmetry; causality not yet proven)
`~/.codex/config.toml` `[hooks.state]` is keyed by
`"<abs-path-to-hooks.json>:<event>:<idx>:<idx>"` → `trusted_hash = "sha256:…"`.
Entries present:
- `/home/j3d1/super-coder/.codex/hooks.json:` **session_start, session_end,
  user_prompt_submit, stop** — the four lifecycle events, at the REPO ROOT.
- `pre_tool_use` entries for root paths of several repos (dos-arch, rst-c,
  super-coder, ami).
- **ZERO `hooks.state` entries for any `.sc-worktrees/*` path — lifecycle or
  otherwise.**

That maps exactly onto F1: the only path with approved lifecycle hashes is the
only cwd that ever stamped `provider_ready_at`.

### F5 — ROOT CAUSE (PROVEN by live probe): codex fires SessionStart LAZILY, at the first user turn — never at session init.

Probe: `/tmp/u7probe`, a throwaway git repo carrying the **exact** hooks.json
`interface_hooks._codex_merge()` writes (generated by calling the real function),
with each emitter command swapped for a marker that appends to `/tmp/u7probe.log`.
Driven through real `codex` TUI in tmux (`originator=codex-tui`, v0.145.0) —
the same binary and launch mode as every session in F1.

Sequence of probe runs, each a controlled step:

1. **Untrusted dir, `--dangerously-bypass-hook-trust`** → codex blocks on
   "Do you trust the contents of this directory?… Trusting the directory allows
   project-local config, **hooks**, and exec policies to load." No fire.
   Confirms `run.py:338-339`'s claim: the bypass flag does NOT substitute for
   directory trust.
2. **Trusted dir + bypass flag** → session starts, banner confirms the flag is
   live (`⚠ --dangerously-bypass-hook-trust is enabled`). **No fire.**
3. **Trusted dir, NO bypass flag** → codex prompts "Hooks need review · 1 hook is
   new or changed." **This is the known-positive control the task demands**: it
   proves the probe's hooks.json IS discovered and parsed at the project layer.
4. **Answered "Trust all and continue"** → codex persisted
   `[hooks.state."/tmp/u7probe/.codex/hooks.json:session_start:0:0"]` +
   `session_end` + `user_prompt_submit` + `stop` — the SAME four keys the real
   repo root carries. It also printed
   `⚠ clamping SessionEnd hook timeout to 3s in /tmp/u7probe/.codex/hooks.json`,
   proving codex read, validated, and registered our four groups. **Still no fire
   at startup**, on that run or on a fresh fully-trusted run after it.
5. **Submitted one real turn** (`say only the word pong` → model answered `pong`).
   `/tmp/u7probe.log` then contained, in this order:
   ```
   FIRED-SessionStart
   FIRED-UserPromptSubmit
   FIRED-Stop
   ```

**All three lifecycle hooks fire — but SessionStart is deferred until the first
user prompt is submitted.** Codex 0.145.0 does not emit SessionStart during
session init. Registration, trust, discovery, enablement, matcher and version are
all fine; the event's TIMING is what the contract gets wrong.

### F6 — this explains the deadlock, and it re-reads the "successes" as the same bug
`interface_broker.py:543-568`: `provider_ready_at` is stamped ONLY by
`event='session_start'` with `source='provider'` — and the same branch is what
moves lifecycle `starting → idle`.

So on a codex seat:
> no human prompt → no SessionStart → `provider_ready_at` NULL → lifecycle stays
> `starting` → never reads `occupied+idle` → wake never arms → every submit
> silently defers → **the planner is never woken.**

That is precisely the reported defect (flag #303, downstream issue #638,
kcsos/sol). It is a **deadlock**: the wake path needs a hook that codex will only
send after somebody does by hand the very thing the wake path exists to do.

**This also corrects F1.** `cwd` is confounded too, not causal. Sessions 52/54
(ADM1, root) stamped at **+45s** and **+23s** after creation — those are not
startup latencies, they are *the FnB typing the first prompt into an interactive
seat*. The predicate that actually fits is **"did a human submit a turn before
the session ended"**, and root-vs-worktree only tracked which seats a human sat
at. Neither the 18:24 restart nor the cwd is the mechanism; both are artefacts of
the same lazy-SessionStart behaviour.

### F7 — in-deployment confirmation: the failing sessions received ZERO harness hooks
`interface_generations.last_hook_seq` joined to the 12 codex sessions:

- **All 10 failures: `last_hook_seq = 1`.** Seq 1 is *by construction* the
  entrypoint's own pre-exec `session_start` (`interface_hook.py:66-95` — the
  counter starts at 1 and the first harness-side hook issues 2). So not one
  harness-side hook ever arrived in any of them — no `prompt_submit`, no
  `turn_stop`, nothing.
- **The 2 successes: `last_hook_seq = 6` (sess 52) and `4` (sess 54)** — several
  provider hooks arrived, consistent with session_start + prompt_submit + stop
  from one or more real turns.

This is exactly what F5 predicts: before the first user turn codex emits
*nothing at all*, so a seat nobody types into produces a perfectly clean seq-1
record. It also rules out a partial-delivery or receive-side-rejection story —
there was nothing to reject.

### F8 — H-27 CALIBRATION (this is what U2/DEV3 needs, and it is not a number)
H-27 proposes to alert when `provider_ready_at` is unset beyond a NAMED
THRESHOLD after session creation. **On codex no such threshold exists.** Readiness
is gated on a human submitting the first turn, so the delay is unbounded and not
a machine latency at all. The only two healthy codex readiness samples in the
deployment are **+45s** and **+23s** from creation, and both of those numbers
measure *how long the FnB took to start typing* — they are not a distribution to
fit, and calibrating against them would be fitting noise.

Consequence, stated plainly for U2: **a purely time-based
`hooks_declared_but_silent` alert cannot stay silent on a healthy idle codex
seat.** An idle-but-fine codex seat is indistinguishable, by that predicate, from
a broken one — it would fire on every codex seat awaiting a wake. That is a
monitor lying, which decision #76 forbids outright, and it is the second half of
my own gate. H-27 must key on *"a completed human turn produced no `turn_stop`"*
(its other clause, which IS sound and IS discriminating) and must NOT key on
"`provider_ready_at` unset N seconds after creation" for harnesses whose
readiness is first-turn-gated.

### F9 — option (C) "observe the codex rollout file instead of the hook" is DEAD
Worth pricing because the engine ALREADY reads codex rollouts
(`activity_readers.py:684-696`, `_codex_marker`), so a readiness signal there
would need no new machinery. It does not work:

- Direct experiment: rollout-file count in `~/.codex/sessions/2026/07/27/` was
  **37 before and 37 after** a full turn-less codex TUI session (launched,
  18s alive, killed). **No rollout file is created until the first turn.**
- `_codex_marker` is in any case doubly first-turn-gated: it matches on
  `type == "turn_context"` records, which by definition do not exist before a
  turn.

So the rollout surface is gated on exactly the same event as the hook. There is
no human-free codex liveness signal on disk today. This strengthens direction (A).

Method note (rejected an earlier misreading): five rollouts at 11:10–11:16 with
`session_meta` written ~0.5s after init initially looked like proof that
turn-less sessions do write rollouts. Their cwds are `/tmp/hk/{plain,b,b/sub,wt,
main/.sc-worktrees/inner}` — probe dirs left by my own process before the 529
killed it (it was testing the same root-vs-worktree hypothesis I re-derived).
The 0.5s gap is consistent with those runs having been given a prompt at launch
(`codex [PROMPT]` is a positional arg that submits immediately), not with a
turn-less session. The controlled 37→37 count above is the measurement I trust,
because it is the only one where I controlled the input.

### F10 — incidental: `codex [PROMPT]` submits a first turn at launch
Relevant only because it makes direction (B) cheaper than I assumed — priming
needs no tmux keystroke race, just a positional argument. It still costs a model
call per codex seat and still puts a synthetic turn in the transcript, so it does
not change my recommendation.

### F11 — CONDITION-2 ENUMERATION: there is a FOURTH description, and it is the frozen spec

PLN2's condition 2 asked me to enumerate every description of codex readiness
BEFORE editing, and to say how I looked if I found more than the three
`interface_hooks.py` claims. I found **eight** descriptions across five files,
and one of them is not a comment.

How I looked (stated so the negative is evidence, not silence):
1. `grep -rn "readiness\|startup_hook\|session_created"` over `*.py *.md *.json
   *.ts *.tsx`, whole repo, node_modules excluded.
2. `grep -rn "real start-readiness\|is NOT provider readiness\|only the
   provider\|wake-armable\|never becomes wake"` — the phrasings that assert the
   entrypoint/provider split without using the word "readiness".
3. `grep -rn -i "session_start\|SessionStart\|provider readiness"` over `docs/`,
   `*.md`, `.super-coder/adapters/`.
4. `.super-coder/ui/app.js` — searched for `provider_ready`/`readiness`: **no
   hits.** The UI renders `lifecycle` only; it holds no readiness description.
5. The DB `documents` surface — spec #20 (the governing frozen spec), grepped
   for the same terms.

The eight:

| # | Site | What it claims |
|---|---|---|
| 1 | `interface_hooks.py` module docstring | codex SessionStart "during session init — pre-prompt" |
| 2 | `interface_hooks.py` readiness comment | two classes only; codex grouped with claude |
| 3 | `CAPABILITIES["codex"]["readiness"]` | `"startup_hook"` |
| 4 | `interface_hooks.py:75-80` SOURCES comment | "provider … session_start is the real readiness signal that moves starting→idle" |
| 5 | `interface_hooks.py:63` EVENTS comment | session_start = "provider readiness" |
| 6 | `interface_broker.py:505-507` `record_hook` docstring | "only the provider's session_start is real start-readiness" |
| 7 | `interface_routes.py:2881` `_hook_callback` docstring | "only the provider's session_start is readiness" |
| 8 | `interface_exec.py:194` | "without the provider session_start the session simply never becomes wake-armable" |

1–3 are the three PLN2 named. 4–8 are the fourth-and-beyond, and they matter
because 6/7/8 are the *engine* path's own statements, not the adapter table's.

**SWEEP 2 (2026-07-27, after REV2 SC-356) — the enumeration above was not
complete, and its negative was not clean.** REV2 found a TENTH description in a
file this unit had already corrected. Recorded as a finding rather than a
footnote, because the sweep's own claim to completeness is what failed:

| # | Site | What it claimed | Disposition |
|---|---|---|---|
| 10 | `interface_hooks.py:352-355` `install()` docstring | on a failed install "the provider session_start simply never arrives — lifecycle stays `starting`, sprint wake can never arm on it (fail closed)" | REWRITTEN. Both clauses were false for codex at `4f2b6f9`: the provider hook never arrives for a codex seat *whether or not* the install succeeded, and the entrypoint claim armed it anyway. The docstring now states the real behaviour and names the report the caller owes (`hooks_installed`). |

**Why sweep 1 missed it.** Its phrase list (items 2 and 3 above) targeted the
entrypoint/provider *split* — "real start-readiness", "is NOT provider
readiness", "only the provider", "wake-armable". Site 10 asserts the same
invariant in the vocabulary of its CONSEQUENCE — "can never arm on it", "stays
`starting`" — which no phrase in the list matched. A sweep keyed on how a claim
is worded misses every restatement of it that changes register.

Sweep 2's list adds the consequence forms: `can never arm`, `never arm`,
`cannot arm`, `arm on it`, `fail closed`, `fail-closed`, `stays \`starting\``,
`stays 'starting'`, `stays starting`, `provider session_start`, `session_start
simply never`, plus the sweep-1 terms — run case-insensitively over `*.py *.md
*.json *.ts *.tsx *.js *.sql *.sh`, whole repo, `node_modules`/`.git`/`spikes/`
excluded.

**Sweep 2's stated negative (all hits triaged, nothing left silent):**

- `.super-coder/scripts/interface_hooks.py:352-355` — site 10. Fixed.
- `tests/test_interface_wake.py:10-12` — the module docstring's flag #49 line
  ("the quiet baseline keys off REAL provider readiness … never the pre-exec
  occupied_at") became incomplete when U7 added `process_ready_at` to the
  baseline. AMENDED in the same commit — an eleventh, found by this sweep.
- `tests/test_interface_api.py:1282` — "wake can never arm on it" describes a
  seat with **no hook adapter at all** (`harness="ed"` → `mandatory_ok` False).
  TRUE, and unrelated to readiness class. No change.
- `tests/test_interface_api.py:1080` — "is NOT readiness: lifecycle stays
  'starting'" describes the entrypoint claim on that test's own seat, which
  runs the default harness (claude), not codex. TRUE of its subject. No change.
- `tests/test_interface_wake_tmux.py:167` — describes the flag #49 e2e's own
  `/bin/sh` fake harness. TRUE of its subject. No change.
- `.super-coder/api/interface_routes.py:1702` — the binding-arm refusal on
  `mandatory_ok` False. TRUE. No change.
- `*.md`, `*.sql`, `*.js`, `*.ts`, `*.tsx`: **zero** hits describing codex
  readiness outside this findings file. This is the same negative F11 item 4/5
  reported for the UI, now measured over the whole doc/seed surface with the
  augmented list. The frozen spec #20 sentence (the ninth) stands as reported —
  ruled by PLN2, not silently edited.

**And the ninth is spec #20 itself — frozen, and it contradicts direction (A)
in terms:**

> "A provider event that fires before its interactive prompt is ready does not
> satisfy start-ready; **without a later native readiness signal that harness
> cannot arm sprint wake.**"

Codex has no later native readiness signal that arrives unbidden (F5, F9), so
that sentence forbids arming a codex seat at all. (A) arms it on entrypoint
evidence. That is a real departure from frozen spec text and it is a planner's
call, not mine — reported to PLN2 rather than silently overridden.

**But the sentence is already-violated text, not a live constraint.** Claude's
`readiness` is `"startup_hook"`, which the code's own comment defines as
"fires during startup, **before the interactive prompt is proven painted**" —
precisely the class the spec says does not satisfy start-ready. The next
sentence of that same comment states the deployed answer outright: *"Neither CLI
offers a later native prompt-ready signal; the wake gate's quiet debounce +
submit-hook fence absorb the residual window."* Claude arms today (46/46
stamped) on exactly that reasoning. So the engine adopted "weak proof + debounce
+ fence" for claude long ago, in written tension with the spec sentence.

(A) is therefore not a new exception — it extends an already-deployed principle
to the one harness whose weak proof arrives from the entrypoint instead of from
a native hook. Recommendation to PLN2: amend spec #20's sentence, or record
decision #98 as superseding it. I have built (A) as ruled and flagged the text.

### F12 — probe process disposition (PLN2 msg #3213 item 1)
pid 66150 (`codex`, cwd `/tmp/u7probe`, started 11:38:24) — **killed, not
reused.** Two reasons, both disqualifying: (a) it is post-first-turn, so its
`SessionStart` has already fired and it can no longer exhibit the turn-less
state that is the whole object of study; (b) it carries no engine hook
credentials (`SC_INTERFACE_HOOK_TOKEN`/shell/generation), so it cannot exercise
the entrypoint → broker path the fix changes. Verification needs an
Interface-managed seat, which is what I am requesting from PLN2.
`~/.codex/config.toml` was re-checked: zero `u7probe` references remain.

### F13 — NEGATIVE CONTROL on a live Interface-managed codex seat (OBSERVED)

PLN2 ran my invocation verbatim from the main checkout at 12:40:35 — so on
**pre-fix code**, which is what makes this a negative control rather than a
verification. Nothing was typed into it, no writer attached.

    ./sc interface start ADM1 --harness codex --json

Readings from the live DB (`interface_sessions` 68, shell 14 / generation 8),
observed, not predicted:

| field | value |
|---|---|
| `harness` / `cli_version` | `codex` / `codex-cli 0.145.0` |
| `created_at` / `occupied_at` | `12:40:35` / `12:40:35` |
| `provider_ready_at` | **NULL** — for the seat's entire 4m40s life |
| `last_hook_seq` | **1** |
| `lifecycle` | never `idle`; `starting` → `ended` |
| `end_reason` / `ended_at` | `operator_end` / `12:45:15` |
| `pane_pid` / `harness_pid` | 138236 / 138236 — the process was alive |

`last_hook_seq = 1` is decisive: seq 1 is *by construction* the entrypoint's own
pre-exec claim (`interface_hook.py` starts the counter at 1; the first
harness-side hook issues 2). So across 4m40s of a live, trusted, hooks-installed
codex process, **not one provider hook arrived**. The seat was promoted to
`occupied` (occupied_at stamped) and then sat in `starting` until it was ended.

Pre-fix, only a provider `session_start` or a `turn_stop` can move
`starting → idle`, and seq 1 proves neither arrived — so the seat could not have
armed a wake at any point in its life. That is flag #303's deadlock, reproduced
under engine control on a real Interface-managed seat, which is what my `/tmp`
probe could not do.

Note for accuracy, not interpretation: PLN2 said they would leave the seat up
until I released it, and it ended at 12:45:15 with `end_reason=operator_end`. I
did not end it and do not know who did. It does not affect the reading — every
value above was already fixed by then, and the row is durable.

### F14 — dispositions of the ruled items (PLN2 #3222)
- **Ninth description (spec #20's frozen sentence): decision #100.** The frozen
  text stays — a frozen doc is the permanent ship-time record and is never
  edited — and #100 (citing #98/#99) supersedes it. Close-time conformance reads
  spec #20 and #100 together. My reading was ratified: the sentence was already
  dead in deployment, killed by claude's own `startup_hook` class arming
  pre-paint with the debounce + fence absorbing the window.
- **Migration number: 0108 → 0113.** The sprint reserved 0108-0109 for U3,
  0110-0111 for U4, 0112 for U5. My read-back of `origin/main` showed 0107 as
  head and 0108 free, which was true of main and useless: main cannot show
  in-flight numbers on unmerged branches. The read-back rule gives you the floor;
  the reservation gives you your slot, and I used one of the two. Worth
  recording as PLN2 asked: the same collision fired **three ways in one hour**
  (U3, me, and the FnB's own PR #660 from outside the sprint), which is evidence
  that migration allocation is a table in a doc plus discipline and nothing
  structural. Not a fix for this unit.
- **Positive verification: a sprint-close acceptance gate, not a U7 merge gate.**
  U7's merge gate is suite green + renumbered migration + negative control
  recorded + review clean. The positive half needs merge → reconcile → restart
  and runs in the FnB's restart window before sprint close.

### F15 — the promotion needed a SECOND operand: the install actually landing (REV2 SC-354)

REV2 executed the hole at `4f2b6f9`: corrupt an existing `.codex/hooks.json` →
`_codex_merge` returns False → `install()` reports `installed=False` → **but
`capability()` is a static version lookup and still reads `mandatory_ok=True`**
→ the entrypoint claim promoted the seat and the wake gate submitted into it.
A seat with zero lifecycle hooks: no `prompt_submit` fence, no `turn_stop`.
Pre-fix that configuration was fail-CLOSED (nothing promoted it at all), so U7
had turned a fail-closed gate fail-OPEN on the exact surface this sprint is
hardening.

**The choice, stated.** Two fixes were available: couple `installed` into the
promotion, or persist the install outcome in a new column the gate reads. I took
the first. The promotion is a ONE-SHOT decision made at the moment the claim
arrives, and the claim is the only actor that knows what it installed — so the
operand belongs on the claim, not in a row a later reader would have to
re-derive. A column would need a second migration to record a value with exactly
one consumer, at one instant, and would still be written from the same claim.
Withholding the promotion also needs no new gate logic: `lifecycle` stays
`starting`, and every downstream refusal (wake ingress → batch → submit) already
keys off that.

Path: `interface_exec` posts `hooks_installed` on the entrypoint claim →
`_hook_callback` whitelists and forwards it (`is True`, so a missing or
non-boolean field withholds rather than grants) → `record_hook` requires it
alongside the `first_turn_gated` class. `process_ready_at` is still stamped
unconditionally: the process IS up, that stamp was never the thing at fault.

Version skew is deliberate and safe in the one direction it can occur: an
interface_exec predating this commit sends no field, so a codex seat it launches
is not promoted — the flag #303 deadlock, which is the status quo, never the new
fail-open.

Proofs (all red-first against the unfixed predicate):
- `test_a_failed_hook_install_neither_promotes_nor_arms` — chains the REAL
  installer over a corrupt `.codex/hooks.json` rather than passing a literal
  `False`, asserts `mandatory_ok` is still True (the gap is the premise), then
  proves the seat stays `starting`, the composer uncertified, and the gate
  refuses. With the operand removed it fails on 5 detectors and the gate writes
  50 bytes into a hook-less pane — REV2's execution, reproduced.
- `test_hook_install_report_crosses_the_route` — both values over the route with
  one body shape. Probed twice: un-whitelisting the field reds both subtests
  (422), hardcoding the forward to `True` reds the False subtest alone.
- `test_session_start_post_contract` / `test_hook_install_argv_is_appended` —
  the claim reports False on a refused install and True on a successful one, so
  the report tracks the installer rather than a constant.

## Mechanism verdict (all three candidates resolved)
- **CONFIG DISCOVERY — RULED OUT.** F2 + probe step 3/4: the project-layer file is
  found, parsed, validated and registered, in worktrees and in probes alike.
  `$CODEX_HOME` is not consulted for hook config; the project layer is correct.
- **TRUST — RULED OUT as the cause.** F3 (worktrees are trusted) + probe step 2
  (bypass flag live, still silent) + step 4 (fully persisted trust, still silent
  at startup). Trust gates hooks, but every failing session had it satisfied.
- **EMIT — CONFIRMED, with a precise shape.** Codex emits all four mandatory
  events, but `session_start` arrives at first-prompt, not at init. The engine's
  `CAPABILITIES["codex"]["readiness"] = "startup_hook"` and its docstring claim
  ("SessionStart (during session init — pre-prompt)") are **factually wrong for
  0.145.0** — a third readiness class exists that the table cannot express.

## Working diagnosis (superseded by F5/F6 — kept for provenance)
Mechanism is **hook TRUST**, at the per-hook-hash layer, not EMIT and not CONFIG
DISCOVERY. Codex 0.145.0 appears to refuse to run a hook whose
`(hooks.json path, event, index)` has no matching `trusted_hash`. The root
accumulated those approvals; no worktree ever has.

Open contradiction to resolve: `adapters/codex/adapter.json:3` launches
`["codex", "--dangerously-bypass-hook-trust"]`, and `run.py:1168` puts that
adapter `launch` list at the head of `plan.argv`, so the Interface path DOES
appear to pass the bypass flag. If the flag worked as `run.py:338-339` claims
("only skips per-hook hash review"), missing `trusted_hash` should not matter.
Either the flag does not reach the process, or it does not do what the docstring
says. **Resolving that contradiction is the next action** and it decides the fix.

## Ruled out
- CLI version / `capability()` mandatory_ok — all 12 sessions carry
  `cli_version = "codex-cli 0.145.0"`, parses to (0,145,0) ≥ min_version
  (0,145,0), so `mandatory_ok` passes for every one. Not the cause, and the
  discrimination the task asks for is available: version-parse failure and
  genuine incapability are distinguishable here because the version string is
  present and well-formed in all 12 rows.
- `codex exec` (headless runs no hooks) — all 12 are `originator=codex-tui`.
- Config discovery / `$CODEX_HOME` for the lifecycle install — see F2.

## Open questions
- Does `--dangerously-bypass-hook-trust` actually reach the launched codex
  process on the Interface path, and does it bypass hash review in 0.145.0?
- Who wrote the root's four lifecycle `trusted_hash` entries, and when?
- `SC_ENGINE_DIR` is used unguarded in the lifecycle hook command
  (`python3 "$SC_ENGINE_DIR/scripts/interface_hook.py" … || true`) while the
  fork's branch-guard hook in the SAME file uses a
  `${SC_ENGINE_DIR:-<git-common-dir derivation>}` fallback. If that var is unset
  or wrong in the codex env, the lifecycle hook fails silently via `|| true`.
  Asymmetry is suspicious and cheap to check — verify before ruling in/out.

