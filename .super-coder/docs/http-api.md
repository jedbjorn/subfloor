# http api — the engine server's route surface

**Posture: current architecture reference.** Applies to the single localhost
server in `.super-coder/api/`. It enumerates what the process routes today; it
is not a compatibility promise. Credential classes below are the ones the code
enforces — the reasoning behind them, and the limits of what loopback
authority means, belong to the
[browser trust boundary](interface-trust-boundary.md) and are not restated
here. Exact CLI syntax remains in `sc help --all` and verb help.

> [!NOTE]
> Reading this because the file is missing? A fork pinned at a ref that
> contains this reference can still be short of it on disk — see
> `engine-integrity.md` in this directory (also viewable upstream at
> `jedbjorn/subfloor` under `.super-coder/docs/`) for the detect-and-recover
> procedure.

## Shape of the server

One process, one per-fork port (derived by `scripts/ports.py`), serving JSON,
the static review UI and one SSE stream. There is no web framework: `api/`
stays stdlib. `api/transport.py` runs an asyncio one-port HTTP+WS multiplex and
calls `server.dispatch_http(method, path, headers, body)`, which first puts
the request to the sign-in gate (`browser_gate.authorize`), then forwards
review and conversation paths to their own modules and drives everything else
through a socketless `BaseHTTPRequestHandler` subclass (`_ShimHandler`).
Dispatch inside that handler is an ordered chain of `if path == …` /
`path.startswith(…)` / `len(parts) ==` tests in `do_GET`, `do_POST`,
`do_PATCH`, `do_PUT`, `do_DELETE` — there is no route table to read. The
conversation and review modules are the exception: they match compiled
`re` patterns in a single `handle()`.

`HEAD` is answered only for `/vendor/…`; every other path gets `405`.

### Credential classes

The gate is default deny: `dispatch_http` decides every request before a route
is chosen, and the event stream makes the same session check when it opens and
at every turn of its loop. The rules, and what they do and do not protect, are
in the [browser trust boundary](interface-trust-boundary.md#the-gate).

| Class | How it is presented | Enforced by |
|---|---|---|
| Open | nothing; the method and path are in `browser_gate.OPEN_ROUTES` | `browser_gate.authorize` |
| Session | this install's session cookie, verified against the hash list the host auth authority publishes, on a loopback `Host`, with the origin rule | `browser_gate.authorize`; again at `Handler._require_browser_operator` and `conversation_routes._operator` |
| Shell token | `Authorization: Bearer <shells.api_key>` on `/_sc/*` | `Handler._require_shell_auth` (401 on absent or unknown) |
| Planner shell token | the same, plus `shells.flavor == 'planner'` | `_resolve_planner_shell` (403 otherwise) |
| CLI credential | `Authorization: Bearer` a shell token or the engine CLI token, on the two requests in `browser_gate.CLI_ROUTES`, exact to the query string: `POST /api/scripts/snapshot` with no query and `GET /api/models?refresh=1` | `browser_gate.authorize` |
| Runtime token | `X-SC-Runtime-Token` header | `runtime_flags.token_matches` |

Every `/api/*` route and every static file needs a **session** unless its row
says otherwise. There is no unauthenticated class beyond the open list, and
no `Authorization` header never means the operator.

Refusals at the gate:

| Status + code | When |
|---|---|
| `401 sign_in_required` | no session and no accepted credential. Body: `{"error": {"code": "sign_in_required", "message", "details": {"sign_in": "/signin.html", "auth_status": "/api/auth/status"}}}` |
| `302` to `/signin.html?next=…` | the same, for a `GET` of a page (never under `/api/`) |
| `403 same_origin_required` | a session, but a mutation without this origin's `Origin`, or a cross-origin `GET /api/*` |
| `403 fnb_operator_required` | a shell token or the engine CLI token, off the CLI routes. A credential never makes its holder the operator |
| `503 gate_unavailable` | the check itself could not run (the engine DB was busy while a session's user or a Bearer's shell was looked up). `Retry-After: 2`. The request is refused, never let through undecided |

A presented shell token is an explicit **disqualifier** on the browser's
routes: `/_sc` answers a shell, `/api` answers the signed-in operator. A
request that carries an `Authorization` header is never a session, whatever
cookie it also carries. Conversation and review requests also validate a
loopback `Host` (`403 HOST_NOT_ALLOWED` / `HOST_FORBIDDEN`), and repeat the
same-origin check for their mutations (`403 NOT_SAME_ORIGIN`).

## Stability

The engine's own documentation promises `sc` verbs, not HTTP paths. Derive
your posture from that:

- **Supported for fork tooling — indirectly.** `/_sc/*` is the contract
  `sc mem`, `sc sprint`, `sc context`, `sc search`, `sc skill`, `sc pr` and
  `sc browser` speak, and shell boot injects `SC_API_BASE` + `SC_API_TOKEN` so
  those verbs work from a sandboxed seat. Fork tooling should call the **CLI**,
  which the docs and skills do promise; the paths underneath are an
  implementation of it and are versioned only by the engine's own migrations.
- **Engine-private.** `/api/*` is the Review GUI's private surface (plus the
  brokers' status/validate helpers). `/api/conversations/*`,
  `/api/review-targets/*` and `/api/review-observations/*` are the browser
  chat and Diff review surfaces. Nothing in the repo offers them to fork
  tooling.
- **No stability promise is recorded anywhere in the repo for any HTTP path.**
  Neither `README.md`, `docs/README.md`, `.super-coder/docs/` nor any skill
  states a versioning or deprecation policy for this server. Treat the tables
  below as a description of one engine ref, re-verified per the note at the
  end. The one shipped precedent for removal is `POST /api/publish`, which now
  answers `410 Gone` in place of its old behavior.

Two things that look like API families and are not: `sc vm` and `sc remote`
reach the host **vm-broker over a unix socket**
(`scripts/vm.py`, `scripts/remote.py`) and never touch this server — the
`/api/vm` routes here only read and write the `vm` block in `instance.json`
and run single validation checks for the Interface tab. `sc map-sql` /
`sc map-schema` open the map DB directly with `sqlite3`; the only map route is
the GUI's `/api/map` summary.

## `/_sc/mem` — control-plane memory

Shell token on every route. Base `/_sc/mem`. Normal client: `sc mem`
(`scripts/mem.py`), which is how every shell writes identity, state, flags,
decisions, documents, tasks and messages.

| Method + path | Purpose / key fields |
|---|---|
| `GET /whoami` | resolves the token → `shell_id`, `shortname`, `display_name`, `flavor` |
| `GET /state` | `{current_state}` |
| `GET /seed`, `GET /lns` | law-curated entries: `entry_id`, `kind`, `body`, `entry_date`, `source_tag` |
| `POST /seed`, `POST /lns` | append; caps (10 / 20) and length are enforced server-side, and a rejected write is the feedback |
| `POST /state` | replace the rolling status in place |
| `POST /lns/curated` | stamp the L&S curation sweep |
| `GET /narrative` (`?list=1`, `?prev=1`), `GET /narrative/{archive_id}`, `POST /narrative` | narrative line log. Reads are the caller's own session archives only: default = the active one (`narrative`, `archive_id`, `session_id`, `date`, `lines`, `stub`, `active`, `previous`); `?list=1` = the newest `NARRATIVE_INDEX_CAP` (10) archives without bodies; `?prev=1` = the newest one before the active that is not a `stub` — empty or only its session-start line (as does `previous`); `{archive_id}` = one archive, `404` when it is missing, out of range, or another shell's |
| `GET /decisions` (`?all=1`), `GET /decisions/{id}`, `POST /decisions` | index capped at `DECISIONS_INDEX_CAP` (30) active rows; `--parent` supersession lives in the body |
| `GET /flags` (`?resolved=1&feature=<id>`), `GET /flags/{id}`, `POST /flags`, `PATCH /flags/{id}` | any authenticated shell may edit or resolve any flag; authorship stays on the opening `shell_id`. `404 no such flag`; `409 managed_flag` when `management_state='system'` (runtime advisories clear only on lifecycle evidence) |
| `GET /roadmap`, `POST /roadmap`, `PATCH /roadmap/{feature_id}` | feature rows |
| `GET /projects`, `POST /projects`, `PATCH /projects/{shortname}` | work-streams |
| `GET /documents` (`?feature=`), `GET /documents/{id}` | spec/doc read |
| `POST /docs`, `PATCH /docs/{id}`, `PATCH /docs/{id}/feature`, `PATCH /docs/{id}/freeze` | author, edit-in-place, move a spec to another feature, freeze. A frozen document refuses title and body edits |
| `GET /tasks` (`?doc=<id>` or `?feature=<id>`), `POST /tasks`, `PATCH /tasks/{id}` | `400` when neither selector is given |
| `GET /messages` (`?direction=inbox\|sent`), `POST /messages`, `PATCH /messages/{id}/read` | the shell inbox |
| `GET /shells` | shell roster |
| `GET /delivery-audit` | Planner-only projection; `403 planner_only_delivery_audit` for any other flavor |
| `PATCH /identity-entries/{id}/retire` | retire a seed or L&S entry; `404` if it is not this shell's |
| `POST /telemetry` | harness telemetry ingest (`hooks/telemetry-hook.sh`) |
| `POST /oriented` | mark first-run orientation complete |

## `/_sc/sprint` — Sprints v2

Shell token on every route; authority beyond that is role- and
lifecycle-checked inside `sprint_domain` / `sprint_review_loop`. Normal client:
`sc sprint` (`scripts/sprint_cli.py`), driven by the `sprint_prep`,
`sprint_pln`, `sprint_protocol` skills.

Reads:

| Method + path | Purpose |
|---|---|
| `GET /_sc/sprint/{id}/board` | the whole-Sprint read-only projection, for its own participants |
| `GET /_sc/sprint/{id}/inbox` | this shell's Sprint messages |
| `GET /_sc/sprint/{id}/report?limit=` | `{evidence_packet}` from `sprint_close` |
| `GET /_sc/sprint/{id}/timeline` | the authoritative lifecycle read — terminal `lifecycle.completed` / `lifecycle.aborted` events |
| `GET /_sc/sprint/approvals/{document_id}` | `{approvals}` for a bound spec |
| `GET /_sc/sprint/spec-revisions/{sprint_id}/{document_id}` | the governing revision |
| `GET /_sc/sprint/cleanup-runs/{sprint_id}` | worktree-cleanup recovery status |
| `GET /_sc/sprint/watcher-state?sprint_id=` | PR-watcher state; the query parameter is required exactly once |

Writes — all `POST`, all under `/_sc/sprint/`, body is JSON:

`qaqc` · `declare` · `plan-unit` · `replan-unit` · `recall-unit` ·
`cancel-unit` · `complete-unit` · `resolve-unit` · `arm` · `rebind-spec` ·
`reroute-participant` · `dispatch` · `monitor` · `send` · `inbox-read` ·
`inbox-decline` · `review-request` · `review-record` · `register-pr` ·
`reconcile-pr` · `merge-authorize` · `pause` · `resume` · `complete` ·
`abort` · `conformance` · `cleanup-runs`

Refusals are uniform across the family (`Handler._sprint_error`,
`_sprint_board_mutation_error`): `403 forbidden` for a `SprintAuthorityError`,
`409 lifecycle_conflict` for state, invariant and conflict errors, `404` for an
unmatched path shape, `400` for a malformed integer or list field. Only the
owning Planner may call `dispatch` or `monitor`.

## `/_sc` — the rest of the shell surface

| Method + path | Credential | Purpose / client |
|---|---|---|
| `GET /_sc/context?task=<id>\|work_unit=<id>[&worktree=&seat=&branch=]` | shell | the six-part task/work-unit projection; read-only, no event, no telemetry. `sc context` |
| `GET /_sc/skills` | shell | the shell's skill catalogue; takes no filters (`400` otherwise). `sc skill` |
| `GET /_sc/model-routes?harness=&selector=` | shell | model route catalogue; each filter once and non-empty, else `400` |
| `POST /_sc/search` | shell | `{query, max_results, depth}` → Tavily results; the host-held key never crosses the boundary. Errors are `{error, code}` at the status `web_search` picked. `sc search` |
| `POST /_sc/browser` | shell | `{action: status\|open}` only — setup, lifecycle and arm are the FnB's (`403`). `open` requires the `drive_browser` grant (`403`) and accepts no config overrides (`400`); returns `proxy_url`, `tab_group`, `output_dir`. `sc browser` |
| `POST /_sc/pr/subscribe` | shell | `{repository, pr_number}` → `{subscription_id, created}`; `201` on create, `200` when already subscribed. `sc pr` |
| `POST /_sc/skills/{put\|grant\|revoke\|rm\|retire\|unretire}` | Planner shell | fork-local skill catalogue mutations, enforced server-side against the same rules as the host CLI |
| `PUT /_sc/skills/assign` | Planner shell | `{name, shell\|shells, granted}` — grant or revoke in one verb |
| `PUT /_sc/skills/retire/{name}` | Planner shell | `{retired: bool}` (default `true`) — the same retire/unretire lane and Planner check as the `POST` routes |
| `PUT /_sc/runtime-flags/{source}` | runtime token | system-managed non-blocking advisories; `401` when `X-SC-Runtime-Token` does not match |

## `/api` — the Review GUI

Engine-private. Unless a row says otherwise the credential class is
**session**; the client is the local browser app (`ui/app.js`), tab named in
the last column. Every write additionally needs this origin's `Origin`.

### Reads

| `GET` path | Credential | Purpose / tab |
|---|---|---|
| `/api/health` | open | liveness payload |
| `/api/auth/status?next=` | open | `{enrolled, signed_in, user_id, authority: {url, up, up_source, start_cli}, sign_in, setup_cli, next}`; `user_id` only when signed in. `next` is the query value reduced to a same-origin relative path, or `/`. Renews a signed-in caller's cookie. See the [trust boundary](interface-trust-boundary.md#get-apiauthstatus) |
| `/api/logs` | session | last 20 webapp events, newest first (`LOG_MAX_EVENTS`) |
| `/api/git-state?fetch=1` | session | live git hygiene; `fetch=1` does the network fetch. `#repo` |
| `/api/shells`, `/api/shells/{id}` | session | `{shells, repo_root}`; `404 no such shell`. `#view-shells` |
| `/api/shell-templates`, `/api/flavor-defaults` | session | flavor catalogue and per-flavor model defaults |
| `/api/models?refresh=1` | session; exactly `?refresh=1` also takes a CLI credential | model catalogue. `./sc models refresh` presents the calling shell's token. Without the query, or with any other, a credential is refused |
| `/api/skills`, `/api/skills/{id}` | session | skill catalogue with origin tags |
| `/api/roadmap` | session | `#view-roadmap` |
| `/api/docs` | session | specs and docs with feature/work-stream metadata for `#view-docs` |
| `/api/documents/{id}` | session | full document row |
| `/api/documents/{id}/open` | session | `302` to a rendered-markdown URL |
| `/api/flags` | session | `#view-flags` |
| `/api/map` | session | repo-map summary. `#view-map` |
| `/api/analytics/{sessions,tokens,usage,filters}` | session | `#view-analytics`; all take the shared filter query |
| `/api/analytics/quota` | session | one entry per provider — `{providers:[{provider,status,detail,captured_at,windows}], ttl_seconds, probed, notes}`. Probes only when this process's last probe *attempt* is older than the TTL; never at boot, never on a timer. Carries no operator identity by design |
| `/api/scripts`, `/api/services` | session | `#view-scripts`, including broker/sidecar configured/running/persistent state |
| `/api/vm`, `/api/ts`, `/api/pm2` | session | the stored `instance.json` block |
| `/api/ts/status`, `/api/pm2/status` | session | live tailnet / process view; proxied to the host broker under `SC_SANDBOX`, `503` with the exact `./sc …-broker-up` command when that socket is absent |
| `/api/browser`, `/api/web-search` | session | config + status. The web-search key itself never crosses this boundary |
| `/api/engine-ops` | session | where the host-side engine-ops service is: `{port, url, up, up_source, runtime, ops, recovery_cli, start_cli}`. The service itself, not this API, runs update / restart / harness update — see [engine-ops.md](engine-ops.md) |
| `/api/sprints?lifecycle=&limit=&cursor=` | session | Sprint list. `#view-sprints` |
| `/api/sprints/{id}` | session | the same board projection shells read at `/_sc/sprint/{id}/board` |
| `/api/sprints/{id}/events`, `/api/sprints/{id}/summaries` | session | paged by `limit`, `cursor`, `work_unit_id` |
| `/api/sprints/{id}/spec-revisions/{document_id}` | session | FnB view of the governing revision; `404 spec_revision_not_found`, `409` on `BoundRevisionUnavailable` |

### Writes

| Method + path | Credential | Notes |
|---|---|---|
| `POST /api/flags`, `POST /api/projects`, `POST /api/shells` | session | `201` on create, `400` on validation |
| `POST /api/flavor-defaults` | session | set a flavor's default model route |
| `POST /api/analytics/sweep` | session | recompute analytics |
| `POST /api/analytics/quota/probe` | session | force a probe (the refresh button) |
| `POST /api/snapshot` | session | `{output}` from the snapshot render. `#snapshot` |
| `POST /api/publish` | session | **`410 Gone`** — retired |
| `POST /api/scripts/{key}` | session; `snapshot` also takes a CLI credential | run one registered script; `404 no such script`, `500` on failure. `./sc snapshot` and `./sc update` present the engine CLI credential to `/api/scripts/snapshot` and to no other key |
| `POST /api/services/{key}/{up\|down\|install\|uninstall}` | session | sidecars accept `up`/`down` only; `{init:true}` enables-and-starts the sidecar. `404` unknown pair, `409 not_configured`, `503 host_required` (brokers are host processes) |
| `POST /api/{vm,ts,pm2}/validate/{check}` | session | run one live check against the candidate config in the body, test-before-save. A failed check is `200 {ok:false}`, not an error; `404` unknown check, `503` when the host broker is unreachable |
| `POST /api/browser`, `POST /api/web-search/validate` | session | `{action, config}` link/validate |
| `PATCH /api/shells/{id}` | session | operational fields only — seed and L&S have no write endpoint at all, by law |
| `PATCH /api/flags/{id}`, `PATCH /api/roadmap/{id}`, `PATCH /api/documents/{id}` | session | frozen documents refuse body edits |
| `PATCH /api/sprints/{id}` | session | FnB lifecycle actions from the board |
| `PUT /api/shells/{id}/skills/{skill_id}`, `PUT /api/flavors/{flavor}/skills/{skill_id}` | session | `{granted}`. `409` when the shell inherits from a flavor pack; `500 {ok:false, committed:true}` when the write landed but projection failed |
| `PUT /api/roadmap/{id}/blockers` | session | `{blocked_by:[ids]}` replaces the set; empty clears it |
| `PUT /api/vm`, `PUT /api/ts`, `PUT /api/pm2` | session | persist the block to `instance.json`; no key material |
| `PUT /api/web-search`, `DELETE /api/web-search` | session | set/rotate or clear the stored key |
| `DELETE /api/shells/{id}` | session | soft delete (`is_deleted=1`); `404` if already gone |

## `/api/conversations` — browser chat

Engine-private; `api/conversation_routes.py`. Session on every route,
loopback `Host` required, same-origin required on every mutation and on the
event stream. A conversation belongs to the session's user. Client:
the Chats rail in the GUI. Conversation ids are `cv_` + 32 hex.

| Method + path | Purpose / key fields |
|---|---|
| `GET /api/conversations` | list, paged (`cursor`, `limit` ≤ 200) |
| `POST /api/conversations` | create; body carries the shell, harness and model binding; honors `Idempotency-Key` replay |
| `GET /api/conversations/{cv}` | the conversation projection (state, process, binding) |
| `PATCH /api/conversations/{cv}` | close/reopen and operational fields |
| `GET /api/conversations/{cv}/messages` | message projections |
| `POST /api/conversations/{cv}/messages` | queue a user turn; durable before dispatch; returns its queue position |
| `GET /api/conversations/{cv}/transcript?cursor=` | bounded transcript projection with an explicit truncation record |
| `GET /api/conversations/{cv}/source` | source projection for the chat |
| `POST /api/conversations/{cv}/uploads` | raw image bytes, not JSON — routed before the body parse; extension inferred from content |
| `POST /api/conversations/{cv}/interruptions` | interrupt or replay the live run |
| `POST /api/conversations/{cv}/compactions` | ask the chat's harness to compact its stored session now (the compact button); empty body, `Idempotency-Key` required; `202` with the queued request, `409` when the harness has no trigger, the chat has no session, a turn is in flight or a compaction is already pending |
| `POST /api/conversations/shell-release` | release a shell a CLI or browser session owns |
| `GET /api/conversations/{cv}/events` | **SSE only.** `text/event-stream`, `id:` = event sequence, `?after=`/`Last-Event-ID` resume, `: keepalive` heartbeat. Served by `stream_events` straight off the transport; the ordinary handler answers `406 SSE_REQUIRED`. The session is checked again before each batch and after each wait; when it is gone the stream sends `event: sign_in_required` and closes |

Refusals: `401 sign_in_required` (no session), `401 UNAUTHORIZED` and
`403 OPERATOR_REQUIRED` on the event stream when a Bearer token was presented
(elsewhere the gate answers first, with `401 sign_in_required` or
`403 fnb_operator_required`), `403 NOT_SAME_ORIGIN`, `403 HOST_NOT_ALLOWED`,
`409` for shell-busy and close/reopen conflicts, `422 VALIDATION_ERROR`,
`503 ENGINE_DB_BUSY` with `{retry_after: 2}`, `500 INTERNAL_ERROR`. Event payloads are redacted of harness session refs and
secret-shaped keys before they reach the browser.

## `/api/review-*` — Diff review

Engine-private; `api/review_routes.py`. Session, loopback `Host`,
same-origin. Each path accepts exactly one method and answers `405` with an
`Allow` header otherwise. Target ids are `gt_` + 32 hex; observation ids are a
64-hex digest.

| Method + path | Purpose |
|---|---|
| `GET /api/conversations/{cv}/review-targets?refresh=` | the conversation's review targets |
| `POST /api/conversations/{cv}/review-observations` | snapshot the current worktree; empty body required (`422` otherwise); honors `Idempotency-Key` |
| `GET /api/review-targets/{gt}/files?scope=&cursor=&limit=&status=&path=` | file list |
| `GET /api/review-targets/{gt}/diff?scope=&path=` | diff text |
| `GET /api/review-targets/{gt}/commits?cursor=&limit=` | commit list |
| `GET /api/review-observations/{sha}/patch?file=` | the snapshot's patch |
| `GET /api/review-observations/{sha}/shell-file?file=` | one file at snapshot state |

Git-layer refusals map through `_REVIEW_ERROR_STATUS`:
`404 REVIEW_TARGET_NOT_FOUND`; `409` for `REVIEW_WORKTREE_MISSING`,
`REVIEW_NOT_A_GIT_REPOSITORY`, `REVIEW_REF_MISSING`;
`422 REVIEW_PATH_INVALID`; `503 REVIEW_REMOTE_UNAVAILABLE`;
`503 DATABASE_BUSY`.

## Static and assets

The sign-in page is open: `GET /signin.html`, `/signin.js`, `/signin.css`,
`/theme.js`, `/style.css` and `GET`/`HEAD /vendor/qrcode.js`. Every other
static file needs a session; without one a page (`/`, `/index.html`) answers
`302` to `/signin.html?next=…` and a subresource answers `401`.

`GET /`, `/index.html`, `/app.js`, `/style.css` and the sign-in files from
`.super-coder/ui/`, with
`Cache-Control: no-cache` plus a content-derived `ETag` (so a rebuild that
produces identical bytes still revalidates cheaply, and an updated `app.js`
never survives an engine update). `index.html` and `signin.html` additionally
carry the restrictive CSP, and a signed-in `GET` of `index.html` re-issues the
session cookie with a fresh lifetime. `GET`/`HEAD /vendor/…` resolves per request against
`ui/vendor/`, so a newly vendored asset is reachable without a restart. `404`
for a missing build; `503` for a WebSocket upgrade — the transport multiplexes
the port but offers no WS route.

## How to find the source

There is no route table; the routes are control flow. To re-verify this
document against a checkout:

```sh
cd .super-coder/api
# every path literal, per module
grep -rnoE '"/(_sc|api|vendor)[^"]*"' server.py conversation_routes.py review_routes.py | sort -u
# every dispatch test, in the order the handler evaluates it
grep -nE 'path *(==|\.startswith|in \()|len\(parts\) *==|parts\[' server.py
# the regex-matched families
grep -nE '_PATH = re\.compile|re\.compile\(r"\^/api' conversation_routes.py review_routes.py
```

Then read `server.py`'s `do_GET` / `do_POST` / `do_PATCH` / `do_PUT` /
`do_DELETE` (near the end of the file) for the top-level fan-out, the
`_mem_*`, `_sprint_*`, `_skills_mutation_*`, `_context_get`, `_search_post`,
`_browser_shell_post` and `_pr_post` methods for the `/_sc` families, and
`handle()` in each of the two route modules. `dispatch_http` at the bottom of
`server.py` shows the gate and which prefixes are peeled off before the shim
runs. `tests/test_signin_gate.py` walks every path literal in the three
modules without a session.

For the client side — which verb calls which path — grep the CLI:

```sh
grep -rn '/_sc/' ../scripts/
```

`scripts/dispatch.sh` maps each `sc` verb to the script that owns it.
