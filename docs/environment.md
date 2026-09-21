---
title: Subfloor — Environment
tags: [substrate, configuration, operations]
date: 2026-09-21
project: subfloor
purpose: Every environment variable the engine reads
---

# Subfloor — Environment variables

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/environment.md)

## Overview

The engine reads configuration from three places: the gitignored
`.super-coder/instance.json` (ports, runtime mode, opt-in feature blocks), the
tracked `.subfloor/dev-kit.json` ([dev kit](dev-kit.md)), and the process
environment. This page is the reference for the third.

Variables fall into five audiences, one per tab:

| Audience | Who sets it | Rule |
|---|---|---|
| **Operator knobs** | you, in the launch environment | supported; a fork may set them per machine |
| **Engine-set context** | the launcher, at boot | read-only facts a fork script or hook may consume |
| **Build contracts** | you declare, the engine supplies | a fork's sandbox extension Dockerfile must honour them |
| **Internal** | the engine, process to process | not an interface; do not set |
| **Non-`SC_` variables** | mixed | standard names the engine also honours |

> [!class4]
> **No secret belongs in this file or in a shell prompt.** The engine injects `SC_API_TOKEN` per boot from the shell's own API key; harness credentials stay in their host directories and are bind-mounted; the Tavily key and the app-DB DSN live in host-side files. Set a credential variable only in your own shell environment, never in a tracked file.

An unset variable always means "use the default" — the engine never infers a
value from a neighbouring one. An invalid value is refused or warned about at
the point of use, never silently substituted; each row below says which.

## Operator knobs

Set these in the environment you run `./sc` from. Unless noted, they apply to
the next launch and need no restart of anything else.

| Variable | Default | Effect | Read by |
|---|---|---|---|
| `SC_PYTHON` | `python3` on `PATH` | Absolute path of the Python 3.14.x interpreter every engine command uses. A set-but-not-executable value refuses the preflight. | `dispatch.sh:56,89,119-123` |
| `SC_PROTECTED_BRANCHES` | `main master` | Space-separated branches the branch guard refuses edits and commits on. Its **first** entry is also read as the repo's default branch for freshness and hygiene reports. | `branch-guard.sh:56-58` · `git_freshness.py:59` · `git_hygiene.py:68-70` |
| `SC_SHARED_DIRS` | unset | Space-separated absolute paths every shell may write into without a branch-guard warning — host handoff and screenshot folders. Passed through to the harness automatically. | `branch-guard.sh:156-164` · `run.py:2742-2745` |
| `SC_DISABLED_HARNESSES` | unset | Comma-separated harness names suppressed from every surface (picker, browser, Sprints). Forwarded into the sandbox when set. | `harness_surfaces.py:24-29` · `dispatch.sh:1414` |
| `SC_DB_BACKUP_DIR` | `~/db_backups/<repo>`, then `.sc-state/db_backups` | First-choice engine DB backup destination. Every candidate is created and write-probed; this is the remedy the engine itself prints when none is writable. | `db_backup.py:4-8,66` · `instance_state.py:748` |
| `SC_CHAT_INACTIVITY_CEILING` | `3600` (seconds) | Silence after which a hung chat turn is closed so the reaper can collect it. A non-numeric or non-positive value logs a warning and uses the default. | `activity_monitor.py:17,24-36` |
| `SC_REAPER_HEARTBEAT_SECONDS` | `60` | Reaper scan interval. Must be a positive number or the reaper refuses to start. | `conversation_reaper.py:22,72-76` |
| `SC_REAPER_TERM_GRACE_SECONDS` | `15` | Grace between interrupt and `TERM`. Zero allowed; negative refused. | `conversation_reaper.py:23,77-82` |
| `SC_REAPER_KILL_GRACE_SECONDS` | `15` | Grace between `TERM` and `KILL`. Zero allowed; negative refused. | `conversation_reaper.py:24,83-88` |
| `SC_REAPER_YOUNG_GRACE_SECONDS` | `30` | Age below which a fresh run is left alone. Zero allowed; negative refused. | `conversation_reaper.py:25,89-94` |
| `SC_SPRINT_FORCE_NEW_QUIET_SECONDS` | `10` | Quiet gate a `force-new` wake waits out before closing the old chat. Must be a non-negative number. | `sprint_message_delivery.py:985-998` |
| `SC_NET` | `sc-net` | Docker network the sandbox and the Postgres sidecar share. Set it to isolate one fork onto its own network. | `dispatch.sh:367-368,417,1505` |
| `SC_PG_SHM` | `1g` | `--shm-size` for the Postgres sidecar. Changing it needs `pg-down` then `pg-up`. | `dispatch.sh:378-379,923,2021` |
| `SC_DATABASE_URL` | `postgresql://sc:sc@<pg container>:5432/sc` | Overrides the `DATABASE_URL` forwarded into the sandbox for the fork's **app**. The engine memory DB is never affected. | `dispatch.sh:1417-1424,2017` |
| `SC_RO_ENVFILE` | `~/.config/<repo>/db-broker.env` | Path of the host-side env file that carries the app-DB broker's read-only DSN. | `dispatch.sh:842-843` |
| `SC_RO_DSN` | — | The read-only DSN itself, written into that env file host-side. Never mounted into the sandbox; the name is the broker's configured `dsn_env`. | `dbq.py:28,62,71` · `dispatch.sh:879,889` |
| `SC_PARENT_IMAGE` | `python:3.14-slim` | Image the engine sandbox baseline builds from. Must be exactly one non-empty reference with no whitespace. | `sandbox_devkit.py:34,258-261` |
| `SC_BIND` | `127.0.0.1` | Bind host for the review server and `sc preview`. A non-loopback value is **refused** unless the process is genuinely inside a container; the engine sets `0.0.0.0` there itself. | `server.py:5920-5951,6027` · `preview.py:34,360` |
| `SC_NO_AUTOPRUNE` | unset | Any value skips the boot-time worktree and merged-branch autoprune. | `run.py:2500-2502` |
| `SC_NO_COLOR` | unset | Any value disables ANSI styling, exactly like `NO_COLOR`. | `style.py:5,24` |
| `SC_DEVKIT_OUTPUT` | `compact` | `full` restores unbounded hook output instead of the bounded envelope. For `test`/`lint`/`typecheck` any other value exits `64`. | `devkit.py:37,824-831` |
| `SC_USER` | unset | Operator username used when a boot or `./sc verify` has no TTY to prompt on. Without it a headless boot aborts. | `run.py:1155-1169` |
| `SC_ADMIN` | unset | `1` clears the serialize guard so `snapshot` and `render` may run. The engine sets it for its own admin subprocesses; an operator uses it for a host-side `SC_ADMIN=1 ./sc snapshot`. | `_serialize_guard.py:10-32` · `init_fork.py:14,181` |
| `SC_MEM_AS` | unset | On a host Admin seat, picks one runtime memory credential by shortname when several exist. Ambiguity otherwise refuses. | `mem.py:25-31,210-229` |
| `SC_MEM_CREDENTIAL_FILE` | unset | Owner-only credential artifact `sc mem` validates and reads instead of an injected token. | `mem.py:21-23,240-243` |
| `SC_GH_TOKEN` | unset | GitHub token candidate for push and PR creation. `GH_TOKEN` is accepted as an equivalent; a host `gh` login is the usual path. | `github_auth.py:37,542` · `server.py:2287,2494` |
| `SC_ARTIFACT_MODE` | unset | Only `local` is accepted; any other value refuses. Generated artifacts are local-only, so this exists to reject a stale expectation, not to change behaviour. | `artifact_policy.py:49-55` |

> [!class2]
> **Ports are not an environment knob.** The review port and `dev_port` are derived per repo and persisted in `.super-coder/instance.json`, which you can hand-edit. `SC_DEV_PORT` carries the derived value into a session; it does not configure it.

## Engine-set context

The launcher exports these before it execs the harness. A fork's scripts, git
hooks and dev-kit hooks may **read** them; setting them yourself has no
supported meaning and can misdescribe the seat.

| Variable | Value | Meaning |
|---|---|---|
| `SC_SANDBOX` | `1` inside the Docker sandbox, unset on the host | The seat discriminator every broker, launcher and report keys on. Setting it on a bare host does not create a container and does not grant a publish mapping. |
| `SC_DEV_PORT` | this fork's derived dev port | Bind a dev server to `0.0.0.0:$SC_DEV_PORT` inside the sandbox; the host reaches it at `127.0.0.1:$SC_DEV_PORT`. On the host runtime, bind loopback. |
| `SC_SHELL_ID` · `SC_SHELL_SHORTNAME` · `SC_SHELL_NAME` · `SC_SHELL_FLAVOR` | the booted shell's identity | `SC_SHELL_FLAVOR` is also what exempts `admin` from the branch guard. |
| `SC_SHELL_WORKTREE` | absolute worktree path | The directory the harness is exec'd from; the branch guard compares an edit's target against it. |
| `SC_HARNESS` | `claude` · `codex` · `opencode` · `vibe` · `kimi` | The harness this session runs. |
| `SC_CONVERSATION_SURFACE` | the browser conversation surface | Set only for browser-owned conversations. |
| `SC_API_BASE` · `SC_API_TOKEN` | `http://127.0.0.1:<api port>` and the shell's API key | How `sc mem`, `sc job` and the telemetry hook reach the engine. Identity **is** the token; no command takes a `--shell`. |
| `SC_ENGINE_DIR` · `SC_ROOT` | engine and repo-root paths | Present only for an unrestricted (Admin) view; a restricted shell has both removed. |
| `SC_EXECUTION_VIEW` | `restricted-source` or `restricted-downstream` | Present only in a restricted view, naming which mask set applies. |
| `SC_ENTER_LEASE` | a lease file path | Held open by `./sc enter` so a departed client reads as client-gone rather than busy forever. |
| `SC_DEVKIT_ROOT` · `SC_DEVKIT_SEAT` · `SC_DEVKIT_HOOK` | checkout · `host`/`docker` · hook name | Neutral context handed to every dev-kit hook child. |
| `SC_DEVKIT_REPAIR` | `1` | Set for `./sc enter --devkit-repair`; the boot makes no readiness claim in that seat. |

Evidence: `run.py:2088-2104,2696-2738`, `conversation_launch.py:137-149`,
`execution_view.py:48-54,175`, `dispatch.sh:306-329,1507-1508,1574`,
`devkit.py:816-823`, `branch-guard.sh:52,87`, `shell_liveness.py:56,270-277`.

## Build contracts

`SC_BASE_IMAGE` is not a knob — it is the **contract between a fork's sandbox
extension Dockerfile and the engine's image chain**. The engine builds its own
proven baseline first, then builds your extension `FROM` that exact image, and
passes the tag in as a build argument. Your Dockerfile must therefore be
written to receive it:

```dockerfile
ARG SC_BASE_IMAGE
FROM ${SC_BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends your-tool \
 && rm -rf /var/lib/apt/lists/*
```

Three rules are validated before any build runs, and each has its own refusal:

| Rule | Refusal |
|---|---|
| `ARG SC_BASE_IMAGE` must be declared | `must declare ARG SC_BASE_IMAGE before its final FROM` |
| That `ARG` must be **global** — before the first `FROM` | `ARG SC_BASE_IMAGE must be global (before the first FROM)` |
| The **final** stage must be `FROM ${SC_BASE_IMAGE}` | `final stage must use FROM ${SC_BASE_IMAGE}` |

A `--platform=` prefix and an `AS <name>` suffix are allowed on that final
`FROM`; anything else is not. Earlier stages may use any base you like.
Evidence: `sandbox_devkit.py:193-221`; the tag is passed at
`:1381` (extension) and `:800,838` (package layers).

The engine's own baseline image takes these build arguments. They are supplied
by the launcher and are not fork-facing, but they appear in `docker history`
and in build logs:

| Build argument | Supplied from |
|---|---|
| `SC_USER` · `SC_UID` · `SC_GID` | the invoking host user |
| `SC_HARNESS_EPOCH` | the rolled harness cache key (see [harness freshness](README.md#keeping-harnesses-and-therefore-models-current)) |
| `SC_PARENT_IMAGE` | the operator knob above |
| `SC_GITHUB_HOST_TRUST_B64` · `SC_GITHUB_HOST_TRUST_SHA256` | the engine's pinned GitHub host-key file |

Evidence: `sandbox_devkit.py:1287-1310`.

## Internal

Engine-private, process-to-process state. Listed so a name found in a log or a
`docker inspect` is identifiable — **not a supported interface**. Setting any
of them is unsupported and several will simply break the run.

| Variable | What it is |
|---|---|
| `SC_CALLER_ROOT` | One-hop projection of the invoking checkout; `dispatch.sh` unsets it immediately so it never nests. |
| `SC_DISPATCH` | Explicit dispatcher path for the `./sc` shim. |
| `SC_PYTHON_EXECUTABLE` · `SC_PYTHON_RUNTIME` | Results of the interpreter probe, exported alongside the resolved `SC_PYTHON`. |
| `SC_PLATFORM_KERNEL` · `SC_RESTART_FAILED` | Shell-local variables inside `dispatch.sh`, not exported configuration. |
| `SC_UPDATE_TARGET_REF` | The ref an in-flight `./sc update` is materializing, read by the compatibility check. |
| `SC_OPENCODE_ENFORCED_MODEL` | JSON route the OpenCode plugin enforces; set only for an interactive host Admin boot and popped otherwise. |
| `SC_BROWSER_GUARD_CONFIG` · `SC_BROWSER_READY_FD` | Launch-guard config path and readiness file descriptor for the managed browser. |
| `SC_ADOPT_PASSWORD` | Transient credential passed to the guest during `./sc vm adopt`. |
| `SC_MEM_CRED_DIR` | Override for the runtime memory-credential directory. |
| `SC_HARNESS_EPOCH_FILE` | Test-only override for where the rolled harness epoch is stored. |
| `SC_GITHUB_AUTH_ARGS` · `SC_DEVKIT_MOUNTS` | **Not environment variables.** Literal placeholder tokens in the `docker run` argument list, replaced with the resolved auth and volume arguments before the command runs. Each must appear exactly once. |

Evidence: `dispatch.sh:5-21,1506,1524` · `sc:54-59` · `update_compat.py:50` ·
`run.py:2713-2717` · `browser.py:561,567` · `vm_adopt.py:533-542` ·
`mem.py:106` · `install.py:383-384` · `sandbox_devkit.py:18,63,1764-1769`.

## Non-`SC_` variables

The engine also honours a small set of standard names.

| Variable | Effect |
|---|---|
| `NO_COLOR` | Same as `SC_NO_COLOR` — disables ANSI styling. |
| `GH_TOKEN` | Accepted wherever `SC_GH_TOKEN` is, for push and PR creation. |
| `MISTRAL_API_KEY` | Forwarded into the sandbox when set on the host, for Vibe's key-based path. |
| `DATABASE_URL` | Forwarded into the sandbox for the fork's **app** when the `pg` sidecar is configured; the boot report shows it as `configured (URL withheld)`. |
| `RENDER_ONLY` | `1` renders a boot document and exits without exec'ing a harness and without mutating state. Used by `./sc verify`. |
| `IS_SANDBOX` | Read alongside `SC_SANDBOX` when resolving the global pointer. |

Evidence: `style.py:24` · `server.py:2287` · `dispatch.sh:1412,1424` ·
`run.py:166-172,1818-1850,2290` · `verify.py:40,139` ·
`global_pointer.py:187`.
