# Sprint 2 — Reviewer decision: **re-enter**

Sprint 2 · feature 38 · governing spec document **#108**, bound revision
`4b1b2cf76b75e38eddcc436c99e85ed9048103a0003c69029e02c60665a626e4`
(bound == current; **zero** mid-Sprint edits). All 6 units terminal
(6 completed, 0 cancelled); PRs 1027, 1028, 1034, 1035, 1042 merged.

- `decision`: **re-enter**
- `reason`: the spec-mandated downstream acceptance gate (#108 §Acceptance)
  did not pass, and the delivered MCP gate has a defect that makes it
  unachievable from any pre-existing session.
- `outcome`: no conformance recorded this episode. The re-entry episode below
  is the failed-pass record. Episode 1 of 3.

## What I verified myself

Integrated `main` at `003f6ae` (tip, all five PRs merged), clean detached
worktree: **1936 passed, 942 subtests, zero failures**. Migration `0180` unique;
`sc vm` dispatches (`dispatch.sh:1275`); adapter matrix as-specced; both skills
carry the canonical supplied-state / start-if-off / end-only-reset workflow.
Tool surface, result contract, relay identity gating, and tunnel idempotency
all match #108.

## Blocking findings

### B1 — Major · the mandated rst-c acceptance gate did not pass

#108 §Acceptance requires the downstream session to "exercise push, complex
exec, capture, **MCP up, one GUI observation/action**, and MCP down", and to
"open the testing application only if absent". WU12's own honest record (work
unit 12, task 328): `mcp up` → exit 1, `mcp_adapter_unsupported`; **no**
first-turn Windows MCP tools; **no** GUI observation or action; Revit never
appeared in `tasklist` after `cmd /c start`; the capture was an all-black
display.

Requirement class: **unimplemented** (verification not performed), not a proven
code defect. Objective items 5 and 6 therefore ship with zero live end-to-end
proof against the linked VM — WU10's live Codex proof covers *injection*
(19 `mcp__windows_mcp__*` tools at first turn), not `mcp up` reaching a
verified endpoint. Re-run is feasible: WU12's final `reset --off` confirmed
snapshot `clean` and `powered_off`, and #108 workflow step 3 covers starting an
off VM without a reset.

### B2 — Major · `SC_HARNESS` ships in this Sprint, so no pre-existing session can pass the MCP gate, and nothing says so

`env["SC_HARNESS"] = harness` was **introduced by PR #1035 in this Sprint**
(`git log -S`, `run.py:1224` and `run.py:1710`). `active_mcp_adapter()`
(`vm.py:1458-1466`) maps an unset value to `state:"unknown", supported:false,
reason:"SC_HARNESS is not set"`, and `run_mcp_operation` (`vm.py:1772-1779`)
hard-refuses `up` on `not adapter["supported"]`.

Consequence: every shell launched before that merge — including the acceptance
shell (shell 6, launched in wave 1; PR 1035 merged 11:04) — gets a hard stop
naming an internal env var with no remedy. `./sc update` inside the session
updated engine files but could not change the already-exported environment of
the running harness process. This is the mechanical cause of B1. Neither skill
distinguishes *unknown* (env unset → relaunch through the engine) from
*unsupported* (kimi/vibe → honest stop); `windows_vm_gui` says only "an
unsupported adapter is an honest stop", which is what the shell correctly did.

Fix boundary: distinguish the two states in `active_mcp_adapter` /
`run_mcp_operation`'s error, and name the recovery in both skills. Do not add a
supervisor or auto-relaunch.

### B3 — Medium · managed MCP injection is unconditional in every fork

`managed_mcp_injection` (`run.py:159-181`) gates only on the adapter
declaration — never on whether the fork has a `vm` block. Post-update, **every**
interactive claude/codex/opencode launch in **every** fork carries a
`windows-mcp` server at `http://127.0.0.1:18000/mcp`, VM or not. WU10's review
empirically proved graceful degradation for **opencode only** (0.74s,
`✗ windows-mcp failed`); claude and codex were verified for argv parsing, not
for a dead endpoint in a no-VM fork.

#108 is **silent** on gating, so this is spec silence with downstream blast
radius, not a silent deviation. Narrow fix: gate on the same `vm.py configured`
check `sc_vm_broker_up` already uses (`dispatch.sh:609`). If FnB judges the
silence deliberate, demote this to a follow-up — that call is theirs, not mine.

## Spec tasks to cut against document #108

1. **Distinguish unknown from unsupported harness MCP state** — Separate
   "SC_HARNESS is not set" (session predates the adapter contract; recovery is a
   relaunch through the engine) from a declared-unsupported adapter in
   `active_mcp_adapter`, the `mcp_adapter_unsupported` error details, and both
   Windows skills. Add regressions for both branches.
2. **Gate managed MCP injection on a linked VM** — Return no injection recipe
   when the fork has no `vm` block, so forks without a Windows VM do not carry a
   permanently-failing MCP server into every launch. Cover linked and unlinked
   forks for all three supported adapters.
3. **Re-run the single rst-c downstream acceptance** — Report-only. From the
   supplied powered-off `clean` state with no opening reset: `status`, `start`,
   open the application if absent, push, complex exec, capture, `mcp up`, **one
   GUI observation/action**, `mcp down`, then the sole final `reset --off`.
   Record first-turn tool evidence and any structured error; claim nothing
   unconfirmed.

## Suggested grouping, waves, routing

- **Wave 1 · unit A (code)** — tasks 1 + 2. Same files (`vm.py`, `run.py`, both
  skill sources, reseed migration), so one lane avoids a conflict.
  Developer **DEV6 (12)** · Reviewer **REV1 (7)**.
- **Wave 2 · unit B (report_only)** — task 3. `depends_on: [unit A]`.
  Developer **DEV4 (6)** · Reviewer **REV2 (8)**.
  **Launch the wave-2 developer shell only after unit A merges.** A shell
  launched earlier has no `SC_HARNESS` and will reproduce B1 exactly.

## Safety impact for FnB

None immediate. No live VM is touched by wave 1; wave 2 performs exactly one
end-of-session `reset --off`, as #108 requires.

## Post-Sprint follow-ups (not blocking, carried to the close report)

- Guest Unicode round-trip unverified end-to-end. #1024's coverage stops at the
  client→broker wire; `do_exec` decodes with `errors="replace"` (`vm.py:598`),
  so WU12's `—`→`-`, `ø`→`o` is guest console codepage, not transport. The code
  satisfies the boundary the spec names ("reaches `exec` unchanged").
- `vm_mcp_relay.py`'s module docstring still teaches `claude mcp add …` (#108
  line 115 forbids it in delivered guidance) and calls the relay
  container-only — untrue on the bare-metal seat. Source-only; not printed.
- Durable task drift: **#318** `in_progress`, **#328** `pending`, though WU7/WU12
  are `completed` and #318's PR merged (319-327 are `done`).
- 18 `liveness.escalated` events, all `silence_episode 1`, all resolved (0 open,
  0 failed wakes, 1 recovered `pr.poll_failed`) — the escalation window fires
  routinely inside normal review turns.

Evidence: `shared/sprints/sprint-2/` (gitignored) — `evidence.json`
(compile-report, limit 50, zero truncation), `spec-108.md`, this decision.
