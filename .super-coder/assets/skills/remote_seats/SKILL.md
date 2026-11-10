---
name: remote_seats
description: Drive a test VM or named remote through the host vm-broker with sc vm and sc remote; load before touching a test seat or named remote. Posture 1 owns a test VM's lifecycle (start, exec, push/pull, snapshot, reset); posture 2 runs exec, push, and pull against named remotes. Opt-in; Linux targets need nothing more, Windows adds windows_testing.
category: substrate
command: sc remote
common: false
---

# remote_seats — two postures, one broker

## Posture

GitHub has history and a test VM has snapshots, so a mistake there is
recoverable. A machine in real use — a workstation, a server, production — has
no undo, so there you run read-only diagnostics unless your mandate or an
explicit FnB assignment covers the change. That is a precaution against accidents, not distrust. The class of
machine decides, not the route: a test seat reached over the tailnet is still
a test seat.

A test seat is disposable and shared. Any shell may reset one to its snapshot
at the end of its own session without asking, on every OS. Refreshing the
shared snapshot itself is the owner's call: a shell that keeps reinstalling
the same dependencies proposes a snapshot refresh to the FnB instead of
absorbing the cost (decision #434).

## Access model

Reach a target through the host-side vm-broker: `./sc vm` and `./sc remote`
speak to its unix socket, on bare metal or in a sandbox. The broker holds the
libvirt connection, the SSH keys, and the known-hosts files, so key material
has one host-side home and never lands in a worktree, a log, or a sandbox
(decision #353). This is key hygiene, not a mechanism that confines you: use
the broker instead of raw `ssh`, `scp`, or `virsh` because it pins host keys,
serializes sessions behind one lock, and returns structured results. Two
postures share it (decision #355):

| Posture | Who owns lifecycle | Shell verbs |
|---|---|---|
| 1 · host to VM | this engine, through `./sc vm` | adopt, status, start, stop, restart, snapshot, bake, reset (`--running` or `--off`), push, pull, exec, capture |
| 2 · VM to VM | the FnB or the upstream instance | `./sc remote` status, exec, push, pull |

`./sc remote` has no lifecycle verbs. When a named remote is a test seat, its
fork skill names the reset procedure; otherwise report the need for a start,
reset, or snapshot instead of improvising one.

## Set up once per fork

The FnB links the targets; each command writes only its own block of the
gitignored `instance.json` and reports whether the broker is up plus the
command that starts it (`./sc vm-broker-up`).

```bash
./sc vm adopt --domain <libvirt-domain> --ssh-user <guest-account>
./sc vm init --domain <libvirt-domain> --snapshot <baseline> \
  --ssh-host <guest-address> --ssh-user <guest-user> --ssh-key-path </abs/host/key>
./sc remote add <name> --host <address> --user <user> --key-path </abs/host/key> [--port 22] [--known-hosts-path </abs/path>]
./sc ts init ...        # Tailscale diagnostics tier; see tailscale_diagnostics
```

`adopt` is the normal path for a Windows guest: the operator runs one bootstrap
line in the guest console, then `adopt` installs a host-held key, provisions the
guest, writes the block and takes the baseline snapshot. It is host-only and
idempotent. `init` is the hand-link path for a guest already prepared by other
means. `windows_testing` carries the Windows detail.

Remote names match `[a-z0-9][a-z0-9-]{0,31}`. `key_path` is an absolute,
host-owned, mode-0600 file; the broker refuses anything else. Without
`known_hosts_path` the broker pins the host key on first contact into its own
file under `.sc-state/local/remotes/`. `./sc remote list` and
`./sc remote remove <name>` manage the block; no output includes key material.

## Posture 1 session

```bash
./sc vm status                       # broker, domain, SSH readiness, MCP tunnel
./sc vm start                        # only when off; waits for SSH
./sc vm push <local-file> [<dest>]   # scp to the guest; dest defaults under the workspace
./sc vm pull <guest-path> <dest>     # scp back into the repo or .sc-state/local/
./sc vm exec -- <command>            # or --command-file <utf-8 file>
./sc vm capture [--output <path>]    # console screenshot under .sc-state/local/vm-captures
./sc vm reset --off                  # back to the configured baseline, powered off
```

Lifecycle verbs beside those:

- `stop` is graceful; `--force` is the only route to `virsh destroy`. `restart`.
- `snapshot list`, `snapshot create <name>` (any state; a running domain gets a
  live snapshot, and a hypervisor that refuses one answers
  `snapshot_live_unsupported`), `snapshot delete <name>` (the configured
  baseline is refused).
- `reset [<name>] --off|--running`; exactly one of the two flags is required.
- `bake [<name>]`: graceful shutdown, then a replace-not-stack offline snapshot
  that becomes the baseline. `./sc vm-bake` runs the same operation against
  libvirt in-process with no broker — host-only, never in the sandbox, and only
  when the broker is down.

`push` sources and `pull` destinations must sit inside the repo or
`.sc-state/local/`, and never inside `.sc-state/local/vm/` (the host's key and
host-key pin live there); a pull destination inside `.super-coder/` or `.git/`
is refused too. Add `--json` to any verb for one result object.

The VM is a disposable test box, so install, reconfigure, snapshot, and reset
are yours (decision #434). `bake` replaces the baseline every later session
resets to: that is the snapshot refresh the same decision leaves to the owner,
so propose it and bake when the FnB asked for it or agreed.

Sharing rule: the broker's mutation lock is the only concurrency control, so
read `status` first, do not start, reset, or snapshot a VM another shell is
visibly using, and finish your own session with `reset --off` while you still
have control. The engine supervises nothing after you stop (decision #101).

## Posture 2 session

```bash
./sc remote status <name>
./sc remote exec <name> -- <command>          # or --command-file <utf-8 file>
./sc remote push <name> <repo-file> <remote-path>
./sc remote pull <name> <remote-path> <local-path>
```

`push` sources resolve inside the repo; `pull` destinations resolve inside the
repo or `.sc-state/local/`. Treat the remote as shared: leave it as you found
it and say what you changed.

## Structured errors

Every verb returns `{ok, operation, error: {code, message}}` on failure. The
full vocabulary is the Structured errors table in
`.super-coder/docs/remote-seats.md`; read the row for a code not listed here.
The ones you act on:

| Code | Do |
|---|---|
| `vm_not_linked` | no `vm` block exists; starting the broker fixes nothing. Ask the FnB to link a VM with `./sc vm adopt` or `./sc vm init` |
| `remote_not_found` | the name is undeclared (also the answer when the broker is down and the remote was never added); check `./sc remote list`, then ask the FnB to add it |
| `broker_unreachable` | a seat is linked but the broker is down; report the printed start command (`./sc vm-broker-up`) to the FnB |
| `vm_busy` | another call holds the mutation lock and nothing was attempted; wait, then retry |
| `remote_path_not_allowed` | move the file inside the repo or `.sc-state/local/` |
| `remote_unreachable`, `remote_exec_failed`, `exec_failed`, `push_failed`, `pull_failed` | read `stderr` or the message; report, do not retry blindly |
| `snapshot_live_unsupported` | normal on many hosts: `./sc vm stop`, snapshot, then start again |
| `snapshot_protected` | the baseline is redefined with `bake`, never deleted |
| `stop_timeout`, `reset_result_unknown`, `<operation>_timeout` | the final state is unconfirmed and the work may still be running; read `status`, report it, do not retry blindly |
| any `adopt_*` | adoption is the FnB's host-only command (`adopt_sandboxed` says so from a sandbox); report the code and message verbatim. `adopt_host_key_changed` names a pin file and the `rm` that clears it — leave that removal to the FnB. After `adopt_broker_failed` the `vm` block IS written: the resume is `./sc vm-broker-up`, then adopt again |

Other codes fall into two groups. Argument errors are yours to correct:
`remote_name_invalid`, `snapshot_name_invalid`, `pull_source_invalid`,
`pull_destination_invalid`, `push_source_invalid`, and an empty command's
`exec_validation_failed`. Host-configuration errors are reported: keys
(`remote_key_invalid`), the saved block (`remote_config_invalid`,
`bake_config_write_failed`), the host's OpenSSH (`scp_unsupported`,
`exec_unavailable`). Do not hand-install keys, aliases, or known-hosts entries,
and do not open a raw `ssh` to a target the broker serves. If a target needs
something the broker does not give you, stop and report it.
