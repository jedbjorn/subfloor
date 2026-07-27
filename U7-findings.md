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

