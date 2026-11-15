---
title: Browser trust boundary
tags: [subfloor, browser, security]
date: 2026-11-11
project: subfloor
purpose: Current browser authority and reach
---

# Browser trust boundary

## Applicability

**Posture: current architecture reference.** Applies to the Review GUI, Chats
and the Sprint Board, and to every `/api/*` route of the review server.

The review GUI requires sign-in (Feature #94, spec #271). This supersedes two
earlier postures: "no browser sessions" (PR #684), under which a request with
no `Authorization` header was treated as the operator, and "sessions are never
persisted" (PR #576). Neither is true any more.

Source owners are `api/browser_gate.py` (the gate), `api/auth_sessions.py`
(recognising a session), `api/server.py`, `api/conversation_routes.py` and
`api/review_routes.py`. Signing in, the credential store, the session cookie
and the `./sc auth` verbs are the host auth authority's and are described in
[auth.md](auth.md). The user workflow is in
[Browser conversations](../../docs/README.md#browser-conversations).

## What sign-in protects, and what it does not

It protects against:

- a browser, person, or device that has not signed in;
- a page on another origin, including another page on this machine's loopback;
- a shell or a local script calling `/api/*` without a credential. Before
  sign-in that caller was treated as the operator.

It does **not** protect against:

- **code running as the operator's own user on the host.** It can read the
  credential store, and it can change the code the operator runs.
- **a shell inside the docker sandbox reading or changing the review server it
  shares a container with.** The sandbox holds the review server, the engine
  database, and a writable checkout. A shell there can change what the review
  server does, with or without a session, and can read and write the engine
  database directly. The browser sends the session cookie to the review server
  on every request, so a review server that has been changed from inside the
  sandbox can see a live session token. Sign-in does not defend against agents
  on the operator's own box. Making the engine read-only inside the sandbox is
  a separate, later feature.

**Engine operations keep a stronger line.** Update, restart and harness update
are run by the engine-ops service, on the host. It checks the session itself,
against state the sandbox cannot read, and never trusts the hash list the
sandbox can see. The line there is the session requirement, not the network:

- on the default docker network the sandbox has no route to the host's
  loopback, so it cannot reach the service's port at all;
- with `SC_NET=host` the sandbox shares the host's network and **can** reach
  that port. It still cannot start an engine operation or read the status,
  because it has no session and nothing to sign in with: what it is given is
  hashes only.

See [engine-ops.md](engine-ops.md) and [auth.md](auth.md#limits).

## The gate

One decision, made before any route runs. `server.dispatch_http` calls
`browser_gate.authorize` first; the conversation event stream, which does not
pass through `dispatch_http`, makes the same session check when it opens and
again at every turn of its loop. It is default deny: a route answers only for
one of these reasons.

| Reason | What it takes |
|---|---|
| Open | the method and path are on the open list below |
| Session | this install's session cookie, verified, and the origin rule below |
| Shell API | the path is under `/_sc/`; those routes keep their own Bearer rules, unchanged |
| CLI credential | a Bearer credential, on one of the two CLI routes below |

Anything else is refused. **No `Authorization` header proves nothing.**

### The open list

Exact methods and paths, in one constant (`browser_gate.OPEN_ROUTES`):

| Method + path | Why it is open |
|---|---|
| `GET /api/health` | liveness for `./sc launch`, `./sc update`, `./sc health`, `./sc snapshot` |
| `GET /api/auth/status` | what the sign-in page needs before anyone is signed in |
| `GET /signin.html`, `/signin.js`, `/signin.css`, `/theme.js`, `/style.css` | the sign-in page and its files |
| `GET` and `HEAD /vendor/qrcode.js` | the QR encoder the first sign-in draws with |

Everything else that is static, `/`, `/index.html`, `/app.js` and the rest of
`/vendor/` included, needs a session. No favicon is served.

### What a visitor gets

| Request without a session | Answer |
|---|---|
| anything under `/api/` | `401` with `{"error": {"code": "sign_in_required", "message": …, "details": {"sign_in": "/signin.html", "auth_status": "/api/auth/status"}}}` — never a redirect |
| `GET` for a page: `/`, a `.html` path, or a last path segment with no extension | `302` to `/signin.html?next=<path and query>`; `next` is left off for `/` |
| any other method or static file | the same `401` |

Whether a `GET` is a page is decided from the path alone, not from headers the
client chooses. `next` is only ever a same-origin relative path: one leading
`/`, not `//`, no backslash, no control character, no scheme or host, at most
2048 characters, and not the sign-in page itself. Anything else becomes `/`.
`GET /api/auth/status?next=<value>` applies the same rule and returns the
result as `next`, so the page never has to judge a URL itself. A URL fragment
never reaches the server; the browser carries it across the redirect.

### `GET /api/auth/status`

Open. No secret. The database is read only to confirm that a verified
session's user is still an active engine user.

    {"enrolled": true,
     "signed_in": true,
     "user_id": 1,
     "authority": {"url": "http://127.0.0.1:8838", "up": true,
                   "up_source": "probe", "start_cli": "./sc launch"},
     "sign_in": "/signin.html",
     "setup_cli": "./sc auth user add <username>",
     "next": "/"}

- `enrolled`: a GUI user exists. While it is `false` the GUI shows only the
  instruction to run `./sc auth user add <username>`; the gate stays closed.
  There is no grace period.
- `user_id` is present only when signed in.
- `authority.url` is the host auth authority (the engine-ops service) the page
  posts credentials to. It is built for the hostname the page was loaded from,
  `127.0.0.1` or `localhost`, because the cookie is bound to that exact host.
  `up` comes from the same discovery as `GET /api/engine-ops`: a probe on the
  host, the service's published hint inside the sandbox (`up_source`). `url`
  is `null` until the install has allocated the service's port.
- A signed-in caller also has its cookie renewed (below).
- The route is open, so a cross-origin caller is answered, but as a stranger:
  `signed_in` is `false`, there is no `user_id`, and nothing is renewed,
  whatever cookie its browser attached. "Signed in" here is the gate's own
  answer for a `GET` under `/api/`: the operator's session on a request that
  is not cross-origin. The GUI's own same-origin call is unaffected.

### The origin rule

Cookies and `SameSite` both ignore ports, so every page on this machine's
loopback is "same site" and the browser sends it the session cookie. The
request's own headers are what tell the GUI from another local page.

| Request with a session | Must also |
|---|---|
| any mutation (not `GET` or `HEAD`) | carry an `Origin` equal to the origin it was sent to (`http://<Host>`). No `Origin` is not the GUI. `Sec-Fetch-Site`, when present, must be `same-origin` or `none` |
| `GET` under `/api/`, the event stream included | not be cross-origin: an `Origin` that is present must be this one, and `Sec-Fetch-Site`, when present, must be `same-origin` or `none`. A same-origin `GET` carries no `Origin`, so one is not demanded |
| `GET` for a page or a static file | nothing more. Following a link to the GUI is a cross-site navigation, and the files are the checkout's own source |

A failure is `403 same_origin_required` (`403 NOT_SAME_ORIGIN` on the event
stream). This matches the authority, which counts a cookie only together with
the pinned GUI origin.

A cookie is not counted when `Host` is anything but `127.0.0.1`, `localhost`
or `::1`, and never beside an `Authorization` header: a request that names a
Bearer credential is judged as that credential.

The session's user must be an active engine user (`users.is_active`). A
session whose user was deactivated is not signed in. "The cookie verifies and
its user is active" is one decision (`browser_gate.operator_session`), made by
the gate, by the status route, at the routes, and at every turn of an open
event stream.

### Revocation, and an open page

The review server re-reads the published hash list whenever the file changes,
so a revoked, signed-out or reset session is refused from the next request.

An open event stream makes that same decision again before each batch of
events and after each wait, so a revoked session or a deactivated user ends
it at its next event or its next heartbeat (15 seconds), whichever comes
first. If the check cannot run, the stream ends. It ends with a named event,
then closes:

    event: sign_in_required
    data: {"error": {"code": "sign_in_required", …}}

The reconnect an `EventSource` makes then gets the `401`, which closes it for
good. A `fetch` gets the `401` body above.

### Cookie renewal

The engine never expires a session. A browser drops a cookie after about 400
days, so the cookie is re-issued with a fresh lifetime as it is used. The
authority does this on a signed-in `GET /auth/status`. The review server does
it too, on a signed-in `GET /` or `/index.html` (including a `304`) and on a
signed-in `GET /api/auth/status`, so that renewal does not depend on the
authority being up or on the page calling it. The review server re-issues the
token the request carried, under the published cookie name, with the
authority's own spelling of the cookie (`auth_sessions.set_cookie`). It writes
no state and mints nothing.

Only the GUI's own request is renewed. A request that says it came from
another origin (an `Origin` that is not this one, or a `Sec-Fetch-Site` other
than `same-origin` or `none`) is never answered with a `Set-Cookie`: the page
is still served to a signed-in browser that followed a link from another
local page, and the status route still answers, but neither renews.

## Shell and CLI authority

`/_sc/*` is the shell surface. Its Bearer rules are unchanged: a shell token
on every route, Planner flavor where a route says so. A browser session is not
a shell.

Two `/api/*` requests are made by `sc` verbs and accept a Bearer credential
(`browser_gate.CLI_ROUTES`). They are exact, to the query string:

| Request | Caller | What it presents |
|---|---|---|
| `POST /api/scripts/snapshot`, no query | `./sc snapshot`, `./sc update` | the engine CLI credential |
| `GET /api/models?refresh=1`, exactly that query | `./sc models refresh` | the calling shell's own token |

`GET /api/models` with no query, with another value, or with any parameter
added or repeated is the GUI's request, and a credential is refused there
like anywhere else.

Either request accepts either credential:

- **a shell token** (`shells.api_key`), the one the shell already holds;
- **the engine CLI credential.** The review server mints a random token at
  every boot and keeps it in memory. Once it holds its port, and not before,
  it writes the token to `.super-coder/run/api/cli-token.<port>` (directory
  `0700`, file `0600`), under the gitignored `run/`, keyed by the port it
  bound: the port its socket actually holds, and each of them when a
  dual-stack bind on port 0 gives the two families different ports. A second server of the same checkout on another port publishes its
  own file and leaves this one alone, and one that fails to bind publishes
  nothing, so the file for a port names the token of the server listening on
  that port. Nothing deletes a file, at startup or at shutdown: another
  port's may belong to a live server, and a token with no server behind it
  opens nothing. A host verb that has no shell identity reads the file for
  the port it is about to call; if the server there asks for a credential and
  none is published for that port, the verb says so (no server of this
  checkout on that port, or not this checkout's server). On docker the server
  runs in the container on the same port number the host publishes and
  resolves, and `run/` is in the bind-mounted checkout, so the host verb
  finds the file where the container wrote it. The server
  compares against the value in memory, so writing the file grants nothing,
  and a restart retires the old token. It is worth exactly what a shell token
  is worth on those two requests; it exists so that `./sc snapshot` does not
  depend on a shell existing.

Neither makes its holder the operator. On every other `/api/*` route a Bearer
credential the engine issued is refused with `403 fnb_operator_required`; one
it did not issue gets the `401`. Engine operations and user management are not
on the review server at all: they are the host authority's, and it refuses a
Bearer header outright.

`./sc token` is retired. It printed an Admin shell token as a "browser sign-in
token", from a paste-a-token flow that PR #684 removed; sign-in is now a
password and an authenticator code, and no token a CLI can print is a browser
session.

Work that runs inside the server process needs no HTTP authority and is
unchanged: Sprint participant chats, the conversation broker, and the reaper.

**Host-side tools get a session from the host, not from this server.** The
boundary: only a host-side CLI caller can obtain an operator session without
the password and the authenticator, by running
`./sc auth sessions mint --label <text>` in the install's main checkout
([auth.md](auth.md#tool-sessions)). That caller is the host OS user, who can
already read the credential store and reset any user, so the verb grants
nothing new. It refuses inside the sandbox, where the store is absent. No
route here or on the engine-ops service mints one, and the gate has no second
path for it: a tool session is an ordinary published hash, decided by the
same `browser_gate.operator_session` as a browser's, held to the same origin
rule, listed in `./sc auth sessions` with its label and the kind `tool`, and
revoked like any other without touching the operator's browser sessions.

The source-maintainer tools under `maintainer/` (`dos_app_sprint_canary.py`,
`capture_public_docs.py`) play the operator this way, through
`maintainer/tool_session.py`. The gate each one faces is that of the
disposable install it drives, not this checkout's, so the session is minted
by that install's own `./sc`. Such an install has no GUI user; the tool runs
its `./sc auth user add <engine user>` first (the canary always, the capture
with `--enrol-user`). The session is labelled with the tool's name and revoked
when the tool ends, including on failure, on Ctrl-C, and when acquiring it is
interrupted after the mint. A kill while the mint verb itself runs, or one
that runs no cleanup, can leave the session listed under that label for
manual revoke ([auth.md](auth.md#tool-sessions)). The cookie is held in
memory only: never printed, logged, or written to a receipt or gallery. When
no session can be had (no user and no leave to create one, the store not
reachable, a checkout that is not the one serving the port) the tool stops
with one message before it creates anything.

The Sprint Board distinguishes browser-operator actions from
shell-authenticated Sprint commands. Shell commands use scoped Bearer identity
and role and lifecycle checks. A browser session does not replace the required
operator merge grant or a Developer's live merge-authorization proof.

## Network boundary

On the host, server startup refuses a non-loopback bind unless it observes a
container context. Setting `SC_SANDBOX` alone is insufficient. In a container,
the server can bind its container interface; `sc launch` publishes the port on
host `127.0.0.1`.

Container detection does not verify the host's publish mapping or the
container's network namespace. Use the supported launcher. Sign-in removes one
obstacle to serving the GUI beyond loopback; it does not make that supported.
The session cookie is not `Secure`, there is no TLS, and a cookie is not
counted on any `Host` but the three local names. Remote access still needs a
separately authenticated transport.

The server supplies a restrictive Content Security Policy on both pages
(`index.html`, `signin.html`) and serves its static UI locally. `connect-src`
names this server's own socket origins and the authority's two origins on
this machine, port-exact, and nothing else; scripts must be same-origin, with
no inline script. Same-origin script execution can exercise a signed-in
operator's authority; content sanitization and avoiding credential exposure
remain necessary. The session cookie is `HttpOnly`, so page script cannot read
it.

## Conversation data and ownership

The browser receives normalized conversation projections and events. Native
harness session/run references and harness credentials remain server-side.
Queued user messages are durable before dispatch; adapters start or resume the
recorded native session, and the broker records the terminal result.

A conversation belongs to the user the session belongs to. CLI and browser
sessions cannot own the same shell concurrently. Close waits for terminal
proof before releasing ownership. Reopening an eligible ordinary chat requires
route and ownership validation; Sprint-scoped chats remain closed. Browser
refresh reads the bounded transcript and event stream rather than creating
another native session.

## Private engine state

The live DB, snapshot and backups use the private XDG instance namespace.
Ordinary shells operate through API-backed surfaces; engine paths are not
advertised to them and engine SQL is Admin-only (decision #427). Admin owns
direct maintenance and recovery. See the
[engine state reference](../README.md#source-dependency-and-private-state).

GUI credentials and session records live in host-private state that no sandbox
mount reaches; the sandbox is given the session-hash list, read-only, and
nothing else of the authority's ([auth.md](auth.md#where-credentials-live)).
Nothing secret is written to the engine database, the checkout, `.sc-state/`
or `run/`. The engine CLI credential under `run/api/` is not a secret in that
sense: it opens two routes that any shell token opens.

The host service runs as the installing user. Rootless Docker maps container
root to that user; rootful Docker access has the host privilege implications
of Docker itself. Optional host brokers retain their own credentials and
configured scope. None of this promises isolation from a hostile host owner.
