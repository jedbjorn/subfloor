# Sprint 2 temporary Claude resume workaround

Your prior Sprint chat was closed after a confirmed engine defect: Claude
resume inspection rejects a session if any historical transcript row records a
child directory, even when that directory is inside the assigned worktree.
The defect is tracked as subfloor issue #1044.

For all remaining Sprint 2 work until that issue is patched:

- keep the harness working directory at the assigned worktree root;
- do not run `cd`, `pushd`, or an equivalent directory-changing command;
- use `git -C <path>`, tool `--workdir` options, or absolute paths instead;
- keep Sprint evidence under `shared/sprints/sprint-2/` but address it without
  changing the process working directory;
- if a turn reports `HARNESS_WORKTREE_MISMATCH` or “Turn outcome could not be
  proven,” stop rather than generating recursive Re-enter retries and notify
  the Planner through the next durable wake.

This is a temporary recovery constraint only; do not change product code for
it inside the current feature.
