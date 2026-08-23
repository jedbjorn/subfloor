-- 0232 — seed Developer-owned fork harness readiness workflow.
-- The source-maintainer promotion workflow remains fork-local to the upstream
-- instance; this skill helps tracking forks diagnose and report exact evidence.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'harness_readiness',
  'Assess newer or unverified harness versions and newly discovered models in a tracking fork. Use when model status is best-effort, newer-unverified, missing, or unexpectedly unresolved. Refresh local evidence, separate harmless discovery lag from adapter incompatibility, and prepare an actionable upstream handoff without editing the materialized engine.',
  'craft',
  NULL,
  0,
  '# harness_readiness — diagnose local support evidence

Developer-owned workflow for a super-coder tracking fork. Outcome = the local
harness and model route are either proven usable from fresh authoritative
evidence, or an exact upstream maintenance request explains what must change.

## Boundaries

- Treat `tested`, `best-effort`, and `newer-unverified` as support metadata,
  not an admission gate. A locally evidenced route remains selectable and
  fails once at runtime without silent fallback.
- Never edit a tracking fork''s materialized `.super-coder/` files. Maintained
  version metadata and adapter fixes are published by the source maintainers
  and arrive through the normal `sc update` boundary.
- Require operator approval before `sc update-harnesses`, a sandbox rebuild,
  a provider-token call, a session restart, or any destructive cleanup.
- Never add a public model ID to local source merely because it exists. Local
  account evidence is authoritative for availability.

## 1. Classify the warning

Run:

```bash
sc harness-status
sc models refresh
sc models list <harness>
sc models resolve <harness> <selector> [--effort <level>] --json
```

Omit effort when the harness or route does not support it. Record the exact
observed version line, runtime seat, selector, effort, evidence source,
fingerprint or digest, and resolution result.

Classify the result:

| Observation | Meaning | Next action |
|---|---|---|
| New model resolves from fresh local cache/config/provider evidence | Discovery is working | Use it; no engine change is required |
| Model exists publicly but is absent locally | Account or rollout mismatch | Keep it unavailable; refresh later |
| Model is local but effort/variant is rejected | Route contract mismatch | Check the advertised capability and report exact evidence upstream |
| Harness build is newer than the maintained exact build | Compatibility is unqualified | Continue with the bounded checks below |
| Harness executable is missing or runtime seat differs from shell use | Environment fault | Repair the environment before judging compatibility |

Pass for ordinary model discovery = list and resolve agree on one fresh local
source, the selector is exact, supported effort is preserved, and unsupported
effort still fails before dispatch.

## 2. Check the installed runtime without self-promoting it

Inspect the adapter manifest and the version status output. Compare:

- the complete observed `<harness> --version` first line;
- parsed release and maintained exact build;
- minimum and diagnostic maximum compatibility bounds;
- adapter-declared capabilities used by this fork.

Do not treat a matching semantic-version prefix as an exact match. Wrapper,
vendor, channel, and build text are part of the evidence. Do not change local
metadata to make the warning disappear.

If the operator authorizes a harness refresh, run `sc update-harnesses`, then
rebuild/restart only at the separately approved boundary. Re-run all four
commands from step 1 against the runtime shells actually use; a host result
does not prove a sandbox result.

## 3. Run a bounded native smoke check when authorized

Use the fork''s declared development hook or the adapter''s smallest native
one-shot path with a non-sensitive prompt. Exercise the exact local
model/effort being assessed. Pass only when:

- the requested model/effort produced the response;
- one request was dispatched with no fallback or changed-effort retry;
- response events and session identity remained parseable;
- no useful project changes or credentials entered the receipt.

A passing smoke check is useful compatibility evidence, but it does not turn a
new harness build into the maintained source baseline. That promotion belongs
to source maintainers because it must be proven across every claimed adapter
surface and shipped with regression coverage.

## 4. Resolve locally or hand off upstream

Close the local investigation without an upstream change when a new model is
freshly discovered and resolves under the existing adapter contract. Record
the exact selector, effort, source, digest, and observed harness version.

When source maintenance is required, use `issue_reporting` and include:

- harness name + complete observed version line;
- host/sandbox runtime identity and engine commit;
- model selector + effort/variant;
- `harness-status`, list, and resolve outcomes;
- sanitized native smoke result, if authorized;
- expected versus actual behavior and the narrow failing boundary;
- whether the older maintained build still works.

Do not propose widening parsers, discarding unknown fields, weakening source
requirements, or changing defaults without evidence. After maintainers publish
the qualified release, the operator runs the normal `sc update` and approved
runtime rebuild/restart, then repeats step 1. Pass = the exact build reports
tested while simulated newer builds remain best-effort and the local route
still resolves from fresh evidence.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'dev', skill_id FROM skills
WHERE name='harness_readiness' AND is_deleted=0;

COMMIT;
