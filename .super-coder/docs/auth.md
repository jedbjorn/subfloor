# GUI sign-in: the host auth authority

**Posture: current operator runbook.** Applies to every install. The authority
is the [engine-ops service](engine-ops.md), which `./sc launch` starts; it
needs no configuration block. Exact CLI syntax remains in `sc help --all`.

The review GUI asks for a password and an authenticator code (Feature #94,
spec #271). This page covers the host side of that: where credentials live,
the routes the sign-in page calls, the session cookie, the `./sc auth` verbs,
and what the review server reads to recognise a signed-in browser. The review
server's own gate and the sign-in page are described with those parts.

## What sign-in protects, and what it does not

It protects against:

- a browser, person, or device that has not signed in;
- a page on another origin, including another page on this machine's loopback;
- a process that can reach the engine-ops port and writes the GUI's `Origin`
  header itself. Engine operations need the session, not just the header.

It does **not** protect against:

- **code running as the operator's own user on the host.** It can read the
  credential store, and it can change the code the operator runs.
- **a shell inside the docker sandbox reading or changing the review server it
  shares a container with.** The sandbox holds the review server, the engine
  database, and a writable checkout. A shell there can change what the review
  server does, with or without a session. That includes the pages it serves
  and the requests it receives: the browser sends the session cookie to the
  review server on every request (cookies ignore ports), so a review server
  that has been changed from inside the sandbox can see a live session token.
  What the sandbox is given at rest is hashes only; what a changed review
  server can do while the operator is signed in is not something sign-in
  defends against. Making the engine read-only inside the sandbox is a
  separate, later feature.

Engine operations keep a stronger line than the rest of the GUI: the service
that runs them is on the host, checks the session itself against state the
sandbox cannot read, and never trusts the list the sandbox can see.

## Where credentials live

Two directories under the owner's state home
(`$XDG_STATE_HOME`, default `~/.local/state`), both named from the checkout's
path and from no file inside it (`sandbox_devkit.install_key`):

| Directory | Mode | In the sandbox | Holds |
|---|---|---|---|
| `subfloor/engine-ops/<repo>-<hash>/` | `0700` | never | the engine-ops private state, with `auth.json` (`0600`) and `auth.lock` |
| `subfloor/auth-sessions/<repo>-<hash>/` | `0700` | read-only, at `/run/super-coder/auth-sessions` | `sessions.json` (`0600`) and nothing else |

`auth.json` is the credential store:

- **users**, a list keyed by the engine's `users.user_id`: `username`, `state`
  (`awaiting` first sign-in, or `active`), the scrypt password hash with its
  parameters and salt, the TOTP secret, the last accepted TOTP step, and the
  scrypt hash of the one-time claim code;
- **sessions**: an id, the user, the SHA-256 of the session token, a device
  label, created and last-seen times. The token itself is never stored. A
  [tool session](#tool-sessions) also carries `kind: "tool"` and its label; a
  record with no `kind` is a browser's;
- **rate**: the failed-attempt count and the time the next attempt is allowed.

Nothing secret is written to the engine database, the checkout, `.sc-state/`,
or `run/`. The unused `users.password_*` columns stay unused.

Writers are the service process and the `./sc auth` verbs, through one module
(`api/host_auth.py`) and one lock file. The store is read from disk for every
decision, so a `./sc auth user reset` reaches a running service at once.

`sessions.json` is what the review server verifies against:

    {"version": 1,
     "cookie": "sc_session_1a2b3c4d",
     "enrolled": true,
     "sessions": [{"hash": "<sha256 of a session token>", "user_id": 1}],
     "updated_at": "2026-01-01T00:00:00Z"}

It holds no username and no secret. A hash lets a reader recognise a token and
gives it nothing to sign in with: presenting a hash matches nothing, because
what is compared is the hash of what was presented.

### Two files, one rule

**A session exists only while its hash is in both files.** The review server
reads the published list; the service reads the store and, on every read,
drops any session the published list does not carry. A change is two or three
atomic writes, always in this order:

1. sessions being removed leave the published list;
2. the store is saved;
3. sessions being added enter the published list.

Stopped anywhere, by a failed write or a crash, it fails closed:

| Stopped | Removed session | New session |
|---|---|---|
| before write 1 lands | nothing changed; the command or request reports the failure | nothing changed |
| after 1, before 2 | gone for both servers (the list no longer carries it) | not saved |
| after 2, before 3 | gone for both | in the store only: accepted by neither, and never handed out |

Each write is flushed to disk, file and directory entry, before the next
begins, so the table holds across a power loss as well as a crash.

A service start settles the two files by the same rule and so cannot bring a
session back: a hash missing from the list is dropped from the store, never
put back into the list. The consequence to know about: if `sessions.json` is
deleted or damaged, every session ends and each browser signs in again. Users,
passwords and authenticators are not affected.

This covers a single revoke, sign-out, `./sc auth sessions revoke`,
`./sc auth sessions mint`, and `./sc auth user reset`. A reset interrupted after write 1 has already ended
every session and has not yet cleared the password; run it again.

### The launcher's mount

On the docker runtime the launcher (`sandbox_devkit.docker_run`) creates the
`auth-sessions` directory, owner-only, and adds one mount:

    --mount type=bind,src=<that directory>,dst=/run/super-coder/auth-sessions,readonly

The launch guard (`sandbox_devkit.guard_engine_ops_state`) holds both
directories to their rules on the final argument list:

- no mount may overlap the private directory, in either direction;
- the `auth-sessions` directory may be reached by exactly one argument: the
  launcher's own, character for character, once. Any other mount that reaches
  it stops the launch: a writable one, a parent (the state home, the whole
  home), a file in it, another target, a second copy, and also another
  spelling that Docker would read as the same read-only mount (`-v …:ro`,
  `source=`/`target=`, a different field order). Nothing rests on reading a
  mount value the way Docker does. A sandbox that could write the list could
  sign itself in to the review API;
- a mount value says each thing once. Docker reads `--mount` fields in order,
  lets a later field override an earlier one, and takes several names for one
  field (`source`/`src`; `target`/`dst`/`destination`; `readonly`/`ro`). A
  value that repeats a field, gives two names for one, or names its type
  twice is refused, wherever it points, and a bind may carry only the fields
  the launcher writes (type, source, target, read-only). `-v` is `SRC:DST`
  with at most one of `ro` or `rw`;
- the two directories may not resolve into each other.

### The verifier's call

`api/auth_sessions.py` is the read side, stdlib only, and never writes:

    session = auth_sessions.session_from_cookie_header(headers.get("Cookie"))
    # {"user_id": 1, "hash": "..."} when the request proves a session, else None

It reads the cookie name from the published file, takes the cookie of that
name, and compares the SHA-256 of its value with every published hash, each in
constant time. On the host it reads the derived directory; inside the sandbox,
the fixed mount. A missing, unreadable, or malformed file means nobody is
signed in. `auth_sessions.enrolled()` says whether a GUI user exists, and
`auth_sessions.set_cookie(name, token)` is the one spelling of the cookie.

## The checks

- **Password**: scrypt (`hashlib.scrypt`), N=2^15, r=8, p=1, 32-byte key, a
  16-byte salt per hash. Each stored hash carries its parameters, so the cost
  can be raised later without invalidating anything. 8 to 256 characters.
- **Authenticator code**: RFC 6238 TOTP, SHA-1, 30-second steps, six digits.
  The current step and one either side are accepted. A step is accepted once:
  a code at or before the last accepted step is refused, inside its window or
  not. The secret is 160 random bits, shown once, at first sign-in.
- **Both are evaluated before anything is answered**, against the user's
  record, or against throwaway values when there is none to compare with
  (unknown user, user awaiting first sign-in). Every refusal is the same
  `401 invalid_credentials` with the same message.
- **Clock drift beyond one step** fails like a wrong code; the message says to
  check the device's clock.

### Failed attempts

One counter per install, in the store, so it survives a service restart. It
counts wrong passwords, wrong codes, and wrong claim codes alike.

- The first 3 failures cost nothing.
- Each further failure doubles the wait before the next attempt is looked at:
  1 s, 2 s, 4 s, … up to 300 s.
- While waiting, every sign-in and first-sign-in attempt is
  `429 rate_limited` with `Retry-After`, without looking at what was sent.
  Correct details are refused too. A refused attempt does not lengthen the
  wait.
- A successful sign-in clears the counter. So does an hour with no failure,
  and so does `./sc auth unlock` on the host.

Existing sessions are not affected by the wait. A process on this machine that
keeps failing on purpose can therefore keep a *new* sign-in waiting, up to the
ceiling each time, and can do nothing to a browser that is already signed in.
`./sc auth unlock` clears the wait; if the failures do not stop, stop what is
making them first (for a sandbox on the host network, `./sc down`), then
unlock and sign in.

## The session cookie

    Set-Cookie: sc_session_<8 hex>=<token>; Max-Age=34560000; Path=/; HttpOnly; SameSite=Strict

- The token is 256 random bits. Only its SHA-256 is stored.
- The name carries a per-install suffix. Cookies are scoped by host and not by
  port, so two installs on one machine would otherwise overwrite each other's.
- No `Domain`: it is bound to the exact host the browser used (`127.0.0.1` or
  `localhost`). Because cookies ignore ports, the review API and this service
  both receive it.
- **`Secure` is not set.** The GUI is served over loopback http, and browsers
  do not agree on `Secure` cookies there. A deployment over TLS must add it;
  that is a later feature.
- **The engine never expires a session.** Browsers keep a cookie for about 400
  days at most, so a signed-in `GET /auth/status` answers with the same cookie
  and a fresh `Max-Age`. A browser unused for longer than its own limit signs
  in again. `last_seen` is written at most every 10 minutes.
- **A copied cookie** works in another browser until it is revoked, and shows
  in the device list as the same device.

A cookie is honoured only on a request whose `Origin` is the pinned GUI
origin. `SameSite` ignores ports just as cookies do, so any page on this
machine's loopback is "same site" and the browser would send it the cookie;
the `Origin` is what tells the GUI from another local page. A request with no
`Origin` is not signed in, whatever cookie it carries.

## Routes

On the engine-ops service, with its existing checks (loopback `Host`, no
`Authorization` header, the GUI `Origin`): see
[engine-ops.md](engine-ops.md#request-checks). CORS allows credentials for the
pinned GUI origin only. Every `POST` under `/auth/` takes a JSON object as
`application/json`, at most 8 KiB.

| Method + path | Needs | Request | Answer |
|---|---|---|---|
| `GET /auth/status` | — | — | `200 {ok, service, pinned, gui_origin, enrolled, signed_in, user, session, retry_after, setup_cli}`; `reason` too while unpinned. Signed in: `Set-Cookie` renews the cookie |
| `POST /auth/identify` | — | `{username}` | `200 {first_sign_in}`: true only for a user awaiting first sign-in |
| `POST /auth/sign-in` | — | `{username, password, code, device?}` | `200 {signed_in, user, session}` + `Set-Cookie` |
| `POST /auth/first/begin` | — | `{username, claim_code}` | `200 {enrolment, username, secret, otpauth_uri, issuer, algorithm, digits, period, expires_in, password_min}` |
| `POST /auth/first/confirm` | — | `{enrolment, password, code, device?}` | `200 {signed_in, user, session}` + `Set-Cookie` |
| `POST /auth/sign-out` | — | `{}` | `200 {signed_in: false}` + a cookie that clears it; the same answer with or without a session |
| `GET /auth/sessions` | session | — | `200 {sessions: [{id, device, created_at, last_seen_at, current}]}`, most recently seen first |
| `POST /auth/sessions/revoke` | session | `{id}` | `200 {revoked, current}`; clears the cookie when it was this device |
| `GET /status`, `POST /ops/<op>` | session | — | see [engine-ops.md](engine-ops.md#routes) |

`user` is `{user_id, username}`; `session` is
`{id, device, created_at, last_seen_at, current}`.

| Status + code | When |
|---|---|
| `401 sign_in_required` | a route that needs a session, without a valid one from the GUI origin |
| `401 invalid_credentials` | any failed sign-in |
| `401 invalid_claim` | first sign-in: wrong or used claim code, unknown user, or a user who is not awaiting |
| `401 invalid_code` | first sign-in: the confirming code does not match |
| `400 password_unacceptable` | first sign-in: password outside 8 to 256 characters |
| `409 enrolment_expired` | first sign-in: no such enrolment any more (15 minutes, a service restart, a reset, or it was completed) |
| `404 session_not_found` | revoke: not one of this user's sessions |
| `429 rate_limited` | too many failures; `Retry-After` and `details.retry_after` say how long |
| `409 gui_origin_unpinned` | any `POST` before a GUI user was created |
| `415 json_required` · `400 bad_request` | the body is not a small JSON object |

Revocation, sign-out, and reset hold from the next request: the service reads
its store for every request, and the review server re-reads the published
list whenever it changes.

## First sign-in

The CLI creates the user; the GUI completes it.

1. **`./sc auth user add <username>`** on the host. The username must be an
   active engine user (`users.username`, matched without regard to case); the
   record is keyed by that row's `user_id`. The verb reads the engine database
   directly and read-only, so it needs no running review server. It sets no
   password and no TOTP secret, marks the user `awaiting`, and prints a
   one-time claim code (`XXXX-XXXX`), stored only as a scrypt hash.
   The same step confirms the GUI origin the host accepts: at a terminal it
   shows what `instance.json` suggests and asks you to type the review GUI's
   port (Enter keeps a port that is already pinned); without a terminal, pass
   `--port <port>`. Nothing is created if the origin is not confirmed.
2. **The sign-in page** asks for the username. `POST /auth/identify` says
   whether that user is awaiting first sign-in. It is the same answer for an
   unknown username and a completed user.
3. **`POST /auth/first/begin`** with the username and the claim code. The
   service creates the TOTP secret and returns it, once, with its
   `otpauth://` URI (for the QR code and as the manual key).
   **Nothing is saved**: the secret stays in the service's memory, and the
   claim code is not used up yet.
4. **`POST /auth/first/confirm`** with the chosen password and one code from
   the authenticator just linked. Only now are the password hash and the
   secret saved, the claim code used up, and the first session issued. The
   confirming code's step is spent; it does not also sign in.

A completed user cannot be claimed again: the claim is gone, and the same
username gets the normal form. A wrong claim code is refused and counted like
a wrong password. An enrolment lasts 15 minutes and does not survive a service
restart; the claim code still works afterwards.

## Tool sessions

A host-side tool that plays the operator over HTTP, or drives the GUI in a
browser, gets its session from a host CLI verb, not from the sign-in page:

    ./sc auth sessions mint --label <text> [--user <username>] [--json]

- **What it makes.** An ordinary session, through the same code as a sign-in
  (`host_auth._new_session`, then the three ordered writes above), with two
  more fields in the store: `kind: "tool"` and the label. The published list
  is unchanged in shape: a hash and a `user_id`. The review server's gate has
  one decision function (`browser_gate.operator_session`) and it treats this
  session exactly as a browser's: a published hash of an active engine user,
  on a loopback `Host`, with a same-origin `Origin` on every mutation.
- **No password and no code.** The verb runs as the host OS user, who can
  already read the store and reset any user with `./sc auth user reset`. It
  grants nothing that user lacks. It is a CLI verb only: no route of the
  service or of the review server mints a session without the password and
  the authenticator, and the verb refuses wherever the other `./sc auth`
  verbs refuse (inside the sandbox, from a linked worktree).
- **What it prints.** The cookie, once, on stdout: `sc_session_<8 hex>=<token>`
  on one line, or with `--json` one object,
  `{id, kind, label, username, user_id, cookie_name, cookie_value}`. A line on
  stderr names the session id and the revoke command, never the cookie.
- **Which user.** `--user`, or the only GUI user when there is one. With no
  GUI user the verb refuses and names `./sc auth user add`. A user still
  `awaiting` first sign-in may be given a tool session (a tool's own
  disposable install has nobody to complete one). The review server accepts
  it; the engine-ops service does not, because its own check wants a
  completed user, so such a session cannot run an engine operation.
- **Label.** 1 to 80 visible characters. It is what the session is listed
  under: `./sc auth sessions` shows it in the device column with `tool` in
  the kind column (`browser` for a signed-in device). The GUI's device list
  shows it by its label among that user's devices.
- **Ending it.** `./sc auth sessions revoke <id>`, `--all`, the GUI's device
  list, or `./sc auth user reset`: the same as any session, and without
  touching the operator's browser sessions when revoked by id. It does not
  expire.

The tool sends the cookie, and `Origin: http://<host>:<port>` of the review
server it calls, as the GUI would. The source-maintainer tools do this through
`maintainer/tool_session.py`, which mints in the install the tool drives,
labels the session with the tool's name, keeps the cookie in memory only, and
revokes on the way out. Once the mint verb has answered, that covers a normal
exit, a failure, Ctrl-C, and an interruption or error while the new session is
still being checked against the server. The remaining gap: a Ctrl-C or kill
that lands while the mint verb itself is running, or a kill of the tool that
runs no cleanup (SIGKILL, SIGTERM), can leave a session nobody holds the id
of. It stays listed under the tool's label; end it with
`./sc auth sessions revoke <id>`.

## The `./sc auth` verbs

Host terminal, main checkout. Refused from a linked worktree (it has no
instance of its own) and inside the sandbox (the state is not there).

| Verb | What it does |
|---|---|
| `./sc auth user add <username> [--port <port>]` | create the GUI user awaiting first sign-in; print the claim code; confirm the GUI origin |
| `./sc auth user reset <username>` | clear the password, the TOTP secret, and every session; back to `awaiting`; print a new claim code. This is the recovery path for a lost authenticator or password |
| `./sc auth user list` | `user_id`, username, state, session count |
| `./sc auth sessions [list]` | every session: id, user, device or tool label, kind (`browser` or `tool`), last seen, since |
| `./sc auth sessions mint --label <text> [--user <username>] [--json]` | give a host-side tool a labelled [tool session](#tool-sessions); print its cookie once |
| `./sc auth sessions revoke <id>` · `--all` | sign one device, or every device, out |
| `./sc auth unlock` | clear the failed-attempt wait |
| `./sc auth origin [<port> \| --show]` | confirm or show the GUI origin by itself (after a review-port change). `./sc engine-ops-pin` is the same verb under its first name |

Exit codes: `0` done, `1` refused or failed (stderr says why, nothing was
changed), `2` usage. `auth origin --show` exits `1` when nothing is pinned.

## Existing installs

After the update that brings sign-in, no GUI user exists: `GET /auth/status`
answers `enrolled: false`, and the GUI shows only the instruction to run
`./sc auth user add <username>`. There is no grace period.

## Limits

- One GUI user per engine user; the engine has one user today. No roles.
- The claim code has no expiry of its own. It is replaced by
  `./sc auth user reset`.
- While the service is down, existing sessions keep working against the
  published list, and nobody new can sign in until `./sc launch` brings it
  back.
- A `--network host` sandbox (`SC_NET=host`) can reach the service's port. It
  cannot start an engine operation or read the status without a session, and
  it has only hashes; it can slow new sign-ins as described under
  [Failed attempts](#failed-attempts).
