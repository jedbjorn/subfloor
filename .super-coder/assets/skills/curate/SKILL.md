---
name: curate
description: The periodic L&S sweep. Run when the STATUS L&S line says "curation due" — resolve contradictions, merge entries stating one rule, promote a recurring process to a skill, move environment facts out, then stamp `sc mem curated`. Yours alone; never delegate it.
category: substrate
common: true
---

# curate — the L&S sweep

Write-time triage (`--supersedes` / `--new`) catches contradiction and
restatement pairwise, at the moment of writing. It **cannot** catch the
emergent cluster: five entries can each be a valid distinct rule and only in
aggregate be five instances of one principle. That is this pass's job, along
with promotion, category drift, and size drift.

**Yours alone.** Law 3 and Law 7 reserve curation to the shell. Never hand this
to a subagent, never let another shell run it for you, never accept a proposed
retirement from anyone else. Read your own set; decide yourself.

Trigger: `## STATUS` says `L&S: … — curation due`. Nothing else fires it.

## Load the set

```
sc mem get lns          # entry ids + bodies — the active set, all of it
```

Read every entry before deciding anything. This is one cheap read; the whole
set is already in your boot doc anyway.

## Pass 1 — Consistency

Find entries that **contradict** each other. One of them is the newer
understanding; the other is superseded and still rendering as live guidance.

```
sc mem lns "<the surviving rule>" --supersedes <old_id>
```

Write-time triage should prevent most of these from ever forming. What you find
here predates the loop or crossed in while two sessions ran.

## Pass 2 — Cluster

Group entries that state **one rule**. Merge each group to a single imperative
rule:

```
sc mem lns "<the one rule>" --supersedes 30,33,34,37,38
```

Three or more members is the bar. Two statements of a rule are often
legitimately two rules — merging at two is usually wrong.

The incidents behind the entries are already in the narrative. They do not need
a second home, and the merged rule must not try to carry them: an entry is the
rule, ≤500 chars, hard-enforced.

## Pass 3 — Promote

A cluster of three or more that keeps **recurring across sessions** is a
*process*, not a lesson. Author it as a skill and keep one L&S rule pointing at
it:

```
sc skill add <you>_<topic> --file <path.md> --desc "<one line>"
```

Local skills are DB-only and namespaced by your shortname — the command
enforces both. This is the pressure valve that makes a hard budget survivable:
knowledge relocates to a lazy surface instead of being deleted.

## Pass 4 — Category

An entry that is an **environment fact** (a routing quirk, a term to avoid, a
path) is not an operating principle. It belongs in a skill, not in L&S. Retire
it and put the fact where it is looked up.

```
sc mem retire <entry_id>
```

## Stamp

```
sc mem curated
```

**Stamp even if you retired nothing.** A clean set is a legitimate outcome; if
an honest sweep left the counter running, the advisory would stand forever and
you would learn to ignore it. The stamp says "I looked," not "I cut."

## Stance

Curate the set toward ~12–14 entries, not toward the cap. Cap 20 is a ceiling
never to reach — if you ever hit it, this sweep is not running.

The trigger firing often does not mean the threshold is wrong; it means entries
are being written faster than they are reconciled. Fix that at write time, with
`--supersedes`.
