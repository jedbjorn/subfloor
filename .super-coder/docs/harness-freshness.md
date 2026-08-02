---
title: Harness freshness — keeping shells on current CLIs and models
tags: [super-coder, sandbox, harness, docker, models, runbook]
date: 2026-07-26
project: super-coder
purpose: Why a shell cannot reach a model that exists, and the commands that fix it — the harness epoch, what rolls it, and what a restart does
---

# Harness freshness — keeping shells on current CLIs and models

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/.super-coder/docs/harness-freshness.md)

## You are probably here because

A shell cannot select a model that exists — a new Opus, a new GPT — and the
obvious commands did not fix it. That symptom is almost never the model, the
account, or the picker. It is the **harness CLI version inside the sandbox**.

Vendors ship new models in new CLI releases. Claude Code 2.1.218 had no idea
Opus 5 existed; 2.1.219 did. A sandbox baked with 2.1.218 offers an `opus` alias
that cannot resolve to Opus 5, and nothing in the picker says why — the picker
shows aliases, never the build behind them.

## Where harnesses actually live

**Harness executables belong to the sandbox image, never the host mount.** This
is the fact every wrong guess about this system comes from.

`./sc launch` bind-mounts harness state homes — `~/.claude`, `~/.claude.json`,
`~/.codex`, `~/.vibe`, `~/.kimi-code`, opencode's two dirs — so authentication,
sessions, goals, plugins, and other durable state survive container replacement.
The launchers must still resolve image-owned executables from
`.super-coder/Dockerfile`.

Three reasons it must stay that way, each of which has already bitten someone:

- **A darwin binary is fatal in a linux container.** On a macOS host, mounting
  the host's install shadows the baked one with a Mach-O binary the container
  cannot execute. The Dockerfile installs kimi to `/usr/local` for exactly this
  reason — its native home, `~/.kimi-code`, is a mounted cred dir.
- **vibe is not relocatable.** `~/.local/bin/vibe` is a script whose shebang is
  an absolute path into a host uv-managed interpreter. Mounting the binary
  without that interpreter tree gives you a dangling shebang.
- **libc baselines differ.** We support Arch, Ubuntu and macOS hosts against a
  Debian container. Auth is portable JSON; native binaries are not.
- **State homes can grow packages.** Codex's installer now stores its standalone
  package under `~/.codex/packages`, inside the state tree we mount. The
  Dockerfile copies the installed executable to image-owned
  `~/.local/libexec/codex` and repoints its launcher there, so an older host
  package cannot mask a newer image package.

The cost of baking is that the CLIs are only as new as the image, and docker
serves those installer layers from cache indefinitely. Hence the epoch.

## The harness epoch

`SC_HARNESS_EPOCH` is the **cache key on the image's harness layers**. It is
referenced inside both harness `RUN` instructions, so changing it re-runs the
vendor installers (which resolve "latest" themselves — the epoch is an expiry,
never a version pin). It sits below the node/playwright/pip layers, so rolling
it costs the harness downloads and nothing else.

| | |
|---|---|
| Stored at | `${XDG_CONFIG_HOME:-~/.config}/super-coder/harness-epoch` |
| Scope | **The machine**, not the repo — every fork shares the `super-coder-sandbox` image tag |
| Value | A unique UTC token for every explicit refresh |
| Unrolled | `0` — the Dockerfile default, so an untouched machine builds exactly what it always did |
| Recorded in the image | `LABEL sc.harness_epoch`, which is how `harness-status` knows whether a build is owed |

## Commands

| Command | What it does |
|---|---|
| `./sc harness-status` / `make dos-harness-status` | Versions **inside the sandbox** + whether the image owes a rebuild |
| `./sc restart` / `make dos-r` | Roll a fresh epoch, rebuild the image, then bounce into it |
| `./sc restart --no-build` | Deliberately reuse the existing image; no refresh and no build |
| `./sc update-harnesses` / `make dos-update-harnesses` | Roll and rebuild without bouncing; activate that exact build with `restart --no-build` |
| `./sc build --harnesses` | Same staged refresh without the rest of `update-harnesses`' output |
| `./sc build` | Ordinary rebuild — passes the **stored** epoch, so it stays cache-warm |
| `./sc update` / `make dos-u` | Marks harness layers stale as part of the update; the normal restart refreshes and activates them |

## Routine cadence

Nothing to remember. A normal restart is the convergence boundary: it asks each
official installer for current, finishes the image build, and only then tears
down the running sandbox.

```
make dos-u                 # update the engine floor
make dos-r                 # fresh harnesses + safe bounce
make dos-harness-status    # confirm
```

To stage the potentially slow/networked build while the old sandbox keeps
running, use `make dos-update-harnesses`, then activate that exact image later
with `./sc restart --no-build`.

## Runbook: a shell cannot reach a model that exists

**1. Ask what the sandbox is actually running.**

```
./sc harness-status
```

This probes inside the container, which is the only answer that matters. Your
host's own `claude --version` is irrelevant on the docker path — nothing mounts
it in.

**2. Confirm the CLI is the cause, not the catalogue.** Grep the binary for the
model id. Zero hits is proof; it cannot offer what it has never heard of.

```
docker exec sc-<repo> sh -c 'grep -c "claude-opus-5" "$(readlink -f "$(command -v claude)")"'
```

**3. Refresh and bounce.**

```
make dos-r                  # refreshes harnesses, builds, then safely bounces
make dos-harness-status     # verify the new build is what shells got
```

**4. If the CLI is current and the model still is not offered**, it is no longer
a freshness problem — look at the model catalogue (`./sc models list claude`,
`./sc models refresh`) and then at account entitlements. `model_routes` marks
CLI-probed routes `available` and models.dev-sourced ones `advisory`.

## Gotchas

- **A rebuilt image is not a refreshed sandbox.** Running containers keep the
  image they started with until a bounce. After a staged
  `update-harnesses`/`build --harnesses`, use `restart --no-build` to activate
  exactly that image without downloading the harnesses again.
- **Restart discards in-container installs.** `launch`/`restart` do
  `docker rm -f`, destroying the writable layer. Installing a harness inside a
  running container "works" and then evaporates at the next bounce — which is
  exactly how a fixed sandbox appears to un-fix itself. Let normal restart
  rebuild the image instead.
- **Normal restart needs the network.** It refreshes all official installers
  before teardown. If that build fails, the healthy container remains running
  and the stored epoch records that a build is still owed. Use `--no-build`
  only when deliberately pinning/reusing the existing image.
- **`update-harnesses` without docker does something different, on purpose.**
  There the host *is* the runtime, so it runs the vendor installers against
  `$HOME` as it always did.
- **An unlabelled image means unknown, not current.** Images built before this
  seam carry no `sc.harness_epoch`, and `harness-status` reports a rebuild owed
  rather than assuming the best.
- **A fork needs the engine change before any of this exists.** Forks get it
  through their own `./sc update`; that same update rolls their epoch.

## Several forks on one machine

They share the image tag, so harnesses are installed **once for all of them** —
there is no per-container installation. Every normal restart deliberately
requests a fresh image-wide harness build; all later containers use that shared
image until another explicit refresh changes it.

To see the fleet's drift at a glance:

```
for c in $(docker ps --format '{{.Names}}' | grep '^sc-'); do
  printf '%-18s ' "$c"; docker exec "$c" claude --version 2>/dev/null | head -1
done
```

Containers running an older image report older CLIs until each is restarted.
That is expected, not a fault — restarts are per-fork and kill live sessions, so
they stay an operator decision.

## Why the regression was invisible

Worth keeping in mind when judging a future report. Before `harness-status`
existed, **nothing anywhere printed the harness CLI version**. The picker and the
model catalogue show aliases (`opus`, `sonnet`); the launch banner showed ports.
A sandbox one release behind a new model looked identical to a current one, and
three separate commands (`update`, `update-harnesses`, `restart`) each reported
success while changing nothing a shell could see. Normal restart now expires
the installer layers; `./sc launch` names the Claude build in its banner, and
`harness-status` answers the rest.
