---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser chat diff review
roadmap_status: shipped
frozen: true
title: Diff: current worktree review
tags: [browser, conversations, diff, git]
date: 2026-07-31
project: super-coder
purpose: Focused shell code review
---

# Diff: current worktree review

## Objective

Make browser-chat Diff a stable inspection of the selected shell's current
worktree against fresh `origin/main`. It shows only reviewable codebase work:
non-ignored dirty files, committed branch changes, ahead-side commits, and a
behind-main warning. It does not browse historical branches or pull requests.

Done means a reviewer can enter Diff, understand exactly how the shell differs
from remote main, inspect its on-disk boot and skill files, and keep their place
until they deliberately refresh.

## User contract

Diff remains available for every selected normal conversation, but its source is
the conversation's shell and current worktree. Conversation history is not a Git
review identity.

The Diff body has two sections:

- **Changes** — `Dirty | Branch | Commits`.
- **Shell files** — `CLAUDE.md | AGENTS.md | Skills`.

A branch name by itself is not a change. A clean branch at `origin/main`, or a
worktree containing only ignored paths, produces a concise `No code changes`
state. The shell remains selectable so its Shell files can still be inspected.

Historical observed branches, open or merged PR targets, canonical PR patches,
cleanup history, and branch-name candidate selection do not appear in Diff.
Other repository and GitHub surfaces remain responsible for that history.

## Change projection

Every successful observation resolves these four facts from the selected
shell's checked-out worktree:

| View | Contents |
|---|---|
| Dirty | Staged, unstaged, conflicted, and untracked non-ignored paths relative to `HEAD` |
| Branch | Aggregate non-ignored file changes introduced from `merge-base(origin/main, HEAD)` through `HEAD`; never includes dirty worktree content |
| Commits | Ahead-side commits from the same merge base through `HEAD` that affect at least one visible path |
| Status | Current branch or detached SHA, fetched base SHA, visible dirty count, visible ahead-commit count, and raw behind count |

Main-side commits are never rendered as though they were the shell's work. A
clean branch that is only behind remains visible as `N behind origin/main`, with
empty Branch and Commits views. A diverged branch reports both its visible ahead
work and its behind count.

If a commit changes both ignored and visible paths, the commit remains listed
and its patch exposes only visible paths. A commit whose paths are all ignored
is omitted and does not make an otherwise synchronized shell appear changed.

Use merge-base semantics. Never compare tips directly in a way that introduces
main-side changes into the shell's patch.

## Ignore authority

The worktree's effective Git ignore rules at observation time are the only
visibility policy. Use Git's own ignore resolver, including nested `.gitignore`
files; do not maintain a second UI ignore list.

Apply the ignore check to every candidate path, including paths already tracked
or committed. A tracked path that now matches `.gitignore` is excluded from
Dirty, Branch, patches, file counts, and commit visibility. Ignored-only state
must be indistinguishable from no reviewable code change.

The filtering contract applies consistently to file lists and selected-file
patches. The API must never allow a client to request an ignored path that was
removed from the projection.

## Freshness and position

Diff has no timer, visibility-change refresh, background poll, or automatic
repaint.

On the first navigation into Diff for a selected shell in the current page
session, create one observation. Returning Chat -> Diff reuses that observation.
The **Refresh Diff** button creates the next observation; it is the only refresh
after first entry.

Each observation:

1. runs a bounded fetch of the fixed `origin/main` ref;
2. records the exact fetched base SHA;
3. reads worktree status, branch divergence, visible commits, and Shell files;
4. returns one fingerprinted snapshot with `observed_at`.

Fetch may update the remote-tracking ref but must never pull, checkout, merge,
reset, stage, restore, or modify worktree files. Only one refresh may be in
flight. The button shows progress and cannot be double-submitted.

If fetch fails, retain the current displayed snapshot. On first entry with no
snapshot, show dirty state and compare against the last-known `origin/main` only
when it exists, clearly labelling the base stale; if no remote-main ref exists,
show dirty state and a typed `remote main unavailable` branch state.

An unchanged refresh performs no DOM replacement. A changed refresh preserves
the active section, filters, selected path, navigator scroll, and patch vertical
and horizontal position when that path still exists. If the selected path
vanishes, select the nearest remaining path and start only that new patch at the
top. Chat scroll and draft preservation remain unchanged.

## Shell files

Shell files are read-only, exact text from the selected shell's worktree—not a
DB reconstruction and not Markdown-rendered content.

Expose only:

- root `CLAUDE.md`;
- root `AGENTS.md`;
- granted skill files found at the adapter-declared skill roots, normally
  `.claude/skills/<name>/SKILL.md` and additionally `.opencode/skills/<name>/SKILL.md`
  for OpenCode.

Show the worktree-relative path and observation time. Byte-identical mirrored
skills appear once with all physical paths listed. If mirrors differ, show each
copy separately with a mismatch warning; never silently choose one. Missing boot
files or skill roots render a typed unavailable state without affecting Changes.

Refresh Diff refreshes Shell files in the same observation. Navigating among
Shell files does not reread disk automatically.

## API and safety

Replace the UI's historical-target query with a shell-worktree observation
resource. A `POST` creates an observation because it intentionally fetches and
advances the local `origin/main` tracking ref; retry protection prevents two
concurrent observations for the same browser action.

The response carries an opaque snapshot fingerprint. Bounded file-patch reads
must present that fingerprint; the server resolves the conversation, shell,
worktree, base, head, and path. If disk state no longer matches, return a typed
`REVIEW_SNAPSHOT_CHANGED` response and let the operator choose Refresh Diff.

Shell-file reads accept only server-issued file identifiers from the current
observation. Resolve regular files beneath the worktree, reject traversal and
escaping symlinks, cap file count and bytes, and return text only. Never expose
an arbitrary filesystem path API.

Use fixed argument-vector Git commands, bounded subprocess deadlines, `--`
before paths, and disabled external diff/text-conversion helpers. Patch bodies
and Shell-file bodies never enter logs, analytics, or conversation events.

The existing historical `conversation_git_targets` data and endpoints may
remain for compatibility, but this Diff surface neither reads nor writes them.
Removing that subsystem is separate cleanup.

## Failure behavior

| Condition | Required result |
|---|---|
| Clean and synchronized | `No code changes`; Shell files remain available |
| Ignored-only dirty or committed work | Same as no reviewable code change |
| Behind only | Behind warning; no main-side patch or commits |
| Fetch unavailable | Preserve prior snapshot, or use labelled last-known base on first entry |
| Detached HEAD | Show abbreviated HEAD identity and normal ahead/behind projection |
| File changes after observation | Keep snapshot stable; selected patch returns `REVIEW_SNAPSHOT_CHANGED` |
| Selected path disappears on refresh | Select nearest visible path; reset only that patch's scroll |
| Boot or skill file missing | Typed unavailable state for that file only |
| Shell worktree missing | Typed Diff-unavailable state; Chat remains fully functional |

## Construction plan

1. **Projection contract** — replace historical target selection in the Diff
   path with current-worktree fixtures for dirty, ahead, behind, diverged,
   detached, ignored-untracked, ignored-tracked, and ignored-only commits.
   **Gate:** only shell-authored visible paths enter Branch or Commits.
2. **Observation API** — add bounded `origin/main` fetch, snapshot fingerprint,
   filtered file/commit projections, patch revalidation, and safe Shell-file
   enumeration. **Gate:** no client-controlled cwd/ref/path and no worktree
   mutation. This step and UI shell layout may begin in parallel after the
   projection fixture freezes.
3. **Stable UI** — replace target/history controls with Changes and Shell files,
   remove polling, add Refresh Diff, and reconcile refreshed data without losing
   review position. **Gate:** unchanged refresh performs zero workspace DOM
   replacement.
4. **Integrated proof** — exercise first entry, manual refresh, fetch failure,
   ignored-only state, behind-only state, changing patches, missing shell files,
   and Chat/Diff navigation. **Gate:** the release journey below passes against
   a disposable remote and real scroll containers.

## Release gate

1. Enter Diff for a shell once and observe one `origin/main` fetch and one
   snapshot; wait without seeing another request or repaint.
2. Review a long patch, switch Chat -> Diff, and retain the selected file and
   exact horizontal and vertical position.
3. Press Refresh Diff with no changes and retain the DOM and scroll position.
4. Add staged, unstaged, conflicted, and untracked files; refresh and see them in
   Dirty but not Branch.
5. Commit visible work; refresh and see its aggregate Branch patch and commit
   metadata without dirty duplication.
6. Add ignored-untracked, tracked-now-ignored, and committed-ignored fixtures;
   none appear or contribute visible counts.
7. Advance remote main; refresh a clean shell and see a behind warning without a
   patch containing remote-main work.
8. Diverge a branch and see visible ahead work plus behind count from the exact
   fetched base SHA.
9. Fail the fetch and retain the prior review with a truthful freshness warning.
10. Open CLAUDE.md, AGENTS.md, and granted skills and verify their displayed text
    and relative paths match disk; verify OpenCode mirrors deduplicate or warn.
11. Change or remove the selected path between observations; require explicit
    refresh and reset only when the path no longer exists.
12. Attempt ignored paths, arbitrary refs, traversal, escaping symlinks, binary
    shell files, and oversized files and receive bounded typed refusals.

## Out of scope

- Historical branches, PR discovery, PR lifecycle, canonical PR patches, checks,
  merge status, or cleanup state.
- Repository-global branch browsing or comparison with local `main`.
- Editing, staging, restoring, committing, pushing, switching, rebasing, merging,
  resetting, or pulling.
- Automatic polling or refresh on tab visibility changes.
- Markdown rendering or editing of Shell files.
- Review comments, approvals, suggestions, syntax-aware diff, or split diff.

## Prior decisions

- Decision #35 supersedes decision #31: current shell worktree state, not PR
  identity, owns Diff.
- Decision #14 remains: generated artifacts stay local-only and must not re-enter
  code review merely because an old branch tracked them.
- Decision #20 remains: Diff observes the shell worktree without creating a new
  owner.
- Decision #25 remains: entering or refreshing Diff never interrupts a run.
