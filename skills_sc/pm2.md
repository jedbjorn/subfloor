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

Observe + manage the host's pm2-supervised app from the sandbox: process
status, app health, log tails, scoped restarts. Opt-in + link-only
(`common=0`; **admin** + **devops** flavors): the operator brings a host whose
app is already pm2-managed; you drive a scoped loop against it.

## Every verb goes through the host-side pm2-broker

The sandbox has no `pm2` binary and no route to the host's 127.0.0.1-bound
ports — NEVER shell out to pm2 or curl a host port directly (even read-only
`curl host:8000/health` has no route). Call the broker over its unix socket in
the bind-mounted repo; the broker runs pm2 and curls the app's local port
host-side. Detail: `.super-coder/docs/pm2-broker.md`.

```bash
SOCK="$(sc pm2-broker-sock)"
curl -s --unix-socket "$SOCK" http://pm2/health      # liveness check first
```

"not reachable" = broker down -> ask the operator to run `sc pm2-broker-up` on
the host. You cannot start it yourself (host-side process).

## Precondition — `pm2` block in `.super-coder/instance.json`

```json
"pm2": { "processes": ["myapp-api", "myapp-ui"],
         "health_url": "http://127.0.0.1:8000/health",
         "allow_lifecycle": false }
```

- No `pm2` block = no stack linked -> stop; ask the operator to set it
  (hand-edit, or `PUT /api/pm2`).
- `processes` = fail-closed allow-list: every verb (even `status`) works only
  against names listed there; empty/absent list denies all. You see what the
  fork declared, never the host's full process table.
- The block carries no secret material — pm2 + the app stay host-side.

## Verbs

| Verb | Call |
|---|---|
| **status** | `curl -s --unix-socket "$SOCK" http://pm2/status` -> `{processes: [{name, status, pid, uptime_s, restarts, cpu, memory}], missing: []}` |
| **app-health** | `curl -s --unix-socket "$SOCK" http://pm2/app-health` -> `{ok, code, body}` — broker curls `health_url` host-side |
| **logs** | `curl -s --unix-socket "$SOCK" http://pm2/logs -d '{"proc":"myapp-api","lines":100}'` -> `{out, err}` tails (capped at 1000) |
| **restart** | `curl -s --unix-socket "$SOCK" http://pm2/restart -d '{"proc":"myapp-api"}'` -> `{ok, exit, stdout, stderr}` |
| **stop / start** | same shape as restart — refused unless `"allow_lifecycle": true` |

`restart` = the deploy verb (bounce a process so it loads what `make deploy`
put on disk); it rides the allowlist alone. `stop`/`start` can leave the app
down -> they need the extra `allow_lifecycle` opt-in.

## Deploy confirmation — the loop this exists for

A host-run `make deploy` is invisible from the sandbox; this loop makes the
live-app half of a deploy audit verifiable:

1. `status` -> every declared process `online`, restart counts sane.
2. `restart` the affected process(es) if the deploy didn't already.
3. `status` again -> uptime reset + still `online` + restarts didn't spiral.
4. `app-health` -> `ok` on the health URL.
5. `logs` -> no fresh stack traces in `err`.

## Rules

- Drive, don't hold: no pm2 binary, no host shell, no route to host ports —
  link-only stays link-only.
- Keep `processes` to what the fork actually manages (it is the blast
  radius); widen deliberately, not by default.
- Think you need stop/start -> state why to the operator; `allow_lifecycle`
  is their call, not yours.
- A deploy is confirmed only by `status: online` + `app-health` green
  TOGETHER — either alone can lie (a crash-looper reports online between
  crashes; a stale process can serve health).
- Lanes: fork infra + deploys = yours (admin/devops); app features = dev's;
  the super-coder engine = admin's via `self_update`.
