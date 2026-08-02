---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint v1 architecture removal
roadmap_status: shipped
frozen: false
---

# CONFORMANCE: Sprint v1 Architecture Removal

- **Feature:** #29 · **Spec:** #44 (seq 1) · **Reviewer:** REV2 (Review-02, shell 8)
- **Evidence set:** merged `main` at `b35cd61` (PR #837 + removal chain #825/#826/#828/#829/#830/#834) **plus** open test-only PR #838 at `83a9229` (PLN1 addendum, msg #410) — reviewed as one final evidence set.
- **Method:** independent review of final code/schema/runtime against the authoritative DB spec — not PR diffs, not DEV3's report. Three adversarial gates run from detached worktrees (`/tmp/rev2-main`, `/tmp/rev2-838`): (A) schema/migration incl. independent runner re-execution, (B) full-tree absence denylist scan, (C) retained-systems integrity incl. full suite. Blocking claims (SC-020/021/022) re-verified firsthand against source before flagging.
- **Narrative input:** PLN1's kickoff (msg #405) carried **no ratified judgement-call list**. By construction nothing below can be classified `deviated-intentionally`; every departure from the spec's letter is therefore reported as `deviated-silently` for the planner to rule.

## Verdict

**NOT CLEAN — do not close out feature #29.** The removal itself is thorough and the evidence is strong: every schema/migration/runtime/retained-system requirement verified as-specced, full suite 1261 passed + `./sc verify` PASS on a fresh clone of `b35cd61`, and PR #838 is a genuine regression (recommend merge). But three spec-silent deviations survive the cut — one Major under the spec's own bar ("any surviving executable reference is a blocking finding") — and PR #838's disposition is still open.

## Requirement verdicts

### Absence (the cut)

| Requirement | Verdict | Evidence |
|---|---|---|
| Gen 1 — Markdown-board conventions/reports/hooks absent | as-specced | tree scan; no Sprint doc title conventions, close/freeze hooks in runtime |
| Gen 2 — DB board/eventing (sprint_units, watched_prs, poller, inbox wake) absent | as-specced | modules deleted; zero non-allowlisted hits; no watch registry/API/CLI |
| Gen 3 — retired Interface/TMUX wake state absent | **deviated-silently (Major)** | schema/runtime clean, but `.super-coder/aliases.mk:75-83,33-36` ships `dos-status/start/view/attach/take/take-control/stop/reconcile/recover` → dead `./sc interface …` verbs; `dos-help` teaches the INTERFACE workflow; `tests/test_aliases_make.py:117-134,158-176,181-200` enshrines it → flag SC-020 |
| Gen 4 — Conductor (shell, sentinel, directives, Sprint conversations/UI/CLI, role skills) absent | as-specced | zero CON1/conductor hits in code, schema, templates, hooks, adapters; `conductor_routes.py`/`conductor_runtime.py` deleted; flavor defaults + grants deleted by 0144 |
| Sprint PR watcher removed (registry, poller, observations, service, API, CLI, heartbeat UI, docs) | as-specced | no poller/watch-daemon anywhere; `ecosystem.config.cjs` supervises only server + hourly remap |
| Sprint skills (sprint_pln/dev/rev/cond/onboarding + predecessors) absent from assets, seeds, grants | as-specced | assets/skills clean; 0001 seed cleaned; 0145 reseeds generic guidance; template grants clean |
| 24 removed tables + indexes/triggers absent (fresh + migrated); no archive/replacement table | as-specced | 0144:115-139 drops (+ bonus `interface_recovery_observations`); independent fresh build (93/93 migrations) and dirty-fixture migration: zero residue in sqlite_master; 37/37 spot assertions |
| Removed columns gone (conversations.mode/sprint_doc_id, shell_messages.sprint_doc_id, archives.sprint_ref) | as-specced | 0144 rebuilds with narrowed projections (222-346); PRAGMA table_info on fresh + migrated DBs confirms |
| Append-only event trigger + message CHECKs survive cutover correctly | as-specced | `trg_conversation_events_append_only_delete` lifted at 0144:94 for the purge, recreated byte-equivalent at 469-479 (all 15 recreated triggers diffed vs 0132 — identical semantics); shell_messages kind CHECK narrowed to shell/task/result; `pr_event` insert rejected on fresh DB |
| API: removed routes → standard unknown-route; no 410 stubs; messages/conversation APIs carry no Sprint scope | as-specced | route enumeration; test_sprint_entrypoint_removal passes; MESSAGE_KINDS = {shell,task,result} (server.py:259) |
| CLI: sc sprint/directives/events/watch + slot flags gone; old verbs fail as unknown surfaces | as-specced for `sc` itself; see SC-020 for the make surface | `./sc interface status DEV1` → `unknown command`; dispatcher clean |
| Browser: no Sprints nav/badge/board/assignments/Conductor transcript/cancel/analytics grouping; Chats/Diff unchanged | as-specced | ui/index.html nav has no Sprints item; no sprint CSS/views/API calls (test_conversation_ui asserts) |
| Guidance: README/docs/quick-start teach no removed workflow; no Sprint/Conductor/TMUX references | as-specced | case-insensitive scans clean; image refs consistent |
| Migration-source policy: schema.sql + historical migrations cleaned; deleted filenames may stay stamped; runner keys on present files | as-specced | schema.sql (432 lines) contains none of the removed names; migrate.py:47-50 globs present files minus ledger; 40+ numbering gaps tolerated |

### Retained (the keep)

| Requirement | Verdict | Evidence |
|---|---|---|
| Normal conversations: create/send/queue/resume/interrupt/close, replay, outbox, stars, Git targets, Diff review (incl. current-worktree Diff) | as-specced | full suite green (1261 passed, 1 env-skip); broker finish_run/recovery paths traced — conversation-local only |
| Broker supervision, lease recovery, generic daemon heartbeat — no Sprint queries, no directive/sentinel/assignment emission | as-specced | conversation_broker.py:348,389,593,852-867; heartbeat is generic daemon_heartbeats insert |
| Generic messages (shell/task/result, dedupe, read state); job completion results | as-specced | server.py:1986-2577; dedupe_key path; generic (100,'shell'),(101,'result') fixture rows survive cutover |
| Models: discovery, route resolution, flavor defaults, quota probes — no Sprint naming/skills | as-specced | model_catalog/models/live_model/quota_probes clean; flavor defaults = admin/cartographer/dev/devops/planner/reviewer only |
| Generic headless sc run — no slots/Sprint env/Sprint archive annotation | as-specced | run.py:1153-1158,1592-1619 generic env only |
| Identity/memory/roadmap/docs/spec_tasks/flags/projects retained | as-specced | fixture identity/memory/roadmap rows survive migration; routes + mem surfaces clean |
| Install/update/restart/snapshot/rebuild/seed-skills/render retained | as-specced (with SC-022 caveat on update ordering) | `./sc verify` PASS on fresh clone of b35cd61 (rebuild → render → render-only boot) |
| Startup: no poller/sentinel/Conductor/watch thread; no startup race | as-specced | server.py main(): require_current_schema → transport + commit-woken broker only |
| Update/restart ordering: stop old service → migrate → start new | **deviated-silently (Medium)** | update.py:850 migrates live DB with no stop/bounce of the pm2 server; docs say only "restart the session" → flag SC-022 |
| No Sprint v2 entity/name/schema/workflow introduced | as-specced | no new Sprint-shaped abstraction anywhere in the scan |

### Cutover behavior

| Requirement | Verdict | Evidence |
|---|---|---|
| Missing Sprint table during cleanup = successful no-op | as-specced | 0144 placeholder tables (16-35), IF EXISTS throughout; applies clean on fresh DB |
| Rebuild failure rolls back whole migration; partial cleanup not stamped | as-specced | migrate.py:81-96 single txn + rollback; rollback test passes |
| Idempotency / ledger stamp-once / retry no-op | as-specced | cleanup-body-twice test; PR #838's runner-level proof (below); independent retry run → "nothing pending" |
| Dirty pre-removal fixture → converges to retained schema; normal data survives | as-specced | fixture migration converges; conversations/events/runs/outbox/git-targets/messages/identity/roadmap all preserved; PRAGMA foreign_key_check clean |
| Active Sprint does not block removal; queued Sprint conversations discarded | as-specced | destructive by design; fixture Sprint rows purged, verified absent |

## PR #838 disposition (msg #410 requirement)

`test: prove Sprint removal migration runner retry` — +73 lines, one new test, `tests/test_sprint_removal_manifest.py` only; branched off b35cd61; all hosted checks green.

**Judgment: genuine regression, not a tautology — recommend merge.** It is the only test driving the cutover through the real runner (`migrate.migrate()`), exercising `_strip_outer_txn`, BEGIN/COMMIT re-wrapping, and the ledger — the pre-existing dirty test runs 0144 raw via executescript. Concrete bugs that turn it red: re-applying a stamped migration (ledger PK), apply-without-stamp (retry re-applies), strip mangling the recreated append-only triggers (its DELETE/UPDATE assertions fire). Honest limits: no fault injection (crash-mid-migration stays with the pre-existing rollback test); its append-only statements duplicate the raw-path test (redundant there, meaningful via the runner); pr_event rejection lives in the fresh-build test, not here. One nit: a dead `PRAGMA foreign_keys=ON` on an immediately-closed connection (Low).

**Feature close-out stays blocked until #838 is merged or explicitly declined.**

## Findings

| # | Severity | Finding | Flag |
|---|---|---|---|
| F1 | **Major** | aliases.mk ships the retired Interface operator surface (9 dead `./sc interface …` make targets + dos-help INTERFACE section); test_aliases_make.py enshrines it. Spec bar: surviving executable reference = blocking. Nuance for the ruling: the dead-verb condition predates this stack (#687) — possibly a deliberately deferred Interface-CLI removal rather than Sprint-removal residue | SC-020 (#61) |
| F2 | **Medium** | mem.py:64-65 send-help advertises removed `--assignment/--result-kind/--directive` Sprint flags the parser rejects | SC-021 (#62) |
| F3 | **Medium** | update.py migrates the live DB (incl. destructive 0144) without stop-old→migrate→start-new ordering; unenforced and undocumented. Reverse direction guarded by require_current_schema | SC-022 (#63) |
| F4 | Low | server.py:277 docstring + :311-312 sys.exit recovery text reference retired "Interface reconciliation" machinery (stale prose in an incident error path) | noted under SC-022 |
| F5 | Low | 0144's shell_messages rebuild drops its sqlite_sequence row — message_ids restart at max(retained)+1, can reuse ids of deleted Sprint rows. Inert today (no FK to message_id); worth a header comment | doc-only |
| F6 | Low | Fresh-vs-migrated DDL differ only in a legacy comment on daemon_heartbeats (fixture provenance, cosmetic) | doc-only |
| F7 | Low | test_conversation_diff_browser.py skips without playwright — browser-Diff e2e rests on static UI contracts locally; CI coverage seat worth confirming | doc-only |
| F8 | Informational | db_backups/*.db git-tracked binaries contain pre-removal schema — historical, non-executable, predate the stack | none |

## Evidence log

- Suite: `pytest tests/ -q` @ b35cd61 → **1261 passed, 1 skipped (playwright env), 738 subtests, 72s**.
- `./sc verify` on fresh clone @ b35cd61 → **PASS** (rebuild from schema+migrations+content → init → flat render → render-only boot).
- Targeted: test_sprint_removal_manifest + test_migrate → 18 passed @ b35cd61; 14 passed @ 83a9229 (delta = exactly the new runner test). Entrypoint/operator/alias/schema-guard/UI/broker/run suites → 105 passed, 252 subtests.
- Independent spot check (real migrate.py via subprocess, scratch DBs): fresh build 93/93 migrations + dirty fixture + runner + retry → **37/37 absence/retention assertions** (all 25 tables, 4 columns, 2 triggers, pr_event rejection, CONX soft-delete, skill deletion, FK integrity, retry no-op).
- Full-tree denylist scan (all 24 table names, removed columns, SC_SPRINT_*, route prefixes, CON1/conductor, sentinel, pr_poller, watch, sprint skill names): zero non-allowlisted executable hits outside F1/F2/F4.

---

## Scoped rerun — 2026-07-31, current `main` @ `25982686` (post-PR #844)

- **Scope (PLN1 task, msg #417):** rejudge only the prior blockers F1/SC-020, F2/SC-021, F3/SC-022 + stale server.py Interface prose, plus immediate regression surface; confirm retained unrelated aliases/update behavior. Independent inspection of the tree at `25982686` from a detached worktree (`/tmp/rev2-rerun`) — not the PR report.
- **Disposition residue resolved:** PR #838 merged as `a4dfac1` — the prior close-out blocker on its disposition is gone.

### Rerun verdicts

| Finding | Rerun verdict | Evidence |
|---|---|---|
| F1 / SC-020 (Major) — dead Interface/TMUX make aliases + dos-help workflow | **RESOLVED** | `aliases.mk` carries zero Interface/TMUX targets; the 9 retired aliases (`dos-status/start/view/attach/take/take-control/stop/reconcile/recover`) and the dos-help INTERFACE section are gone; sole remaining "interface" hit is a generic prose word in the header comment. `test_aliases_make.py:113-122` now enshrines the *negative* contract (targets don't resolve, absent from both help surfaces), and 38 positive delegation subtests pin the retained unrelated aliases (dos-e/l/r/d/u/t/url/models/job/build/…/remove). No `dos-*` Interface target name survives anywhere in the tree. |
| F2 / SC-021 (Medium) — mem.py help/parser mismatch | **RESOLVED** | Zero occurrences of `assignment`/`result-kind`/`directive` in `mem.py`; docstring synopsis reads `--kind shell|task|result`, matching the parser (`mem.py:1099`); live `--help` output confirms. |
| F3 / SC-022 (Medium) — update ordering | **RESOLVED** | `update.py:728-734` `migrate_with_service_cutover()`: stop-old → `migrate_or_rebuild()` → start-new, single call site (`update.py:911`), single funnel. Stop failure → `sys.exit` *before* migration ("refusing to migrate the live DB"). Migration failure → start deliberately **not** in `finally` — incompatible old code is never restarted. Start failure → loud exit naming the manual recovery. PM2 name contract `sc-<repo>` matches `ports.py name`. **Verified firsthand against real pm2:** unregistered process → `pm2 pid` exits 0/empty → no cutover, update proceeds; stopped registered process → `pm2 pid` prints `0` → no cutover (registered/stopped processes untouched, as PR body states). |
| F4 (Low) — stale server.py Interface prose | **RESOLVED** | Both sites rewritten (`server.py:277` docstring, `:310-311` recovery text). Remaining "Interface" hits at `:3182/:3217` are the *living* browser-session-minting concept (spec #26), not the retired TMUX machinery — accurate prose, not residue. |

### Regression surface

- Full suite @ `25982686`: **1270 passed, 1 skipped (playwright env), 732 subtests** — matches the PR's claim; baseline at `b35cd61` was 1261, delta = the new cutover/mem/alias contract tests.
- Focused batch (cutover, aliases, mem, update materialize/source-sync, migrate, sprint removal manifest + entrypoint): 114 passed, 126 subtests.
- Denylist rescan of the three touched runtime files (`mem.py`, `aliases.mk`, `update.py`) for sprint/conductor/sentinel: zero hits. Update routing test proves `main()` cannot bypass the cutover seam.

### Scoped rerun verdict

**CLEAN.** All three prior blockers and the Low prose finding are resolved as-specced on current `main`, PR #838's disposition is settled by merge, and the retained alias/update surfaces are contract-pinned and green. Feature #29 close-out ruling is the planner's; from the conformance seat nothing in the rerun scope blocks it.

*Residual notes (non-blocking, informational):* (1) a broken pm2 *daemon* (binary present, `pm2 pid` itself erroring) now hard-fails `./sc update` conservatively before migration — fail-stop by design, but undocumented in operator help; (2) the word "Interface" now names two different things across the codebase (retired TMUX machinery vs. the living browser-session surface of spec #26) — worth a terminology pass someday, not a defect.
