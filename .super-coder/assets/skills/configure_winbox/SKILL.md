---
name: configure_winbox
description: Provision the Windows Test VM — push the fork's committed winget manifest to the guest via the vm-broker, install + verify what the MANIFEST says, then hand the operator the one-command snapshot bake (./sc vm-bake). Admin-only; runs before the snapshot, re-runs on any toolchain change.
category: substrate
common: false
---

# configure_winbox — provisioning the Windows Test VM

Admin half of the Windows Test VM capability: install the build toolchain into
the operator's Windows VM, then get the **clean snapshot** — the one every
`windows_devkit` run reverts to — re-baked on top of it. Sibling to
`self_update` / `migration_management`: infrastructure work only the admin
shell does. Grant is explicit, per-fork (`common=0`).

## Scope boundary

You do NOT create the VM, install the guest OS, or enable OpenSSH inside it —
that bootstrap is the operator's, host-side, once. Assume a reachable guest
with key auth already working; provision the *toolchain* on top of it.

## Execution plane = the broker — no ssh, no virsh

The sandbox holds no SSH key, no `virsh`, no route to the VM. Every guest
operation goes through the host-side **vm-broker** over its unix socket,
exactly like `windows_devkit`. NEVER fall back to raw `ssh`/`virsh`.

```bash
SOCK="$(sc vm-broker-sock)"
curl -s --unix-socket "$SOCK" http://vm/health                        # broker up?
curl -s --unix-socket "$SOCK" http://vm/exec -d '{"command":"ver"}'   # run in guest
curl -s --unix-socket "$SOCK" http://vm/push -d '{"src":"winget-manifest.json"}'
```

`/health` fails → broker down → ask the operator to run `./sc launch`
(auto-starts the broker when a VM is linked) or `./sc vm-broker-up`.

## Order is the design — provision BEFORE the snapshot

```
operator: OS + OpenSSH + key   →   YOU: manifest toolchain + verify (broker)   →   operator: ./sc vm-bake   →   devs run loop
```

Clean snapshot = pristine OS + toolchain; every test reverts to it. Provision
*after* snapshotting → the first test hits an empty box. Toolchain bump →
re-run this skill → re-bake — an unbaked bump is invisible, every test still
reverts to the old box.

## Procedure

1. **Confirm the link + the plane.** `.super-coder/instance.json` `vm` block
   names the domain, snapshot, transfer dir → `/health` returns ok →
   `exec {"command":"ver"}` returns a Windows version string = broker → guest
   proven end to end.

2. **Push the fork's committed manifest into the guest.** The fork commits a
   `winget export` (e.g. `winget-manifest.json` at the repo root) — you supply
   the mechanism, the fork supplies the package list. `push` stages it into
   the transfer share the guest has mounted (a drive letter, e.g. `Z:`); then
   install over `exec`:

   ```
   winget import --import-file Z:\winget-manifest.json --accept-package-agreements --accept-source-agreements
   ```

3. **Verify what the MANIFEST installs — not a remembered tool list.** Read
   the committed manifest; probe each package it declares over `exec`
   (`git --version`, `dotnet --version`, `pwsh -v`, `where.exe wix` —
   whichever the manifest carries), each returning success. NEVER probe a
   tool the manifest doesn't install (fails a faithful import) and NEVER
   install a tool the manifest doesn't declare (breaks toolchain-as-code) —
   a tool the fork needs but the manifest lacks = manifest PR, never an
   ad-hoc install. The `windows_devkit` wizard's `toolchain` probe must also
   pass — check what it probes for this fork.

4. **Hand the bake to the operator.** Redefining the snapshot is
   host-authority only, deliberately not a broker verb (a sandbox that could
   re-bake could persist tampering across every reset). All probes green →
   ask the operator to run:

   ```bash
   ./sc vm-bake     # graceful shutdown → delete old snapshot → re-bake OFFLINE
   ```

   One command, idempotent, leaves the guest powered off. NEVER hand off a
   bake before the probes are green — a snapshot of a half-installed box is a
   clean snapshot of a broken kit. Confirm afterwards with the wizard's
   `snapshot` check or a `windows_devkit` reset round-trip.
