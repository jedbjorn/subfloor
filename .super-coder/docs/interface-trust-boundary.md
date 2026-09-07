---
title: Browser trust boundary
tags: [subfloor, browser, security]
date: 2026-09-07
project: subfloor
purpose: Current browser authority and reach
---

# Browser trust boundary

## Applicability

**Posture: current architecture reference.** Applies to the current Review GUI,
Chats and Sprint Board. The former Interface browser-session bootstrap,
operator-token cookie and writer-lease description is superseded; those are
not the current conversation API contract.

Source owners are `api/server.py`, `api/conversation_routes.py`, and the
conversation broker/adapter modules under `scripts/`. The user workflow is in
[Browser conversations](../../docs/README.md#browser-conversations).

## Local operator and shell authority

The GUI assumes a trusted local operator and a loopback deployment. It is not
a multi-user access-control boundary against other processes running as that
operator. Do not expose its port directly to untrusted users or networks.

Conversation routes identify the active local operator and reject shell bearer
credentials as browser authority. Conversation requests validate loopback Host;
mutations validate supplied Origin against Host and reject cross-site fetch
metadata. Missing browser provenance headers are accepted by the current local
client contract: do not describe these checks as cryptographic authentication
against arbitrary network clients.

The Sprint Board likewise distinguishes browser-operator actions from
shell-authenticated Sprint commands. Shell commands use scoped bearer identity
and role/lifecycle checks. Browser authority does not replace the required
operator merge grant or a Developer's live merge-authorization proof.

## Network boundary

On the host, server startup refuses a non-loopback bind unless it observes a
container context. Setting `SC_SANDBOX` alone is insufficient. In a container,
the server can bind its container interface; `sc launch` publishes the port on
host `127.0.0.1`.

Container detection does not verify the host's publish mapping or establish a
separate network namespace. Use the supported launcher. A custom container
with host networking or a wider published port does not inherit the documented
loopback boundary. Remote access needs a separately authenticated transport.

The server supplies a restrictive Content Security Policy and serves its static
UI locally. Same-origin script execution can exercise operator authority;
content sanitization and avoiding credential exposure remain necessary.

## Conversation data and ownership

The browser receives normalized conversation projections and events. Native
harness session/run references and harness credentials remain server-side.
Queued user messages are durable before dispatch; adapters start or resume the
recorded native session, and the broker records the terminal result.

CLI and browser sessions cannot own the same shell concurrently. Close waits
for terminal proof before releasing ownership. Reopening an eligible ordinary
chat requires route and ownership validation; Sprint-scoped chats remain
closed. Browser refresh reads the bounded transcript and event stream rather
than creating another native session.

## Private engine state

The live DB, snapshot and backups use the private XDG instance namespace.
Ordinary downstream shells operate through API-backed surfaces and a restricted
execution view. Admin owns direct maintenance and recovery. See the
[engine state reference](../README.md#source-dependency-and-private-state).

The host service runs as the installing user. Rootless Docker maps container
root to that user; rootful Docker access has the host privilege implications
of Docker itself. Optional host brokers retain their own credentials and
configured scope. None of this promises isolation from a hostile host owner.
