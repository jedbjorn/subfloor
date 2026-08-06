#108 spec seq 1 · feature 38 — Windows VM Driving Hardening

---
title: Windows VM Driving Hardening
tags:
  - windows
  - vm
  - mcp
  - reliability
date: 2026-08-05
project: super-coder
purpose: Make Windows test driving dependable
---

## Status

Proposed for feature #38. This revision implements FnB scope correction #1419 and decision #101. It replaces the supervisor, finish-ledger, finish-fence, operator-disposition, and separate acceptance-fixture design from decision #100. Findings documents #109 through #112 concern that removed machinery and are moot for this revision.

The retained scope covers subfloor issues [#1020](https://github.com/jedbjorn/subfloor/issues/1020), [#1021](https://github.com/jedbjorn/subfloor/issues/1021), [#1022](https://github.com/jedbjorn/subfloor/issues/1022), [#1023](https://github.com/jedbjorn/subfloor/issues/1023), [#1024](https://github.com/jedbjorn/subfloor/issues/1024), [#1025](https://github.com/jedbjorn/subfloor/issues/1025), and [#1026](https://github.com/jedbjorn/subfloor/issues/1026), plus flags SC-467 and SC-468.

## Objective

Give a shell clear Windows-driving instructions and dependable tools with which to carry them out.

The work is complete when a shell can:

1. inspect the linked VM without changing it;
2. use its supplied running state, starting it only when it is off;
3. wait for guest readiness without inventing sleeps;
4. push files, execute Windows commands, and view screenshots without building raw JSON or decoding blobs;
5. obtain Windows GUI MCP tools through the active harness adapter;
6. start, inspect, and stop the MCP tunnel and relay safely;
7. reset to the configured testing snapshot and leave the VM powered off when testing finishes; and
8. receive a clear structured error and report it to the user when an operation cannot be confirmed.

## Product Boundary

The skills govern the procedure. The engine provides the operations the skills call.

- `windows_devkit` explains preflight, current-state startup, application readiness, push, exec, capture, failure reporting, and end-only reset.
- `windows_vm_gui` explains MCP setup, UI Automation driving, visual verification, teardown, and the same end-only reset rule.
- `./sc vm` provides stable commands so the shell does not assemble broker HTTP, Unix-socket curl, JSON, SSH, PowerShell quoting, screenshot decoding, relay process control, or harness-specific MCP registration.
- Harness adapters expose supported MCP servers before the harness begins. The skill brings the endpoint up when GUI work is needed.

The shell performs cleanup while it still has control. The engine does not supervise the harness, intercept every exit, or claim cleanup after an uncatchable process or host failure.

## Skill Workflow

The canonical procedure in both Windows-driving skills is:

1. Assume the operator supplied a running test VM with the testing application open.
2. Run `./sc vm status` before the first operation. This is read-only and never resets or restarts the VM.
3. If the VM is off, run `./sc vm start`. If it is running but SSH is not ready, the same command waits for readiness without restarting it.
4. If the testing application is not open, open it through `./sc vm exec` or the GUI tools, according to the test.
5. Use `push`, `exec`, and `capture` for scripted work. For GUI work, run `./sc vm mcp up`, verify the endpoint, then use the harness-provided Windows MCP tools.
6. Perform the test. A test failure does not skip cleanup.
7. When testing is finished and the shell still has control, attempt `./sc vm mcp down` if GUI transport was used, then run `./sc vm reset --off` once. Reset restores the configured testing snapshot and leaves the VM powered off.
8. Report the test result and the cleanup result separately. If an operation fails or returns an uncertain result, include its structured error for the user and do not claim it succeeded.

There is no reset at the beginning or during the test. Planning, probing, skill review, and static adapter verification must not reset the supplied VM.

## Tool Surface

`./sc vm` is the supported model-facing boundary. Existing raw broker routes may remain for internal compatibility and diagnostics, but the skills no longer teach them.

| Command | Required behavior |
|---|---|
| `./sc vm status [--json]` | Read broker, domain, SSH, tunnel, relay, endpoint, and adapter state without mutation. |
| `./sc vm start [--json]` | Start the current domain only when off, retain a running domain, and wait for SSH readiness within a bounded budget. Never reset or restart. |
| `./sc vm push SRC [DEST] [--json]` | Stage a permitted local artifact through the configured transfer directory. |
| `./sc vm exec [--command-file FILE] [--json] -- COMMAND...` | Execute the exact guest command without caller-built JSON. |
| `./sc vm capture [--output PATH] [--json]` | Save a validated screenshot artifact locally and return its path and metadata. |
| `./sc vm mcp status\|up\|down [--json]` | Inspect or control the verified broker tunnel and local relay. `up` verifies the HTTP endpoint before success. |
| `./sc vm reset --off [--json]` | Restore the configured testing snapshot, leave the domain off, wait for the broker result, and report the observed final domain state. |

The public reset form requires `--off`; the skills do not expose a reset-and-boot shortcut. `start` is the non-resetting way to make the supplied VM ready.

## Tool Contracts

Every command has concise human output, a nonzero exit on failure, and an optional single JSON object suitable for model inspection:

```json
{
  "schema_version": 1,
  "ok": false,
  "operation": "reset",
  "result": null,
  "error": {
    "code": "reset_result_unknown",
    "message": "the broker connection closed before reset could be confirmed",
    "details": {
      "domain_state": "unknown"
    }
  }
}
```

The stable requirements are `schema_version`, `ok`, `operation`, `result`, and `error`; each operation documents its result fields in CLI help and tests. Error details must remain bounded and must not contain credentials, key material, file contents, guest command text, screenshot pixels, or stack traces.

On validation, connection, timeout, malformed-response, or broker failure, the CLI returns a structured error rather than an empty body or a successful-looking shell status. The skill instructs the shell to report that error to the user. A read-only `status` check may add context, but the client does not automatically repeat a reset whose effect is uncertain.

`exec` accepts either arguments after `--` or one UTF-8 command file, never both. PowerShell quotes, variables, pipes, backticks, multiline input, paths, and Unicode pass without shell-authored JSON.

`capture` writes atomically to a mode-0600 file under `.sc-state/local/vm-captures/` by default. An explicit output path must remain inside an allowed local artifact area. Partial and oversized captures are removed and return an error.

## GUI MCP Access

Harness adapters declare whether and how they can expose a streamable-HTTP MCP server. Claude, Codex, and OpenCode are in scope where their current adapters support this injection. Kimi and Vibe must report Windows GUI MCP as unsupported until their adapters gain an equivalent mechanism.

For supported adapters:

- launch renders or passes a managed Windows MCP entry before the harness starts, pointing to the local relay endpoint;
- registration is adapter-specific and idempotent;
- setup does not reset, start, or otherwise mutate the VM;
- no skill runs `claude mcp add` or writes persistent user/project harness configuration;
- repeated shell launches do not produce duplicate-name failures;
- `./sc vm mcp up` starts the broker tunnel and relay, waits with paced probes, and verifies the endpoint before reporting success; and
- `./sc vm mcp down` stops only the tunnel and relay instances whose recorded process identity still matches.

The active harness tool list may be fixed at launch. That is why the adapter supplies the MCP definition before launch while the skill controls endpoint readiness during the session.

## Reliability Requirements

Relay and tunnel lifecycle must address #1020 and SC-467:

- paced readiness checks with a documented total timeout;
- stderr or a bounded log tail on early exit and bind failure;
- atomic state-file creation and stale-state cleanup;
- process ownership verified with PID plus start identity and expected executable before signaling;
- host and Docker-seat PID handling covered by tests; and
- idempotent status, up, and down behavior.

Broker and client behavior must address #1022, #1026, and SC-468:

- one guarded response path per request, including disconnect and timeout cases;
- payload-safe, bounded logging;
- `start` owns the bounded SSH readiness loop and reports attempts plus the last readiness error;
- the client timeout exceeds the broker's documented reset budget and remains responsive enough to report progress or timeout;
- powered-off reset returns a parseable result and observed domain state; and
- a lost or malformed response becomes `reset_result_unknown`, followed by user-visible reporting rather than an automatic reset retry.

Concurrency protection is limited to real shared resources. VM state-changing operations serialize through the existing broker boundary. Relay and tunnel commands use atomic state plus verified process identity. No session journal, finish ledger, finish fence, successor operation, or operator-disposition state machine is introduced.

## Implementation Map

| Area | Change |
|---|---|
| `.super-coder/scripts/vm.py` | Add/normalize the model-facing status, non-resetting start, push, shell-safe exec, capture artifact, MCP, and powered-off reset client paths; harden broker responses and readiness. |
| `.super-coder/scripts/vm_mcp_relay.py` | Fix listener readiness, logs, stale state, process identity, and bounded teardown. |
| `sc` | Dispatch the grouped `sc vm` command without requiring raw broker curl. |
| `.super-coder/adapters/*/adapter.json` and launch rendering | Declare and inject supported HTTP MCP configuration without persistent ad-hoc registration. |
| `.super-coder/assets/skills/windows_devkit/SKILL.md` | Replace opening reset and raw curl with the canonical skill workflow and typed commands. |
| `.super-coder/assets/skills/windows_vm_gui/SKILL.md` | Replace Claude-only registration with adapter-aware tool use and simple MCP command lifecycle. |
| trailing reseed migration | Distribute the two revised authoritative skill sources. |
| `tests/test_vm_broker.py` and focused new tests | Cover broker, client, relay, adapter, skill-render, and downstream regressions. |

`run.py` may render adapter MCP configuration before launch. It must not become a Windows session supervisor or own VM reset cleanup.

## Delivery Plan

1. Harden relay identity/readiness and broker response/readiness behavior. These two changes are parallelizable and establish reliable primitives.
2. Add the grouped `sc vm` status/start/reset and push/exec/capture clients against those primitives. Client subcommands may be split in parallel after the shared result/error helper lands.
3. Add adapter-declared MCP injection and the `mcp status/up/down` client flow. Verify the thin slice on the currently failing Codex sandbox seat.
4. Rewrite both skills and add the required trailing reseed migration only after command names and behavior are stable.
5. Run focused unit/integration tests, then the single downstream `rst-c` acceptance session.

The highest-risk checks land early: relay process identity across host/container namespaces, fixed-at-launch MCP discovery in Codex, and the long powered-off reset response.

## Acceptance

Automated verification must prove:

- #1020: relay `up` either reaches a verified listener or returns bind/exit evidence; stale or reused PID state cannot signal an unrelated process;
- #1021 and #1023: supported adapter configurations expose the Windows MCP definition without a Claude-only command, persistent duplicate registration, or repeat-launch failure;
- #1022: off and already-running domains reach bounded SSH-ready results through `start`, with timeout evidence on failure;
- #1024: representative multiline PowerShell containing quotes, dollar variables, paths, pipes, backticks, and Unicode reaches `exec` unchanged;
- #1025: capture produces a viewable local artifact and removes partial/oversized output;
- #1026: powered-off reset has a sufficient client budget and returns structured success or failure instead of an empty response;
- SC-467: relay state, identity validation, logs, and shutdown work on host and Docker seats; and
- SC-468: broker requests send at most one response and keep logs bounded and payload-safe.

Adapter golden tests cover every declared supported adapter. At minimum, a live Codex launch must show the Windows MCP tools on its first turn because #1021 was reported from Codex. Unsupported adapters must fail clearly rather than inherit Claude instructions.

The downstream gate uses the operator-supplied `rst-c` instance as one test session:

1. begin from its supplied state with no opening reset;
2. confirm `status`; start only if off; open the testing application only if absent;
3. exercise push, complex exec, capture, MCP up, one GUI observation/action, and MCP down;
4. run `reset --off` only after testing is complete; and
5. verify the reported snapshot restoration and powered-off domain state.

Static review, unit tests, adapter golden tests, and planning must not touch or reset that instance. The test record includes command results, the capture artifact path, adapter/tool evidence, final reset result, and any structured errors. Acceptance fails if it claims an unconfirmed operation succeeded.

## Anticipated User Activity

### Vocabulary

- **Valid Privileged User**: the operator who links and provisions the Windows test VM and selects its testing snapshot.
- **Shell**: an AI agent using its granted Windows skills, CLI commands, and harness-provided GUI tools.
- **System**: the local engine, broker, relay, and harness adapter acting for those explicit commands.
- **Unexpected Participant**: any process or caller outside the linked repo and granted local tool path.

### Expected Activity

- The Valid Privileged User supplies the linked VM, snapshot, credentials by path, and application state.
- The Shell observes current state, starts or opens only what is absent, performs tests, and attempts the documented end cleanup.
- The System validates configuration, serializes VM mutations, carries commands and artifacts, and returns bounded results.
- The Shell reports failed or uncertain operations to the Valid Privileged User instead of asserting success.

### Reach

- The Shell reaches the feature through local `./sc vm` commands and injected MCP tools.
- The broker reaches libvirt and SSH on the host; sandboxed shells do not receive keys or direct libvirt access.
- The relay listens only on configured loopback and connects only to the broker-owned Unix socket.
- Captures stay under the repo's local artifact area unless a checked output path is supplied.

### Data Tenancy

- VM configuration and artifacts belong to the linked local repo and operator.
- Guest command output and screenshots remain local; no new shared or remote store is introduced.
- Keys remain host-side and are referenced by path only.

### Beyond Intention

- A Shell cannot use these commands to read key material, signal an unrelated process, bind the relay beyond loopback, or write captures outside allowed artifact paths.
- Unsupported harnesses do not receive fabricated GUI instructions.
- The feature does not provision or bake the VM, install guest toolchains during tests, or reset at session start.

## Out of Scope

- Automatically resetting the VM when a harness exits, crashes, is killed, or the host loses power.
- Atomic or exactly-once reset guarantees.
- Harness supervision, signal forwarding, browser-close hooks, durable cleanup journals, ledgers, fences, recovery successors, or operator disposition APIs.
- A separate Windows matrix fixture or one reset per adapter/seat.
- VM provisioning, snapshot creation, or guest toolchain installation.
- Application-specific test automation.
- Claiming successful cleanup after an empty, timed-out, malformed, or otherwise uncertain response.

These exclusions are deliberate. The product objective is a well-instructed model with dependable tools, not a transactional VM orchestration service.

