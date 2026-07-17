---
name: messaging
description: Shell-to-shell inbox — send a markdown message to another shell (typed: shell/task/result; pr_event is daemon-emitted), check your unread inbox, verify delivery via the sent view, mark messages read. Driven by `sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.
category: substrate
command: sc mem message
common: true
---

# messaging — the shell inbox

Shell-to-shell markdown messages, driven by `sc mem message`. Sender = you;
recipient addressed by `shortname`. Body = markdown, preserved verbatim.
Recipient discovers it on its next boot via the `## STATUS` `Inbox:` count.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | sent | mark-read <id>`

## Message kinds

Every message carries a `kind` — the trail stays filterable
(`SELECT * FROM shell_messages WHERE kind != 'shell'` replays a sprint's
whole coordination history):

- `shell` — ordinary shell-to-shell mail (the default; what `send` does
  unless told otherwise).
- `task` — planner → worker instruction (a sprint kickoff / re-task).
- `result` — worker → planner completion or transition report.
- `pr_event` — GitHub watcher daemon → shell PR transition (checks
  green/red, review submitted, merged, closed). Daemon-emitted only:
  `send` refuses it — a forged PR event would poison the wake loop's
  ground truth. Detail lives in `gh`; the row is the wake-up, not the
  payload.

## check — your unread inbox

```
sc mem message check [N]      # N optional; default 50, max 200
```

Read-only — it does NOT auto-mark-read. Non-`shell` rows show their kind
inline. Surface the body to the operator (reply if warranted — a reply is
itself a `send`), then `mark-read` the inbound in the same turn.

## send — message another shell

```
sc mem message send <to-shortname> "<body>" [--kind shell|task|result]
```

- Multi-word body = one quoted argument; markdown preserved verbatim.
- Examples: `sc mem message send cartographer "map is stale — re-run sc map"`
  · `sc mem message send plan1 "sprint 12: unit 3 merged (PR #41)" --kind result`
- `cartographer` is a **role alias**: when no shell has that literal
  shortname, it resolves to the fork's cartographer shell whatever its
  shortname (e.g. `CART1`). Address the map-keeper as `cartographer` — no
  shortname lookup needed. An exact shortname match always wins.
- Unknown / deleted recipient -> `mem: recipient shortname '<x>' unknown`;
  empty body -> `mem: body is empty`. Surface either to the operator plainly.
- Sends are idempotent under load: each invocation carries a dedupe key, so
  a timed-out send retries itself and can never write a duplicate. Do NOT
  re-run a timed-out send by hand — the retry already happened; if it still
  died, check `sent` first.

## sent — your outbound view

```
sc mem message sent           # latest 50 you sent, newest first, read receipts
```

Verify delivery after an ambiguous failure (a send that died after its
retries) before ever resending. A row present = delivered; absent = safe
to resend.

## mark-read — clear an inbox item (idempotent)

```
sc mem message mark-read <message_id>
```

Pass the `message_id` that `check` surfaced. Only messages addressed to you
clear — another shell's message = no-op; re-marking a read message = no-op.

## Stance

- On boot, `Inbox:` non-zero -> run `--message check` and surface the first
  item before continuing.
- No threading: a reply = a new `send`; include `Re: <topic>` in the body if
  it matters.
- `mark-read` only after you have actually acted on the message.
