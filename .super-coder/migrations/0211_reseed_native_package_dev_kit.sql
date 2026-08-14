-- 0211 — reseed native-package-aware dev-kit guidance.
-- Converge existing installs to the separated core/package readiness and
-- system-managed advisory contract; the seed asset supplies fresh installs.

BEGIN;

UPDATE skills SET
  description='Run fork-owned dev-kit hooks and diagnose host or Docker provisioning states without inferring project policy.',
  category='substrate',
  command=NULL,
  common=0,
  content='# dev_kit — target-aware project tooling

`deps`, `test`, `lint`, and `typecheck` are invariant exact-execution hooks on
both host and Docker seats. The fork owns their argv in the tracked
`.subfloor/dev-kit.json`; the engine validates the declaration, selects the
invoking Git checkout, runs that argv without a shell, preserves child output
and status, and reports the selected checkout, cwd, seat, and executable.

The engine never infers manifests, languages, package managers, tools, file
sets, or acceptance policy. It never installs privileged host packages. A
missing hook is intentionally non-successful, not a request for a fallback.

From a checkout, bare `sc` uses the managed cwd-resolving wrapper on the host
and the equivalent baked wrapper in Docker.
<!-- sc-root-only: the tracked launcher is the fallback when the managed wrapper is unavailable -->
`./sc` remains valid and behaviorally identical for root-checkout commands.

## Read the active seat

Read the boot document''s execution-context section before acting. It is the
authority for this shell''s active seat.

- **Host:** commands and project processes run directly on the host. Respect an
  existing supervisor (`pm2`, `systemd`, or `make`) and bind ad-hoc dev servers
  to `127.0.0.1:$SC_DEV_PORT` unless the task requires another interface.
- **Docker:** the checkout is bind-mounted at its host path. Run a dev server on
  `0.0.0.0:$SC_DEV_PORT`; the published host URL is
  `http://127.0.0.1:$SC_DEV_PORT`. The FnB''s host-supervised app is a separate
  instance.

Host lifecycle remedies such as `sc launch` and `sc enter --devkit-repair`
must be run from a host terminal. If this shell is in Docker, exit the container
before using them. Never restart the FnB''s host stack from a sandbox shell.

## State and remedy contract

User-facing dev-kit output uses these states consistently:

| State | Meaning | Remedy |
|---|---|---|
| **absent** | The message `no fork dev kit declared` means the declaration is absent; the named hook may instead be unconfigured. The engine baseline remains usable, and an absent hook uses exit `78`. | Add or correct the fork-owned declaration only if the fork needs that capability. |
| **invalid** | The declaration, path, mount, image identity, or invocation failed validation before trusted execution. Hook configuration errors exit `64`. | Correct the reported fork-owned file or invocation, then retry the same command. |
| **failed** | A declared hook or provisioning attempt started but did not succeed. Docker retains the container and local attempt evidence and writes no ready receipt. | On the host, inspect `.sc-state/local/dev-kit/`, retry with `sc launch --no-build`, or enter `sc enter --devkit-repair`. |
| **stale** | A declared Docker provision step has no current receipt, or its fingerprint no longer matches the declaration, inputs, checkout, image, or labels. Normal entry is blocked. | On the host, run `sc launch`; if provisioning fails, use the failed/repair path. |
| **advisory** | A declared native apt package or package-dependent candidate failed while the engine baseline remained runnable. Core shell entry stays available; `native_packages=advisory` and `fork_readiness=degraded` are not blocker states. | From the fork root, run `make dos-admin`, inspect the named status/proof evidence and selected baseline, then submit a reviewed tracked remediation. Never infer, rename, unpin, or substitute a package. |
| **ready** | The selected hook can run, or Docker has a current receipt for the exact provision fingerprint and pinned image labels. | Continue with the declared hook or normal `sc enter`. |
| **repair** | An explicit retained-container session is open without a readiness claim. Normal shell entry remains blocked. | Diagnose the declaration/hook, exit to the host, rerun `sc launch`, and require a ready result. |

An unavailable executable exits `126`; a started child keeps its
shell-observable status. `SC_DEVKIT_ROOT`, `SC_DEVKIT_SEAT`, and
`SC_DEVKIT_HOOK` tell fork-owned code which checkout, seat, and hook the engine
selected.

## Ownership layers

- **Engine baseline:** the shipped sandbox image and generic runner. Its baked
  tools are mechanisms, not a promise that a fork uses them.
- **Native packages:** an optional bounded `sandbox.packages.apt` array of exact
  `NAME` / `NAME=VERSION` atoms. The engine installs the canonical array over
  the immutable baseline and proves every package in the final image. Pass =
  the format-version-2 receipt matches the current labels, proof, and checkout.
- **Fork extension:** an optional fork-owned Dockerfile and mounts declared in
  `.subfloor/dev-kit.json`. The Dockerfile must extend `SC_BASE_IMAGE`; the
  engine passes the exact package-layer ID when native packages are declared.
- **Checkout setup:** an optional fork-owned provision hook plus explicit input
  files. A successful receipt is keyed to the declaration, executable, inputs,
  checkout identity, extension image identity, labels, and seat.
- **Host prerequisites:** Git, Docker, language runtimes, credentials, and
  privileged packages installed by the operator. The engine reports missing
  prerequisites; it does not elevate or install them.

Read `.subfloor/dev-kit.json` and its executable before invoking a hook. Run
`sc deps` first only when the declaration makes `deps` the fork''s dependency
policy. A fork may choose a virtualenv, npm, another tool, or no dependency step
at all. In Docker, fork code must treat an out-of-checkout interpreter as
host-managed and shared: verify it, but never install into it.

Treat package advisories as capability evidence, not authorization to edit a
live declaration or restart the sandbox. Inspect `.sc-state/local/dev-kit/` and
the System-managed Flags record. Pass remediation back to the FnB as a reviewed
tracked change; only the FnB authorizes downstream materialization and cutover.

## Engine-baseline tools

The standard sandbox image includes `rg`, the `sqlite3` CLI, `curl`, Node 22,
npm, and Playwright with Chromium at
`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`. These are available mechanisms,
not inferred lifecycle hooks. Frontend tools such as `svelte-check`, `tsc`, and
vitest still come from fork-owned dependencies and run only through declared
policy.

## Postgres sidecar (app-only)

When a fork sets `"pg": {}` in `.super-coder/instance.json` (`sc pg-init` adds
it), `sc launch` starts a `postgres:17` sidecar and forwards `DATABASE_URL` into
Docker. This is only the fork application''s database. The engine memory DB is
always SQLite and never reads `DATABASE_URL`.

Inside Docker the app connects by the container hostname in `DATABASE_URL`, not
`127.0.0.1`. The fork owns its Postgres driver and its declared setup/test
hooks. Data persists in the install-owned Docker volume.

An unset `DATABASE_URL` means no sidecar is configured. A set URL with an empty
schema means provision the real app DB through the fork''s migrations and
bootstrap; it is not a blocker and is not permission to create a second
throwaway database.

## Stance

The declaration and active boot seat are the truth. Diagnose the exact state,
use the remedy for that seat, and require observable execution evidence rather
than command narration. Do not convert an absent capability into inferred
policy or a repair session into a readiness claim.',
  is_deleted=0
WHERE name='dev_kit';

COMMIT;
