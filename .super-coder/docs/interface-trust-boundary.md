---
title: Interface trust boundary & browser sign-in
tags: [super-coder, interface, security, browser, auth]
date: 2026-07-24
project: super-coder
purpose: What the Interface defends against, why the browser is signed in automatically, and who owns the running service
---

# Interface trust boundary & browser sign-in

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/.super-coder/docs/interface-trust-boundary.md)

## The short version

Open the Interface tab and it works. There is nothing to paste, no sign-in
step, and no credential to keep anywhere.

That is deliberate, and this page is the reasoning — because "it just opens"
is exactly the sentence that should make you ask what is being trusted.

## What Subfloor treats as trusted

**The personal machine and the people who use it** (decision `#29`).

Subfloor is a personal-machine tool. The expected deployment is one user, or
two or three mutually trusted family members. Under that model these are
**not** adversaries and Subfloor does not try to isolate itself from them:

- other processes running as you,
- other same-UID shells,
- other trusted local accounts on the same machine.

That is not a concession — it is a statement of what the tool is for. Software
already running as you can reach your repositories, your harness credentials,
your terminal processes, and every loopback service you run. A lock on the
Interface would not change that; it would only add friction for the owner.

**If untrusted people use your machine, do not run Subfloor on it.** That is
the boundary, stated plainly, rather than a control that implies a protection
it cannot deliver.

## What Subfloor defends against

The web, and the network — in full, and these controls are release-critical:

| Threat | Control |
|---|---|
| A webpage on another origin scripting your Interface | Exact same-origin `Origin` **and** `Sec-Fetch-Site: same-origin` on bootstrap; `SameSite=Strict` cookie; `X-CSRF` on every mutation; CORS stays off |
| Cross-site request forgery | `SameSite=Strict` + an anti-forgery token held only in page memory |
| DNS rebinding | Exact `Host` allowlist (`127.0.0.1` / `localhost`), every route |
| Accidental network exposure | Reachable from the host's loopback interface only — see [the exact guarantee](#the-bind-guarantee-exactly) |
| Credential leakage from the browser | The browser is never given a credential to leak — see below |
| Remote clients | No route in; reach it through your own secure transport (e.g. a tailnet) or not at all |

### The bind guarantee, exactly

Because the browser session mints automatically, the *only* thing standing
between a remote client and Interface authority is that the remote client
cannot reach the port — a network client able to choose its own `Host` and
`Origin` headers passes every other fence. So it is worth being precise about
what enforces that, rather than restating "binds loopback only":

- **On your host** (`./sc serve`, the supervised stack): the server checks
  `SC_BIND` at startup and **exits** unless it is a loopback address. Setting
  `SC_BIND=0.0.0.0` does not widen the Interface; it stops it booting.
- **Inside the sandbox container**: the bind *is* `0.0.0.0`, deliberately, so
  docker can publish the port — and the boundary is the published mapping,
  `-p 127.0.0.1:PORT:PORT`, which is loopback-only on the host whatever the
  container binds. The in-process guard stands down there (it detects
  `SC_SANDBOX`) because refusing would break `./sc launch` without removing
  any exposure.

Net: reachable from the host's loopback interface only, enforced in-process
off-sandbox and by docker's port mapping in it. Neither path exposes the
Interface to the network, and neither claims more than it does. Remote access
is a separate authenticated boundary you put in front (e.g. a tailnet) — never
a wider bind.

Same-origin script execution is operator-equivalent here. The restrictive CSP,
the vendored scripts, the sanitized rendered content, and the absence of any
third-party runtime asset are therefore security controls, not hygiene — an
XSS bug in the Interface is a security defect, not a rendering defect.

## Why the browser is not given the operator capability

The Interface API has an operator capability: `.super-coder/run/interface/
operator.token`, mode `0600`, provisioned at server boot. The **CLI** uses it.
The **browser never sees it.**

An earlier build asked the operator to paste that token into a browser prompt.
That defended against a local process self-minting browser authority — an
actor the trust boundary above excludes — and it did so by putting a
long-lived filesystem capability into page JavaScript, where an XSS bug could
exfiltrate it. It bought protection from a non-adversary at the cost of a real
leak path (decisions `#29`, `#30`).

So the browser presents nothing, and receives nothing durable:

1. The Interface tab POSTs to `/api/interface/browser-sessions` with no
   `Authorization` header. A bearer sent from browser code is **refused**, so
   page script cannot start carrying one.
2. The server checks provenance the browser itself vouches for and a foreign
   page cannot forge: exact allowed `Host`, exact same-origin `Origin`,
   `Sec-Fetch-Site: same-origin`. Anything missing or cross-site is `403`.
3. It mints a scoped session, returned **only** as an `HttpOnly`,
   `SameSite=Strict`, `Path=/` cookie (plus `Secure` if you front it with
   HTTPS). `HttpOnly` means page script cannot read it either.
4. The anti-forgery token comes back in the response body, lives in page
   memory, and is sent as `X-CSRF` on every mutation.

The session is scoped to the Interface. It is not an operator capability and
does not become one: viewer versus writer authority still comes from the
writer-lease protocol, streams still need single-use generation-bound tickets,
and CLI and hook capabilities are unchanged.

## Session lifecycle

Browser sessions are **live-process state only** — never the DB, a snapshot,
rendered state, or a log.

- **24-hour inactivity deadline.** Authenticated use advances it; past it the
  server deletes the session and answers `401 browser_session_expired`.
- **Rotation revokes.** Every bootstrap mints a new identifier and a new
  anti-forgery token, and destroys the session the caller presented in the
  same step. A request still in flight under the revoked identifier is
  re-checked at dispatch and answered `401` rather than completing — the one
  exception being a handler already executing, which finishes under the
  authority it started with.
- **A service restart invalidates every session.** That is the normal recovery
  path, not a fault.
- **One silent retry.** On the first `401` the UI bootstraps once and retries
  the original request with its original idempotency key — so recovery cannot
  duplicate a mutation. A second failure surfaces the error; it never loops.
- **Cookies blocked?** The Interface says browser sessions are unavailable. It
  does not fall back to a pasted token, because there is nothing to fall back
  to.

Several browsers may hold sessions at once. They all get the same scope and no
distinct identity; only the writer-lease holder can send input.

## Who owns the running service

The supervised service runs **as the installing user, with no elevated host
privilege**:

- The sandbox container runs under your uid/gid — or, under rootless Docker,
  as a namespace root that maps to your host user. Either way nothing it
  writes to the bind-mounted repo lands root-owned.
- Optional brokers install as `systemctl --user` units. User-level systemd, no
  system units, no root.
- The engine never invokes `sudo`. The only `sudo` in Subfloor is *printed
  advice* during one-time host setup (`./sc doctor`) for installing Docker
  itself — an OS package step you perform, not something the service does.
- The published port is bound to `127.0.0.1` only.

One caveat worth stating rather than hiding: on **rootful** Docker, membership
in the `docker` group is effectively root-equivalent on the host. That is a
property of Docker, not of Subfloor, and it is the reason `./sc doctor` offers
rootless Docker first.

There is no service account, no privilege broker, no OS keychain, and no
per-user identity inside Subfloor — by design. Adding them would imply
isolation between local users that the trust boundary above explicitly does
not claim.

## Related

- `./sc token` — prints the **Review GUI** sign-in credential (the Admin
  runtime credential under `.super-coder/run/mem/`). That is a different
  surface from the Interface browser session described here, and it is
  unaffected.
- Decisions `#29` (browser mints a scoped same-origin session; the operator
  capability stays out of browser JavaScript) and `#30` (the personal-machine
  threat-model ruling that set the direction).
