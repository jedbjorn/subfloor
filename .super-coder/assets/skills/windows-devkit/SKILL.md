---
name: windows-devkit
description: Drive the linked Windows Test VM — push a build artifact, exec the installer/test over SSH, capture output + a screenshot, then reset to the clean snapshot. High-fidelity installer/system-level testing where Wine is useless. Use when building or verifying Windows software in a fork that has a configured VM.
category: substrate
common: false
---

# windows-devkit — driving the Windows Test VM

Real Windows, for the testing Wine can't fake: MSI installers, services, the
registry, system-level behavior. This is **opt-in and link-only** — the operator
runs the VM; you drive a verified loop against it. Devs build and test; the
reviewer independently verifies a candidate build with **exec → capture →
reset**. Grant is explicit, per-fork (`common=0`); you have it because someone
granted it to your shell.

## Precondition — the link is configured

The VM lives in `.super-coder/instance.json` under the `vm` key (set via the GUI
Scripts → **Windows Test VM** wizard, which live-tests every field before save):

```json
"vm": { "domain": "win-test", "ssh_host": "127.0.0.1", "ssh_port": 22,
        "ssh_user": "tester", "ssh_key_path": "~/.ssh/sc_win_test",
        "transfer_dir": "/var/sc/win-xfer", "snapshot": "clean" }
```

No `vm` block → no VM linked: stop and ask the operator to run the wizard. The
admin `configure_winbox` skill must also have run, or the box has no toolchain
(the `toolchain` check in the wizard is how you confirm it did).

`ssh_key_path` is a **path**, never key material — read it, never echo it.

## The loop — push → exec → capture → reset

Every run starts from the clean snapshot, so installer side-effects never leak
between runs. That property is the whole point — without the reset, system-level
testing isn't trustworthy.

| Verb | How |
|---|---|
| **push** | drop the build artifact into `transfer_dir` (the host side of the guest's virtio-fs share, or scp to the guest) |
| **exec** | `ssh -i <ssh_key_path> -p <ssh_port> <ssh_user>@<ssh_host> "<installer / test cmd>"` |
| **capture** | collect stdout + exit code; `virsh screenshot <domain> /tmp/win.ppm` for installer GUI state |
| **reset** | `virsh snapshot-revert <domain> <snapshot>` — back to clean before the next run |

SSH non-interactively: `-o BatchMode=yes -o ConnectTimeout=10`. A run that
exec'd anything stateful **must** end with a reset, even on failure — leave the
box clean for the next shell.

## Stance

- **Reset is not optional.** Test from clean, return to clean. A dirty box makes
  the next run's result a lie.
- **You drive, you don't provision.** Missing toolchain → that's the admin's
  `configure_winbox` + re-snapshot, not an install from here. Never `winget
  install` from this loop; it would poison the clean snapshot.
- **The reviewer verifies, doesn't build.** Reviewer uses exec → capture → reset
  on the dev's candidate artifact to confirm the claim independently.
- **Link-only stays link-only.** If a field is wrong, fix it in the wizard (it
  re-validates); don't hand-edit secrets into config.
