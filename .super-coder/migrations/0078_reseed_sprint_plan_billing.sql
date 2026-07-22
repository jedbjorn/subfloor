-- 0078 — require plan billing for sprint worker launches by default.
--
-- The sprint planner must not treat a callable model route as permission to
-- incur API or Extra Usage charges. The source asset is authoritative; this
-- idempotent replacement carries the policy to existing installs while fresh
-- builds converge with the same asset.

BEGIN;

UPDATE skills SET content = replace(content,
'**The model & provider interview — exactly two questions to the FnB:**',
'**The model & provider interview — two routine routing questions to the FnB:**')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'2. **Reviewers** — which harness and model? One answer; every reviewer
   runs it.

Flavor-uniform by design:',
'2. **Reviewers** — which harness and model? One answer; every reviewer
   runs it.

**Billing gate — Plan billing only by default.** Do not research provider docs
during a sprint. Before resolving models, run the chosen harness''s preflight
exactly:

```sh
# OpenAI / Codex: ignore the per-run API override; persisted auth must be ChatGPT.
test "$(env -u CODEX_API_KEY codex login status 2>&1)" = "Logged in using ChatGPT"

# Anthropic / Claude: ignore the API override; require first-party plan auth.
env -u ANTHROPIC_API_KEY claude auth status --json |
  python3 -c ''import json,sys; s=json.load(sys.stdin); raise SystemExit(0 if s.get("loggedIn") and s.get("authMethod") == "claude.ai" and s.get("apiProvider") == "firstParty" and s.get("subscriptionType") and not s.get("apiKeySource") else 1)''
```

Exit 0 = auth gate passed. Any other result -> hold the route and ask the FnB
to correct the login. Keep the API override scrubbed on EVERY launch:

```sh
env -u CODEX_API_KEY ./sc run <shell> --harness codex -m <model> --effort high
env -u ANTHROPIC_API_KEY ./sc run <shell> --harness claude -m <model> --effort high
```

CLI auth cannot see account-side overage controls. The standing FnB setup for
plan-only sprints is: Anthropic **Settings -> Usage -> Usage Credits disabled**;
OpenAI **Codex Settings -> Usage -> Auto top-up off and flexible-credit balance
0**. Once the FnB establishes these account invariants, planners run the gates
above — they do not research or re-check web settings every sprint. A nonzero
OpenAI flexible-credit balance, enabled Anthropic Usage Credits, API billing, or
any exception requires explicit FnB permission recorded with its scope in the
sprint doc before the affected run. Choosing a harness/model is not permission.
No standing invariant or recorded exception -> do not launch that route.

`sc models resolve` proves callability, not billing; run it only after this gate.

Flavor-uniform by design:')
WHERE name='sprint_orchestration';

COMMIT;
