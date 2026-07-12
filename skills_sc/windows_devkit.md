---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# windows_devkit

Drive the linked Windows Test VM — push a build artifact, exec the installer/test over SSH, capture output + a screenshot, then reset to the clean snapshot. High-fidelity installer/system-level testing where Wine is useless. Use when building or verifying Windows software in a fork that has a configured VM.

**Category:** substrate

---

# windows_devkit — driving the Windows Test VM

Real Windows, for the testing Wine can't fake: MSI installers, services, the
registry, system-level behavior. Opt-in + link-only — the operator runs the
VM; you drive a verified loop against it. Devs build + test; the reviewer
independently verifies the dev's candidate artifact with exec → capture →
reset. Grant is explicit, per-fork (`common=0`).

## Precondition — the link is configured

VM config = `vm` key in `.super-coder/instance.json` (set via the GUI Scripts
→ **Windows Test VM** wizard, which live-tests every field before save):

```json
"vm": { "domain": "win-test", "ssh_host": "127.0.0.1", "ssh_port": 22,
        "ssh_user": "tester", "ssh_key_path": "~/.ssh/sc_win_test",
        "transfer_dir": "/var/sc/win-xfer", "snapshot": "clean",
        "libvirt_uri": "qemu:///system" }
```

`libvirt_uri` optional — set it when the domain is system-scope (the default
`qemu:///session` can't see it); omit otherwise.

- No `vm` block → no VM linked: stop + ask the operator to run the wizard.
- `configure_winbox` must also have run, or the box has no toolchain — the
  wizard's `toolchain` check confirms it did.
- A wrong field → fix it in the wizard (it re-validates); NEVER hand-edit
  secrets into config.
- `ssh_key_path` = a path, never key material. Never read it — the key lives
  host-side with the broker (below).

## Drive through the host broker — never ssh/virsh directly

You run inside the sandbox; the VM lives on the host's libvirt NAT,
unreachable from here, and the container has no `ssh`, no `virsh`, no key.
Call the host-side **vm-broker** over its unix socket in the bind-mounted
repo; the broker holds the key + libvirt. (Detail:
`.super-coder/docs/windows-vm-broker.md`.)

```bash
SOCK="$(sc vm-broker-sock)"
curl -s --unix-socket "$SOCK" http://vm/health      # liveness check first
```

curl fails "not reachable" → broker down → ask the operator to run
`sc vm-broker-up` on the host. You cannot start it yourself (host process,
not sandbox).

## The loop — push → exec → capture → reset

Every run starts from the clean snapshot — without the reset, installer
side-effects leak between runs and the next result is a lie.

| Verb | Call |
|---|---|
| **push** | `curl -s --unix-socket "$SOCK" http://vm/push -d '{"src":"<repo path to artifact>"}'` — stages into `transfer_dir` (the guest's share) |
| **exec** | `curl -s --unix-socket "$SOCK" http://vm/exec -d '{"command":"<installer / test cmd>"}'` → `{ok, exit, stdout, stderr}` |
| **capture** | `curl -s --unix-socket "$SOCK" http://vm/capture -d '{"command":"<optional cmd>"}'` → stdout + a base64 `virsh screenshot` for GUI state |
| **reset** (start) | `curl -s --unix-socket "$SOCK" http://vm/reset -X POST` — revert to clean + **boot** |
| **reset** (done) | `curl -s --unix-socket "$SOCK" http://vm/reset -d '{"running":false}'` — revert to clean, leave **powered OFF** |

The broker runs ssh non-interactively from the saved `vm` block — name a
command, never a host or a key.

**Bracket every run with reset.** Start with `/reset` (boots a clean box);
end — even on failure — with `/reset {"running":false}` (clean + powered off:
an idle running VM pins ~12 GB of host RAM). The clean snapshot is OFFLINE
(this CPU's non-migratable `invtsc` flag refuses a live snapshot), so a bare
revert lands powered-off; `{"running":true}` (the default) boots it —
`{"running":false}` gets clean **and** off in a single op.

## Stance

- **You drive, you don't provision.** Missing toolchain → admin's
  `configure_winbox` + re-bake. NEVER `winget install` from this loop — it
  poisons the clean snapshot.
- **The reviewer verifies, doesn't build.** Reviewer runs exec → capture →
  reset on the dev's candidate artifact to confirm the claim independently.
