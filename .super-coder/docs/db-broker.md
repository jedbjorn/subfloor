# Application database broker

Shells can diagnose the live application PostgreSQL database through the host
broker. Operators can opt into repair access with `db.access_mode: read_write`.
The default remains `read_only`; the configured PostgreSQL role determines the
database privileges. Credentials stay on the host.
Subfloor's own SQLite memory and repository map use their existing `sc mem`
and `sc map-*` interfaces.

## Shell commands

These commands work from the root checkout and shell worktrees, including
sandboxed shells that share the broker socket:

```sh
./sc db health
./sc db query "SELECT count(*) FROM example_table"
./sc db query "SELECT table_schema, table_name FROM information_schema.tables"
./sc db query "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM example_table LIMIT 10"
./sc db query "SHOW server_version"
./sc db query --file /path/to/repair.sql
./sc db query - < /path/to/repair.sql
./sc db query --autocommit "VACUUM ANALYZE example_table"
```

SQL is executed unchanged: string literals, comments, dollar quotes, and CTEs
retain PostgreSQL semantics. Read/write mode also permits DDL and multiple
statements. Every request has
its own connection. By default it commits on success and rolls back on failure.
Keep all steps of a transaction in one request. SQL containing explicit
`COMMIT` or `ROLLBACK` controls its own boundaries. `--autocommit` supports
commands such as `VACUUM` and `CREATE INDEX CONCURRENTLY`; earlier committed
work will remain if a later command fails. For a rollback rehearsal, send
`BEGIN; ...; ROLLBACK;` as one request. The interface takes SQL, not psql
backslash commands or COPY STDIN/STDOUT streams.

Results are JSON. Existing `columns`, `rows`, `row_count`, and `truncated`
fields remain; non-null cells are strings and SQL NULL is JSON null. Writes
report `affected_rows` and PostgreSQL's `status`. Multiple statements return a
`results` array; top-level result fields describe the last statement. SQL
errors include `sqlstate`. The CLI exits nonzero on failure. Requests are never
automatically retried; after a client disconnect, establish the outcome before
repeating a write.

The existing HTTP socket client remains compatible:

```python
import dbq
result = dbq.broker_call("POST", "/query", {"sql": "SELECT count(*) FROM example_table"})
```

`POST /query` also accepts `autocommit: true`. `GET /health` tests PostgreSQL
and returns database, role, access mode, and transaction-read-only status.
A failed connection returns `ok: false` with HTTP 503.

## Host configuration

The `db` block in `.super-coder/instance.json` selects access policy:

```json
{
  "dsn_env": "SC_DB_DSN",
  "access_mode": "read_write",
  "allow_tables": ["*"],
  "row_cap": 1000,
  "statement_timeout_ms": 30000
}
```

In `read_write` mode, the broker imposes no table allowlist or SQL-verb filter;
PostgreSQL role permissions apply. An omitted `access_mode` retains `read_only`
mode with SQL validation and a read-only transaction default. Use a dedicated PostgreSQL role with SELECT
grants for that mode. It accepts SELECT/WITH plus EXPLAIN and SHOW diagnostics;
mutating statements and stacked SQL remain rejected. In read-only mode, `['*']`
disables the table allowlist. Returned rows are capped per result set; the cap
does not limit how many rows a repair changes or the server work performed.

Store the DSN in the host's `~/.config/<repo>/db-broker.env` with mode 0600.
The broker reads the selected variable from this file on each request, ahead
of inherited environment, so a stale login-shell export cannot override it.
`dsn_env_file` in config or `SC_RO_ENVFILE` can override the path. Values are
parsed without shell execution. When the file does not define the variable,
the broker falls back to its process environment. Never put credentials in
instance.json or client SQL/arguments.

The broker executes through psycopg instead of the former psql subprocess.
`db-broker`, `db-broker-up` (without a systemd unit), and `db-broker-install`
provision a dedicated virtual environment with `psycopg[binary]==3.3.5` under
`$XDG_DATA_HOME/subfloor/db-broker-venvs/` (default `~/.local/share`). This leaves
the engine's Python and the application's environment untouched. Updates reuse
the environment when its Python and driver versions match; new versions get a
separate directory. First provisioning requires access to the Python package
index. Clients only use the socket and need no driver installed.

For a managed interpreter of your own, persist an absolute `db.broker_python`
path in instance.json, or set `SC_DB_BROKER_PYTHON` during installation. It must
already contain the pinned psycopg version. A systemd drop-in still takes
precedence over the generated unit: remove a prior `ExecStart` workaround when
adopting the engine-managed interpreter, then rerun `./sc db-broker-install`.
The existing `psql_bin` option is no longer used.

The statement timeout defaults to 5 seconds (30 seconds in the example), the
lock timeout is at most 5 seconds, and the connect timeout is 3 seconds.
Connection settings apply to each request. The socket client waits up to
70 seconds by default; Python callers can pass `timeout` for longer scripts.

## Host lifecycle

```sh
./sc db-broker-up
./sc db-broker-down
./sc db-broker-install
systemctl --user restart sc-db-broker-example.service
./sc db health
```

`up` uses the installed systemd service when present. It reports success only
when the database responds; an unhealthy existing service produces an error
instead of launching a competing broker. `down` stops a supervised broker
through systemd. The enabled user unit survives host restarts. Existing shells
can use the fixed broker without restarting their sessions.

Every SQL request is recorded in `.super-coder/run/db-broker.audit.log` with
its result status. The socket is `.super-coder/run/db-broker.sock` (0600).
