# Tailnet broker

**Posture: current operator runbook.** Applies to a fork with this optional
host broker configured. Host setup and lifecycle belong to its authorized
operator; Planner supplies fork-local shell guidance. Example names, ports and
application tables below are illustrative and do not grant access to another
fork. Exact CLI syntax remains in `sc help --all` and verb help.


The host-side authority that lets a shell observe and diagnose declared
tailnet hosts without ever holding a tailnet credential. Sibling of the
[VM and remotes broker](remote-seats.md); same shape, different backend.

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
   the container `curl`s the socket and holds nothing. As with the typed VM client using `vm-broker`: one host process
   holds the credential so nothing downstream needs it.

The socket transport is filesystem-namespace, not network-namespace, so it works
from the container with **no route, no firewall, no new host surface**. It is
fs-perm gated (0600) — reachable only by processes sharing the bind mount.

## Two tiers of declared hosts

A tailnet has many hosts, so the loop verbs are parameterized by
`{host, command}` rather than acting on one saved target, and the `ts` block
carries the scoping policy (fail-closed): a shell may only `exec` against hosts
the fork has declared, so a compromised sandbox cannot reach arbitrary tailnet
nodes. Declared hosts come in two tiers:

- `allowed_hosts` — unrestricted exec as `ssh_user`.
- `readonly_hosts` — implicitly allowed, but `/exec` accepts only the fixed
  diagnostic verb table below (decision #354). A host in both lists is
  read-only.

## Link config — the `ts` block: `allowed_hosts` and `readonly_hosts`

Lives under the `ts` key of `.super-coder/instance.json` (gitignored, per-instance
— so there is no schema migration; the tailnet is a host resource, not shell
state). It coexists with the `vm` block; `ports.py` preserves keys it does not
own. It holds **no secret material** — the host node's identity is the credential
and it never leaves the host:

```json
"ts": {
  "ssh_user": "tester",
  "allowed_hosts": ["build-box", "deploy-target"],
  "readonly_hosts": ["blade"],
  "tailscale_bin": "tailscale"
}
```

| Field | Meaning |
|---|---|
| `ssh_user` | remote user for `tailscale ssh` (`user@host`) |
| `allowed_hosts` | the tailnet hosts this fork may `exec` against without restriction (fail-closed scoping) |
| `readonly_hosts` | hosts limited to the diagnostic verb table; implicitly allowed |
| `tailscale_bin` | path/name of the tailscale CLI (default `tailscale`) |

Write it with `./sc ts init --ssh-user tester --allowed-host build-box
--readonly-host blade`; the command writes only the `ts` block, keeps values
it was not given, and prints whether the broker is up plus the command that
starts it.

## Read-only tier

For a host in `readonly_hosts` the broker accepts a command only when its first
tokens match `READONLY_COMMANDS` in `ts.py` (kept as data so a test can
enumerate it), the string contains none of `; & | > < $ \` ( )` or a newline,
and `sudo` appears nowhere:

| Verb | Permitted forms |
|---|---|
| `systemctl` | `status`, `is-active`, `list-units`, `list-timers` |
| `journalctl` | any flags except `--vacuum*` and `--rotate` |
| `pm2` | `status`, `list`, `describe`, `logs --nostream` |
| `docker` | `ps`, `logs`, `inspect`, `stats --no-stream`, `images` |
| host facts | `df`, `free`, `uptime`, `ps`, `ss`, `ip`, `nproc`, `hostname`, `whoami`, `id` |
| files | `ls`, `stat`, `cat`, `head`, `tail`, `du` |

Anything else returns `{ok: false, error: "readonly_refused", token: "<what>"}`
naming the offending token. A host only in `allowed_hosts` is not checked
against the table. This is broker allowlist plus skill doctrine: the
`tailscale_diagnostics` skill tells the shell, in one paragraph, that it never
reaches a read-only host with `tailscale` or `ssh` directly and that a refusal
is a stop. Device-side enforcement on the target is deferred, not rejected.

## Routes

All JSON `{ok, ...}`. The broker acts on the **saved** `ts` block + a caller-named
host; `/validate` tests a **candidate** block passed in the body (before save).

| Method | Route | Does |
|---|---|---|
| `GET` | `/health` | liveness |
| `GET` | `/ts` | read the saved `ts` block |
| `PUT` | `/ts` `{ts}` | write the `ts` block |
| `GET` | `/status` | `tailscale status --json` → self + peers summary |
| `POST` | `/exec` `{host, command, timeout?}` | `tailscale ssh` → `{ok, exit, stdout, stderr}`; on a `readonly_hosts` entry, off-table input → `{error: readonly_refused, token}` |
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

## Client verbs

Shells use the typed client instead of raw socket routes:

```bash
./sc ts status                          # backend state, self, peers
./sc ts exec build-box -- uptime        # one command; policy-checked on readonly_hosts
./sc ts exec blade -- journalctl -u app --since -1h
./sc ts init --ssh-user tester --allowed-host build-box --readonly-host blade
```

Each prints one JSON object and exits non-zero on `ok: false`;
`broker_unreachable` names the operation and the transport error. The socket
is still there for liveness checks (`curl -s --unix-socket "$(./sc
ts-broker-sock)" http://ts/health`), and a sandboxed shell reaches it through
the engine bind mount exactly like the VM broker.

## Limits

- **GUI wizard** for the `ts` link — the `GET/PUT /api/ts` + `POST
  /api/ts/validate/{check}` endpoints already make it settable + testable; hand-edit
  the block for now.
- **`/push`** (artifact transfer over the tailnet) — `exec` closes the primary loop.
- **`tailscale up` / node provisioning** from the broker — stays **link-only**, like
  vm; the operator authenticates the host node once.
- Device-side read-only enforcement (an observer account or Tailscale SSH
  ACLs) — deferred by decision #354; the boundary today is the broker table
  plus skill doctrine.
