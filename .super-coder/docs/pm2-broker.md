# pm2 broker

The host-side authority that lets a sandboxed shell observe + manage the host's
pm2-supervised app stack without holding any host access. Third sibling of the
[Windows VM broker](windows-vm-broker.md) and the
[tailnet broker](tailscale-broker.md); same shape, different backend.

Upstreamed from a fork's deploy-confirmation gap
([super-coder#254](https://github.com/jedbjorn/subfloor/issues/254)): an
admin shell mandated to own its fork's infra could not see or bounce the pm2
stack it was responsible for.

## Why a broker, not pm2-in-the-container

A fork's shells run inside the sandbox container (`.super-coder/Dockerfile`):
no pm2 binary, and the host publishes its ports on `127.0.0.1` only — even a
read-only `curl host:8000/health` has no route. Isolation is deliberate. But an
admin/devops shell still needs the live-app half of its job: is the stack up,
did the deploy take, what do the error logs say, bounce the process that loaded
stale code. Two ways to give it that:

1. **Put pm2 / a host route in the container.** Rejected. The pm2 daemon is
   effectively arbitrary host code execution (`pm2 start <anything>`), and
   opening the host port bind widens every fork's network surface whether or
   not it uses pm2.

2. **A host-side broker over a unix socket.** Chosen. pm2 and the app stay on
   the **host**; the broker exposes whitelisted verbs over a unix socket in the
   bind-mounted engine dir; the container `curl`s the socket and holds nothing.
   Exactly how `windows_devkit` sits on `vm-broker` and `tailscale` on
   `ts-broker`: one host process holds the capability so nothing downstream
   needs it.

The socket transport is filesystem-namespace, not network-namespace, so it works
from the container with **no route, no firewall, no new host surface**. It is
fs-perm gated (0600) — reachable only by processes sharing the bind mount.

## Scoping: declared processes only

Like the tailnet (N hosts), a pm2 daemon supervises N processes — some of them
none of the fork's business. So every verb, **including `status`**, is
fail-closed on the `pm2` block's `processes` allowlist: the sandbox sees and
acts on what the fork declared, and cannot enumerate the host's process table.
Empty/absent list denies all.

Lifecycle verbs are tiered:

- `restart` — allowlist only. It is the deploy verb (bounce a process so it
  runs what `make deploy` put on disk) and it heals; pm2 brings the process
  straight back.
- `stop` / `start` — allowlist **plus** `"allow_lifecycle": true`. A stopped
  app is an outage; that surface is an explicit second opt-in.
- `delete` — not a verb. Removing a process from supervision is topology
  change, not operation; it stays host-side with the operator.

## Link config — the `pm2` block

Lives under the `pm2` key of `.super-coder/instance.json` (gitignored,
per-instance — so there is no schema migration; the process stack is a host
resource, not shell state). It coexists with the `vm` / `ts` blocks; `ports.py`
preserves keys it does not own. It holds **no secret material**:

```json
"pm2": {
  "processes": ["myapp-api", "myapp-ui"],
  "health_url": "http://127.0.0.1:8000/health",
  "pm2_bin": "pm2",
  "allow_lifecycle": false
}
```

| Field | Meaning |
|---|---|
| `processes` | the pm2 process names this fork may see + act on (fail-closed scoping) |
| `health_url` | the app's local health endpoint — curled **host-side** by the broker (optional) |
| `pm2_bin` | path/name of the pm2 CLI (default `pm2`) |
| `allow_lifecycle` | opt-in for `stop`/`start` (default `false`; `restart` is always available) |

## Routes

All JSON `{ok, ...}`. The broker acts on the **saved** `pm2` block; `/validate`
tests a **candidate** block passed in the body (before save).

| Method | Route | Does |
|---|---|---|
| `GET` | `/health` | liveness (of the broker, not the app) |
| `GET` | `/pm2` | read the saved `pm2` block |
| `PUT` | `/pm2` `{pm2}` | write the `pm2` block |
| `GET` | `/status` | parsed `pm2 jlist` → `{processes: [{name, status, pid, uptime_s, restarts, cpu, memory}], missing}` — declared processes only |
| `GET` | `/app-health` | curl `health_url` host-side → `{ok, code, body}` |
| `POST` | `/logs` `{proc, lines?}` | tail one process's out+err logs (capped 1000 lines) |
| `POST` | `/restart` `{proc}` | `pm2 restart` → `{ok, exit, stdout, stderr}` |
| `POST` | `/stop` / `/start` `{proc}` | same, gated by `allow_lifecycle` |
| `POST` | `/validate/{check}` `{pm2}` | one live setup check: `daemon` · `procs` · `health` |

## Running it (on the HOST — never in the sandbox)

`pm2_broker.py` refuses to start under `SC_SANDBOX`. Same supervision model as
its siblings: `./sc launch` brings it up (and `./sc down` stops it)
automatically when a stack is linked, so it tracks the sandbox lifecycle.

```
./sc pm2-broker            run the broker in the foreground (unix socket)
./sc pm2-broker-up         start it backgrounded (nohup + pidfile); self-skips if unlinked/up
./sc pm2-broker-down       stop the backgrounded broker
./sc pm2-broker-sock       print the socket path
./sc pm2-broker-install    supervise via a systemd --user unit (survives logout/reboot)
./sc pm2-broker-uninstall  remove the systemd unit
```

A shell in the container reaches it exactly like the other brokers:

```bash
SOCK="$(./sc pm2-broker-sock)"
curl -s --unix-socket "$SOCK" http://pm2/health
curl -s --unix-socket "$SOCK" http://pm2/status
curl -s --unix-socket "$SOCK" http://pm2/restart -d '{"proc":"myapp-api"}'
```

## Deferred (not in v1)

- **GUI wizard** for the `pm2` link — the `GET/PUT /api/pm2` + `POST
  /api/pm2/validate/{check}` endpoints already make it settable + testable;
  hand-edit the block for now (like the tailnet).
- **`sc pm2 <verb>` CLI passthrough** — the skill curls the socket directly,
  matching how `windows_devkit`/`tailscale` consume their brokers; a passthrough
  would be a second client for the same verbs.
- **`delete` / ecosystem edits** — supervision topology stays host-side.
- **Log streaming/follow** — verbs return; tails are capped. Re-poll instead.
