---
name: wake
description: Wake another shell's active chat outside a Sprint; load only when the FnB says "send a wake" or "--wake". Never on your own initiative. Also says how to treat a wake you receive.
category: workflow
common: false
---

# wake — an FnB-directed wake outside a Sprint

A normal message waits in the recipient's inbox. A wake goes straight into
the recipient's active chat as its next turn.

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

A wake arrives headed `Wake from <SHORTNAME>, sent on the FnB's direction`.
Act on it. Do not reply just to acknowledge; reply only when the body asks a
question or you are blocked, and then with a normal message, never a wake.
