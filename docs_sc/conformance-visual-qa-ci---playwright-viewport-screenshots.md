---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Visual QA CI — Playwright viewport screenshots
roadmap_status: shipped
frozen: false
---

# CONFORMANCE: Visual QA CI — Playwright viewport screenshots

**Sprint:** doc #14 (feature #19) · **Spec:** doc #13 · **Judged:** `main` @ `90545e6`
(tree byte-identical to kickoff SHA `6bbd512`, PR #442's pre-squash head) ·
**By:** REV2, 2026-07-20 · **Method:** spec-vs-code only; the five ratified
judgement calls from task #58 are the sole narrative input.

**Totals:** 46 requirements judged — 36 as-specced · 6 deviated-intentionally ·
3 deviated-silently · 2 unimplemented (one is a spec self-contradiction, one is
the ship-gated feature doc). **Findings: 0 Major · 1 Medium · 5 Low.**

Ratified calls referenced below: **RC1** example config seeded inactive at
`.sc-state/visual-qa.example.json` · **RC2** route success = any viewport
200+screenshot · **RC3** unresolvable base diff → capture, never skip ·
**RC4** shim exports `github.token` as `GITHUB_TOKEN`, marker v1→v2 ·
**RC5** unit-scoped task ledgers on spec 13.

## Verdict table

| # | Spec requirement | Verdict | Where / note |
|---|---|---|---|
| §Overview / v1 shape |||
| 1 | Capture-only — no baselines, no pixel-diffing | as-specced | no diff logic anywhere in `visual_qa.py` |
| 2 | Advisory check — fails only on can't-boot/serve, broken config, no `engine.ref` | as-specced | exit paths in `cmd_ci`; **but see F1** for an undeclared extra red path |
| 3 | Results = sticky PR comment + CI artifact | as-specced | `publish_result` + shim upload step |
| 4 | No GUI tab, no inbox eventing in v1 | as-specced | nothing added |
| 5 | Existing forks adopt via `make update` | as-specced | `update.py` main calls `ensure_workflows()` |
| §Architecture |||
| 6 | Three-part split at the named fork paths (shim / config / runner) | as-specced | `VISUAL_QA_TEMPLATE_TARGETS`, `CONFIG_RELATIVE` |
| 7 | Shim "~30 lines that should not change": checkout → clone → invoke → upload | deviated-silently | **F3** (Low) — 65 lines incl. a shim-side config-parsing retention step |
| 8 | Deterministic: clone at `engine.ref`; no ref → fail "run make update first"; never falls back to `main` | as-specced | shim materialize step, `test -s .sc-state/engine.ref` guard |
| §Workflow shim |||
| 9 | Triggers `pull_request` + `workflow_dispatch`; no path filter in yml | as-specced | |
| 10 | Permissions `contents: read`, `pull-requests: write` | as-specced | |
| 11 | Concurrency `subfloor-visual-qa-${{ github.ref }}`, cancel-in-progress | as-specced | |
| 12 | Upload artifact always (even on failure), retention per config default 14 | as-specced | `if: always()`, `retention-days` from resolved output |
| 13 | Browser cache "keyed on the Playwright version the runner pins" | deviated-intentionally | planner ruling in sprint doc §Unit contracts: key = `hashFiles` of the materialized runner, never a shim-hardcoded version |
| 14 | Managed marker header (spec text shows v1) | deviated-intentionally | RC4 — marker is v2 so seeded shims reconcile |
| 15 | Token for comment posting (spec silent on mechanism) | deviated-intentionally | RC4 — shim exports `GITHUB_TOKEN: ${{ github.token }}` |
| §Fork config |||
| 16 | Schema keys, defaults, validation; `serve`+`routes` required; invalid config fails the check with a clear message | as-specced | `validate_config`, `load_config`; bad JSON → failed summary + exit 1 |
| 17 | `viewports: "default"` = 375×812 / 768×1024 / 1440×900, or explicit `{name,width,height}` list | as-specced | `DEFAULT_VIEWPORTS`, `_validate_viewports` |
| 18 | `paths` skip → neutral pass; empty/absent = always run | as-specced | `should_skip` (falsy paths → never skip); **F2** (Low) glob-semantics note |
| 19 | `services: ["postgres"]` → container, `DATABASE_URL` exported, `setup` runs after | as-specced | `ci_app` orders services → setup → serve |
| 20 | `{port}` substituted by the runner | as-specced | `start_server` |
| 21 | "local mode picks a free port instead of the fixed CI one" | unimplemented | **F4** (Low) — spec self-contradiction: §Runner defines `run` as capture-only against an already-running app; nothing serves, nothing picks a port |
| 22 | Engine ships a "commented example" | deviated-silently | **F5** (Low) — example is strict uncommented JSON |
| 23 | Example seeded inactive; live `visual-qa.json` never auto-created; absent config stays neutral | deviated-intentionally | RC1 — `.sc-state/visual-qa.example.json` |
| §Runner — ci |||
| 24 | Absent config → neutral pass, green, one-line pointer comment | as-specced | exit 0, "run `./sc visual-qa init`" |
| 25 | Path-skip → neutral pass, "no app paths changed" | as-specced | |
| 26 | Unresolvable PR base diff → capture, never a false-neutral skip | deviated-intentionally | RC3 — `pr_changed_paths` returns `None` → run (spec was silent) |
| 27 | Pinned Playwright install + chromium only, ephemeral to CI; engine stays stdlib-only | as-specced | pin `1.54.0`; lazy import; hermetic tests |
| 28 | services/setup/serve/ready poll; ready timeout **fails** the check; boot-log tail in artifact + comment | as-specced | `wait_until_ready`; `boot_log_tail` in summary + `<details>` block |
| 29 | Route × viewport: networkidle + `settle_ms`, full-page PNG at `gallery/<route-slug>/<viewport>.png` | as-specced | `capture_gallery`, `_slug` (dedupe suffixing) |
| 30 | Per-route failure → screenshot what rendered, ✗ in table, check stays green | deviated-intentionally | RC2 — route ok = **any** viewport 200+screenshot |
| 31 | All routes failed → check fails ("app not serving") | as-specced | outcome `failed` → exit 1 (meaning preserved under RC2) |
| 32 | `gallery/index.html` + `summary.json` | as-specced | `write_gallery`, escaped HTML |
| 33 | Sticky comment: marker, edited in place (one comment per PR), status line, ✓/✗ table with dimensions, artifact + run links, no-thumbnails limitation named | as-specced | `build_comment`, paginated search + PATCH |
| 34 | Comment-post failure degrades gracefully: artifact + `$GITHUB_STEP_SUMMARY` land, check status unaffected | as-specced | `post_sticky_comment` non-fatal; step summary written first |
| 35 | (unlisted edge) pre-existing non-runner `gallery/` in the fork checkout | deviated-silently | **F1 (Medium)** — hard red outside the publish path; see Findings |
| §Runner — run |||
| 36 | Capture loop vs a locally running app; default `$SC_DEV_PORT`, `--url` override; local `gallery/`; missing Playwright → install guidance | as-specced | `cmd_run`, `--output` safety checks, `PlaywrightCapture.__enter__` message |
| §Runner — init |||
| 37 | Scaffold best-guess config from repo detection; never overwrite existing | as-specced | `detect_init_config` (package.json scripts, npm ci/install, preview/dev/start, static fallback) |
| §Distribution |||
| 38 | New forks: `install.py` + `init_fork.py` seed shim + example, `is_source_repo()` guard | as-specced | install step 3.7; init_fork shared seed path |
| 39 | `ensure_workflows()` four reconcile rules, in order, with the printed guidance | as-specced | seeded / updated (v-compare) / unmanaged-notice / source no-op; `git add` line printed |
| 40 | Update-time example seeding (preserve existing) | as-specced | required by §Done ("make update seeds the shim + example config") — REV2's in-sprint Low #4 called it spec-silent; §Done covers it |
| 41 | Runner/logic changes ride engine repin, no workflow-file touch | as-specced | shim invokes `./sc visual-qa ci` at pinned ref |
| 42 | Update stages, never commits for the fork | as-specced | guidance print only |
| §Change surface + Testing |||
| 43 | New runner + templates; `sc` dispatch + help line | as-specced | `sc` case arm + help text |
| 44 | `engine_manifest.py` covers the template paths | as-specced | `FORK_TEMPLATE_PATHS` + `.super-coder/templates` in `ENGINE_PATHS`; guarded by tests |
| 45 | Docs: feature doc on ship (docs skill flow) | unimplemented | **F6** (Low) — no feature-19 doc row yet; ship-gated, due at close-out — flagged so the freeze doesn't skip it |
| 46 | Hermetic test suite: config validation, skip logic, marker/version reconcile, comment build, gallery/summary with mocked capture | as-specced | 28 + 11 tests; no playwright/network imports; source guard + seeding covered |

Edge-case table (spec §Edge cases): all 13 listed rows verified as implemented —
no-config, invalid-config, path-skip, boot-timeout (+log tail), one-route-error,
all-routes-error, no-engine.ref, external-fork token, force-push cancel,
marker-removed, source-repo, auth-routes out of scope, settle best-effort.

## Findings

### F1 · Medium · deviated-silently — a tracked `gallery/` dir in a fork reds every PR, outside the graceful-degradation path
`cmd_ci` calls `prepare_gallery(gallery)` **before** its try block
(`.super-coder/scripts/visual_qa.py:1084`). In CI the checkout is fresh, so the
only way `gallery/` pre-exists is the fork **tracking** a real `gallery/`
directory (plausible for the web apps this feature targets). That dir has no
runner `summary.json`, so `prepare_gallery` raises → exit 1: **red check on
every PR**, no sticky comment, no step summary (the whole `publish_result`
machinery is bypassed), and the artifact upload ships the fork's own gallery
content under the `visual-qa-gallery` name. This violates the v1 contract
("advisory — fails only when the app cannot boot or serve") and the spirit of
step 8's graceful degradation; the edge-case table is silent on it. The error's
own guidance — "choose another `--output` directory" — is unactionable in `ci`
mode, which has no `--output` and no config key for the gallery dir (REV1's Low
caught the wording, not the behavior). Only removing the managed marker and
editing the shim works around it.
**Recommend (planner's call):** fix unit — move `prepare_gallery` inside the
publishing try so this degrades to a failed-summary comment at worst, and give
`ci` an escape (config `output` key, or a runner-owned default like
`.sc-visual-qa/gallery`) — or ratify as an accepted v1 boundary + add an
edge-case row to the spec.

### F2 · Low · note — `paths` matching is fnmatch, not path-aware glob
`should_skip` uses `fnmatch.fnmatchcase`, where `*` crosses `/`: `src/*` and
`*.js` match at any depth. This only ever **under-skips** (fail-open toward
capture — consistent with the advisory stance) but is wider than the glob
semantics the spec's `src/**` example implies. One spec sentence would settle it.

### F3 · Low · deviated-silently — shim is 65 lines with a config-parsing step, vs "~30 lines, checkout → clone → invoke → upload"
The "Resolve artifact retention" step parses `.sc-state/visual-qa.json` in the
shim — the one thing §Architecture says lives runner-side. Mitigating: the
spec's own upload requirement ("retention per config default 14 days") forces
*some* shim-side resolution, since `retention-days` is a workflow-level input
the runner can't set retroactively. Declared in-sprint (REV2 Low #3, DEV4 unit
report) but absent from the ratified list. **Recommend:** ratify as intentional
+ one spec sentence acknowledging the retention step.

### F4 · Low · unimplemented (spec self-contradiction) — "local mode picks a free port"
§Fork config's `{port}` note promises free-port picking in local mode, but
§Runner defines `run` as a capture loop against an **already-running** app —
nothing serves, so nothing picks a port. Code follows §Runner. (REV1 Low
follow-up (b), unratified.) **Recommend:** spec fix — delete the sentence or
re-scope it to a future `run --serve`.

### F5 · Low · deviated-silently — example config is strict JSON, not the spec's "commented example"
`templates/fork/visual-qa.example.json` carries no comments; a commented file
would break `json.loads` the moment it's copied live, so the deviation is
defensible. Declared in-sprint (REV2 Low #1, unit report), unratified.
**Recommend:** ratify + spec wording fix ("example config" not "commented
example"), or ship a commented `.jsonc` variant if annotation matters.

### F6 · Low · unimplemented (ship-gated) — feature doc not yet authored
§Change surface: "Docs — feature doc on ship (docs skill flow)". No feature-19
doc row exists (docs index: spec #13 + sprint #14 only). Expected at close-out,
not a unit defect — filed so the freeze sequence doesn't skip it.

## Cross-unit seams checked

- Shim → runner invocation contract (`./sc visual-qa ci`, cwd = fork root): holds.
- RC4 token seam: shim env export ↔ runner `post_sticky_comment` reading
  `GITHUB_TOKEN`: holds; marker v2 ↔ `ensure_workflows` version compare
  refreshes already-seeded v1 shims: holds.
- Config schema contract (spec-fixed): runner validation ↔ shipped example ↔
  init scaffold all agree on keys and defaults; the example passes
  `validate_config` (asserted by `test_example_config_is_valid_and_inactive`).
- Cache-key seam: shim `hashFiles('.super-coder/scripts/visual_qa.py')` runs
  after engine materialization (ordering holds), so the key tracks the pinned
  runner — cache invalidates on any runner change, a superset of version bumps
  (harmless: miss → reinstall).
- Artifact-name seam: shim uploads `gallery/` — matches runner `DEFAULT_GALLERY`.

## Verdict

**Conformant for freeze with one Medium to rule on.** Nothing shipped
contradicts a ratified call; all five ratified calls are implemented exactly as
ratified. The Medium (F1) is a real advisory-contract violation on a plausible
fork shape and deserves an explicit ruling (fix unit vs accepted-boundary
ratification) before the spec freezes; the five Lows are spec-text hygiene and
a ship-gated docs task.


---

## Verdict delta — F1 re-check (2026-07-20 · task #88 · REV2)

**Scope:** the F1 surface only, after fix unit 3 (PR #443, merged as `6e3615e`;
the kickoff cited the pre-squash branch head `6ced66e` — trees verified
identical outside `.super-coder/`). Ratified call referenced: **RC6** — `ci`
output resolved via a validated config `output` key; shim reads it as a step
output with `'gallery'` fallback; managed marker v2→v3 (REV1-ratified).
Method unchanged: spec-vs-code on `main` @`6e3615e`; suites re-run green
(30 + 11 hermetic tests, incl. 3 new tracked-gallery regressions).

**(a) Row 24 — absent config, tracked `gallery/`: as-specced, strengthened.**
`cmd_ci` no longer touches the gallery before it knows it will capture: the
unconfigured branch publishes the neutral comment + step summary with
`write=False` — nothing created, nothing modified. Regression test asserts a
tracked `gallery/` file survives byte-identical and the dir gains no entries.
The path-skip neutral (row 25) got the same non-destructive treatment.

**(b) Row 35 / F1 — resolved: deviated-silently → deviated-intentionally (RC6).**
Configured run against a tracked non-runner `gallery/`: `prepare_gallery`
raises *before* any `rmtree`, `cmd_ci` catches it, publishes the ✗ sticky
comment + step summary with now-actionable guidance ("choose another config
key 'output'"), and exits 1 — a published failure, never a bare red.
Invalid-config runs degrade the same way (combined error; summary write
skipped when the dir isn't runner-owned). `rmtree` still fires only on a dir
carrying the full runner `summary.json` key set. **F1 (Medium) closed as
verified-fixed; flag REV2-001 (#10) closed.**

**(c) `output` escape — verified end-to-end.** `_relative_output` validation
(non-empty string; NUL/CR/LF rejected — no `GITHUB_OUTPUT` line injection;
relative; no `..`; not `.`), default `"gallery"`; example config carries the
key and stays valid + inactive; `cmd_ci` re-roots the gallery at the
configured path and writes `output=<path>` to `GITHUB_OUTPUT` on every branch
before any exit; shim v3 tags the run step `id: visual_qa` and uploads
`${{ steps.visual_qa.outputs.output || 'gallery' }}/` (fallback covers a
runner crash before the write); `ensure_workflows` numeric compare (v2 < v3)
refreshes seeded shims and leaves unmanaged ones alone; distribution tests pin
the exact marker + upload-path strings. Implemented exactly as ratified.

### F7 · Low · note — collision/neutral artifact still ships fork-owned `gallery/` content
On the two paths where the runner produced nothing but the fork tracks
`gallery/` (unconfigured-neutral, and the collision red), `output=gallery` has
already been written, so the `if: always()` upload publishes the fork's own
tracked content under `visual-qa-gallery` for the retention period. Residual
sliver of F1's original complaint — harmless (repo-visible content) but
confusing and wasteful. Cheap fix: append a second `output=` line pointing at
a runner-owned empty path once it's known nothing was produced
(`GITHUB_OUTPUT` is last-write-wins) — or ratify as accepted.

**Notes (no verdict change):**
- Spec text still lacks an `output` row in §Fork config's schema and a
  tracked-`gallery/` row in the edge-case table — spec-hygiene for the
  freeze, same bucket as F2/F4/F5.
- `detect_init_config` scaffold omits `output` (valid — default applies)
  while the example lists it. Key-surface inconsistency, cosmetic.
- Neutral runs no longer upload any artifact (nothing is written;
  `if-no-files-found: warn`). The spec requires the upload *step* to always
  run, not that neutral runs produce files — conformant; noted as a behavior
  change from the pre-fix build.
- F3 (shim length): +2 lines (now ~66); verdict unchanged.

**Corrections to the original pass (clerical, mine):** the pre-fix test count
was 27 + 11, not "28 + 11"; and the totals line miscounted — the table's 46
rows summed to 35 as-specced · 6 deviated-intentionally · 3 deviated-silently
· 2 unimplemented, not 36/6/3/2.

**Revised totals:** 46 judged — 35 as-specced · 7 deviated-intentionally ·
2 deviated-silently · 2 unimplemented. **Findings open: 0 Major · 0 Medium ·
6 Low** (F2–F7; F1 resolved).

**Verdict: F1 surface conformant.** Fix unit 3 implements RC6 exactly as
ratified; no Medium remains. The freeze still owes the spec-text hygiene
(F2/F4/F5 + the two notes above) and the ship-gated feature doc (F6).
