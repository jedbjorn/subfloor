# Remote seats — VM and named remotes

**Posture: current operator runbook.** Applies to a fork with the optional
host vm-broker configured. Host setup and lifecycle belong to its authorized
operator; Planner grants the opt-in skills. Example names, paths and hosts
below are illustrative and do not grant access to another fork. Exact CLI
syntax remains in `sc help --all` and verb help.


One host-side broker gives a shell two ways to drive a machine (decision
#355). Posture 1, host to VM: the engine on the host owns one libvirt domain's
lifecycle through `./sc vm`. Posture 2, VM to VM: the same broker runs SSH
against named remotes for `./sc remote`; lifecycle there belongs to the FnB or
the upstream instance. In both, keys stay in host-owned files and the broker
runs `ssh`; the shell never does (decision #353). The sibling
[tailnet broker](tailscale-broker.md) serves the separate read-only
diagnostics tier.

## Why a broker

A fork's shells run in a sandbox container with no route across libvirt NAT,
no `virsh`, no SSH key and no known-hosts material; a bare-metal shell shares
the operator's account and must not become a second place keys live. The
vm-broker runs on the host, where libvirt and the keys are, and exposes verbs
over a unix socket inside the bind-mounted engine dir
(`.super-coder/run/vm-broker.sock`). The socket is fs-perm gated (0600); no
network surface, no auth token, nothing copied into a container. The broker
refuses to start under `SC_SANDBOX`.

## Two blocks, one file

Both live in the gitignored `.super-coder/instance.json`; `ports.py`
preserves keys it does not own, and neither block holds key material.

### `vm` — posture 1

```json
"vm": {
  "domain": "w10c-testing",
  "snapshot": "baseline",
  "transfer_dir": "/srv/vm-share",
  "libvirt_uri": "qemu:///system",
  "ssh_host": "192.168.122.50",
  "ssh_user": "tester",
  "ssh_key_path": "/home/op/.ssh/vm_tester",
  "ssh_port": 22,
  "mcp_port": 8000
}
```

| Field | Meaning |
|---|---|
| `domain` | the one libvirt domain this broker controls |
| `snapshot` | the protected baseline; `reset` defaults to it and `snapshot delete` refuses it |
| `transfer_dir` | host side of the guest's share; `push` stages files here, contained inside it |
| `libvirt_uri` | optional `virsh -c` target |
| `ssh_host`, `ssh_user`, `ssh_key_path`, `ssh_port` | guest SSH for `exec`, readiness waits and the MCP tunnel |
| `mcp_port` | guest port the Windows-MCP tunnel forwards to (default 8000) |

### `remotes` — posture 2

```json
"remotes": {
  "halo": {
    "host": "halo.internal",
    "user": "jedi",
    "port": 22,
    "key_path": "/home/op/.ssh/halo_shell",
    "known_hosts_path": "/home/op/.ssh/halo_known_hosts"
  }
}
```

| Field | Meaning |
|---|---|
| name (key) | matches `[a-z0-9][a-z0-9-]{0,31}`; the shell addresses the remote by it |
| `host`, `user`, `port` | SSH target; `port` defaults to 22 |
| `key_path` | absolute, host-owned, regular file with mode 0600, or every verb is refused with `remote_key_invalid` |
| `known_hosts_path` | optional absolute path; absent means the broker pins the host key on first contact (`accept-new`) into `.sc-state/local/remotes/<name>.known_hosts` (directory mode 0700) |

## Onboarding — three commands

Each writes only its own block and reports whether its broker is up plus the
command that starts it. None of them installs a guest, an SSH server, a key or
a toolchain.

```bash
./sc vm init --domain w10c-testing --snapshot baseline --transfer-dir /srv/vm-share \
  --ssh-host 192.168.122.50 --ssh-user tester --ssh-key-path /home/op/.ssh/vm_tester
./sc remote add halo --host halo.internal --user jedi --key-path /home/op/.ssh/halo_shell
./sc ts init --ssh-user ops --allowed-host build-box --readonly-host blade   # tailnet tier
```

`./sc remote list`, `./sc remote remove <name>`, and the GUI `PUT /vm` wizard
manage the blocks afterwards. `remotes` has no GUI surface yet.

## Routes

All JSON `{ok, ...}`. Verbs act on the saved blocks; `/validate` tests a
candidate `vm` block passed in the body.

| Method | Route | Does |
|---|---|---|
| `GET` | `/health` | liveness |
| `GET` | `/status` | read-only domain, SSH and tunnel state |
| `GET` / `PUT` | `/vm` | read / write the `vm` block |
| `POST` | `/start` | start only if off, then wait for SSH readiness |
| `POST` | `/stop` `{force?}` | `virsh shutdown`, waits for `shut off`; `virsh destroy` only with `force: true` |
| `POST` | `/restart` | graceful stop followed by start readiness |
| `GET` | `/snapshot/list` | names, creation time, current marker |
| `POST` | `/snapshot/create` `{snapshot}` | `snapshot-create-as`; domain must be shut off |
| `POST` | `/snapshot/delete` `{snapshot}` | `snapshot-delete`; refuses the configured baseline |
| `POST` | `/reset` `{snapshot?}` | `snapshot-revert` to the named or default snapshot, left powered off |
| `POST` | `/push` `{src, dest?}` | stage one repo file into `transfer_dir` |
| `POST` | `/exec` `{command}` | ssh the guest → `{ok, exit, stdout, stderr}` |
| `POST` | `/capture` `{command?}` | optional exec plus a `virsh screenshot` (base64) |
| `POST` | `/mcp/up` · `/mcp/down` · `GET /mcp/status` | the Windows-MCP tunnel to the `vm` block's `mcp_port` |
| `GET` | `/remote/<name>/status` | SSH readiness with the broker-held key |
| `POST` | `/remote/<name>/exec` `{command}` | one command on the named remote |
| `POST` | `/remote/<name>/push` `{src, dest}` | scp one repo-contained file to the remote |
| `POST` | `/remote/<name>/pull` `{src, dest}` | scp into the repo or `.sc-state/local/` |

Guest-mutating routes share one mutation lock; a busy broker answers before
attempting the operation. Push sources must resolve inside the repo; pull
destinations inside the repo or `.sc-state/local/`; `push` destinations stay
inside `transfer_dir`. An undeclared remote name, a malformed name, a key with
the wrong mode, or a path outside those roots is refused with a structured
error before any `ssh` or `scp` runs.

## Running it (on the HOST — never in the sandbox)

```
./sc vm-broker            run the broker in the foreground (unix socket)
./sc vm-broker-up         start it backgrounded (nohup + pidfile); self-skips when nothing is linked
./sc vm-broker-down       stop the backgrounded broker
./sc vm-broker-sock       print the socket path
./sc vm-broker-install    supervise via a systemd --user unit (survives logout/reboot)
./sc vm-broker-uninstall  remove the systemd unit
```

`./sc launch` brings the broker up and `./sc down` stops it once a `vm` or
`remotes` block exists. A sandboxed shell reaches the same socket through the
engine bind mount and needs no SSH alias, key or known-hosts file; nothing
under `~/.config/subfloor` or `~/.ssh` is mounted.

## Client verbs

```
./sc vm init|status|start|stop [--force]|restart|snapshot list|create NAME|delete NAME|reset [NAME] --off|push SRC [DEST]|exec -- CMD|capture|mcp status|up|down
./sc remote add NAME --host H --user U --key-path /abs [--port N] [--known-hosts-path /abs] | remove NAME | list | status NAME | exec NAME -- CMD | push NAME SRC DEST | pull NAME SRC DEST
./sc vm-mcp-relay up|down|status      in-sandbox TCP 127.0.0.1:18000 → the broker's vm-mcp.sock (run by `./sc vm mcp up`)
```

Every verb takes `--json` for one result object; failures carry
`{ok: false, error: {code, message}}`. `sc vm test` is gone: the fixed-target
Windows test controller and its Dev-to-Halo SSH route were retired outright
(decision #356); there is no second controller and no session token to hold.

## Skills

Grant them per shell with `sc skill grant <name> <shell>...`; all three are
opt-in (`common: false`).

- `remote_seats` — both postures, the three setup commands, the sharing rule,
  structured-error handling. Linux targets need nothing more.
- `windows_testing` — the Windows add-on: guest prerequisites, PowerShell exec
  conventions, capture, Windows-MCP transport.
- `tailscale_diagnostics` — the read-only tailnet tier; see the
  [tailnet broker](tailscale-broker.md).

## Limits

- A GUI surface for `remotes` — hand-run `./sc remote add` for now.
- Routing the Windows-MCP tunnel to a posture 2 remote — it stays bound to the
  `vm` block.
- Guest provisioning, OpenSSH server setup and toolchains — the skills state
  the prerequisites; the engine installs nothing.
- Multi-tenant scheduling or leases across shells — the mutation lock is the
  only concurrency control; the skill states the sharing rule.
