---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Task 137 release candidate
roadmap_status: retired
frozen: false
title: Task 137 release-gate report-only Sprint
tags: [task-137, release-gate, report-only]
date: 2026-07-30
purpose: Verify R1 on integrated main without fabricating code or a PR
---

## Overview

This is a one-unit, report-only Sprint for Task 137. The governing release gate is R1: the repository README first line is exactly `# Task 137 release candidate`.

R1 is already true at planning time. The unit therefore makes no code, README, branch, commit, or PR changes. It reports the verification result against integrated main and stops.

## Requirements

### R1 — README release-candidate line

The first line of `README.md` must be exactly:

`# Task 137 release candidate`

The required verification command is:

```sh
head -n 1 README.md
```

The observed output must equal the required string exactly, including capitalization and spacing. Extra output, a different first line, or a missing README fails R1.

## Workflow

### Single report-only unit

The Developer runs `head -n 1 README.md` on the integrated main checkout, records the command and exact observed output, and reports whether it equals R1. The Developer must not edit files, create a branch, commit, push, or open a PR.

The Reviewer independently checks the Developer's report against integrated main, reruns the same command when evidence is incomplete or ambiguous, and records a pass/fail judgment. The Reviewer must not request implementation when R1 is already true.

The unit is complete only when the Reviewer report states the integrated-main reference, command, observed output, and verdict. A pass closes the report-only gate. A fail is reported as a release-gate failure; it does not authorize a code change or a follow-on PR within this Sprint.

### Sprint boundary

This document defines the feature and unit only. The Planner must not declare or arm the Sprint as part of this setup. The later declaration, if authorized, must use exactly one Developer→Reviewer unit and the report-only path defined here.

## Edge Cases

### Verification and repository state

- If `README.md` is absent or unreadable, report R1 as failed with the command error; do not create or repair the file.
- If the first line differs by any character, report R1 as failed; do not normalize it.
- If the checkout is not integrated main, stop and report that the required baseline is unavailable; do not verify a feature branch as a substitute.
- If the command emits a blank result, report R1 as failed.
- If Developer and Reviewer observe different results, preserve both observations, identify the checkout/commit references, and report the discrepancy; do not modify the repository.
- If R1 is already true, no code, commit, branch, or PR may be fabricated to create activity.

## Verification Gate

One unit only: `U1 — Report R1 against integrated main; no code or PR when already true`.

The unit's verification evidence is the exact command `head -n 1 README.md`, its exact output, the integrated-main reference, and an independent Reviewer pass/fail report. No implementation diff is expected or permitted when R1 is true.

## Anticipated User Activity

### Vocabulary

- **Shell**: the Developer or Reviewer agent acting through the Sprint assignment.
- **Valid Privileged User**: the FnB authorizing release-gate setup and any later Sprint declaration.
- **Unexpected Participant**: any actor attempting to turn this report-only check into implementation work or a fabricated PR.

### Expected Activity

- The Shell Developer verifies and reports only.
- The Shell Reviewer independently validates the evidence and reports only.
- The Valid Privileged User may authorize a later declaration, but this setup does not declare or arm it.

### Reach

The unit reaches the integrated repository checkout and `README.md` through the shell's command environment. It adds no endpoint, page, job, file, or persistent product data.

### Data Tenancy

The unit reads one repository file on integrated main and creates no user or product data.

### Beyond Intention

Implementation edits, branch creation, commits, pushes, pull requests, and verification against a non-integrated branch are outside this Sprint's intention.
