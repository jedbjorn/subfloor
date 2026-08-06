# Sprint 2 conformance — episode 2 (units 16, 17)

Reviewer: REV1. This is the second, final conformance record for Sprint 2.
Report #3 (13:39:59) covered the nine units terminal at that time; decision #188
then re-entered the Sprint for units 16 and 17. Both are now terminal, so this
episode judges the added scope and restates the whole-Sprint verdict.

Spec document #108, bound revision
`4b1b2cf76b75e38eddcc436c99e85ed9048103a0003c69029e02c60665a626e4`, still equal
to the document's current revision; zero mid-Sprint spec edits recorded.
Integrated target: `jedbjorn/subfloor` `origin/main` @ `3780151` (unit 16 /
PR #1049 merged 14:10:52).

## Method

I compiled the evidence packet myself (`compile-report --limit 50`; no
truncation counters set on any section), read unit 16's full merge diff, ran the
suite on the clean main checkout at `3780151`, and traced unit 17's preflight
failure into the actual downstream repo rather than accepting the report's
framing.

Test result on integrated main @ `3780151`: **1942 passed, 947 subtests passed**
(125.9s), full `tests/` suite. No skips, no xfails, no red. Episode 1 measured
1939/945 at `f9dbc9c`; unit 16 added three tests and broke nothing.

## Unit 16 — as-specced

PR #1049 @ `4a257a4699`, approved by REV2 (judgment #58), merged. Tasks
#334/#335. The diff is documentation and help-text only — `dispatch.sh` help
text, two skill bodies, migration 0183, an `exec` argparse description, and the
`vm_mcp_relay.py` module docstring. `vm.py`'s only change is help prose; no
behavioral code path moves. It also corrects a real omission: `./sc vm push`,
`exec`, and `capture` were undocumented in `./sc help` despite shipping in
earlier units. Correct, in-lane, and low-risk.

## Unit 17 — unimplemented; the gate remains unproven

Unit 17 (task #336, report-only, DEV4) self-declared **INVALID — mandatory
preflight failed. This report is not gate evidence.** That self-declaration is
correct and is the right call: the runner stopped at its safety boundary rather
than manufacture a result, satisfying spec line 192.

Episode 2 did eliminate episode 1's root cause. Report #3 held that the gate
needed a shell launched *inside* `rst-c` on a current engine; unit 17 did
exactly that — `rst-c` reconciled and pinned to `3780151`, shell launched there,
`SC_HARNESS=codex` proven set. The old blocker is gone. A different one stopped
it: the first preflight command returned `sc: unknown command 'vm'`.

I traced that rather than accept it as environmental. Finding 1 below records
the cause: an engine defect in the update path, outside spec #108's scope.

## Whole-Sprint classification (restated)

- **as-specced** — #1020, #1021/#1023, #1022, #1024, #1025, #1026, SC-467,
  SC-468, and unit 16's guidance corrections. All verified in code and green on
  integrated main at `3780151`, not taken from PR prose. Detail in report #3.
- **deviated-intentionally (ratified)** — guest console stdout transliterates
  non-ASCII on the return leg. The acceptance clause binds the inbound leg,
  proven byte-exact on the socket; the corruption is the Windows guest console
  codepage, outside the engine boundary. Documented with a base64 workaround by
  unit 15. Ratified by REV2, judgment #56.
- **unimplemented** — spec line 182 (live first-turn Windows MCP tool evidence)
  and spec line 188 (one `rst-c` session exercising `mcp up`, one GUI
  observation/action, `mcp down`). Unproven after three attempts: units 12, 14,
  and 17.
- **deviated-silently** — none.

## Why this concludes rather than re-enters

Three acceptance attempts have failed for three *different* environmental
reasons, each newly discovered at the attempt: units 12 and 14 because
`SC_HARNESS` was unset in shells that reached `rst-c` by `cd`; unit 17 because
the launching worktree carried a pre-migration dispatcher. Two re-entry episodes
have been spent on this one gate without closing it, and each episode surfaced a
fresh blocker instead of converging.

The remaining blocker is not fixable inside this Sprint's bound scope. Spec #108
is Windows VM driving hardening; finding 1 is a defect in
`.super-coder/scripts/update.py`'s dispatcher reconciliation — the spec #105
single-owner-dispatcher lane. Closing it in-Sprint would require the Planner to
widen a lane the bound spec does not cover, and a fourth patch round on a Sprint
whose spec cannot own the fix is the pattern this skill tells me to escalate
instead of repeat.

Every requirement spec #108 does own is delivered, green, and verified. I
therefore record conformance and route the gate and its blocker as follow-ups,
with the non-convergence escalated to FnB.

## Conclusion

Eleven planned units terminal and completed; eight PRs merged; every automated
acceptance clause in spec #108 met and green on integrated main at `3780151`.
The two live-session acceptance gates remain **unproven**, now for a diagnosed
engine defect outside this Sprint's scope rather than an unexplained one.
Conformance: **pass with follow-ups**, including one Major requiring FnB
disposition before any fork relies on `./sc update` to carry new commands into
existing worktrees.
