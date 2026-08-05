-- 0180 — reseed the canonical supplied-state Windows VM skills.
-- Full-body UPSERTs converge existing forks without changing their grants.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'windows_devkit',
  'Drive the linked Windows Test VM from its supplied state with typed status, start, push, exec, capture, and end-only reset commands. Use for Windows installer, service, registry, and system-level verification that Wine cannot represent.',
  'substrate',
  NULL,
  0,
  '# windows_devkit — drive the supplied Windows test VM

Use the operator-supplied VM and application state. Inspect first, start or open
only what is absent, perform the test, and reset once at the end. Planning,
probing, skill review, and static verification never reset the VM.

## Preflight

The linked fork must have a `vm` block in `.super-coder/instance.json`, created
and validated through Scripts → **Windows Test VM**. The operator owns the VM,
testing snapshot, credentials, and guest toolchain.

- No `vm` block: stop and ask the operator to link the VM.
- Invalid configuration or missing broker: report the structured `./sc vm`
  error. Do not read key material, use `ssh` or `virsh` directly, or build raw
  broker requests.
- Missing guest toolchain: ask the operator to run `configure_winbox` and
  re-bake. Never install tools during the test and poison the testing snapshot.

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
   have control, stop GUI transport if it was used, then run
   `./sc vm reset --off --json` once. This restores the configured testing
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
./sc vm exec --json -- <guest command and arguments>
./sc vm exec --command-file <utf8-file> --json
./sc vm capture [--output .sc-state/local/vm-captures/<name>] --json
./sc vm mcp status|up|down --json
./sc vm reset --off --json
```

- `exec` accepts arguments after `--` or one UTF-8 command file, never both.
  Preserve PowerShell quotes, dollar variables, pipes, backticks, paths,
  multiline input, and Unicode instead of adding JSON or shell escape layers.
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
  linked repo and operator.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'windows_vm_gui',
  'Drive the linked Windows Test VM through adapter-provided Windows MCP tools, using UI Automation element IDs, visual verification, managed MCP lifecycle, and one end-only powered-off reset.',
  'substrate',
  NULL,
  0,
  '# windows_vm_gui — drive the supplied Windows GUI

Use this for exploratory GUI QA/QC and visual verification that cannot be
expressed through `windows_devkit` commands alone. The operator supplies the VM
and application state. Observe first, start or open only what is absent, and
reset once when all testing is finished.

## Preflight and tool availability

The harness adapter declares the managed streamable-HTTP `windows-mcp` server
before the harness launches. Claude, Codex, and OpenCode expose it where their
active adapter supports Windows MCP. Kimi and Vibe are unsupported until their
adapters gain an equivalent injection mechanism.

The harness tool list may be fixed at launch. If Windows MCP tools are absent,
do not run persistent registration commands or edit user/project harness
configuration. Report the adapter state from `./sc vm status --json`; an
unsupported adapter is an honest stop, not a reason to fabricate GUI access.

Windows-MCP runs inside the prepared guest. A missing guest server or toolchain
requires the operator''s `configure_winbox` and re-bake flow. Never install it
ad hoc during testing.

## Canonical workflow

1. Assume the operator supplied a running VM with the testing application open.
2. Run `./sc vm status --json`. It is read-only and must not reset, restart, or
   otherwise mutate the VM.
3. If the VM is off, run `./sc vm start --json`. For a running VM whose SSH is
   not ready, the same command performs bounded readiness checks without a
   restart. Do not invent sleeps.
4. If the application is absent, open it through `./sc vm exec` or the
   harness-provided Windows MCP tools.
5. Run `./sc vm mcp up --json`. Success means the broker tunnel, verified local
   relay, and MCP HTTP endpoint are ready. Then use the already-provided
   Windows MCP tools; do not register them from inside the skill.
6. Perform the GUI test. A test failure does not skip cleanup.
7. When all testing is finished and you still have control, run
   `./sc vm mcp down --json`, then `./sc vm reset --off --json` once. Report MCP
   cleanup and reset results separately from the test result.

There is no opening or mid-test reset. If MCP setup or teardown returns a
structured failure, include it for the operator and do not claim success. Never
automatically repeat an uncertain reset.

## Driving rules

- Call `Snapshot` first. Act on UI Automation element IDs, not screenshot
  coordinates.
- Re-run `Snapshot` after a window-set change; stale element IDs can misclick.
- Use `Click`, `Type`, and `Scroll` on element IDs, and verify meaningful state
  changes with `Screenshot`.
- Standard WPF, WinForms, and WinUI chrome is normally UIA-visible. A
  custom-rendered canvas with no UIA element is the only coordinate fallback:
  take a screenshot, perform one coordinate action, then take another
  screenshot before continuing.
- Batch reads instead of issuing repeated single-element queries.
- Keep application state and screenshots local; never expose the relay beyond
  its configured loopback endpoint.

## Prefer a scripted path when repeatable

Anything likely to run more than twice belongs in an in-process framework or a
typed `./sc vm exec` test, not a click sequence:

```text
in-process test framework  →  UIA by element ID  →  coordinates only when UIA is blind
```

Use `./sc vm capture --json` when the result needs a durable local screenshot
artifact. Use `windows_devkit` for push, exact guest commands, and the shared
end-only cleanup contract.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

COMMIT;
