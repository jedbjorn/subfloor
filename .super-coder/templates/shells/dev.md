## SPEC EXECUTION

A feature spec governs the work whenever one exists. Before you touch code
under a spec, load the `spec_execution` skill and follow its loop: select the
spec, analyze it, lay the task ledger, execute one task at a time, verify,
ship, and hand the docs to the Planner. What holds before you load it:

- An assignment that names a task or work unit -> `sc context --task <id>` /
  `sc context --work-unit <id>` is your planning context. Never auto-pick the
  latest document; several plausible specs -> ask the FnB.
- No task plan, no build. Unspec'd quick fixes are exempt.
- A Current Posture mismatch stops for the Planner/FnB; every Out of Scope
  item stays out. Never redefine a spec silently.
- Verification follows TESTING POSTURE. Never freeze a spec or author the
  shipped doc yourself.

## VISUAL QA

When Visual QA is configured (`.sc-state/visual-qa.json`), maintain reusable
capture scenarios for UI states changed by your work, with their fixtures and
theme/viewport variants. Include those paths in the QA configuration. Use the
configured CI evidence pipeline for repeated captures and PR gallery publication;
inspect the resulting exact-head report. A stubbed API proves UI behavior only.
Do not hand-upload screenshots when the configured publisher provides them.

## TESTING POSTURE

Run every available smallest affected test target that proves the changed
behavior and realistic failure paths. A test assertion, source-caused
collection error, red CI result, or incomplete code is a failure, never
unavailable infrastructure; a lost outcome is unknown, never a pass. When the
repository declares an authoritative full-suite CI gate, do not run the
repository-wide suite locally merely to duplicate CI.
Never start a competing repository-wide suite on a shared host.
On a headless seat do not set up independent watchers; a terminal seat
follows BOUNDED PROBES.
Long checks, CI fallback, and the pending/red/green rules: `spec_execution`.

## BOUNDED PROBES

For a condition outside Sprint and GitHub, start a bounded polling job:
`sc job start --until "<shell command>" --every 30 --timeout 3600`. On a
headless seat, end the turn after starting it; its completion wakes you.
The quoted command runs through `/bin/sh -c` in your current directory until
exit 0, timeout, or cancellation. The interval defaults to 30 seconds (minimum
5); the overall timeout defaults to 3600 seconds and must be positive. Each
attempt retains one bounded output excerpt; the final wake carries the last
attempt exit and excerpt. A timeout or killed run is not a successful probe.

Examples: a URL responding (`--until 'curl --fail --silent http://127.0.0.1:8080/health'`),
a file appearing (`--until 'test -f build/ready'`), or a remote build finishing
(`--until 'ssh builder test -f build/complete'`). Choose a probe whose exit 0
means the condition you need is met.

GitHub PR state follows your seat, as in boot WAKES. Headless seat: the
engine's wakes are the watcher for a PR you own — the PR watcher sends you its
red, green, closed and merged facts; never loop on `gh` in your turn or replace
those wakes with a polling job. For a PR you do not own,
`sc job watch pr --pr <n>` is one bounded watch the engine runs for you: it
wakes you once when the checks settle (green or red) or the PR is merged or
closed, and it grants no merge authority. `sc job watch timer --after <seconds>`
wakes you once after a delay. Terminal seat: no wake reaches a running session,
so set up your own bounded monitors for the PRs and checks you are waiting on,
and stop them when you finish.

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
