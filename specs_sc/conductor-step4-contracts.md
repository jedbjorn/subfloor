---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Conductor — CLI sprint orchestration v1
roadmap_status: retired
frozen: true
title: Conductor Step 4 contracts
tags: [conductor, directives, sentinel, migration]
date: 2026-07-28
project: super-coder
purpose: Fix the orchestration data contracts
---

# Conductor Step 4 contracts

## Objective

Land one rebuild-safe contract for directives, sentinel observations, unit
expectations, and retirement of the Interface wake machine. Done means a
flavor-valid directive round-trips through the authenticated API and CLI,
invalid issuers are refused, events are append-only, and the migration drains
a copy of an installed fork with live bindings without losing its audit trail.

## Fixed doctrine

- The Conductor executes data; it does not invent instructions.
- A shell's authenticated database identity determines its issuer. Request
  bodies cannot claim another shell or flavor.
- Directive kind authorization is relational data, not a Python conditional.
- Sentinel observations are evidence, not commands.
- Unit expectations are calibratable rows, not timing constants.
- The five retired wake tables stay dormant for rollback/audit compatibility;
  no current runtime reads or writes them after this step.

## Directive contract

`directive_kinds` is the whitelist. Its composite key is
`(issuer_flavor, kind)`. The v1 rows are:

| Issuer | Kinds |
|---|---|
| dev | `ready-for-review`, `ask-planner`, `merged`, `unit-report` |
| reviewer | `review-clean`, `findings`, `ask-planner` |
| planner | `kickoff`, `hold`, `re-scope`, `re-task`, `close`, `answer` |
| system | `pr-green`, `pr-red`, `pr-merged`, `stall`, `dead-shell` |

`directives` carries `directive_id`, authenticated `issuer_shell_id`,
`issuer_flavor`, `kind`, JSON-object `payload`, nonblank `target`,
`sprint_doc_id`, optional `unit_id`, status
`pending|executed|refused`, timestamps, and refusal detail. System directives
have no issuer shell; shell-issued directives must match the issuing shell's
stored flavor. A linked unit must belong to the linked sprint. Pending rows
have no execution timestamp; executed and refused rows do.

The HTTP boundary exposes list, inspect, and authenticated creation. Creation
always starts pending and derives issuer identity from the bearer token.
Supplying a mismatched issuer field is a validation error. `./sc directives`
provides `list`, `inspect`, and `emit`; list/inspect are the Step 4 read
contract and emit is the literal seam Steps 7–8 build against.

## Sentinel contract

`sentinel_events` is an append-only observation log. Each row carries an event
kind, optional shell/sprint/unit/directive linkage, a JSON-object evidence
payload, and `observed_at`. Evidence may carry last worktree mtime, last commit,
PR/check state, last message id, process evidence, and dwell seconds. Database
triggers reject updates and deletes.

The HTTP and `./sc events` surfaces are read-only in Step 4: list with bounded
filters and inspect by id. The engine service writes observations directly in
Step 5 inside its own database transaction.

## Expectations

`unit_expectations` has one row per `sprint_units.state`: JSON-array
`expected_signals`, `max_dwell_seconds`, monitoring enabled, and an update
timestamp. Pending, working, in-review, and blocked seed conservative initial
windows. Merged and cancelled are explicitly present but disabled with no
dwell. Step 6 calibrates values by updating data, not code.

## Wake drain

The migration creates `wake_machine_retirements`, one immutable audit row per
legacy planner binding. It copies the sprint, planner, session, prior release,
and counts of wake batches/items before changing anything.

Then it:

1. cancels every nonterminal wake item;
2. closes every nonterminal batch;
3. reconciles incomplete action receipts with a retirement detail;
4. resolves open planner alerts;
5. releases every live binding with reason `conductor-step4-retired`.

The original five tables remain dormant because installed forks may need a
forensic rollback and SQLite destructive table surgery would add risk without
runtime value. Snapshot stops serializing them. Re-pinning an old engine can
read the retained closed rows but finds no live binding to resume. Re-pinning
the Conductor engine repeats no drain because the migration ledger and audit
uniqueness make the operation idempotent.

## Construction order

1. Add schema, seed whitelist/expectations, and invariant triggers.
2. Add authenticated directive API plus directive CLI round-trip.
3. Add event read API and CLI.
4. Drain legacy rows and remove all current runtime/snapshot dependencies.
5. Exercise the migration against fresh schema and a copy of dos-arch.
6. Run a fresh-context adversarial review and patch every Major.
7. Freeze this spec only after focused and full verification pass.

Steps 2 and 3 are parallelizable after schema exists. The drain follows the
contract because its audit shape is part of the contract.

## Risks and gates

> [!class4]
> The highest risk is a live fork whose binding references Interface parents
> that a clean rebuild no longer serializes. The gate uses a copied real
> dos-arch database, never the live file.

- Fresh rebuild must apply every migration in order with foreign keys enabled.
- Valid issuer/kind pairs insert; cross-flavor and claimed-issuer requests fail.
- Unit/sprint mismatches fail.
- Event update and delete fail.
- Migration on the copied installed DB leaves zero live bindings, zero
  nonterminal wakes, resolved alerts, and one audit row per prior binding.
- Rollback is the pre-migration database copy; applying the old engine to the
  migrated copy sees only closed legacy rows.
- Focused tests, complete pytest, render-check, verify, and diff-check all pass.
