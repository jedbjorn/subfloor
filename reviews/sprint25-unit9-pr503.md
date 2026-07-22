# Review — Sprint 25 unit 9: PR #503 vs spec doc #20 (§GitHub Polling, §Event Ingress, delivery plan 7)

- PR: #503 `feat(polling): watched-PR polling + daemon cutover (sprint 25 seq 9, task #85)`
  (feat/pr-polling-cutover @2968c3b, DEV4)
- Scope: service-side watched-PR polling + legacy host-daemon cutover — spec #20
  §GitHub Polling / §Event Ingress / §Data Model / delivery plan item 7; decision #19.
- Checks at head: tests ✅ verify ✅ render-check ✅ Analyze ×2 ✅ CodeQL ✅ (6/6).
- Local reproduction: `test_pr_poller.py` 26/26 ✅ · `test_sprint_eventing.py` 64/64 ✅
  (archive extract of @2968c3b, hermetic).
- Dev's declared ambiguity calls (ratified by PLN1 — NOT re-flagged): per-scope
  uniqueness (repo,PR,shell,sprint) · code-atomic cutover · scheduler reuses the
  'watch' heartbeat row.
- Verdict: **1 Medium, 5 Low** — not review-clean; M1 blocks, Lows to the sprint report.

## What the diff does (verified, not trusted)

- New `.super-coder/scripts/pr_poller.py` (466 lines): normalized GraphQL surface,
  `transitions()` (keyed semantic events), `baseline_read()`, `poll_cycle()`
  (runs/observations/events/retirement per repo), `PollerState` (per-repo capped
  backoff + blind-window tracking), `Poller` (service scheduler thread).
- `server.py`: registration now takes an immediate GitHub baseline before arming
  (failed baseline → 502 retryable, no row); `--sprint` scoping validated against
  ACTIVE+unfrozen SPRINT docs; explicit rebind of dormant unscoped watches;
  `POST /_sc/watches/reconcile` one-shot; `main()` starts `Poller(DB_PATH)`.
- Migration 0080: `watched_prs` rebuild dropping `UNIQUE(repo,pr,shell)` → partial
  unique index `idx_watched_prs_active` on `(repo, pr, shell, COALESCE(sprint_doc_id,0))
  WHERE closed_at IS NULL`; closed history retained; ids preserved.
- Cutover: `sc watch daemon` prints RETIRED and exits 0; `watch-daemon-up` no-ops,
  `install` refuses, `down`/`uninstall` remain as the legacy stop+disable path;
  `launch`/`persist` no longer start the host daemon.
- `tests/test_sprint_eventing.py`: old `PollOnceTest` + two heartbeat tests removed
  (poll-cycle coverage re-homed to `test_pr_poller.py`); registration-contract tests
  updated to the baseline world.

## The six focus points — all verified

1. **ACTIVE-watch-only bounded scheduling** ✅ — `Poller.run` sleeps 30s +
   uniform(0,25%) jitter; the GitHub fetch is gated on `armed_watches()` (live rows
   scoped to unfrozen `SPRINT:%` docs matching `^status: ACTIVE$`); first iteration
   is `source="startup"`; reconcile rides the API with `source="reconcile"`; no model,
   no terminal, no git anywhere in the module. The 30s wake itself (cheap local DB
   read + heartbeat) runs regardless — GitHub polling is what's gated; defensible.
2. **Normalized fingerprints only** ✅ — `fingerprint()` returns exactly
   `{state, sha, checks, reviews, review_state}` (a test asserts the key set);
   `_sanitize_err` bounds stored errors to one 200-char line; no prose, logs,
   commit messages, payloads, or tokens in any write path.
3. **Semantic transition dedupe** ✅ — `dedupe_key =
   pr-event|{watch}|{kind:state}|{head_sha}` against the shell_messages partial
   unique index; replay test proves a rewound state store re-detects but cannot
   double-emit. Wake items: same-transaction `INSERT OR IGNORE` under unique
   `(binding_id, message_id)`, only when a live unreleased binding owns the
   (sprint, planner) pair — tested (planner's watch wakes, the other subscriber's
   doesn't).
4. **Cutover** ✅ — verb retired + scheduler enabled in the same commit (ratified
   call b); 0080's uniqueness rebuild verified safe: confirmed no writer of
   `pr_poll_observations` exists on origin/main, so the FK-on `DROP TABLE` is
   row-safe; row/id preservation tested; `git grep` confirms no remaining
   direct-DB `watched_prs` writer outside server.py (which is the service).
5. **Blind windows + backoff + startup reconciliation** ✅ — `PollerState` marks
   any success after ≥1 failure as a blind window (durable `blind_window=1`
   observations, tested end-to-end incl. failure→audit→backoff→skip→recovery);
   per-repo exponential skip capped at 900s with cross-repo isolation (tested);
   startup pass runs inside the thread before the first wait (tested).
6. **Never injects terminal input** ✅ — the poller touches only `gh` and the
   engine DB; no tmux/terminal/broker reference in the module.

Infra cross-checks: `ThreadingHTTPServer` (the ≤60s baseline `gh` call blocks one
handler thread, not the API); `db_driver` WAL + 5s busy_timeout (poller/API
write contention raises into `Poller.run`'s catch and self-heals);
`planner_alerts` has the `dedupe_key WHERE resolved_at IS NULL` partial unique
index `_alert` relies on; migrate.py strips the file's BEGIN/COMMIT and wraps
body+ledger in one transaction, matching 0080's comment.

## Findings

### M1 (Medium) — a heartbeat failure now kills polling; the guard and its regression test were deleted

The retired `cmd_daemon` deliberately isolated the beat:

```python
try:
    beat(con, interval)
except Exception as e:
    print(f"watch daemon: heartbeat error ({e})", flush=True)
n = poll_once(con)   # the poll must still run
```

with the comment "a beat raising into the poll's except would turn a working
daemon into a dead-with-noise one" and a load-bearing test
(`test_beat_failure_never_blocks_the_poll`). The new `Poller.run` calls
`beat(con, …)` first inside the shared `try`, so any persistent beat failure
skips `armed_watches`/`poll_cycle` every cycle — polling dies permanently while
the process lives, with only a stdout line. The PR deletes the guard AND the
test without remark (the test list in the PR body doesn't mention it). The beat
is ancillary liveness; polling is the mission — #359's whole lesson. Any
durable beat failure family (schema-lagging DB missing `daemon_heartbeats` —
the exact pre-0068 case the deleted test defended — disk I/O error, persistent
lock) triggers it. Visible via `sc watch list` STALE, which keeps it Medium
rather than Major. Fix is ~5 lines: wrap `beat` in its own try/except inside
`Poller.run` and restore an equivalent regression test (beat raises → cycle
still polls).

### L1 (Low) — `_emit_event` swallows any IntegrityError as "dedupe"

`except sqlite3.IntegrityError: return False` assumes the dedupe index fired. A
non-dedupe integrity failure (FK/NOT NULL) would be silently swallowed while
the observation and the advanced `last_seen` still commit — the transition is
then permanently lost (fingerprint says seen, no event row, no dedupe row to
catch a retry). Narrow (the armed-watches join makes bad FKs unlikely); check
the error message or pre-query the dedupe key.

### L2 (Low) — baseline-at-registration silently retires already-terminal PRs, event-free

Because registration always stores a baseline, `transitions()`'s prev-None
conclusive-state path (watch registered moments after a transition must still
wake) is effectively dead via the API. A watch registered on an already-merged
PR now retires on the first poll with **zero** events — the old no-baseline
world emitted `merged` (+ checks) on first poll. Spec mandates the baseline,
so the semantics change is per-spec, but the `transitions()` docstring still
advertises the old reach, and the silent retire-no-event case deserves a named
decision (or a `closed_at` note in the register response).

### L3 (Low) — default `sc watch pr` now arms nothing; sprint 25's live watches go dark at merge

Registration without `--sprint` creates a dormant watch (spec: unscoped stays
readable but dormant until rebound). The sprint skill's step 4 still teaches
`./sc watch pr <owner/repo> <n> --shell <planner>` with no `--sprint` — so at
merge, every live unscoped watch (sprint 25's own in-flight units) stops being
polled until rebound or until the operator-workflow unit updates the skill
text. Planner action at merge: rebind sprint-25 watches
(`sc watch pr … --sprint <doc>`) — the rebind path exists and is tested.

### L4 (Low) — reconcile bypasses in-flight backoff

`/_sc/watches/reconcile` calls `poll_cycle` with a fresh `PollerState`, so it
force-polls repos the scheduler is deliberately backing off (rate-limit
recovery), and any shell token can invoke it repeatedly. Operator-intended
override, but it undercuts the backoff exactly when the backoff matters most.
Consider threading the service's `Poller.state` through, or operator-scoping
the route.

### L5 (Low) — GraphQL query interpolates repo owner/name unquoted-validated

`build_query` f-strings `owner`/`name` into the query; server-side validation
is only `repo.count("/") == 1`. A `"` in the repo name breaks (or reshapes)
the query. Pre-existing pattern carried from the old daemon, authenticated
localhost callers only, worst case is a sanitized poll error — but a cheap
regex (`^[A-Za-z0-9_.-]+$` per segment) at registration closes it.

## Verdict

**1 Medium (M1), 5 Low — not review-clean.** M1 must be fixed (restore the
beat/poll isolation + regression test); re-review on the fix push. L1–L5 go to
the sprint report; none gates. On M1 fixed: review-clean, DEV4 merges.
