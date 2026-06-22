# Tailnet broker

The host-side authority that lets a sandboxed shell drive the tailnet without
ever holding a tailnet credential. Sibling of the [Windows VM
broker](windows-vm-broker.md); same shape, different backend.

> Canonical architecture decision: the host-side-broker-over-in-container choice
> is recorded in CC's `shell_decisions` (the substrate's memory DB). This doc is
> the repo-facing mirror, not the source of record.

## Why a broker, not tailscale-in-the-container

A fork's shells run inside the sandbox container (`.super-coder/Dockerfile`): no
ssh, no host network path, bound to `sc-net`/127.0.0.1 only — isolation is
deliberate. A devops shell still needs to reach hosts over the tailnet. Two ways
to give it that:

1. **Bake `tailscaled` into the image.** Rejected. It would (a) put a reusable
   **tailnet node credential inside every fork's sandbox** (`tailscaled.state`),
   and (b) require `CAP_NET_ADMIN` + `/dev/net/tun` to bring up the interface — a
   real isolation regression for every fork, whether or not it uses tailscale.

2. **A host-side broker over a unix socket.** Chosen. `tailscaled` + the tailnet
   identity stay on the **host** (already `tailscale up`, authenticated once).
   The broker exposes verbs over a unix socket in the bind-mounted engine dir;
   the container `curl`s the socket and holds nothing. This is exactly how
   `windows_devkit` (skill) sits on `vm-broker` (engine dep): one host process
   holds the credential so nothing downstream needs it.

The socket transport is filesystem-namespace, not network-namespace, so it works
from the container with **no route, no firewall, no new host surface**. It is
fs-perm gated (0600) — reachable only by processes sharing the bind mount.

## One difference from vm-broker: N hosts

The Windows VM is a single fixed target; a tailnet has many hosts. So the loop
verbs are parameterized by `{host, command}` rather than acting on one saved
target, and the `ts` config block carries an `allowed_hosts` **scoping policy**
(fail-closed): a shell may only `exec` against hosts the fork has declared, so a
compromised sandbox cannot reach arbitrary tailnet nodes.

## Link config — the `ts` block

Lives under the `ts` key of `.super-coder/instance.json` (gitignored, per-instance
— so there is no schema migration; the tailnet is a host resource, not shell
state). It coexists with the `vm` block; `ports.py` preserves keys it does not
own. It holds **no secret material** — the host node's identity is the credential
and it never leaves the host:

```json
"ts": {
  "ssh_user": "tester",
  "allowed_hosts": ["build-box", "deploy-target"],
  "tailscale_bin": "tailscale"
}
```

| Field | Meaning |
|---|---|
| `ssh_user` | remote user for `tailscale ssh` (`user@host`) |
| `allowed_hosts` | the tailnet hosts this fork may `exec` against (fail-closed scoping) |
| `tailscale_bin` | path/name of the tailscale CLI (default `tailscale`) |

## Routes

All JSON `{ok, ...}`. The broker acts on the **saved** `ts` block + a caller-named
host; `/validate` tests a **candidate** block passed in the body (before save).

| Method | Route | Does |
|---|---|---|
| `GET` | `/health` | liveness |
| `GET` | `/ts` | read the saved `ts` block |
| `PUT` | `/ts` `{ts}` | write the `ts` block |
| `GET` | `/status` | `tailscale status --json` → self + peers summary |
| `POST` | `/exec` `{host, command, timeout?}` | `tailscale ssh` → `{ok, exit, stdout, stderr}` |
| `POST` | `/validate/{check}` `{ts}` | one live setup check: `daemon` · `auth` · `peer` · `ssh` |

## Running it (on the HOST — never in the sandbox)

`ts_broker.py` refuses to start under `SC_SANDBOX`. Same supervision model as
vm-broker: `./sc launch` brings it up (and `./sc down` stops it) automatically
when a tailnet is linked, so it tracks the sandbox lifecycle.

```
./sc ts-broker            run the broker in the foreground (unix socket)
./sc ts-broker-up         start it backgrounded (nohup + pidfile); self-skips if unlinked/up
./sc ts-broker-down       stop the backgrounded broker
./sc ts-broker-sock       print the socket path
./sc ts-broker-install    supervise via a systemd --user unit (survives logout/reboot)
./sc ts-broker-uninstall  remove the systemd unit
```

A shell in the container reaches it exactly like the VM broker:

```bash
SOCK="$(./sc ts-broker-sock)"
curl -s --unix-socket "$SOCK" http://ts/health
curl -s --unix-socket "$SOCK" http://ts/status
curl -s --unix-socket "$SOCK" http://ts/exec -d '{"host":"build-box","command":"uptime"}'
```

## Deferred (not in v1)

- **GUI wizard** for the `ts` link — the `GET/PUT /api/ts` + `POST
  /api/ts/validate/{check}` endpoints already make it settable + testable; hand-edit
  the block for now.
- **`/push`** (artifact transfer over the tailnet) — `exec` closes the primary loop.
- **`tailscale up` / node provisioning** from the broker — stays **link-only**, like
  vm; the operator authenticates the host node once.
- A unified remote-target interface over vm-broker + ts-broker.
