-- 0264 — reseed the winbox adoption skills (feature #82, spec #232):
-- windows_testing gains two-command guest adoption, the shell's authority over
-- the disposable test box (decision #372) and the Windows-MCP recovery path;
-- remote_seats gains the adopt/bake/pull verbs, reset --running|--off and the
-- new structured-error codes. Both stay common=0 substrate skills; feature
-- grants stay with the FnB/Planner. No schema change: a full-body UPSERT on
-- name keeps skill_id, so existing grants survive, and converges upgraded
-- installations on the same text a fresh seed produces. Idempotent.

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
| 1 · host to VM | this engine, through `./sc vm` | adopt, status, start, stop, restart, snapshot, bake, reset (`--running` or `--off`), push, pull, exec, capture |
| 2 · VM to VM | the FnB or the upstream instance | `./sc remote` status, exec, push, pull |

Under posture 2 lifecycle and snapshots are someone else''s job: never ask for
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

Posture 1''s VM is a disposable test box, so the lifecycle verbs above are
genuinely yours to use — snapshot, bake and reset included (decision #372).

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
| `snapshot_protected`, `snapshot_name_invalid` | a lifecycle guard tripped | the baseline is redefined with `bake`, never deleted |
| `snapshot_live_unsupported` | libvirt refused a live internal snapshot | `./sc vm stop`, snapshot, then start again |
| `stop_timeout`, `reset_result_unknown` | the final state was not confirmed | run `status`, report it, do not retry blindly |
| `adopt_sandboxed` | `adopt` is host-only; it needs `virsh`, `ssh-keygen`, `scp` and a TTY | ask the FnB to run it on the host |
| `adopt_guest_not_found` | no address resolved from DHCP, ARP or `--ssh-host` | report; the FnB passes `--ssh-host` |
| `adopt_ssh_timeout` | the guest never answered on TCP 22 in the wait window | the bootstrap line has not run in the guest yet |
| `adopt_key_install_failed` | the password-authenticated key install did not take | report; key material is the FnB''s |
| `adopt_provision_failed`, `adopt_verify_failed` | a provisioning step or a declared check failed | read the named step; fix `.subfloor/winbox.json` or the guest, re-run adopt |

Never hand-install keys, aliases, or known-hosts entries anywhere, and never
open a raw `ssh` to a target the broker serves — key material stays host-side
(decision #353). If a target needs something the brokers do not give you, stop
and report it.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'windows_testing',
  'Windows-only add-on to remote_seats — two-command guest adoption, the shell''s authority over the box, PowerShell exec conventions, capture, and the managed Windows-MCP GUI transport for a posture 1 Windows VM. Opt-in; load with remote_seats.',
  'substrate',
  'sc vm',
  0,
  '# windows_testing — the Windows add-on

`remote_seats` owns setup, the session shape, sharing, snapshots, and errors.
This skill adds only what a Windows guest changes. Linux guests never need it.

## Preparing a Windows guest

Bring-your-own-licence: the operator supplies a booted, licensed Windows 10 or
11 guest with a working network and a local administrator account. Adoption is
two commands. First, in an elevated PowerShell on the guest console:

```powershell
powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=''Tls12''; irm https://raw.githubusercontent.com/jedbjorn/subfloor/<ref>/.super-coder/assets/winbox/bootstrap.ps1 | iex"
```

`<ref>` is the engine''s pinned commit — `.super-coder/engine.ref` when that file
exists, else `main`; `./sc vm adopt` prints the exact line. Then, on the host:

```bash
./sc vm adopt --domain <libvirt-domain> --ssh-user <account>
```

Adopt locates the guest, installs a host-held key behind one password prompt,
turns password auth off, pins the host key, provisions, verifies, writes the
`vm` block, brings the broker up and takes the baseline snapshot. The key is
generated and kept on the host and never enters a shell (decision #353); the
password is read from the operator''s TTY, never on argv, never persisted. Every
phase is idempotent, so re-running adopt after a toolchain change skips what is
already satisfied. `./sc vm init` still hand-links a guest prepared by other
means.

Provisioning reads the fork-tracked `.subfloor/winbox.json`. Every key is
optional:

```json
{
  "winget_manifest": "winget-manifest.json",
  "checks": ["dotnet --version", "git --version"],
  "mcp": true,
  "mcp_port": 8000,
  "workspace": "C:\\SubfloorTest"
}
```

Defaults: `winget_manifest` is `winget-manifest.json` at the repo root when that
file exists, else none; `checks` empty; `mcp` true; `mcp_port` 8000; `workspace`
`C:\SubfloorTest`. The declared `checks` are also what `./sc vm status` runs as
the toolchain check — none declared means the check passes with "no checks
declared". Changing the fork''s toolchain means editing `winbox.json` and running
`./sc vm adopt` again, or doing it by hand over `./sc vm exec` and then
`./sc vm bake`.

## Authority

The guest is a disposable test box: it holds no real data and no real accounts,
snapshots are free, and the FnB owns the physical host. So install software,
change settings, schedule tasks, snapshot, bake and reset are all yours
(decision #372). Snapshot before a risky change — `./sc vm snapshot create
<name>` works while the domain is running — and reset to it when the change goes
wrong. Redefine the baseline with `./sc vm bake [<name>]` rather than trying to
delete it; the baseline is the one snapshot `reset` depends on and delete still
refuses it. Leave the box in a state the next shell can either use or reset:
finish with `./sc vm reset --off`, or say plainly what you changed and which
snapshot returns the box to the state you found it in. The sharing rule in
`remote_seats` still governs: read `status` first and do not reset or snapshot a
VM another shell is visibly using.

## PowerShell exec conventions

`./sc vm exec` runs under `cmd.exe`, so invoke PowerShell explicitly and
prefer a command file for anything longer than one line:

```bash
./sc vm exec -- powershell -NoProfile -NonInteractive -Command "Get-Service sshd | Select Status"
./sc vm exec --command-file run-tests.ps1.cmd      # exact UTF-8 contents, one command
```

The command file holds the exact text the guest receives; a typical body is
`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\SubfloorTest\run.ps1`
after `./sc vm push run.ps1` lands it in the guest workspace. Use Windows paths
on the guest side and `--json` when you need `exit_code`, `stdout`, and `stderr`
separately. Guest console output is decoded lossily; base64-encode guest-side
(`[Convert]::ToBase64String(...)`) when a result must be byte-exact, and write
large results to a file and `./sc vm pull` it rather than through stdout.

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
verify each step with a capture, and `mcp down` before `reset`.

Provisioning installs the guest side as the per-user login task
`windows-mcp-server`, listening on the guest''s loopback only; the tunnel is the
only route in. When `./sc vm mcp up` reports no listener, the task has not run —
usually no interactive desktop session. Log the adopting account in on the
console, or re-run the install yourself over exec:

```bash
./sc vm exec -- windows-mcp install --transport streamable-http --host 127.0.0.1 --port <mcp_port>
```

That is a legitimate repair under Authority above, not a workaround. Report what
you did.

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
