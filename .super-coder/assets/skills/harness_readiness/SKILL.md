---
name: harness_readiness
description: Read Subfloor harness/model support states, refresh the supplied local evidence, run bounded compatibility checks, and prepare an exact upstream handoff when the installed runtime is unqualified. Developer-only.
category: substrate
common: false
---

# harness_readiness — qualify the installed route

Subfloor reports maintained harness support as `tested`, `best-effort`, or
`newer-unverified`. These states describe source evidence; they do not hide a
locally discovered model or silently substitute another route.

## Read the supplied evidence

```bash
sc harness-status
sc models refresh
sc models list <harness>
sc models resolve <harness> <selector> [--effort <level>] --json
```

Record the complete version line, active host/container seat, exact selector,
effort, evidence source, digest/fingerprint, and resolve result. Pass = list and
resolve agree on the same fresh local route. A public model absent from local
evidence remains unavailable for that account; an unsupported effort fails
before dispatch.

## Use the smallest available compatibility check

When the FnB authorizes a provider call or harness refresh, exercise the exact
installed model/effort through the fork's declared hook or the adapter's native
one-shot surface. Pass = one request uses the requested route, returns parseable
events and session identity, and performs no fallback or changed-effort retry.

`sc update-harnesses`, sandbox rebuild, provider-token use, and session restart
remain operator-authorized boundaries. A host result does not prove the
container seat, and a passing newer build does not promote the maintained
source baseline.

## Hand source maintenance upstream

Use `issue_reporting` when the installed version or adapter contract remains
unqualified. Include the complete version line, seat and engine commit,
selector/effort, status/list/resolve outputs, sanitized native-check result,
expected versus actual behavior, and the narrow failing boundary.

Tracking forks do not edit materialized `.super-coder/` metadata or adapters.
Pass after a published fix = the exact build reports `tested`, simulated newer
builds remain `best-effort`, and the local route still resolves from fresh
evidence after the authorized update/restart.
