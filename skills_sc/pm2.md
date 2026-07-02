---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# pm2

Observe + manage the host's pm2-supervised app stack through the host-side pm2-broker — status, app health, log tails, and scoped restarts, holding no host access yourself. The admin shell's deploy-confirmation companion. Use when verifying a deploy, checking the live app, or bouncing a declared process.

**Category:** substrate

---

# pm2 — driving the host process stack

How a sandboxed shell observes and manages the host's pm2-supervised app:
process status, the app's live health, log tails, and scoped restarts. This is
**opt-in and link-only** — the operator brings a host whose app is already
pm2-managed; you drive a scoped loop against it. Grant is explicit, per-fork
(`common=0`); it belongs to the **admin** (fork infra owner) and **devops**
flavors. You have it because someone granted it to your shell.

## You drive pm2 through the host broker — not pm2 directly

You run inside the sandbox container. There is no `pm2` binary here, and the
host's ports are 127.0.0.1-bound — even a read-only `curl host:8000/health` has
no route. So you do **not** shell out — you call the **host-side pm2-broker**
over a unix socket in the bind-mounted repo. The broker runs pm2 and curls the
app's local port where they work; nothing about your isolation changes. (See
`.super-coder/docs/pm2-broker.md`. It is the third sibling of the Windows VM
and tailnet brokers — `windows_devkit` and `tailscale` work the exact same way.)

The socket path comes from `sc pm2-broker-sock`. Every verb is a `curl`:

```bash
SOCK="$(sc pm2-broker-sock)"
curl -s --unix-socket "$SOCK" http://pm2/health      # liveness check first
```

If the curl fails with "not reachable", the broker isn't running — ask the
operator to start it on the host: `sc pm2-broker-up`. You cannot start it
yourself (it runs on the host, not in your sandbox).

## Precondition — the link is configured

The stack lives in `.super-coder/instance.json` under the `pm2` key. It carries
**no secret material** — pm2 and the app stay host-side:

```json
"pm2": { "processes": ["myapp-api", "myapp-ui"],
         "health_url": "http://127.0.0.1:8000/health",
         "allow_lifecycle": false }
```

No `pm2` block → no stack linked: stop and ask the operator to set it
(hand-edit, or `PUT /api/pm2`). `processes` is a **fail-closed allow-list** —
every verb (even `status`) works only against names listed there; an
empty/absent list denies all. You see what the fork declared, never the host's
full process table.

## The verbs

| Verb | Call |
|---|---|
| **status** | `curl -s --unix-socket "$SOCK" http://pm2/status` → `{processes: [{name, status, pid, uptime_s, restarts, cpu, memory}], missing: []}` |
| **app-health** | `curl -s --unix-socket "$SOCK" http://pm2/app-health` → `{ok, code, body}` — the broker curls `health_url` host-side |
| **logs** | `curl -s --unix-socket "$SOCK" http://pm2/logs -d '{"proc":"myapp-api","lines":100}'` → `{out, err}` tails (capped at 1000) |
| **restart** | `curl -s --unix-socket "$SOCK" http://pm2/restart -d '{"proc":"myapp-api"}'` → `{ok, exit, stdout, stderr}` |
| **stop / start** | same shape as restart — but **gated**: refused unless the block sets `"allow_lifecycle": true` |

`restart` rides the allowlist alone — it is the deploy verb (bounce a process
so it loads what a `make deploy` put on disk). `stop`/`start` can leave the app
down, so they need the extra opt-in.

## The loop this exists for — deploy confirmation

A host-run `make deploy` is invisible from the sandbox. With the broker, the
live-app half of a deploy audit is verifiable instead of a human hand-off:

1. `status` — every declared process `online`, restart counts sane.
2. `restart` the affected process(es) if the deploy didn't already.
3. `status` again — uptime reset, still `online`, restarts didn't spiral.
4. `app-health` — the app answers on its health URL.
5. `logs` — no fresh stack traces in `err`.

## Stance

- **Drive, don't hold.** You operate the stack through the broker; you never
  get pm2, a host shell, or a route to host ports. Link-only stays link-only.
- **Declare your processes.** `processes` is the blast radius. Keep it to what
  the fork actually manages; widen it deliberately, not by default.
- **Restart is not stop.** A restart heals; a stop is an outage. If you think
  you need stop/start, say why to the operator — `allow_lifecycle` is their
  call, not yours.
- **A running process is not a working app.** `status: online` + `app-health`
  green together confirm a deploy; either alone can lie (a crash-looping
  process reports online between crashes; a stale process can serve health).
- **Lanes.** The fork's infra + deploys are yours (admin/devops). App features
  are dev's; the super-coder engine is admin's via `self_update`.
