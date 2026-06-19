---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# configure_winbox

Provision the Windows Test VM — winget-install the fork's toolchain from a committed manifest, verify each tool, then bake the clean snapshot every test reverts to. Admin-only; runs before the snapshot, re-runs on any toolchain change. Use when standing up or updating a fork's Windows build/test box.

**Category:** substrate

---

# configure_winbox — provisioning the Windows Test VM

The admin half of the Windows Test VM capability. You install the build
toolchain into the operator's Windows VM and bake the **clean snapshot** that
every `windows_devkit` run reverts to. Sibling to `self_update` /
`migration_management` — infrastructure work only the admin shell does. Grant is
explicit, per-fork (`common=0`).

## Scope boundary — what you do NOT do

You do **not** create the VM, install the guest OS, or enable OpenSSH inside it.
That bootstrap is the operator's, host-side, once (the engine can't reach inside
a fresh OS install). You assume a reachable guest with key auth already working,
and you provision the *toolchain* on top of it.

## Order is the design — provision BEFORE the snapshot

```
operator: OS + OpenSSH + key   →   YOU: winget toolchain + verify   →   snapshot = clean   →   devs run loop
```

The clean snapshot is **pristine OS + toolchain**. Every test reverts to it, so
the toolchain must be baked in. Provision *after* snapshotting and the first
test hits an empty box. Bump the toolchain → re-run this skill → **re-snapshot**.

## Procedure

1. **Read the link.** `.super-coder/instance.json` `vm` block gives the domain +
   SSH coordinates. Confirm SSH works: `ssh -i <ssh_key_path> -p <ssh_port>
   <ssh_user>@<ssh_host> "echo ok"`.

2. **Install the toolchain from the fork's committed manifest.** The fork commits
   a `winget export` at a known path (e.g. `winget-manifest.json`); you supply
   the *mechanism*, the fork supplies the *package list* — so this skill stays
   generic across forks. Over SSH:
   `winget import --import-file <manifest> --accept-package-agreements --accept-source-agreements`
   (for dos-arch: WiX, .NET SDK, MSBuild.)

3. **Verify each tool** over SSH — `dotnet --version`, `where.exe wix`, `msbuild
   -version`. The `windows_devkit` wizard's `toolchain` check (`dotnet
   --version`) is the same probe; it must go green after this step.

4. **Bake the clean snapshot.** `virsh snapshot-create-as <domain> <snapshot>
   --description "pristine OS + toolchain"`. Use the `snapshot` name from the
   `vm` block (default `clean`). If re-provisioning, delete the old snapshot
   first (`virsh snapshot-delete <domain> <snapshot>`) so the name is reused.

## Stance

- **Toolchain-as-code.** Install from the committed manifest, never ad hoc —
  the package set is reproducible and reviewable, and re-provisioning is a
  re-import, not a memory of what you clicked.
- **Re-provision means re-snapshot.** A toolchain bump that isn't followed by a
  fresh snapshot is invisible — every test still reverts to the old box.
- **Verify before you snapshot.** A snapshot of a half-installed box is a clean
  snapshot of a broken kit. Green checks first, snapshot second.
