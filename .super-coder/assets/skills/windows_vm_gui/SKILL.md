---
name: windows_vm_gui
description: Drive the linked Windows Test VM through adapter-provided Windows MCP tools, using UI Automation element IDs, visual verification, managed MCP lifecycle, and one end-only powered-off reset.
category: substrate
common: false
---

# windows_vm_gui — drive the supplied Windows GUI

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
configuration. Report the adapter state from `./sc vm status --json`. State
`unknown` with no `SC_HARNESS` means this session predates the adapter identity
contract: relaunch through the engine so it can inject the active harness
identity. State `unsupported` means the active adapter declares no injection
mechanism; it is an honest capability stop, not a reason to fabricate GUI
access or relaunch repeatedly.

## Missing guest Windows-MCP

Windows-MCP is baked guest toolchain, never an ad-hoc test dependency. If it is
missing, the operator must use the `configure_winbox` flow to:

1. Add Python 3.13+ (for example, `Python.Python.3.13`) to the fork's committed
   winget manifest and import it into the guest.
2. Run `pip install uv`, verify `uvx windows-mcp serve --help` exits zero, and
   register the auto-start task with:

   ```text
   windows-mcp install --transport streamable-http --host 127.0.0.1 --port 8000
   ```

   The server must be bound to localhost ONLY (never expose it on the VM network).
3. Run `./sc vm-bake` so resets restore the prepared server.

The guest requires Python 3.13+ and `uv`. Prefer English-language Windows due
to the App-tool limitation. UAC prompts and elevated windows are inaccessible
unless the server itself runs elevated.

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
end-only cleanup contract.
