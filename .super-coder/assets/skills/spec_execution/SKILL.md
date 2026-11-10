---
name: spec_execution
description: Load before you build against a feature spec, task, or work unit, and before you choose which tests prove a change. Holds spec selection, the pre-plan analysis, the task ledger, one-task-at-a-time execution, verification, the ship and docs handoff, and the testing posture (smallest affected gate, long-running checks, CI fallback, when the full suite runs). Developer.
category: workflow
common: false
---

# spec_execution — build what the spec promises, prove what you built

A feature spec governs the work whenever one exists. Unspec'd quick fixes (a
small UI tweak, a minor migration) skip the task plan; the testing posture
below applies to every change.

## Before you touch code

1. **Select the spec.** An assignment that names a task or work unit ->
   `sc context --task <id>` / `sc context --work-unit <id>` is your planning
   context. Otherwise `sc mem get documents --feature <id>`, then the whole
   body with `sc mem get documents --doc <doc_id>` and its ledger with
   `sc mem get tasks --doc <doc_id>`. Never auto-pick the latest document;
   several plausible specs -> ask the FnB. Existing tasks -> resume the first
   unfinished one. Read broader indexes only for an unresolved need.
2. **Analyze before planning.** Viability: bounded, clear entry points,
   session-sized verification; a missing done-condition is unclear. Current
   Posture matches the documented baseline; a mismatch stops for the
   Planner/FnB — never redefine it silently. Every In Scope promise is
   planned and every Out of Scope item stays out. Anticipated User Activity
   roles, reach, authority, and tenancy are planned. Two plausible readings or
   unstated required knowledge -> ask the FnB. A missing prerequisite,
   environment, or external dependency -> open one High flag on the feature
   and stop at its boundary.
3. **Plan.** Building now moves `brainstorm|long_term|near_term` to
   `in_progress`: `sc mem roadmap status <feature_id> in_progress`. Confirm
   the work-stream; assign the obvious one with
   `sc mem roadmap project <feature_id> <shortname>`, ask when ambiguous. Lay
   the ledger: `Preparation` at seq 0, one row per independently verifiable
   step, `Verification` last, each
   `sc mem task add "<step>" --feature <id> --doc <doc_id> --seq <n> --desc "<outcome>"`,
   then `sc mem state "[F<id>] last: —. next: Preparation."`. No task plan, no
   build.

## Build

4. **Execute one task at a time.** `sc mem task start <id>`; work and verify
   only that task; `sc mem task done <id>`; re-read the ledger; set
   `current_state` to the highest done + lowest pending task. Work moved
   elsewhere -> `sc mem task cancel <id> --notes "moved to F<id> task #<n>"`;
   a whole unfrozen spec moves intact with
   `sc mem doc move <document_id> --feature <target>`. Small growth within the
   same intent -> revise the unfrozen spec and add tasks; a separate mental
   model -> stop and recommend a new feature + spec, never absorb it.
5. **Verification** follows TESTING POSTURE below and requires every In Scope
   done-condition and Anticipated User Activity contract; unexpected reach,
   weakened hardening, or crossed tenancy fails. A large spec may stop after a
   verified task slice with the next task named in `current_state`.
6. **Ship and hand docs to the Planner.** All tasks done + Verification green
   -> `sc mem roadmap status <feature_id> shipped`; open one Medium flag
   `"[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc"
   --feature <id>`; message the planner to freeze the spec, write the
   `kind=doc` document, and close the flag; tell the FnB. No planner shell ->
   tell the FnB and leave the flag open. Never freeze or author the shipped
   doc yourself.

## TESTING POSTURE

- **What to run.** Run every available smallest affected test target that
  proves the changed behavior and realistic failure paths.
- **Long checks.** Use `sc job start --label gate -- <command and args>` for
  checks that may outlive the current tool call or session. After confirmed
  registration/start, end the turn and continue on the owner wake; inspect
  `sc job status <id>` and `sc job tail <id>`. A lost outcome is unknown,
  never a pass. Do not set up independent watchers.
- **CI fallback.** Complete the implementation before using CI fallback. If a
  focused local gate cannot execute because the selected interpreter, runner,
  or declared dependency is unavailable, record the exact evidence, run the
  remaining checks, then push/open the PR and register it when the workflow
  provides registration. Required checks pending -> wait; red -> diagnose,
  fix, and push; green -> review readiness.
- **What counts as failure.** A test assertion, source-caused collection
  error, red CI result, or incomplete code is a failure, never unavailable
  infrastructure. No configured checks or an untrustworthy watcher after one
  bounded read -> block because no trustworthy seat remains. An optional
  browser-capability skip is informational and non-failing.
- **The full suite.** When the repository declares an authoritative full-suite
  CI gate, do not run the repository-wide suite locally merely to duplicate
  CI. Run the full suite locally only when no authoritative CI gate exists,
  the change crosses test/CI/harness infrastructure, the FnB explicitly
  requests it, or bounded diagnosis requires it. Never start a competing
  repository-wide suite on a shared host.
