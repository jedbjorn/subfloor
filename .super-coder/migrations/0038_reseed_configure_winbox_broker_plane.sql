-- 0038 — reseed configure_winbox: broker execution plane + manifest-driven verify
--
-- dos-arch QAQC-03: the skill drove raw `ssh -i` / `virsh` — neither exists in
-- the sandbox where the admin shell runs (broker-only by design) — and its
-- verify step probed a remembered tool list (WiX/MSBuild) that contradicted the
-- fork's committed winget manifest (Git+DotNet.SDK+PowerShell), so Step 3 failed
-- after a faithful import. Rewritten: all guest ops via the vm-broker verbs
-- (push the manifest + exec winget/probes), verification derives from what the
-- MANIFEST declares, and the snapshot bake is handed to the operator as the new
-- one-command `./sc vm-bake` (host-authority — the sandbox must never redefine
-- the clean snapshot it reverts to).
--
-- 0001 is regenerated from the asset for fresh builds; this forward reseed
-- carries the same body to already-installed forks (UPSERT by name; skill_id +
-- grants preserved).

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'configure_winbox',
  'Provision the Windows Test VM — push the fork''s committed winget manifest to the guest via the vm-broker, install + verify what the MANIFEST says, then hand the operator the one-command snapshot bake (./sc vm-bake). Admin-only; runs before the snapshot, re-runs on any toolchain change.',
  'substrate',
  NULL,
  0,
  '# configure_winbox — provisioning the Windows Test VM

The admin half of the Windows Test VM capability. You install the build
toolchain into the operator''s Windows VM and get the **clean snapshot** — the
one every `windows_devkit` run reverts to — re-baked on top of it. Sibling to
`self_update` / `migration_management` — infrastructure work only the admin
shell does. Grant is explicit, per-fork (`common=0`).

## Scope boundary — what you do NOT do

You do **not** create the VM, install the guest OS, or enable OpenSSH inside it.
That bootstrap is the operator''s, host-side, once (the engine can''t reach inside
a fresh OS install). You assume a reachable guest with key auth already working,
and you provision the *toolchain* on top of it.

## Your execution plane is the broker — you have no ssh, no virsh

You run **inside the sandbox**, which holds no SSH key, no `virsh`, and no route
to the VM — by design (a compromised sandbox must not script the hypervisor or
read the credential). Every guest operation below goes through the **host-side
vm-broker** over its unix socket, exactly like `windows_devkit` does:

```bash
SOCK="$(sc vm-broker-sock)"
curl -s --unix-socket "$SOCK" http://vm/health                        # broker up?
curl -s --unix-socket "$SOCK" http://vm/exec -d ''{"command":"ver"}''   # run in guest
curl -s --unix-socket "$SOCK" http://vm/push -d ''{"src":"winget-manifest.json"}''
```

If `/health` fails, the broker isn''t up — ask the operator to `./sc launch` (it
auto-starts when a VM is linked) or `./sc vm-broker-up`. Never fall back to raw
`ssh`/`virsh`; you don''t have them, and you shouldn''t.

## Order is the design — provision BEFORE the snapshot

```
operator: OS + OpenSSH + key   →   YOU: manifest toolchain + verify (broker)   →   operator: ./sc vm-bake   →   devs run loop
```

The clean snapshot is **pristine OS + toolchain**. Every test reverts to it, so
the toolchain must be baked in. Provision *after* snapshotting and the first
test hits an empty box. Bump the toolchain → re-run this skill → **re-bake**.

## Procedure

1. **Confirm the link + the plane.** `.super-coder/instance.json` `vm` block
   names the domain, snapshot, and transfer dir. Then `/health`, then
   `exec {"command":"ver"}` — a green `ver` proves broker → guest end to end.

2. **Push the fork''s committed manifest into the guest.** The fork commits a
   `winget export` (e.g. `winget-manifest.json` at the repo root); you supply
   the *mechanism*, the fork supplies the *package list* — this skill stays
   generic across forks. `push` stages it into the transfer share the guest has
   mounted (a drive letter, e.g. `Z:`); then install over `exec`:

   ```
   winget import --import-file Z:\winget-manifest.json --accept-package-agreements --accept-source-agreements
   ```

3. **Verify what the MANIFEST installs — not a remembered tool list.** Read the
   committed manifest and probe each package it actually declares over `exec`
   (`git --version`, `dotnet --version`, `pwsh -v`, `where.exe wix` — whichever
   the manifest carries). Verifying tools the manifest doesn''t install fails a
   faithful import; installing tools the manifest doesn''t declare breaks
   toolchain-as-code. If the fork *needs* a tool the manifest lacks, the fix is
   a manifest PR, never an ad-hoc install. (The `windows_devkit` wizard''s
   `toolchain` probe must also hold — check what it probes for this fork.)

4. **Hand the bake to the operator.** The snapshot is the fork''s trust anchor —
   every test run reverts to it — so *redefining* it is host-authority only,
   deliberately not a broker verb (a sandbox that could re-bake could persist
   tampering across every reset). When your verifies are green, ask the
   operator to run:

   ```bash
   ./sc vm-bake     # graceful shutdown → delete old snapshot → re-bake OFFLINE
   ```

   One command, idempotent, leaves the guest powered off. Confirm afterwards
   with the wizard''s `snapshot` check (or a `windows_devkit` reset round-trip).

## Stance

- **Toolchain-as-code.** Install from the committed manifest, never ad hoc —
  the package set is reproducible and reviewable, and re-provisioning is a
  re-import, not a memory of what you clicked.
- **The manifest is the verify list.** Probes derive from what the manifest
  declares; a skill that checks tools the manifest never installed manufactures
  failures (and vice versa hides them).
- **Re-provision means re-bake.** A toolchain bump that isn''t followed by a
  fresh `./sc vm-bake` is invisible — every test still reverts to the old box.
- **Verify before the bake.** A snapshot of a half-installed box is a clean
  snapshot of a broken kit. Green checks first, bake second.
- **Broker-only.** You never hold the key, never run virsh, never redefine the
  snapshot. Ask the operator for the one host command instead.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
