---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# messaging

Shell-to-shell inbox — send a markdown message to another shell (typed: shell/task/result; pr_event is daemon-emitted), check your unread inbox, mark messages read. Driven by `sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.

**Category:** substrate

---

# messaging — the shell inbox

Shell-to-shell markdown messages, driven by `sc mem message`. Sender = you;
recipient addressed by `shortname`. Body = markdown, preserved verbatim.
Recipient discovers it on its next boot via the `## STATUS` `Inbox:` count.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | mark-read <id>`

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
- Unknown / deleted recipient -> `mem: recipient shortname '<x>' unknown`;
  empty body -> `mem: body is empty`. Surface either to the operator plainly.

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
