---
title: Windows VM broker
tags: [subfloor, windows, broker, runbook]
date: 2026-09-07
project: subfloor
purpose: Operate a configured VM through its broker
---

# Windows VM broker

## Applicability

**Posture: current operator runbook.** Applies to a fork with an already
provisioned Windows VM linked in `instance.json.vm`. The host owns its SSH key,
libvirt access and baseline preparation. A sandboxed shell uses typed `sc vm`
commands; Planner supplies fork-local operating guidance and scope.

For the installation and target boundary, see
[Windows test VM architecture](windows-test-vm.md). The separate `sc vm test`
controller has its own lease, target and baseline procedure; it is not a second
name for the configured VM broker.

## Boundary and configuration

The broker is a host process serving a per-fork Unix socket under the engine's
runtime directory. The sandbox can access that socket without receiving the
SSH key, a host route or a hypervisor client. The saved `vm` block selects the
VM, SSH endpoint, transfer directory, snapshot and optional libvirt URI.

Guest `exec` deliberately runs commands on the configured guest. That is broad
guest authority, not permission to run arbitrary host commands or select a
different target. A snapshot reset changes the guest; use only the authorized
testing baseline. Capture and transfer outputs may contain application data,
so review artifacts before publishing them.

## Normal operation

Use `sc vm --help` and the selected verb's `--help` for exact syntax.

| Task | Typed surface | Expected evidence |
|---|---|---|
| Inspect | `sc vm status` | Broker, domain, SSH and MCP state without mutation |
| Start | `sc vm start` | Non-resetting start with bounded SSH readiness |
| Transfer | `sc vm push` | Artifact staged through the configured transfer directory |
| Execute | `sc vm exec` | Guest stdout, stderr and exit result |
| Capture | `sc vm capture` | Validated screenshot artifact metadata |
| Reset and stop | `sc vm reset --off` | Restored configured baseline and confirmed powered-off state |
| Inspect/start/stop GUI seam | `sc vm mcp status`, `up`, `down` | Tunnel, relay and endpoint evidence |

Do not infer successful execution from transport success alone. If reset
returns an unknown outcome, inspect state before retrying; an interrupted
response is not proof that the guest remained unchanged.

## Host lifecycle

`sc launch` starts the broker when a VM is linked; `sc down` stops the
lifecycle-managed process. `sc vm-broker` runs it in the foreground;
`sc vm-broker-up` and `sc vm-broker-down` manage its background form.
`sc vm-broker-sock` prints the configured socket location.

For reboot survival, the operator can install the user service with
`sc vm-broker-install`. A launch reuses an answering broker; ordinary down
does not take ownership of a separately systemd-managed process. Use the
matching uninstall surface when removing that supervision.

Baseline creation is host provisioning. `sc vm-bake` is host-side and is not a
broker verb offered to sandbox callers. Operate existing supervision through
its owner rather than starting a competing broker.

## Windows-MCP seam

For adapters that advertise managed streamable-HTTP MCP support, the engine
injects the `windows-mcp` endpoint. `sc vm mcp up` brings up the broker-owned
SSH tunnel and local relay, then verifies the endpoint. The tunnel reaches the
configured guest's loopback service; callers do not choose an arbitrary host
or port. `sc vm mcp status` distinguishes tunnel, relay and endpoint state.

```linear
Harness endpoint :::class1 -> Local relay :::class2 -> Broker socket :::class2 -> Host SSH tunnel :::class2 -> Guest Windows-MCP :::class3
```

The relay listens on container loopback. Its Unix-socket bridge keeps the host
SSH credential outside the sandbox. Guest UI automation carries broad guest
access comparable to command execution; it does not grant host hypervisor
control. Consult the active adapter manifest for support instead of assuming
all harnesses accept this injection.

## Source owners

- `api/vm_broker.py`: host socket API.
- `scripts/vm.py`: configuration, typed client and broker operations.
- `scripts/vm_mcp_relay.py`: local TCP-to-socket relay.
- `api/server.py`: GUI VM configuration and validation routes.
- `scripts/dispatch.sh`: lifecycle and host service commands.
