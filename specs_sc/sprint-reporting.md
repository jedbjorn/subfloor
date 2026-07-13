---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint reporting — unit reports, conformance pass, planner synthesis
roadmap_status: next
frozen: false
title: Sprint reporting & conformance
tags: [sprints, reporting, conformance, review, orchestration]
date: 2026-07-13
project: super-coder
purpose: Ground-truth sprint close-out
---

# Sprint reporting — unit reports, conformance pass, planner synthesis

## Overview

Sprint close-out today ends on a prose mandate: "write the sprint report,
the message trail is your primary source." That leaves the planner
reconstructing the sprint from one-line transition rows — and leaves one
question nobody in the loop is positioned to answer: **does what shipped
on `main` actually match the spec?** Per-unit reviewers gate diffs against
their unit's scope; no gate ever reads the integrated result against the
whole spec.

This feature adds three pieces, all skill-text and templates — no schema,
no daemon, no new CLI:

1. **Dev unit reports** — every dev closes its unit with ONE structured
   `result` row (fixed template): what shipped, judgement calls, issues
   hit, known deviations, follow-ups. Written at merge, while the unit's
   history is still in the dev's context.
2. **Conformance pass** — after all units merge and `main` is green,
   *before* the freeze, the planner boots review shell(s) to read the
   spec and the code on `main` — not the diffs, not the narrative — and
   classify every spec requirement four ways.
3. **Planner synthesis** — the sprint report becomes a fixed section
   skeleton the planner fills by reasoning over the unit reports + the
   conformance doc + the trail, so the FnB gets a realistic state of
   where things are, not a merge log.

> [!class1]
> Design intent (FnB, 2026-07-13): each dev reports on its own unit, a
> final conformance review covers the integrated whole, and the planner
> generates the sprint report by reasoning over both — "so we grab a
> realistic state of where we are."

## Problem

Three gaps in the current close-out:

- **Self-reports are the only per-unit record.** A dev's transitions
  arrive as one-liners (`pr-open`, `merged`). The judgement calls, the
  CI fights, the corners knowingly cut — all of it lives in an ephemeral
  worker session that exits at merge. By close-out it is gone; the
  planner reconstructs from breadcrumbs.
- **Nobody reads the whole against the spec.** Unit reviewers verify
  *my diff does my unit*. Cross-unit seams — unit 3's interface drifting
  from what unit 5 assumed, a spec requirement that fell between two
  units, a silent deviation each reviewer individually waved through —
  are invisible to every existing gate. "All units merged" and "the spec
  shipped" are different claims; today we only ever check the first.
- **The report has no contract.** "Cover: units shipped, review
  outcomes, ambiguity calls, stalls…" is guidance, not a skeleton. Each
  sprint report is shaped by whatever the planner happens to remember,
  and reports aren't comparable across sprints.

## Dev unit reports

At merge (the `sprint` skill's step 7), the dev's merged-notification to
the planner grows from one line into the **unit report** — still a single
`result` row, fixed template:

```
unit-report <doc-id> unit=<seq> pr=#<n>
shipped: <what the unit does now, 1-2 lines — the claim, not the diff>
judgements: <ambiguity calls incl. final state (ratified/overruled); 'none'>
issues: <CI reds (real vs anomalous), fix loops, stalls, review friction; 'none'>
deviations: <known departures from the spec's reading + why; 'none'>
follow-ups: <Lows deferred, TODOs left, cleanup owed; 'none'>
```

- One report per unit, at merge, mandatory — timed when the unit's whole
  history is still in the dev's context, not reconstructed later.
- Every field answered; `none` is an answer. `deviations` is the honesty
  field: a deviation declared here is a judgement to ratify, the same
  deviation found only by the conformance pass is a finding.
- Transition rows stay one-line; the unit report is the ONE sanctioned
  multi-line `result` row. No new message kind — the `unit-report`
  header line makes the trail greppable, and widening the `kind` CHECK
  means rebuilding a hot table for zero query power (see Non-goals).
- Reviewers don't file unit reports: their `review-clean` + Low-notes
  rows already carry their record.

## Conformance pass

When every unit is `merged` and `main` is green — **before**
`status: CLOSED`, before the freeze — the planner runs the sprint's
final gate:

```linear
All units merged, main green :::class2 -> Planner boots conformance shell(s) :::class1 -> Spec vs main, four-way verdicts :::class2 -> CONFORMANCE doc + result row :::class2 -> Planner rules on findings :::class1 -> Fix units OR freeze + report :::class3
```

**Who.** Review shell(s) chosen by the planner — reviewer lineage, the
sprint's reviewer harness/model (the declaration interview already
covers them). One shell by default; shard by spec section only when the
spec is genuinely too large for one context, planner's call.

**Inputs.** The kickoff `task` row carries exactly: the spec doc id, the
sprint doc id, the merge SHA of `main`, the section scope (if sharded) —
plus the planner's list of **ratified judgement calls**. That list is
the only narrative input, and it is what lets the shell tell an
intentional deviation from a silent one. Everything else is artifact:
the shell judges the spec against the code on `main`, never the diffs,
never the message trail, never the devs' reasoning.

**Verdicts.** Every spec requirement in scope gets one of four:

| Verdict | Meaning | Lands in |
|---|---|---|
| `as-specced` | code matches the spec's reading | Spec Accuracy |
| `deviated-intentionally` | matches a ratified judgement call | Spec Accuracy, linked to Judgements Made |
| `deviated-silently` | departs from spec, nobody declared it | a finding — severity attached |
| `unimplemented` | spec requires it, nothing on `main` does it | a finding — severity attached |

Findings carry spec section, code location, and Major/Medium/Low — the
sprint's existing severity bar, same meanings.

**Output.** A `documents` row — `CONFORMANCE: <sprint title>`, kind
`doc` — plus a one-line `result` row to the planner pointing at it.
Detail in the doc, wake-up in the message: the body-cap doctrine holds.

**Rulings.** Findings route exactly like sprint events: **Major** → the
planner inserts a fix unit at the front of the chain under still-ACTIVE
sprint authority (this is why the pass runs before the freeze — a
reopened sprint re-grants nothing); re-run the pass scoped to the fix.
**Medium** → planner's judgment: fix unit now, or defer with the FnB
told explicitly in the report's Verdict. **Low** → Deferred &
Follow-ups, never blocks the freeze.

> [!class4]
> The conformance shell holds no authority — it files verdicts. Rulings
> stay with the planner; anything that changes what the sprint *means*
> stays with the FnB. Same escalation ladder as the rest of the sprint.

## Sprint report

The close-out report (orchestration step 5) becomes a fixed skeleton.
The planner fills it by **reasoning over the unit reports and the
conformance doc against each other** — where a dev's `deviations: none`
meets a `deviated-silently` finding on its unit, the report says so; the
whole point is the reconciled, realistic state, not either source pasted
verbatim.

| Section | Primary source |
|---|---|
| `## Verdict` | planner synthesis — five-second answer: N units / N PRs, conformance state (conforms / conforms-with-deviations / gaps-found), main green, anything deferred-with-eyes-open |
| `## Units Shipped` | the board — final table, planned vs. actual order |
| `## Judgements Made` | unit reports (`judgements:`) + planner rulings + severity disputes; every call with its final state |
| `## Spec Accuracy` | conformance doc — verdict table + findings, cross-checked against unit reports' `deviations:` |
| `## Issues Encountered` | unit reports (`issues:`) + the `pr_event`/stall trail — CI fights, anomalous reds, re-scopes, unblocks |
| `## Deferred & Follow-ups` | unit reports (`follow-ups:`) + reviewers' Lows + conformance Lows + anything cut — one actionable backlog, the next sprint's seed list |
| `## Spec Debt` | judgement calls that should be written back into the spec + places the spec was silent, wrong, or contradictory — the input to the spec-update pass |
| `## Metrics` (optional) | mechanical from the trail: review cycles per unit, CI reds, boots per shell, planned vs. actual merge order |

Spec Accuracy reports the artifact; Spec Debt reports on the spec
itself. Every ambiguity call is evidence for both — the call is a
judgement (Accuracy ratifies it), and the fact the spec forced a call
is debt (Debt writes it back). Without the Spec Debt section, the same
ambiguities re-fire next sprint.

Delivery is unchanged: one `documents` row (`SPRINT REPORT: <title>`) +
the `shared/SPRINT_REPORT_<slug>.md` copy + the FnB message. The
CONFORMANCE doc stays alongside as the report's evidence trail.

## Close-out, end to end

```linear
Last unit merges, main green :::class2 -> Conformance pass + rulings :::class1 -> status CLOSED + freeze (authority off) :::class1 -> Participants messaged, watches verified :::class2 -> Report synthesized from unit reports + conformance doc :::class3 -> FnB messaged: report + evidence :::class3
```

The freeze moves one slot later than today — after conformance rulings,
still before the report. Everything else in step 5 (participant
messages, watch teardown, bookkeeping) is unchanged.

## Surfaces to change

| Surface | Change |
|---|---|
| `sprint` skill | step 7: merged-notification becomes the unit-report template; new third slot — **conformance slot** (wake = planner task row; inputs, four-way verdicts, doc + pointer row, no authority) |
| `sprint_orchestration` skill | step 5 rewritten: conformance pass + rulings before freeze; report section becomes the fixed skeleton with source mapping; kickoff mentions the unit-report duty |
| README (*Sprints*) | close-out description gains the conformance pass + report skeleton |
| seed pipeline | `./sc seed-skills` after the two skill edits — regenerated seed migration carries them to installed forks |

No schema change, no new CLI verb, no daemon change. The feature is
skill-text, templates, and one new document convention.

## Non-goals

- **A new `report` message kind.** SQLite can't widen a CHECK in place —
  extending `kind` means rebuilding `shell_messages`, a hot table, for a
  distinction the `unit-report` header line already gives every query.
  Revisit only if trail-filtering demonstrably hurts.
- **Automated conformance tooling.** No coverage scoring, no
  requirement-extraction parser, no lint. The conformance shell is a
  reviewer reading a spec against code — judgment, not measurement.
- **Per-unit conformance passes.** The unit reviewer already gates the
  unit; the conformance pass exists for the integrated whole. Running it
  per-merge would re-pay the cost N times to check seams that don't
  exist yet.
- **Blocking the freeze on advisory findings.** Major blocks (fix unit,
  sprint stays open); Medium is a planner ruling; Low never holds the
  close.
- **Enforcement.** Advisory v1, like all sprint authority: templates and
  skeletons live in skill text, not pre-commit checks. A missing unit
  report is a planner nudge (`task` row), not a merge blocker.

## Done condition

A closed sprint produces: exactly one `unit-report` result row per unit,
filed at merge; one CONFORMANCE doc with a four-way verdict for every
spec requirement in scope, produced against `main` before the freeze;
and one sprint report in the fixed skeleton where every section traces
to its named sources. Silent deviations are either zero or itemized
findings with rulings — "all units merged" and "the spec shipped" are
finally separately-checked claims, and the report states both.
