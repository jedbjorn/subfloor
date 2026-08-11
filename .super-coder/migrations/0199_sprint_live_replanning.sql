-- 0199 — permit deliberate governing-task reuse across Sprint work units.
--
-- A task remains unique inside one work unit through the composite primary
-- key.  Removing the global UNIQUE(task_id) constraint lets a later
-- conformance/recovery lane repeat the same governing task without inventing
-- duplicate product scope.

-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;

BEGIN;

ALTER TABLE sprint_work_unit_tasks
  RENAME TO _sprint_work_unit_tasks_one_lane;

CREATE TABLE sprint_work_unit_tasks (
    sprint_id    INTEGER NOT NULL REFERENCES sprints(sprint_id),
    work_unit_id INTEGER NOT NULL,
    task_id      INTEGER NOT NULL REFERENCES spec_tasks(task_id),
    PRIMARY KEY (sprint_id, work_unit_id, task_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);

INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id)
SELECT sprint_id,work_unit_id,task_id
FROM _sprint_work_unit_tasks_one_lane;

DROP TABLE _sprint_work_unit_tasks_one_lane;

COMMIT;

PRAGMA foreign_keys=ON;
