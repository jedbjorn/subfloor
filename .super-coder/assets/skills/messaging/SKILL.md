---
name: messaging
description: Shell-to-shell inbox — send a markdown message to another shell, check your unread inbox, mark messages read. Direct SQL against shell_db.db (no API in v1). Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.
category: substrate
common: true
---

# messaging — the shell inbox

One shell writes a markdown message to another; the recipient discovers it on its
next boot via the `## STATUS` `Inbox:` count, surfaces it with `check`, and clears
it with `mark-read`. Body is markdown — preserved verbatim. There is no API layer
in v1: you run parameterized SQL directly against `.super-coder/shell_db.db`.

`<self>` = **your** `shell_id` — read it from the `shell_id:` line in this boot
doc's `## ACTIVE SESSION` block. Recipients are addressed by `shortname`; the
`send` SQL resolves the shortname to a `shell_id` for you.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> | mark-read <id>`

Run every statement with foreign keys ON so an unknown recipient is caught, e.g.:

```sh
sqlite3 .super-coder/shell_db.db <<'SQL'
PRAGMA foreign_keys=ON;
<the statement, with the params filled in>
SQL
```

## check — your unread inbox

```sql
SELECT m.message_id, s.shortname AS from_shortname, m.body, m.created_at
FROM shell_messages m
JOIN shells s ON s.shell_id = m.from_shell_id
WHERE m.to_shell_id = <self> AND m.read_at IS NULL
ORDER BY m.created_at
LIMIT 50;   -- optional N; default 50, max 200
```

`check` does **not** auto-mark-read — you decide when. Surface the body to the
operator (and reply if warranted, which is itself a `send`), then `mark-read` the
inbound in the same turn.

## send — message another shell

```sql
INSERT INTO shell_messages (from_shell_id, to_shell_id, body)
VALUES (
    <self>,
    (SELECT shell_id FROM shells
      WHERE shortname = :to_shortname
        AND COALESCE(is_deleted,0) = 0),
    :body
);
```

- Multi-word body = a single quoted argument; markdown is preserved verbatim.
- Examples: `--message send cartographer "map is stale — re-run ./sc map"`
  · `--message send cc "spec ready for review — see flag SC-014"`

Error paths to translate for the operator:
- Unknown / deleted `to_shortname` → the subquery yields no row → the INSERT
  fails (`NOT NULL constraint failed: shell_messages.to_shell_id`, or
  `FOREIGN KEY constraint failed` with FK on). Surface as **"recipient shortname
  unknown."**
- Empty body → `CHECK constraint failed` on `body`. Surface as **"body is empty."**

## mark-read — clear an inbox item (idempotent)

```sql
UPDATE shell_messages
SET read_at = datetime('now')
WHERE message_id = :id
  AND to_shell_id = <self>
  AND read_at IS NULL;
```

`to_shell_id = <self>` is the only access control — you cannot mark read a message
addressed to another shell (0 rows updated). `read_at IS NULL` makes it idempotent
(re-marking a read message is a no-op). Pass the `message_id` that `check` surfaced.

## Stance

On boot, if the `## STATUS` `Inbox:` line is non-zero, run `--message check` and
surface the first item before continuing. A reply is a new `send` — there is no
threading; include `Re: <topic>` in the body if it matters. Keep the inbox honest:
mark-read only once you've actually acted on the message.
