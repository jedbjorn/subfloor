---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Boot spinner — launch feedback after harness pick
roadmap_status: shipped
frozen: true
title: Boot spinner — QA corrections
tags: [ux, cli, boot, qa]
date: 2026-07-20
project: super-coder
purpose: Close lifecycle QA findings
---

# Boot spinner — QA corrections

## Scope

Correct three QA findings in the open boot-spinner pull request without
changing its intended output or launch sequencing:

- wait until the redraw worker is actually stopped before clearing the line;
- treat thread-start failure as a structural no-op rather than a boot failure;
- publish sync and prune labels only when those phases execute.

## Done condition

Delayed-output regression coverage proves no frame can land after cleanup,
thread-start failure leaves the context usable and silent, and launcher phase
wiring does not claim skipped work. Focused and full tests pass apart from
documented sandbox-only failures.
