---
name: tailscale_diagnostics
description: Observe declared tailnet hosts through the host ts-broker with sc ts; load before running anything on a tailnet host. Gives tailnet status and a fixed read-only diagnostic verb table on readonly_hosts. Read-only is the default on machines in real use; a test seat reached over the tailnet is still a test seat. Opt-in; a readonly_refused answer is a stop, never something to work around.
category: substrate
command: sc ts
common: false
---

# tailscale_diagnostics — look, do not touch

## Posture

Read-only applies to machines in real use: workstations, servers, production.
They have no undo, so the default there is to observe, and you change one
only when your mandate or an explicit FnB assignment covers the change. That
is a precaution against accidents, not distrust. GitHub has history and test VMs
have snapshots, so mistakes there are recoverable; a test seat reached over
the tailnet is still a test seat. The class of machine decides, not the route:
on a test seat you work freely and may reset it to its snapshot at the end of
your own session without asking, while refreshing a shared snapshot stays the
owner's call (decision #434; `remote_seats` holds the seat rules).
`readonly_hosts` is the tier with no exception: nothing on it is changed.

## The broker

The ts-broker runs on the host with the host's Tailscale identity and serves
two verbs over a unix socket; `./sc ts` is the client. Keeping that identity
host-side is hygiene, not a mechanism that confines you. The fork's `ts` block
names `readonly_hosts` (machines in real use: diagnostic verbs only) and
`allowed_hosts` (unrestricted exec as the configured `ssh_user`; a test seat
belongs here). A host in `readonly_hosts` is implicitly allowed; a host in
neither list is refused.

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
enforces the verb table only on its own path; nothing on the device enforces
it, so this paragraph is the rest of the boundary (decision #354), kept by you
because that machine has no undo.

## What a read-only host accepts

The broker accepts a command on a `readonly_hosts` entry only when its first
tokens match this table, the string contains none of `; & | > < $ \` ( )` or a
newline or carriage return, and `sudo` appears nowhere. The table lives as data
in `ts.py` (`READONLY_COMMANDS`); trailing operands select a unit, container,
or file.

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
run it from another host, or open your own session to the target. The broker
does not restrict a host that is only in `allowed_hosts`, so the class of
machine still decides there: a test seat is yours to change, and a machine in
real use stays read-only unless your mandate or an explicit FnB assignment
covers the change.

Other refusals: `not in allowed_hosts or readonly_hosts` means the fork has
not declared the host — ask the FnB; `ts_not_linked` means no `ts` block
exists, so `./sc ts-broker-up` starts nothing — ask the FnB to run
`./sc ts init`; `broker_unreachable` means a tailnet is linked but the
ts-broker is down — report the printed start command.
