---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
title: Fresh-install shell roster
tags: [shells, installer, browser]
date: 2026-07-30
project: super-coder
purpose: Canonical ten-shell starting team
---

# Fresh-install shell roster

## Contract

A fresh fork provisions exactly ten ordinary shells:

| Flavor | Display name | Shortname |
|---|---|---|
| Admin | Admin | ADM1 |
| Cartographer | Cartographer | CART1 |
| Planner | Planner-01 | PLN1 |
| Planner | Planner-02 | PLN2 |
| Dev | Dev-01 | DEV1 |
| Dev | Dev-02 | DEV2 |
| Dev | Dev-03 | DEV3 |
| Dev | Dev-04 | DEV4 |
| Reviewer | Rev-01 | REV1 |
| Reviewer | Rev-02 | REV2 |

One planner is the primary shell by default. All ten use ordinary flavor
templates and may be selected in CLI or browser surfaces when available.

## Boundary

The roster is seeded only during fresh-install bootstrap. Existing installed
forks are not renamed by this slice. Later shell creation, rename, grants, and
retirement continue through the shared shell factory and GUI.

## Verification

Fresh-install, shell-factory, CLI-picker, browser shell-list, rebuild, and render
tests prove the exact roster and absence of hidden singleton roles.
