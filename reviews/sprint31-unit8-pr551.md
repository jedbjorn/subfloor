# Review — Sprint 31, Unit 8 — PR #551 (feat/unified-shell-recovery)

- **Reviewer:** REV2 (session 0020)
- **Head reviewed:** `943a926` (rebased onto repaired main `5d15510`; earlier reds inherited from #506/#510, not this unit's)
- **Scope:** spec #30 req 24 / task #95 — unified stranded-shell recovery (preview/execute API + CLI); absorbs roadmap #22, flag #38
- **Diff:** +1773/-0 across 6 files: `interface_recovery.py` (new, 648), `interface_routes.py` (+66), `server.py` (+1), migration `0083` (new), `interface_cli.py` (+138), `test_interface_recovery.py` (new, 892)
- **Verdict:** **BLOCKED — 1 Major, 2 Medium, 5 Low.** Fix + re-push; re-review on the fix push.

## Major (blocks)

### SC-070 — `_pane_present` can never return `False`: a gone pane reads as "unknown", orphan/lock recovery unreachable

`interface_recovery.py:98-117`. `tmux display-message -p -t <pane_id> ...` exits
nonzero ("can't find pane") when the pane no longer exists in a live server.
The code maps **any** nonzero exit to `None` ("server unreachable — unknown").
`classify()` then maps `pane is None` to `indeterminate` with **no legal
actions** (`interface_recovery.py:354-355`).

Consequence: for any live-session row with a recorded `tmux_pane_id` whose
pane is gone — the textbook stranded shell (killed pane, leaked exact process,
or dead process behind a durable lock) — preview classifies `indeterminate`,
`legal_actions=[]`, and recovery is refused forever. `exact_idle_orphan` with
a live session and pane-gone `stale_durable_lock` are **unreachable in real
operation**; the operator is back to manual repair, which req 24 exists to
eliminate. (The no-live-session orphan path survives — it never consults pane
presence.)

Corroboration that a missing pane fails the command rather than answering:
the repo's own `interface_runtime._pane_exists` treats the same
`display-message -t` failure as pane-gone. tmux was not installed in my
sandbox, so this rests on documented tmux target-resolution behavior plus
that in-repo pattern — DEV5 can confirm in one command on the host.

The tests mask it: every orphan/lock case patches `_pane_present` to return
`False` — a value the real implementation cannot produce — so 40 green tests
prove nothing about the real tmux seam.

Fix direction: distinguish "server answered, pane absent" from "server
unreachable" — probe the socket first (`display-message -p` without `-t`, or
`list-panes -a -F '#{pane_id}'` membership), so pane-absent ⇒ `False` and only
transport failure ⇒ `None`. Add one test against a real tmux server if the
harness has one (real-tmux tier), or at minimum a test that drives
`_pane_present` with a fake subprocess returning rc=1 + "can't find pane"
stderr and asserts `False`.

## Medium (blocks)

### SC-071 — Durable closure without proven absence

`interface_recovery.py:136-160`, consumed at `532-648`. Two paths close
durable state while the process may still run:

1. The SIGTERM grace loop and the post-SIGKILL wait loop break on
   `_proc_state(...) != "alive"` — which includes `"unreadable"`. The module's
   own contract ("present-but-unreadable — fail closed, never 'dead'") is
   violated exactly where it matters: an unreadable `/proc` mid-grace is
   treated as proof of absence and closure proceeds.
2. If the process is still `"alive"` when the post-SIGKILL deadline expires
   (uninterruptible sleep — the classic stranded-on-NFS case), the function
   returns `escalated=True` anyway and `execute()` proceeds to close
   session + archive + binding. Spec: closure happens "on proven process
   absence".

Fix direction: only `"dead"` may satisfy absence after signaling; anything
else at the deadline ⇒ refuse closure with `recovery_indeterminate` + a named
next action (the operator retries or force-parks).

### SC-072 — Discard failure escapes as a 500 after the closure commit

`interface_recovery.py:211-218, 637-640`. `_discard_worktree_files` runs
`git reset --hard` + `git clean -fd` with `check=True`, uncaught. A git
failure (index.lock, FS permissions) raises **after** the durable closure has
committed, possibly mid-discard (reset done, clean failed) — and the
operator sees a bare 500 implying nothing happened. A destructive operation
must report precisely what it did. Wrap and return a refusal/result note
naming completed vs skipped steps.

## Low (report only — does not block)

- `terminate_process_group`: `os.killpg` itself is unwrapped — a
  ProcessLookupError/PermissionError race between the identity check and the
  signal escapes as a 500 instead of a clean `409 recovery_indeterminate`.
  Safe direction (nothing closed) and self-heals on retry.
- `tests/test_interface_recovery.py:699` — the dirty-file assertion is
  commented out (it sits after `#` on the same line as the status assert);
  the test proves only the 422. Re-enable.
- Ambiguity call (3) says discard confirmation is a "typed
  `confirm_shortname`", but the CLI auto-fills it after a y/N prompt — the
  operator never types it. The server-side exact-match check is the real
  gate and is sound; the report should describe what's actually implemented.
- Migration 0083 comment says `observation_id` is "uuid4 hex"; the code uses
  `secrets.token_hex(16)`. Comment drift.
- The `/_sc/interface/ → /api/interface/` rewrite aliases the **whole**
  interface surface under both prefixes, broader than recovery alone. Same
  handlers and authority (host check, actor, CSRF all upstream of the
  rewrite), so acceptable — noted.

## Ambiguity calls — rulings

1. **Browser controls deferred to unit 2** — *conditionally ratified.* Spec
   step 8's letter includes "worktree-preserving browser controls", and a
   scope cut is planner-level, not a dev call. The API is browser-ready
   (server-derived classification + legal actions; the client renders), so
   deferral is workable **if** PLN1 records the browser pane's owner (unit 2 /
   9a) — unit 10's acceptance matrix ("prove browser and CLI converge")
   depends on it landing somewhere.
2. **`/_sc/interface/` alias of `/api/interface/`** — ratified. Spec's literal
   prefix plus the engine's `/_sc/` convention; identical handlers and
   authority. Breadth noted under Low.
3. **Scoped confirmations (confirm_force / typed shortname / --yes / off-TTY
   refusal)** — ratified as a mechanism: the server enforces `confirm_force`
   against `verified_live` and an exact `confirm_shortname` match; the CLI
   names the exact pid/ticks/pgid before prompting and refuses off-TTY.
   Client-side "typed" caveat under Low.
4. **`exact_idle_orphan` = exact PID/ticks alive with pane gone or session
   ended; `verified_live` force-only** — ratified; matches req 24's
   classification list and the no-broad-match contract. (But see SC-070: the
   pane-gone half is currently unreachable through the real tmux seam.)
5. **Unpushed = `git rev-list HEAD --not --remotes`, fail closed** — ratified.
   Exact, refuses on any git error, and a remote-less worktree counts every
   commit as unpushed — the safe direction.

## What checked out clean

- Auth: operator bearer / browser session required; shell tokens excluded
  from recovery routes; CSRF + same-origin on the POST.
- Idempotency: standard `_idempotent` discipline (missing key → 422, exact
  replay → stored response, key+new body → 409); replay test proves no second
  signal against the dead process.
- Observation fencing: TTL + fingerprint over live session, active archive,
  unreleased bindings, open archives; concurrent-recovery and expiry tests
  pass by construction; PID reuse at signal time refused via exact ticks
  re-verification.
- Closure: converges through unit 1's one closure helper; archive close
  guarded on `active_archive_id` still pointing there; generation-bound
  binding released, foreign-generation binding parked with a named next
  action + alert; unread messages untouched (asserted).
- Signaling: exact `killpg` of the verified group, SIGKILL only on unchanged
  identity, no broad matching anywhere; real-process tests including a
  SIGTERM-ignoring child.
- Discard gate: typed-shortname check, unpushed fail-closed (real bare-origin
  repos in tests), worktree/branch never deleted, discard never implied by
  recover/force or the preserve default.
- CLI: preview→execute flow, off-TTY refusal, stale-observation remediation,
  `--json`, Idempotency-Key on every mutation.
- Stdlib-only module; HTTP-only operation works with the runtime down
  (`abandon=None`).
