#!/usr/bin/env python3
"""Regression coverage for truthful, non-owning map finalization."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import map_finalize  # noqa: E402
import map_repo  # noqa: E402


EXTRACTOR = b"def extract(con, repo_root, cfg):\n    return 'ok'\n"


class MapFinalizeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / "live"
        self.live.mkdir()
        self.local = self.root / "instance" / ".sc-state" / "local"
        self.db = self.local / "map" / "map.db"
        self.db.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.db)
        connection.executescript((ENGINE / "map_schema.sql").read_text())
        connection.execute(
            "INSERT INTO dr_repo "
            "(repo_id,name,root,remote,vcs,default_branch,file_count,mapped_at) "
            "VALUES (1,?,?,?,?,?,?,?)",
            ("live", str(self.live), "origin", "git", "main", 2, "2026-08-20T00:00:00"),
        )
        connection.executemany(
            "INSERT INTO dr_filepath (path,desc) VALUES (?,?)",
            (("README.md", "Project overview and entrypoint"),
             ("src/app.py", "Runs the application service")),
        )
        connection.execute(
            "INSERT INTO dr_section (name,path_prefix,description,sort_order) "
            "VALUES ('Source','src/','Application source',1)"
        )
        connection.commit()
        connection.close()
        self.snapshot = self.local / "map" / "content.sql"
        self.snapshot.write_text(
            "BEGIN;\n"
            "INSERT INTO dr_section "
            "(name,path_prefix,description,sort_order) "
            "VALUES ('Source','src/','Application source',1);\n"
            "COMMIT;\n"
        )
        self.map_root_patch = mock.patch.object(map_repo, "MAP_ROOT", self.live)
        self.local_patch = mock.patch.object(artifact_policy, "LOCAL_DIR", self.local)
        self.map_root_patch.start()
        self.local_patch.start()
        self.addCleanup(self.map_root_patch.stop)
        self.addCleanup(self.local_patch.stop)
        self.refresh = map_repo.MapRefreshResult(
            files=2,
            dependencies=0,
            env_vars=0,
            truncated=False,
            extractor_summaries=(),
            map_root=self.live,
            mapped_at="2026-08-20T00:00:00",
        )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db)

    @staticmethod
    def empty_api(path: str) -> dict:
        if path == "/_sc/mem/messages":
            return {"messages": []}
        raise AssertionError(f"unexpected API read: {path}")

    def install_target_and_receipt(
        self,
        worktree: Path,
        source_path: str = ".sc-state/map_extractors/routes.py",
    ) -> tuple[Path, Path]:
        target = self.live / ".sc-state" / "map_extractors" / "routes.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(EXTRACTOR)
        receipt = artifact_policy.map_extractor_receipts_dir() / "routes.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "version": 1,
            "extractor": "routes.py",
            "digest": hashlib.sha256(EXTRACTOR).hexdigest(),
            "source_path": source_path,
            "source_worktree": str(worktree),
            "source_git_ref": None,
            "installed_at": "2026-08-20T00:00:00+00:00",
        }))
        return target, receipt

    def init_source_git(self, *, with_remote: bool) -> Path:
        worktree = self.root / "cart-worktree"
        source = worktree / ".sc-state" / "map_extractors" / "routes.py"
        source.parent.mkdir(parents=True)
        source.write_bytes(EXTRACTOR)
        commands = (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "cart@example.invalid"],
            ["git", "config", "user.name", "Cartographer"],
            ["git", "add", ".sc-state/map_extractors/routes.py"],
            ["git", "commit", "-qm", "add extractor"],
        )
        for command in commands:
            subprocess.run(command, cwd=worktree, check=True)
        if with_remote:
            remote = self.root / "remote.git"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(
                ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                cwd=remote,
                check=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)],
                cwd=worktree,
                check=True,
            )
            subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=worktree, check=True)
        return worktree

    def test_all_clear_report_is_green_and_human_json_agree(self):
        before_db = self.db.read_bytes()
        before_snapshot = self.snapshot.read_bytes()

        rows = map_finalize.build_report(self.refresh, None, self.empty_api)

        self.assertEqual(
            ["PASS", "PASS", "N/A", "N/A", "N/A", "PASS", "N/A"],
            [row.status for row in rows],
        )
        self.assertEqual(("PASS", 0), map_finalize.report_exit(rows))
        human = map_finalize.render_human(rows)
        payload = json.loads(map_finalize.render_json(rows))
        self.assertTrue(human.startswith("MAP FINALIZE: PASS (exit 0)"))
        self.assertEqual("PASS", payload["overall"])
        self.assertEqual(0, payload["exit_code"])
        self.assertEqual([row.key for row in rows], [row["key"] for row in payload["rows"]])
        self.assertEqual(before_db, self.db.read_bytes())
        self.assertEqual(before_snapshot, self.snapshot.read_bytes())

    def test_live_quality_pending_and_invariant_or_runtime_failure(self):
        connection = self.connect()
        connection.execute("UPDATE dr_filepath SET desc=NULL WHERE path='README.md'")
        connection.commit()
        connection.close()
        pending = map_finalize.check_live_map(self.refresh, None)
        self.assertEqual("PENDING", pending.status)
        self.assertTrue(any("README.md" in item for item in pending.evidence))

        failed_refresh = map_finalize.check_live_map(None, "extractor exploded")
        self.assertEqual("FAIL", failed_refresh.status)
        self.assertTrue(failed_refresh.next_actions)

        broken = map_repo.MapRefreshResult(
            **{**self.refresh.__dict__, "extractor_summaries": ("routes: FAILED (boom)",)}
        )
        failed_extractor = map_finalize.check_live_map(broken, None)
        self.assertEqual("FAIL", failed_extractor.status)
        self.assertTrue(any("routes" in item for item in failed_extractor.evidence))

        connection = self.connect()
        connection.execute(
            "INSERT INTO dr_section (name,path_prefix,description,sort_order) "
            "VALUES ('Catch all','','Invalid section',2)"
        )
        connection.commit()
        connection.close()
        empty_prefix = map_finalize.check_live_map(self.refresh, None)
        self.assertEqual("FAIL", empty_prefix.status)
        self.assertTrue(any("empty prefix" in item for item in empty_prefix.evidence))

    def test_authored_snapshot_can_pass_pend_or_fail(self):
        self.assertEqual("PASS", map_finalize.check_authored_sections().status)
        self.snapshot.unlink()
        pending = map_finalize.check_authored_sections()
        self.assertEqual("PENDING", pending.status)
        self.assertEqual(("Admin: ./sc snapshot",), pending.next_actions)
        self.snapshot.write_text("not valid SQL")
        self.assertEqual("FAIL", map_finalize.check_authored_sections().status)

    def test_install_source_and_admin_rows_advance_independently(self):
        worktree = self.init_source_git(with_remote=False)
        self.install_target_and_receipt(worktree)
        install, records, names = map_finalize.check_extractor_install()
        self.assertEqual("PASS", install.status)
        self.assertEqual("PASS", map_finalize.check_extractor_source(records, names).status)
        self.assertEqual("PENDING", map_finalize.check_admin_handoff(records, names).status)

        (artifact_policy.map_extractor_receipts_dir() / "routes.json").unlink()
        missing, _, _ = map_finalize.check_extractor_install()
        self.assertEqual("PENDING", missing.status)
        self.assertTrue(any("map-extractor install" in action for action in missing.next_actions))

    def test_receipt_source_must_be_the_exact_authored_extractor_path(self):
        worktree = self.init_source_git(with_remote=False)
        self.install_target_and_receipt(worktree, source_path="other/routes.py")
        alternate = worktree / "other" / "routes.py"
        alternate.parent.mkdir()
        alternate.write_bytes(EXTRACTOR)
        subprocess.run(["git", "add", "other/routes.py"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "add alternate"], cwd=worktree, check=True)

        install, records, names = map_finalize.check_extractor_install()

        self.assertEqual("PASS", install.status)
        source = map_finalize.check_extractor_source(records, names)
        self.assertEqual("PENDING", source.status)
        self.assertTrue(any("unsafe" in item for item in source.evidence))

    def test_admin_handoff_passes_only_after_matching_remote_default_source(self):
        worktree = self.init_source_git(with_remote=True)
        self.install_target_and_receipt(worktree)
        install, records, names = map_finalize.check_extractor_install()

        self.assertEqual("PASS", install.status)
        self.assertEqual("PASS", map_finalize.check_extractor_source(records, names).status)
        self.assertEqual("PASS", map_finalize.check_admin_handoff(records, names).status)

    def test_unread_notice_and_flags_are_independent_and_fail_closed(self):
        body = (
            "shape: API changed — paths: src/; ref: PR #1\n"
            "flags: 9=SC-009\n"
            "curate; verify and close each flag; mark this notice read last."
        )

        def open_api(path: str) -> dict:
            if path == "/_sc/mem/messages":
                return {"messages": [{"message_id": 12, "body": body, "read_at": None}]}
            if path == "/_sc/mem/flags/9":
                return {"flag": {"flag_id": 9, "display_name": "SC-009", "resolved": 0}}
            raise AssertionError(path)

        notices, flags = map_finalize.check_notices(open_api)
        self.assertEqual("PENDING", notices.status)
        self.assertEqual("PENDING", flags.status)
        self.assertIn("sc mem flag close 9", flags.next_actions[0])

        def resolved_api(path: str) -> dict:
            if path == "/_sc/mem/messages":
                return {"messages": [{"message_id": 12, "body": body, "read_at": None}]}
            return {"flag": {
                "flag_id": 9,
                "display_name": "SC-009",
                "resolved": 1,
                "resolution_notes": "Verified src/ descriptions and section",
            }}

        notices, flags = map_finalize.check_notices(resolved_api)
        self.assertEqual("PENDING", notices.status)
        self.assertEqual("PASS", flags.status)

        malformed = lambda path: {"messages": [{
            "message_id": 13,
            "body": body.replace("flags: 9=SC-009\n", ""),
            "read_at": None,
        }]}
        notices, flags = map_finalize.check_notices(malformed)
        self.assertEqual("PENDING", notices.status)
        self.assertEqual("PENDING", flags.status)

    def test_missing_api_identity_is_pending_not_a_false_green(self):
        notices, flags = map_finalize.check_notices(
            lambda path: (_ for _ in ()).throw(map_finalize.ApiUnavailable("missing identity"))
        )
        self.assertEqual("PENDING", notices.status)
        self.assertEqual("PENDING", flags.status)
        self.assertTrue(notices.next_actions)

    def test_exit_precedence_is_fail_then_pending_then_green(self):
        passing = map_finalize.CheckRow("a", "A", "PASS", (), ())
        not_applicable = map_finalize.CheckRow("b", "B", "N/A", (), ())
        pending = map_finalize.CheckRow("c", "C", "PENDING", (), ("next",))
        failed = map_finalize.CheckRow("d", "D", "FAIL", (), ("retry",))
        self.assertEqual(("PASS", 0), map_finalize.report_exit([passing, not_applicable]))
        self.assertEqual(("PENDING", 2), map_finalize.report_exit([passing, pending]))
        self.assertEqual(("FAIL", 1), map_finalize.report_exit([pending, failed]))

    def test_notice_evaluator_exception_still_produces_exact_seven_rows(self):
        rows = map_finalize.build_report(
            self.refresh,
            None,
            lambda path: [],
        )

        self.assertEqual(7, len(rows))
        self.assertEqual("FAIL", rows[-2].status)
        self.assertEqual("FAIL", rows[-1].status)

    def test_dispatcher_preserves_plain_map_and_routes_finalize(self):
        dispatch = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertIn('finalize)  shift', dispatch)
        self.assertIn('exec "$PY" "$S/map_finalize.py" "$@"', dispatch)
        self.assertIn('exec "$PY" "$S/map_repo.py" ;;', dispatch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
