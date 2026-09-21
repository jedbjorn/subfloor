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
calls `server.dispatch_http(method, path, headers, body)`, which forwards
review and conversation paths to their own modules and drives everything else
through a socketless `BaseHTTPRequestHandler` subclass (`_ShimHandler`).
Dispatch inside that handler is an ordered chain of `if path == …` /
`path.startswith(…)` / `len(parts) ==` tests in `do_GET`, `do_POST`,
`do_PATCH`, `do_PUT`, `do_DELETE` — there is no route table to read. The
conversation and review modules are the exception: they match compiled
`re` patterns in a single `handle()`.

`HEAD` is answered only for `/vendor/…`; every other path gets `405`.

### Credential classes

| Class | How it is presented | Enforced by |
|---|---|---|
| Shell token | `Authorization: Bearer <shells.api_key>` | `Handler._require_shell_auth` (401 on absent or unknown) |
| Planner shell token | the same, plus `shells.flavor == 'planner'` | `_resolve_planner_shell` (403 otherwise) |
| Browser operator | **no** `Authorization` header; the first active `users` row | `Handler._require_browser_operator`, `conversation_routes._operator` |
| Runtime token | `X-SC-Runtime-Token` header | `runtime_flags.token_matches` |
| None | nothing checked beyond reachability | the route itself |

A presented shell token is an explicit **disqualifier** on browser-operator
routes: `/_sc` answers a shell, the operator surfaces answer the operator, and
crossing them returns `403 fnb_operator_required` (server.py) or
`403 OPERATOR_REQUIRED` (conversation/review). Browser-operator *mutations*
additionally require same-origin provenance: a supplied `Origin` must equal
`Host`, and `Sec-Fetch-Site` must be absent, `same-origin` or `none`
(`403 same_origin_required` / `403 NOT_SAME_ORIGIN`). Conversation and review
requests also validate a loopback `Host`
(`403 HOST_NOT_ALLOWED` / `HOST_FORBIDDEN`).

Routes marked "none" below are unauthenticated: the loopback bind is the whole
control. That is a deliberate property of a single-operator local server, not
an oversight — see the trust boundary doc before reasoning about it.

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
| `GET /narrative`, `POST /narrative` | narrative line log |
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
`abort` · `conformance` · `followup-disposition` · `cleanup-runs`

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
| `PUT /_sc/skills/retire/…` | — | always `403`: retire/unretire write the tracked fork retire manifest on the host and stay Admin-only |
| `PUT /_sc/runtime-flags/{source}` | runtime token | system-managed non-blocking advisories; `401` when `X-SC-Runtime-Token` does not match |

## `/api` — the Review GUI

Engine-private. Unless a row says otherwise the credential class is **none**;
the client is the local browser app (`ui/app.js`), tab named in the last
column.

### Reads

| `GET` path | Credential | Purpose / tab |
|---|---|---|
| `/api/health` | none | liveness payload |
| `/api/logs` | none | last 20 webapp events, newest first (`LOG_MAX_EVENTS`) |
| `/api/git-state?fetch=1` | none | live git hygiene; `fetch=1` does the network fetch. `#repo` |
| `/api/shells`, `/api/shells/{id}` | none | `{shells, repo_root}`; `404 no such shell`. `#view-shells` |
| `/api/shell-templates`, `/api/flavor-defaults` | none | flavor catalogue and per-flavor model defaults |
| `/api/models?refresh=1` | none | model catalogue |
| `/api/skills`, `/api/skills/{id}` | none | skill catalogue with origin tags |
| `/api/roadmap` | none | `#view-roadmap` |
| `/api/docs` | none | `#view-docs` |
| `/api/documents/{id}` | none | full document row |
| `/api/documents/{id}/open` | none | `302` to a rendered-markdown URL |
| `/api/flags` | none | `#view-flags` |
| `/api/map` | none | repo-map summary. `#view-map` |
| `/api/analytics/{sessions,tokens,usage,filters}` | none | `#view-analytics`; all take the shared filter query |
| `/api/analytics/quota` | none | one entry per provider — `{providers:[{provider,status,detail,captured_at,windows}], ttl_seconds, probed, notes}`. Probes only when this process's last probe *attempt* is older than the TTL; never at boot, never on a timer. Carries no operator identity by design |
| `/api/scripts`, `/api/services` | none | `#view-scripts`, including broker/sidecar configured/running/persistent state |
| `/api/vm`, `/api/ts`, `/api/pm2` | none | the stored `instance.json` block |
| `/api/ts/status`, `/api/pm2/status` | none | live tailnet / process view; proxied to the host broker under `SC_SANDBOX`, `503` with the exact `./sc …-broker-up` command when that socket is absent |
| `/api/browser`, `/api/web-search` | browser operator | config + status. The web-search key itself never crosses this boundary |
| `/api/sprints?lifecycle=&limit=&cursor=` | browser operator | Sprint list. `#view-sprints` |
| `/api/sprints/{id}` | browser operator | the same board projection shells read at `/_sc/sprint/{id}/board` |
| `/api/sprints/{id}/events`, `/api/sprints/{id}/summaries` | browser operator | paged by `limit`, `cursor`, `work_unit_id` |
| `/api/sprints/{id}/spec-revisions/{document_id}` | browser operator | FnB view of the governing revision; `404 spec_revision_not_found`, `409` on `BoundRevisionUnavailable` |

### Writes

| Method + path | Credential | Notes |
|---|---|---|
| `POST /api/flags`, `POST /api/projects`, `POST /api/shells` | none | `201` on create, `400` on validation |
| `POST /api/flavor-defaults` | none | set a flavor's default model route |
| `POST /api/analytics/sweep` | none | recompute analytics |
| `POST /api/analytics/quota/probe` | none | force a probe (the refresh button) |
| `POST /api/snapshot` | none | `{output}` from the snapshot render. `#snapshot` |
| `POST /api/publish` | none | **`410 Gone`** — retired |
| `POST /api/scripts/{key}` | none | run one registered script; `404 no such script`, `500` on failure |
| `POST /api/services/{key}/{up\|down\|install\|uninstall}` | none | sidecars accept `up`/`down` only; `{init:true}` enables-and-starts the sidecar. `404` unknown pair, `409 not_configured`, `503 host_required` (brokers are host processes) |
| `POST /api/{vm,ts,pm2}/validate/{check}` | none | run one live check against the candidate config in the body, test-before-save. A failed check is `200 {ok:false}`, not an error; `404` unknown check, `503` when the host broker is unreachable |
| `POST /api/browser`, `POST /api/web-search/validate` | browser operator + same-origin | `{action, config}` link/validate |
| `PATCH /api/shells/{id}` | none | operational fields only — seed and L&S have no write endpoint at all, by law |
| `PATCH /api/flags/{id}`, `PATCH /api/roadmap/{id}`, `PATCH /api/documents/{id}` | none | frozen documents refuse body edits |
| `PATCH /api/sprints/{id}` | browser operator + same-origin | FnB lifecycle actions from the board |
| `PUT /api/shells/{id}/skills/{skill_id}`, `PUT /api/flavors/{flavor}/skills/{skill_id}` | none | `{granted}`. `409` when the shell inherits from a flavor pack; `500 {ok:false, committed:true}` when the write landed but projection failed |
| `PUT /api/roadmap/{id}/blockers` | none | `{blocked_by:[ids]}` replaces the set; empty clears it |
| `PUT /api/vm`, `PUT /api/ts`, `PUT /api/pm2` | none | persist the block to `instance.json`; no key material |
| `PUT /api/web-search`, `DELETE /api/web-search` | browser operator + same-origin | set/rotate or clear the stored key |
| `DELETE /api/shells/{id}` | none | soft delete (`is_deleted=1`); `404` if already gone |

## `/api/conversations` — browser chat

Engine-private; `api/conversation_routes.py`. Browser operator on every route,
loopback `Host` required, same-origin required on every mutation. Client:
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
| `POST /api/conversations/shell-release` | release a shell a CLI or browser session owns |
| `GET /api/conversations/{cv}/events` | **SSE only.** `text/event-stream`, `id:` = event sequence, `?after=`/`Last-Event-ID` resume, `: keepalive` heartbeat. Served by `stream_events` straight off the transport; the ordinary handler answers `406 SSE_REQUIRED` |

Refusals: `401 UNAUTHORIZED`, `403 OPERATOR_REQUIRED` (a shell token was
presented), `403 NOT_SAME_ORIGIN`, `403 HOST_NOT_ALLOWED`, `409` for
shell-busy and close/reopen conflicts, `422 VALIDATION_ERROR`,
`503 ENGINE_DB_BUSY` with `{retry_after: 2}`, `503 OPERATOR_UNAVAILABLE`,
`500 INTERNAL_ERROR`. Event payloads are redacted of harness session refs and
secret-shaped keys before they reach the browser.

## `/api/review-*` — Diff review

Engine-private; `api/review_routes.py`. Browser operator, loopback `Host`,
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

`GET /`, `/index.html`, `/app.js`, `/style.css` from `.super-coder/ui/`, with
`Cache-Control: no-cache` plus a content-derived `ETag` (so a rebuild that
produces identical bytes still revalidates cheaply, and an updated `app.js`
never survives an engine update). `index.html` additionally carries the
restrictive CSP. `GET`/`HEAD /vendor/…` resolves per request against
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
`server.py` shows which prefixes are peeled off before the shim runs.

For the client side — which verb calls which path — grep the CLI:

```sh
grep -rn '/_sc/' ../scripts/
```

`scripts/dispatch.sh` maps each `sc` verb to the script that owns it.
