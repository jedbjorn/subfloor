---
name: windows_devkit
description: Drive the linked Windows Test VM from its supplied state with typed status, start, push, exec, capture, and end-only reset commands. Use for Windows installer, service, registry, and system-level verification that Wine cannot represent.
category: substrate
common: false
---

# windows_devkit — drive the supplied Windows test VM

Use the operator-supplied VM and application state. Inspect first, start or open
only what is absent, perform the test, and reset once at the end. Planning,
probing, skill review, and static verification never reset the VM.

## Preflight

The linked fork must have a `vm` block in `.super-coder/instance.json`, created
and validated through Scripts → **Windows Test VM**. The operator owns the VM,
testing snapshot, credentials, and guest toolchain.

- No `vm` block: stop and ask the operator to link the VM.
- Invalid configuration or missing broker: report the structured `./sc vm`
  error and ask the operator to run `./sc vm-broker-up`. Do not read key
  material, use `ssh` or `virsh` directly, or build raw broker requests.
- Missing guest toolchain: ask the operator to run `configure_winbox` and
  re-bake. Never install tools during the test and poison the testing snapshot.
- If GUI work reports adapter state `unknown` because `SC_HARNESS` is absent,
  the session predates the adapter identity contract. Relaunch the shell through
  the engine; do not add persistent harness configuration. A declared
  `unsupported` adapter is a capability stop, not a relaunch prompt.

## Canonical workflow

1. Assume the operator supplied a running VM with the testing application open.
2. Run `./sc vm status --json`. This is read-only: it never starts, restarts, or
   resets the VM.
3. If the domain is off, run `./sc vm start --json`. If it is already running
   but SSH is not ready, the same command waits for readiness without restarting
   it. Do not invent sleeps.
4. If the testing application is absent, open it through `./sc vm exec` or the
   Windows GUI tools, according to the test.
5. Use `push`, `exec`, and `capture` as needed, then perform the test.
6. A test failure does not skip cleanup. When testing is finished and you still
   have control, run `./sc vm mcp down --json` if GUI transport was used, then
   run `./sc vm reset --off --json` once. This restores the configured testing
   snapshot and leaves the domain powered off.
7. Report the test result and cleanup result separately. Include any structured
   error and never claim an unconfirmed operation succeeded.

There is no reset at the beginning or during a test. Do not automatically retry
a reset after a timeout, disconnect, malformed response, or
`reset_result_unknown`; its effect may already have occurred.

## Typed commands

```text
./sc vm status --json
./sc vm start --json
./sc vm push <repo-file> [destination] --json
./sc vm exec --json -- <simple guest command and arguments>
./sc vm exec --command-file <utf8-command-file> --json
./sc vm capture [--output .sc-state/local/vm-captures/<name>] --json
./sc vm mcp status|up|down --json
./sc vm reset --off --json
```

- `exec` accepts arguments after `--` or one UTF-8 command file, never both.
  Arguments after `--` are re-joined with single spaces; local shell token
  boundaries are not preserved. Use that form for simple commands, or pass the
  entire guest command as one locally quoted argument. For complex, quoted, or
  multiline PowerShell, use `--command-file` so quotes, dollar variables,
  pipes, backticks, paths with spaces, and Unicode reach the broker unchanged.
- `cmd.exe` is the guest default SSH shell. Invoke PowerShell syntax explicitly,
  for example with
  `powershell -NoProfile -Command "Get-ChildItem Env:"`.
- Client-to-broker command content remains unchanged, but guest console stdout
  may transliterate non-ASCII on return. For byte-exact output, base64-encode
  it guest-side and decode it locally.
- `capture` writes an atomic mode-0600 artifact under
  `.sc-state/local/vm-captures/`; use the returned path for visual inspection.
- `mcp up` verifies the tunnel, relay, and HTTP endpoint before success.
  `mcp down` reports relay and tunnel cleanup separately.
- Every command exits nonzero on an operation failure. With `--json`, inspect
  the single object containing `schema_version`, `ok`, `operation`, `result`,
  and `error`.

## Stance

- Observe before changing state. Retain a supplied running domain and open app.
- Drive through `./sc vm`; never assemble broker HTTP, socket curl, JSON, SSH,
  PowerShell transport quoting, screenshot decoding, or relay process control.
- Reset is end-only cleanup, not test setup. Attempt it once while you still
  have control, and report its observed final state honestly.
- Guest output, screenshots, configuration, and credentials remain local to the
  linked repo and operator.
