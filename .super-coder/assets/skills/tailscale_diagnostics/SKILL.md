---
name: tailscale_diagnostics
description: Observe declared tailnet hosts through the host ts-broker with sc ts — tailnet status and a fixed read-only diagnostic verb table on readonly_hosts. Opt-in; a readonly_refused answer is a stop, never something to work around.
category: substrate
command: sc ts
common: false
---

# tailscale_diagnostics — look, do not touch

The host's Tailscale identity stays on the host. The ts-broker runs there and
serves two verbs over a unix socket; `./sc ts` is the client. The fork's `ts`
block names `allowed_hosts` (unrestricted exec as the configured `ssh_user`)
and `readonly_hosts` (diagnostic verbs only). A host in `readonly_hosts` is
implicitly allowed; a host in neither list is refused.

```bash
./sc ts status                        # tailnet backend, self, peers
./sc ts exec <host> -- <command>      # one policy-checked command
./sc ts init --ssh-user <user> [--allowed-host H]... [--readonly-host H]... [--tailscale-bin <path>]
```

`init` writes only the `ts` block and prints whether the broker is ready and
the command that starts it (`./sc ts-broker-up`). Every result is one JSON
object: `{ok, exit, stdout, stderr}` for `exec`.

## Read-only doctrine

A shell never reaches a read-only host with `tailscale` or `ssh` directly, on
bare metal or otherwise, even when the operator's own tailnet identity or a
login would let it; the only path to a `readonly_hosts` entry is
`./sc ts exec`, and observation means observation — read state and logs,
report what you saw, and change nothing on that host by any route. The broker
allowlist plus this paragraph is the accepted boundary (decision #354);
device-side enforcement is deferred, not a gap you are free to use.

## What a read-only host accepts

The broker accepts a command on a `readonly_hosts` entry only when its first
tokens match this table, the string contains none of `; & | > < $ \` ( )` or a
newline, and `sudo` appears nowhere. The table lives as data in `ts.py`
(`READONLY_COMMANDS`); trailing operands select a unit, container, or file.

| Verb | Permitted forms |
|---|---|
| `systemctl` | `status`, `is-active`, `list-units`, `list-timers` |
| `journalctl` | any flags except `--vacuum*` and `--rotate` |
| `pm2` | `status`, `list`, `describe`, `logs --nostream` |
| `docker` | `ps`, `logs`, `inspect`, `stats --no-stream`, `images` |
| host facts | `df`, `free`, `uptime`, `ps`, `ss`, `ip`, `nproc`, `hostname`, `whoami`, `id` |
| files | `ls`, `stat`, `cat`, `head`, `tail`, `du` |

Pipes and redirections are refused, so ask for the raw output and filter it on
your side.

## On `readonly_refused`

The broker answers `{ok: false, error: "readonly_refused", token: "<what>"}`
and names the offending token. Stop. Report the host, the exact command, and
the token to the FnB or the assigning shell. Do not rephrase the command to
slip past the table, split it into permitted pieces that add up to a change,
run it from another host, or open your own session to the target. A host that
is only in `allowed_hosts` is unrestricted by the broker, which is why the
fork declares which hosts are which; do not treat that as permission to
mutate production.

Other refusals: `not in allowed_hosts or readonly_hosts` means the fork has
not declared the host — ask the FnB; `broker_unreachable` means the ts-broker
is down — report the printed start command.
