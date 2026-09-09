---
name: windows_testing
description: Windows-only add-on to remote_seats — PowerShell exec conventions, capture, the managed Windows-MCP GUI transport, and guest prerequisites for a posture 1 Windows VM. Opt-in; load with remote_seats.
category: substrate
command: sc vm
common: false
---

# windows_testing — the Windows add-on

`remote_seats` owns setup, the session shape, sharing, snapshots, and errors.
This skill adds only what a Windows guest changes. Linux guests never need it.

## Guest prerequisites (the FnB installs; the engine does not)

- Windows OpenSSH server with `ssh_user` in local Administrators; `ssh_key_path`
  on the host is that account's key. The default SSH shell is `cmd.exe`.
- PowerShell available on `PATH`. A stock `Restricted` execution policy
  rejects `-File` scripts; a disposable test image may set
  `Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy Bypass -Force` and
  verify with `Get-ExecutionPolicy -List`. Apply that only to a throwaway guest
  holding no data, credentials, or logged-in sessions; a snapshot reset removes
  later changes, not a secret baked into the baseline.
- A writable workspace such as `C:\SubfloorTest`, the toolchain under test, and
  the guest-side mount of the transfer share `./sc vm push` stages into.
- For GUI work, Windows-MCP listening in the guest on the block's `mcp_port`
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
./sc vm mcp up          # SSH-forward the guest's MCP port to run/vm-mcp.sock, start the relay, probe the endpoint
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
and stop rather than retrying the reset.
