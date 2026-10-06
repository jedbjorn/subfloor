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

## VISUAL QA

When Visual QA is configured (`.sc-state/visual-qa.json`), maintain reusable
capture scenarios for UI states changed by your work, with their fixtures and
theme/viewport variants. Include those paths in the QA configuration. Use the
configured CI evidence pipeline for repeated captures and PR gallery publication;
inspect the resulting exact-head report. A stubbed API proves UI behavior only.
Do not hand-upload screenshots when the configured publisher provides them.

## TESTING POSTURE

Run every available smallest affected test target that proves the changed behavior and realistic failure paths. Use `./sc job start --label gate -- <command and args>` for checks that may outlive the current tool call or session. After confirmed registration/start, end the turn and continue on the owner wake; inspect `./sc job status <id>` and `./sc job tail <id>`. A lost outcome is unknown, never a pass. Do not set up independent watchers. Complete the implementation before using CI fallback. If a focused local gate cannot execute because the selected interpreter, runner, or declared dependency is unavailable, record the exact evidence, run the remaining checks, then push/open the PR and register it when the workflow provides registration. Required checks pending -> wait; red -> diagnose, fix, and push; green -> review readiness. A test assertion, source-caused collection error, red CI result, or incomplete code is a failure, never unavailable infrastructure. No configured checks or an untrustworthy watcher after one bounded read -> block because no trustworthy seat remains. An optional browser-capability skip is informational and non-failing. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.

## BOUNDED PROBES

For a condition outside Sprint and GitHub, start a bounded polling job and end
the turn: `sc job start --until "<shell command>" --every 30 --timeout 3600`.
The quoted command runs through `/bin/sh -c` in your current directory until
exit 0, timeout, or cancellation. The interval defaults to 30 seconds (minimum
5); the overall timeout defaults to 3600 seconds and must be positive. Each
attempt retains one bounded output excerpt; the terminal wake carries the last
attempt exit and excerpt. A timeout or killed run is not a successful probe.

Examples: a URL responding (`--until 'curl --fail --silent http://127.0.0.1:8080/health'`),
a file appearing (`--until 'test -f build/ready'`), or a remote build finishing
(`--until 'ssh builder test -f build/complete'`). Choose a probe whose exit 0
means the condition you need is met. GitHub PR state belongs to the PR watcher;
never replace its wakes with a polling job.

## RUN RECEIPTS

Attach completed local evidence by run id with `sc job attach <id> --pr <number>`
or `sc job attach <id> --work-unit <id>`. The run and target must share an owner;
a PR must already be registered. Use `--repository owner/name` if its number is
ambiguous. The Runs drawer and fleet offer the same attachment action.
Receipts show beside the observed GitHub check in Chats and the Sprint board.
A commit outside the PR head's ancestry is stale; unavailable ancestry stays
unknown. Pruned logs retain their receipt. Receipts supplement CI evidence;
only GitHub checks and the existing merge grant authorize a merge.

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
