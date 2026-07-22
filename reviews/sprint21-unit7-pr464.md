# Sprint 21 · Unit 7 review — PR #464 (session status + analytics integration)

- **PR:** #464 `feat(session): add operator status and analytics integration` @46b682b
- **Branch:** `feat/session-status-analytics` → main · CI all green (tests, verify, render-check, CodeQL ×2, actions)
- **Spec:** doc #20 (feature 14), Operator surfaces + Analytics/GUI rows of the Surfaces table; unit 6 ruling: unit 7 owns release-while-server-lives credential cleanup
- **Author:** DEV3 · Reviewer: REV1
- **Verdict:** 1 Medium (blocks), 7 Lows. Not review-clean until the Medium is fixed.

## Scope reviewed

Public `sc session status|manage|release|retry` (`session_cli.py --operator` +
`sc` routing), unauthenticated `/api/session-control/*` operator routes and
`operator_session_control` in `server.py`, generic provider credential cleanup
on release (`_binding_credential_paths` / `_remove_binding_credentials`),
status/summary payload (`archive`, `summary`, overview), Shells/Analytics GUI
status + queued/error counts (`app.js`, `style.css`), exact native-binding
analytics attribution (`analytics.py`, three token parsers), tests.

## Medium

### SC-465 — `sc session release` crashes with AttributeError on any shell without a binding

`cmd_operator_action` (session_cli.py): `status.get("binding", {}).get("state")`.
The status API returns `{"binding": null, ...}` with HTTP 200 for a shell that
has no session binding — the key is *present*, so `.get("binding", {})` returns
`None` and `.get("state")` raises `AttributeError: 'NoneType' object has no
attribute 'get'`. Reproduced against the branch's code with the real payload
shape.

Every non-planner shortname triggers it — `./sc session release DEV3` gives an
uncaught traceback instead of the server's own clean
"no session binding for this shell" error (which the POST would have returned
had the pre-flight status check not crashed first). Same expression also runs
on the `--after-turn` path. Public operator surface shipped by this unit;
first ordinary misuse hits it.

Fix shape: `(status.get("binding") or {}).get("state")` — plus a test for the
no-binding release path (test_session_cli.py covers idle/dispatching payloads
but never `binding: null`).

## Lows (report notes, non-blocking)

1. **Unbounded `--after-turn` wait.** The client polls status every 1s forever.
   A binding wedged in `dispatching` (dead dispatcher before reconciliation)
   hangs the command indefinitely with no progress output. Bound it or print a
   waiting notice.
2. **Operator route status codes.** POST errors all map to 409 — "unknown shell"
   should be 404, bad `sprint_ref` 400/422; GET maps every error to 404.
   Cosmetic API-design inconsistency; CLI just prints the message either way.
3. **`manage --sprint` accepts any document id.** Validation checks only that a
   `documents` row exists — not `kind`/`SPRINT:` title — and unconditionally
   overwrites the archive's existing `sprint_ref`. A typoed doc id silently
   re-points the archive.
4. **Unauthenticated cross-shell mutations + full-payload status GET.**
   `/api/session-control/{manage,release,retry}/<shortname>` carries no token,
   so any local process (including sandboxed shells that can reach
   127.0.0.1:8800) can release another shell's managed binding — cancelling its
   queued wake jobs and deleting the kimi token file. Pattern-consistent with
   the existing unauthenticated `/api` GUI surface (which can already create
   shells/flags), but this is the first `/api` route with cross-shell
   sprint-liveness and credential side effects — flagged for an FnB posture
   ruling, not as a defect. Relatedly, GET `status/<shortname>` returns the full
   binding payload (control_endpoint, capabilities incl. token_file path, lease
   PIDs) — more than the compact credential-free overview the GUI uses; spec
   only constrains the GUI, so informational.
5. **`summary.errors` conflates counts.** `failed_jobs + int(state=='error' or
   last_error)` double-counts (error binding with N failed jobs reads N+1); a
   stale `last_error` on a recovered binding still adds 1; and failed jobs whose
   trigger messages were read are never requeued by `retry` (unread-only) so
   they inflate the count permanently. Fine as a warn-light, misleading as a
   number.
6. **Release leaves stale owner fields.** `release_session_control` clears
   `managed`/`last_error` but not `lease_pid`/`active_channel_*`, so
   post-release status can report `owner=lease` with a defunct PID until
   reconciliation next runs.
7. **Status GET path parsing.** No URL-unquote and `rsplit("/", 1)` silently
   treats the last segment of a deeper path as the shortname
   (`/status/a/b` → shell `b`). Harmless with current alnum shortnames.

## Verified sound (adversarial checks that passed)

- **Credential cleanup (SC-463 handoff honored).** Strict containment: declared
  paths must resolve into `ENGINE/run/session-control` AND be named
  `{harness}-{binding_id}.*`; violations fail closed and refuse release (tested
  both ways). Kimi adapter's `kimi-{binding_id}.token` in the same RUN_DIR
  matches; codex/claude declare no token files (nothing to delete — correct).
  Delete-before-transition cannot strand a binding: `released` is reachable
  from every state and `dispatching` is rejected before deletion; `unlink
  missing_ok` keeps re-release idempotent.
- **Transition/lease discipline.** All release/manage/retry mutations run under
  `BEGIN IMMEDIATE` with compare-and-set transitions; retry requeues only
  failed jobs whose trigger messages are still unread, attempt_count reset —
  matches spec's retry semantics. Manage keeps unit 5's posture validation and
  capability gate; re-manage reactivates only unread cancelled jobs.
- **Analytics exact attribution.** Exact match (harness, native_session_id) →
  binding wins before cwd/time-window fallback, per spec; UPDATE keyed by
  (harness, ref, model IS ?) so multi-model conversations attribute row-by-row
  without duplication (tested: two model rows, off-window timestamp, off-repo
  cwd → both attributed, count stays 2). No-binding native IDs fall through to
  the old path. Transient `native_session_id`/`cwd` keys can't leak into the
  DB: upsert uses an explicit column allowlist.
- **GUI redaction.** Shells + Analytics use the compact overview only; test
  asserts key-set equality and absence of token/endpoint strings. Session card
  join on bindings can't fan out (archive_id UNIQUE).
- **`sc` routing + help text** correct; `session-control` internal surface
  unchanged for adapters.

## Spec conformance

All four operator commands present with spec'd fields (binding id, engine +
native ids, harness/model, state, owner, queued, last delivery, last error).
`release --after-turn` is implemented as client-side wait-then-release — the
spec's "refuses while a dispatch is active unless `--after-turn`" doesn't say
server- vs client-side; behavior matches intent (noted, no flag). GUI shows
compact status + queued/error counts per spec. No schema changes (uses unit 1
tables) — correct for this unit. No ambiguity calls were declared by DEV3 and
none were needed on my reading.
