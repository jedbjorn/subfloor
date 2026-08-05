#!/usr/bin/env python3
"""Startup refusals: a materialized-engine / unmigrated-DB half floor, and a
non-loopback bind (spec #26)."""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
ACK_MIGRATION = "0142_conversation_git_targets.sql"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402
import transport  # noqa: E402  (main()'s serve call — the line the guard precedes)


def build_pre_acknowledgement_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == ACK_MIGRATION:
                break
            con.executescript(migration.read_text())
            con.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration.name,))
        con.commit()
    finally:
        con.close()


class TransportReadinessTest(unittest.TestCase):
    def test_failed_start_callback_is_never_advertised_as_listening(self):
        events = []

        class FakeTransport:
            port = 8844

            async def start(self):
                events.append("bound")

            async def stop(self):
                events.append("stopped")

        def fail_readiness():
            events.append("readiness")
            raise RuntimeError("runtime unavailable")

        logs = []
        with (
            mock.patch.object(transport, "Transport", return_value=FakeTransport()),
            self.assertRaisesRegex(RuntimeError, "runtime unavailable"),
        ):
            asyncio.run(
                transport.serve(
                    "127.0.0.1",
                    8844,
                    object(),
                    object(),
                    log=logs.append,
                    on_started=fail_readiness,
                )
            )

        self.assertEqual(["bound", "readiness", "stopped"], events)
        self.assertEqual([], logs)


class ServerSchemaGuardTest(unittest.TestCase):
    def test_new_code_old_schema_refuses_startup_with_rollback_recovery(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            build_pre_acknowledgement_db(db_path)
            con = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError) as raw:
                    con.execute(
                        "SELECT target_id FROM conversation_git_targets"
                    ).fetchall()
            finally:
                con.close()
            self.assertIn("no such table", str(raw.exception))

            with mock.patch.object(
                server, "DB_PATH", db_path
            ), mock.patch.object(
                server.ports_mod, "resolve", return_value={"port": 8800}
            ), mock.patch.object(
                server.backfill_shell_api_keys, "backfill"
            ) as backfill, self.assertRaises(SystemExit) as refused:
                server.main([])

            message = str(refused.exception)
            self.assertIn("installed engine/DB schema mismatch", message)
            self.assertIn(ACK_MIGRATION, message)
            self.assertIn("before first DB use", message)
            self.assertIn("`./sc rollback --engine-only`", message)
            self.assertIn("preserving this unchanged DB", message)
            self.assertNotIn("no such table", message)
            backfill.assert_not_called()

    def test_current_migration_ledger_passes(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(SCHEMA.read_text())
                for migration in sorted(MIGRATIONS.glob("*.sql")):
                    con.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (?)",
                        (migration.name,))
                con.commit()
            finally:
                con.close()

            server.require_current_schema(db_path, MIGRATIONS)


class ContainerEvidenceTest(unittest.TestCase):
    """`in_container()` reads artifacts a container RUNTIME wrote, and nothing
    a caller can assert (spec #26, conformance finding SC-149).

    The distinction is the whole finding: the check this replaced returned
    True for `SC_SANDBOX=1`, so the "boundary" it certified was a string an
    operator, a stray shell, or a `.env` could set. These tests hold the
    evidence to the filesystem — patched to a temp path so they say something
    on a host runner AND inside the sandbox, where the real `/.dockerenv`
    would otherwise make every case pass for free.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / ".dockerenv"
        self.cgroup = Path(self.tmp.name) / "cgroup"
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(server, "_CONTAINER_MARKERS",
                                    (str(self.marker),)),
                  mock.patch.object(server, "_PID1_CGROUP", self.cgroup)):
            p.start()
            self.addCleanup(p.stop)

    def test_no_evidence_is_not_a_container(self):
        self.assertFalse(server.in_container())

    def test_an_environment_variable_is_never_evidence(self):
        # The SC-149 pin at its narrowest: the old discriminator, alone,
        # must now prove exactly nothing.
        for value in ("1", "true", "yes"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"SC_SANDBOX": value}):
                    self.assertFalse(server.in_container())

    def test_a_runtime_marker_file_is_evidence(self):
        self.marker.touch()
        self.assertTrue(server.in_container())

    def test_pid1_cgroup_naming_a_runtime_is_evidence(self):
        self.cgroup.write_text(
            "12:pids:/docker/6f2c1a\n11:cpu:/docker/6f2c1a\n")
        self.assertTrue(server.in_container())

    def test_a_host_pid1_cgroup_is_not_evidence(self):
        self.cgroup.write_text("0::/init.scope\n")
        self.assertFalse(server.in_container())

    def test_an_unreadable_cgroup_fails_closed(self):
        # No /proc at all (a stripped chroot, a non-Linux runner): absence of
        # evidence is not evidence, so the refusal applies.
        self.assertFalse(server.in_container())


class LoopbackBindGuardTest(unittest.TestCase):
    """Spec #26 Failure Modes: a non-loopback bind refuses to start unless the
    replacement boundary is POSITIVELY VERIFIED.

    This is the fence behind the automatic browser bootstrap: a session mints
    for any caller that can present an allowed `Host` and a same-origin
    `Origin`, both of which a remote client chooses freely. Unreachability is
    therefore the control, and it has to be enforced rather than assumed —
    including in the sandbox exception, which is what SC-149 caught.

    Every case patches `in_container` rather than the environment, because the
    suite runs INSIDE the sandbox: reading the real evidence would make the
    refusal cases unreachable here and the file would quietly stop testing
    the guard at all.
    """

    def _guard(self, bind, *, container):
        with mock.patch.object(server, "in_container", return_value=container):
            server.require_loopback_bind(bind)

    def test_host_refuses_a_non_loopback_bind(self):
        for bind in ("0.0.0.0", "", "::", "192.168.1.10",
                     "10.0.0.5", "example.com"):
            with self.subTest(bind=bind):
                with self.assertRaises(SystemExit) as caught:
                    self._guard(bind, container=False)
                self.assertIn("loopback", str(caught.exception))

    def test_host_accepts_loopback_binds(self):
        for bind in ("127.0.0.1", "localhost", "LocalHost", "::1",
                     "[::1]", "127.0.0.53"):
            with self.subTest(bind=bind):
                self._guard(bind, container=False)

    def test_a_claimed_sandbox_off_container_still_refuses(self):
        # SC-149 exactly as conformance reproduced it: `SC_SANDBOX=1` with
        # `SC_BIND=0.0.0.0` on a bare host used to open a wide listener the
        # spec asserted was fenced. Setting the variable does not create a
        # docker publish mapping, so it must not buy the exemption.
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}):
            with self.assertRaises(SystemExit) as caught:
                self._guard("0.0.0.0", container=False)
        self.assertIn("SC_SANDBOX", str(caught.exception))

    def test_container_keeps_the_wide_bind_docker_publishes(self):
        # The counterweight. `./sc launch` sets SC_BIND=0.0.0.0 in the
        # container ON PURPOSE so docker can publish the port; the boundary
        # there is its `-p 127.0.0.1:PORT:PORT` mapping — a precondition, not
        # something this check verifies (see BoundaryClaimTest). Over-refusing
        # here would make the sandbox unlaunchable while removing no exposure —
        # and it must hold with SC_SANDBOX absent, since the evidence is now
        # the filesystem's.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SC_SANDBOX", None)
            self._guard("0.0.0.0", container=True)


class BoundaryClaimTest(unittest.TestCase):
    """What we SAY the bind guard establishes, held to what it establishes
    (conformance finding SC-154 — SC-149 one layer up).

    SC-149 was a guard claiming more than it enforced. SC-154 was the same
    defect one layer up: the code comment and the public trust-boundary page
    honestly admitted that `--network=host` shares the host's namespace and
    still passes, then concluded host-loopback-only anyway. Both statements
    cannot be true, and it is the conclusion that was wrong — the summary
    sentence had not inherited its own caveat.

    So the pin is per SENTENCE, deliberately: a loopback-only claim must name
    what makes it true where it makes it, because a caveat three paragraphs
    away is exactly what failed here. These are prose assertions, and prose is
    the artifact — the guarantee an operator acts on is the one they read.
    """

    TRUST_DOC = ENGINE / "docs" / "interface-trust-boundary.md"

    # A sentence asserting the loopback guarantee, in either word order.
    LOOPBACK_ONLY = re.compile(r"loopback[^.]*\bonly\b|\bonly\b[^.]*loopback",
                               re.I)
    # What may make that claim true, named in the same sentence: the in-process
    # guard (which EXITS unless the bind is loopback) or the operator
    # precondition the sandbox exemption rests on. "Enforced in-process and by
    # docker's port mapping" is NOT one of these — it is the overclaim.
    QUALIFIED = re.compile(r"precondition|assum\w+|unless|provided|"
                           r"--network=host", re.I)
    # The claim the evidence cannot support: that it demonstrates a network
    # namespace. A marker file and a cgroup path show a container filesystem
    # and cgroup context; `--network=host` has neither of its own.
    NAMESPACE_CLAIM = re.compile(
        r"(shows?|proves?|establish\w*|demonstrat\w*|means)[^.]*"
        r"network namespace", re.I)
    NAMESPACE_LIMIT = re.compile(r"\bnot\b[^.]*network namespace", re.I)

    def sentences(self, text: str) -> list[str]:
        """Prose split into sentences. Soft-wrapped lines fold together first
        (or a claim and its qualifier land in different fragments purely
        because the paragraph was rewrapped); a bullet, table row, or heading
        starts a new block. A sentence ends at `. ` before a capital, so
        `127.0.0.1` and `e.g.` do not split one."""
        blocks: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                blocks.append("")
            elif stripped[0] in "-*|#" or not blocks or not blocks[-1]:
                blocks.append(stripped)
            else:
                blocks[-1] += " " + stripped
        out: list[str] = []
        for block in blocks:
            out.extend(s for s in re.split(r"(?<=\.)\s+(?=[A-Z*`\"])", block)
                       if s.strip())
        return out

    def prose(self):
        """Every place the guarantee is stated: the operator-facing page and
        the two docstrings a maintainer reads before touching the guard."""
        return (("the trust-boundary doc", self.TRUST_DOC.read_text()),
                ("in_container()", server.in_container.__doc__),
                ("require_loopback_bind()",
                 server.require_loopback_bind.__doc__))

    def test_no_loopback_only_claim_stands_without_its_precondition(self):
        for where, text in self.prose():
            for sentence in self.sentences(text):
                if not self.LOOPBACK_ONLY.search(sentence):
                    continue
                with self.subTest(where=where, sentence=sentence[:70]):
                    self.assertRegex(
                        sentence, self.QUALIFIED,
                        f"{where}: this concludes loopback-only without "
                        f"naming what makes it true — the in-process refusal "
                        f"or the operator precondition:\n  {sentence}")

    def test_nothing_claims_the_evidence_shows_a_network_namespace(self):
        for where, text in self.prose():
            for sentence in self.sentences(text):
                with self.subTest(where=where, sentence=sentence[:70]):
                    self.assertNotRegex(
                        sentence, self.NAMESPACE_CLAIM,
                        f"{where}: a marker file and a cgroup path do not "
                        f"establish a network namespace — `--network=host` "
                        f"passes both:\n  {sentence}")

    def test_the_namespace_limit_is_stated_where_the_evidence_is(self):
        # The negative above passes trivially on prose that says nothing at
        # all; the limit has to be stated, not merely not-contradicted.
        for where, text in self.prose()[:2]:
            with self.subTest(where=where):
                self.assertTrue(
                    any(self.NAMESPACE_LIMIT.search(s)
                        for s in self.sentences(text)),
                    f"{where} never says the evidence is not a network "
                    f"namespace")


class LoopbackBindStartupWiringTest(unittest.TestCase):
    """The guard only fences anything if main() actually calls it.

    LoopbackBindGuardTest above pins the FUNCTION: delete the single
    `require_loopback_bind(bind)` line from main() and every one of its
    subtests still passes while the unsafe startup it exists to stop comes
    straight back. So these drive `server.main([])` itself, with the
    neighbouring startup steps stubbed, and assert the one thing that is
    load-bearing — an unsafe SC_BIND exits BEFORE the transport ever serves,
    and the sandbox's deliberate wide bind still reaches it.
    """

    def setUp(self):
        # main() rebinds the module-level `_CSP` to the served port; put it
        # back so test order cannot matter.
        csp = server._CSP
        self.addCleanup(lambda: setattr(server, "_CSP", csp))

    def _start(self, bind, *, container=False, port=8800):
        """Run main([]) to the bind decision.

        Returns refusal, served host, and whether the conversation broker,
        reaper, and armed Sprint services crossed their lifecycle boundaries.
        """
        served = []

        async def _fake_serve(
            host,
            port,
            http_handler,
            ws_handler,
            log=print,
            on_started=None,
            stream_handler=None,
        ):
            served.append(host)
            self.assertIs(
                stream_handler,
                server.conversation_routes.stream_events,
                "startup did not wire browser conversation SSE to transport",
            )
            if on_started is not None:
                on_started()
            return port

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shell_db.db"
            db_path.touch()
            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                # SC_SANDBOX is pinned ON throughout: post-SC-149 it grants
                # nothing, so leaving it set proves the wiring keys on the
                # container evidence and not on the variable.
                enter(mock.patch.dict(os.environ,
                                      {"SC_BIND": bind, "SC_SANDBOX": "1"},
                                      clear=False))
                enter(mock.patch.object(server, "in_container",
                                        return_value=container))
                enter(mock.patch.object(server, "DB_PATH", db_path))
                enter(mock.patch.object(server.ports_mod, "resolve",
                                        return_value={"port": port}))
                # Everything main() does between the DB check and the bind is
                # someone else's contract — stub it so only the ordering of
                # guard vs. serve is under test here.
                enter(mock.patch.object(server, "require_current_schema"))
                enter(mock.patch.object(server.backfill_shell_api_keys,
                                        "backfill"))
                enter(mock.patch.object(server.mem_credentials, "provision"))
                broker_start = enter(mock.patch.object(
                    server.conversation_broker, "start_service"
                ))
                reaper_start = enter(mock.patch.object(
                    server.conversation_reaper, "start_service"
                ))
                reaper_stop = enter(mock.patch.object(
                    server.conversation_reaper, "stop_service"
                ))
                sprint_start = enter(mock.patch.object(
                    server.sprint_runtime, "start_service"
                ))
                watcher_start = enter(mock.patch.object(
                    server.sprint_pr_watcher, "start_service"
                ))
                enter(mock.patch.object(transport, "serve", _fake_serve))
                enter(contextlib.redirect_stdout(io.StringIO()))
                enter(contextlib.redirect_stderr(io.StringIO()))
                refusal = None
                try:
                    server.main([])
                except SystemExit as exc:
                    refusal = exc
        return (
            refusal,
            served[0] if served else None,
            broker_start.called,
            reaper_start.called,
            reaper_stop.called,
            sprint_start.called,
            watcher_start.called,
        )

    def test_startup_refuses_an_unsafe_bind_before_serving(self):
        for bind in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            with self.subTest(bind=bind):
                (
                    refusal,
                    served,
                    broker_started,
                    reaper_started,
                    reaper_stopped,
                    sprint_started,
                    watcher_started,
                ) = self._start(bind)
                self.assertIsNotNone(
                    refusal, "main() accepted a non-loopback bind on a host")
                self.assertIn("loopback", str(refusal))
                self.assertIn("SC_BIND", str(refusal))
                # The refusal is only a fence if the listener never opened —
                # assert that directly rather than inferring it from the exit.
                self.assertIsNone(
                    served, f"transport served {served!r} despite the refusal")
                self.assertFalse(
                    broker_started,
                    "conversation broker started before bind refusal",
                )
                self.assertFalse(
                    reaper_started,
                    "conversation reaper started before bind refusal",
                )
                self.assertFalse(
                    reaper_stopped,
                    "conversation reaper shutdown ran without a server",
                )
                self.assertFalse(
                    sprint_started,
                    "Sprint runtime started before bind refusal",
                )
                self.assertFalse(
                    watcher_started,
                    "Sprint PR watcher started before bind refusal",
                )

    def test_startup_serves_a_loopback_bind(self):
        self.assertEqual(
            self._start("127.0.0.1"),
            (None, "127.0.0.1", True, True, True, True, True),
        )

    def test_startup_imports_no_removed_scheduler(self):
        for name in (
            "pr_poller",
            "conductor_runtime",
            "conductor_routes",
            "sprint_routes",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(server, name),
                    f"server startup still imports removed machinery: {name}",
                )

    def test_startup_serves_the_wide_bind_in_the_sandbox(self):
        # The counterweight: over-refusing here would make `./sc launch`
        # unbootable, so the sandbox exception has to survive the wiring too.
        self.assertEqual(
            self._start("0.0.0.0", container=True),
            (None, "0.0.0.0", True, True, True, True, True),
        )

    def test_startup_binds_the_csp_to_the_served_port(self):
        # The app shell's socket sources are port-exact (SC-152), so main()
        # rebinding `_CSP` is load-bearing: skip it and a fork on any port but
        # the module default serves a policy that forbids its own terminal
        # stream. Assert against a port that cannot come from the default.
        self._start("127.0.0.1", port=8899)
        self.assertIn("ws://127.0.0.1:8899", server._CSP)
        self.assertNotIn("8800", server._CSP)


class CspSourceListTest(unittest.TestCase):
    """The app shell's CSP is release-critical (spec #26 Trust Boundary), and
    spec #26 Delivery Plan step 6 owes it a check. Nothing asserted it until
    conformance finding SC-152 — which is how `connect-src 'self' ws: wss:`
    shipped while the comment above it said "same-origin connections only".

    `ws:` and `wss:` are CSP scheme-sources: they match ANY host on that
    scheme, so they left injected same-origin script an outbound socket to
    anywhere. These tests fail if that widening is reintroduced in any form.
    """

    def _directive(self, csp, name):
        for part in csp.split(";"):
            tokens = part.split()
            if tokens and tokens[0] == name:
                return tokens[1:]
        self.fail(f"CSP has no {name!r} directive: {csp!r}")

    def test_connect_src_names_no_scheme_source(self):
        sources = self._directive(server._csp(8800), "connect-src")
        for src in sources:
            with self.subTest(source=src):
                # A scheme-source is `scheme:` with no `//authority` — the
                # exact shape that matches every host.
                self.assertFalse(
                    src.endswith(":") and "//" not in src,
                    f"{src!r} is a CSP scheme-source and matches any host")

    def test_connect_src_is_limited_to_this_servers_own_origins(self):
        sources = self._directive(server._csp(8800), "connect-src")
        self.assertIn("'self'", sources)
        self.assertEqual(
            {s for s in sources if s != "'self'"},
            {"ws://127.0.0.1:8800", "wss://127.0.0.1:8800",
             "ws://localhost:8800", "wss://localhost:8800",
             "ws://[::1]:8800", "wss://[::1]:8800"})

    def test_the_rest_of_the_policy_stays_strict(self):
        csp = server._csp(8800)
        self.assertEqual(self._directive(csp, "script-src"), ["'self'"])
        self.assertEqual(self._directive(csp, "default-src"), ["'self'"])
        self.assertEqual(self._directive(csp, "object-src"), ["'none'"])
        self.assertEqual(self._directive(csp, "frame-ancestors"), ["'none'"])
        self.assertEqual(self._directive(csp, "base-uri"), ["'none'"])


if __name__ == "__main__":
    unittest.main()
