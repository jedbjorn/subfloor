# quota_probes fixtures

Recorded payload shapes for the three provider quota endpoints, used by
`tests/test_quota_probes.py`. **No test in this suite performs a live call** —
`urlopen` is stubbed to fail loudly if one is attempted.

| File | Endpoint |
|---|---|
| `anthropic_usage.json` | `GET api.anthropic.com/api/oauth/usage` |
| `openai_usage.json` | `GET chatgpt.com/backend-api/codex/usage` |
| `moonshot_usages.json` | `GET api.kimi.com/coding/v1/usages` |

## Provenance — these are real captures

All three were captured live from this host's own credentials on **2026-07-25**
(spec doc #57), each by issuing exactly the request its probe issues and keeping
the response body verbatim.

| File | How it was taken |
|---|---|
| `anthropic_usage.json` | `GET` with `Authorization: Bearer <accessToken from ~/.claude/.credentials.json>` and `anthropic-beta: oauth-2025-04-20`. |
| `openai_usage.json` | `GET` with the token and `chatgpt-account-id` from `~/.codex/auth.json`, **plus the codex client identity** (`User-Agent: codex_cli_rs/0.145.0`, `originator: codex_cli_rs`, `version: 0.145.0`). Without those three headers the endpoint answers `403` with an HTML anti-bot page and there is no payload to capture at all — flag #196. |
| `moonshot_usages.json` | `GET` with the access token from `~/.kimi-code/credentials/kimi-code.json`, captured inside the **900-second** window after the operator booted Kimi. No refresh was performed to obtain it, and none may be: Kimi rotates its refresh token on every refresh, so a refresh by anything other than the Kimi CLI strands the CLI with an invalidated token and signs the operator out of the provider this panel monitors. To re-capture: ask the operator to boot Kimi, then capture within 15 minutes. |

**They replaced transcriptions.** The previous fixtures were written from spec
doc #49's observed-field tables rather than from the wire, because the sprint
task scoped that unit to "capture fixtures from the spec's documented response
shapes" rather than re-probe. Where those tables were wrong the probes were
built wrong and every test agreed with them (flag #198). Six field-level
defects — four in moonshot, one in openai, one in anthropic — were found only by
capturing, and two of them were in the payloads nobody suspected.

### Sanitization

Identifiers are replaced with placeholders. **Nothing else is touched** — no
tidying, no normalizing, no filling in of "obviously missing" fields:

| File | Replaced |
|---|---|
| `anthropic_usage.json` | nothing — this payload carries no identifier of any kind |
| `openai_usage.json` | `email`, `account_id`, `user_id` |
| `moonshot_usages.json` | `user.userId`, `parallel.details[]` |

Three properties in particular are **exactly as the wire sent them**, because
each is something a transcription got wrong:

- **enum prefixes** — `"timeUnit": "TIME_UNIT_MINUTE"`, not `"MINUTE"`
- **string-typed counts** — `"limit": "100"`, not `100`
- **nested blocks** — moonshot's `limits[].detail`, openai's
  `additional_rate_limits[].rate_limit`

`openai_usage.json` keeps its (placeholder) `email` key deliberately. The probe
must not propagate an address even when the wire offers one, and a fixture with
the field removed could not tell the difference between a probe that drops it
and a probe that never saw it.

## What drift detection actually covers

An earlier version of this file promised that *"a drift between the two shows up
as an `error` status from a live probe, never as a wrong number."* **That claim
was false and it was disproved in production**: the moonshot probe returned
status `ok` while the operator's card showed a wrong window kind, an empty row,
and a Python dict repr.

The reason is structural. Spec #49's drift rule judges the **envelope** — is
`usage` a dict, is `limits` a list, is `rate_limit` a dict. In that failure every
one of them held. **The divergence was one level below the envelope, in field
names and nesting, and an envelope check does not look there.**

Stated accurately, then:

- **Drift detection catches** an envelope that is absent or of the wrong type.
  That yields `error` with no rows, and it is the case where a probe would
  otherwise report a zero as though it had been measured.
- **Drift detection does not catch** a payload whose envelope is intact and
  whose *contents* have moved — a renamed field, a value that gained a nesting
  level, an enum that gained a prefix.
- **What catches those is these fixtures**, and only to the extent that they
  match the wire. That is why they are captures rather than transcriptions, and
  why re-capturing is the first move whenever a card looks wrong.

A few specific shapes are pinned directly by the suite — moonshot's `detail`
unwrap and `TIME_UNIT_` prefix, openai's nested
`additional_rate_limits[].rate_limit` — because those have already broken once.
That is a list of known hazards, not a schema. Spec #57 deliberately declined to
build a general payload validator: it would carry its own failure modes and
would still only know what a capture had told it.
