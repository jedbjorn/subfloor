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
  "libvirt_uri": "qemu:///system",
  "ssh_host": "192.168.122.50",
  "ssh_user": "tester",
  "ssh_key_path": "/home/op/.ssh/vm_tester",
  "ssh_port": 22,
  "known_hosts_path": "/home/op/repo/.sc-state/local/vm/w10c-testing.known_hosts",
  "workspace": "C:\\SubfloorTest",
  "mcp_port": 8000
}
```

| Field | Meaning |
|---|---|
| `domain` | the one libvirt domain this broker controls |
| `snapshot` | the protected baseline; `reset` defaults to it and `snapshot delete` refuses it |
| `libvirt_uri` | optional `virsh -c` target |
| `ssh_host`, `ssh_user`, `ssh_key_path`, `ssh_port` | guest SSH for `exec`, `push`, `pull`, readiness waits and the MCP tunnel |
| `known_hosts_path` | host-owned known-hosts file the guest's key is pinned into during adoption; every later `ssh`/`scp` uses it |
| `workspace` | guest-side working directory; `push` defaults its destination to `<workspace>\<basename>` (default `C:\SubfloorTest`) |
| `mcp_port` | guest port the Windows-MCP tunnel forwards to (default 8000) |

The host-share field older blocks carried is ignored and never written back;
the guest share it named is retired in favour of `scp` over the guest's SSH.

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

## Onboarding a Windows guest — two commands

Bring your own booted, licensed Windows 10 or 11 guest with a working network
and a local administrator account. Adoption is then two commands and about five
minutes.

Host prerequisites: `virsh`, `ssh-keygen`, and an **OpenSSH client 8.7 or
newer**. 8.7 is the floor because `push` and `pull` run `scp -s` to force the
SFTP protocol, which is what keeps a Windows path byte-exact; an older client
rejects `-s` and both verbs answer `scp_unsupported`. Adoption also needs the
operator's terminal for the one password prompt, or `--password-file`.

**1 — in the guest**, from an elevated PowerShell on the console:

```powershell
powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm https://raw.githubusercontent.com/jedbjorn/subfloor/<ref>/.super-coder/assets/winbox/bootstrap.ps1 | iex"
```

`<ref>` is the engine's pinned commit — the contents of `.sc-state/engine.ref`
when that file is present, else `main`. `./sc vm adopt` prints the exact line for
this install, and `--bootstrap-url <url>` overrides it for a branch under test.
The repository is public, so the guest needs only outbound HTTPS; a guest with
no internet can have the file copied in by any means and run it the same way.
The script installs and starts OpenSSH server, opens its firewall rule, makes
key and password authentication explicit, relaxes the execution policy, creates
the workspace, and prints the account name, the guest's addresses and the next
command.

**2 — on the host**:

```bash
./sc vm adopt --domain w10c-testing --ssh-user tester
```

Adopt locates the guest, waits for `sshd`, generates a host-held key and
installs it with one password prompt, turns password authentication off, pins
the guest host key, provisions the guest from `.subfloor/winbox.json`, verifies
the declared checks, writes the `vm` block, brings the broker up and takes the
baseline snapshot. Every phase is idempotent: re-running it skips what is
already satisfied, so a toolchain change is the same command again. The
password is read from the TTY (or `--password-file`), never passed on argv and
never written to disk. Without a TTY and without `--password-file` adopt
refuses up front rather than reading the password with echo on. The engine
never installs or licenses Windows itself.

Adoption reports `warnings` alongside its phases, and the commonest one is a
Windows-MCP listener that has not appeared: `windows-mcp install` registers a
per-user **login** task, so on a guest with no interactive desktop session the
tool is installed and correct and has simply never started. That is a warning,
not a failed adoption. `./sc vm mcp up` reports readiness; the recovery is to
log the adopting account in on the console, or to re-run
`windows-mcp install --transport streamable-http --host 127.0.0.1 --port <mcp_port>`
over `./sc vm exec` once a session exists.

A declared `checks` entry is a command line run through the guest's `cmd.exe`,
so it must not contain a double quote — cmd re-parses the line and the quotes
arrive as literal characters. Put anything that needs quoting in a `.cmd` file
in the workspace and name that file instead.

`./sc vm init` remains for hand-linking a guest you prepared yourself, and no
longer takes `--transfer-dir`:

```bash
./sc vm init --domain w10c-testing --snapshot baseline \
  --ssh-host 192.168.122.50 --ssh-user tester --ssh-key-path /home/op/.ssh/vm_tester
```

The other two seats are one command each:

```bash
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
| `POST` | `/snapshot/create` `{name}` | `snapshot-create-as`; allowed in any state — a running domain gets a live internal snapshot, and a hypervisor that refuses one answers `snapshot_live_unsupported` |
| `POST` | `/snapshot/delete` `{name}` | `snapshot-delete`; refuses the configured baseline — redefine that with `/bake` |
| `POST` | `/bake` `{name?}` | graceful shutdown, then a replace-not-stack offline snapshot that becomes the new baseline; the `vm` block's `snapshot` is updated when `name` is given |
| `POST` | `/reset` `{snapshot?, running}` | `snapshot-revert` to the named or default snapshot; `running` chooses whether the domain is left running or powered off |
| `POST` | `/push` `{src, dest?}` | `scp` one file to the guest; `dest` is a guest path defaulting to `<workspace>\<basename>` |
| `POST` | `/pull` `{src, dest}` | `scp` one file from the guest into the repo or `.sc-state/local/` |
| `POST` | `/exec` `{command}` | ssh the guest → `{ok, exit, stdout, stderr}` |
| `POST` | `/capture` `{command?}` | optional exec plus a `virsh screenshot` (base64) |
| `POST` | `/mcp/up` · `/mcp/down` · `GET /mcp/status` | the Windows-MCP tunnel to the `vm` block's `mcp_port` |
| `GET` | `/remote/<name>/status` | SSH readiness with the broker-held key |
| `POST` | `/remote/<name>/exec` `{command}` | one command on the named remote |
| `POST` | `/remote/<name>/push` `{src, dest}` | scp one repo-contained file to the remote |
| `POST` | `/remote/<name>/pull` `{src, dest}` | scp into the repo or `.sc-state/local/` |
| `POST` | `/validate/<check>` `{vm}` | run one check (`domain`, `ssh`, `snapshot`, `toolchain`) against the CANDIDATE block in the body, before it is saved; an unknown check name is a 404 |

Guest-mutating routes share one mutation lock; a busy broker answers before
attempting the operation. Push sources must resolve inside the repo or
`.sc-state/local/`; pull destinations inside the repo or `.sc-state/local/`. An
undeclared remote name, a malformed name, a key with the wrong mode, or a path
outside those roots is refused with a structured error before any `ssh` or
`scp` runs.

Three sub-trees are carved back out of those roots. `.sc-state/local/vm/` holds
the adoption private key and the pinned known-hosts file, and is excluded from
push sources and pull destinations alike — key material stays on the host
(decision #353). `.super-coder/` and `.git/` are additionally refused as pull
destinations, so guest bytes can never rewrite the running engine or the
repository's own object store. A symlink aimed at any of them is refused too:
the check runs on the resolved path.

Guest paths may be written with either separator (`C:\SubfloorTest\out.bin` or
`C:/SubfloorTest/out.bin`). The engine normalises to forward slashes on the
wire because `scp -s` speaks SFTP, where a backslash is an escape character.

## Structured errors

Every verb and route answers `{ok: false, error: {code, message}}` on failure.
Act on the code.

| Code | Meaning |
|---|---|
| `broker_unreachable` | the vm-broker is not serving; the printed start command is the fix |
| `remote_not_found`, `remote_name_invalid` | the named remote is undeclared or malformed |
| `remote_key_invalid`, `remote_config_invalid` | key path, mode, or block fields are wrong on the host |
| `remote_path_not_allowed` | a push source or a pull destination resolved outside the repo and `.sc-state/local/` |
| `remote_unreachable`, `remote_exec_failed` | SSH failed, or the command exited non-zero |
| `snapshot_protected`, `snapshot_name_invalid` | a lifecycle guard tripped; the baseline is redefined with `bake`, not deleted |
| `snapshot_live_unsupported` | libvirt refused a live internal snapshot (UEFI pflash, non-migratable CPU flags); the normal case on many hosts. Checkpoint offline: `mcp down` if up, `stop`, `snapshot create <name>`, `start`; `reset <name> --running` restores it booted |
| `stop_timeout`, `reset_result_unknown` | the final state was not confirmed; read `status` |
| `adopt_guest_not_found` | no address resolved from the lease table, ARP or `--ssh-host` |
| `adopt_ssh_timeout` | the guest never answered on TCP 22 inside the wait window — the bootstrap line has not run yet |
| `adopt_key_install_failed` | the password-authenticated key install did not take |
| `adopt_provision_failed` | `provision.ps1` reported a failed step |
| `adopt_verify_failed` | a declared `checks` entry or the MCP listener check failed |
| `adopt_harden_failed` | `PasswordAuthentication no` could not be set, or sshd did not come back after the restart |
| `adopt_host_key_changed` | the guest presented a different host key than the pinned one; the message names `.sc-state/local/vm/<domain>.known_hosts` and the `rm` that clears it. Expected after a guest reinstall — remove the pin and re-run adopt |
| `adopt_config_invalid` | a flag or the saved block is unusable before anything runs: no `--domain`, no `--ssh-user` and no saved block, a snapshot name outside `[a-z0-9][a-z0-9-]{0,31}`, or no TTY to read the password from and no `--password-file` |
| `adopt_block_write_failed` | the `vm` block could not be saved, or did not read back as written |
| `adopt_broker_failed` | `./sc vm-broker-up` failed, or the broker never answered after it. **The `vm` block IS written**: start the broker with `./sc vm-broker-up` and re-run adopt, which skips every satisfied phase |
| `adopt_baseline_failed` | the baseline snapshot was not taken (usually the guest did not shut down within the budget) |
| `adopt_sandboxed` | `adopt` is host-only; it needs `virsh`, `ssh-keygen`, `scp` and the operator's TTY |
| `push_failed`, `pull_failed` | `scp` itself failed; the message carries its output |
| `pull_source_invalid`, `pull_destination_invalid`, `push_source_invalid` | a transfer path was empty |
| `scp_unsupported` | this host's `scp` rejects `-s` (force SFTP) — OpenSSH 8.7 or newer is the floor |
| `bake_config_write_failed` | the snapshot was baked but the `vm` block could not be updated to name it as the baseline |
| `<operation>_timeout` | generated per verb (`exec_timeout`, `push_timeout`, `bake_timeout`, …): the broker call exceeded that verb's client budget. The operation may still be running — read `status` before retrying |

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
./sc vm adopt --domain D [--ssh-user U] [--ssh-host H] [--snapshot NAME] [--libvirt-uri URI]
              [--password-file P] [--bootstrap-url URL] [--no-provision] [--wait SECONDS] [--json]
./sc vm init|status|start|stop [--force]|restart|snapshot list|create NAME|delete NAME|bake [NAME]|reset [NAME] (--off|--running)|push SRC [DEST]|pull SRC DEST|exec -- CMD|capture|mcp status|up|down
./sc remote add NAME --host H --user U --key-path /abs [--port N] [--known-hosts-path /abs] | remove NAME | list | status NAME | exec NAME -- CMD | push NAME SRC DEST | pull NAME SRC DEST
./sc vm-mcp-relay up|down|status      in-sandbox TCP 127.0.0.1:18000 → the broker's vm-mcp.sock (run by `./sc vm mcp up`)
```

`./sc vm bake [NAME]` is the broker route and the one shells use.
`./sc vm-bake [NAME]` is the same operation run HOST-DIRECT against libvirt in
the dispatcher's own process, with no broker in the path — the escape hatch for
when the broker is down, and the reason existing operator notes keep working.
Both are host-only for the same reason every `virsh` call is.

Every verb takes `--json` for one result object; failures carry
`{ok: false, error: {code, message}}`. `sc vm test` is gone: the fixed-target
Windows test controller and its Dev-to-Halo SSH route were retired outright
(decision #356); there is no second controller and no session token to hold.

## Skills

Grant them per shell with `sc skill grant <name> <shell>...`; all three are
opt-in (`common: false`).

- `remote_seats` — both postures, the three setup commands, the sharing rule,
  structured-error handling. Linux targets need nothing more.
- `windows_testing` — the Windows add-on: preparing a guest, the shell's
  authority over it, PowerShell exec conventions, capture, Windows-MCP
  transport.
- `tailscale_diagnostics` — the read-only tailnet tier; see the
  [tailnet broker](tailscale-broker.md).

## Limits

- A GUI surface for `remotes` — hand-run `./sc remote add` for now.
- Routing the Windows-MCP tunnel to a posture 2 remote — it stays bound to the
  `vm` block.
- Installing, licensing or activating Windows itself, unattended answer files,
  virtio driver download and VM creation. This is a bring-your-own-licence
  seat: the operator supplies a booted, licensed Windows 10 or 11 guest with a
  working network and an admin account, and the engine takes it from there.
  Guest provisioning above that line — OpenSSH, the key, the toolchain and
  Windows-MCP — is `./sc vm adopt`'s job.
- `adopt` for Linux guests — the bootstrap is PowerShell; use `./sc vm init`.
- Multi-tenant scheduling or leases across shells — the mutation lock is the
  only concurrency control; the skill states the sharing rule.
