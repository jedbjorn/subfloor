# db broker

Read-only diagnostic access to the fork's **live app Postgres** for a sandboxed
shell — without handing that shell a credential or a network route. The fourth
sibling of the pm2, Windows-VM, and tailnet brokers: one host process holds the
capability so nothing downstream needs it.

Code: `.super-coder/api/db_broker.py` (the HTTP-over-unix-socket server) +
`.super-coder/scripts/dbq.py` (config, validation, the query verb, the socket
client). The `db_query` skill curls the socket. Spec: `specs_sc/db-query.md`.

## Why a broker, not the DSN in the container

A fork's shells run in a sandbox with an **empty private pg sidecar**
(`sc-pg-<fork>`, the dev/test target — see the dev_kit skill). That isolation is
correct: dev/test writes must never touch live data. But it also means the
live app DB — where the runtime/telemetry a shell needs to *confirm* a diagnosis
lives — is unreachable:

- the sidecar DSN resolves to the empty sidecar, not the live stack;
- the host env file carrying the real DSN is not mounted in;
- the live app Postgres sits on a host network the container has no route to.

The two cruder ways to close that gap both widen the blast radius:

- **mount the host DSN + open a route** → full SQL + host networking from
  inside the sandbox;
- **a read-only role whose DSN is mounted** → still needs a route, and leaks a
  live credential into the sandbox — which is `-v $here:$here` bind-mounted, so
  anything under the repo (`instance.json` included) is sandbox-readable.

The broker keeps the credential and the route entirely host-side and hands the
sandbox one narrow verb, exactly as pm2-broker does for `pm2` + the host app.

## Read-only, enforced twice

1. **The DSN points at a read-only Postgres role** (`GRANT SELECT` only) — the
   DB-enforced backstop. The broker also connects with
   `default_transaction_read_only=on`.
2. **The broker rejects any statement that is not a single `SELECT`/`WITH`**
   before `psql` ever runs — no stacked statements, no data-modifying CTEs, no
   DDL/DML/session-or-transaction control.

Table scoping is likewise belt-and-suspenders: the RO role should `GRANT SELECT`
only on the allowlisted tables (DB-authoritative), and the broker additionally
rejects a query whose `FROM`/`JOIN` targets a table outside `allow_tables`. The
default allowlist is **ops/telemetry only** (`skill_runs`, `tool_call_attempts`,
`models`); content/tenant tables (contacts, emails, chat bodies) are gated —
added only by explicit operator scope. Every query also gets a
`statement_timeout` and a row cap, and every call is logged to
`.super-coder/run/db-broker.audit.log`.

## Link config — the `db` block

Held under the `db` key of `.super-coder/instance.json`. It carries **no secret**
— instance.json is sandbox-readable, so the block names an *env var*, and the
broker (host-side) resolves the DSN from it at query time:

```json
"db": {
  "dsn_env": "SC_RO_DSN",
  "allow_tables": ["skill_runs", "tool_call_attempts", "models"],
  "row_cap": 1000,
  "statement_timeout_ms": 5000,
  "psql_bin": "psql"
}
```

`./sc db-init` scaffolds the block and prints the one-time host steps (create the
RO role, `GRANT SELECT` on the allowlisted tables, export `SC_RO_DSN`). The DSN
is parsed into libpq `PG*` env vars for the `psql` subprocess, so the password
never lands on a process argv.

## Routes

All JSON `{ok, ...}`, over the unix socket at `.super-coder/run/db-broker.sock`
(`0600`; path from `./sc db-broker-sock`):

| Verb | Call |
|---|---|
| **health** | `curl -s --unix-socket "$SOCK" http://db/health` → `{ok, service}` |
| **query** | `curl -s --unix-socket "$SOCK" http://db/query -d '{"sql":"SELECT …"}'` → `{ok, columns, rows, row_count, truncated}` or `{ok:false, error}` |

## Running it (on the HOST — never in the sandbox)

```
./sc db-init             scaffold the `db` block + print host setup steps
export SC_RO_DSN=postgresql://sc_ro:…@<host>:5432/<db>
./sc db-broker           foreground (unix socket)
./sc db-broker-up        background (nohup + pidfile); self-skips if unlinked/up
./sc db-broker-down      stop the backgrounded broker
./sc db-broker-install   supervise via a systemd --user unit (EnvironmentFile
                         carries SC_RO_DSN — a unit has no login shell)
```

`db_broker.py` guards on `SC_SANDBOX` and refuses to start inside the container.
`db-broker-up` self-skips when no `db` block is linked and no-ops when the socket
already answers; `db-broker-down` only stops what it started (a systemd-managed
broker is left alone — use `db-broker-uninstall`).

## Deferred (not in v1)

- **Any write path.** Read-only forever; a write broker is a different tool with
  a different threat model.
- **Query builders / pagination beyond the row cap / result streaming.** The
  verb takes raw `SELECT` text and returns capped rows.
- **A review-GUI panel.** The surface is the `db_query` skill (CLI over the
  socket); no `/api/db/*` route on the review server.
- **Cross-fork / cross-host access.** One broker serves one fork's live DB on
  its own host.
