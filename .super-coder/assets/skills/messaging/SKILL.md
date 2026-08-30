---
name: messaging
description: Shell-to-shell messaging — ordinary `sc mem message` inbox mail by default, with wake delivery only when explicitly instructed. Use to send, check, verify, or mark ordinary messages and to follow a named wake-producing workflow.
category: substrate
command: sc mem message
common: true
---

# messaging — ordinary inbox + explicit wakes

Choose delivery before sending:

| Mode | Use when | Surface |
|---|---|---|
| Normal message | Default for every shell-to-shell message. | `sc mem message` |
| Wake message | The operator or a loaded workflow explicitly instructs a wake. | The exact wake-producing command named by that instruction/workflow. |

Urgency, message kind, or a desire for a prompt response does not authorize a
wake. An explicit wake instruction with no supported command -> surface the
missing capability and stop. Use only the supported message commands; do not
call internal Python or send both modes unless the instruction requires both.

## Normal messages — the shell inbox

Normal messages are shell-to-shell markdown driven by `sc mem message`.
Sender = you; recipient addressed by `shortname`; body preserved verbatim.
The recipient discovers the message on its next boot via the `## STATUS`
`Inbox:` count. `sc mem message send` always sends this normal mode; it never
wakes or rotates a chat.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | sent | mark-read <id>`

## Message kinds

Every message carries a `kind`, so ordinary mail, delegated tasks, and
completion evidence remain independently filterable:

- `shell` — ordinary shell-to-shell mail (the default; what `send` does
  unless told otherwise).
- `task` — a bounded instruction for another shell.
- `result` — worker → planner completion or transition report.

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
  · `sc mem message send plan1 "feature 12 task 3 complete (PR #41)" --kind result`
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

## Wake messages — active chat delivery

A wake message creates durable delivery intent for the recipient's active
chat. Pending wakes coalesce per receiver; one wake turn drains every
undelivered wake message for that shell. A wake does not enter the normal
`sc mem message` inbox or `sent` view.

Use only the wake type selected by the instruction/workflow:

| Recipient state | Delivery result |
|---|---|
| Verified live turn | Re-enter at the turn's natural boundary. |
| Idle registered chat | Any coalesced New rotates; all Re-enter resumes. |
| No registered chat | Create a chat and deliver as New. |

Engine-wide wakes need no Sprint. Sprint-scoped wakes deliver only while that
Sprint is armed; a producer may create delivery intent earlier and leave it
queued. Typed Sprint commands and engine producers return their durable
message/wake receipt. Receipt present = complete; do not add a normal-message
duplicate.

## Stance

- On boot, `Inbox:` non-zero -> run `--message check` and surface the first
  item before continuing.
- No threading: a reply = a new `send`; include `Re: <topic>` in the body if
  it matters.
- `mark-read` only after you have actually acted on the message.
