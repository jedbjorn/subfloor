-- 0232 — seed Developer-owned maintained harness promotion.
-- The source asset ships with this migration; the Developer flavor grant makes
-- the workflow available in existing and freshly rebuilt downstream forks.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'harness_promotion',
  'Promote an installed harness CLI release from detected or best-effort to the engine''s maintained tested baseline. Use when the FnB asks to qualify and publish support for a newer Claude, Codex, OpenCode, Vibe, Kimi, or DeepSeek runtime. Do not use for ordinary model discovery.',
  'craft',
  NULL,
  0,
  '# harness_promotion — qualify + publish a harness release

Developer-owned maintainer workflow for the super-coder source repository.
Outcome = one exact observed harness build passes the current adapter contract
on every claimed surface, its tested metadata is updated without becoming an
admission gate, and a review PR carries the evidence downstream forks need.

## Boundaries

- Run only when `git ls-files --error-unmatch .super-coder/schema.sql` exits 0.
  A tracking fork consumes the result through `sc update`; never edit its
  materialized `.super-coder/` tree.
- Require the FnB to name the harness + target release. Updating harnesses,
  rebuilding a sandbox image, spending provider tokens, restarting sessions,
  and merging remain separate operator authorities.
- Promote an exact runtime build, not a semantic-version guess. Preserve the
  complete first line emitted by `<harness> --version`; wrapper/vendor/build
  text is part of the evidence.
- Preserve Decision #219: `tested` / `best-effort` is support metadata. A
  locally evidenced best-effort route remains admissible and fails once at
  runtime without fallback.
- Never promote only to clear a warning. A green unit suite with no native
  canary is not maintained-version evidence.

## Classify the request first

| Observation | Action |
|---|---|
| New model appears in a local Codex cache, Kimi config, connected OpenCode provider, DeepSeek Host config, or Claude alias | Refresh + resolve it. No engine edit when existing evidence and transport contracts pass. |
| Model appears only in a public catalogue | Keep it advisory; public presence does not prove local account availability. |
| Operator wants a new default/static floor or the model needs a new provider/effort schema | Stop this workflow and scope that separate behavior change. |
| Installed harness version is not the maintained exact build | Continue with the harness promotion below. |

Pass for an ordinary new model = `sc models refresh`, `sc models list
<harness>`, and `sc models resolve <harness> <selector> [--effort <level>]
--json` return fresh exact local evidence. Omit effort for Vibe or a harness
default. Do not add the model ID to source merely because it is new.

## 1. Bind the target runtime

Load `git` + `surface_catalogue` before edits. Sync, branch, then read the
adapter, model catalogue, version probe, and focused tests the map identifies.
Read active decisions bearing on the harness; honor or explicitly supersede
them.

Run `sc harness-status` against the runtime shells actually use. Pass = the
named harness reports one non-empty exact observed version and the runtime seat
is explicit. A stopped sandbox, host/sandbox mismatch, unavailable executable,
or unexpected wrapper is not promotion evidence.

`sc update-harnesses` refreshes every managed harness and rebuilds the image;
running sandboxes keep the old image until restart. Run it only when the FnB
authorized that refresh window. Never restart active sessions from this skill.

Record before editing:

- harness + complete observed version line;
- runtime identity (host/sandbox + container/host identity);
- official distribution/release reference;
- engine commit + adapter contract version;
- current compatibility/support state.

Pass = a reviewer can identify the exact binary and source contract without
using a mutable `latest` label.

## 2. Prove local route evidence

Run:

```bash
sc models refresh
sc models list <harness>
sc models resolve <harness> <selector> [--effort <supported-level>] --json
```

Use a locally authoritative route: installed Claude aliases, Codex model
cache, Kimi configured aliases, connected OpenCode provider projection, or
configured DeepSeek Host route. Exercise every advertised effort/variant that
the adapter will expose, including `default` where supported. Omit effort for
Vibe or a harness default. Pass = a controlled binding is fresh,
exact-versioned, and carries one source fingerprint/evidence digest; an
uncontrolled binding is explicitly typed with null effort; unsupported values
still fail before dispatch.

If the target changed a cache/config/provider schema, transport flag, event
shape, or capability surface, patch the adapter and add a realistic regression
before promotion. Never widen a parser, discard unknown fields, or downgrade a
required source merely to admit the release.

## 3. Run the native current-version canary

Inspect the target adapter''s declared `surfaces`. Exercise every `true` surface
against the exact target binary:

- terminal launch when declared;
- one-shot with an exact locally available model/effort when declared;
- managed Browser start + one response + exact re-entry when declared;
- one disposable Sprint participant through terminal completion when
  declared.

Use the repository''s harness-specific live canary when one exists. Add a
bounded canary when the claimed surface has no executable proof; deterministic
unit adapters do not replace a native run. Require explicit FnB approval before
any provider-token turn. Use a minimal non-sensitive prompt, create no useful
project changes, and retain only a sanitized receipt.

Each surface passes only when:

- the response came from the requested model/effort or declared uncontrolled
  default;
- start/resume/session identity follows the adapter contract;
- structured events, interruption/reconciliation, permissions, and tool
  boundaries used by that surface remain parseable;
- exactly one prompt dispatch occurred; no fallback or changed-effort retry
  occurred;
- cleanup removed disposable conversations/Sprints/worktrees without touching
  operator work.

Any unexercised claimed surface, fallback, duplicate dispatch, protocol drift,
credential exposure, or cleanup failure -> do not promote. Report the failing
boundary and leave the release `best-effort`.

## 4. Publish the tested identity

Change only the evidence the canary earned:

1. In `.super-coder/adapters/<harness>/adapter.json`, set the appropriate
   `verified_cli_version` to the exact parsed release. Preserve the capability
   minimum unless evidence proves it changed. Move
   `maximum_cli_version_exclusive` only far enough to contain the target under
   the harness''s established release-line policy; it remains diagnostic, not
   an admission gate.
2. In `.super-coder/scripts/harness_versions.py`, set
   `MAINTAINED_OBSERVED_VERSIONS[<harness>]` to the complete observed line.
   For a manifest-owned prerelease identity such as DeepSeek, update
   `verified_observed_version` consistently.
3. Update an exact package pin/runtime manifest only for a harness whose
   distribution is deliberately pinned. Latest-resolving installers remain
   unpinned.
4. Update literal current-version expectations and add negative coverage for
   the displaced version, newer versions, prereleases, custom wrappers,
   missing binaries, and non-semver output. Preserve best-effort admission
   tests.

Do not change flavor defaults, static model floors, model aliases, supported
efforts, or provider manifests unless the FnB separately requested and the
native evidence proved that behavior.

## 5. Verification + handoff

Run the repository-declared Python test mechanism over at least:

```text
tests/test_harness_versions.py
tests/test_conversation_adapters.py
tests/test_conversation_capabilities.py
tests/test_model_catalog.py
tests/test_route_bindings.py
tests/test_conversation_release_gate.py
```

Then run the repo''s full verification hook. Pass = focused tests + full hook
are green, simulated untested versions remain `best-effort`, the exact target
reports `verified`/`tested`, and native receipts still name the target binary.

Commit + push + open a PR; never merge from this workflow. Put this evidence
in the PR:

- exact observed version + runtime identity;
- official release reference;
- local route/effort matrix;
- native surface canary matrix + sanitized receipt locations;
- focused/full test commands + results;
- manifest/version/pin changes;
- explicit statement that model admission remains evidence-driven and
  untested harness versions remain best-effort.

After FnB-approved merge, the operator reconciles/restarts the source engine.
Forks receive the maintained baseline through their normal `sc update` and
runtime rebuild/restart boundary.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'dev', skill_id FROM skills
WHERE name='harness_promotion' AND is_deleted=0;

COMMIT;
