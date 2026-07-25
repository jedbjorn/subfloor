# quota_probes fixtures

Recorded payload shapes for the three provider quota endpoints, used by
`tests/test_quota_probes.py`. **No test in this suite performs a live call** —
`urlopen` is stubbed to fail loudly if one is attempted.

## Provenance — read this before trusting a field

These files are transcribed from the **observed field tables in spec doc #49**
(`## Proven Endpoints`), which record what the three endpoints returned when
PLN2 called them live against this host's credentials on 2026-07-25 (decision
#65). They are not raw captures: the live calls were made before this unit
existed and their bodies were not vendored, and the sprint task explicitly
scoped unit 2 to "capture fixtures from the spec's documented response shapes"
rather than re-probe.

So: every field the probes *depend on* is a recorded observation, but the
surrounding envelope (nesting of `additional_rate_limits`, the exact value
formats) is a faithful reconstruction. Values are placeholders — no real
account label, id, or token appears here.

The consequence, stated plainly so it is not discovered as a surprise: these
fixtures pin the probes against the spec's reading of the payloads, not against
the wire. A drift between the two shows up as an `error` status from a live
probe, never as a wrong number — which is the stance spec #49 adopts for all
three endpoints.

| File | Endpoint |
|---|---|
| `anthropic_usage.json` | `GET api.anthropic.com/api/oauth/usage` |
| `openai_usage.json` | `GET chatgpt.com/backend-api/codex/usage` |
| `moonshot_usages.json` | `GET api.kimi.com/coding/v1/usages` |
