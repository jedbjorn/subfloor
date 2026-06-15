---
name: messaging
description: Shell-to-shell inbox — send a markdown message to another shell, check your unread inbox, mark messages read. Driven by `./sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.
category: substrate
common: true
---

# messaging — the shell inbox

One shell writes a markdown message to another; the recipient discovers it on its
next boot via the `## STATUS` `Inbox:` count, surfaces it with `check`, and clears
it with `mark-read`. Body is markdown — preserved verbatim.

Drive it with **`./sc mem message`**, never raw `sqlite3`. `shell_messages` lives
in the engine DB, and `./sc mem` resolves + guards *this* DB (from any cwd,
including a worktree where a literal `.super-coder/shell_db.db` path would create
an empty stub and drop the message) and snapshots the send so it survives a
rebuild. The sender is you; recipients are addressed by `shortname`.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> | mark-read <id>`

## check — your unread inbox

```
./sc mem message check [N]      # N optional; default 50, max 200
```

`check` is read-only — it does **not** auto-mark-read. Surface the body to the
operator (and reply if warranted, which is itself a `send`), then `mark-read` the
inbound in the same turn.

## send — message another shell

```
./sc mem message send <to-shortname> "<body>"
```

- Multi-word body = a single quoted argument; markdown is preserved verbatim.
- Examples: `./sc mem message send cartographer "map is stale — re-run ./sc map"`
  · `./sc mem message send cc "spec ready for review — see flag SC-014"`
- Unknown / deleted recipient → `mem: recipient shortname '<x>' unknown`. Empty
  body → `mem: body is empty`. Surface either to the operator plainly.

## mark-read — clear an inbox item (idempotent)

```
./sc mem message mark-read <message_id>
```

Access control: you can only mark read a message addressed to **you** — one for
another shell is a no-op. Re-marking a read message is also a no-op. Pass the
`message_id` that `check` surfaced.

## Stance

On boot, if the `## STATUS` `Inbox:` line is non-zero, run `--message check` and
surface the first item before continuing. A reply is a new `send` — there is no
threading; include `Re: <topic>` in the body if it matters. Keep the inbox honest:
mark-read only once you've actually acted on the message.
