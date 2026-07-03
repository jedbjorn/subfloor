---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# windows_vm_gui

Drive the Windows Test VM's GUI — Windows-MCP in the guest, UIA-tree clicking by element ID (never blind coordinates), screenshot verification between actions, and mouse-free in-process test paths for anything repeatable. Exploratory GUI QAQC where windows_devkit's exec loop can't see the UI.

**Category:** substrate

---

# windows_vm_gui — GUI-driving the Windows Test VM

Clicking by screenshot coordinates is the failure mode this skill exists to
prevent: pixel targets break on DPI scaling, window position, theme, and dense
UIs (a CAD ribbon, a settings tree). The reliable approach is **Windows UI
Automation (UIA)** — pull the accessibility tree and act on elements by ID,
the way a screen reader would. Deterministic across all of the above.

Tooling: **Windows-MCP** (`windows-mcp` on PyPI) running *inside the guest*,
reached over HTTP. It exposes `Snapshot` (UIA tree with element IDs),
`Click`/`Type`/`Scroll` (act on those IDs), `Screenshot` (visual verify), plus
`App`, `PowerShell`, `Clipboard`. Expect 0.2–0.5 s per action.

## Where this sits

| Skill | Loop | Use for |
|---|---|---|
| `windows_devkit` | push → exec → capture → reset | installers, services, anything scriptable |
| **this** | connect → Snapshot → click/type → verify | exploratory GUI QAQC, visual verification |
| `configure_winbox` | provision → verify → bake | the admin prep both of the above assume |

GUI driving is for **exploratory QAQC and visual verification** only. If a
check will run more than twice, it does not belong in a click sequence — see
the last section.

## One-time guest prep (admin — through the configure_winbox flow)

Every `windows_devkit` run reverts the VM to the `clean` snapshot, so anything
installed but not baked evaporates on the next reset. Windows-MCP is therefore
**toolchain**, and it follows the toolchain rule: a missing piece is a
manifest PR + re-bake, never an ad-hoc install from a test loop.

1. Add Python 3.13+ to the fork's committed winget manifest (e.g.
   `Python.Python.3.13`); `configure_winbox` pushes + imports it.
2. Via the broker (`/exec`, like every `configure_winbox` step):
   `pip install uv`, sanity-check `uvx windows-mcp serve --help`, then
   register the auto-start scheduled task, **bound to localhost only** — the
   guest must never expose it on the VM network:

   ```
   windows-mcp install --transport streamable-http --host 127.0.0.1 --port 8000
   ```

3. Re-bake: `./sc vm-bake` (host-side, operator-run — the snapshot is the
   trust anchor). From then on every reset comes back with the server already
   listening.

Constraints: Python 3.13+ and `uv` in the guest; English-language Windows
preferred (App-tool limitation); UAC prompts and elevated windows are
unreachable unless the server itself runs elevated.

## Per-session connect — seat-dependent

**Host-run seat** (a shell booted with `./sc boot` on the host, no sandbox):

1. Start from a clean box (`windows_devkit`'s `/reset`); wait until SSH
   answers.
2. Tunnel: `ssh -f -N -L 18000:127.0.0.1:8000 <ssh_user>@<ssh_host>` (values
   from the `vm` block in `.super-coder/instance.json`).
3. Verify the endpoint answers: `curl -s http://127.0.0.1:18000/mcp`.
4. Connect the harness:
   `claude mcp add --transport http windows-mcp http://127.0.0.1:18000/mcp`
5. Endpoint dead → check the tunnel first, then the guest task
   (`schtasks /run /tn windows-mcp-server` over SSH) — and if the task is
   missing, the snapshot was baked without the prep above.

**Sandboxed seat** (the engine default): the sandbox has no `ssh`, no key,
and no route to the VM — that is the broker design — so the connection is
brokered in two halves. The vm-broker (host-side, holds the key) ssh-forwards
a unix socket in the bind-mounted `run/` dir to the guest's Windows-MCP; a
tiny in-sandbox relay gives that socket the TCP URL `claude mcp add` needs:

1. Start from a clean box (`windows_devkit`'s `/reset`); wait until SSH
   answers.
2. Open the broker tunnel:
   `curl --unix-socket $(./sc vm-broker-sock) -X POST http://vm/mcp/up`
   (idempotent; forwards `run/vm-mcp.sock` to the guest's `mcp_port`,
   default 8000).
3. Start the in-sandbox relay: `./sc vm-mcp-relay up` (listens on
   `127.0.0.1:18000`, pipes to the tunnel socket; also idempotent).
4. Verify the endpoint answers: `curl -s http://127.0.0.1:18000/mcp`.
5. Connect the harness:
   `claude mcp add --transport http windows-mcp http://127.0.0.1:18000/mcp`
6. Endpoint dead → `./sc vm-mcp-relay status` first (`upstream: false` means
   the broker tunnel is down — redo step 2; a reset drops it, so reconnect
   after every `/reset`), then the guest task
   (`schtasks /run /tn windows-mcp-server` via broker `/exec`) — and if the
   task is missing, the snapshot was baked without the prep above.
7. Done driving: `./sc vm-mcp-relay down` and
   `curl --unix-socket $(./sc vm-broker-sock) -X POST http://vm/mcp/down`.

No key, no ssh, no network surface enters the sandbox — both hops are unix
sockets in the bind mount, same posture as every other broker verb. Do
**not** fake GUI driving by guessing pixel coordinates off `/capture`
screenshots — that is the exact failure mode this skill bans.

## Driving rules

- **Snapshot first, always.** Act on element IDs. Never guess coordinates
  from a screenshot when an element ID exists.
- Standard chrome (ribbons, dialogs, palettes — WPF/WinForms/WinUI) is
  UIA-visible → click by element.
- Custom-rendered surfaces (drawing canvases, game views, embedded GL) have
  no UIA elements — the one legitimate coordinate fallback: `Screenshot` →
  pick the pixel target → `Click(x, y)` → `Screenshot` again to verify. Never
  chain canvas clicks without verifying between them.
- Re-`Snapshot` after anything that changes the window set — stale element
  IDs misclick.
- Verify state visually after each meaningful action.
- Batch reads; don't spam single-element queries.

## Prefer no mouse at all

Anything repeatable belongs in an in-process test path driven over the exec
loop, not in a click sequence. Most GUI platforms have one — for Revit
add-ins: RevitTestFramework, ricaun.RevitTest, Revit.TestRunner (NUnit
in-process via journal, no UI at all). The hierarchy, in order:

```
in-process test framework  →  UIA by element ID  →  coordinates (only where the tree is blind)
```

If a check will run more than twice, it goes to the top of that list.
