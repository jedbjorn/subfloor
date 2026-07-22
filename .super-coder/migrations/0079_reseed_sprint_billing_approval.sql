-- 0079 — observe sprint billing auth; preserve approved metered credentials.
--
-- 0078 scrubbed API-key overrides from every worker launch, which made its own
-- explicit-FnB-permission exception impossible to exercise. Replace that block
-- with read-only classification plus a scoped approval record. Source asset and
-- this idempotent delta converge fresh builds and existing installs.

BEGIN;

UPDATE skills SET content = replace(content,
'**Billing gate — Plan billing only by default.** Do not research provider docs
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

`sc models resolve` proves callability, not billing; run it only after this gate.',
'**Billing gate — Plan billing by default; observe, never mutate auth.** NEVER
unset, scrub, replace, or print a credential. Before resolving models, classify
the chosen harness exactly:

```sh
# OpenAI / Codex: exit 0 = plan; 10 = API override; 11 = persisted auth unknown.
(
  if [ -n "${CODEX_API_KEY+x}" ]; then
    echo "billing=api source=CODEX_API_KEY"; exit 10
  fi
  status="$(codex login status 2>&1)"
  if [ "$status" = "Logged in using ChatGPT" ]; then
    echo "billing=plan source=ChatGPT"; exit 0
  fi
  echo "billing=api-or-unknown source=persisted-login"; exit 11
)

# Anthropic / Claude: exit 0 = plan; 10 = API key; 11 = unknown auth.
claude auth status --json 2>/dev/null |
  python3 -c ''import json,sys
try: s=json.load(sys.stdin)
except Exception: print("billing=unknown"); raise SystemExit(11)
key=s.get("apiKeySource"); plan=s.get("loggedIn") and s.get("authMethod") == "claude.ai" and s.get("apiProvider") == "firstParty" and s.get("subscriptionType") and not key
print("billing=plan source=claude.ai" if plan else ("billing=api source=" + str(key) if key else "billing=unknown")); raise SystemExit(0 if plan else (10 if key else 11))''
```

Exit 0 + `billing=plan` -> launch normally. Exit 10 -> hold and ask the FnB to
authorize the metered route. Exit 11 -> hold until the FnB corrects the login or
explicitly authorizes the unknown route. Model/harness selection is not billing
permission.

Ask in the planner turn, then stop before booting the worker:

```
Billing approval required: provider=<openai|anthropic> mode=<api|extra-usage> route=<harness/model> scope=<shell/unit/role/sprint> cap=<amount|provider limit|not specified> expires=<one launch|time|sprint close>. Authorize this metered run?
```

Only an explicit affirmative FnB reply counts. Silence, prior model selection,
or an approval for another provider/scope does not. Default scope = one launch;
broader authority must be stated explicitly.

Record an approval before launching:

```
billing-exception: provider=<openai|anthropic> mode=<api|extra-usage> scope=<role, unit, or whole sprint> cap=<amount or provider limit> expires=<time or sprint close> approved-by=FnB
```

After approval, run the ordinary resolved `./sc run ...` command with the
current environment unchanged; this preserves the credential the FnB approved.
No matching, unexpired approval -> do not launch the metered route.

CLI auth cannot see account-side overage controls. Do not claim Extra Usage was
validated. If the provider reports an included-plan limit or offers paid
continuation, hold and request the same scoped approval. Automatic overage is an
FnB-owned account policy: treat it as permission only when the sprint doc records
its scope/cap/expiry; otherwise the FnB keeps it disabled for plan-only sprints.

`sc models resolve` proves callability, not billing; run it only after this gate.')
WHERE name='sprint_orchestration';

COMMIT;
