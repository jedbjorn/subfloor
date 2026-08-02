---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Shell-facing API identity wording
roadmap_status: shipped
frozen: true
title: API Identity Wording
tags: [memory, boot, docs]
date: 2026-07-29
project: super-coder
purpose: Hide auth plumbing
---

# Shell-Facing API Identity Wording

## Objective

Ordinary non-admin shells should know how to use engine memory without being taught bearer-token mechanics. The shell-facing contract is:

> Use `sc mem` for engine memory. It is already wired to this launched shell; the engine resolves API identity for you.

This gives enough explanation that a model does not wonder how API identity works, while avoiding terms that invite direct HTTP calls or token handling.

## Scope

Update normal shell-facing text that currently exposes implementation details:

- `.super-coder/templates/boot.md`
- `.super-coder/templates/shell_system_prompt.md`
- `.super-coder/assets/skills/db_map/SKILL.md`
- `.super-coder/render/compose.py` `render_api`
- `.super-coder/scripts/mem.py` user-facing help text for `which` and its printed identity line

Also update tests or render fixtures that assert the old wording.

## Required Wording

Use these phrases, or very close equivalents:

- `sc mem is already wired to this launched shell; the engine resolves API identity for you.`
- `sc mem which` confirms the memory API is reachable and which shell this session resolves as.
- Memory commands use `sc mem`; this session is already API-wired by the launcher.

Keep the direct rule:

- Do not read or write the engine DB directly.

## Remove From Ordinary Shell-Facing Docs

Remove these terms from ordinary boot docs and normal memory/db-map guidance:

- `SC_API_TOKEN`
- bearer token
- `Authorization`
- `api_key`
- token resolves to shell id

These terms may remain in implementation comments, API code, maintainer/debug documentation, and API/test-authoring skills where token handling is the subject.

## Acceptance

The implementation is done when:

- freshly rendered boot docs no longer teach ordinary shells about bearer tokens or `SC_API_TOKEN`;
- `db_map` still tells shells to use `sc mem`, but describes identity as already resolved for the launched shell;
- `sc mem which` remains useful for debugging API reachability and shell resolution without exposing token mechanics in normal output/help;
- bearer-token language remains available only in implementation/test contexts where it is necessary;
- existing render/check tests pass after expected fixture updates.

## Out Of Scope

- No API auth behavior changes.
- No schema changes.
- No admin runtime-credential changes.
- No changes to direct HTTP test helpers except fixture/wording updates needed by this cleanup.
