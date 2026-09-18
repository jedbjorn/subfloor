# engine integrity — when the installed engine falls short of its pin

**Posture: operator runbook.** Applies when a fork's installed engine
(`.super-coder/`) is missing something it should have — a document a help
entry names, a script a verb dispatches to, a skill the catalogue grants —
while `./sc update` reports success and `.sc-state/engine.ref` reads current.
Host setup and lifecycle belong to the authorized operator; a sandboxed shell
hands this procedure to the operator and waits.

## Symptom

A shell or the operator follows a pointer the engine itself gave out and hits
the absence:

- `sc help --all` says `See .super-coder/docs/<name>.md` — the file is not
  there (worked example: issue
  [#1609](https://github.com/jedbjorn/subfloor/issues/1609), where
  `.super-coder/docs/db-broker.md` was absent from a fork pinned at a ref
  that contains it upstream);
- a dispatcher verb fails with a missing-module or missing-file error for a
  path that exists upstream at the fork's pin;
- the boot render or a skill references an engine asset that is absent.

The pin is not stale in these cases — `git ls-remote
https://github.com/jedbjorn/subfloor HEAD` shows the fork current or the fix
already upstream — and `./sc update` has run green. The tree on disk is simply
short of its recorded ref.

## Why it can happen silently

The engine materialize lays declared paths (`ENGINE_PATHS`, mostly
directories) from the pinned ref onto disk with `git archive | tar -x`, then
asserts that every *declared path* exists. A directory existing says nothing
about every file that should be inside it. The hash manifest written after the
materialize records the files that are present on disk — a file that failed to
land is not recorded, so the fork's own integrity check (`local_edits()`)
reads clean forever. Nothing else compares the tree file-by-file against the
pin.

This is a known gap, reported upstream: the manifest could record the exact
upstream file set — including files absent from disk — so a short tree is
detected and healable instead of invisible.

## Recover: re-lay the engine at the pin

On the host, from the fork's root (never inside the sandbox):

```bash
./sc update                     # fetches + re-materializes the whole engine, then reconciles
```

A fetch update always re-materializes the entire engine from the target ref
and rewrites the manifest — there is no "already at this ref" skip. At the
same pin it is a byte-identical re-lay that restores missing files; after it,
confirm the file that was absent:

```bash
ls .super-coder/docs/<name>.md
```

Two variants of the same remedy:

- **Update refuses at the local-edits gate** naming the missing file as
  *deleted*: the manifest did record it (from an earlier materialize) and it
  vanished since. Confirm the deletion was not a deliberate local edit —
  engine files are upstream-owned; the strong default is to upstream any real
  change (PR to subfloor) — then `./sc update --force` to discard and re-lay.
- **`./sc update --no-fetch` is not a remedy here**: it reconciles against
  the current tree and does not re-materialize. A short tree needs the fetch
  path.

`./sc rollback` restores the engine at `engine.ref.prev` — use it only when
the *previous* pin is known good for the missing file; rolling back past the
commit that introduced the file loses the feature it belongs to.

## Verify the pin carries the file

Before or after the heal, from the fork's root:

```bash
git cat-file -e "$(cat .sc-state/engine.ref):.super-coder/docs/<name>.md" && echo present-at-pin
```

Present at the pin but absent on disk confirms the short-tree diagnosis (and
the upstream report below). Absent at the pin means the pin predates the
file — the remedy is an ordinary `./sc update` to head, not this runbook.

## Bounds

- **Never hand-patch `.super-coder/`.** Recreating a missing file from a web
  copy or another fork leaves the manifest blind and the tree uncertified;
  the engine is upstream-owned. The heal is the re-materialize.
- **The sandbox cannot fix this.** Engine lifecycle runs on the host; a
  sandboxed shell surfaces the symptom and hands the procedure over.
- **An ejected fork has no pin to heal from** — its engine is fork source,
  edited and committed like any other code; restore the file from its own
  history instead.
- **Report upstream.** A short tree at a current pin is an engine defect, not
  a fork quirk. File on jedbjorn/subfloor with the engine ref (`sc
  engine-ref`) and the missing path — see the `issue_reporting` skill. Your
  report supplies the triage data (which paths, which ref) that turns this
  runbook into a fix.