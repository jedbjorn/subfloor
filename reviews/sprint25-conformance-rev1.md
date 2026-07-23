# CONFORMANCE: Sprint 25 boundary/API/auth — REV1 shard working notes

- Spec: doc #20 (Interface-backed planner wake) · main @13f5405 (worktree
  /tmp/sc-main-13f5405, detached)
- Shard: Overview, Product Boundary, API Resources, Occupancy×auth, Sprint
  Scope, Security And Privacy, Verification
- Narrative input: decisions #19, #23, #26 (direction-superseded by #30),
  #28/#31, #32, #33, #34; frozen-CANCEL = intentional.

## Findings so far (verified first-hand)

### API Resources
- All 16 spec-table routes exist under `/api/interface/*` EXCEPT pr-watches:
  `POST/DELETE /api/interface/pr-watches` have no route — the watch contract
  (baseline-before-arm, ACTIVE-sprint scoping, idempotent registration,
  explicit reconcile) ships on the pre-existing `/_sc/watches` shell-token
  surface (api/server.py:2084-2247). Not in the ratified list →
  deviated-silently (Low; contract honored on an adjacent surface, but not
  the Interface API the spec names, and not browser/operator-scoped).
- 201+Location on session create: interface_routes.py:433-436 ✓
- 409 occupied race w/ current session ref: :328-338, :438-450 ✓
- 422 validation / 401 / 403: throughout ✓; error envelope
  `{error:{code,message,details}}`: :111-113 ✓
- Unknown payload fields rejected: ONLY create_session (:309-312) and
  hook-callbacks (:1353-1359). stream-tickets, writer-leases,
  clean-certifications, termination-requests, reconciliations,
  sprint-bindings, receipts, retry silently ignore unknown fields →
  deviated-silently (Low).
- Idempotency-Key: every routed mutation goes through `_idempotent`
  (:197-240) — missing key 422, exact replay returns stored response,
  key+other body 409, actor+operation scoped, insert-race backstop. ✓
  EXCEPTION: POST /api/interface/browser-sessions requires the key
  (:1485-1487) but never stores it — an exact replay mints a SECOND browser
  session instead of returning the original → deviated-silently (Low;
  benign, both sessions valid until restart).
- Terminal input frames use lease sequence, not HTTP key ✓ (WS 0x01 frames).

### Auth surface (the focus)
- Bootstrap = operator-capability exchange (decision #26 impl):
  `_browser_session` (:1470-1493) requires same-origin proof + Bearer
  operator token + Idempotency-Key; mints in-memory session, Set-Cookie
  `sc_if=…; HttpOnly; SameSite=Strict; Path=/`, CSRF token in JSON body.
  Direction-superseded by #30 — recorded, NOT flagged.
- HttpOnly/SameSite=Strict cookie ✓; CSRF: browser mutations need X-CSRF
  matching the session (:190-191, :1533-1536) ✓; cross-site mutation
  rejected via Origin/Sec-Fetch-Site (:150-157, :1537-1539) ✓.
- Host allowlist (127.0.0.1/localhost/[::1]) enforced in handle() (:60,
  :132-134, :1501-1502) — but WS upgrades are demuxed by transport.py:98-99
  STRAIGHT to interface_ws.handle_ws, bypassing handle(); the WS path
  checks Origin==Host exactly (interface_ws.py:199-204) but has NO Host
  allowlist. DNS-rebinding-shaped requests (Host: attacker.tld → 127.0.0.1,
  Origin matching) pass the WS Origin check. Mitigated: a valid single-use
  ticket is still required and tickets are minted only via the Host-checked,
  session+CSRF-gated HTTP POST → defense-in-depth gap, not an open door.
  → deviated-silently (Low/Medium — spec's control set names Host/Origin
  for the whole surface).
- Generation-scoped hook tokens: callback-route only. Dispatch sends
  hook-callbacks before actor resolution (:1516-1517); the hook token
  authenticates ONLY against interface_generations.hook_token_hash
  (:1370-1379) and grants no actor — it cannot attach/write/take
  over/stop/reconcile. ✓ Content-free callbacks (allowlisted fields,
  unknown rejected) ✓. Every rejection path audited via _log (flag #51,
  decision #31) — verified all 10 rejection exits log ✓.
- Stream tickets: single-use (pop on consume), 60s TTL, bound to
  session/role/client/lease (interface_runtime.py:608-639); writer tickets
  require the current lease_token (:592-602) ✓. Generation binding is
  implicit (session row ↔ one live generation; consume re-checks
  generation liveness, interface_ws.py:215-218). Tickets are in-memory —
  nothing durable in plaintext ✓.
- Exact-Origin WS ✓ (:199-204); versioned subprotocol sc-term.v1 required
  (:205-208) ✓.
- WS permessage-deflate: ServerProtocol constructed without extensions
  (interface_ws.py:175) → compression not negotiated ✓ (disabled as specced).
- Bounded buffers: WS message 1 MiB (:42), input frame 64 KiB (:44,
  :305-310), per-client outbound 2 MiB → close 1011 (:43, :98-102), HTTP
  head 64 KiB / body 8 MiB (transport.py:31-32) ✓. Ping 20s/timeout 40s ✓.
- Input ack only after durable broker accept + forward:
  interface_runtime.py:1028-1093 — writer_revoked/stale_generation/seq_gap/
  duplicate(replayed-ack)/delivery_unknown rejections without forwarding ✓.
  Viewer input rejected server-side (interface_ws.py:311-314) ✓.
- Viewer cannot become writer or stop: writer ticket needs current
  lease_token (:592-602); terminate requires operator/browser actor
  (shell actors excluded :1526-1532) and occupied session; lease release
  only with the lease's own token hash (:549-560) ✓.
- Unmanaged-harness refusal: New chat 409 unmanaged_harness when the
  liveness scan finds a legacy process (:339-350); availability projects
  unreconciled (:276-280) ✓.
- Decision-#30 boundary: browser never holds the operator capability —
  the exchange response carries only session cookie + CSRF; nothing in the
  routes layer exposes the operator token to JS. (UI asset check: see
  security agent report.)

### Nitpicks (Low, spec-literal)
- Timestamps: sqlite `datetime('now')` → "YYYY-MM-DD HH:MM:SS" (space
  separator, no Z) vs spec "ISO-8601 UTC".
- Browser sessions are process-memory only (restart = re-bootstrap) —
  consistent with the personal-machine boundary; noted, not flagged.

## Shard A (occupancy + sprint scope) — explore-agent findings (spot-checked)
- starting→error: extra legal lifecycle edge, not in spec, not ratified,
  currently undriven (interface_state.py:31 + trigger) → deviated-silently (Low)
- "input broker healthy" not checked at wake-item eligibility
  (interface_wake.py:38-82); submit-time re-gate fails closed with
  retries+alert → deviated-silently (Low)
- client dimension not projected in the spec's none/viewer/writer/unmanaged
  vocabulary (API returns writer.held/client_id + clients count) → Low
- illegal-edge rejection audited (log) on hook path only; receipt PATCH
  swallows to bare 409 (:1320-1321); triggers keep no audit → Low
- everything else as-specced; frozen-CANCEL deviated-intentionally (ratified)

## Security shard (explore agent, M1 re-verified first-hand)
- Bullets 1,3,4,5,6,7,8 as-specced; 9 deviated-intentionally (#32,
  compensation verified total: writer preflight owns unreachable-tmux as
  definite PreSendError, interface_runtime.py:543-552 → broker:919-931).
- **M1 (Medium, deviated-silently):** `_idempotent` persists full mutation
  responses; acquire_lease/mint_ticket responses carry PLAINTEXT
  lease_token/ticket (interface_routes.py:218-227,542,604-608);
  interface_idempotency_keys is snapshotted with no row filter and no
  SENSITIVE_COLUMNS exclusion (snapshot.py:88) → live plaintext lease tokens
  into git-tracked .sc-state/content.sql (24h idem TTL). Violates Security
  bullet 2 categorically. Verified first-hand against snapshot.py.
- Bootstrap exchange conformant with #26; operator cap never JS-reachable
  (cookie HttpOnly; body carries only CSRF; ui/app.js nulls the paste).
- CSP connect-src admits any ws:/wss: host (server.py:104-111) — Low F9.
- Browser sessions never expire server-side — observation.

## Verification shard (explore agent; CI gap confirmed by local run)
- Suite at 13f5405: 801 passed, 4 skipped (the tmux-gated TmuxIntegrationTest
  tests — no tmux/node on CI runner, wired nowhere else). Subset
  interface_api/transitions/wake/wake_submit/hooks: 121 passed.
- Covered: A, C, E, F (exhaustive app+trigger edge walk), G, H, I.
- **M2 (Medium):** group D byte-fidelity matrix — zero shipped-code coverage;
  spike-only evidence against spike code, unwired.
- Partial: B (slow client, bounded buffers, stream-loss resync, resize
  ordering), J (container/host restarts, exactly-once startup reconcile).
- Provider smoke (10 scenarios): no harness — pending-by-design (#34).
- Minor assertion gaps: coalescing, dangling-intent detection, WS-upgrade
  exact-Origin, 2.99/3.00s boundary, erased-but-dirty, explicit force-push.

## Outcome
- CONFORMANCE doc filed as document #27; result row #539 to PLN1
  (0 Major / 2 Medium / 13 Low). Shard does not reopen the sprint.
- Fixed prompt verified verbatim: interface_broker.py:28.
- Decision #26 recorded direction-superseded (not flagged); frozen-CANCEL
  deviated-intentionally; seq-10 route shapes deviated-intentionally (#33).
