---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Modal and flag action normalization
roadmap_status: in_progress
frozen: false
title: Modal and Flag Action Normalization
tags: [ui, modal, flags, accessibility, patch]
date: 2026-08-01
project: super-coder
purpose: Normalize dialogs and flag editing
---

# Modal and Flag Action Normalization

## Overview

The browser UI already has one shared `openModal` frame, but its `footNodes`
array makes every caller responsible for physical button order. The New Shell
dialog now renders Cancel on the left and Create on the right, while the other
action dialogs still render their primary action on the left and dismissal on
the right.

Production currently has six `openModal` consumers: five action/form dialogs
and one read-only viewer. The active Sprint-board branch adds one action dialog
and one read-only detail viewer, bringing the target surface to eight modal
types. Four production action dialogs need normalization; the pending Sprint
action dialog is the fifth.

The Flags surface also lacks an edit action even though the existing
`PATCH /api/flags/{flag_id}` route already accepts `display_name`,
`description`, `feature_id`, and `priority`. Create and edit must share one
form implementation, and the current four-line description field must become
an eight-line editor in a taller modal.

> [!class1]
> Done means action placement is enforced by the shared frame, every expanded
> flag card exposes Edit at bottom-right, and create/edit flags use the same
> roomy form without duplicating behavior.

Decision #46 remains authoritative for the Sprint board. This patch changes
presentation and flag editing only; it does not alter the board's Pause,
Resume, or Abort authority.

## Footer Contract

Keep `openModal` as the single structural frame for overlay, header, body,
footer, dimensions, and close behavior. Retire the arbitrary `footNodes`
argument.

The base frame accepts explicit physical slots:

- `footerStart` renders at the left/start edge.
- `footerEnd` renders at the right/end edge.
- Either slot may be omitted.
- A slot may contain one node or a wrapper containing a related group.

Add a thin `openActionModal` wrapper over `openModal` with semantic inputs:

- `dismissNode` is always passed to `footerStart`.
- `actionNode` is always passed to `footerEnd`.
- The wrapper forwards title, header content, body, dimensions, and close
  behavior unchanged.
- The wrapper returns the same close function as `openModal`.

Action callers must use `openActionModal`; they must not place action buttons
directly into physical footer slots. Read-only viewers may use `openModal`
directly because their leading content is utility or context rather than a
cancel/primary pair.

The DOM order is dismissal first and action last. Existing button labels,
classes, click handlers, validation, disabled states, loading text, API calls,
toasts, and close timing remain owned by each caller.

## Dialog Matrix

| Dialog | Kind | Canonical start | Canonical end |
|---|---|---|---|
| Current-state editor | action | Cancel | Save |
| Skill-content viewer | viewer | Raw/rendered toggle | Close |
| New Shell | action | Cancel | Create |
| Feature editor | action | Cancel | Save |
| New/Edit Flag | action | Cancel | Create or Save |
| Windows Test VM | action | Close | Save |
| Sprint lifecycle action | action | Cancel | Pause, Resume, or Abort |
| Sprint work-unit detail | viewer | Sprint context | Close |

The first six rows exist on `origin/main`. The final two are present on the
active Sprint-board branch and must be included once that work is part of the
implementation baseline.

## Flag Editing

Replace `openNewFlagModal(features)` with one shared
`openFlagModal(features, flag = null)` form path.

### Create mode

- The title is `New flag` and fields use the current blank/default values.
- The primary action is Create and submits `POST /api/flags`.
- Success closes the modal, reports `flag created`, and reloads Flags.

### Edit mode

- The title identifies the flag being edited.
- Name, description, linked feature, and priority are prefilled from the flag
  row.
- The primary action is Save and submits `PATCH /api/flags/{flag_id}` with only
  `display_name`, `description`, `feature_id`, and `priority`.
- Resolution state, resolution notes, resolved date, creator, and flag ID are
  not editable in this modal.
- Success closes the modal, reports `flag saved`, and reloads Flags.

Both modes use the same field nodes, validation, payload builder, async error
handling, and `openActionModal` invocation. Description remains required. The
description textarea has `rows: 8`, double the current four visible lines, and
the flag modal is `600 × 520` instead of `600 × 400` so the additional writing
space does not crowd the remaining fields or footer.

Every expanded open or resolved flag card exposes an Edit action at the
bottom-right of `.flag-body`. Add a dedicated `.flag-actions` row after the
description, linked feature, and resolution content:

- An open flag keeps Resolve on the left and places Edit on the right.
- A resolved flag places Edit on the right with no empty visible control on
  the left.
- Edit opens the shared form with that card's complete flag row and the feature
  options already returned by `GET /api/flags`.
- Clicking Edit must not collapse the expanded `<details>` card before the
  modal opens.

## Frame Semantics

While the frame is being centralized, add the minimum dialog semantics that
belong at the frame boundary:

- The modal container has `role="dialog"` and `aria-modal="true"`.
- The generated title element has a unique ID and the modal references it with
  `aria-labelledby`.
- Closing by button, Escape, or overlay restores focus to the element that had
  focus before the modal opened when that element remains connected.
- Existing Escape and overlay-dismiss behavior is preserved.

A full focus trap, native `<dialog>` migration, animation system, and redesign
of unrelated modal sizing or visual styling are outside this patch.

## Construction Plan

```linear
Frame contract and semantics :::class1 -> Migrate callers and Flags :::class2 -> Contract and regression tests :::class3
```

### Step 1 — Frame contract

In `.super-coder/ui/app.js`, replace `footNodes` with named start/end footer
slots, add `openActionModal`, and add the dialog semantics and focus restoration
defined above. Update `.super-coder/ui/style.css` only where the named slots
need wrappers to preserve the existing space-between layout.

Verification: a focused frame-level test proves slot placement, dialog
attributes, all three close paths, and focus restoration.

### Step 2 — Callers and Flags

Migrate all action dialogs to `openActionModal` and both viewer dialogs to the
named base-frame slots. Refactor the New Flag form into the shared create/edit
form, enlarge its description editor and modal, and add the expanded-card
action row. Preserve every unrelated caller behavior.

This step must run on a baseline containing the Sprint-board UI. If that branch
has not merged, sequence this patch after it or rebase the patch before final
verification so the two Sprint modal callers cannot be missed.

Verification: source inspection finds no `footNodes` uses; every matrix row has
the expected semantic API and footer positions; create sends POST, edit sends
PATCH, and both modes repaint the Flags view after success.

### Step 3 — Regression coverage

Add `tests/test_modal_ui_contract.py` for the shared frame and matrix-wide
contract. Add `tests/test_flags_ui.py` for the shared create/edit form, field
prefill, eight-line description, modal dimensions, POST/PATCH payloads, and
expanded-card action placement. Update the New Shell assertion in
`tests/test_shells_ui_contract.py` to assert semantic wrapper use, and extend
`tests/test_sprint_board_ui.py` for the two Sprint modal contracts.

Verification: the focused modal, Flags, Shells, and Sprint-board suites pass
together, followed by the repository verification gate.

## Acceptance

- [ ] There is one structural modal frame and one thin semantic action wrapper.
- [ ] `footNodes` no longer exists in application code.
- [ ] Every action dialog renders dismissal left and action right.
- [ ] Viewer utility/context remains left and Close remains right.
- [ ] Primary and destructive classes remain attached to the action button.
- [ ] Existing form validation, async progress, success, failure, and close
  behavior is unchanged outside the specified Flags additions.
- [ ] New and Edit Flag use one form implementation and one payload builder.
- [ ] The flag description editor shows eight rows in a `600 × 520` modal.
- [ ] Every expanded flag card has Edit anchored bottom-right.
- [ ] Open flag cards retain Resolve on the left; resolved cards remain
  editable without exposing resolution fields in the edit form.
- [ ] Edit prepopulates all four editable fields and saves them through the
  existing PATCH route.
- [ ] Escape and overlay dismissal still work and restore prior focus.
- [ ] The modal exposes dialog role, modal state, and an accessible title.
- [ ] Tests cover the shared contract and every current modal caller.

## Verification Gate

Run the focused tests:

```bash
pytest -q tests/test_modal_ui_contract.py tests/test_flags_ui.py tests/test_shells_ui_contract.py tests/test_sprint_board_ui.py tests/test_api_endpoints.py
```

Then run the engine gates:

```bash
./sc render-check
./sc verify
git diff --check
```

The patch fails the gate if any action dialog remains action-left, if a viewer
loses its utility/context placement, if any `footNodes` call survives, if Edit
is absent from an expanded flag card or not bottom-right, if create/edit forms
diverge, or if a modal close path fails to restore focus.

## Risks and Boundaries

- The active Sprint-board branch adds two modal callers. Building from an older
  baseline would produce a falsely complete migration.
- Tests that assert literal source fragments can pass while runtime layout is
  wrong; the shared frame and flag action row require DOM-behavior tests.
- Moving buttons must not move their event handlers or change async state
  transitions.
- The existing resolution-notes `prompt` and the other browser-native
  `prompt`/`confirm` flows remain outside this patch.
- The existing API already supports editable flag fields; no API endpoint,
  database schema, application mutation authority, or unrelated visual design
  change is included.
