---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
title: Browser-native conversations
tags: [conversations, browser, broker, headless, sessions]
date: 2026-07-29
project: super-coder
purpose: Durable normal browser chat
---

# Browser-native conversations

## Objective

An FnB opens an ordinary shell in the browser, creates or selects a durable
conversation, sends ordered messages, sees truthful working state, and receives
the final response. Harness compute may exit after each turn; the conversation
persists because the engine stores its queue, normalized events, and exact
harness-native session reference.

> [!class1]
> Persistent conversation, ephemeral compute, event-driven re-entry.

A shell has at most one open browser conversation. Browser and CLI ownership are
mutually exclusive and enforced by durable server state.

## Durable model

| Object | Lifetime | Meaning |
|---|---|---|
| Shell | Durable | Identity, permissions, skills, and work surface |
| Conversation | Until closed | One user-visible browser chat |
| Harness session | Harness-owned | Exact native context used for resume |
| Message | Durable | Ordered user prompt or engine result |
| Run | One turn | One leased harness execution attempt |
| Event | Append-only | Replayable normalized browser state |
| Outbox item | Until dispatched | Transactional intent to start a run |

The engine owns queue and delivery truth. Harness transcripts remain
harness-owned projections and recovery evidence; the engine never appends user
messages to them.

## Ownership and lifecycle

| Shell state | Browser action | CLI action |
|---|---|---|
| No open browser conversation | May create one | May start |
| Open idle/waiting/error conversation | May continue or Close | Refuses until Close |
| Open queued/running conversation | May queue, Stop, or Close | Refuses until Close |
| CLI owns shell | Creation refuses with `SHELL_BUSY` | Existing CLI continues |

- **New chat** closes only an idle, waiting, or failed prior chat for that shell.
- **Stop** interrupts the active run and preserves queued follow-ups.
- **Close** cancels queued work, requests interruption, waits for terminal proof,
  and releases ownership.
- Closed conversations remain durable history and are never reopened.
- Stars pin history without changing lifecycle.

## Broker contract

```linear
Commit message and outbox :::class1 -> Claim one run :::class2 -> Start or exact-resume :::class2 -> Store events :::class3 -> Commit terminal result :::class3
```

Delivery is at-least-once at the outbox boundary and exactly-once at the run
boundary through idempotency and uniqueness. Only one non-terminal run exists
per conversation; additional messages remain ordered in the queue.

Startup and lease-expiry scans perform bounded crash recovery. No correctness
path depends on periodically asking whether a process is alive.

## Harness adapters

Each supported adapter provides probe, start, exact resume, stream, interrupt,
inspect, and reconcile operations. Capability flags make differences explicit.
An adapter that cannot resume an exact native session is not conversation
capable and fails instead of rebuilding context from displayed text.

OpenCode may use a managed loopback server; Claude, Codex, and Kimi may use
process-per-turn execution. These mechanisms stay behind one engine contract.

## Browser product

The browser provides:

- shell selection and New chat;
- paged recent/starred history;
- bounded transcript snapshots plus live event continuation;
- durable queued, working, waiting, failed, interrupted, idle, and closed state;
- Markdown assistant output with normalized activity;
- explicit Stop, Retry, and Close controls; and
- Chat and read-only Diff views over the same live conversation.

Diff reads the current worktree, branch, or pull-request target without mutating
Git. Switching views leaves the conversation and event stream live.

## API boundary

The browser talks only to the engine API. It never receives harness credentials
or calls a harness server directly. Conversation create, message, interruption,
close, star, history, transcript, event, Git-target, and review resources use
stable idempotency/version fences and uniform error envelopes.

## Release gate

- Exact start/resume works for every required harness.
- Queued turns stay ordered and never run concurrently.
- Stop preserves queued work; Close releases ownership only after terminal proof.
- Restart recovers leases without duplicate dispatch.
- History, stars, transcript limits, event cursors, Chat, and Diff remain truthful.
- CLI/browser ownership conflicts fail explicitly.
- Rebuild, snapshot, render, and full conversation tests pass.

## Out of scope

- Mutating harness transcript files.
- Keeping every harness process permanently resident.
- Remote multi-tenant hosting.
- Replacing shell memory or roadmap/spec authority with chat.
- Promising identical tool-level event richness across harnesses.
