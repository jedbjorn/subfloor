## SPEC EXECUTION

A feature spec governs the work whenever one exists. Before you touch code:

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
   build. Unspec'd quick fixes (a small UI tweak, a minor migration) are exempt.
4. **Execute one task at a time.** `sc mem task start <id>`; work and verify
   only that task; `sc mem task done <id>`; re-read the ledger; set
   `current_state` to the highest done + lowest pending task. Work moved
   elsewhere -> `sc mem task cancel <id> --notes "moved to F<id> task #<n>"`;
   a whole unfrozen spec moves intact with
   `sc mem doc move <document_id> --feature <target>`. Small growth within the
   same intent -> revise the unfrozen spec and add tasks; a separate mental
   model -> stop and recommend a new feature + spec, never absorb it.
5. **Verification** follows TESTING POSTURE and requires every In Scope
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

Run every available smallest affected test target that proves the changed behavior and realistic failure paths. Use the engine's `./sc job` tools to watch checks that may outlive the current tool call or session. Do not set up independent watchers. Complete the implementation before using CI fallback. If a focused local gate cannot execute because the selected interpreter, runner, or declared dependency is unavailable, record the exact evidence, run the remaining checks, then push/open the PR and register it when the workflow provides registration. Required checks pending -> wait; red -> diagnose, fix, and push; green -> review readiness. A test assertion, source-caused collection error, red CI result, or incomplete code is a failure, never unavailable infrastructure. No configured checks or an untrustworthy watcher after one bounded read -> block because no trustworthy seat remains. An optional browser-capability skip is informational and non-failing. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.

## CODE CRAFT

How to write, not just what.

- Before implementing, ask the question that could delete the work. If bending a requirement makes the implementation 10 lines instead of 200, say so and ask — don't build the 200.
- Smallest diff that fully solves it. Every extra moving part is a future bug's home.
- Flat over nested: guard clauses and early returns. Three levels of indentation means restructure, not indent further.
- No speculative abstraction. Don't build for the second caller until the second caller exists. Duplicate once; extract on the third.
- Build what was asked, nothing more. Unrequested options, fallbacks, and "while I'm here" features are scope creep — open a flag instead.
- Match the neighborhood. Reuse the existing util and idiom; introducing a new pattern requires a stated reason.
- Handle errors where something can be done about them. Blanket try/except at every layer hides bugs; let unexpected failures fail loudly.
- When a fix needs a fix, stop — suspect the diagnosis, not the patch.
- State the trade-off you picked. If a simpler approach existed and you rejected it, say why in the PR, not silently.
- Prefer deletable over extensible. Code that's easy to remove beats code that's built to grow.
