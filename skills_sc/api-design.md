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

Catalogue skill (opt-in). Reach for it when shaping or reviewing an endpoint.

## Resources & methods
- Name resources as **plural nouns**, not verbs: `/users`, `/users/{id}/orders`.
- `GET` read (safe, idempotent) · `POST` create / non-idempotent action ·
  `PUT` full replace (idempotent) · `PATCH` partial update · `DELETE` remove.
- Nest only one level deep; beyond that, link by id.

## Status codes
- `200` ok · `201` created (+ `Location`) · `204` no content.
- `400` bad input · `401` unauth · `403` forbidden · `404` not found ·
  `409` conflict · `422` validation · `429` rate-limited.
- `5xx` = server's fault; never use it for client errors.

## Payloads
- Consistent error shape: `{ "error": { "code", "message", "details" } }`.
- **Pagination**: cursor-based for large/changing sets (`?cursor=&limit=`),
  return `next_cursor`; offset only for small stable sets.
- **Filtering/sorting**: explicit query params (`?status=open&sort=-created`);
  whitelist fields — never interpolate them into a query.
- Timestamps ISO-8601 UTC; ids opaque strings.

## Robustness
- **Idempotency**: make retries safe — `PUT`/`DELETE` naturally; for `POST`,
  accept an idempotency key.
- **Versioning**: prefix (`/v1/…`) or header; add fields backward-compatibly,
  never repurpose one.
- Validate at the boundary; reject unknown fields or ignore them — decide and
  document. Don't leak internals (stack traces, SQL) in errors.

## Review lens
Does each endpoint do one thing? Right method + status? Errors uniform? Inputs
validated/whitelisted? Retries safe? Breaking change hiding in a "small" tweak?
