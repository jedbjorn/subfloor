#!/usr/bin/env python3
"""Tests for shell_liveness orphan detection: the pure classifier
(classify_orphan), the guard-shaping helper (orphan_split), and a compute()
smoke pass against the live /proc.

Stdlib `unittest`, matching the sibling suites.

Run:
    python3 tests/test_shell_liveness.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import run
import shell_liveness  # noqa: E402


class ClassifyOrphanTest(unittest.TestCase):
    """The pure verdict: (tty_nr, ppid, tty_fd, tty_exists) → orphan state."""

    def test_attached_normal_session(self):
        # Interactive session, pty alive → not an orphan.
        self.assertIsNone(shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3", True))

    def test_tty_gone_deleted_suffix(self):
        # Terminal window closed; readlink flags the dead pty.
        self.assertEqual("tty-gone", shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3 (deleted)", True))

    def test_tty_gone_device_missing(self):
        # Same closure, no (deleted) marker — the pts node just isn't there.
        self.assertEqual("tty-gone", shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3", False))

    def test_detached_reparented_to_init(self):
        # Headless survivor: no controlling TTY, parent gone → init.
        self.assertEqual("detached", shell_liveness.classify_orphan(
            0, 1, None, None))

    def test_headless_with_live_parent_is_not_orphaned(self):
        # A NORMAL headless boot: no TTY but its spawner is alive.
        self.assertIsNone(shell_liveness.classify_orphan(0, 4242, None, None))

    def test_missing_stat_is_conservative(self):
        # No /proc data → never call an orphan.
        self.assertIsNone(shell_liveness.classify_orphan(None, None, None, None))

    def test_tty_present_but_stdio_redirected_is_conservative(self):
        # tty_nr says attached, but no stdio fd resolves to a tty → no verdict.
        self.assertIsNone(shell_liveness.classify_orphan(34816, 4242, None, None))

    def test_unknown_tty_existence_is_conservative(self):
        # The classifier is namespace-BLIND: it is handed the answer, and an
        # unknown answer stays unknown. It must never go and test the path in
        # the scanner's own namespace — that is a different question about a
        # different device, and answering it confidently is the U1 Part 2 bug.
        self.assertIsNone(shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3", None))


class OrphanSplitTest(unittest.TestCase):
    """orphan_split shapes the sc run guard: (all pids, orphaned pids)."""

    SNAP = {
        "processes": [
            {"pid": 100, "shortname": "dev1", "orphaned": "tty-gone"},
            {"pid": 101, "shortname": "dev1", "orphaned": None},
            {"pid": 200, "shortname": "dev2", "orphaned": "detached"},
            {"pid": 300, "shortname": None, "orphaned": None},  # admin root
        ],
    }

    def test_mixed_shell_is_not_all_orphaned(self):
        pids, orphans = shell_liveness.orphan_split("dev1", self.SNAP)
        self.assertEqual([100, 101], pids)
        self.assertEqual([100], orphans)
        self.assertNotEqual(len(pids), len(orphans))

    def test_fully_orphaned_shell(self):
        pids, orphans = shell_liveness.orphan_split("dev2", self.SNAP)
        self.assertEqual(pids, orphans)
        self.assertEqual([200], orphans)

    def test_case_insensitive_shortname(self):
        pids, _ = shell_liveness.orphan_split("DEV2", self.SNAP)
        self.assertEqual([200], pids)

    def test_unknown_shell_is_empty(self):
        self.assertEqual(([], []), shell_liveness.orphan_split("ghost", self.SNAP))


class SessionStateTest(unittest.TestCase):
    """session_state shapes the picker annotation: 'busy' / 'orphan' / None."""

    SNAP = {
        "supported": True,
        "processes": [
            {"pid": 100, "shortname": "dev1", "orphaned": "tty-gone"},
            {"pid": 101, "shortname": "dev1", "orphaned": None},
            {"pid": 200, "shortname": "dev2", "orphaned": "detached"},
            {"pid": 300, "shortname": None, "orphaned": None},  # admin root
        ],
    }

    def test_live_session_wins_over_orphan_sibling(self):
        # One working session among orphans → someone is there → busy.
        self.assertEqual("busy", shell_liveness.session_state("dev1", self.SNAP))

    def test_all_orphaned_is_orphan(self):
        self.assertEqual("orphan", shell_liveness.session_state("dev2", self.SNAP))

    def test_dormant_shell_is_none(self):
        self.assertIsNone(shell_liveness.session_state("ghost", self.SNAP))

    def test_case_insensitive_shortname(self):
        self.assertEqual("orphan", shell_liveness.session_state("DEV2", self.SNAP))

    def test_unsupported_snapshot_is_none(self):
        # Non-Linux: no /proc → no verdicts, the picker degrades to unmarked.
        self.assertIsNone(shell_liveness.session_state(
            "dev2", {"supported": False, "processes": self.SNAP["processes"]}))


class AdminPresenceTest(unittest.TestCase):
    """Cleanup requires positive evidence that the current Admin owns root."""

    def test_missing_self_identity_is_indeterminate_and_unsafe(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(shell_liveness, "PROC", Path(td)), \
                mock.patch.object(shell_liveness, "harness_binaries",
                                  return_value={"codex"}), \
                mock.patch.object(shell_liveness, "_shell_labels", return_value={}):
            snap = shell_liveness.compute()

        self.assertIsNone(snap["self_pid"])
        self.assertEqual("indeterminate", snap["admin_presence"])
        self.assertFalse(snap["safe_to_clean_all"])

        output = io.StringIO()
        with redirect_stdout(output):
            shell_liveness._print_text(snap)
        self.assertIn("admin_presence=indeterminate", output.getvalue())
        self.assertIn("cleanup remains unsafe", output.getvalue())

    def test_matched_root_self_is_positive_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            proc = Path(td)
            process = proc / "123"
            process.mkdir()
            (process / "comm").write_text("codex\n")
            (process / "cwd").symlink_to(shell_liveness.REPO_ROOT)

            with mock.patch.object(shell_liveness, "PROC", proc), \
                    mock.patch.object(shell_liveness, "harness_binaries",
                                      return_value={"codex"}), \
                    mock.patch.object(shell_liveness, "_shell_labels",
                                      return_value={}), \
                    mock.patch.object(shell_liveness, "_self_harness_pid",
                                      return_value=123), \
                    mock.patch.object(shell_liveness, "_tty_nr", return_value=0), \
                    mock.patch.object(shell_liveness, "_ppid", return_value=2), \
                    mock.patch.object(shell_liveness, "_tty_fd", return_value=None):
                snap = shell_liveness.compute()

        self.assertEqual("present", snap["admin_presence"])
        self.assertEqual([123], snap["admin_root_pids"])
        self.assertTrue(snap["safe_to_clean_all"])


def _adapter(root: Path, name: str, spec: "dict | None") -> None:
    """One adapters/<name>/ dir; spec None = a dir with no adapter.json."""
    d = root / name
    d.mkdir()
    if spec is not None:
        (d / "adapter.json").write_text(json.dumps(spec))


class HarnessBinariesTest(unittest.TestCase):
    """comm is the RUNTIME name — the expected set unions launch[0],
    headless.launch[0] and the adapter's declared comm_aliases."""

    def _bins(self, build) -> set:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build(root)
            with mock.patch.object(shell_liveness, "ADAPTERS", root):
                return shell_liveness.harness_binaries()

    def test_comm_alias_is_matched(self):
        bins = self._bins(lambda r: _adapter(r, "kimi", {
            "launch": ["kimi"], "comm_aliases": ["kimi-code"]}))
        self.assertIn("kimi-code", bins)

    def test_headless_launch_binary_is_matched(self):
        # A harness whose headless entry point differs from its interactive one
        # is live under BOTH names; matching only launch[0] misses half of them.
        bins = self._bins(lambda r: _adapter(r, "h", {
            "launch": ["h-tui"], "headless": {"launch": ["h-batch"]}}))
        self.assertIn("h-tui", bins)
        self.assertIn("h-batch", bins)

    def test_long_alias_truncated_to_comm_width(self):
        # /proc/<pid>/comm is capped at 15 chars, so a longer declared alias
        # must be compared in its truncated form or it can never match.
        long = "a-very-long-harness-name"
        bins = self._bins(lambda r: _adapter(r, "long", {
            "launch": ["short"], "comm_aliases": [long]}))
        self.assertIn(long[:15], bins)
        self.assertNotIn(long, bins)
        self.assertTrue(all(len(b) <= 15 for b in bins))

    def test_alias_is_basenamed(self):
        bins = self._bins(lambda r: _adapter(r, "p", {
            "launch": ["p"], "comm_aliases": ["/opt/vendor/bin/p-runtime"]}))
        self.assertIn("p-runtime", bins)

    def test_duplicate_aliases_across_adapters_union_harmlessly(self):
        def build(r):
            _adapter(r, "a", {"launch": ["a"], "comm_aliases": ["shared-rt"]})
            _adapter(r, "b", {"launch": ["b"], "comm_aliases": ["shared-rt"]})
        bins = self._bins(build)
        self.assertIn("shared-rt", bins)

    def test_adapter_dir_without_json_leaves_the_fallback_floor(self):
        bins = self._bins(lambda r: _adapter(r, "empty", None))
        self.assertEqual(shell_liveness._FALLBACK_BINS, bins)

    def test_malformed_adapter_json_is_skipped_not_fatal(self):
        def build(r):
            (r / "broken").mkdir()
            (r / "broken" / "adapter.json").write_text("{not json")
            _adapter(r, "ok", {"launch": ["ok"], "comm_aliases": ["ok-rt"]})
        bins = self._bins(build)
        self.assertIn("ok-rt", bins)

    def test_adapter_without_aliases_is_unaffected(self):
        bins = self._bins(lambda r: _adapter(r, "plain", {"launch": ["plain"]}))
        self.assertIn("plain", bins)


class KimiCommAliasTest(unittest.TestCase):
    """Against the REAL adapters dir. A live headless kimi execs
    /usr/local/bin/kimi and then renames itself `kimi-code` (PR_SET_NAME) —
    captured from a real worker, not quoted from a transcript. Unaliased, the
    worker matched nothing and its shell projected `available`, inviting the
    operator to double-book a shell that was busy."""

    def test_kimi_runtime_comm_is_recognised(self):
        self.assertIn("kimi-code", shell_liveness.harness_binaries())


class NamespaceAwareTtyTest(unittest.TestCase):
    """The pty existence question is asked in the PROCESS's mount namespace.

    Every case below uses a tty path that does NOT exist in the scanner's own
    namespace, so a check against the bare path cannot pass them by accident.
    """

    TTY = "/dev/pts/99999"           # absent in this test runner's namespace

    def _proc(self, td: str, *, tty=TTY, ns_has_tty=True, ns_readable=True,
              tty_nr=34816, ppid=4242) -> Path:
        """A fake /proc with one pid: comm, stat, cwd → this repo, fd/0 → tty,
        and root → its own namespace root (which may or may not hold the tty
        device). comm + cwd are what let compute() pick the pid up as a live
        session, so the same fixture serves both the seam tests below and the
        end-to-end ones."""
        proc = Path(td) / "proc"
        entry = proc / "4242"
        (entry / "fd").mkdir(parents=True)
        entry.joinpath("comm").write_text("kimi-code\n")
        entry.joinpath("stat").write_text(
            f"4242 (kimi-code) S {ppid} 4242 4242 {tty_nr} -1 0 0 0\n")
        entry.joinpath("cwd").symlink_to(shell_liveness.REPO_ROOT)
        entry.joinpath("fd", "0").symlink_to(tty)
        if ns_readable:
            ns_root = Path(td) / "nsroot"
            (ns_root / tty.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
            if ns_has_tty:
                (ns_root / tty.lstrip("/")).write_text("")
            entry.joinpath("root").symlink_to(ns_root)
        return proc

    def test_live_container_pty_is_not_an_orphan(self):
        # The repro: a tmux-hosted session holding a pty from the container's
        # devpts, scanned from the host. The device is absent at the bare path
        # and present through the process's own root → the session is LIVE.
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td)
            self.assertFalse(os.path.exists(self.TTY),
                             "fixture invalid: tty must be absent to the scanner")
            with mock.patch.object(shell_liveness, "PROC", proc):
                self.assertTrue(shell_liveness._tty_exists(4242, self.TTY))
                self.assertIsNone(shell_liveness._orphan_verdict(4242))

    def test_dead_pty_still_classifies_tty_gone(self):
        # The regression case: same vantage, but the device is genuinely gone
        # inside the process's own namespace too. Still an orphan.
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td, ns_has_tty=False)
            with mock.patch.object(shell_liveness, "PROC", proc):
                self.assertFalse(shell_liveness._tty_exists(4242, self.TTY))
                self.assertEqual("tty-gone",
                                 shell_liveness._orphan_verdict(4242))

    def test_unreadable_namespace_yields_no_verdict(self):
        # /proc/<pid>/root unreadable (foreign user) or the process exited
        # mid-scan: we cannot tell, so we do not accuse. os.path.exists would
        # have folded this into False and called a live session an orphan.
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td, ns_readable=False)
            with mock.patch.object(shell_liveness, "PROC", proc):
                self.assertIsNone(shell_liveness._tty_exists(4242, self.TTY))
                self.assertIsNone(shell_liveness._orphan_verdict(4242))

    def test_unresolvable_tty_path_is_not_an_absent_device(self):
        # ONLY "the device is not there" (ENOENT) may become a tty-gone verdict.
        # Any other failure to resolve the path — here a non-directory component
        # inside the namespace — is us being unable to answer, and an unanswered
        # question must not convict a live session. Distinct from the case
        # above: /proc/<pid>/root itself stats fine, so the first guard passes
        # and it is the SECOND lookup that has to stay conservative.
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td, ns_has_tty=False)
            pts = Path(td) / "nsroot" / "dev" / "pts"
            pts.rmdir()
            pts.write_text("not a directory")
            with mock.patch.object(shell_liveness, "PROC", proc):
                self.assertIsNone(shell_liveness._tty_exists(4242, self.TTY))
                self.assertIsNone(shell_liveness._orphan_verdict(4242))

    def test_no_tty_fd_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td)
            with mock.patch.object(shell_liveness, "PROC", proc):
                self.assertIsNone(shell_liveness._tty_exists(4242, None))

    def test_detached_verdict_survives_the_new_seam(self):
        # tty_nr == 0 and reparented to init: decided before the pty question
        # is ever asked, so the namespace change must not disturb it.
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td, tty_nr=0, ppid=1)
            with mock.patch.object(shell_liveness, "PROC", proc):
                self.assertEqual("detached",
                                 shell_liveness._orphan_verdict(4242))

    def _snapshot(self, **kw) -> dict:
        """compute() over the same fake /proc — the end-to-end vantage."""
        with tempfile.TemporaryDirectory() as td:
            proc = self._proc(td, **kw)
            with mock.patch.object(shell_liveness, "PROC", proc), \
                    mock.patch.object(shell_liveness, "harness_binaries",
                                      return_value={"kimi-code"}), \
                    mock.patch.object(shell_liveness, "_shell_labels",
                                      return_value={}):
                return shell_liveness.compute()

    # The two below are the only tests that pin compute()'s orphan seam. Every
    # case above drives _orphan_verdict directly, so dropping compute()'s
    # tty_exists argument — the pre-PR call shape, and exactly what a partial
    # revert or conflict resolution restores — leaves them all green while
    # silently disabling every existence-based tty-gone verdict in production.

    def test_compute_reports_a_dead_pty_as_tty_gone(self):
        snap = self._snapshot(ns_has_tty=False)
        self.assertEqual(["tty-gone"], [p["orphaned"] for p in snap["processes"]])
        self.assertEqual([4242], snap["orphaned_pids"])

    def test_compute_leaves_a_live_container_pty_unaccused(self):
        # Present through the process's own root, absent at the bare path: a
        # scanner-namespace check would convict this live session here.
        snap = self._snapshot()
        self.assertEqual([None], [p["orphaned"] for p in snap["processes"]])
        self.assertEqual([], snap["orphaned_pids"])


class ZombieHarnessTest(unittest.TestCase):
    """A zombie keeps its comm, so it still LOOKS like a harness — but it has
    exited, holds no worktree, and its cwd link is empty. Counting it files it
    under `indeterminate`, which pins safe_to_clean_all False permanently on the
    word of a process that is gone. Container PID 1 is frequently not a reaper,
    so these accumulate and never clear (ruled into U1 by the planner, #1660)."""

    def _snapshot(self, state: str, *, with_cwd: bool):
        with tempfile.TemporaryDirectory() as td:
            proc = Path(td) / "proc"
            entry = proc / "4242"
            entry.mkdir(parents=True)
            entry.joinpath("comm").write_text("kimi-code\n")
            entry.joinpath("stat").write_text(
                f"4242 (kimi-code) {state} 1 4242 4242 0 -1 0 0 0\n")
            if with_cwd:
                entry.joinpath("cwd").symlink_to(shell_liveness.REPO_ROOT)
            with mock.patch.object(shell_liveness, "PROC", proc), \
                    mock.patch.object(shell_liveness, "harness_binaries",
                                      return_value={"kimi-code"}), \
                    mock.patch.object(shell_liveness, "_shell_labels",
                                      return_value={}):
                return shell_liveness.compute()

    def test_zombie_harness_is_not_a_live_session(self):
        # No cwd link — exactly what a real zombie looks like in /proc.
        snap = self._snapshot("Z", with_cwd=False)
        self.assertEqual([], snap["processes"])
        self.assertEqual([], snap["indeterminate_pids"])
        self.assertEqual(0, snap["indeterminate"])

    def test_zombie_does_not_pin_the_cleanup_gate(self):
        # The consequence that matters: a dead process must not be able to hold
        # the admin's cleanup gate shut forever.
        snap = self._snapshot("Z", with_cwd=False)
        self.assertNotIn(4242, snap["admin_root_pids"])
        self.assertEqual([], snap["active_other_shells"])

    def test_zombie_never_holds_a_shell_slot(self):
        # Even with a readable cwd inside the repo, an exited process must not
        # be attributed to a shell — that is what blocks a headless re-boot.
        snap = self._snapshot("Z", with_cwd=True)
        self.assertEqual([], snap["processes"])
        self.assertEqual({}, snap["worktree_sessions"])

    def test_live_harness_with_the_same_comm_is_still_counted(self):
        # The guard keys on state, not on the name — a running process with the
        # identical comm must survive it, or the guard has eaten the feature.
        snap = self._snapshot("S", with_cwd=True)
        self.assertEqual([4242], [p["pid"] for p in snap["processes"]])

    def test_live_harness_with_unreadable_cwd_is_still_indeterminate(self):
        # The honesty gap the scan DOES own (roadmap #24's case) is untouched.
        snap = self._snapshot("S", with_cwd=False)
        self.assertEqual([4242], snap["indeterminate_pids"])
        self.assertFalse(snap["safe_to_clean_all"])


def _stat_line(*, state: str = "S", ppid: int = 1, tty_nr: int = 0,
               start_ticks: int = 5150) -> str:
    """A /proc/<pid>/stat line with the four fields this module reads.
    The comm deliberately carries a space and parens — the parser must survive
    it, and a fixture that never exercises that is a fixture that agrees with a
    naive split()."""
    rest = ["0"] * 30
    rest[0] = state                    # field 3  — state
    rest[1] = str(ppid)                # field 4  — ppid
    rest[4] = str(tty_nr)              # field 7  — tty_nr
    rest[19] = str(start_ticks)        # field 22 — starttime
    return "42 (co dex (x)) " + " ".join(rest) + "\n"


def _proc_entry(proc: Path, pid: int, *, cwd: "Path | None" = None,
                comm: str = "codex", **stat) -> Path:
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "comm").write_text(comm + "\n")
    (entry / "stat").write_text(_stat_line(**stat))
    if cwd is not None:
        (entry / "cwd").symlink_to(cwd)
    return entry


class ClaimLiveTest(unittest.TestCase):
    """H-25: identity is pid + start ticks + worktree hold. Parentage never."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.worktree = self.root / ".sc-worktrees" / "dev6"
        self.worktree.mkdir(parents=True)
        self.claim = {"pid": 700, "start_ticks": 5150,
                      "worktree": str(self.worktree)}
        patch = mock.patch.object(shell_liveness, "PROC", self.proc)
        patch.start()
        self.addCleanup(patch.stop)

    def test_detached_worker_reparented_to_init_is_live(self):
        # The exact process H-25 exists for: tty_nr 0, ppid 1 — 'detached' by
        # every lineage read, and working.
        _proc_entry(self.proc, 700, cwd=self.worktree, ppid=1, tty_nr=0)
        self.assertTrue(shell_liveness.claim_live(self.claim))

    def test_recycled_pid_does_not_inherit_the_claim(self):
        # Same pid number, different process. Without start_ticks this is a
        # false "working" that resolves the reconciler's alert on a stranger.
        _proc_entry(self.proc, 700, cwd=self.worktree, start_ticks=99999)
        self.assertFalse(shell_liveness.claim_live(self.claim))

    def test_zombie_keeps_its_ticks_but_holds_nothing(self):
        _proc_entry(self.proc, 700, cwd=self.worktree, state="Z")
        self.assertFalse(shell_liveness.claim_live(self.claim))

    def test_pid_that_is_simply_gone_is_not_live(self):
        self.assertFalse(shell_liveness.claim_live(self.claim))

    def test_cwd_outside_the_worktree_refutes_the_hold(self):
        elsewhere = self.root / "somewhere-else"
        elsewhere.mkdir()
        _proc_entry(self.proc, 700, cwd=elsewhere)
        self.assertFalse(shell_liveness.claim_live(self.claim))

    def test_subdirectory_of_the_worktree_still_holds_it(self):
        nested = self.worktree / "src"
        nested.mkdir()
        _proc_entry(self.proc, 700, cwd=nested)
        self.assertTrue(shell_liveness.claim_live(self.claim))

    def test_unreadable_cwd_does_not_refute(self):
        # Missing data must never become "the worker is gone" — that is the
        # failure direction this requirement removes.
        _proc_entry(self.proc, 700, cwd=None)
        self.assertTrue(shell_liveness.claim_live(self.claim))


class RecordStateTest(unittest.TestCase):
    """The launch record's verdict, asked after the process scan found none."""

    SNAP = {
        "supported": True,
        "claimed_pids": {"dev6": 700},
        "claimed_absent": ["dev5"],
    }

    def test_live_claim_is_working(self):
        self.assertEqual("working",
                         shell_liveness.record_state("DEV6", self.SNAP))

    def test_dead_claim_is_expected_absent_not_available(self):
        self.assertEqual("expected_absent",
                         shell_liveness.record_state("dev5", self.SNAP))

    def test_unclaimed_shell_has_no_record_verdict(self):
        self.assertIsNone(shell_liveness.record_state("dev1", self.SNAP))

    def test_unsupported_snapshot_is_none(self):
        self.assertIsNone(shell_liveness.record_state(
            "dev5", {**self.SNAP, "supported": False}))


class ClaimedOrphanTest(unittest.TestCase):
    """A claimed pid is never advised for killing, however it was launched."""

    SNAP = {
        "supported": True,
        "processes": [
            {"pid": 700, "shortname": "dev6", "orphaned": "detached",
             "claimed": True},
            {"pid": 800, "shortname": "dev5", "orphaned": "detached",
             "claimed": False},
        ],
    }

    def test_claimed_detached_worker_is_not_in_the_orphan_half(self):
        pids, orphans = shell_liveness.orphan_split("dev6", self.SNAP)
        self.assertEqual([700], pids)
        self.assertEqual([], orphans)

    def test_claimed_detached_worker_reads_busy_not_orphan(self):
        self.assertEqual("busy", shell_liveness.session_state("dev6", self.SNAP))

    def test_unclaimed_detached_remnant_is_still_an_orphan(self):
        # The positive control: the rule narrows the orphan verdict, it does
        # not delete it.
        self.assertEqual([800],
                         shell_liveness.orphan_split("dev5", self.SNAP)[1])
        self.assertEqual("orphan",
                         shell_liveness.session_state("dev5", self.SNAP))


class ComputeWithClaimsTest(unittest.TestCase):
    """compute() end to end over a fake /proc plus a launch record."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.worktree = self.root / ".sc-worktrees" / "dev6"
        self.worktree.mkdir(parents=True)
        for patch in (
            mock.patch.object(shell_liveness, "PROC", self.proc),
            mock.patch.object(shell_liveness, "REPO_ROOT", self.root),
            mock.patch.object(shell_liveness, "harness_binaries",
                              return_value={"codex"}),
            mock.patch.object(shell_liveness, "_shell_labels", return_value={}),
            mock.patch.object(shell_liveness, "_self_harness_pid",
                              return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _claims(self, claims):
        patch = mock.patch.object(shell_liveness, "_launch_claims",
                                  return_value=claims)
        patch.start()
        self.addCleanup(patch.stop)

    def test_claimed_detached_worker_is_working_not_unreconciled(self):
        _proc_entry(self.proc, 700, cwd=self.worktree, ppid=1, tty_nr=0)
        self._claims({"dev6": {"pid": 700, "start_ticks": 5150,
                               "worktree": str(self.worktree)}})

        snap = shell_liveness.compute()

        self.assertEqual({"dev6": 700}, snap["claimed_pids"])
        self.assertEqual([], snap["claimed_absent"])
        self.assertEqual([], snap["orphaned_pids"])
        self.assertTrue(snap["processes"][0]["claimed"])
        # Lineage is still REPORTED — it is just no longer the verdict.
        self.assertEqual("detached", snap["processes"][0]["orphaned"])
        self.assertEqual("busy", shell_liveness.session_state("dev6", snap))

    def test_relaunch_gap_is_expected_absent_not_available(self):
        # No process at all: the shell between two work items. Before the
        # record this projected a bare "available" and the fleet read as idle.
        self._claims({"dev6": {"pid": 700, "start_ticks": 5150,
                               "worktree": str(self.worktree)}})

        snap = shell_liveness.compute()

        self.assertEqual({}, snap["claimed_pids"])
        self.assertEqual(["dev6"], snap["claimed_absent"])
        self.assertIsNone(shell_liveness.session_state("dev6", snap))
        self.assertEqual("expected_absent",
                         shell_liveness.record_state("dev6", snap))

        output = io.StringIO()
        with redirect_stdout(output):
            shell_liveness._print_text(snap)
        self.assertIn("EXPECTED BUT ABSENT", output.getvalue())

    def test_unclaimed_detached_process_keeps_the_orphan_verdict(self):
        _proc_entry(self.proc, 800, cwd=self.worktree, ppid=1, tty_nr=0)
        self._claims({})

        snap = shell_liveness.compute()

        self.assertEqual([800], snap["orphaned_pids"])
        self.assertFalse(snap["processes"][0]["claimed"])
        self.assertEqual("orphan", shell_liveness.session_state("dev6", snap))

    def test_a_shell_with_no_record_and_no_process_stays_available(self):
        self._claims({})
        snap = shell_liveness.compute()
        self.assertIsNone(shell_liveness.session_state("dev6", snap))
        self.assertIsNone(shell_liveness.record_state("dev6", snap))


class BrowserSessionTest(unittest.TestCase):
    """A browser turn is a harness child of the API server whose cwd is the
    shell's worktree. Unnamed it reads as an anonymous CLI session and locks
    the shell out with nothing to point at; joined against the registry and the
    run rows by pid + start_ticks it is a NAMED hold."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.worktree = self.root / ".sc-worktrees" / "dev6"
        self.worktree.mkdir(parents=True)
        self.db = self.root / "shell.db"
        for patch in (
            mock.patch.object(shell_liveness, "PROC", self.proc),
            mock.patch.object(shell_liveness, "REPO_ROOT", self.root),
            mock.patch.object(shell_liveness, "DB_PATH", self.db),
            mock.patch.object(shell_liveness, "harness_binaries",
                              return_value={"codex"}),
            mock.patch.object(shell_liveness, "_shell_labels", return_value={}),
            mock.patch.object(shell_liveness, "_launch_claims", return_value={}),
            mock.patch.object(shell_liveness, "_self_harness_pid",
                              return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _db(self, *, registry=(), runs=(), tables: bool = True) -> None:
        """A DB carrying only the two tables the scan reads, best-effort style."""
        con = sqlite3.connect(self.db)
        if tables:
            con.executescript(
                "CREATE TABLE active_shell_chats (shell_id INTEGER,"
                "chat_id TEXT,process_pid INTEGER,process_start_ticks INTEGER);"
                "CREATE TABLE conversation_runs (run_id INTEGER PRIMARY KEY,"
                "conversation_id TEXT,state TEXT,process_pid INTEGER,"
                "process_start_ticks INTEGER);")
            con.executemany(
                "INSERT INTO active_shell_chats "
                "(shell_id,chat_id,process_pid,process_start_ticks) "
                "VALUES (1,?,?,?)", registry)
            con.executemany(
                "INSERT INTO conversation_runs "
                "(conversation_id,state,process_pid,process_start_ticks) "
                "VALUES (?,?,?,?)", runs)
        else:
            con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()

    def test_registry_identity_names_the_conversation(self):
        _proc_entry(self.proc, 700, cwd=self.worktree, start_ticks=5150)
        self._db(registry=[("cv_abc", 700, 5150)],
                 runs=[("cv_abc", "running", 700, 5150)])

        snap = shell_liveness.compute()

        self.assertEqual("cv_abc", snap["processes"][0]["browser_conversation"])
        self.assertFalse(snap["processes"][0]["lingering"])
        self.assertEqual(
            {"dev6": [{"pid": 700, "conversation_id": "cv_abc",
                       "lingering": False}]},
            snap["browser_sessions"])

    def test_terminal_run_identity_is_lingering(self):
        # The incident's shape: the run finished, the process kept working.
        _proc_entry(self.proc, 700, cwd=self.worktree, start_ticks=5150)
        self._db(runs=[("cv_abc", "succeeded", 700, 5150)])

        snap = shell_liveness.compute()

        self.assertEqual("cv_abc", snap["processes"][0]["browser_conversation"])
        self.assertTrue(snap["processes"][0]["lingering"])
        self.assertTrue(snap["browser_sessions"]["dev6"][0]["lingering"])

    def test_recycled_pid_is_not_tagged(self):
        # Same pid number, a different process — identity is pid + start ticks,
        # and tagging on the number alone would name a stranger's conversation.
        _proc_entry(self.proc, 700, cwd=self.worktree, start_ticks=99999)
        self._db(registry=[("cv_abc", 700, 5150)],
                 runs=[("cv_abc", "running", 700, 5150)])

        snap = shell_liveness.compute()

        self.assertIsNone(snap["processes"][0]["browser_conversation"])
        self.assertEqual({}, snap["browser_sessions"])

    def test_absent_db_leaves_the_scan_untagged(self):
        _proc_entry(self.proc, 700, cwd=self.worktree, start_ticks=5150)

        snap = shell_liveness.compute()

        self.assertIsNone(snap["processes"][0]["browser_conversation"])
        self.assertEqual({}, snap["browser_sessions"])

    def test_db_without_the_tables_is_not_fatal(self):
        # An un-migrated fork, or a DB we cannot read: best-effort, exactly as
        # _launch_claims degrades.
        _proc_entry(self.proc, 700, cwd=self.worktree, start_ticks=5150)
        self._db(tables=False)

        snap = shell_liveness.compute()

        self.assertIsNone(snap["processes"][0]["browser_conversation"])
        self.assertEqual({}, snap["browser_sessions"])


class BrowserSessionStateTest(unittest.TestCase):
    """session_state's fourth answer: 'browser' when every live pid holding the
    worktree is one the engine launched itself."""

    def _snap(self, processes, browser) -> dict:
        return {"supported": True, "processes": processes,
                "browser_sessions": browser}

    def test_all_browser_pids_are_a_browser_hold(self):
        snap = self._snap(
            [{"pid": 700, "shortname": "dev6", "orphaned": None}],
            {"dev6": [{"pid": 700, "conversation_id": "cv_abc",
                       "lingering": True}]})
        self.assertEqual("browser", shell_liveness.session_state("DEV6", snap))

    def test_a_plain_cli_pid_alongside_is_still_busy(self):
        # One human session among browser turns → someone is working there.
        snap = self._snap(
            [{"pid": 700, "shortname": "dev6", "orphaned": None},
             {"pid": 701, "shortname": "dev6", "orphaned": None}],
            {"dev6": [{"pid": 700, "conversation_id": "cv_abc",
                       "lingering": False}]})
        self.assertEqual("busy", shell_liveness.session_state("dev6", snap))

    def test_orphans_still_win_over_the_browser_verdict(self):
        snap = self._snap(
            [{"pid": 700, "shortname": "dev6", "orphaned": "detached"}],
            {"dev6": [{"pid": 700, "conversation_id": "cv_abc",
                       "lingering": False}]})
        self.assertEqual("orphan", shell_liveness.session_state("dev6", snap))

    def test_browser_sessions_lookup_is_case_insensitive(self):
        snap = self._snap(
            [], {"dev6": [{"pid": 700, "conversation_id": "cv_abc",
                           "lingering": False}]})
        self.assertEqual([700], [s["pid"] for s in
                                 shell_liveness.browser_sessions("DEV6", snap)])
        self.assertEqual([], shell_liveness.browser_sessions("ghost", snap))


class BrowserRefusalTest(unittest.TestCase):
    """The CLI still refuses a browser-held slot — but it names the turn and
    the two ways out instead of leaving a pid to hunt by hand."""

    def setUp(self):
        self.snap = {
            "supported": True,
            "processes": [{"pid": 700, "shortname": "dev6", "orphaned": None}],
            "browser_sessions": {
                "dev6": [{"pid": 700, "conversation_id": "cv_abc",
                          "lingering": True}]},
        }

    def test_refusal_names_the_conversation_the_pid_and_the_exits(self):
        message = run.browser_refusal("dev6", self.snap)
        self.assertIn("cv_abc", message)
        self.assertIn("pid 700", message)
        self.assertIn("lingering", message)
        self.assertIn("interrupt the turn from that GUI chat", message)
        self.assertIn("close that chat", message)

    def test_the_picker_paints_a_browser_hold_rather_than_available(self):
        shell = {"flavor": "dev", "shortname": "dev6", "display_name": "Dev",
                 "browser_active": 0}
        self.assertIn("BROWSER", run._shell_status(shell, self.snap))


class ComputeSmokeTest(unittest.TestCase):
    """compute() against the real /proc: shape only, no liveness assumptions."""

    def test_snapshot_shape(self):
        snap = shell_liveness.compute()
        if not snap.get("supported"):
            self.skipTest("non-Linux: /proc unavailable")
        self.assertIn(snap["admin_presence"], ("present", "indeterminate"))
        if snap["admin_presence"] == "indeterminate":
            self.assertFalse(snap["safe_to_clean_all"])
        self.assertIn("orphaned_pids", snap)
        self.assertIsInstance(snap["orphaned_pids"], list)
        for p in snap["processes"]:
            self.assertIn("orphaned", p)
            self.assertIn(p["orphaned"], (None, "tty-gone", "detached"))
            if p["is_self"]:
                # The scanning session is by definition not an orphan.
                self.assertIsNone(p["orphaned"])


if __name__ == "__main__":
    unittest.main()
