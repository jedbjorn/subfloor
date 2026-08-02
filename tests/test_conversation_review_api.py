"""Feature #26 authenticated browser review GET resources."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from functools import partial
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
TESTS = ROOT / "tests"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(TESTS))

import git_review
import review_routes
import server
from github_pull_requests import GitHubReadError, PullRequest
from review_fixtures import ReviewRepository


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class FakeReader:
    pull_requests: ClassVar[list[PullRequest]] = []
    available = True
    patch_text = (
        "diff --git a/api.txt b/api.txt\n"
        "new file mode 100644\n"
        "index 0000000..257cc56\n"
        "--- /dev/null\n"
        "+++ b/api.txt\n"
        "@@ -0,0 +1 @@\n"
        "+review api\n"
    )

    def __init__(self, _worktree) -> None:
        pass

    def _available(self) -> None:
        if not self.available:
            raise GitHubReadError("fixture GitHub is unavailable")

    def list(self) -> list[PullRequest]:
        self._available()
        return list(self.pull_requests)

    def get(self, number: int) -> PullRequest:
        self._available()
        return next(item for item in self.pull_requests if item.number == number)

    def patch(self, _number: int) -> str:
        self._available()
        return self.patch_text


class ReviewDispatchTest(unittest.TestCase):
    def test_real_server_dispatches_both_review_resource_families(self) -> None:
        expected = (299, [("X-Review", "yes")], b"review")
        conversation_id = "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        routes = (
            ("GET", f"/api/conversations/{conversation_id}/review-targets?refresh=remote"),
            ("GET", "/api/review-targets/gt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/files"),
            ("POST", f"/api/conversations/{conversation_id}/review-observations"),
            ("GET", "/api/review-observations/" + "a" * 64 + "/patch?file=rf_a"),
        )
        with patch.object(server.review_routes, "handle", return_value=expected) as call:
            for method, path in routes:
                with self.subTest(path=path):
                    self.assertEqual(
                        server.dispatch_http(
                            method,
                            path,
                            "Host: 127.0.0.1\r\n\r\n",
                            b"",
                        ),
                        expected,
                    )
            self.assertEqual(call.call_count, 4)


class ConversationReviewApiTest(unittest.TestCase):
    conversation_id = "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    headers = "Host: 127.0.0.1\r\n\r\n"

    def setUp(self) -> None:
        self.fixture = ReviewRepository()
        self.addCleanup(self.fixture.cleanup)
        self.fixture.branch("feature/review-api")
        self.fixture.write("api.txt", "review api\n")
        self.fixture.write("second.txt", "second\n")
        self.head_sha = self.fixture.commit("review api")
        self.db_path = self.fixture.root / "shell.db"
        con = sqlite3.connect(self.db_path)
        apply_schema(con)
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (1,'Dev','dev','dev','prompt',1,'shell-token')"
        )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (?,1,1,'codex',?,'create','hash')",
            (self.conversation_id, str(self.fixture.repo)),
        )
        con.commit()
        con.close()
        self.original_db_path = review_routes.DB_PATH
        self.original_reader = review_routes.READER_FACTORY
        self.original_cache = review_routes.CACHE_FACTORY
        self.original_fetcher = review_routes.FETCHER
        review_routes.DB_PATH = self.db_path
        review_routes.READER_FACTORY = FakeReader
        review_routes.CACHE_FACTORY = partial(
            git_review.MergedPatchCache,
            self.fixture.root / "review-cache",
        )
        review_routes._REMOTE_ATTEMPTS.clear()
        review_routes._REMOTE_RESULTS.clear()
        review_routes._OBSERVATIONS.clear()
        review_routes._OBSERVATION_KEYS.clear()
        FakeReader.pull_requests = []
        FakeReader.available = True
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        review_routes.DB_PATH = self.original_db_path
        review_routes.READER_FACTORY = self.original_reader
        review_routes.CACHE_FACTORY = self.original_cache
        review_routes.FETCHER = self.original_fetcher
        review_routes._REMOTE_ATTEMPTS.clear()
        review_routes._REMOTE_RESULTS.clear()
        review_routes._OBSERVATIONS.clear()
        review_routes._OBSERVATION_KEYS.clear()
        FakeReader.pull_requests = []
        FakeReader.available = True

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: str | None = None,
        body: bytes = b"",
    ):
        status, response_headers, body = review_routes.handle(
            method,
            path,
            headers or self.headers,
            body,
        )
        value = json.loads(body) if body else None
        return status, dict(response_headers), value

    def target_id(self) -> str:
        status, _headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets"
        )
        self.assertEqual(status, 200, body)
        return next(
            item["target_id"]
            for item in body["items"]
            if item["kind"] == "workspace"
        )

    def observe(self, key: str = "observe-1"):
        return self.request(
            f"/api/conversations/{self.conversation_id}/review-observations",
            method="POST",
            headers=(
                "Host: 127.0.0.1\r\n"
                f"Idempotency-Key: {key}\r\n\r\n"
            ),
        )

    def test_owned_targets_hide_worktree_and_expose_fresh_selection(self) -> None:
        status, headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets"
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["freshness"]["local"], "fresh")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["kind"], "workspace")
        self.assertEqual(
            body["selected_target_id"],
            body["items"][0]["target_id"],
        )
        self.assertNotIn(str(self.fixture.repo), json.dumps(body))

        not_modified, response_headers, response_body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets",
            headers=(
                "Host: 127.0.0.1\r\n"
                f"If-None-Match: {headers['ETag']}\r\n\r\n"
            ),
        )
        self.assertEqual(not_modified, 304)
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertIsNone(response_body)

    def test_dirty_current_workspace_is_recommended_before_its_open_pr(self) -> None:
        workspace_target = self.target_id()
        self.fixture.write("dirty.txt", "not committed\n")
        FakeReader.pull_requests = [
            PullRequest(
                number=816,
                head_ref="feature/review-api",
                base_ref="main",
                head_sha=self.head_sha,
                state="OPEN",
                merged_at=None,
                merge_sha=None,
                title="Review API",
                url="https://example.test/pull/816",
                review_decision=None,
                checks="PENDING",
                checks_failed=False,
            )
        ]
        review_routes._REMOTE_ATTEMPTS.clear()

        status, _headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets"
            "?refresh=remote"
        )

        self.assertEqual(status, 200, body)
        self.assertEqual(body["selected_target_id"], workspace_target)
        self.assertEqual(
            next(
                item["kind"]
                for item in body["items"]
                if item["target_id"] == body["selected_target_id"]
            ),
            "workspace",
        )

    def test_files_diff_and_commits_are_server_selected_and_bounded(self) -> None:
        target_id = self.target_id()
        status, headers, files = self.request(
            f"/api/review-targets/{target_id}/files?scope=review&limit=1"
        )
        self.assertEqual(status, 200, files)
        self.assertEqual(files["items"][0]["path"], "api.txt")
        self.assertTrue(files["items"][0]["file_id"].startswith("rf_"))
        self.assertIsNotNone(files["next_cursor"])
        status, second_headers, second_page = self.request(
            f"/api/review-targets/{target_id}/files?scope=review&limit=1"
            f"&cursor={files['next_cursor']}"
        )
        self.assertEqual(status, 200, second_page)
        self.assertEqual(second_page["items"][0]["path"], "second.txt")
        self.assertNotEqual(headers["ETag"], second_headers["ETag"])

        status, _headers, patch = self.request(
            f"/api/review-targets/{target_id}/diff"
            "?scope=review&path=api.txt"
        )
        self.assertEqual(status, 200, patch)
        self.assertIn("+review api", patch["patch"])

        status, _headers, commits = self.request(
            f"/api/review-targets/{target_id}/commits?limit=1"
        )
        self.assertEqual(status, 200, commits)
        self.assertEqual(commits["items"][0]["sha"], self.head_sha)
        self.assertNotIn("body", commits["items"][0])

        status, _headers, error = self.request(
            f"/api/review-targets/{target_id}/commits?ref=HEAD"
        )
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "VALIDATION_ERROR")

        status, _headers, error = self.request(
            f"/api/review-targets/{target_id}/diff?scope=review&path="
            + quote("../secret", safe="")
        )
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "REVIEW_PATH_INVALID")

        not_modified, _, response_body = self.request(
            f"/api/review-targets/{target_id}/files?scope=review&limit=1",
            headers=(
                "Host: 127.0.0.1\r\n"
                f"If-None-Match: {headers['ETag']}\r\n\r\n"
            ),
        )
        self.assertEqual(not_modified, 304)
        self.assertIsNone(response_body)

    def test_current_worktree_observation_is_retry_safe_and_fingerprinted(self) -> None:
        calls = []
        real_fetcher = review_routes.FETCHER

        def fetcher(worktree):
            calls.append(worktree)
            return real_fetcher(worktree)

        review_routes.FETCHER = fetcher
        self.fixture.write("CLAUDE.md", "claude boot\n")
        self.fixture.write("AGENTS.md", "agents boot\n")

        status, _headers, observed = self.observe()
        retry_status, _retry_headers, retried = self.observe()

        self.assertEqual(status, 201, observed)
        self.assertEqual(retry_status, 200, retried)
        self.assertEqual(len(calls), 1)
        self.assertEqual(retried, observed)
        self.assertEqual(len(observed["fingerprint"]), 64)
        self.assertEqual(observed["status"]["branch"], "feature/review-api")
        self.assertEqual(observed["status"]["ahead_count"], 1)
        self.assertEqual(observed["status"]["behind"], 0)
        self.assertEqual(
            {item["path"] for item in observed["changes"]["branch"]},
            {"api.txt", "second.txt"},
        )
        self.assertEqual(
            {item["name"] for item in observed["shell_files"][:2]},
            {"CLAUDE.md", "AGENTS.md"},
        )
        self.assertNotIn(str(self.fixture.repo), json.dumps(observed))

        review_routes.FETCHER = lambda _worktree: git_review.FetchProjection(
            False,
            "offline",
        )
        second_status, _second_headers, second = self.observe("observe-2")
        retry_status, _retry_headers, retried_again = self.observe()
        self.assertEqual(second_status, 201, second)
        self.assertEqual(second["fingerprint"], observed["fingerprint"])
        self.assertFalse(second["fetch"]["fresh"])
        self.assertEqual(retry_status, 200, retried_again)
        self.assertEqual(retried_again, observed)

    def test_observation_patch_accepts_only_snapshot_files_and_detects_change(self) -> None:
        status, _headers, observed = self.observe()
        self.assertEqual(status, 201, observed)
        api_file = next(
            item for item in observed["changes"]["branch"]
            if item["path"] == "api.txt"
        )

        status, _headers, patch_body = self.request(
            f"/api/review-observations/{observed['fingerprint']}/patch"
            f"?file={api_file['file_id']}"
        )
        self.assertEqual(status, 200, patch_body)
        self.assertEqual(patch_body["section"], "branch")
        self.assertIn("+review api", patch_body["patch"])

        status, _headers, refused = self.request(
            f"/api/review-observations/{observed['fingerprint']}/patch"
            "?file=rf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        self.assertEqual(status, 422, refused)
        self.assertEqual(refused["error"]["code"], "REVIEW_PATH_INVALID")

        self.fixture.write("api.txt", "changed after observation\n")
        status, _headers, changed = self.request(
            f"/api/review-observations/{observed['fingerprint']}/patch"
            f"?file={api_file['file_id']}"
        )
        self.assertEqual(status, 409, changed)
        self.assertEqual(changed["error"]["code"], "REVIEW_SNAPSHOT_CHANGED")

    def test_fetch_failure_uses_stale_base_or_typed_unavailable_base(self) -> None:
        review_routes.FETCHER = lambda _worktree: git_review.FetchProjection(
            False,
            "offline",
        )

        status, _headers, stale = self.observe("stale-base")
        self.assertEqual(status, 201, stale)
        self.assertFalse(stale["fetch"]["fresh"])
        self.assertTrue(stale["fetch"]["base_stale"])
        self.assertTrue(stale["status"]["base_available"])
        self.assertEqual(stale["fetch"]["error"], "offline")

        self.fixture.git("update-ref", "-d", "refs/remotes/origin/main")
        self.fixture.write("dirty.txt", "dirty without base\n")
        status, _headers, unavailable = self.observe("missing-base")
        self.assertEqual(status, 201, unavailable)
        self.assertFalse(unavailable["status"]["base_available"])
        self.assertIsNone(unavailable["status"]["base_sha"])
        self.assertEqual(unavailable["changes"]["branch"], [])
        self.assertEqual(unavailable["changes"]["commits"], [])
        self.assertIn(
            "dirty.txt",
            {item["path"] for item in unavailable["changes"]["dirty"]},
        )

    def test_shell_files_are_exact_deduplicated_and_mismatch_aware(self) -> None:
        con = sqlite3.connect(self.db_path)
        self.conversation_id = "cv_cccccccccccccccccccccccccccccccc"
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (2,'OpenCode Dev','ocdev','dev','prompt',1)"
        )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (?,2,1,'opencode',?,'opencode-create','hash')",
            (self.conversation_id, str(self.fixture.repo)),
        )
        con.execute(
            "INSERT INTO skills (name,description,content) "
            "VALUES ('sample','sample skill','body')"
        )
        skill_id = con.execute(
            "SELECT skill_id FROM skills WHERE name='sample'"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO shell_skills (shell_id,skill_id) VALUES (2,?)",
            (skill_id,),
        )
        con.commit()
        con.close()
        self.fixture.write("CLAUDE.md", "claude boot\n")
        self.fixture.write("AGENTS.md", "agents boot\n")
        mirrored = "---\nname: sample\n---\nexact body\n"
        self.fixture.write(".claude/skills/sample/SKILL.md", mirrored)
        self.fixture.write(".opencode/skills/sample/SKILL.md", mirrored)

        status, _headers, observed = self.observe("shell-identical")
        self.assertEqual(status, 201, observed)
        skill_files = [
            item for item in observed["shell_files"] if item["name"] == "sample"
        ]
        self.assertEqual(len(skill_files), 1)
        self.assertEqual(
            skill_files[0]["paths"],
            [
                ".claude/skills/sample/SKILL.md",
                ".opencode/skills/sample/SKILL.md",
            ],
        )
        self.assertFalse(skill_files[0]["mismatch"])

        status, _headers, content = self.request(
            f"/api/review-observations/{observed['fingerprint']}/shell-file"
            f"?file={skill_files[0]['file_id']}"
        )
        self.assertEqual(status, 200, content)
        self.assertEqual(content["body"], mirrored)
        self.assertEqual(content["paths"], skill_files[0]["paths"])

        self.fixture.write(
            ".opencode/skills/sample/SKILL.md",
            "---\nname: sample\n---\ndifferent mirror\n",
        )
        status, _headers, stable = self.request(
            f"/api/review-observations/{observed['fingerprint']}/shell-file"
            f"?file={skill_files[0]['file_id']}"
        )
        self.assertEqual(status, 200, stable)
        self.assertEqual(stable["body"], mirrored)
        status, _headers, mismatched = self.observe("shell-mismatch")
        self.assertEqual(status, 201, mismatched)
        skill_files = [
            item for item in mismatched["shell_files"] if item["name"] == "sample"
        ]
        self.assertEqual(len(skill_files), 2)
        self.assertTrue(all(item["mismatch"] for item in skill_files))

    def test_shell_file_symlink_binary_and_size_limits_fail_closed(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO skills (name,description,content) "
            "VALUES ('sample','sample skill','body')"
        )
        skill_id = con.execute(
            "SELECT skill_id FROM skills WHERE name='sample'"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO shell_skills (shell_id,skill_id) VALUES (1,?)",
            (skill_id,),
        )
        con.commit()
        con.close()
        self.fixture.write("CLAUDE.md", b"binary\x00body")
        self.fixture.write("AGENTS.md", "x" * (512 * 1024 + 1))
        secret = self.fixture.root / "outside-secret.txt"
        secret.write_text("must not escape\n")
        skill_dir = self.fixture.repo / ".claude" / "skills" / "sample"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").symlink_to(secret)

        status, _headers, observed = self.observe("unsafe-shell-files")

        self.assertEqual(status, 201, observed)
        by_name = {item["name"]: item for item in observed["shell_files"][:2]}
        self.assertFalse(by_name["CLAUDE.md"]["available"])
        self.assertEqual(
            by_name["CLAUDE.md"]["error"],
            "REVIEW_SHELL_FILE_NOT_TEXT",
        )
        self.assertFalse(by_name["AGENTS.md"]["available"])
        self.assertEqual(
            by_name["AGENTS.md"]["error"],
            "REVIEW_SHELL_FILE_TOO_LARGE",
        )
        sample = next(
            item for item in observed["shell_files"] if item["name"] == "sample"
        )
        self.assertFalse(sample["available"])
        self.assertEqual(sample["error"], "REVIEW_SHELL_FILE_UNAVAILABLE")
        self.assertNotIn("must not escape", json.dumps(observed))

    def test_target_ownership_and_operator_auth_do_not_leak(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO users (user_id,username) VALUES (2,'other')")
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (2,'Other','other','dev','prompt',2)"
        )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES ('cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',2,2,"
            "'codex',?,'other-create','other-hash')",
            (str(self.fixture.repo),),
        )
        con.execute(
            "INSERT INTO conversation_git_targets "
            "(target_id,conversation_id,branch_name,base_ref,first_head_sha,"
            "latest_head_sha) VALUES "
            "('gt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
            "'cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','private','main',?,?)",
            (self.head_sha, self.head_sha),
        )
        con.commit()
        con.close()

        status, _headers, body = self.request(
            "/api/review-targets/gt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/files"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "REVIEW_TARGET_NOT_FOUND")

        status, _headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets",
            headers=(
                "Host: 127.0.0.1\r\n"
                "Authorization: Bearer shell-token\r\n\r\n"
            ),
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "OPERATOR_REQUIRED")

        status, _headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets",
            headers=(
                "Host: 127.0.0.1\r\n"
                "Origin: https://attacker.example\r\n"
                "Sec-Fetch-Site: cross-site\r\n\r\n"
            ),
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "NOT_SAME_ORIGIN")

    def test_stale_cursor_and_mid_read_target_change_are_conflicts(self) -> None:
        target_id = self.target_id()
        status, _headers, first_page = self.request(
            f"/api/review-targets/{target_id}/files?scope=review&limit=1"
        )
        self.assertEqual(status, 200, first_page)
        self.fixture.write("later-untracked.txt", "later\n")

        status, _headers, stale = self.request(
            f"/api/review-targets/{target_id}/files?scope=review&limit=1"
            f"&cursor={first_page['next_cursor']}"
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"]["code"], "REVIEW_TARGET_CHANGED")

        original_projection = review_routes._file_projection

        def change_target(row, owner_user_id, scope):
            projection = original_projection(row, owner_user_id, scope)
            con = sqlite3.connect(self.db_path)
            con.execute(
                "UPDATE conversation_git_targets SET latest_head_sha=? "
                "WHERE target_id=?",
                ("f" * 40, target_id),
            )
            con.commit()
            con.close()
            return projection

        with patch.object(review_routes, "_file_projection", change_target):
            status, _headers, changed = self.request(
                f"/api/review-targets/{target_id}/files?scope=review"
            )
        self.assertEqual(status, 409)
        self.assertEqual(changed["error"]["code"], "REVIEW_TARGET_CHANGED")

    def test_exact_pr_is_persisted_and_cached_metadata_survives_outage(self) -> None:
        self.target_id()
        pull_request = PullRequest(
            number=815,
            head_ref="feature/review-api",
            base_ref="main",
            head_sha=self.head_sha,
            state="OPEN",
            merged_at=None,
            merge_sha=None,
            title="Review API",
            url="https://example.test/pull/815",
            review_decision="APPROVED",
            checks="SUCCESS",
            checks_failed=False,
        )
        FakeReader.pull_requests = [pull_request]
        review_routes._REMOTE_ATTEMPTS.clear()

        status, _headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets"
            "?refresh=remote"
        )
        self.assertEqual(status, 200, body)
        pr_item = next(item for item in body["items"] if item["pr_number"] == 815)
        self.assertEqual(pr_item["lifecycle"], "pr_open")
        self.assertEqual(pr_item["freshness"]["remote"], "fresh")

        status, _headers, files = self.request(
            f"/api/review-targets/{pr_item['target_id']}/files?scope=review"
        )
        self.assertEqual(status, 200, files)
        self.assertEqual(files["items"][0]["path"], "api.txt")

        FakeReader.available = False
        review_routes._REMOTE_ATTEMPTS.clear()
        status, _headers, offline = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets"
            "?refresh=remote"
        )
        self.assertEqual(status, 200, offline)
        cached = next(item for item in offline["items"] if item["pr_number"] == 815)
        self.assertEqual(cached["freshness"]["remote"], "cached")
        self.assertEqual(offline["freshness"]["remote"], "unavailable")
        self.assertEqual(cached["title"], "Review API")

    def test_multiple_pr_identities_and_merged_patch_cache_survive_cleanup(
        self,
    ) -> None:
        self.target_id()
        open_pr = PullRequest(
            number=816,
            head_ref="feature/review-api",
            base_ref="main",
            head_sha=self.head_sha,
            state="OPEN",
            merged_at=None,
            merge_sha=None,
            title="Later delivery",
            url="https://example.test/pull/816",
            review_decision=None,
            checks="PENDING",
            checks_failed=False,
        )
        merged_pr = PullRequest(
            number=815,
            head_ref="feature/review-api",
            base_ref="main",
            head_sha=self.head_sha,
            state="MERGED",
            merged_at="2026-07-30T20:29:37Z",
            merge_sha="e" * 40,
            title="Merged delivery",
            url="https://example.test/pull/815",
            review_decision="APPROVED",
            checks="SUCCESS",
            checks_failed=False,
        )
        FakeReader.pull_requests = [open_pr, merged_pr]
        review_routes._REMOTE_ATTEMPTS.clear()

        status, _headers, body = self.request(
            f"/api/conversations/{self.conversation_id}/review-targets"
            "?refresh=remote"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            {item["pr_number"] for item in body["items"] if item["pr_number"]},
            {815, 816},
        )
        merged_target = next(
            item for item in body["items"] if item["pr_number"] == 815
        )

        status, _headers, fresh = self.request(
            f"/api/review-targets/{merged_target['target_id']}/files"
            "?scope=review"
        )
        self.assertEqual(status, 200, fresh)
        self.assertEqual(fresh["freshness"], "fresh")
        con = sqlite3.connect(self.db_path)
        artifact = con.execute(
            "SELECT patch_artifact,patch_sha256 "
            "FROM conversation_git_targets WHERE target_id=?",
            (merged_target["target_id"],),
        ).fetchone()
        con.close()
        self.assertIsNotNone(artifact[0])
        self.assertFalse(artifact[0].startswith("/"))
        self.assertEqual(len(artifact[1]), 64)

        FakeReader.available = False
        status, _headers, cached = self.request(
            f"/api/review-targets/{merged_target['target_id']}/files"
            "?scope=review"
        )
        self.assertEqual(status, 200, cached)
        self.assertEqual(cached["freshness"], "cached")
        self.assertEqual(cached["items"][0]["path"], "api.txt")


if __name__ == "__main__":
    unittest.main()
