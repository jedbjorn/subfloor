---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Boot spinner — launch feedback after harness pick
roadmap_status: shipped
frozen: true
title: Boot spinner — launch feedback
tags: [ux, cli, boot, launcher]
date: 2026-07-20
project: super-coder
purpose: Kill the silent 7-10s boot gap
---

# Boot spinner — launch feedback

## Overview

After the operator picks a harness in `./sc enter` / `./sc boot`, the terminal
goes silent for 7–10 seconds before the first `→ booted …` line appears. The
work is real (network round-trips, render), but the operator gets zero feedback
and reads it as a freeze.

Fix: a small CLI spinner — a spinning ASCII glyph with a short phase label —
covering the silent stretch. It does not report real progress; it reports
liveness. TTY-only, interactive-only, zero change to headless/CI output.

> [!class1]
> Scope is feedback, not speed. The boot is not made faster by this spec; the
> sequencing follow-up that would make it faster is named and deferred.

## Where the time goes

The silent stretch is one contiguous region of `main()` in
`.super-coder/scripts/run.py`: from the harness resolution (~line 786) to the
first print block (`→ booted …`, line 878). Interactive boots print nothing in
between. In order:

| Step | Call | Cost |
|---|---|---|
| Analytics sweep | `analytics.sweep(quiet=True)` | mtime-gated; near-zero steady-state, large on first-ever sweep |
| Session open | `open_session` + shell row fetch | fast (local DB) |
| Worktree sync | `sync_worktree` → `git fetch origin main` | **network round-trip, ≤20s timeout** |
| Branch prune | `git_prune.prune` → `gh pr list --state all --limit 300` | **network round-trip, ≤20s timeout** |
| Render | `compose_boot` + `render_skill_md` + artifact writes | sub-second (local) |

The two sequential network calls dominate — 7–10s is their normal sum, and the
worst case (both timing out) is a silent ~40s. A second, smaller gap exists
*after* `→ exec …`: `os.execvpe` replaces the process, then the harness draws
its own UI (~1–3s). That gap is unreachable from our side (see Out of scope).

## Design

Two touch points, both in the engine:

### 1. `style.py` — a `spinner` helper

`style.py` is the launcher's ANSI module and already degrades to plain text
off-TTY; the spinner lives beside that logic.

- Context manager: `with style.spinner("booting") as sp:` … `sp.label = "syncing worktree"`.
- Frames: ASCII `| / - \` at ~10 fps, rendered as `\r<frame> <label>…`.
- A daemon background thread owns the redraw; `stop()` joins it (short
  timeout) and clears the line (`\r` + erase-line) so the next print starts
  clean.
- No-op entirely when `sys.stdout` is not a TTY — the context manager still
  works, the thread never starts, nothing is written.

### 2. `run.py` — wrap the silent region

Start the spinner immediately after the harness is resolved (the picker's
`input()` has returned); stop it before the `→ booted` print block. Update the
label at the existing phase boundaries:

```linear
sweeping analytics :::class2 -> opening session :::class2 -> syncing worktree :::class1 -> pruning merged branches :::class1 -> rendering boot doc + skills :::class2
```

Labels are honest phase names, not progress percentages — the two `class1`
phases are the network-bound ones where the spinner earns its keep.

Gate: spinner only when `not headless`, `RENDER_ONLY` unset, and stdout is a
TTY — the same condition that already gates the wordmark banner. Headless
(`./sc run`), verify, and CI output stay byte-identical to today.

## Edge cases

- **Exception mid-region** — the context manager's exit path always stops the
  thread and clears the line first, so tracebacks and `sys.exit` messages
  print on a clean line, never appended to a half-drawn frame.
- **Ctrl-C** — KeyboardInterrupt takes the same exit path; no orphan thread
  (daemon), no stained line.
- **Prints inside the region** — today the interactive path is silent there
  (analytics runs `quiet=True`; sync/prune return strings, printed later).
  Rule for future edits: nothing inside the spinner region may print directly;
  either update `sp.label` or move the print past `stop()`.
- **Non-TTY / dumb terminals** — off-TTY the spinner is a structural no-op.
  Frames are plain ASCII with `\r`, so even minimal TTYs render sanely; no
  cursor-hide escapes, nothing to restore on kill.
- **Fetch/gh timeout path** — worst case the spinner spins ~40s through both
  network timeouts; the existing "drift check skipped (offline?)" notes still
  print afterward, unchanged. This is the case the spinner exists for.
- **Exec boundary** — the spinner is long stopped before `os.execvpe`; no
  thread survives into the harness process.

## Verification gate

- Interactive boot on a TTY: spinner visible, labels advance, boot summary
  prints on a clean line.
- `RENDER_ONLY=1` render and `./sc run …` headless: output byte-identical to
  pre-change (diff the captured transcripts).
- Kill the network (or point origin at a black-hole host): boot still
  completes, spinner runs through the timeouts, skip-notes intact.
- Ctrl-C during the sync phase: prompt returns clean, no frame residue.

## Out of scope

- **Making the boot faster.** The obvious follow-up: run the worktree fetch
  and the `gh pr list` concurrently, or defer the prune until after render —
  the boot then costs ~max(network) instead of the sum. Deferred so this
  change stays a pure-feedback, low-risk diff; file it as its own feature if
  the 7–10s still grates once the spinner lands.
- **The post-`exec` gap.** After `→ exec` the harness owns the terminal; its
  startup blank is its own. The exec line itself is the feedback there.
- **Real progress reporting** (percentages, timers). Liveness + phase name is
  the contract; anything more couples the spinner to step internals.
