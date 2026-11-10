# Tailnet broker

**Posture: current operator runbook.** Applies to a fork with this optional
host broker configured. Host setup and lifecycle belong to its authorized
operator; Planner supplies fork-local shell guidance. Example names, ports and
application tables below are illustrative and do not grant access to another
fork. Exact CLI syntax remains in `sc help --all` and verb help.


The host-side process that lets a shell run commands on declared tailnet
hosts through the host's own Tailscale node. Sibling of the
[VM and remotes broker](remote-seats.md); same shape, different backend.

## What matters is the machine, not the route

The tailnet is a **transport**. What a shell may do is decided by the **class
of machine** at the other end:

| Class | Examples | Posture | How it is declared |
|---|---|---|---|
| Test seat | a VM kept for testing, including one that travels with the owner and is only reachable over the tailnet | Disposable and **not protected**. A mistake costs a snapshot restore, so a shell may run anything there and reset it to the kept snapshot without asking. Refreshing a shared snapshot is the owner's call (decision #434). | `allowed_hosts` |
| Machine in real use | the owner's workstations, servers, production | No undo, so shells get read-only diagnostics. | `readonly_hosts` |

Reaching a test seat over the tailnet is an ordinary, allowed use of this
broker — the route does not make a seat any more or less disposable.

The read-only tier is a precaution against accidents, not a statement of
distrust: these guards are confusion rails, not containment (decisions #427,
#428). This page describes only what is actually enforced:

- **Enforced by the broker:** `exec` is refused for a host the `ts` block does
  not declare, and a `readonly_hosts` entry accepts only the diagnostic
  command table below.
- **Not enforced by the broker:** anything a process does outside it. A
  bare-metal shell runs as the operator's own account, where the `tailscale`
  CLI is in reach; nothing in the engine stops it being run directly.
- **What actually protects a real machine:** the tailnet policy (the ACL/SSH
  policy JSON in the Tailscale admin console). Which node may open an SSH
  session to which machine, as which user, is decided there. Keep real
  machines out of reach of the node's SSH grants, or grant a low-privilege
  user, if the read-only table is not enough for them.

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
   the container `curl`s the socket and holds nothing. As with the typed VM
   client using `vm-broker`, the credential has one home: it is never copied
   into a container or a fork.

The socket transport is filesystem-namespace, not network-namespace, so it works
from the container with **no route, no firewall, no new host surface**. It is
fs-perm gated (0600) — reachable only by processes sharing the bind mount.

## Two tiers of declared hosts

A tailnet has many hosts, so the loop verbs are parameterized by
`{host, command}` rather than acting on one saved target, and the broker
refuses `exec` for any host the `ts` block does not declare (fail-closed), so
a mistyped or guessed name never reaches a machine nobody listed. Declared
hosts come in two tiers, one per class above:

- `allowed_hosts` — test seats: unrestricted exec as the host's user.
- `readonly_hosts` — machines in real use: implicitly allowed, but `/exec`
  accepts only the fixed diagnostic verb table below (decision #354). A host
  in both lists is read-only.

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
  "ssh_users": {"deploy-target": "deploy"},
  "tailscale_bin": "tailscale"
}
```

| Field | Meaning |
|---|---|
| `ssh_user` | default remote user for `tailscale ssh` (`user@host`) |
| `ssh_users` | optional `{host: user}` map: that host's own remote user, overriding `ssh_user`. Absent on a block that never needed it; a host without an entry uses `ssh_user` |
| `allowed_hosts` | the tailnet hosts this fork may `exec` against without restriction (fail-closed scoping) |
| `readonly_hosts` | hosts limited to the diagnostic verb table; implicitly allowed |
| `tailscale_bin` | path/name of the tailscale CLI (default `tailscale`) |

Write it with the client, not by hand:

```bash
./sc ts init --ssh-user tester --allowed-host build-box --readonly-host blade
./sc ts add-host deploy@deploy-target      # one more test seat, with its own user
./sc ts add-host blade2 --readonly         # one more machine in real use
./sc ts remove-host build-box              # stop declaring one host
```

- **Hosts are added, never dropped as a side effect.** `init` folds the hosts
  it is given into the saved lists and reports `hosts.added`, `hosts.kept` and
  `hosts.removed`. Pass `--replace-hosts` to replace each list that is named
  (the behaviour `init` had before); the hosts it drops are listed in
  `hosts.removed`. `add-host` and `remove-host` change one host and nothing
  else.
- **`[USER@]HOST`** on `--allowed-host`, `--readonly-host` and `add-host`
  records that host's own user in `ssh_users`.
- **Tiers only tighten.** Naming an allowed host read-only moves it to
  `readonly_hosts`. Naming a read-only host as allowed leaves it read-only and
  says so; `add-host` refuses with `host_is_readonly`. Loosening a host is two
  deliberate commands: `remove-host`, then `add-host`.
- **The link is tested when it is declared.** `init` and `add-host` save the
  block, start the broker if it is not answering (on the host; from a sandbox
  they print the exact command instead), then run the setup checks and print
  pass/fail per check with the next command for each failure: `daemon`, `auth`,
  then `peer` and `ssh` for the hosts the command named (every declared host
  when it named none). A check that depends on a failed one is reported
  skipped, not failed again. A failed check exits non-zero with
  `ts_verify_failed` and `saved: true` — the block **is** saved, so fix the
  cause and re-run. `--no-verify` saves without testing or starting anything,
  for offline setup.
- Every command writes only the `ts` block and keeps values it was not given.

## Read-only tier

For a host in `readonly_hosts` the broker accepts a command only when its first
tokens match `READONLY_COMMANDS` in `ts.py` (kept as data so a test can
enumerate it), the string contains none of `; & | > < $ \` ( )`, a newline or a
carriage return, and `sudo` appears nowhere:

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
against the table. The table is enforced inside the broker and nowhere else:
the `tailscale_diagnostics` skill tells the shell that it never reaches a
read-only host with `tailscale` or `ssh` directly and that a refusal is a
stop, and that instruction — not a mechanism — is what keeps a bare-metal
shell on the broker path. Device-side enforcement on the target is deferred,
not rejected.

## Routes

All JSON `{ok, ...}`. The broker acts on the **saved** `ts` block + a caller-named
host; `/validate` tests a **candidate** block passed in the body (before save).

| Method | Route | Does |
|---|---|---|
| `GET` | `/health` | liveness |
| `GET` | `/ts` | read the saved `ts` block |
| `PUT` | `/ts` `{ts}` | write the `ts` block; a failed save answers `{ok: false, error: ts_write_failed, message}` instead of closing the connection |
| `GET` | `/status` | `tailscale status --json` → self + peers summary |
| `POST` | `/exec` `{host, command, timeout?}` | `tailscale ssh` → `{ok, exit, stdout, stderr}`; on a `readonly_hosts` entry, off-table input → `{error: readonly_refused, token}` |
| `POST` | `/validate/{check}` `{ts, hosts?}` | one live setup check: `daemon` · `auth` · `peer` · `ssh`; `hosts` narrows `peer`/`ssh` to the named declared hosts (without it `ssh` probes the first declared host) |

## Running it (on the HOST — never in the sandbox)

`ts_broker.py` refuses to start under `SC_SANDBOX`. Same supervision model as
vm-broker: `./sc launch` brings it up (and `./sc down` stops it) automatically
when a tailnet is linked, so it tracks the sandbox lifecycle.

```
./sc ts-broker            run the broker in the foreground (unix socket)
./sc ts-broker-up         start it backgrounded (nohup + pidfile); when no tailnet is linked it
                          starts nothing and says so, with the command that links one
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
./sc ts add-host [USER@]HOST [--readonly] [--no-verify]
./sc ts remove-host HOST
```

`init`, `add-host` and `remove-host` are described under
[Link config](#link-config--the-ts-block-allowed_hosts-and-readonly_hosts).
Each verb prints one JSON object and exits non-zero on `ok: false`. The socket
is still there for liveness checks (`curl -s --unix-socket "$(./sc
ts-broker-sock)" http://ts/health`), and a sandboxed shell reaches it through
the engine bind mount exactly like the VM broker.

## Error codes

The `error` field of a failed result. Act on the code.

| Code | Meaning | Next |
|---|---|---|
| `ts_not_linked` | no `ts` block exists, so there is nothing for the broker to serve — `./sc ts-broker-up` starts nothing in this state | `./sc ts init --ssh-user <user> --allowed-host <host>` |
| `broker_unreachable` | a tailnet **is** linked but the broker is not answering; the result names the operation, the transport error and `start_command` | `./sc ts-broker-up` on the host |
| `ts_verify_failed` | the block is saved (`saved: true`) but a setup check failed; `verify.checks[]` carries `check`, `ok`, `skipped`, `output` and `next` | the `next` of each failed check |
| `ts_write_failed` | the block could **not** be saved; the previous block is still in effect. From `PUT /ts` this means the broker process could not write the instance file | run `./sc ts init` / `./sc ts add-host` in the operator's own terminal, which writes the file directly |
| `host_not_declared` | `remove-host` named a host in neither list; nothing changed | `./sc ts init` output or `GET /ts` lists the declared hosts |
| `host_is_readonly` | `add-host` named a read-only host without `--readonly`; nothing changed | `./sc ts remove-host <host>`, then `add-host` |
| `host_invalid` | a `[USER@]HOST` argument has no host or contains whitespace | fix the argument |
| `readonly_refused` | `exec` on a `readonly_hosts` entry used input outside the diagnostic table; `token` names what was refused | a refusal is a stop, not something to rephrase around |

A refused `exec` for an undeclared host answers `{ok: false, exit: -1,
stderr: "host '<h>' is not in allowed_hosts or readonly_hosts"}` with no
`error` code.

## Limits

- **GUI wizard** for the `ts` link — not built. Link from the CLI with
  `./sc ts init` / `./sc ts add-host`; the `GET/PUT /api/ts` and `POST
  /api/ts/validate/{check}` endpoints exist for a future form.
- **`/push`** (artifact transfer over the tailnet) — `exec` closes the primary loop.
- **`tailscale up` / node provisioning** from the broker — stays **link-only**, like
  vm; the operator authenticates the host node once.
- Device-side read-only enforcement (an observer account or Tailscale SSH
  ACLs) is not set up by the engine — deferred by decision #354. What the
  engine enforces today is the broker's command table; what protects a real
  machine beyond that is the tailnet policy the owner maintains.
