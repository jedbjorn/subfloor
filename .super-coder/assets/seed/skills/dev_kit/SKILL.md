---
name: dev_kit
description: Read the fork's declared development hooks, execution seat, readiness state, evidence locations, and supported recovery surfaces. Planner-only on demand; Developer and Reviewer receive the same inventory in boot.
category: substrate
common: false
---

# dev_kit — read the fork development contract

The fork owns `.subfloor/dev-kit.json`. Subfloor validates the declaration,
selects the invoking checkout and host/container seat, runs exact hook argv,
and retains readiness evidence under `.sc-state/local/dev-kit/`. The engine
does not infer project policy from manifests or install privileged host tools.

## Hook inventory

The supported hook names are `deps`, `test`, `lint`, and `typecheck`:

```bash
sc deps [args...]
sc test [args...]
sc lint [args...]
sc typecheck [args...]
```

Read the declaration and its executable before running a hook. A configured
hook reports the selected checkout, cwd, seat, executable, and child status.
An absent hook is `unavailable`; do not reconstruct one from package metadata.

## Canonical states

| State | Meaning | Supported recovery |
|---|---|---|
| `absent` | No declaration exists; engine-baseline tools remain mechanisms, not project policy. | Add a tracked declaration only when the fork needs one. |
| `declared` | The declaration is valid; hook configuration is known, but execution/receipt evidence decides readiness. | Run the exact configured hook. |
| `invalid` | Declaration, path, mount, image, or invocation validation failed. | Correct the named tracked input and retry. |
| `ready` | The hook can execute on the active seat or the exact Docker receipt is current. | Continue. |
| `failed` | A declared hook or provisioning attempt ran and failed. | Inspect retained logs/evidence; retry the same supported surface. |
| `stale` | Docker provisioning or package evidence no longer matches the declaration, checkout, image, or labels. | From the host, run `sc launch`; use repair only after a failed attempt. |
| `advisory` | Engine baseline is runnable while a declared native-package candidate is degraded. | Inspect the named advisory evidence and submit a reviewed tracked remediation. |
| `repair` | A retained-container repair session is open without readiness. | Exit to the host, rerun `sc launch`, and require `ready`. |

Unavailable executable = exit 126; missing hook = exit 78; invalid
configuration = exit 64. A started child preserves its own status.

## Seats and evidence

Host hooks use the host checkout and toolchain. Container hooks use the
bind-mounted checkout, engine-baseline tools, declared sandbox extension, and
current provisioning receipt. `$SC_DEV_PORT` is loopback-bound on the host and
published from `0.0.0.0` in the container. A configured `$DATABASE_URL` reaches
the fork application sidecar; it never points at the engine memory DB.

`SC_SEAT=gui` wraps shell `test`, `lint`, and `typecheck` invocations in one
registered job. After the job id is confirmed, end the turn; its completion
wake tells you to inspect `sc job status <id>` and `sc job tail <id>` before
continuing. TUI and Admin hooks remain foreground without a completion wake
and return the child status; `deps` remains foreground in every seat. The wrapper marker is consumed by the
runner and removed from the hook's environment, so nested hooks get their own
foreground run and return their real status without a completion wake. A
separate nesting marker prevents GUI wrapping inside a hook.

Every executed hook retains a log and adjacent `.receipt.json` under
`.sc-state/local/devkit-logs/<hook>/`. The receipt records declared argv with
appended arguments, checkout, host/container seat, starting commit and branch,
exit status, duration and log path. Test receipts parse pytest's terminal
summary and failing ids; an absent summary is `null`, never zero failures.
Authenticated shell runs register before execution; wrapped hooks attach to
the existing run. Standalone CI and Admin retain local receipts without
claiming a shell run or completion wake. Receipts are local test evidence;
GitHub checks still own the merge gate.

Pruning retains the newest twenty completed log/receipt pairs per hook and
keeps ledger receipts with `evidence_pruned` set. If that acknowledgment is
unavailable, the files remain for a later pruning attempt. Submission outages
retain the run's receipt and outcome for reconciliation, without rerunning the
hook. `SC_DEVKIT_OUTPUT=full` streams stdout and stderr while retaining the same
log and receipt. Provisioning/readiness evidence lives under
`.sc-state/local/dev-kit/`. Planner
uses this skill for pinch-hit development and capability design. It describes
the available surface and boundaries, not the fork's test assertions,
deployment ritual, database technique, or VM lifecycle.

## RUN RECEIPTS

Attach completed local evidence by run id with `sc job attach <id> --pr <number>`
or `sc job attach <id> --work-unit <id>`. The run and target must share an owner;
a PR must already be registered. Use `--repository owner/name` if its number is
ambiguous. The Runs drawer and fleet offer the same attachment action.
Receipts show beside the observed GitHub check in Chats and the Sprint board.
A commit outside the PR head's ancestry is stale; unavailable ancestry stays
unknown. Pruned logs retain their receipt. Receipts supplement CI evidence;
only GitHub checks and the existing merge grant authorize a merge.
