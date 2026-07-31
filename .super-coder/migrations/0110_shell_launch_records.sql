-- Spec #76 H-25 — the liveness verdict must not depend on launcher lineage.
--
-- A generic headless shell runs detached: no controlling TTY, launcher gone
-- (ppid==1), harness relaunched per work item. The rail's fallback scan
-- (interface_routes._availability -> shell_liveness) judged such a process by
-- its PARENTAGE, so classify_orphan read `tty_nr==0 and ppid==1` -> 'detached'
-- and every live worker projected 'unreconciled'; between relaunches no process
-- held the worktree at all and the same shell projected 'available'. Both are
-- wrong, and the true state was unreachable by construction -- 'busy' required
-- a live parent, which a detached headless launch never has.
--
-- The record replaces the inference. run.py is the single exec chokepoint for
-- every harness (it BECOMES the harness via execvpe), so it stamps the pid it
-- is about to become. One row per shell, upserted: the newest launch is the
-- only claim, which is what "re-stamped on each relaunch" means.
--
-- `start_ticks` is /proc/<pid>/stat field 22 and is not optional: a pid alone
-- is a reusable integer, so a record naming a dead pid would resurrect as a
-- false "working" the moment the OS handed that number to an unrelated
-- process. pid + start_ticks is the stable process identity pair. execvpe does not
-- reset it -- the value belongs to the PROCESS, not to the program image --
-- so the launcher may read its own before exec and the harness matches after.
--
-- `worktree` is the cwd run.py chdir's into immediately before exec. The scan
-- tests that the claimed pid still holds it; parentage is never consulted.
BEGIN;

CREATE TABLE IF NOT EXISTS shell_launch_records (
    shell_id    INTEGER PRIMARY KEY REFERENCES shells(shell_id) ON DELETE CASCADE,
    pid         INTEGER NOT NULL,
    start_ticks INTEGER NOT NULL,
    worktree    TEXT    NOT NULL,
    harness     TEXT,
    launched_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

COMMIT;
