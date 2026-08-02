---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser chat diff review
roadmap_status: shipped
frozen: false
title: Browser Diff: how it works
tags: [browser, conversations, diff, git]
date: 2026-07-31
project: super-coder
purpose: Current shell worktree review
---

# Browser Diff: how it works

## Overview

Every normal browser conversation has a read-only **Diff** mode beside Chat.
Diff reviews the selected shell's current worktree as it exists on disk. It is
not a pull-request or historical-branch browser.

Diff observes once on first entry. After that, its snapshot remains stable until
the operator selects **Refresh Diff**. Switching between Chat and Diff does not
submit a message, interrupt the shell, reconnect the harness, or change Git
files.

## Changes

The Changes section has three views:

| View | What it shows |
|---|---|
| Dirty | Staged, unstaged, conflicted, and untracked files relative to `HEAD` |
| Branch | The aggregate committed file change from `merge-base(origin/main, HEAD)` through `HEAD` |
| Commits | Ahead-side commits that affect at least one visible file |

The summary shows the checked-out branch, or an abbreviated detached `HEAD`,
plus dirty, ahead, and behind counts. A branch that is only behind
`origin/main` shows the behind warning without presenting main-side files as the
shell's work.

A clean branch at remote main shows **No code changes**. Merely creating a branch
name does not count as a change.

## Ignore rules

Git's effective ignore rules are the visibility authority. Diff runs Git's own
ignore resolver against every candidate path, including tracked and committed
paths.

Consequently:

- ignored dirty files do not appear;
- a tracked file that now matches `.gitignore` does not appear;
- ignored paths are removed from branch patches and counts;
- a commit that changes only ignored paths is omitted;
- a mixed commit remains visible, but only its non-ignored files are reviewable.

There is no separate browser ignore list.

## Refresh behavior

Creating an observation performs one bounded, non-interactive fetch of the fixed
`origin/main` tracking ref, then records the exact base SHA and worktree
fingerprint. Refresh never pulls, checks out, merges, resets, stages, restores,
or edits worktree files.

There is no polling or visibility-change refresh. Only one Refresh Diff request
can run at a time.

If a manual refresh cannot fetch, the existing displayed snapshot remains in
place with an error. On first entry, Diff can use an existing `origin/main` ref
with a stale-base warning; without that ref it still shows Dirty and reports
remote main unavailable.

An unchanged refresh does not replace the review workspace DOM. When a changed
snapshot still contains the selected path, Diff preserves the active section,
filters, selection, navigator position, and both vertical and horizontal patch
scroll. If the path disappeared, it selects the nearest remaining file and
starts that new patch at the top.

## Shell files

The Shell files section displays exact read-only text from the shell worktree:

- root `CLAUDE.md`;
- root `AGENTS.md`;
- each granted `.claude/skills/<name>/SKILL.md`;
- the additional `.opencode/skills/<name>/SKILL.md` mirror when OpenCode is the
  conversation harness.

The UI shows worktree-relative paths. Byte-identical skill mirrors collapse into
one entry with all paths listed. Different mirror bodies appear separately with
a mismatch warning. Missing files receive a local unavailable state and do not
break code review.

Shell-file contents come from disk, not from the skill or shell body stored in
the database, and are shown as exact text rather than rendered Markdown.

## API and safety

`POST /api/conversations/{conversation_id}/review-observations` creates a
fingerprinted observation and uses an idempotency key to make retries safe.
Bounded reads then use the opaque fingerprint and server-issued file IDs:

- `GET /api/review-observations/{fingerprint}/patch?file={file_id}`;
- `GET /api/review-observations/{fingerprint}/shell-file?file={file_id}`.

The server resolves the conversation owner, shell, worktree, Git refs, and
paths. Clients cannot provide an arbitrary working directory, revision, or
filesystem path. A worktree change after observation returns
`REVIEW_SNAPSHOT_CHANGED`; the operator decides when to refresh.

Git commands use fixed argument arrays, timeouts, disabled external diff and
text-conversion helpers, and bounded responses. Shell-file reads accept only
regular text files from the allowed worktree locations, reject traversal and
escaping symlinks, and cap file count and bytes. Patch and Shell-file bodies are
not written to logs, analytics, or conversation events.

## Operational states

| State | Browser result |
|---|---|
| Clean or ignored-only | No code changes; Shell files remain available |
| Behind only | Behind warning with no main-side patch |
| Fetch failure after a load | Existing snapshot retained with warning |
| No remote-main ref | Dirty remains available; Branch and Commits unavailable |
| Worktree changes after load | Current snapshot stays visible; content read requests require Refresh Diff |
| Missing boot or skill file | Only that Shell-file entry is unavailable |
| Missing worktree | Diff unavailable; Chat continues normally |

Historical review-target endpoints remain present for compatibility, but the
active Diff UI does not read or write historical targets or pull-request state.
