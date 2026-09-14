---
name: windows_testing
description: Windows-only add-on to remote_seats — two-command guest adoption, the shell's authority over the box, PowerShell exec conventions, capture, and the managed Windows-MCP GUI transport for a posture 1 Windows VM. Opt-in; load with remote_seats.
category: substrate
command: sc vm
common: false
---

# windows_testing — the Windows add-on

`remote_seats` owns setup, the session shape, sharing, snapshots, and errors.
This skill adds only what a Windows guest changes. Linux guests never need it.

## Preparing a Windows guest

Bring-your-own-licence: the operator supplies a booted, licensed Windows 10 or
11 guest with a working network and a local administrator account. Adoption is
two commands. First, in an elevated PowerShell on the guest console:

```powershell
powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm https://raw.githubusercontent.com/jedbjorn/subfloor/<ref>/.super-coder/assets/winbox/bootstrap.ps1 | iex"
```

`<ref>` is the engine's pinned commit — `.sc-state/engine.ref` when that file
exists, else `main`; `./sc vm adopt` prints the exact line. Then, on the host:

```bash
./sc vm adopt --domain <libvirt-domain> --ssh-user <account>
```

Adopt locates the guest, installs a host-held key behind one password prompt,
turns password auth off, pins the host key, provisions, verifies, writes the
`vm` block, brings the broker up and takes the baseline snapshot. The key is
generated on the host and never handed to a shell (decision #353); the password
is read from the operator's TTY, never on argv, never persisted. Every phase is
idempotent, so re-running adopt after a toolchain change skips what is already
satisfied. `./sc vm init` still hand-links a guest prepared by other means.

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
declared". Each entry is a command line run through the guest's `cmd.exe`, so it
must not contain a double quote: cmd re-parses the line and the quotes arrive as
literal characters. Anything needing quotes goes in a `.cmd` file you push and
name instead. Changing the fork's toolchain means editing `winbox.json` and
running `./sc vm adopt` again, or doing it by hand over `./sc vm exec` and then
`./sc vm bake`.

## Authority

The guest is a disposable test box: it holds no real data and no real accounts,
snapshots are free, and the FnB owns the physical host. So install software,
change settings, schedule tasks, snapshot, bake and reset are all yours
(decision #372). Snapshot before a risky change and reset to it when the change
goes wrong. `./sc vm snapshot create <name>` is allowed while the domain is
running, but many hosts cannot checkpoint a live guest (a host-passthrough CPU
with non-migratable flags, UEFI pflash) and answer `snapshot_live_unsupported`.
That is the normal case, not a fault; take the snapshot offline instead:

1. Finish or save any in-guest work — an offline snapshot does not keep memory.
2. `./sc vm mcp down` if the tunnel is up, then `./sc vm stop`.
3. `./sc vm snapshot create <name>` (now offline), then `./sc vm start`.
4. Later, `./sc vm reset <name> --running` returns to that state booted, or
   `--off` leaves it powered down.

Redefine the baseline with `./sc vm bake [<name>]` rather than trying to
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
separately. A guest path for `push` and `pull` may be written with either
separator — `C:\SubfloorTest\out.bin` or `C:/SubfloorTest/out.bin` — because
the engine normalises it for scp's SFTP mode; `exec` command text is yours and
is passed through untouched. `push` and `pull` will not read from or write into
`.sc-state/local/vm/`, and will not write into `.super-coder/` or `.git/`. Guest console output is decoded lossily; base64-encode guest-side
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
./sc vm mcp up          # SSH-forward the guest's MCP port to run/vm-mcp.sock, start the relay, probe the endpoint
./sc vm mcp down        # stop relay and tunnel; reports each separately
```

Supported harnesses receive a managed `windows-mcp` adapter definition;
`mcp up` is what brings that endpoint online (in a sandbox it also runs the
in-container relay on 127.0.0.1:18000). Do not add an MCP server by hand or
edit harness configuration. Drive the GUI through the MCP tools by element,
verify each step with a capture, and `mcp down` before `reset`.

Provisioning installs the guest side as the per-user login task
`windows-mcp-server`, listening on the guest's loopback only; the tunnel is the
only route in. Because it is a LOGIN task, a guest with no interactive desktop
session has it installed and correct and never started — adoption reports that
as a warning and finishes, so a freshly adopted guest with no listener is
expected, not broken. `./sc vm mcp up` is what reports readiness. When it finds
no listener, log the adopting account in on the console, or re-run the install
yourself over exec:

```bash
./sc vm exec -- windows-mcp install --transport streamable-http --host 127.0.0.1 --port <mcp_port>
```

That is a legitimate repair under Authority above, not a workaround. Report what
you did.

## Session end

Finish as `remote_seats` says: `./sc vm mcp down` if you brought it up, then
`./sc vm reset --off`, and report test results separately from the reset
result. A `reset_result_unknown` answer is reported as-is; run `./sc vm status`
and stop rather than retrying the reset.
