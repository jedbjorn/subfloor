# Postgres sidecar

**Posture: current operator runbook.** Applies to a fork that has enabled this
optional sidecar. Lifecycle belongs to the host operator, because it is a
docker container; Planner supplies fork-local shell guidance. Example names and
ports below are illustrative and do not grant access to another fork. Exact CLI
syntax remains in `sc help --all` and verb help.


A per-fork `postgres:17` container on the sandbox network, for developing and
testing the fork's **application** against real Postgres. It is the one entry in
the service registry (`scripts/services.py`) that is a *sidecar* rather than a
host broker, and the only one the sandbox reaches over the container network
instead of a unix socket.

The hard line: **the engine's own memory DB is SQLite and stays SQLite.**
`scripts/db_driver.py` speaks nothing else, and the engine never reads
`DATABASE_URL` — only the fork's app does. That separation is load-bearing: a
sidecar that fed the engine was the #207 regression, and it stays fixed.

## Why a sidecar, not a host Postgres

A fork's shells run inside the sandbox container, which publishes nothing and
has no route to the host's own Postgres. Pointing dev/test at a live app
database would put test writes on live data, which is exactly what the
[read-only DB broker](db-broker.md) exists to avoid. So the two surfaces are
deliberately different things:

- **this sidecar** — an *empty, private, writable* Postgres the fork's app owns
  for dev and test, reachable from the sandbox by container DNS;
- **the db-broker** — `SELECT`-only, table-allowlisted reads of the fork's
  **live** app database, over a host unix socket, with the DSN never leaving the
  host.

A shell needs both for different questions and must never confuse them.

## Link config — the `pg` key

Lives under the `pg` key of `.super-coder/instance.json` (gitignored,
per-instance — the container is a host resource, not shell state). It coexists
with the `vm` / `ts` / `pm2` / `db` blocks; `ports.py` owns `pg` in
`MANAGED_KEYS` and preserves keys it does not own. The block is a **marker, not
a configuration**: `./sc pg-init` and `./sc feature enable pg` both write an
empty object, and every knob is an environment variable or a baked constant.

```json
"pg": {}
```

| Name | Value | Where |
|---|---|---|
| container | `sc-pg-<repo-dir-name>` | `dispatch.sh` `PGNAME` |
| data volume | `sc-pg-<repo-dir-name>-data` → `/var/lib/postgresql/data` | `dispatch.sh` `PGVOL` |
| network | `$SC_NET` (default `sc-net`, shared between forks) | `dispatch.sh` `SC_NET` |
| credentials | `sc` / `sc` / `sc` — sandbox-local, never published to the host | `POSTGRES_USER/PASSWORD/DB` |
| `/dev/shm` | `$SC_PG_SHM`, default `1g` | docker's 64 MB default starves parallel-query DSM and trips a postmaster crash-reinit (#298) |
| restart policy | `--restart unless-stopped` | so the docker daemon brings it back after a host reboot |
| published ports | **none** | reachable only from `$SC_NET`, by container name |

## What a shell sees

`./sc launch` forwards one variable into the sandbox when — and only when — the
`pg` key is present:

```
DATABASE_URL=postgresql://sc:sc@sc-pg-<repo>:5432/sc
```

`SC_DATABASE_URL` on the host overrides the whole value for a fork whose sidecar
differs. The host name is the **container** name: `127.0.0.1` inside the sandbox
is the sandbox's own loopback and reaches nothing. An unset `DATABASE_URL` in a
shell means this fork has no `pg` key — not that the sidecar is down.

## Verbs

| Verb | Does |
|---|---|
| `./sc pg-init` | add `"pg": {}` to `instance.json`; idempotent, prints the next step |
| `./sc feature enable pg` | the same write through the feature front door (`pg` is `block_auto` — it needs no operator-supplied host config) |
| `./sc pg-up` | create the network and the volume, then run the container; self-skips when unconfigured or already alive |
| `./sc pg-down` | `docker rm -f` the container, then **prove** it is gone; the data volume is retained |
| `./sc feature disable pg` | remove the key; a running sidecar keeps running and is stopped with `./sc pg-down` |
| `./sc launch` / `./sc down` | start and stop it as part of the ordinary fork lifecycle |
| `./sc restart` | bounces it and gates on `pg_isready -U sc -d sc` inside the container |

The GUI's Scripts page exposes the same two actions (`up`, `down`) through the
registry; enabling from there runs `pg-init` before `pg-up` in one step.
Lifecycle is refused inside the sandbox — it needs the host's docker — and the
refusal names `./sc pg-up` to run on the host instead.

## Docker runtime vs host runtime

The sidecar is a docker container **in both runtimes**, and it is the one piece
of docker the host runtime keeps:

- **sandbox runtime** — the container joins `$SC_NET` alongside the sandbox, and
  `DATABASE_URL` is forwarded into the sandbox at `docker run` time. Order does
  not matter: the sidecar starts after the sandbox and the app connects lazily.
- **host runtime** — `./sc launch` still starts the sidecar when the `pg` key is
  present, and `./sc down` still stops it. But there is no sandbox to forward
  `DATABASE_URL` into, and the container publishes no host port, so a host-booted
  shell has neither the variable nor a route by default. Treat the sidecar under
  the host runtime as configured-but-unwired unless the operator arranges its own
  access.

## Failures and refusals

| Symptom | Meaning |
|---|---|
| `pg: no pg key in instance.json — skipping` | `pg-up` on an unconfigured fork. Not an error; `./sc pg-init` enables it |
| `pg already running (sc-pg-<repo>)` | `pg-up` is idempotent and did nothing |
| `postgres teardown could not verify removal of '<name>'` (exit 1) | `docker rm -f` did not leave the container absent — usually broken docker access. Fix docker, `./sc pg-down`, then retry `./sc restart`. `down` fails loudly rather than pretend |
| `postgres: failed (unhealthy after restart)` | the container is up but `pg_isready` never answered inside the wait budget; `./sc restart` reports the whole restart as failed |
| `runtime shutdown left container(s): sc-pg-<repo>` | `./sc remove` will not proceed while the sidecar survives its quiesce step |
| sidecar state shows as unknown in the GUI | `docker inspect` was unreachable from where the status was read (the sandbox has no docker socket); it is not a claim that the container is down |
| connections die mid-suite under parallel queries | the `/dev/shm` case (#298). Raise `SC_PG_SHM` and **recreate**: `--shm-size` is fixed at `docker run`, so `./sc pg-down` then `./sc pg-up` |

## Running it (on the HOST — it needs docker)

```
./sc pg-init             add the "pg" key (enables the sidecar)
./sc pg-up               start the postgres:17 container
./sc pg-down             stop + remove it; the data volume is retained
./sc feature             list the opt-in features and each block's state
./sc launch              starts it and forwards DATABASE_URL
./sc down                stops it
```

From inside a sandboxed shell, the sidecar is just a DSN:

```bash
echo $DATABASE_URL                       # unset -> this fork has no `pg` key
psql "$DATABASE_URL" -c 'select 1'       # if the fork's dev kit ships psql
```

## Limits

- **No per-fork tuning in the block.** `pg` is an empty marker; image tag,
  credentials, volume name and network are baked, and `/dev/shm` and the DSN are
  environment overrides (`SC_PG_SHM`, `SC_DATABASE_URL`). Changing `--shm-size`
  means recreating the container.
- **No `sc persist` unit.** The registry entry has no systemd unit; reboot
  survival comes from `--restart unless-stopped` and therefore from the docker
  daemon starting at boot.
- **The data volume outlives the fork.** `pg-down` retains it by design, and
  `./sc remove` does not remove it either: the volume is created without the
  ownership labels that removal's docker cleanup filters on. Drop it by hand
  with `docker volume rm sc-pg-<repo>-data` when you mean it.
- **Not the visual-QA Postgres.** `./sc visual-qa` starts its own disposable
  `subfloor-visual-qa-<pid>` container with a published loopback port and its own
  `DATABASE_URL`; it is unrelated to this sidecar and is removed when the run
  ends.
- **Not a live-data surface.** For the fork's real app database, use the
  [read-only DB broker](db-broker.md).
