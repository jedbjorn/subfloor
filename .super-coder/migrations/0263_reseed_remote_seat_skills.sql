-- 0263 — reseed the remote-seat skills (feature #80, spec #230): opt-in
-- remote_seats and tailscale_diagnostics, and windows_testing rewritten as the
-- Windows-only add-on with every lease/ForceCommand paragraph removed. All
-- three are common=0; feature grants stay with the FnB/Planner. No schema
-- change: a full-body UPSERT converges upgraded installations on the same
-- text a fresh seed produces. Idempotent.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'remote_seats',
  'Drive a machine through the host brokers — posture 1 owns a VM''s lifecycle with sc vm; posture 2 runs broker-served SSH exec, push, and pull against named remotes with sc remote. Keys never enter the shell. Opt-in; Linux targets need nothing more, Windows adds windows_testing.',
  'substrate',
  'sc remote',
  0,
  '# remote_seats — two postures, one broker

A shell drives another machine only through the host-side vm-broker. It holds
the libvirt connection, every SSH key, and the known-hosts files; the shell
calls `./sc vm` and `./sc remote`, which speak to the broker''s unix socket, and
never runs `ssh`, `scp`, or `virsh` itself — on bare metal or in a sandbox.
That is the whole access model (decision #353). Two postures share it
(decision #355):

| Posture | Who owns lifecycle | Shell verbs |
|---|---|---|
| 1 · host to VM | this engine, through `./sc vm` | status, start, stop, restart, snapshot, reset, push, exec, capture |
| 2 · VM to VM | the FnB or the upstream instance | `./sc remote` status, exec, push, pull |

Under posture 2 lifecycle and snapshots are someone else''s job: never ask for
a start, reset, or snapshot on a named remote; report the need instead.

## Set up once per fork

The FnB links the targets; each command writes only its own block of the
gitignored `instance.json` and reports whether the broker is up plus the
command that starts it (`./sc vm-broker-up`). Nothing here installs a guest,
an SSH server, or a toolchain.

```bash
./sc vm init --domain <libvirt-domain> --snapshot <baseline> --transfer-dir <host-share> \
  --ssh-host <guest-address> --ssh-user <guest-user> --ssh-key-path </abs/host/key>
./sc remote add <name> --host <address> --user <user> --key-path </abs/host/key> [--port 22] [--known-hosts-path </abs/path>]
./sc ts init ...        # Tailscale diagnostics tier; see tailscale_diagnostics
```

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
./sc vm push <repo-file> [<dest>]    # stages into the transfer share
./sc vm exec -- <command>            # or --command-file <utf-8 file>
./sc vm capture [--output <path>]    # console screenshot under .sc-state/local/vm-captures
./sc vm reset --off                  # back to the configured baseline, powered off
```

Lifecycle verbs beside those: `stop` (graceful; `--force` is the only route to
`virsh destroy`), `restart`, `snapshot list`, `snapshot create <name>` (domain
must be shut off), `snapshot delete <name>` (the configured baseline is
refused), and `reset <name> --off` for a named snapshot. `--off` is required
on every reset; `push` sources must sit inside the repo. Add `--json` to any
verb for one result object.

Sharing rule: the broker''s mutation lock is the only concurrency control, so
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
| `snapshot_protected`, `snapshot_requires_off`, `snapshot_name_invalid` | a lifecycle guard tripped | the guard is the answer; do not work around it |
| `stop_timeout`, `reset_result_unknown` | the final state was not confirmed | run `status`, report it, do not retry blindly |

Never hand-install keys, aliases, or known-hosts entries anywhere, and never
open a raw `ssh` to a target the broker serves. If a target needs something
the brokers do not give you, stop and report it.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'tailscale_diagnostics',
  'Observe declared tailnet hosts through the host ts-broker with sc ts — tailnet status and a fixed read-only diagnostic verb table on readonly_hosts. Opt-in; a readonly_refused answer is a stop, never something to work around.',
  'substrate',
  'sc ts',
  0,
  '# tailscale_diagnostics — look, do not touch

The host''s Tailscale identity stays on the host. The ts-broker runs there and
serves two verbs over a unix socket; `./sc ts` is the client. The fork''s `ts`
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
bare metal or otherwise, even when the operator''s own tailnet identity or a
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
is down — report the printed start command.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'windows_testing',
  'Windows-only add-on to remote_seats — PowerShell exec conventions, capture, the managed Windows-MCP GUI transport, and guest prerequisites for a posture 1 Windows VM. Opt-in; load with remote_seats.',
  'substrate',
  'sc vm',
  0,
  '# windows_testing — the Windows add-on

`remote_seats` owns setup, the session shape, sharing, snapshots, and errors.
This skill adds only what a Windows guest changes. Linux guests never need it.

## Guest prerequisites (the FnB installs; the engine does not)

- Windows OpenSSH server with `ssh_user` in local Administrators; `ssh_key_path`
  on the host is that account''s key. The default SSH shell is `cmd.exe`.
- PowerShell available on `PATH`. A stock `Restricted` execution policy
  rejects `-File` scripts; a disposable test image may set
  `Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy Bypass -Force` and
  verify with `Get-ExecutionPolicy -List`. Apply that only to a throwaway guest
  holding no data, credentials, or logged-in sessions; a snapshot reset removes
  later changes, not a secret baked into the baseline.
- A writable workspace such as `C:\SubfloorTest`, the toolchain under test, and
  the guest-side mount of the transfer share `./sc vm push` stages into.
- For GUI work, Windows-MCP listening in the guest on the block''s `mcp_port`
  (default 8000).

Confirm each item with `./sc vm status` and one `./sc vm exec`; never install
or reconfigure a guest to repair a failing prerequisite. Report it.

## PowerShell exec conventions

`./sc vm exec` runs under `cmd.exe`, so invoke PowerShell explicitly and
prefer a command file for anything longer than one line:

```bash
./sc vm exec -- powershell -NoProfile -NonInteractive -Command "Get-Service sshd | Select Status"
./sc vm exec --command-file run-tests.ps1.cmd      # exact UTF-8 contents, one command
```

The command file holds the exact text the guest receives; a typical body is
`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\SubfloorTest\run.ps1`
after pushing `run.ps1` through the share. Use Windows paths on the guest side
and `--json` when you need `exit_code`, `stdout`, and `stderr` separately.
Guest console output is decoded lossily; base64-encode guest-side
(`[Convert]::ToBase64String(...)`) when a result must be byte-exact, and write
large results to the share rather than stdout.

## Capture

`./sc vm capture [--output <path>]` saves a console screenshot as a mode-0600
file under `.sc-state/local/vm-captures/`; an explicit `--output` must stay
there. Installers and dialogs show state on screen, so capture before and after
a GUI step and after any timeout.

## Windows-MCP transport

```bash
./sc vm mcp status      # adapter, tunnel, relay, endpoint — read-only
./sc vm mcp up          # SSH-forward the guest''s MCP port to run/vm-mcp.sock, start the relay, probe the endpoint
./sc vm mcp down        # stop relay and tunnel; reports each separately
```

Supported harnesses receive a managed `windows-mcp` adapter definition;
`mcp up` is what brings that endpoint online (in a sandbox it also runs the
in-container relay on 127.0.0.1:18000). Do not add an MCP server by hand or
edit harness configuration. Drive the GUI through the MCP tools by element,
verify each step with a capture, and `mcp down` before `reset --off`.

## Session end

Finish as `remote_seats` says: `./sc vm mcp down` if you brought it up, then
`./sc vm reset --off`, and report test results separately from the reset
result. A `reset_result_unknown` answer is reported as-is; run `./sc vm status`
and stop rather than retrying the reset.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
