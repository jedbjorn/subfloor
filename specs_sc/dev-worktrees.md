---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Dev shell git worktrees
roadmap_status: shipped
frozen: true
---

# Dev shell git worktrees

## Problem

Multiple dev shells sharing one git tree clobber each other — one shell's
`git checkout` or `git restore` undoes another shell's uncommitted work. The
fix is structural: each dev shell owns its own worktree.

## Solution

Use `git worktree` — same repo, multiple checked-out branches, separate
directories. Lightweight: shared object store, no clone overhead.

## Path convention

`<parent>/<repo>-<shortname_lower>/`
e.g. `~/dos-arch-dev1/`, `~/dos-arch-dev2/`

## Surfaces to change

### 1. Schema — `0013_dev_worktrees.sql`
ADD COLUMN `worktree_path TEXT` to `shells`. Nullable — only dev shells get it.

### 2. `shell_factory.py`
After inserting a dev shell:
```python
worktree_path = REPO_ROOT.parent / f"{REPO_ROOT.name}-{shortname.lower()}"
subprocess.run(["git", "-C", str(REPO_ROOT), "worktree", "add",
                str(worktree_path)], check=True)
con.execute("UPDATE shells SET worktree_path=? WHERE shell_id=?",
            (str(worktree_path), shell_id))
```
Non-dev flavors: skip. Branch: start on main, let the shell branch when it
picks up work.

### 3. `run.py` (launcher)
Read `worktree_path` from the shells row at launch. If set, cd into it before
exec'ing claude. The shell's cwd IS its tree.

### 4. Boot template (`shell_system_prompt.md` / `boot.md`)
Render `worktree_path` in the WORKSPACE block for dev shells:
`Worktree: <path>  (main repo object store: <REPO_ROOT>)`

### 5. `git` skill
Add: "Dev shells operate from their worktree (`shells.worktree_path`). All git
work runs there. The main repo root is the object store only — do not cd into
it for day-to-day work."

### 6. Shell deletion cleanup
Wherever shell deletion is handled, add:
```bash
git -C <REPO_ROOT> worktree remove <worktree_path> --force
git -C <REPO_ROOT> worktree prune
```

## Out of scope (this spec)

- Reviewer/planner worktrees — they are git read-only; shared tree is fine
- `./sc worktree-list` command — nice to have, not needed for v1
- snapshot.py changes — snapshots are DB-level, not tree-level; no change needed

## Done condition

A new dev shell created via `./sc init` or the GUI has a worktree at the
expected path. Launching that shell opens claude with cwd = worktree path.
Two dev shells can be launched simultaneously on different branches without
touching each other's uncommitted work.
