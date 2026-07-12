---
name: windows_vm_gui
description: Drive the Windows Test VM's GUI — Windows-MCP in the guest, UIA-tree clicking by element ID (never blind coordinates), screenshot verification between actions, and mouse-free in-process test paths for anything repeatable. Exploratory GUI QAQC where windows_devkit's exec loop can't see the UI.
category: substrate
common: false
---

# windows_vm_gui — GUI-driving the Windows Test VM

Drive the guest GUI via **Windows UI Automation**: pull the UIA tree, act on
elements by ID. NEVER click by screenshot pixel coordinates when an element ID
exists — pixel targets break on DPI scaling / window position / theme / dense
UIs (a CAD ribbon, a settings tree).

Tooling = **Windows-MCP** (`windows-mcp` on PyPI), running inside the guest,
reached over HTTP. Tools: `Snapshot` (UIA tree + element IDs),
`Click`/`Type`/`Scroll` (act on IDs), `Screenshot` (visual verify), plus
`App`, `PowerShell`, `Clipboard`. Expect 0.2–0.5 s per action.

## Where this sits

| Skill | Loop | Use for |
|---|---|---|
| `windows_devkit` | push → exec → capture → reset | installers, services, anything scriptable |
| **this** | connect → Snapshot → click/type → verify | exploratory GUI QAQC, visual verification |
| `configure_winbox` | provision → verify → bake | the admin prep both of the above assume |

GUI driving = exploratory QAQC + visual verification ONLY. A check that will
run more than twice does not belong in a click sequence — see the last section.

## One-time guest prep (admin — via the configure_winbox flow)

Every `windows_devkit` run reverts to the `clean` snapshot → anything installed
but not baked evaporates on the next reset. Windows-MCP is therefore
**toolchain**: a missing piece = manifest PR + re-bake, NEVER an ad-hoc install
from a test loop.

1. Add Python 3.13+ (e.g. `Python.Python.3.13`) to the fork's committed winget
   manifest → `configure_winbox` pushes + imports it.
2. Via the broker (`/exec`, like every `configure_winbox` step):
   `pip install uv` → `uvx windows-mcp serve --help` exits 0 → register the
   auto-start scheduled task, bound to localhost ONLY (never expose it on the
   VM network):

   ```
   windows-mcp install --transport streamable-http --host 127.0.0.1 --port 8000
   ```

3. Operator runs `./sc vm-bake` (host-side — the snapshot is the trust
   anchor) → every subsequent reset boots with the server already listening.

Constraints: Python 3.13+ and `uv` in the guest; English-language Windows
preferred (App-tool limitation); UAC prompts + elevated windows unreachable
unless the server itself runs elevated.

## Per-session connect — seat-dependent

**Host-run seat** (a shell booted with `./sc boot` on the host, no sandbox):

1. `windows_devkit` `/reset` → wait until SSH answers.
2. Tunnel: `ssh -f -N -L 18000:127.0.0.1:8000 <ssh_user>@<ssh_host>` (values
   from the `vm` block in `.super-coder/instance.json`).
3. `curl -s http://127.0.0.1:18000/mcp` answers → endpoint live.
4. Connect the harness:
   `claude mcp add --transport http windows-mcp http://127.0.0.1:18000/mcp`
5. Endpoint dead → check the tunnel first, then the guest task
   (`schtasks /run /tn windows-mcp-server` over SSH); task missing = snapshot
   was baked without the prep above.

**Sandboxed seat** (the engine default): the sandbox has no `ssh`, no key, no
route to the VM — broker design — so the connection is brokered in two halves:
the vm-broker (host-side, holds the key) ssh-forwards a unix socket in the
bind-mounted `run/` dir to the guest's Windows-MCP; an in-sandbox relay gives
that socket the TCP URL `claude mcp add` needs.

1. `windows_devkit` `/reset` → wait until SSH answers.
2. Broker tunnel:
   `curl --unix-socket $(./sc vm-broker-sock) -X POST http://vm/mcp/up`
   (idempotent; forwards `run/vm-mcp.sock` to the guest's `mcp_port`,
   default 8000).
3. Relay: `./sc vm-mcp-relay up` (listens on `127.0.0.1:18000`, pipes to the
   tunnel socket; idempotent).
4. `curl -s http://127.0.0.1:18000/mcp` answers → endpoint live.
5. Connect the harness:
   `claude mcp add --transport http windows-mcp http://127.0.0.1:18000/mcp`
6. Endpoint dead → `./sc vm-mcp-relay status` first: `upstream: false` =
   broker tunnel down → redo step 2 (every `/reset` drops the tunnel —
   reconnect after each). Still dead → guest task via broker `/exec`:
   `schtasks /run /tn windows-mcp-server`; task missing = snapshot was baked
   without the prep above.
7. Done driving: `./sc vm-mcp-relay down` +
   `curl --unix-socket $(./sc vm-broker-sock) -X POST http://vm/mcp/down`.

NEVER fake GUI driving by guessing pixel coordinates off `/capture`
screenshots.

## Driving rules

- `Snapshot` first, always → act on element IDs.
- Standard chrome (ribbons, dialogs, palettes — WPF/WinForms/WinUI) is
  UIA-visible → click by element.
- Custom-rendered surfaces (drawing canvases, game views, embedded GL) have no
  UIA elements — the ONE legitimate coordinate fallback: `Screenshot` → pick
  the pixel target → `Click(x, y)` → `Screenshot` again to verify. NEVER chain
  canvas clicks without verifying between them.
- Re-`Snapshot` after anything that changes the window set — stale element IDs
  misclick.
- Verify state visually after each meaningful action.
- Batch reads; don't spam single-element queries.

## Prefer no mouse at all

Anything repeatable belongs in an in-process test path driven over the exec
loop, not a click sequence. Most GUI platforms have one — Revit add-ins:
RevitTestFramework, ricaun.RevitTest, Revit.TestRunner (NUnit in-process via
journal, no UI). Hierarchy, in order:

```
in-process test framework  →  UIA by element ID  →  coordinates (only where the tree is blind)
```

A check that will run more than twice goes to the top of that list.
