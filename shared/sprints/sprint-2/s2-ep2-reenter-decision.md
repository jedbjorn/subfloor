# Sprint 2 — Reviewer decision: **re-enter** (conformance not recorded)

`decision`: **re-enter** · Sprint 2 · feature 38 · governing spec document
**#108**, bound revision `4b1b2cf7…a626e4`. Packet confirms `bound ==
current` and **zero** mid-Sprint edits. Re-entry episode **2 of 3**.

Reviewer: REV3 (shell 13, participant 16). Evidence packet compiled by me:
`shared/sprints/sprint-2/evidence.json` (limit 50, zero truncation).

## Durable state

9 units terminal, 9 completed, 0 cancelled. 7 PRs merged into `main` @
`f9dbc9c`: #1027(WU7) #1028(WU8) #1034(WU9) #1035(WU10) #1042(WU11)
#1046(WU13) #1048(WU15); WU12 and WU14 were report-only. No open actionable
messages, no open liveness expectations, no failed wakes, no pauses.

Integrated `main` is green under me, not merely at each unit head: full suite
from a clean detached worktree at `f9dbc9c` — **1939 passed, 945 subtests, 0
failures**. Live CI rollup on `main` is green (tests, render-check, CodeQL).
Migration `0182` is the trailing reseed and both embedded skill bodies are
byte-identical to the assets apart from the rendered frontmatter; I diffed
them. No Sprint artifact was committed — `shared/` is gitignored.

## Episode-1 findings — cleared

- **Medium 3 (guest default shell undocumented)** — cleared. `windows_devkit`
  now names `cmd.exe` and the explicit `powershell -NoProfile -Command` form;
  mirrored into `0182` and pinned by `tests/test_windows_vm_skills.py`.
- **Medium 4 (stdout Unicode fidelity)** — cleared. The skill states the
  transliteration limit and the base64 escape hatch; pinned by the same test.
  Input transport was already pinned (`test_vm_guest_clients.py:22`).

I re-verified the delivered surface directly rather than from prose: all seven
`sc vm` subcommands exist; `reset` requires `--off`; `exec` refuses both input
forms together (`exec_arguments_invalid`); `capture --output /tmp/…` is refused
with `capture_output_not_allowed` naming the allowed root. Objectives 1–4, 7
and 8 are `as-specced`, corroborated by live WU12/WU14 command records.

## Findings

**Major 1 — the §Acceptance downstream `rst-c` gate is still unmet, now after
two attempts.** Step 3 requires push, complex exec, capture, **MCP up**, **one
GUI observation/action**, and MCP down. In both WU12 and WU14, `./sc vm mcp up`
exited 1 with `mcp_adapter_unsupported` / "SC_HARNESS is not set", so no GUI
observation or action occurred. Steps 1, 4 and 5 passed both times, and both
reports honestly marked every unconfirmed step unconfirmed — I credit that.

Episode 1 asked the Dev to "relaunch the rst-c Codex shell". A Dev shell cannot
relaunch its own session, so WU14 reproduced the identical negative. **That was
an instruction defect, not a code defect, and it is fixable.** WU10's live proof
(`shared/sprints/sprint-2/wu10-live-proof.md`, credited in REV1's WU10 approval)
already did the hard 90% against `rst-c`: a *fresh Codex 0.145.0 process* given
only the adapter recipe `-c mcp_servers.windows-mcp.url="…"` listed 19
`mcp__windows_mcp__*` tools on its first turn, and `mcp up` returned
`endpoint.ready:true, http_status:200`, with `mcp down` confirming both
cleanups. It stopped short of a GUI action and of the end-only reset. Objectives
5 and 6 (`up` half) therefore stay `unimplemented` at the gate level. Fix
boundary: repeat WU10's fresh-process method and complete the gate. No code
change is requested unless the first-turn inventory comes back negative.

**Medium 2 — the interactive argv injection is unpinned on both of its
duplicated sites.** `run.py:1215` (`prepare_launch`) and `run.py:1692`
(`main`) independently append `(managed or {}).get("launch_args")`. Both read
correct, but no test asserts either; `test_windows_mcp_adapters.py:189` only
asserts `managed_mcp_injection` *returns* the args. This is exactly the seam
whose silent failure would explain the two live negatives, and no
engine-launched shell has ever been observed carrying the tools. Fix boundary:
one assertion per site.

**Low 3 — flag #202** (`mcp up` refuses when `SC_HARNESS` is merely unset,
conflating "unknown" with "unsupported"; `status`/`down` are correctly
ungated). Open for FnB disposition; not requested as in-Sprint work.

**Low 4 — the relay port is hardcoded `18000`** (`vm.py:62`, three adapter
recipes) with no override. Two VM-linked forks on one host would cross-target;
WU13's linked-VM gating largely mitigates this. Post-Sprint follow-up.

**Low 5 — spec tension, not an implementation fault.** §Acceptance calls the
gate "one test session", yet also mandates a live Codex first-turn proof;
`rst-c` was necessarily contacted in WU10, WU12 and WU14. No opening or
mid-session reset ever occurred, so the substantive rule held. Worth a spec
clarification, not a defect.

No immediate safety impact. Nothing in `main` is unsafe to run; the gap is
unproven GUI capability at the acceptance gate, not a live hazard.

## Requested action

Cut against spec #108 and re-arm.

**Unit A — wave 1, `report_only`, no dependencies. Dev DEV6 (shell 12),
Rev REV1 (shell 7).**
Title: *Complete the rst-c acceptance gate from a fresh Codex process.*
Description: use WU10's proven method — do **not** rely on your own session's
`SC_HARNESS`. Spawn a fresh Codex process against `rst-c` carrying only the
managed adapter recipe, record its first-turn tool inventory verbatim, then run
§Acceptance steps 1–5 end to end: `status`; `start` only if off; open the
testing application only if absent and report whether it is confirmed open;
`push`; complex `exec`; `capture`; `mcp up`; **one GUI observation/action
through a `mcp__windows_mcp__*` tool**; `mcp down`; then the single end-only
`reset --off`, confirming snapshot and powered-off state. No opening or
mid-session reset. Report every unconfirmed step as unconfirmed. Closes Major 1
or converts it to a code defect.

**Unit B — wave 1, `code`, no dependencies (parallel with A). Dev DEV3
(shell 5), Rev REV2 (shell 8).**
Title: *Pin the interactive managed-MCP argv injection.* Description: add one
assertion each for `run.py:1215` and `run.py:1692` proving a supported
adapter's `launch_args` reach the assembled harness argv, and that an
unsupported or unlinked adapter contributes none. Tests only; no behavior
change.

Hold any Codex-recipe fix until Unit A reports; do not pre-cut it.
