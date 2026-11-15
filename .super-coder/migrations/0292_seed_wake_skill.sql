-- 0292 — wake skill (`sc mem message send --wake`). Seeds `wake`: a shell
-- sends a wake into another shell's active chat outside a Sprint only when
-- the FnB directs it ("send a wake" / "--wake"), and treats a received
-- wake as act-on-it, no acknowledgement. Granted to every flavor pack; packs
-- are reconciled from templates/shells/*.json on update, and the grants below
-- make a fresh migrate converge without a reconcile. No schema change;
-- full-body UPSERT by name keeps skill_id and grants. Idempotent.
BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'wake',
  'Wake another shell''s active chat outside a Sprint; load only when the FnB says "send a wake" or "--wake". Never on your own initiative. Also says how to treat a wake you receive.',
  'workflow',
  NULL,
  0,
  '# wake — an FnB-directed wake outside a Sprint

A normal message waits in the recipient''s inbox. A wake goes straight into
the recipient''s active chat as its next turn.

## When

- Only when the FnB tells you to: "send a wake" or "--wake". Urgency, a
  workflow step, or wanting a fast answer is not a direction.
- Inside an armed Sprint, use `sc sprint send`, not this.

## Send

```bash
sc mem message send <shortname> "<body>" --wake
```

- Give the recipient and the body; the engine routes it. You choose no model,
  harness, or wake type.
- No active chat -> refused. Tell the FnB; send a normal message only if the
  FnB says so.
- The printed receipt (`wake #N queued`) is the proof. Never resend, and never
  ask the recipient to confirm.

## Receive

A wake arrives headed `Wake from <SHORTNAME>, sent on the FnB''s direction`.
Act on it. Do not reply just to acknowledge; reply only when the body asks a
question or you are blocked, and then with a normal message, never a wake.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'admin', skill_id FROM skills
WHERE name='wake' AND is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'planner', skill_id FROM skills
WHERE name='wake' AND is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'dev', skill_id FROM skills
WHERE name='wake' AND is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'reviewer', skill_id FROM skills
WHERE name='wake' AND is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'devops', skill_id FROM skills
WHERE name='wake' AND is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'cartographer', skill_id FROM skills
WHERE name='wake' AND is_deleted=0;

COMMIT;
