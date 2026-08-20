#!/usr/bin/env python3
"""Regression coverage for the guarded map-extractor install route."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import map_extractor_install  # noqa: E402
import map_repo  # noqa: E402


VALID_V1 = b"def extract(con, repo_root, cfg):\n    return 'v1'\n"
VALID_V2 = b"def extract(con, repo_root, cfg):\n    return 'v2'\n"


class ExtractorInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / "live"
        self.worktree = self.root / "cart-worktree"
        self.live.mkdir()
        self.worktree.mkdir()
        self.source_dir = self.worktree / ".sc-state" / "map_extractors"
        self.source_dir.mkdir(parents=True)
        self.local = self.root / "engine-state" / ".sc-state" / "local"
        self.map_root_patch = mock.patch.object(map_repo, "MAP_ROOT", self.live)
        self.local_patch = mock.patch.object(artifact_policy, "LOCAL_DIR", self.local)
        self.map_root_patch.start()
        self.local_patch.start()
        self.addCleanup(self.map_root_patch.stop)
        self.addCleanup(self.local_patch.stop)
        self.env = {
            "SC_SHELL_FLAVOR": "cartographer",
            "SC_SHELL_WORKTREE": str(self.worktree),
        }

    def write_candidate(
        self,
        body: bytes = VALID_V1,
        name: str = "routes.py",
        source_dir: Path | None = None,
    ) -> Path:
        directory = source_dir or self.source_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(body)
        return path

    def init_git(self, source: Path) -> str:
        commands = (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "cart@example.invalid"],
            ["git", "config", "user.name", "Cartographer"],
            ["git", "add", source.relative_to(self.worktree).as_posix()],
            ["git", "commit", "-qm", "add extractor"],
        )
        for command in commands:
            subprocess.run(command, cwd=self.worktree, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_valid_tracked_install_records_exact_bytes_and_is_idempotent(self):
        source = self.write_candidate()
        head = self.init_git(source)
        candidate = map_extractor_install.validate_candidate(str(source), self.env)

        first = map_extractor_install.install_extractor(candidate)
        target_mtime = first.target.stat().st_mtime_ns
        receipt_mtime = first.receipt.stat().st_mtime_ns
        second = map_extractor_install.install_extractor(candidate)

        expected_digest = hashlib.sha256(VALID_V1).hexdigest()
        self.assertEqual(VALID_V1, first.target.read_bytes())
        self.assertEqual(expected_digest, first.digest)
        self.assertTrue(first.source_tracked)
        self.assertEqual(head, first.source_git_ref)
        receipt = json.loads(first.receipt.read_text())
        self.assertEqual("routes.py", receipt["extractor"])
        self.assertEqual(expected_digest, receipt["digest"])
        self.assertEqual(".sc-state/map_extractors/routes.py", receipt["source_path"])
        self.assertEqual(str(self.worktree), receipt["source_worktree"])
        self.assertEqual(head, receipt["source_git_ref"])
        self.assertFalse(second.changed)
        self.assertEqual(target_mtime, second.target.stat().st_mtime_ns)
        self.assertEqual(receipt_mtime, second.receipt.stat().st_mtime_ns)

    def test_update_reports_old_digest_and_refreshes_pending_source(self):
        source = self.write_candidate()
        candidate = map_extractor_install.validate_candidate(str(source), self.env)
        first = map_extractor_install.install_extractor(candidate)
        source.write_bytes(VALID_V2)

        second_candidate = map_extractor_install.validate_candidate(str(source), self.env)
        second = map_extractor_install.install_extractor(second_candidate)

        self.assertEqual(first.digest, second.old_digest)
        self.assertEqual(VALID_V2, second.target.read_bytes())
        self.assertFalse(second.source_tracked)
        self.assertEqual(second.digest, json.loads(second.receipt.read_text())["digest"])

    def test_receipt_failure_restores_prior_target_and_receipt(self):
        source = self.write_candidate()
        first = map_extractor_install.install_extractor(
            map_extractor_install.validate_candidate(str(source), self.env)
        )
        old_target = first.target.read_bytes()
        old_receipt = first.receipt.read_bytes()
        source.write_bytes(VALID_V2)
        candidate = map_extractor_install.validate_candidate(str(source), self.env)

        with mock.patch.object(
            map_extractor_install,
            "_persist_receipt",
            side_effect=OSError("fixture receipt failure"),
        ):
            with self.assertRaisesRegex(
                map_extractor_install.ExtractorInstallError,
                "prior extractor and receipt restored",
            ):
                map_extractor_install.install_extractor(candidate)

        self.assertEqual(old_target, first.target.read_bytes())
        self.assertEqual(old_receipt, first.receipt.read_bytes())

    def test_invalid_candidates_fail_before_live_state_is_created(self):
        valid = self.write_candidate()
        cases: list[tuple[str, Path, dict[str, str], str]] = []
        cases.append(("missing identity", valid, {}, "missing launched-shell identity"))
        cases.append((
            "wrong flavor",
            valid,
            {**self.env, "SC_SHELL_FLAVOR": "dev"},
            "only a launched Cartographer",
        ))
        outside = self.root / "outside.py"
        outside.write_bytes(VALID_V1)
        cases.append(("escape", outside, self.env, "direct child"))
        reserved = self.write_candidate(name="_helper.py")
        cases.append(("reserved", reserved, self.env, "reserved"))
        unsafe = self.write_candidate(name="bad-name.py")
        cases.append(("unsafe", unsafe, self.env, "filename must match"))
        wrong_suffix = self.write_candidate(name="routes.txt")
        cases.append(("suffix", wrong_suffix, self.env, "filename must match"))
        invalid_utf8 = self.write_candidate(body=b"\xff\xfe", name="utf8.py")
        cases.append(("utf8", invalid_utf8, self.env, "valid UTF-8"))
        invalid_syntax = self.write_candidate(body=b"def extract(:\n", name="syntax.py")
        cases.append(("syntax", invalid_syntax, self.env, "syntax validation failed"))
        invalid_signature = self.write_candidate(
            body=b"def extract(con, cfg):\n    return None\n",
            name="signature.py",
        )
        cases.append(("signature", invalid_signature, self.env, "positional parameters"))
        symlink = self.source_dir / "linked.py"
        os.symlink(outside, symlink)
        cases.append(("symlink", symlink, self.env, "non-symlink"))

        for label, path, env, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    map_extractor_install.ExtractorInstallError,
                    message,
                ):
                    map_extractor_install.validate_candidate(str(path), env)

        self.assertFalse((self.live / ".sc-state").exists())
        self.assertFalse(self.local.exists())

    def test_concurrent_installs_leave_matching_target_and_receipt(self):
        other_worktree = self.root / "other-cart-worktree"
        other_source_dir = other_worktree / ".sc-state" / "map_extractors"
        first_source = self.write_candidate(VALID_V1)
        second_source = self.write_candidate(
            VALID_V2,
            source_dir=other_source_dir,
        )
        second_env = {
            "SC_SHELL_FLAVOR": "cartographer",
            "SC_SHELL_WORKTREE": str(other_worktree),
        }
        candidates = (
            map_extractor_install.validate_candidate(str(first_source), self.env),
            map_extractor_install.validate_candidate(str(second_source), second_env),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(map_extractor_install.install_extractor, candidates))

        target = results[0].target
        receipt = json.loads(results[0].receipt.read_text())
        target_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(receipt["digest"], target_digest)
        self.assertIn(target.read_bytes(), (VALID_V1, VALID_V2))
        self.assertIn(receipt["source_worktree"], {str(self.worktree), str(other_worktree)})

    def test_symlinked_canonical_directory_cannot_redirect_install(self):
        source = self.write_candidate()
        candidate = map_extractor_install.validate_candidate(str(source), self.env)
        outside = self.root / "redirected"
        outside.mkdir()
        os.symlink(outside, self.live / ".sc-state")

        with self.assertRaisesRegex(
            map_extractor_install.ExtractorInstallError,
            "symlinked install path",
        ):
            map_extractor_install.install_extractor(candidate)

        self.assertFalse((outside / "map_extractors" / "routes.py").exists())

    def test_dispatcher_exposes_only_the_dedicated_install_module(self):
        dispatch = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertIn(
            'map-extractor) exec "$PY" "$S/map_extractor_install.py" "$@" ;;',
            dispatch,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
