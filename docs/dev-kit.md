---
title: Subfloor — Dev kit
tags: [substrate, dev-kit, configuration]
date: 2026-09-21
project: subfloor
purpose: The .subfloor/dev-kit.json schema
---

# Subfloor — Fork dev-kit declaration

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/dev-kit.md)

## Overview

A fork owns its dependency, test, lint and typecheck policy in one tracked
file, `.subfloor/dev-kit.json`. The engine **validates** that declaration and
runs the exact argv attached to each named hook. It does not discover
manifests, create a virtualenv, install packages, or choose a test runner. The
workflow-level account is in the user guide's
[Dev kit](README.md#dev-kit) section; this page is the schema reference.

| Fact | Value |
|---|---|
| Path | `.subfloor/dev-kit.json`, relative to the invoking Git checkout |
| Tracked | yes — it is fork source, not generated state |
| Absent | supported; boot reports `no fork dev kit declared` and hooks exit `78` |
| Present but invalid | hard refusal, exit `64` — there is no partial fallback |
| Schema version | integer `1` only |

> [!class4]
> **Every declared path must stay inside the invoking checkout and must already exist.** Absolute paths are refused, `..` escapes are refused, and a `cwd`, `dockerfile`, `context` or `provision` input that does not resolve to an existing directory or file fails validation rather than being created.

The validator is `.super-coder/scripts/devkit.py`; sandbox image work is
`.super-coder/scripts/sandbox_devkit.py`. Refusal messages are field-addressed
in JSONPath form — `$.hooks.test.argv[0]: absolute executable paths are
forbidden` — so the message names the exact key to fix.

## Top-level keys

```json
{
  "version": 1,
  "hooks": { },
  "provision": { },
  "sandbox": { }
}
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `version` | integer | yes | Must be exactly `1`; a string `"1"` is refused. |
| `hooks` | object | no | Default `{}`. Keys are hook names; see the next tab. |
| `provision` | object | no | One first-run preparation step. |
| `sandbox` | object | no | Docker image extension, named volumes, native packages. |

Any other top-level key is refused with `unknown key '<name>'`. A duplicate
key anywhere in the JSON document is refused (`$: duplicate key`), as is
malformed JSON, which reports the line and column.

Evidence: `devkit.py:424-472`.

## Hooks

The hook name set is **closed**: `deps`, `test`, `lint`, `typecheck`. There is
no `build` or `serve` hook — an unknown name is refused with
`$.hooks: unknown hook '<name>'`. Each maps to one CLI verb:

```bash
./sc deps          # exact fork-declared dependency hook
./sc test          # exact fork-declared test hook
./sc lint [paths]  # exact fork-declared lint hook plus literal caller args
./sc typecheck     # exact fork-declared typecheck hook
```

| Field | Type | Required | Default | Rules |
|---|---|---|---|---|
| `argv` | array of strings | yes | — | Non-empty; every entry non-empty and free of NUL. |
| `cwd` | string | no | `"."` | Repo-relative; must resolve to an existing directory inside the checkout. |

`argv[0]` resolves in one of two ways:

| Shape | Resolution | Refusal |
|---|---|---|
| contains `/` (e.g. `./.subfloor/dev-kit`) | resolved relative to the hook's `cwd`, must stay inside the checkout | escapes the checkout, or absolute |
| bare name (e.g. `make`) | looked up on the child's `PATH` at run time | absolute path given |

Absolute executables are refused in both shapes. `argv[0]` and `cwd` are also
rendered into the Developer and Reviewer boot documents, so they may not
contain backticks or control characters.

Caller arguments are appended **literally** after the declared argv — `./sc
lint src/ tests/` runs `<declared argv> src/ tests/`. The engine adds no flags
of its own.

Evidence: `devkit.py:29,224-242,832,849` · `dispatch.sh:1362-1365`.

## Execution and output

The hook child runs with `cwd` set to the declared directory and the caller's
environment plus three neutral facts:

| Variable | Value |
|---|---|
| `SC_DEVKIT_ROOT` | the resolved invoking checkout |
| `SC_DEVKIT_SEAT` | `docker` when `SC_SANDBOX` is set, otherwise `host` |
| `SC_DEVKIT_HOOK` | the hook name |

Exit codes are a contract:

| Code | Meaning |
|---|---|
| `78` | Declaration absent, or the named hook is not declared. No fallback is attempted. |
| `64` | Declaration invalid, or `SC_DEVKIT_OUTPUT` is neither `compact` nor `full`. |
| `126` | The declared executable is unavailable, not executable, or failed to start. |
| child status | Anything else is the child's exact shell-observable status; a signal `N` reports as `128 + N`. |

`deps` always streams its full output. `test`, `lint` and `typecheck` default
to **compact** mode: the full stream is written to a log and the command
prints a bounded envelope naming the checkout, seat, hook, command, cwd, exit
status, duration, output size, log path and hook state, followed by an
excerpt and the recovery line `dev-kit full output: SC_DEVKIT_OUTPUT=full …`.

| Aspect | Success | Failure |
|---|---|---|
| Excerpt sections | diagnostic digest, tail | head, diagnostic digest, tail |
| Section caps (lines) | 24 diagnostics, 40 tail | 40 head, 80 diagnostics, 80 tail |
| Envelope bound | 80 lines / 16 KiB | 240 lines / 48 KiB |

Logs land under the gitignored
`.sc-state/local/devkit-logs/<hook>/<UTC stamp>-<pid>-<uuid>.log`, and the 20
most recent per hook are retained. Output is ANSI-stripped and control
characters are escaped before display; a log is finalized from a `.running`
suffix only after the child is reaped, and an interrupted run says so.

Evidence: `devkit.py:36-51,658-695,719-787,790-874` ·
`artifact_policy.py:122-125`.

## Provision

`provision` names one declared hook to run as the fork's first-run preparation
inside the sandbox, plus the tracked inputs that decide when it must run again.

```json
"provision": {
  "hook": "deps",
  "inputs": ["requirements.txt", "package-lock.json"]
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `hook` | string | yes | Must name a hook declared in `hooks`; otherwise `must name a declared hook`. |
| `inputs` | array of strings | no | Default `[]`. Repo-relative paths that must each resolve to an existing **file**. |

Each input is digested into a capability receipt written under
`.sc-state/local/dev-kit/<checkout identity>/`. A receipt is only produced when
the current tracked commit is clean — a dirty tree refuses with
`capability receipt requires the current tracked commit to be clean`. When the
recorded fingerprint still matches, launch reuses the receipt instead of
re-running the hook.

Declaring `provision` changes the readiness contract: until the receipt is
current, the sandbox reports `fork_readiness=degraded` and `package_receipt=pending`.

Evidence: `devkit.py:245-266` · `sandbox_devkit.py:1316-1324,1456-1463,2307-2356`.

## Sandbox

```json
"sandbox": {
  "dockerfile": ".subfloor/sandbox/Dockerfile",
  "context": ".subfloor/sandbox",
  "mounts": [ { "name": "node-modules", "target": "node_modules" } ],
  "packages": { "apt": ["libexample1", "tool=1.2-3"] }
}
```

| Key | Type | Notes |
|---|---|---|
| `dockerfile` | string | Repo-relative existing file. Must be Git-tracked inside its context. |
| `context` | string | Repo-relative existing directory; defaults to the dockerfile's parent. Requires `dockerfile`. |
| `mounts` | array | Named Docker volumes mounted into the container at repo-relative targets. |
| `packages` | object | Exactly one key, `apt`. |

The object must contain `dockerfile` or `packages`; an object with neither is
refused. `context` without `dockerfile` is refused.

**The build context is the Git-tracked content of `context`, nothing else** —
the engine reads the index, not the working tree, and builds from a
deterministic archive. Untracked and ignored files never enter the image.
Bounds: at most 4096 entries, 32 MiB per file, 128 MiB in aggregate, 512 bytes
per path; modes `100644`, `100755`, `120000` only; a symlink must resolve
inside the context. The extension Dockerfile must honour the
[`SC_BASE_IMAGE` contract](environment.md#build-contracts).

**Mounts** become labelled Docker named volumes, created on demand and chowned
to the sandbox user.

| Rule | Refusal |
|---|---|
| `name` matches `[a-z0-9][a-z0-9_-]{0,47}` | `must be 1-48 lowercase letters, digits, underscores, or hyphens` |
| `name` unique | `duplicate mount name` |
| `target` repo-relative; if it exists it must be a directory | `must resolve to a directory` |
| `target` must not touch `.git`, `.super-coder`, `.sc-state` or the declaration | `must not overlap Git metadata, engine state, or the declaration` |
| targets must not nest inside each other | `must not overlap mount target` |

**Packages** are exact native Debian atoms — `name` or `name=version`, with no
architecture qualifiers, repository options, inferred names or relaxed pins.

| Bound | Value |
|---|---|
| Entries | 1–64, no duplicate names |
| Name pattern | `[a-z0-9][a-z0-9+.-]{1,127}` |
| Version pattern | `([0-9]+:)?[0-9][A-Za-z0-9.+~-]{0,126}` |
| Bytes | 2–256 per entry, 8192 total |

The list is canonicalized by sorting on name, so declaration order does not
change the image identity. Package-local invalidity is an **advisory**, not a
refusal: the rest of the declaration still validates, the proven engine
baseline is selected, and the CLI and Flags report
`native_packages=advisory` / `fork_readiness=degraded`. That advisory never
blocks shell entry or runtime.

Evidence: `devkit.py:269-412` · `sandbox_devkit.py:633-714,1660-1677,1702-1745`.

## Readiness

The boot document's `DEV TOOLS` section (Developer and Reviewer flavors;
Planner can load the `dev_kit` skill on demand) reports one state per boot,
each with its own recovery line.

| State | Meaning | Recovery |
|---|---|---|
| `absent` | No declaration. | Add a tracked declaration only when the fork needs one. |
| `declared` | Valid, but at least one hook's executable is unavailable on this seat. | Run the exact configured hook to produce execution evidence. |
| `invalid` | Declaration failed validation. | Correct the named tracked input, then retry. |
| `ready` | Every declared hook is available and, where required, the receipt matches. | Continue through the configured hook. |
| `failed` | Recorded status or core runtime failed. | Inspect retained evidence and retry the same supported surface. |
| `stale` | Sandbox receipt missing or not matching current tracked inputs. | From the host run `sc launch`; use repair after a failed attempt. |
| `advisory` | Native packages degraded to the proven baseline. | Inspect the named evidence and submit a reviewed tracked remediation. |
| `repair` | Booted via `./sc enter --devkit-repair`; no readiness claim is made. | Exit to the host, rerun `sc launch`, and require `ready`. |

A receipt is required only in the **container** seat and only when
`provision`, `sandbox.dockerfile` or `sandbox.packages` is declared. The status
record binds the checkout identity, the canonical declaration digest, the
package digest and the engine ref — change any tracked input and the state
becomes `stale` until the next launch reconciles it. Normal `./sc enter` is
blocked while the state is stale.

Evidence: `run.py:94-297` · `compose.py:154-242` · `dispatch.sh:1526-1579`.

## Examples

A minimal declaration — this repository's own, which routes every hook through
one fork-owned script:

```json
{
  "version": 1,
  "hooks": {
    "deps":      { "argv": ["./.subfloor/dev-kit", "deps"],      "cwd": "." },
    "test":      { "argv": ["./.subfloor/dev-kit", "test"],      "cwd": "." },
    "lint":      { "argv": ["./.subfloor/dev-kit", "lint"],      "cwd": "." },
    "typecheck": { "argv": ["./.subfloor/dev-kit", "typecheck"], "cwd": "." }
  }
}
```

A fuller one, adding first-run provisioning, a dependency volume and native
packages. Every path shown must exist and be tracked in the fork that declares
it:

```json
{
  "version": 1,
  "hooks": {
    "deps":      { "argv": ["npm", "ci"] },
    "test":      { "argv": ["npm", "run", "test"] },
    "lint":      { "argv": ["npm", "run", "lint"] },
    "typecheck": { "argv": ["npm", "run", "typecheck"] }
  },
  "provision": {
    "hook": "deps",
    "inputs": ["package.json", "package-lock.json"]
  },
  "sandbox": {
    "dockerfile": ".subfloor/sandbox/Dockerfile",
    "context": ".subfloor/sandbox",
    "mounts": [ { "name": "node-modules", "target": "node_modules" } ],
    "packages": { "apt": ["libvips42"] }
  }
}
```

Its `.subfloor/sandbox/Dockerfile`, Git-tracked inside the declared context:

```dockerfile
ARG SC_BASE_IMAGE
FROM ${SC_BASE_IMAGE}
RUN npm install --global pnpm@9
```

> [!class2]
> **Check a change before you rely on it.** Run any hook once from the shell seat that will use it; `./sc enter` reports the resulting `DEV TOOLS` state on the next boot, and `SC_DEVKIT_OUTPUT=full` shows the whole stream when the compact envelope is not enough.
