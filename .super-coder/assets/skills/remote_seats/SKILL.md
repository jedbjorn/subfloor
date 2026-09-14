---
name: remote_seats
description: Drive a machine through the host brokers — posture 1 owns a VM's lifecycle with sc vm; posture 2 runs broker-served SSH exec, push, and pull against named remotes with sc remote. Keys never enter the shell. Opt-in; Linux targets need nothing more, Windows adds windows_testing.
category: substrate
command: sc remote
common: false
---

# remote_seats — two postures, one broker

A shell drives another machine only through the host-side vm-broker. It holds
the libvirt connection, every SSH key, and the known-hosts files; the shell
calls `./sc vm` and `./sc remote`, which speak to the broker's unix socket, and
never runs `ssh`, `scp`, or `virsh` itself — on bare metal or in a sandbox.
That is the whole access model (decision #353). Two postures share it
(decision #355):

| Posture | Who owns lifecycle | Shell verbs |
|---|---|---|
| 1 · host to VM | this engine, through `./sc vm` | adopt, status, start, stop, restart, snapshot, bake, reset (`--running` or `--off`), push, pull, exec, capture |
| 2 · VM to VM | the FnB or the upstream instance | `./sc remote` status, exec, push, pull |

Under posture 2 lifecycle and snapshots are someone else's job: never ask for
a start, reset, or snapshot on a named remote; report the need instead.

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
means. Either way the key is generated and kept on the host and never enters a
shell (decision #353); `windows_testing` carries the Windows detail.

Remote names match `[a-z0-9][a-z0-9-]{0,31}`. `key_path` is an absolute,
host-owned, mode-0600 file; the broker refuses anything else. Without
`known_hosts_path` the broker pins the host key on first contact into its own
file under `.sc-state/local/remotes/`. `./sc remote list` and
`./sc remote remove <name>` manage the block; no output ever includes key
material.

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

Lifecycle verbs beside those: `stop` (graceful; `--force` is the only route to
`virsh destroy`), `restart`, `snapshot list`, `snapshot create <name>` (allowed
in any state — a running domain gets a live snapshot, and a hypervisor that
refuses one answers `snapshot_live_unsupported`), `snapshot delete <name>` (the
configured baseline is refused; redefine it with `bake` instead), `bake [<name>]`
(graceful shutdown, then a replace-not-stack offline snapshot that becomes the
baseline; `./sc vm-bake` is an alias), and `reset [<name>] --off|--running` for a
named snapshot. Exactly one of `--off` and `--running` is required on every
reset. `push` sources and `pull` destinations must sit inside the repo or
`.sc-state/local/`. Add `--json` to any verb for one result object.

Posture 1's VM is a disposable test box, so the lifecycle verbs above are
genuinely yours to use — snapshot, bake and reset included (decision #372).

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

Every verb returns `{ok, operation, error: {code, message}}` on failure. Act on
the code; never repair the transport yourself.

| Code | Meaning | Do |
|---|---|---|
| `broker_unreachable` | vm-broker is not serving | report the printed start command to the FnB |
| `remote_not_found`, `remote_name_invalid` | name undeclared or malformed | check `./sc remote list`; ask the FnB to add it |
| `remote_key_invalid`, `remote_config_invalid` | key path, mode, or fields wrong on the host | report; the FnB owns keys |
| `remote_path_not_allowed` | push source or pull destination outside the allowed roots | move the file inside the repo or `.sc-state/local/` |
| `remote_unreachable`, `remote_exec_failed` | SSH failed or the command exited non-zero | read `stderr`; report, do not retry blindly |
| `snapshot_protected`, `snapshot_name_invalid` | a lifecycle guard tripped | the baseline is redefined with `bake`, never deleted |
| `snapshot_live_unsupported` | libvirt refused a live internal snapshot | `./sc vm stop`, snapshot, then start again |
| `stop_timeout`, `reset_result_unknown` | the final state was not confirmed | run `status`, report it, do not retry blindly |
| `adopt_sandboxed` | `adopt` is host-only; it needs `virsh`, `ssh-keygen`, `scp` and a TTY | ask the FnB to run it on the host |
| `adopt_guest_not_found` | no address resolved from DHCP, ARP or `--ssh-host` | report; the FnB passes `--ssh-host` |
| `adopt_ssh_timeout` | the guest never answered on TCP 22 in the wait window | the bootstrap line has not run in the guest yet |
| `adopt_key_install_failed` | the password-authenticated key install did not take | report; key material is the FnB's |
| `adopt_provision_failed`, `adopt_verify_failed` | a provisioning step or a declared check failed | read the named step; fix `.subfloor/winbox.json` or the guest, re-run adopt |

Never hand-install keys, aliases, or known-hosts entries anywhere, and never
open a raw `ssh` to a target the broker serves — key material stays host-side
(decision #353). If a target needs something the brokers do not give you, stop
and report it.
