# engine-ops service

**Posture: current operator runbook.** Applies to every install; the service
needs no configuration block and is started by `./sc launch`. Host lifecycle
belongs to the install's operator. Exact CLI syntax remains in `sc help --all`
and verb help.


The host-side authority that runs `./sc update`, `./sc restart`, and
`./sc update-harnesses` for the review GUI's engine controls (Feature #93,
spec #270). A sibling of the [VM and remotes broker](remote-seats.md), the
[tailnet broker](tailscale-broker.md), the [pm2 broker](pm2-broker.md), and the
[db broker](db-broker.md): a host process with fixed verbs that refuses to run
in the sandbox. Its boundary is different from theirs, on purpose.

## Why a separate host service

The three verbs replace or bounce the engine, and they are host commands:

- **docker runtime.** The review API runs inside the sandbox container. It has
  no docker client and no route to the host, so it cannot run verbs that stop,
  remove, rebuild, and start its own container.
- **host runtime.** The review API is on the host, but `update` and `restart`
  stop it. A verb run as its child is stopped with it.

So one small host process owns the verbs under both runtimes, and the review
API only tells the GUI where that process is.

## Boundary: host loopback TCP, not a socket in the engine directory

The other brokers listen on a unix socket inside the bind-mounted engine
directory, so that a sandboxed shell can reach them. That is exactly what this
service must not allow: every sandboxed shell can open that directory, and
these verbs replace the engine.

The service therefore listens on **its own host loopback TCP port**
(`127.0.0.1` only, never configurable). `./sc launch` publishes the review port
and the dev port into the container and nothing else, so the sandbox has no
route to it. The browser, which is on the host, calls it directly.

On the host runtime shells already run on the host, so the port is not a
boundary against them there. That matches the existing posture: a host shell
holds host access.

Every request is checked:

| Check | Refusal |
|---|---|
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` for this service's port | `403 loopback_host_required` |
| No `Authorization` header at all. A shell Bearer token is refused; the browser operator carries none | `403 fnb_operator_required` |
| `POST` and `OPTIONS`: `Origin` is present and is this install's GUI origin (`http://127.0.0.1:<review port>` or `http://localhost:<review port>`) | `403 same_origin_required` |
| `GET`: `Origin` may be absent; a present `Origin` must be the GUI origin | `403 same_origin_required` |
| `Sec-Fetch-Site` is not `cross-site` | `403 same_origin_required` |

CORS headers (`Access-Control-Allow-Origin: <the GUI origin>`, `Vary: Origin`)
are sent only when the request's `Origin` is the GUI origin; no wildcard, no
credentials. A preflight from that origin gets `204` with `GET, POST, OPTIONS`
and `Content-Type`; from anywhere else, `403` with no CORS headers.

## Port and discovery

The port is derived the way the review port is: a hash of the repository path
into the 8800 band, bumped past this fork's other ports and every sibling
fork's, then persisted once as `engine_ops_port` in `instance.json`
(`scripts/ports.py`, `ensure_engine_ops_port`). An existing assignment is never
moved while it is clear of the fork's other ports.

    ./sc engine-ops-port      # print it, allocating on first use

The GUI finds the service through the review API:

    GET /api/engine-ops       (browser operator)

    {"port": 8857, "url": "http://127.0.0.1:8857",
     "up": true, "up_source": "probe",
     "runtime": "host",
     "ops": {"update": {"cli": "./sc update", "stops_engine": true,
                        "ends_sessions": "browser", "takes_effect": "now"}, ...},
     "recovery_cli": "./sc launch", "start_cli": "./sc launch"}

`up_source` says how `up` was decided. On the host the port is probed
(`probe`). Inside the sandbox it cannot be, by design, so `up` is whether the
service's state file (`.super-coder/run/engine-ops.json`, written at start,
removed at a clean stop) names this port (`state_file`); a service killed
without cleanup can leave that file behind. `unallocated` means no port has
been assigned yet (`./sc launch` assigns it). The browser's own
`GET <url>/status` is the authority either way.

The review server's CSP `connect-src` names `http://127.0.0.1:<port>` and
`http://localhost:<port>` for this one port. It is computed when the review
server starts, which is why `./sc launch` allocates the port first.

The review API runs none of the verbs and has no route that does.

## Routes

On the service, all JSON:

| Method + path | What it does |
|---|---|
| `GET /health` | `{ok, service: "engine-ops"}` — liveness of the service |
| `GET /status` | `{ok, service, runtime, ops, recovery_cli, running, operation, log_tail, log_truncated, log_tail_limit, engine_ref, live_sessions}` |
| `POST /ops/update` · `/ops/restart` · `/ops/update-harnesses` | start one operation, detached. `202 {started, operation}` |
| `OPTIONS *` | CORS preflight |

`POST` answers `404 unknown_engine_op` (with the list of operations) for
anything else under `/ops/`, `409 engine_op_running` with the running
`operation` when one is in flight, and `500 engine_op_start_failed` when the
runner could not be started.

**The verb table is fixed.** The path segment selects a row; the request body
and query string are never read, so no request field reaches a command line:

| Operation | Command, run from the main checkout |
|---|---|
| `update` | `./sc update` |
| `restart` | `./sc restart -y` |
| `update-harnesses` | `./sc update-harnesses` |

`status.runtime` is `host` or `docker`, and `status.ops[<op>]` carries what the
GUI needs to word a confirmation:

| Field | Values |
|---|---|
| `cli` | the command to type instead |
| `stops_engine` | `true` for `update` and `restart` |
| `ends_sessions` | `all` — docker: the container is stopped or removed and every session in it ends · `browser` — host: the review server stops and the browser turns it owns end; terminal sessions keep running without an API until it returns · `none` |
| `takes_effect` | `now`, or `next_restart` for the docker harness update, which rebuilds the image and leaves the running sandbox on the old one |

`operation` is the current or last one: `{op, state, cli, pid, started_at,
finished_at, exit_code, ok, log, detach, error}`. `state` is `starting`,
`running`, `finished`, or `interrupted` (the runner died without finishing);
only the first two block a new `POST`. `log_tail` is the last 16 KiB of its
log. `live_sessions` is `{count, browser, indeterminate}` from the OS liveness
scan (`scripts/shell_liveness.py`), or `null` where that scan is unavailable.
`engine_ref` is the installed pin, or `null` on an install without one.

## An operation outlives the engine it stops

`./sc restart` and `./sc down` stop the review server and the brokers, this
service among them; `./sc update` stops the review server. The service is
**not** exempted from that stop. Instead the operation runs detached from it:

- The verb runs under a small runner (`api/engine_ops.py run …`) started in its
  **own session**, with stdio on a log file rather than inherited pipes.
  `./sc down` stops a broker by signalling the one pid in its pidfile, which a
  process in another session does not receive.
- Where a systemd user manager answers, the runner is also placed in its **own
  transient scope** (`systemd-run --user --scope`). Stopping a systemd unit
  kills every process left in the unit's control group, new session or not,
  and the host review server always is such a unit. `operation.detach` reports
  `systemd-scope` or `session`.
- The runner owns the **status file**, `.super-coder/logs/engine-op.json`,
  replaced atomically: `op`, `state`, `pid`, `started_at`, `finished_at`,
  `exit_code`, and the log path (`.super-coder/logs/engine-op-<op>.log`,
  overwritten per run). It is on disk, so it is readable when the engine
  returns, by whichever service instance is then running.
- "Running" is the status file plus a liveness check of the recorded pid and
  its start time, so a runner that crashed, or a recycled pid, does not block
  the next operation.

`restart` brings the service back itself (its `launch` step starts it), so
after a restart the GUI is talking to a new service process reading the same
status file. A failed `update` leaves the review server down by design; the
service stays up and reports the failure, and `./sc launch` recovers.

A client should expect its connection to drop after a `202` for `restart`,
poll the review API's `/api/health` until it answers, re-read
`/api/engine-ops`, then read `/status`.

## Running it (on the HOST — never in the sandbox)

    ./sc engine-ops          # foreground
    ./sc engine-ops-up       # background (nohup + pidfile); no-op if already up
    ./sc engine-ops-down     # stop the backgrounded service
    ./sc engine-ops-port     # print its port

`./sc launch` brings it up under both runtimes and `./sc down` stops it;
`./sc restart` reports it in its health summary. It refuses to start when
`SC_SANDBOX` is set or a container marker is present, and every `engine-ops*`
verb refuses from a linked worktree, which has no instance of its own.

It has no systemd install form. After a reboot, `./sc launch` starts it.

## Limits

- One operation at a time, per install.
- The service keeps running the code it started with across `./sc update`,
  like the other brokers; `./sc restart` loads the new code.
- When the service is not running the GUI cannot start it: the controls render
  disabled and show the CLI command.
- A browser on another machine cannot reach it. The GUI is loopback-only and
  so is this.
