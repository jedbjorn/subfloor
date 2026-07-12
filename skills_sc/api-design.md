---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# api-design

REST/HTTP API design patterns — resource naming, status codes, pagination, filtering, errors, versioning, idempotency. Use when designing or reviewing API endpoints.

**Category:** craft

---

# api-design — designing & reviewing HTTP APIs

## Resources & methods
- Name resources as **plural nouns**, never verbs: `/users`, `/users/{id}/orders`.
- `GET` read (safe, idempotent) · `POST` create / non-idempotent action ·
  `PUT` full replace (idempotent) · `PATCH` partial update · `DELETE` remove.
- Nest one level deep max; beyond that, link by id.

## Status codes
- `200` ok · `201` created (+ `Location`) · `204` no content.
- `400` bad input · `401` unauth · `403` forbidden · `404` not found ·
  `409` conflict · `422` validation · `429` rate-limited.
- `5xx` = server's fault; NEVER return `5xx` for a client error.

## Payloads
- One error shape everywhere: `{ "error": { "code", "message", "details" } }`.
- **Pagination**: cursor-based for large/changing sets (`?cursor=&limit=`),
  return `next_cursor`; offset only for small stable sets.
- **Filtering/sorting**: explicit query params (`?status=open&sort=-created`);
  whitelist fields — NEVER interpolate them into a query.
- Timestamps ISO-8601 UTC; ids opaque strings.

## Robustness
- **Idempotency**: make retries safe — `PUT`/`DELETE` naturally; for `POST`,
  accept an idempotency key.
- **Versioning**: prefix (`/v1/…`) or header; add fields backward-compatibly,
  never repurpose an existing one.
- Validate at the boundary; unknown fields = reject or ignore — pick one and
  document it. NEVER leak internals (stack traces, SQL) in error responses.

## Review lens
Each endpoint does one thing? Right method + status? Errors uniform? Inputs
validated/whitelisted? Retries safe? Breaking change hiding in a "small"
tweak?
