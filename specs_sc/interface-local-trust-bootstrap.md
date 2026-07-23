---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Interface chats and interactive planner wake
roadmap_status: in_progress
frozen: false
title: Interface Local Trust
tags: [interface, security, browser]
date: 2026-07-23
project: super-coder
purpose: Remove browser operator-token exchange
---

# Interface Local Trust And Browser Bootstrap

## Objective

Make the Interface open normally on a trusted personal machine without asking
the operator to copy a filesystem capability into the browser.

Done means a fresh same-origin Interface visit silently receives a scoped
browser session, hostile web origins still cannot read or mutate Interface
state, CLI authority is unchanged, and the instance operator capability never
enters browser JavaScript, browser storage, URLs, clipboard workflow, or HTTP
requests from the browser.

This work starts only after the active Interface sprint and its conformance pass
finish. It is a backlog stage under feature `#14`, not a change to the active
sprint sequence.

## Decisions

- Subfloor is a personal-machine tool. One local user is normal; two or three
  mutually trusted family users are supported. Untrusted local users must not
  share the machine.
- Local processes, same-UID shells, and other trusted local accounts are not
  security principals from which Subfloor isolates itself.
- Hostile web origins, DNS rebinding, accidental non-loopback exposure,
  credential leakage, and cross-site request forgery remain in scope.
- Decision `#29` supersedes decision `#26`: browser bootstrap does not present
  or exchange the mode-`0600` instance operator capability.
- Decision `#18` otherwise remains: CLI uses the instance operator capability,
  browser calls use a scoped same-origin session plus anti-forgery proof,
  streams use single-use generation-bound tickets, and hooks use callback-only
  generation capabilities.
- Keep the browser Interface. Do not build a native desktop application.
- Keep the existing supervised service and sandbox-hosted tmux/harness model.
  The host supervisor runs without elevated host privileges as the installing
  user. No service-account, Unix-socket, keychain, or process-host migration is
  required by this stage.

## Trust Boundary

The trusted boundary is the personal machine and its mutually trusted local
users. Subfloor does not claim protection from malware or hostile code already
executing as a trusted local user. Such code can already reach the user's
repositories, harness credentials, terminal processes, and loopback services.

The web boundary remains strict. A webpage from another origin must not be able
to bootstrap a session, read Interface state, submit terminal input, acquire a
writer lease, stop a chat, or mint a stream ticket. A remote network client must
not reach the service except through the owner's separately established secure
transport.

Same-origin script execution is operator-equivalent for the Interface. The
existing restrictive CSP, vendored scripts, sanitized rendered content, and
absence of third-party runtime assets are therefore release-critical controls.

## Runtime

The existing localhost service continues to own the review UI, Interface API,
input broker, and stream endpoint. It is started and supervised through the
existing CLI and user-owned host runtime. Opening a webpage never starts an
unsupervised engine process.

The harness, private tmux server, input ordering, lifecycle hooks, planner wake,
and watched-PR coordinator remain inside the existing Linux sandbox boundary.
This stage changes browser authentication only. It does not move harness
execution onto the host or change the one-generation input ordering contract.

The service binds only to the configured loopback boundary and keeps its exact
Host allowlist. Subfloor does not create per-Unix-user browser identities or
authorization records. Concurrent browsers are separate clients, while the
existing viewer and writer-lease rules continue to serialize writable control.

## Bootstrap Flow

1. The Interface tab calls `POST /api/interface/browser-sessions` when it has no
   usable browser session.
2. The endpoint requires an exact allowed `Host`, an exact same-origin `Origin`,
   and `Sec-Fetch-Site: same-origin`. Missing, cross-site, or malformed browser
   provenance fails with `403`.
3. The endpoint requires no `Authorization` header and rejects an operator
   bearer supplied by browser code.
4. The server atomically replaces any browser session named by the existing
   cookie and mints a random scoped session plus a new anti-forgery token.
5. The session identifier is returned only as an `HttpOnly`,
   `SameSite=Strict`, `Path=/` cookie. HTTPS deployments also set `Secure`.
6. The anti-forgery token is returned in the response body, held only in
   JavaScript memory, and supplied as `X-CSRF` on every Interface mutation.
7. The UI never prompts for, reads, stores, logs, or transmits
   `.super-coder/run/interface/operator.token`.

The operator capability remains mode `0600` and available to the server and CLI.
CLI calls retain their bearer contract. Hook and stream capabilities remain
narrower and unchanged.

## Session Lifecycle

Browser sessions remain live-process state and never enter the durable DB,
snapshot, logs, or rendered state. Each session has a 24-hour inactivity
deadline. Successful authenticated use advances the deadline; expiry deletes
the server-side session and returns `401`.

A service restart invalidates all browser sessions. On the first `401`, the UI
performs one silent bootstrap and retries the original idempotent operation with
the same idempotency key. A second failure surfaces the error and does not loop.

Every successful bootstrap rotates both the cookie session identifier and the
anti-forgery token, revoking the prior browser session when one was presented.
Server-side cleanup removes expired sessions without a scheduled model or
harness poll.

Multiple browser sessions may coexist. They receive no distinct user identity
or privilege level; viewer versus writer authority continues to come from the
Interface writer-lease protocol.

## API Contract

`POST /api/interface/browser-sessions` is the only unauthenticated Interface
route. It is browser-only and requires the provenance checks above plus an
`Idempotency-Key`.

| Condition | Result |
|---|---|
| Exact same-origin bootstrap | `201`, replacement cookie, anti-forgery token |
| Missing or invalid Host | `403 host_not_allowed` |
| Missing or mismatched Origin | `403 not_same_origin` |
| Cross-site fetch metadata | `403 not_same_origin` |
| Missing idempotency key | `422 idempotency_key_required` |
| Expired browser session on another route | `401 browser_session_expired` |
| Cookie without valid anti-forgery token on mutation | `403 csrf` |

All other Interface reads require the browser cookie or CLI bearer. All other
browser mutations require both cookie and anti-forgery token. CORS remains
disabled. Stream setup continues to require exact Origin and a single-use
session, generation, client, role, and expiry-bound ticket.

## Failure Modes

- **Service restart:** browser state is gone; one silent bootstrap restores it.
- **Session expiry:** the first request gets `401`; one bootstrap and exact
  idempotent retry follows.
- **Cookies disabled:** Interface reports browser sessions unavailable. It does
  not fall back to operator-token paste or an unscoped bearer.
- **Hostile website:** Origin or fetch metadata fails before a session is
  minted. No permissive CORS response is emitted.
- **DNS rebinding:** the exact Host allowlist rejects the request.
- **Operator token missing:** browser Interface still works; CLI operator calls
  fail clearly until the server restores its private capability.
- **Concurrent browsers:** all may view; existing writer acquisition and
  takeover rules decide which client can send input.
- **Same-origin XSS:** treated as a security defect because it can act through
  the scoped browser session. CSP and sanitization tests must fail closed.
- **Non-loopback bind:** service startup refuses unless an explicit future
  remote-access design supplies a separate authenticated boundary.

## Delivery Plan

1. Write the personal-machine trust boundary into the Interface security and
   operator documentation, citing decision `#29`.
2. Change browser-session bootstrap to exact same-origin automatic minting and
   reject browser-supplied operator bearers.
3. Remove the token prompt and all browser token handling; retain one silent
   bootstrap-and-retry path with the original mutation idempotency key.
4. Add browser-session rotation, inactivity expiry, revocation, and bounded
   cleanup while keeping sessions out of durable state.
5. Verify the existing supervisor needs no elevated host privilege and document
   the installing-user ownership contract. Do not restructure the process host
   when the existing runtime already satisfies it.
6. Run the focused authority suite, browser workflow tests, CSP checks, and the
   existing Interface regression suite.

Steps 2 and 4 share the session store and land together. UI step 3 can proceed
in parallel once the response contract is fixed. Documentation and supervisor
verification can proceed in parallel with implementation.

## Verification

- A clean browser opens Interface without a credential prompt or manual setup.
- Browser developer tools, storage, request capture, URLs, and application logs
  contain no instance operator capability.
- Same-origin bootstrap, rotation, expiry, restart recovery, and one bounded
  retry pass in real browser tests.
- Cross-origin fetch, form submission, missing Origin, DNS-rebind Host,
  cookie-only mutation, malformed CSRF, and permissive-CORS probes all fail.
- Two browser clients can view concurrently while only the writer-lease holder
  can submit input.
- CLI operator calls and generation hook calls retain their current authority
  and negative tests.
- Existing byte-fidelity, broker ordering, wake gating, reconnect, stop, and
  recovery tests remain green.
- The supervised host process runs as the installing user without host-root
  privileges; harnesses remain in the sandbox.

The release gate fails if any browser path accepts or exposes the operator
capability, any foreign origin can mint or use a browser session, browser
recovery can duplicate a mutation, or the change weakens writer, stream, hook,
or planner-wake authority.

## Out Of Scope

- Native desktop packaging
- Dedicated service accounts or isolation between trusted local users
- Protection from malware or hostile same-user local processes
- Per-family-member identity, permissions, or audit attribution
- Public network exposure or a new remote authentication system
- Moving tmux, harnesses, or planner wake out of the sandbox
- Unix-domain-socket, OS keychain, or privilege-broker redesign

## Open Questions

None. The deployment boundary, browser authority, runtime ownership, session
lifecycle, recovery behavior, and excluded adversaries are decided above.
