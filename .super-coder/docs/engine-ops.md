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

It is also the **host auth authority** (Feature #94, spec #271): it signs the
operator in to the review GUI and keeps the credentials and sessions, and it
runs an engine operation only for a signed-in browser. Sign-in is described in
[auth.md](auth.md); this page covers the service and the engine operations.

## Why a separate host service

The three verbs replace or bounce the engine, and they are host commands:

- **docker runtime.** The review API runs inside the sandbox container. It has
  no docker client and no route to the host, so it cannot run verbs that stop,
  remove, rebuild, and start its own container.
- **host runtime.** The review API is on the host, but `update` and `restart`
  stop it. A verb run as its child is stopped with it.

So one small host process owns the verbs under both runtimes, and the review
API only tells the GUI where that process is.

## Boundary: host loopback TCP, and state the sandbox cannot write

The other brokers listen on a unix socket inside the bind-mounted engine
directory, so that a sandboxed shell can reach them. That is exactly what this
service must not allow: every sandboxed shell can open that directory, and
these verbs replace the engine.

**Transport.** The service listens on its own host loopback TCP port
(`127.0.0.1` only, never configurable). `./sc launch` publishes the review port
and the dev port into the container and nothing else, so on the launcher's own
network the sandbox has no route to it. The browser, which is on the host,
calls it directly.

**Reaching the port is not enough.** That isolation is the launcher's default,
not a guarantee: a sandbox launched on the host's network (`SC_NET=host`)
shares the host's loopback and can connect, and it can write any `Host` and
`Origin` header it likes. So the service does not rely on it. `GET /status`
and every `POST /ops/*` need a signed-in session, checked against the private
state below, and the sandbox only ever sees hashes of session tokens
([auth.md](auth.md#where-credentials-live)). Such a request without the
session is `401 sign_in_required` and starts nothing.

**State.** On the docker runtime the whole checkout is bind-mounted writable
into the sandbox, so `instance.json`, `run/`, and `logs/` are all
sandbox-writable. Nothing the service decides from lives there. Its state is
in a host-private directory outside the bind mount:

    <XDG state home>/subfloor/engine-ops/<repo name>-<hash of the checkout path>/

mode `0700`, owned by the host user, named from the checkout's path and from
no file inside it.

It must not **overlap** anything mounted into the sandbox, with links resolved
and in both directions: the directory inside a mount, a mount inside the
directory, or a mounted file that is one of its files (a `~/.claude.json` that
is a link to its `config.json`). Read-only mounts count; a read still leaks the
pin, the status, and the logs. This is enforced in two places:

- **At the launch**, in `sandbox_devkit.docker_run`, the one place that sees
  the final `docker run` argument list. Every bind source in it (`-v`,
  `--volume`, `--mount type=bind`, however it was built: the checkout, the
  instance state, an interpreter mount, the home mounts, anything added later)
  is resolved and compared with the directory. An overlap stops the launch
  with a message naming both paths. It needs no list of mounts to be kept in
  step, and it does not need the directory to exist yet.
  The guard is a strict grammar for one caller, the launcher, not an
  emulation of Docker's command line:
  - It walks the **option region only**, up to the image. Everything from the
    image on is the container's command and is never looked at.
  - It knows where the image is from a fixed table of the options the launcher
    passes and whether each takes a value (`sandbox_devkit.RUN_OPTIONS`:
    `-d`, `--init`, `--name`, `--restart`, `--network`, `--user`, `-e`, `-w`,
    `-p`, `--tmpfs`, `--memory`, `--memory-swap`, `-v`, `--mount`).
  - An option that is not in the table, in exactly that spelling, stops the
    launch with a message naming it. Joined `--opt=value`, attached values
    (`-vSRC:DST`), short-flag clusters (`-dv`), `--volume`, `--volumes-from`
    and `--` are all outside the table, and so is any option Docker gains
    later. A launcher change that needs a new option adds it to the table; a
    test fails until it does.
  - Mount values are read the way Docker reads them. `--mount`: the whole
    value is trimmed, then read as one CSV record with case-insensitive keys
    and type, `src` for `source`, and `volume` when no type is given. `-v`: a
    source starting with `/` or `.` is a path (relative ones against the
    launch directory), a volume name is a named volume, a lone absolute path
    is an anonymous volume. A value that fits none of that is refused: a
    malformed record, a bind with no, an empty, or a padded source, a mount
    with no target.

- **In the service**, for what it can know without a launch: the checkout,
  the instance state collection, the interpreter mount, and the home mounts
  (`sandbox_devkit.HOME_BIND_MOUNTS`, the list the launcher's arguments are
  built from). On an overlap the service refuses to start and no `engine-ops*`
  verb proceeds; a clear result is re-checked every few seconds, and if the
  list cannot be read nothing is trusted. The error
names the offending path. It holds:

| File | What |
|---|---|
| `config.json` | the pinned `{port, gui_port, pinned_at}` |
| `auth.json`, `auth.lock` | the sign-in credential store and its lock ([auth.md](auth.md#where-credentials-live)) |
| `service.json` | the running service's pid, kernel start time, and port |
| `service.log` | the service's own output |
| `engine-op.json` | the current or last operation |
| `engine-op-<op>.log` | that operation's output (overwritten per run) |

The service never opens a path taken from a record: an operation's log is the
fixed file for its operation name, in that directory. Files are opened with
`O_NOFOLLOW`, and written by exclusive create plus rename.

Two things are published *into* the checkout for the review API, which may be
inside the sandbox: `engine_ops_port` in `instance.json`, and a small hint
file, `.super-coder/run/engine-ops.json`. The service writes them (the hint
with no symlink followed at any level) and never reads them back, so editing
them changes only what the review API reports. That includes `up`: a forged
hint can make discovery say the service is up when it is not. Discovery's
answer is for display only and is trusted for nothing; `GET <url>/status` on
the service is the authority.

On the host runtime shells already run on the host, so neither the port nor
the private directory is a boundary against them there. That matches the
existing posture: a host shell holds host access.

**What this does not cover.** The verbs, the runner, and the service itself
are code in the checkout, which on docker the sandbox can write. That is the
same exposure as the operator typing `./sc restart`, and as the sibling
brokers `./sc launch` starts from the same directory. On docker the GUI is
also served from inside the sandbox, so the GUI origin is not a boundary
against the sandbox either; it is one against other web origins.

### Request checks

| Check | Refusal |
|---|---|
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` for this service's port | `403 loopback_host_required` |
| No `Authorization` header at all. A shell Bearer token is refused; the browser operator carries none | `403 fnb_operator_required` |
| `POST` and `OPTIONS`: `Origin` is present and is the pinned GUI origin (`http://127.0.0.1:<pinned port>` or `http://localhost:<pinned port>`) | `403 same_origin_required` |
| A GUI origin has been pinned (see below) | `POST`: `409 gui_origin_unpinned`; `GET /status`: `200 {pinned: false, reason, pin_cli, setup_cli}` |
| `GET`: `Origin` may be absent; a present `Origin` must be the GUI origin | `403 same_origin_required` |
| `Sec-Fetch-Site` is not `cross-site` | `403 same_origin_required` |
| `GET /status`, `POST /ops/*`, and the session routes under `/auth/`: a valid session cookie, on a request whose `Origin` is the pinned GUI origin | `401 sign_in_required` |

The checks run in that order, so a foreign origin is refused before any
cookie is looked at, and a request with no session learns nothing: not the
status, and not whether an operation name exists.

CORS headers (`Access-Control-Allow-Origin: <the GUI origin>`, `Vary: Origin`)
are sent only when the request's `Origin` is the GUI origin; no wildcard.
`Access-Control-Allow-Credentials: true` is added for the pinned GUI origin
and never for the answers any loopback page may read while unpinned. A
preflight from the GUI origin gets `204` with `GET, POST, OPTIONS` and
`Content-Type`; from anywhere else, `403` with no CORS headers.

## The GUI origin is pinned by the operator

The service accepts operations from exactly one origin, and it never takes
that origin from the checkout: on docker the sandbox can write `instance.json`
and could name its own dev server as "the GUI". It is pinned by an explicit
act in a host terminal. Creating the GUI user is that act, so an install gets
its pin and its first user in one step:

    ./sc auth user add <username>             # shows what instance.json suggests,
                                              # asks you to TYPE the review GUI's
                                              # port, then creates the user
    ./sc auth user add <username> --port 8837 # the same, non-interactively

and the origin can be confirmed again by itself, after a review-port change:

    ./sc auth origin           # asks you to type the review GUI's port
    ./sc auth origin 8837      # the same act, non-interactively
    ./sc auth origin --show    # what is pinned now (exit 1 if nothing is)

`./sc engine-ops-pin` is `./sc auth origin` under its first name and still
works.

At a terminal the suggestion is displayed with a warning about where it came
from; typing anything but a port pins nothing (in `auth user add`, Enter keeps
a port that is already pinned). Without a terminal the port must be given.
The pin takes effect immediately, with no service restart, and `./sc launch` /
`./sc engine-ops-up` print what is pinned every time.

**Until a pin exists** — which is the state of every install after the update
that introduces this service — the service runs but starts nothing:

- `GET /status` answers `200 {ok, service, pinned: false, gui_origin: null,
  pin_cli: "./sc auth origin", setup_cli: "./sc auth user add <username>",
  reason, running: false, operation: null}`.
- every `POST`, under `/ops/` and under `/auth/`, answers
  `409 gui_origin_unpinned` with the same reason. Nobody can sign in.
- those answers carry no state, so any loopback page may read them (without
  credentials); that is what lets the GUI show the reason and the command.

Being answered is not being authorised. Whether an operation starts, and
whether the full status is returned, is decided in one place
(`engine_ops.start`, `engine_ops.status`, both through
`engine_ops.origin_allowed`) against the pin as it is at that moment, and
then against the session: both functions ask the caller's `signed_in` after
the origin decision and refuse when it is missing or says no. A request
that arrived before the pin and reaches that point after it is judged against
the pin like any other: a foreign origin gets `403 same_origin_required` and
starts nothing.

Once pinned, `GET /status` carries `pinned: true` and `gui_origin`.

After a deliberate review-port change, pin again.

## Port and discovery

The service's own port is pinned in `config.json` the first time it is needed
and is not re-read from `instance.json` afterwards. It is derived the way the
review port is: a hash of the repository path into the 8800 band, stepped past
this fork's other ports and every sibling fork's. `instance.json` only tells
that walk which ports to step past.

    ./sc engine-ops-port      # print the port, pinning on first use

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
published hint names this port (`state_file`); a service killed without
cleanup can leave that file behind. `unallocated` means no port has been
published yet (`./sc launch` does it). The browser's own `GET <url>/status` is
the authority either way.

The review server's CSP `connect-src` names `http://127.0.0.1:<port>` and
`http://localhost:<port>` for this one port. It is computed when the review
server starts, which is why `./sc launch` pins the port first.

The review API runs none of the verbs and has no route that does.

## Routes

On the service, all JSON:

| Method + path | What it does |
|---|---|
| `GET /health` | `{ok, service: "engine-ops"}` — liveness of the service. No session needed |
| `GET /auth/status` and the other `/auth/*` routes | sign-in, first sign-in, sign-out, devices: [auth.md](auth.md#routes). `GET /auth/status` needs no session and is how the GUI learns whether to show the sign-in page |
| `GET /status` | **session.** `{ok, service, pinned, gui_origin, pin_cli, runtime, ops, recovery_cli, running, operation, log_tail, log_truncated, log_tail_limit, engine_ref, live_sessions}`; before a pin, only `{ok, service, pinned: false, gui_origin, pin_cli, setup_cli, reason, running, operation}` |
| `POST /ops/update` · `/ops/restart` · `/ops/update-harnesses` | **session.** Start one operation, detached. `202 {started, operation}` |
| `OPTIONS *` | CORS preflight |

`GET /status` and `POST /ops/*` answer `401 sign_in_required` (with
`details.auth_status: "/auth/status"`) without a valid session. With one,
`POST` answers `404 unknown_engine_op` (with the list of operations) for
anything else under `/ops/`, `409 gui_origin_unpinned` before a pin,
`409 engine_op_running` with the running `operation` when one is in flight,
and `500 engine_op_start_failed` when the runner could not be started.

A caller sends the session cookie by making the request with credentials
(`fetch(url, {credentials: "include"})`); nothing else about the two routes
changed. No fresh authenticator code is asked for an operation: a signed-in
session and the GUI's typed confirmation are enough.

**The verb table is fixed.** The path segment selects a row; for `/ops/*` the
request body and query string are never read, so no request field reaches a
command line:

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
`running`, `finished`, or `interrupted`; only the first two block a new
`POST`. `log_tail` is the last 16 KiB of its log.

`runtime` (and so the wording fields), `engine_ref`, and `live_sessions` are
read from the checkout and the process table, so they are advisory: they word
the GUI and decide nothing. `runtime` is one of two values; `engine_ref` is one
short line, read without following a link and shown only if it looks like a
ref; `live_sessions` is `{count, browser, indeterminate}` from the OS liveness
scan (`scripts/shell_liveness.py`), or `null` where that scan is unavailable.

## An operation outlives the engine it stops

`./sc restart` and `./sc down` stop the review server and the brokers, this
service among them; `./sc update` stops the review server. The service is
**not** exempted from that stop. Instead the operation runs detached from it:

- The verb runs under a small runner (`api/engine_ops.py run …`) started in its
  **own session**, with stdio on a log file rather than inherited pipes.
  Stopping the service signals one proven pid, which a process in another
  session does not receive.
- Where a systemd user manager answers, the runner is also placed in its **own
  transient scope** (`systemd-run --user --scope`). Stopping a systemd unit
  kills every process left in the unit's control group, new session or not,
  and the host review server always is such a unit. `operation.detach` reports
  `systemd-scope` or `session`.
- The runner owns the status file, replaced atomically, so the result is
  readable when the engine returns, by whichever service instance is then
  running.

**One at a time.** An operation is running while anything it started is
still doing its work, whatever the status file says:

- the runner, or the command it started, each checked by pid and kernel start
  time; and
- any other live process in the runner's session, and, when the runner has its
  own systemd scope, in that scope. So a command killed after it spawned a
  build or an install still holds the gate until that work ends, even though
  the runner has by then recorded `finished` (the operation reads `running`
  until it is truly over).

Three things in that session or scope are not the operation's work and do not
hold the gate: zombies; the runner itself once it has recorded the result; and
the long-lived services a restart's `launch` step starts on purpose — the vm,
ts, pm2, and db brokers and this service — together with everything those
services run. A service is recognised by its exact identity (a Python
interpreter running that script of this install); its work is any process
with such a service among its ancestors. A broker serves requests for as long
as the engine is up, some of them from the sandbox, so a worker it spawns must
not be able to hold the gate. A record from before a reboot never blocks, and
neither does a session id that has since been reused by another process.

Known limits:

- Without a systemd scope, a process that deliberately leaves the session (a
  daemon that calls `setsid`) is no longer seen. With a scope it is.
- A broker's worker that outlives its broker (the broker is killed mid-request)
  is re-parented and no longer has a service ancestor, so it holds the gate
  until it exits. A sandbox cannot kill a host broker; the next operation after
  that worker ends is unaffected.

`restart` brings the service back itself (its `launch` step starts it), so
after a restart the GUI is talking to a new service process reading the same
status file. A failed `update` leaves the review server down by design; the
service stays up and reports the failure, and `./sc launch` recovers.

A client should expect its connection to drop after a `202` for `restart`,
poll the review API's `/api/health` until it answers, re-read
`/api/engine-ops`, then read `/status`.

## Running it (on the HOST — never in the sandbox)

    ./sc engine-ops          # foreground
    ./sc engine-ops-up       # background; no-op if already up
    ./sc engine-ops-down     # stop the backgrounded service
    ./sc engine-ops-port     # print its port
    ./sc auth origin         # pin the GUI origin it accepts (see above;
                             # `./sc engine-ops-pin` is the same verb)

`./sc launch` brings it up under both runtimes and `./sc down` stops it.
`./sc restart` reports it in its health summary as an advisory line only: a
service that does not come back never changes the restart's exit code, the
same tolerance `launch` gives it.

`engine-ops-down` stops only a process **proven** to be this service: the pid
recorded in the private directory must be alive, have the recorded kernel
start time, and be a Python interpreter whose script argument resolves to this
install's `api/engine_ops_service.py`. A process that merely has that name
somewhere on its command line, or runs a same-named file elsewhere, is not it.
A pid written anywhere in the checkout is never read.

The proof and the signal use one process handle (a pidfd), so the pid cannot
be reused between them. If no handle can be had, nothing is signalled: the
verb reports that the service could not be stopped safely and names the
manual step. There is no signal-by-number fallback.
`./sc engine-ops-down` then exits non-zero. Inside `./sc down` and
`./sc restart` the same message is printed on stderr and the verb carries on
with its own exit code unchanged; the service is left running.

It refuses to start when `SC_SANDBOX` is set or a container marker is present,
and every `engine-ops*` and `auth` verb refuses from a linked worktree, which
has no instance of its own.

It has no systemd install form. After a reboot, `./sc launch` starts it.

## Limits

- One operation at a time, per install.
- The service keeps running the code it started with across `./sc update`,
  like the other brokers; `./sc restart` loads the new code.
- When the service is not running the GUI cannot start it: the controls render
  disabled and show the CLI command.
- Nothing works from the GUI until the operator has run
  `./sc auth user add <username>` once and signed in. If the review port
  changes, the pin does not follow by itself; run `./sc auth origin`.
- A browser on another machine cannot reach it. The GUI is loopback-only and
  so is this.
