---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser chat diff review
roadmap_status: shipped
frozen: true
title: Browser chat diff review
tags:
  - browser
  - conversations
  - diff
  - git
  - pull-requests
date: 2026-07-30
project: super-coder
purpose: Read-only Git and PR review mode
---

# Browser chat diff review

## Overview

Add a read-only **Diff** mode beside **Chat** in the normal browser conversation
surface. Chat remains the default. Switching to Diff replaces the transcript and
composer body with a full review workspace while preserving the shell rail, chat
history, conversation identity, queue state, and persistent header actions.

The feature follows the repository's actual delivery lifecycle:

```linear
Dirty workspace -> Feature branch -> Open PR -> Merged PR -> Local cleanup
```

The review identity changes as work matures. The worktree is the live identity,
the branch is the local delivery identity, and the PR number is the durable
review identity. A merged PR remains reviewable after its branch is deleted.

> [!class1]
> Diff is an observation surface only. It never stages, restores, commits,
> pushes, switches, rebases, merges, deletes, fetches, or otherwise changes Git,
> GitHub, conversation, or shell state.

This feature depends on feature #24, Browser-native conversations. It extends
normal browser chat only; Sprint boards and worker evidence continue to use
their existing surfaces.

## Product contract

### R1 — Coequal modes

For a selected normal conversation, the pane header contains a centered
`Chat | Diff` segmented switch. Chat is the default when the route carries no
explicit mode. The selected mode has exactly one visible conversation body:

- Chat: existing transcript, composer, Send, and Stop experience.
- Diff: review summary, target selector, file navigation, and read-only patch.

No switch appears on the new-chat configuration form or the no-conversation
empty state.

### R2 — Mode is not work state

Changing modes never submits a message, interrupts a run, closes a conversation,
changes ownership, claims a queue item, or reconnects the harness session. It is
browser presentation state only.

The SSE connection and in-memory Chat view remain alive while Diff is selected.
Returning to Chat restores:

- transcript scroll position;
- jump-to-latest state;
- unsent composer draft;
- pending-send retry identity;
- current Stop request state;
- all streamed messages and events received while Diff was visible.

### R3 — URL and defaults

The existing conversation hash remains canonical for Chat. An optional trailing
`diff` mode segment deep-links to Diff:

```text
#interface/<shell>/<conversation>
#interface/<shell>/<conversation>/diff
```

Missing mode means Chat. Selecting another conversation or creating a new chat
uses Chat unless its route explicitly names Diff. Mode changes update browser
history without rebuilding the selected conversation or stopping its stream.

### R4 — Controls remain truthful

The conversation title, shell/route identity, queue count, Analytics, and Close
remain visible in both modes. While a run is active, Diff also exposes the same
manual Stop operation in the persistent header because hiding the Chat composer
must not hide the operator's interruption control.

Stop and Close retain feature #24 semantics. Diff introduces no alternate
interrupt, close, retry, or queue behavior.

## Review targets

### Target scope

The target selector contains only change sets associated with the selected
conversation, not every branch in the shared repository:

1. the conversation's current worktree;
2. branches observed for that conversation at creation or run boundaries;
3. PRs associated with those exact branches and heads;
4. a Sprint unit PR only when a future surface deliberately exposes that
   conversation and the structural unit binding supplies the association.

Repository-global branch and worktree inventory remains the Worktrees tab's job.

### Target kinds

| Kind | Stable identity | Display example |
|---|---|---|
| Workspace | conversation + observed worktree | `Live workspace · feat/foo · 3 dirty` |
| Local branch | target id + branch/head | `Branch feat/foo · local only` |
| Pull request | repository + PR number | `PR #821 · feat/foo · open` |

Once a PR association exists, the PR number is primary and the branch name is
context. A branch name may be reused for several PRs; one PR must never overwrite
another target solely because their branch names match.

### Observation

Capture a cheap local Git observation:

- when a conversation is created;
- after each run becomes terminal;
- when Diff first opens;
- when the operator refreshes Diff.

Git and filesystem reads happen outside conversation write transactions. The
result is persisted afterward in one short database-only transaction. Failure to
observe Git never changes run terminalization or conversation state.

Transient branches entered and left entirely inside one turn are not promised.
The normal one-branch-per-unit workflow is the supported floor.

### Association order

Associate PRs without guessing:

1. an existing structural Sprint unit PR binding, when applicable;
2. exact PR number already recorded for the target;
3. exact GitHub head SHA match;
4. compatible branch plus head ancestry and conversation time window;
5. otherwise expose multiple candidates or leave the branch local-only.

Never choose "the newest PR with this branch name" when several candidates
remain compatible.

## Comparison scopes

Each target exposes three scopes when the evidence exists:

| Scope | Question answered | Source |
|---|---|---|
| Review changes | What does this branch or PR introduce? | canonical PR patch, else merge-base to selected head/worktree |
| Local only | What is present locally but absent from the selected PR/head? | selected remote/head to index and worktree |
| Commits | Which commits belong to this change set? | merge-base to selected head |

For a local branch without a PR, Review changes includes committed, staged, and
unstaged tracked changes from the merge base to the worktree. Untracked files
appear in the file list and become synthetic new-file patches only when selected.

For a PR, Review changes is the canonical PR patch at its recorded head. Local
only separately exposes unpushed commits and uncommitted work. The summary must
be able to say:

> PR #821 contains 6 files. Four additional local changes are not in the PR.

Use three-dot/merge-base semantics for branch review. Never use a direct
`main..topic` tip comparison as the review patch: when `main` advances, it mixes
unrelated base changes into the result. PR state, not Git ancestry, determines
whether squash-merged work is merged.

## Lifecycle status

One primary lifecycle status and several secondary facts describe a target.

| Primary status | Meaning |
|---|---|
| `LOCAL BRANCH` | branch has no known remote PR |
| `PUSHED` | remote branch exists but no PR is associated |
| `PR OPEN` | GitHub reports an open PR |
| `CHECKS FAILED` | open PR has a terminal failing check rollup |
| `PR MERGED` | GitHub reports the PR merged |
| `PR CLOSED` | PR closed without merge |
| `REMOTE UNKNOWN` | remote status is unavailable and no cached verdict exists |

Secondary facts include dirty file count, additions/deletions, unpushed commit
count, base divergence, checked-out state, cleanup pending, branch pruned, and
observation freshness.

### Merged but locally behind

GitHub's merged verdict overrides ancestry and ahead/behind interpretations.
For a squash-merged PR whose old branch remains checked out, show:

```text
PR #712 merged into main.
This worktree still checks out the old topic branch.
Local main has advanced by 40 commits.
No uncommitted changes. Cleanup has not run.
```

This is not an error and must not be rendered as unmerged work. `cleanup
pending` means the local checked-out branch prevented safe boot-time pruning.

If GitHub reports merged but the local `origin/main` ref does not yet contain
the recorded merge SHA, retain `PR MERGED` and add `local base not refreshed`.

## Diff experience

### Pane structure

Diff consumes the full conversation body. On desktop it contains:

1. summary row with target selector, lifecycle status, counts, and freshness;
2. `Review changes | Local only | Commits` scope switch;
3. path filter and file-status filters;
4. file navigator;
5. selected-file unified patch.

The file navigator and patch share the available width. On narrow layouts the
navigator becomes a top selector and the patch occupies one column. Chat rails
retain their existing responsive behavior.

### Target selection

On first entry to Diff, select in this order:

1. current workspace when it has dirty or unpushed work;
2. open PR associated with the current branch;
3. current local branch;
4. most recently observed associated PR, including merged PRs;
5. an empty clean-workspace state.

Changing targets does not switch Git branches or change the worktree.

### File navigation

The file list provides:

- path tree and text filtering;
- `A`, `M`, `D`, `R`, `?`, conflict, submodule, and binary indicators;
- additions/deletions when Git can calculate them;
- generated, binary, deleted, and viewed filters;
- next/previous file navigation;
- an ephemeral Viewed state scoped to target fingerprint.

Viewed state is browser-local. If the target fingerprint changes, affected
files become unviewed. No review comments, approvals, or persisted annotations
are created.

### Patch rendering

Render a unified patch with file and hunk headers, old/new line numbers, and
added, removed, context, and no-newline rows. Use text nodes, not unsanitized
HTML. Binary, submodule, oversized, and unavailable files render bounded typed
states rather than empty panes.

Only one file patch is required at a time. Split diff, syntax-aware semantic
diff, inline comments, and editable suggestions are outside this feature.

## Freshness and polling

Diff separates local freshness from remote freshness:

- local summary fingerprint: poll every two seconds only while Diff is visible,
  the document is visible, and no prior poll is in flight;
- selected file patch: reload only when its fingerprint changes;
- GitHub PR metadata: load on first Diff entry, cache for at least 30 seconds,
  and refresh on explicit operator request;
- canonical merged PR patch: cache after the first successful merged read.

Review mode never runs `git fetch`. It reads existing local refs and uses
read-only GitHub queries for PR state and canonical patches. Every response
states `observed_at`, local base/head SHAs, remote refresh time, and whether
remote data is fresh, cached, or unavailable.

Use ETags for summaries, file pages, and file patches. An unchanged polling
request returns `304 Not Modified`.

## Durable associations

Add `conversation_git_targets` as additive runtime state.

| Field | Contract |
|---|---|
| `target_id` | opaque stable identifier |
| `conversation_id` | owning conversation |
| `branch_name`, `base_ref` | observed local identity |
| `first_head_sha`, `latest_head_sha` | bounded local history |
| `pr_number` | nullable durable PR identity |
| `pr_head_sha`, `pr_state` | normalized remote evidence |
| `merge_sha`, `merged_at` | nullable merge evidence |
| `pr_url`, `pr_title` | bounded display/link metadata |
| `first_seen_at`, `last_seen_at` | local observation window |
| `remote_refreshed_at` | remote freshness |
| `patch_artifact`, `patch_sha256` | nullable local merged-patch cache |

Logical uniqueness is conversation + PR number when a PR exists, otherwise
conversation + branch + first observed head. PR associations are append-only:
later observations update status/head metadata but never repurpose one target
for a different PR.

Patch artifacts are bounded generated state under the gitignored local artifact
root, never tracked source. Store paths relative to that root. A missing cache is
recoverable from GitHub or local Git and never blocks conversation work.

## Git review service

Factor the existing git-hygiene knowledge into a reusable read-only service
rather than implementing competing branch/PR verdicts.

### Local reads

Use argument-vector Git calls with explicit worktree cwd, timeouts, and:

- `status --porcelain=v2 -z --branch`;
- `rev-parse` and `symbolic-ref`;
- `merge-base`;
- `rev-list --left-right --count`;
- `diff --find-renames --no-ext-diff --no-textconv`;
- `log` with bounded machine-readable fields.

Repository configuration must not enable external diff or text conversion.
Pass `--` before validated pathspecs.

### Remote reads

Reuse one normalized GitHub reader for git-hygiene, Diff, and Sprint PR
enrichment. It returns PR number, branch/base, head SHA, state, check rollup,
review decision, merge SHA/time, title, and URL.

Use the exact PR number for canonical patch reads. Branch-name queries may
discover candidates but never become the durable identity.

### Precedence

Remote PR state is authoritative for `OPEN`, `MERGED`, and `CLOSED`. Local Git
is authoritative for worktree dirtiness, current checkout, local commits, and
the patch it can prove from available objects. Cached remote data is labelled,
not silently presented as fresh.

## API contract

All endpoints use the existing operator authentication, same-origin checks,
uniform error envelope, and `Cache-Control: no-store`.

### List targets

`GET /api/conversations/{conversation_id}/review-targets`

Query parameters:

- `refresh=remote` requests a read-only GitHub metadata refresh;
- omitted `refresh` uses the freshness policy.

Returns ordered target summaries, selected-target recommendation, local and
remote freshness, and the conversation's current Git fingerprint. It never
returns the absolute worktree path.

### List files

`GET /api/review-targets/{target_id}/files`

Query parameters:

- `scope=review|local`;
- `cursor`;
- `limit`, bounded to 200;
- optional whitelisted `status` and `path` filters.

Returns file ids, paths, status, old path for renames, additions/deletions,
binary/generated flags, fingerprint, and `next_cursor`.

### Read one patch

`GET /api/review-targets/{target_id}/diff`

Required query parameters:

- `scope=review|local`;
- `path`, which must match a file in the target's current file projection.

Returns one bounded unified patch with truncation/binary/unavailable metadata.
The server resolves every ref and worktree from the stored target; the client
cannot submit an arbitrary cwd or Git revision.

### List commits

`GET /api/review-targets/{target_id}/commits`

Cursor-paginated, bounded commit summaries from the target merge base to the
selected local/PR head. No commit body, raw signature payload, or arbitrary
revision query is exposed.

### Errors

Required stable codes:

- `REVIEW_TARGET_NOT_FOUND`;
- `REVIEW_TARGET_UNAVAILABLE`;
- `REVIEW_WORKTREE_MISSING`;
- `REVIEW_NOT_A_GIT_REPOSITORY`;
- `REVIEW_REF_MISSING`;
- `REVIEW_REMOTE_UNAVAILABLE`;
- `REVIEW_PATH_INVALID`;
- `REVIEW_DIFF_TOO_LARGE`;
- `REVIEW_TARGET_CHANGED`.

Remote failure returns cached/local data with freshness metadata when possible.
Use an error only when the requested representation cannot be produced.

## Security and limits

- Resolve conversation ownership before target lookup.
- Resolve worktree and refs server-side from immutable conversation/target
  records.
- Never interpolate branch, ref, filter, sort, or path into a command string.
- Set bounded subprocess deadlines and terminate process groups on timeout.
- Exclude ignored files and never enumerate ignored-directory contents.
- File lists expose untracked names only. Selecting an untracked regular file
  may return a synthetic new-file patch under the same size limits.
- Never follow an untracked symlink or read outside the resolved worktree.
- Mark binaries rather than transporting their bytes.
- Bound file count, patch bytes, line length, commit count, GitHub response size,
  and local artifact size.
- Never put patch bodies in server logs, analytics, conversation events, or
  telemetry.
- Store patch artifacts with owner-only permissions and validate their hash
  before serving.
- Treat diffs as sensitive source data and return `Cache-Control: no-store`.

Read-only means no source-control mutation. Cache and association writes are
internal observation records; they cannot affect Git refs, the worktree, the
remote repository, or conversation execution.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Worktree clean | show a truthful no-changes state and associated PR history |
| Worktree missing | typed unavailable state; Chat remains fully functional |
| No Git repository | typed unsupported state; no fallback directory |
| GitHub unavailable | show local + cached remote evidence with freshness warning |
| Local branch pruned | serve associated PR from GitHub/cache |
| PR branch reused | keep each PR target separate; never newest-name overwrite |
| Base ref stale | show local-ref freshness; do not fetch or dispute GitHub merge state |
| File changes during read | revalidate fingerprint and return `REVIEW_TARGET_CHANGED` |
| Binary/submodule | typed summary without raw bytes |
| Patch exceeds cap | file remains listed; patch returns bounded truncation metadata |
| Conversation running | local observations continue; Chat stream and Stop remain live |
| Conversation closed | associated PR targets remain selectable and read-only |

Diff failure never changes Chat state, conversation state, shell ownership, or
run delivery.

## Construction plan

### 1. Contract fixtures

Define target, lifecycle, comparison, freshness, and error projections. Build
temporary-repository and mocked-GitHub fixtures for clean base, dirty worktree,
local branch, open PR, squash-merged retained branch, deleted branch, branch
reuse, stale base, remote outage, rename, conflict, binary, and oversized diff.

**Gate:** fixtures prove the merged-PR verdict overrides misleading ancestry and
that three-dot review excludes unrelated base advancement.

### 2. Schema and observation

Add the target table, indexes, migration/rebuild coverage, local observation at
conversation creation and terminal run boundaries, and short post-read writes.

**Gate:** run completion remains correct when every Git observation fails or
times out; two PRs reusing one branch remain distinct.

### 3. Review service

Refactor shared git-hygiene PR normalization, implement local Git projections,
remote enrichment, canonical PR patches, merged-patch cache, ETags, and caps.

**Gate:** git-hygiene behavior remains unchanged while all review fixtures
produce deterministic bounded projections.

### 4. Read API

Implement target, file, patch, and commit GET resources with auth, pagination,
filter validation, freshness, uniform errors, and concurrency revalidation.

**Gate:** API tests prove ownership isolation, no arbitrary cwd/ref/path,
read-only command shape, retry-safe GETs, and local/cached degradation.

### 5. Mode shell

Add the centered Chat/Diff switch, optional Diff route segment, preserved Chat
DOM/stream/draft/scroll, responsive header behavior, and Stop availability in
Diff.

This may proceed in parallel with Step 4 after Step 1 freezes the projection.

**Gate:** switching modes during a streamed run loses no event or draft and
causes no message, interrupt, close, or stream restart.

### 6. Diff workspace

Add target selection, lifecycle summary, comparison scopes, file tree/filter,
Viewed state, unified patch, commits list, empty/error states, freshness, and
local fingerprint polling.

**Gate:** the supplied layout intent is preserved: stable rails/header with the
conversation body switching wholly between Chat and Diff.

### 7. Integrated proof

Run schema, Git service, API, conversation, UI, render, and verify suites. Smoke
one normal chat through dirty work, branch, open PR, merged PR, and a behind
retained branch without allowing Review to mutate the repository.

**Gate:** every release-gate journey below passes on a fresh fork and an
existing fork with stale local Git state.

## Release gate

The feature is ready only when all of the following are demonstrated:

1. Open a normal conversation and observe Chat selected by default.
2. Type an unsent draft, scroll the transcript, switch to Diff, then return and
   recover both exactly.
3. Switch to Diff during a running turn; receive streamed completion in the
   background and keep Stop and Close available.
4. Switch modes without creating a message, interruption, run, close, or new
   harness connection.
5. Deep-link to Diff for an existing conversation; navigate to another
   conversation and default to Chat.
6. Observe a clean shell-base empty state.
7. Create dirty tracked, staged, untracked, renamed, deleted, binary, and
   conflicted files and see truthful bounded file states.
8. Cut a local feature branch and see a merge-base review that excludes later
   unrelated `main` changes.
9. Push without a PR and see `PUSHED`.
10. Open a PR and see exact PR number, head, checks, canonical patch, and local
    changes not yet in that PR.
11. Reuse one branch name for two fixture PRs and retain two distinct targets.
12. Squash-merge a PR and see `PR MERGED` even though ancestry still reports
    topic-only commits.
13. Leave that merged branch checked out and behind; see `cleanup pending` and
    local-base drift without an unmerged-work claim.
14. Delete/prune the merged local branch and continue reviewing its PR target.
15. Remove GitHub access and receive cached/local review with a freshness
    warning; restore it and refresh without changing Git refs.
16. Attempt arbitrary paths, refs, symlinks, oversized patches, and cross-user
    target ids and receive bounded typed refusals.
17. Close and reopen history; associated PR targets remain reviewable while
    Chat stays transcript-only and immutable.

## Out of scope

- Editing, staging, restoring, applying, committing, pushing, branching,
  switching, rebasing, merging, or deleting.
- PR comments, approvals, requested changes, inline suggestions, or review
  submission.
- Split diff, syntax-aware semantic diff, blame, dependency review, or
  executable rich previews.
- Persisted per-user Viewed state.
- Repository-global branch browsing inside a conversation.
- Guaranteed capture of transient branches entered and left inside one turn.
- Durable historical patches for closed no-PR work when both refs and worktree
  content are gone.
- Diff mode for the Sprint board or hidden one-shot worker conversations.
- Automatic `git fetch` from Review.

## Prior decisions

- Decision #14 remains: merged patch caches are generated local-only artifacts,
  never tracked source.
- Decision #18 remains: conversations persist independently of ephemeral
  harness processes; Diff observes their worktree and never becomes transport.
- Decision #20 remains: one browser conversation owns the shell worktree; Diff
  does not create a second owner or weaken CLI exclusion.
- Decision #22 remains: Git/GitHub reads occur outside conversation-path write
  transactions and are revalidated before a short observation write.
- Decision #25 remains: only explicit Stop interrupts a run. A mode switch has
  no delivery semantics, and Stop stays reachable from Diff.

