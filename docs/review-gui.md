---
title: super-coder — Review GUI
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: super-coder
purpose: The localhost GUI: nine tabs, roadmap views, token & session analytics
---

# Review GUI

## Review GUI

> [!class2]
> **UI** this IS the GUI — Shells · Skills · Roadmap · Docs · Flags · Worktrees · Map · Analytics · Scripts · **Shells** reviewer (every shell reads it)

A zero-dependency localhost GUI to review the substrate — shells, roadmap,
flags. One stdlib Python server serves both the JSON API and a static UI; no
venv, no npm, no build step. Its nine tabs are the windows the workflow above
refers to:

| Tab | What it shows |
|---|---|
| **Shells** | Each shell's role, mandate, editable `current_state`, identity, decisions, and skill grants. The default landing tab. |
| **Skills** | The skill catalogue (Repo · Substrate · Craft), with per-shell grant toggles and full content in a modal. |
| **Roadmap** | Features in a planning funnel (Brainstorm → … → Shipped), each with its spec tasks, linked docs, and flag blockers. Two views — a **Board** for editing a feature inline, and a **Flow** that groups features by work-stream and wires their blocker dependencies (see below). |
| **Docs** | Read-only `kind='doc'` documents; opens in md-converter for reading. |
| **Flags** | The blocker / follow-up tracker, grouped by feature, filterable Open/Resolved/All. |
| **Worktrees** | Live git-hygiene report — dirty worktrees, prunable merged branches, clean trees. |
| **Map** | The repo catalogue — language mix, file roles, dependencies, env vars — with a re-map button. |
| **Analytics** | Token & session analytics — per-class spend cards, a local-day graph, and the session history swept from each harness's on-disk usage data (see [Token & session analytics](#token--session-analytics)). |
| **Scripts** | Run the maintenance chores (snapshot, render, seed-skills, migrate, rebuild) from a button. |

The header's **snapshot ⤓** / **publish ⤴** buttons serialize the DB and open a
rolling content PR. How they authenticate to GitHub (`gh auth login` or a scoped
`SC_GH_TOKEN`), and the rolling event log (`.super-coder/logs/webapp.log` /
`GET /api/logs`, last 20 events) for seeing what a publish actually did:
[`.super-coder/docs/publish-and-gh-auth.md`](../.super-coder/docs/publish-and-gh-auth.md).

![Review GUI, Roadmap tab — Board view: a feature expanded into its inline editor with title, status, summary, and spec-task checklist](https://raw.githubusercontent.com/jedbjorn/super-coder/main/docs/images/roadmap-tab.png)

![Review GUI, Worktrees tab — live git-hygiene report: dirty worktrees, each branch ahead/behind its base, and prunable merged branches](https://raw.githubusercontent.com/jedbjorn/super-coder/main/docs/images/worktrees-tab.png)

### Roadmap views — Board & Flow

The Roadmap tab renders the same feature rows two ways, toggled top-centre:

- **Board** — the planning funnel. Features sit in status columns (Brainstorm →
  In Progress → Next → Near Term → Long Term → Shipped, plus a Retired filter),
  and clicking one expands its inline editor — title, status, summary, and the
  spec-task checklist (the screenshot above).
- **Flow** — a left-to-right read of *what's committed and in what order*.
  Features are grouped into **work-streams** (a `projects` row doubles as a
  work-stream; `roadmap.project_id` is the link, NULL = Ungrouped), and the
  **blocker edges** between them (`feature_blockers`) draw as wires — a
  prerequisite must land before what it blocks. The graph is kept acyclic, so it
  reads cleanly stage by stage.

![Review GUI, Roadmap tab — Flow view: features grouped by work-stream across the planning stages, with blocker dependencies wired between cards](https://raw.githubusercontent.com/jedbjorn/super-coder/main/docs/images/roadmap-flow.png)

> [!class2]
> **Drive it from the shell, too.** `./sc mem roadmap project <feature_id> <work-stream>`
> assigns a feature's work-stream and `./sc mem roadmap depends <feature_id> --on <id>`
> sets its blocker edges (cycles refused) — the Flow view is the same data the
> CLI and the `db_map` skill write.

The server runs **inside the sandbox container** as its foreground process, so
`./sc launch` brings it up (printing its URL) and `./sc down` stops it;
`./sc enter` then attaches the interactive harness session into that same
container via `docker exec`, so the shell and the GUI run side by side, sharing
the one bind-mounted repo + creds. The port publishes to `127.0.0.1` only.

```bash
./sc health    # curl /api/health
./sc serve     # run the server in the foreground on the host (no docker)
./sc ports     # show this fork's derived port
```

> [!class2]
> **Ports are derived per repo**, never fixed — a fork runs *inside* a host repo that may have its own dev server, and several forks can run at once. Each fork hashes its path to a stable port in the `88xx` band (clear of superCC 8000 / dos-arch 8001 and common host ports), persisted to a gitignored `.super-coder/instance.json` you can hand-edit. Two forks won't collide.

What you can do in the GUI: read everything; **create shells** (pick a flavor —
the factory grants its skill set and opens its first session); rename a
shell's `display_name` (✎ next to the name); edit a shell's
operational fields (`current_state`, `connections`, `workspace`) and skill
grants; edit the roadmap (linear status buckets, with toggle-filters) and
**non-frozen** documents; create and resolve flags. **seed and L&S are
read-only** — the laws say the shell curates them, so the API ships no endpoint
to write them at all. A `snapshot ⤓` button re-serializes + renders after
edits; **publish** goes one further — it snapshots, then commits your content
edits onto an ephemeral `sc_gui_content` branch, force-pushes it, and opens (or
refreshes) one PR to `main` — then returns to `main` and drops the local branch.
No merge: `main` stays clean and merging the PR stays yours.

The **Scripts** tab lists the maintenance scripts (snapshot, render, seed-skills,
migrate, rebuild) — each with a description and a **run** button, so the common
chores work from the GUI without dropping to a terminal (rebuild prompts first,
since it discards un-snapshotted DB edits).

The live `.super-coder/shell_db.db` is **gitignored and rebuilt** from
git-tracked text. See `.super-coder/README.md` for the full model.

> [!class2]
> **Spec:** the founding design lives in the roadmap (`super-coder` feature row) and renders to `specs_sc/`.

## Token & session analytics

> [!class2]
> **Every token, every harness** — swept from what the CLIs already write to disk; no wrapper, no proxy, nothing in the model path

super-coder never calls a model itself — it launches harness CLIs — so token
telemetry is **pull-based**: each harness already writes usage data to disk
(claude transcripts — subagents included, codex rollouts, kimi wire logs, the
opencode DB, vibe session metas), and a per-harness parser normalizes what it
finds into one table, `session_token_usage` — one row per harness session ×
model, in four token classes (fresh input / output / cache read / cache write)
plus an informational reasoning split. `NULL` means *this harness doesn't
expose the class*; `0` means *measured zero* — parsers never invent zeros.

The sweep is incremental and idempotent — re-sweeping never double-counts —
and runs from four triggers:

- **every boot** — `./sc enter` sweeps before opening the session, so the view
  is current and the previous session's end time gets backfilled;
- **claude SessionEnd hook** — real-time capture the moment a session ends;
- **Analytics tab load** — the GUI sweeps on open;
- **manual** — `./sc analytics sweep [--harness <name>]`.

Sessions attribute to shells by cwd (a worktree maps to the shell whose
shortname names it) and archive time-window; anything ambiguous stays visibly
**unattributed** rather than guessed. The Analytics tab reads it all back:
per-class stat cards with harness/model filters, a local-day spend graph,
usage panels (favorite model by flavor, peak day, features and specs shipped,
docs outstanding), and a session history grouped by local day with sprint
clusters and per-session token rollups.

![Review GUI, Analytics tab — token-class stat cards with harness and model filters, the total-tokens spend graph, usage panels (favorite model by flavor, peak day, features and specs shipped, docs outstanding), and the day-grouped session history](https://raw.githubusercontent.com/jedbjorn/super-coder/main/docs/images/analytics.png)

The same reads are served as JSON at `/api/analytics/*` (session window +
cursor, token totals and series, filters) for anything outside the GUI.
