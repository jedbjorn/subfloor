---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
title: Kimi browser conversations
tags: [conversations, browser, kimi, headless]
date: 2026-07-29
project: super-coder
purpose: Exact Kimi chat adapter
---

# Kimi browser conversations

## Objective

Make Kimi a required browser-conversation harness with the same durable queue,
exact-session resume, normalized events, Stop/Close behavior, and recovery
contract as the other supported adapters.

## Driver

Kimi is process-per-turn:

```text
start  -> kimi -p <message> --output-format stream-json
resume -> kimi -p <message> --output-format stream-json -S <session>
```

`-m` receives a user-local alias and effort is passed through
`KIMI_MODEL_THINKING_EFFORT`. Prompt mode supplies its own permission policy;
unsupported interactive flags are never emitted.

## Identity and stream

The adapter discovers the real session directory and main-agent `turn.prompt`
record before returning the native turn. Session reference, prompt timestamp,
and binary wire offset provide stable exact-run identity even when two prompts
share a millisecond.

Only `agents/main/wire.jsonl` is authoritative. Subagent wires are ignored.
Structured stdout provides assistant output and activity; the exact main-wire
slice provides usage and durable completion/cancellation evidence.

## Recovery

- SIGINT interrupts the adapter-owned process group.
- `turn.cancel` proves interruption for the exact run.
- Turn-scoped usage/completion can terminalize a run even if a child keeps
  stdout open.
- Restart reconstructs exact-run metadata from persisted session/run/context.
- Missing or mismatched identity yields a conservative unknown/failed outcome,
  never a guessed success or blind replay.

## Integration

The adapter is registered through the shared manifest/registry and appears in
browser chat configuration. Model routing reuses the generic local catalogue;
no schema or Kimi-specific API surface is added.

## Verification

Fixtures and tests cover exact two-turn resume, interrupted wire evidence,
same-millisecond prompts, cleanup, persistent children, subagent exclusion,
usage boundaries, restart reconciliation, API creation, browser selection, and
a real host-seat smoke across complete, interrupted, refreshed, and resumed
turns.
