---
title: Windows test VM architecture
tags: [subfloor, windows, vm, testing]
date: 2026-09-07
project: subfloor
purpose: Supplied VM boundary and test workflow
---

# Windows test VM architecture

## Applicability

**Posture: current architecture reference.** Applies to forks that link an
operator-supplied Windows VM. Subfloor does not create the guest, install its
OS or silently turn a daily-use VM into a disposable test target.

The operator supplies guest SSH, transfer access, a verified clean snapshot and
required toolchain. The Scripts tab's VM link flow validates configuration;
the host broker owns SSH and hypervisor access. Shells use the typed `sc vm`
client. See the [broker runbook](windows-vm-broker.md).

## Ownership and sequence

1. The operator provisions the intended test VM and installs its toolchain
   before taking the baseline snapshot.
2. Link and validate the fork's `vm` configuration. Example domain names,
   accounts, ports and transfer paths must be replaced with the supplied target.
3. Planner records the fork's actual testing capability and authority as a
   local skill. Retired global names such as `configure_winbox` and
   `windows_devkit` are not current grants.
4. Developers build and test through the supplied client; Reviewers independently
   verify candidate artifacts. Reset only the authorized disposable target and
   confirm its final state before handing it back.

A fork-committed toolchain manifest can make guest preparation reproducible.
For example, a Windows installer project may need WiX, a .NET SDK and MSBuild.
Those are application choices, not packages every Subfloor VM must install.

## Configuration and validation

The gitignored `instance.json` carries the optional `vm` block. It names the
libvirt domain, SSH endpoint and user, host key path, transfer directory,
snapshot and optional libvirt URI. The key path is not key material; private
credentials stay with the host broker.

The GUI reads/writes `/api/vm` and submits live checks to
`/api/vm/validate/{check}`. A successful config save is not proof of guest
readiness: check the domain, SSH, transfer directory, snapshot and toolchain
for the actual target. Use `sc vm status` before operation and consult verb
help for bounded execution, transfer and capture forms.

## Test and recovery surfaces

`sc vm` operates the configured fork VM through its host broker. The separate
`sc vm test` family operates the supplied fixed Windows test controller and
its lease/baseline workflow. These are different target contracts; do not mix
reset or baseline procedures between them.

The fixed controller's procedure is documented in
[windows_testing](skills/windows_testing/SKILL.md). Its capability must actually
be supplied to the seat. Neither this reference nor a config block grants
access to an unrelated VM.

### Disposable Windows security posture

The current fixed controller uploads a generated PowerShell script and runs it
with `powershell.exe -File`. If the guest's effective execution policy is
`Restricted`, the command is rejected before the requested test starts. On a
purpose-built disposable image, the operator may set the machine-wide policy
from an elevated Windows PowerShell prompt:

```powershell
Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy Bypass -Force
Get-ExecutionPolicy -List
Get-ExecutionPolicy
```

The final command must report effective policy `Bypass`; Group Policy at
`MachinePolicy` or `UserPolicy` can override `LocalMachine`. Verify with a real
controller push/exec/pull round trip and repeat exec after restoring the newly
promoted working baseline.

This is an explicit **disposable-image-only** tradeoff. `Bypass` removes
PowerShell's script-signing, warning, and prompt guardrails machine-wide, and
the controller account already has local Administrator authority. Never apply
this configuration to a daily-use, canonical, or otherwise persistent Windows
VM. The test image must contain no personal/work data, reusable credentials,
authenticated browser sessions, host shares, or other secrets. Frequent reset
limits persistence after the snapshot; it does not limit exposure while the VM
is running and cannot remove secrets captured in a baseline.
