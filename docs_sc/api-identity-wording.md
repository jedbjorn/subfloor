---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Shell-facing API identity wording
roadmap_status: shipped
frozen: false
title: API Identity Wording
tags: [memory, boot, shells]
date: 2026-07-29
project: super-coder
purpose: Shell-facing auth language
---

# API Identity Wording

## Overview

Shell-facing memory guidance now keeps authentication mechanics out of the ordinary working path. Non-admin shells are told to use `sc mem`, and the docs explain only that it is already wired to the launched shell and that the engine resolves API identity.

The behavior did not change: `sc mem` still routes through the engine API, memory writes still land in the shared engine DB, and shell-scoped writes still target the calling shell. The change is presentation. Ordinary shells no longer see bearer-token or environment-variable details unless they are working in implementation or test surfaces where those details are the subject.

## Shipped Behavior

The boot database guidance now says `sc mem which` confirms API reachability and which shell the session resolves as. The rendered API block says to write memory with `sc mem`, already wired to this launched shell, with identity resolved by the engine.

The `db_map` and `memory` skills now describe memory writes as shell-scoped by the engine, without asking the shell to reason about bearer tokens. They keep the important guardrail: no raw `sqlite3` path and no direct engine DB fallback.

The `sc mem which` command remains a diagnostic, but its help/output now describes shell resolution rather than token resolution. The top-level `sc help` memory entry follows the same wording.

## Maintainer Detail

Implementation internals still retain token terminology where needed: API proxy comments, runtime credential discovery, the HTTP authorization call path, and test-authoring helpers. That keeps engine maintainers able to debug auth while avoiding unnecessary plumbing in normal shell prompts.

The shipped migration `0129_reseed_api_identity_wording.sql` updates existing seeded `db_map` and `memory` skill rows so already-installed instances receive the same wording after migration.

## Verification

DEV3 reported targeted contract, memory, credentials, and skill-freshness suites green; `sc render-check` green; and a full-suite run with only five pre-existing host-environment failures unrelated to this change.

PR #736 shipped this work at merge commit `22b4d2b64810bed895325e7dffd2504953f6bc92`.
