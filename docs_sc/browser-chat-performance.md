---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
title: Browser chat performance
tags: [browser, conversations, performance, streaming]
date: 2026-07-31
project: super-coder
purpose: Fast bounded browser chat
---

# Browser chat performance

## Overview

Browser conversations load history and transcripts in bounded stages. Chats
can paint without model discovery, closed histories page per shell, and an
existing chat opens from one compact transcript snapshot instead of replaying
every assistant delta through the live event stream.

Durable messages/events remain authoritative; harness transcripts remain
harness-owned; only explicit Stop interrupts a run; Close and same-shell
switching preserve ownership rules; Chat and Diff share one conversation.

## Arrival flow

Opening Chats reads shells and open conversations first. The selected shell
then loads its recent page; starred summaries load afterward. A direct link
fetches its conversation independently and canonicalizes the shell from the
authoritative record.

Cards are keyed by `conversation_id`, so direct, starred, polled, and paged
summaries update one card instead of duplicating it.

## History controls

- Loaded starred chats pin above recent history.
- **More** pages non-starred history without loading transcripts.
- Failure preserves cards/cursors and exposes Retry.
- Polling reads only open conversations and already loaded open cards.
- Cursors are scoped to owner, shell, starred/open filters, and state.
- Invalid or contradictory filter/cursor combinations return explicit 422s.

## Transcript snapshot

`GET /api/conversations/{id}/transcript` authorizes the owner, opens one read
transaction, captures a high-water mark, and returns bounded prompts, runs, and
normalized events at or below it.

Stable display identities (`message:`, `run:...:assistant`, and `event:`) let
the browser update one node. Raw reasoning, usage frames, harness identifiers,
credentials, and tool chatter are excluded. Caps retain the newest complete
turns and return typed truncation evidence; durable history is never deleted.

## Live continuation

After installing the snapshot, the browser opens events after its high-water
mark. Reconnect combines that value safely with `Last-Event-ID`. A sequence gap,
projection mismatch, malformed delta, or identity conflict triggers one bounded
authoritative reconciliation, then manual Retry if repeated.

DOM updates coalesce behind one animation frame. Completed nodes retain identity
and only the active assistant block reparses. While Diff is visible, Chat state
continues in memory without hidden Markdown or transcript replacement; returning
to Chat performs one catch-up frame.

## Configuration

Existing Chat and Diff routes do not request model/default catalogues. Configure
loads both on demand, shares one in-flight promise, caches success, and clears a
failed promise so Retry performs one new request pair.

## Verification

API tests cover strict filters, cursor scope, query plans, large projections,
every cap, race-free watermarking, redaction, and reconnect recovery. Browser
tests cover request counts, paging, snapshot bootstrap, animation-frame
coalescing, Markdown work, DOM identity, Diff-hidden behavior, and bounded
reconciliation.
