"""Shared rig for the Sprints v2.4 safety-net tests (Feature #92, spec #269).

``LaneRig`` drives one Sprint through the real shell-facing surface: the
``sc sprint`` CLI parser, the authenticated HTTP routes, the wake delivery
service, and the PR watcher.  GitHub reads, harness probes, and native turns
are replaced by the same stubs the existing Sprint tests use, so a run is
hermetic: a temporary database, a loopback port chosen by the kernel, no
network, and no sleeps.

The module also owns the upgrade scaffold used by acceptance criterion 16:

* ``build_database`` lays down ``schema.sql`` and then applies migrations
  through the real runner (``migrate.apply``), optionally stopping at a named
  prior level;
* ``upgrade_database`` runs the real ``migrate.migrate`` over whatever the
  tree holds beyond that level;
* ``load_rows`` restores the frozen mid-lane Sprint that the engine wrote at
  the prior level (``tests/fixtures/sprint_v24/``).

Regenerating the frozen mid-lane state is only meaningful from a checkout
whose engine code is at ``PRIOR_MIGRATION``::

    python tests/sprint_v24_support.py --write-midlane-fixture
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Self
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api"), str(ROOT / "tests")]

import db_driver
import mem
import migrate
import server
import sprint_cli
import sprint_domain
import sprint_message_delivery
import sprint_pr_watcher
from github_pull_requests import PullRequest
from sprint_route_binding_support import candidate as route_candidate

# The migration level every Sprint in flight is assumed to have been written
# at.  It is a fixed name, not "the newest file": a later task adds its
# migration above this level and the upgrade test applies it on top.
PRIOR_MIGRATION = "0284_reseed_sprint_consistency.sql"

MIDLANE_FIXTURE = (
    ROOT / "tests" / "fixtures" / "sprint_v24" / "midlane_at_0284.sql"
)

PLANNER, DEVELOPER, REVIEWER = "PLN1", "DEV1", "REV1"
SHELLS = {
    # shortname: (shell_id, display name, flavor, role, API token)
    DEVELOPER: (1, "Developer", "dev", "developer", "dev-token"),
    REVIEWER: (2, "Reviewer", "reviewer", "reviewer", "review-token"),
    PLANNER: (3, "Planner", "planner", "planner", "planner-token"),
}
REPOSITORY = "acme/repo"


def build_database(path: Path, *, through: str | None = None) -> list[str]:
    """Create an engine database with the real migration runner.

    ``through`` names the last migration to apply; ``None`` applies all.
    """
    con = db_driver.connect(path)
    try:
        con.executescript((ENGINE / "schema.sql").read_text())
        migrate.applied_set(con)
        applied = []
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if through is not None and migration.name > through:
                break
            migrate.apply(con, migration)
            applied.append(migration.name)
        con.commit()
        return applied
    finally:
        con.close()


def upgrade_database(path: Path) -> list[str]:
    """Run the real ``migrate.migrate`` and return the files it applied."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = migrate.migrate(str(path))
    if status != 0:
        raise AssertionError(f"migrate exited {status}: {output.getvalue()}")
    return [
        line.removeprefix("migrate: applied ").strip()
        for line in output.getvalue().splitlines()
        if line.startswith("migrate: applied ")
    ]


def migration_ledger(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [
            str(row[0])
            for row in con.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            )
        ]
    finally:
        con.close()


def _data_lines(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [
            line
            for line in con.iterdump()
            if line.startswith("INSERT INTO ")
            and not line.startswith('INSERT INTO "sqlite_sequence"')
            and not line.startswith('INSERT INTO "schema_migrations"')
        ]
    finally:
        con.close()


def dump_new_rows(path: Path, baseline: Path) -> str:
    """Return the rows ``path`` holds beyond an empty database at its level."""
    before = set(_data_lines(baseline))
    after = _data_lines(path)
    missing = before - set(after)
    changed_tables = {line.split('"')[1] for line in missing}
    lines = [line for line in after if line not in before]
    replaced_tables = {line.split('"')[1] for line in lines}
    if changed_tables - replaced_tables:
        raise AssertionError(
            "mid-lane state deleted baseline rows from "
            f"{sorted(changed_tables - replaced_tables)}"
        )
    return "\n".join(
        line.replace("INSERT INTO ", "INSERT OR REPLACE INTO ", 1) for line in lines
    ) + "\n"


def load_rows(path: Path, fixture: Path) -> None:
    """Restore rows exactly as the engine stored them.

    Triggers guard transitions, not stored states, so replaying finished rows
    through them would reject history the engine legitimately wrote.  They are
    lifted for the load and put back unchanged; foreign keys and integrity are
    checked afterwards.
    """
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        triggers = con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
        for name, _sql in triggers:
            con.execute(f'DROP TRIGGER "{name}"')
        con.executescript("BEGIN;\n" + fixture.read_text() + "COMMIT;\n")
        for _name, sql in triggers:
            con.execute(sql)
        con.commit()
        restored = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone()[0]
        if restored != len(triggers):
            raise AssertionError("fixture load did not restore every trigger")
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise AssertionError(f"fixture violates foreign keys: {violations}")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise AssertionError(f"fixture failed integrity check: {integrity}")
    finally:
        con.close()


class FakeGitHub:
    """One mutable PR per number, read by every watcher and merge gate."""

    def __init__(self) -> None:
        self.pulls: dict[int, PullRequest] = {}

    def set(
        self,
        number: int,
        *,
        state: str = "OPEN",
        checks: str | None = "SUCCESS",
        checks_failed: bool = False,
        head_sha: str = "a" * 40,
    ) -> None:
        self.pulls[number] = PullRequest(
            number=number,
            head_ref=f"feature/pr-{number}",
            base_ref="main",
            head_sha=head_sha,
            state=state,
            merged_at="2026-07-31T20:00:00Z" if state == "MERGED" else None,
            merge_sha="b" * 40 if state == "MERGED" else None,
            title=f"PR {number}",
            url=f"https://github.com/{REPOSITORY}/pull/{number}",
            review_decision=None,
            checks=checks,
            checks_failed=checks_failed,
            base_sha="c" * 40,
        )

    def get(self, number: int) -> PullRequest:
        return self.pulls[number]

    def list(self) -> list[PullRequest]:
        return list(self.pulls.values())


@dataclass(frozen=True)
class Delivery:
    wake_id: int
    recipient: str
    state: str
    attempt: int
    turns: int


class Refused(AssertionError):
    """A command that was expected to be refused succeeded instead."""


class LaneRig:
    """One temporary engine with a Planner, a Developer, and a Reviewer."""

    def __init__(self, db_path: Path, scratch: Path) -> None:
        self.db_path = Path(db_path)
        self.scratch = Path(scratch)
        self.github = FakeGitHub()
        self._stack = contextlib.ExitStack()
        self._files = 0
        self._wake_mark = 0
        self._message_mark = 0
        self._event_mark = 0
        self._future = timedelta(0)
        self.prompts: list[str] = []
        self.con: sqlite3.Connection | None = None

    # -- lifecycle of the rig itself -------------------------------------

    def __enter__(self) -> Self:
        stack = self._stack
        stack.enter_context(
            mock.patch.object(
                sprint_domain,
                "_participant_binding_candidate",
                side_effect=route_candidate,
            )
        )
        stack.enter_context(
            mock.patch.object(
                sprint_domain.route_bindings, "verify_stored_v2_before_first_turn"
            )
        )
        adapter = mock.Mock()
        adapter.probe.return_value = None
        stack.enter_context(
            mock.patch.object(sprint_domain, "adapter_for", return_value=adapter)
        )
        for module in (server.sprint_pr_watcher, server.sprint_review_loop):
            stack.enter_context(
                mock.patch.object(
                    module, "GitHubPullRequestReader", return_value=self.github
                )
            )
        stack.enter_context(
            mock.patch.dict(os.environ, {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"})
        )
        stack.enter_context(mock.patch.object(server, "DB_PATH", self.db_path))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()

        def stop() -> None:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        stack.callback(stop)
        stack.enter_context(
            mock.patch.object(
                mem, "SC_API_BASE", f"http://127.0.0.1:{httpd.server_address[1]}"
            )
        )
        stack.enter_context(mock.patch.object(mem, "SC_API_TOKEN", ""))
        self.con = db_driver.connect(self.db_path)
        stack.callback(self.con.close)
        self.mark()
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()

    # -- seed data ---------------------------------------------------------

    def seed_roster(self) -> dict[str, int]:
        """Insert the operator, three shells, one feature, spec, and tasks."""
        con = self.con
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (?,?,?,?,'prompt',1,?)",
            [
                (shell_id, display, shortname, flavor, token)
                for shortname, (shell_id, display, flavor, _role, token)
                in SHELLS.items()
            ],
        )
        feature_id = int(
            con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Safety-net feature','in_progress')"
            ).lastrowid
        )
        document_id = int(
            con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Safety-net spec','bound safety-net spec')",
                (feature_id,),
            ).lastrowid
        )
        task_ids = [
            int(
                con.execute(
                    "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
                    "VALUES (?,?,?,?)",
                    (feature_id, document_id, seq, f"Task {seq}"),
                ).lastrowid
            )
            for seq in (1, 2)
        ]
        con.commit()
        return {
            "feature_id": feature_id,
            "document_id": document_id,
            "task_1": task_ids[0],
            "task_2": task_ids[1],
        }

    def write(self, body: str) -> str:
        self._files += 1
        path = self.scratch / f"input-{self._files}.txt"
        path.write_text(body)
        return str(path)

    def participants_file(self) -> str:
        return self.write(
            json.dumps(
                [
                    {"shell_id": shell_id, "role": role, "harness": "codex"}
                    for shell_id, _display, _flavor, role, _token in SHELLS.values()
                ]
            )
        )

    # -- the shell-facing surface -----------------------------------------

    def cli(self, shell: str, *argv: str) -> dict:
        """Run one ``sc sprint`` command as ``shell`` and return its receipt."""
        output = io.StringIO()
        with (
            mock.patch.object(mem, "SC_API_TOKEN", SHELLS[shell][4]),
            contextlib.redirect_stdout(output),
        ):
            status = sprint_cli.main([str(item) for item in argv])
        if status != 0:
            raise AssertionError(f"sc sprint {argv[0]} exited {status}")
        return json.loads(output.getvalue())

    def cli_refusal(self, shell: str, *argv: str) -> str:
        """Run a command that must be refused; return the refusal text."""
        output, errors = io.StringIO(), io.StringIO()
        try:
            with (
                mock.patch.object(mem, "SC_API_TOKEN", SHELLS[shell][4]),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                sprint_cli.main([str(item) for item in argv])
        except SystemExit as exc:
            if isinstance(exc.code, str):
                return exc.code
            lines = [line for line in errors.getvalue().splitlines() if line.strip()]
            return f"exit {exc.code}: {lines[-1] if lines else ''}"
        raise Refused(f"sc sprint {' '.join(map(str, argv))} was not refused")

    def api_refusal(self, shell: str, path: str, payload: dict) -> str:
        """POST straight to a Sprint route, bypassing the CLI parser."""
        try:
            with (
                mock.patch.object(mem, "SC_API_TOKEN", SHELLS[shell][4]),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                mem._api("POST", path, payload)
        except SystemExit as exc:
            return str(exc.code)
        raise Refused(f"POST {path} was not refused")

    # -- engine workers the test stands in for ----------------------------

    def advance_clock(self, **delta: int) -> None:
        """Move the delivery worker's clock forward without sleeping."""
        self._future += timedelta(**delta)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc) + self._future

    def _turn(self, unreachable: frozenset[str]):
        """A native turn that starts and at once ends successfully.

        A shell named in ``unreachable`` cannot be launched at all: the
        delivery worker sees a transport failure and no turn exists.
        """
        con = self.con

        def deliver(conversation_id: str, prompt: str, key: str) -> str:
            shell = con.execute(
                "SELECT sh.shortname FROM conversations c "
                "JOIN shells sh ON sh.shell_id=c.shell_id "
                "WHERE c.conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            if shell in unreachable:
                raise RuntimeError("simulated harness launch failure")
            message_id = int(
                con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','test','prompt',?,?,?,'completed',"
                    "'2026-08-01 00:00:01')",
                    (conversation_id, prompt, key, key),
                ).lastrowid
            )
            run_id = int(
                con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,"
                    "lease_owner,lease_expires_at,started_at,ended_at,exit_code) "
                    "SELECT ?,shell_id,?,'succeeded','test-broker',"
                    "'2026-08-01 00:00:01','2026-08-01 00:00:00',"
                    "'2026-08-01 00:00:01',0 FROM conversations "
                    "WHERE conversation_id=?",
                    (conversation_id, message_id, conversation_id),
                ).lastrowid
            )
            for state in ("queued", "running", "waiting"):
                con.execute(
                    "UPDATE conversations SET state=? WHERE conversation_id=?",
                    (state, conversation_id),
                )
            con.commit()
            self.prompts.append(prompt)
            return f"conversation-run:{run_id}"

        return deliver

    def deliver(
        self, *, unreachable: tuple[str, ...] = (), limit: int = 20
    ) -> list[Delivery]:
        """Deliver every claimable wake; each message runs a turn that ends."""
        self.prompts = []
        turn = self._turn(frozenset(unreachable))
        service = sprint_message_delivery.SprintWakeDeliveryService(
            self.con, now=self._now
        )
        delivered: list[Delivery] = []
        for _ in range(limit):
            before = len(self.prompts)
            outcome = service.deliver_once("safety-net-worker", turn)
            if outcome is None:
                return delivered
            recipient = self.con.execute(
                "SELECT sh.shortname FROM sprint_wake_outbox w "
                "JOIN shells sh ON sh.shell_id=w.receiver_shell_id "
                "WHERE w.wake_id=?",
                (outcome.wake_id,),
            ).fetchone()[0]
            delivered.append(
                Delivery(
                    outcome.wake_id,
                    str(recipient),
                    outcome.state,
                    outcome.attempt_number,
                    len(self.prompts) - before,
                )
            )
        raise AssertionError("wake delivery did not drain")

    def poll_github(self) -> None:
        """One PR-watcher pass over every open subscription."""
        sprint_pr_watcher.SprintPRWatcher(
            self.con,
            repo_root=ROOT,
            reader_factory=lambda _repository: self.github,
        ).poll_once()

    # -- observations -------------------------------------------------------

    def mark(self) -> None:
        """Forget everything observed so far; the next read is a delta."""
        self._wake_mark = self._max("sprint_wake_outbox", "wake_id")
        self._message_mark = self._max("wake_message", "message_id")
        self._event_mark = self._max("sprint_events", "event_id")

    def _max(self, table: str, column: str) -> int:
        try:
            return int(
                self.con.execute(
                    f"SELECT COALESCE(MAX({column}),0) FROM {table}"
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            return 0

    def new_messages(self) -> list[tuple]:
        """Messages created since the last read.

        Each is ``(recipient, reason, declared type, wakes)``.  ``wakes`` is
        ``"own wake"`` when the message opened a wake outbox row,
        ``"joins pending wake"`` when it was attached to a wake already queued
        for that shell, and ``"no wake"`` when nothing was queued for it.
        """
        rows = self.con.execute(
            "SELECT m.message_id,sh.shortname,m.message_kind,m.intent,"
            "m.declared_type,m.idempotency_key,m.body,m.from_participant_id,"
            "wm.wake_id "
            "FROM wake_message m JOIN shells sh ON sh.shell_id=m.receiver_shell_id "
            "LEFT JOIN sprint_wake_messages wm ON wm.message_id=m.message_id "
            "WHERE m.message_id>? ORDER BY m.message_id",
            (self._message_mark,),
        ).fetchall()
        seen = self._wake_mark
        result = []
        for row in rows:
            if row["wake_id"] is None:
                wakes = "no wake"
            elif int(row["wake_id"]) > seen:
                wakes = "own wake"
                seen = int(row["wake_id"])
            else:
                wakes = "joins pending wake"
            result.append(
                (
                    str(row["shortname"]),
                    message_reason(row),
                    str(row["declared_type"]),
                    wakes,
                )
            )
        self._message_mark = self._max("wake_message", "message_id")
        self._wake_mark = max(
            self._wake_mark, self._max("sprint_wake_outbox", "wake_id")
        )
        return result

    def new_events(self) -> list[str]:
        rows = self.con.execute(
            "SELECT event_id,event_type FROM sprint_events WHERE event_id>? "
            "ORDER BY event_id",
            (self._event_mark,),
        ).fetchall()
        self._event_mark = self._max("sprint_events", "event_id")
        return [str(row["event_type"]) for row in rows]

    def wake_rows(self) -> list[tuple]:
        """Every wake outbox row: ``(recipient, state, attempts, key family)``."""
        return [
            (
                str(row["shortname"]),
                str(row["state"]),
                int(row["attempt_count"]),
                key_family(str(row["idempotency_key"])),
            )
            for row in self.con.execute(
                "SELECT sh.shortname,w.state,w.attempt_count,w.idempotency_key "
                "FROM sprint_wake_outbox w "
                "JOIN shells sh ON sh.shell_id=w.receiver_shell_id "
                "ORDER BY w.wake_id"
            )
        ]

    def unread(self) -> list[tuple]:
        """Unread messages, oldest first: ``(recipient, reason)``."""
        return [
            (str(row["shortname"]), message_reason(row))
            for row in self.con.execute(
                "SELECT sh.shortname,m.message_kind,m.intent,m.idempotency_key,"
                "m.body,m.from_participant_id FROM wake_message m "
                "JOIN shells sh ON sh.shell_id=m.receiver_shell_id "
                "WHERE m.read_at IS NULL ORDER BY m.message_id"
            )
        ]

    def lifecycle(self, sprint_id: int) -> str:
        return str(
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0]
        )

    def units(self, sprint_id: int) -> list[str]:
        return [
            str(row[0])
            for row in self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE sprint_id=? "
                "ORDER BY work_unit_id",
                (sprint_id,),
            )
        ]

    def pauses(self, sprint_id: int) -> list[str]:
        """The recorded cause of every pause, oldest first."""
        return [
            str(json.loads(row[0])["reason"])
            for row in self.con.execute(
                "SELECT body FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='pause' ORDER BY report_id",
                (sprint_id,),
            )
        ]

    def last_event(self, sprint_id: int, event_type: str) -> dict:
        row = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? AND event_type=? "
            "ORDER BY event_id DESC LIMIT 1",
            (sprint_id, event_type),
        ).fetchone()
        if row is None:
            raise AssertionError(f"no {event_type} event")
        return json.loads(row[0])

    def message_id(self, shell: str, kind: str, *, unread: bool = True) -> int:
        """The newest message of ``kind`` addressed to ``shell``."""
        row = self.con.execute(
            "SELECT m.message_id FROM wake_message m "
            "JOIN shells sh ON sh.shell_id=m.receiver_shell_id "
            "WHERE sh.shortname=? AND m.message_kind=? "
            + ("AND m.read_at IS NULL " if unread else "")
            + "ORDER BY m.message_id DESC LIMIT 1",
            (shell, kind),
        ).fetchone()
        if row is None:
            raise AssertionError(f"no {kind} message for {shell}")
        return int(row[0])

    def inbox_ids(self, shell: str, sprint_id: int) -> list[int]:
        return [
            int(item["message_id"])
            for item in self.cli(shell, "inbox", "--sprint", sprint_id)["messages"]
        ]

    def accept_all(self, shell: str, sprint_id: int) -> list[str | None]:
        """Old-style pickup: ``accept`` every message in the Sprint inbox."""
        return [
            self.cli(shell, "accept", "--sprint", sprint_id, "--message", message_id)[
                "disposition"
            ]
            for message_id in self.inbox_ids(shell, sprint_id)
        ]

    # -- common lane prefixes ----------------------------------------------

    def declare_and_plan(self, seed: dict[str, int]) -> tuple[int, int, int]:
        """Declare one Sprint with two units; the second depends on the first."""
        sprint_id = int(
            self.cli(
                PLANNER,
                "declare",
                "--feature", seed["feature_id"],
                "--spec", seed["document_id"],
                "--participants-file", self.participants_file(),
                "--merge-grant",
            )["sprint_id"]
        )
        first = self.plan_unit(sprint_id, "First lane", seed["task_1"])
        second = self.plan_unit(
            sprint_id, "Second lane", seed["task_2"], depends_on=first
        )
        return sprint_id, first, second

    def plan_unit(
        self,
        sprint_id: int,
        title: str,
        task_id: int,
        *,
        depends_on: int | None = None,
    ) -> int:
        argv = [
            "plan-unit",
            "--sprint", sprint_id,
            "--developer-shell", SHELLS[DEVELOPER][0],
            "--reviewer-shell", SHELLS[REVIEWER][0],
            "--title", title,
            "--expected-output-file", self.write(f"{title}: one merged PR."),
            "--task", task_id,
        ]
        if depends_on is not None:
            argv += ["--depends-on", depends_on]
        return int(self.cli(PLANNER, *argv)["work_unit_id"])

    def arm(self, sprint_id: int) -> dict:
        return self.cli(
            PLANNER,
            "arm",
            "--sprint", sprint_id,
            "--conformance-reviewer-shell", SHELLS[REVIEWER][0],
        )

    def register_pr(self, sprint_id: int, unit_id: int, number: int) -> int:
        return int(
            self.cli(
                DEVELOPER,
                "register-pr",
                "--sprint", sprint_id,
                "--repository", REPOSITORY,
                "--pr", number,
                "--work-unit", unit_id,
            )["registered_pr_id"]
        )

    def start_first_lane(
        self, seed: dict[str, int], *, checks: str | None = "SUCCESS"
    ) -> tuple[int, int, int, int]:
        """Declare, arm, deliver, accept, and register the first lane's PR."""
        sprint_id, first, second = self.declare_and_plan(seed)
        self.arm(sprint_id)
        self.deliver()
        self.accept_all(PLANNER, sprint_id)
        self.accept_all(DEVELOPER, sprint_id)
        self.github.set(41, checks=checks)
        registered = self.register_pr(sprint_id, first, 41)
        return sprint_id, first, second, registered


def key_family(key: str) -> str:
    """Reduce an idempotency key to its stable shape."""
    parts = []
    for part in key.split(":"):
        if len(part) >= 32 and all(c in "0123456789abcdef" for c in part):
            parts.append("HASH")
        else:
            parts.append(re.sub(r"\d+", "N", part))
    return ":".join(parts)


def message_reason(row: sqlite3.Row) -> str:
    """Name why a message exists, from what the engine stored about it.

    A message this table does not recognise keeps its raw key shape, so a new
    kind of wake cannot hide behind an existing name.
    """
    kind, body = str(row["message_kind"]), str(row["body"])
    family = key_family(str(row["idempotency_key"]))
    if kind == "work_assignment":
        return "assignment"
    if kind == "review_request":
        return "review request"
    if family == "sprint:N:arming-model-selections":
        return "route list"
    if family.startswith("pr-transition:"):
        return "PR " + re.search(r"event=(\w+)", body).group(1)
    if family == "sprint-pause:N:vN":
        return "pause notice"
    if family.startswith("sprint:N:delivery-terminal:"):
        return "delivery terminal"
    if body.startswith("Review approved for PR"):
        return "approval notice"
    if body.startswith("Review changes requested for PR"):
        return "changes-requested notice"
    if re.match(r"Sprint \d+ completed by Reviewer conformance", body):
        return "close receipt"
    if row["from_participant_id"] is not None:
        return f"{row['intent']} relay"
    return f"?{family}"


def write_midlane_fixture() -> None:
    """Freeze a Sprint the engine wrote mid-lane at ``PRIOR_MIGRATION``."""
    import tempfile

    newest = max(path.name for path in MIGRATIONS.glob("*.sql"))
    if newest != PRIOR_MIGRATION:
        raise SystemExit(
            f"refusing: this checkout is at {newest}, not {PRIOR_MIGRATION}; "
            "the fixture must be written by the prior engine"
        )
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        baseline = scratch_path / "baseline.db"
        build_database(baseline, through=PRIOR_MIGRATION)
        live = scratch_path / "midlane.db"
        build_database(live, through=PRIOR_MIGRATION)
        with LaneRig(live, scratch_path) as rig:
            # Checks are still running when the update lands.
            rig.start_first_lane(rig.seed_roster(), checks=None)
            rig.deliver()
            rig.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        MIDLANE_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        MIDLANE_FIXTURE.write_text(
            "-- A Sprint in flight, exactly as the engine stored it at\n"
            f"-- {PRIOR_MIGRATION}: declared with --merge-grant, armed,\n"
            "-- first assignment delivered and accepted, PR 41 registered with\n"
            "-- its checks still running.  Written by\n"
            "--   python tests/sprint_v24_support.py --write-midlane-fixture\n"
            "-- Never hand-edit; never regenerate from a later engine.\n"
            # Chat rows record the checkout they ran in; keep the host out.
            + dump_new_rows(live, baseline).replace(str(ROOT), "/srv/engine")
        )
    print(f"wrote {MIDLANE_FIXTURE.relative_to(ROOT)}")


if __name__ == "__main__":
    if sys.argv[1:] != ["--write-midlane-fixture"]:
        raise SystemExit(__doc__)
    write_midlane_fixture()
