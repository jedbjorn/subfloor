"""Read worker activity evidence without classifying it.

This is the observation boundary for the worker-expectation reconciler
(spec 58, U3).  Callers get one complete :class:`Evidence` value even when
individual inputs are unavailable.  Classification, time windows, and alert
delivery deliberately live elsewhere.

``last_durable_write_at`` is intentionally a lower bound.  Only
``shell_messages`` and ``sprint_units`` carry all three facts needed here: a
per-write timestamp, shell attribution, and sprint scope.  Date-only or
unattributed surfaces are excluded rather than guessed.

``read()`` observes integration refs exactly as they currently are.  Ref
freshness is the caller's responsibility because one reconciler tick reads
many worktrees but must fetch the shared integration ref only once.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sprint_units import TERMINAL_UNIT_STATES

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
ADAPTERS = ENGINE / "adapters"
PROC = Path("/proc")

CODEX_CANDIDATE_WINDOW = timedelta(minutes=60)
UNTIMED_DELETE_RENAME = "last_work_at:untimed_delete_rename"
AMBIGUOUS_PLANNER_BINDING = "planner_binding:ambiguous"
INTEGRATION_REF_REFRESH = "integration_refs:refresh_failed"
BOOT_ARTIFACTS = {
    "CLAUDE.md",
    "AGENTS.md",
    ".claude/settings.local.json",
    ".codex/hooks.json",
}
UNREADABLE_FIELDS = {
    "branch": frozenset({"branch_present"}),
    "commits": frozenset({"commits_since_epoch", "last_work_at"}),
    "durable_write": frozenset({"last_durable_write_at"}),
    "epoch": frozenset({"epoch"}),
    "git_state": frozenset(
        {"dirty", "commits_since_epoch", "last_work_at"}
    ),
    INTEGRATION_REF_REFRESH: frozenset(
        {"branch_present", "commits_since_epoch", "last_work_at"}
    ),
    "marker": frozenset({"marker_at"}),
    "newest_mtime": frozenset({"newest_mtime"}),
    "process": frozenset({"process_present", "launch_shape", "cpu_delta"}),
    "process_binding": frozenset({"launch_shape", "cpu_delta"}),
    AMBIGUOUS_PLANNER_BINDING: frozenset(
        {
            "last_durable_write_at",
            "last_result_row_at",
            "state_changed_at",
        }
    ),
    "result_row": frozenset({"last_result_row_at"}),
    "session": frozenset({"session_ended_at", "session_end_reason"}),
    "state_changed_at": frozenset({"state_changed_at"}),
    UNTIMED_DELETE_RENAME: frozenset({"last_work_at"}),
}


@dataclass
class Evidence:
    last_work_at: datetime | None = None
    dirty: bool | None = None
    commits_since_epoch: int | None = None
    last_durable_write_at: datetime | None = None
    last_result_row_at: datetime | None = None
    state_changed_at: datetime | None = None
    session_ended_at: datetime | None = None
    session_end_reason: str | None = None
    newest_mtime: datetime | None = None
    epoch: datetime | None = None
    edits_code: bool = False
    branch_declared: str | None = None
    branch_present: bool | None = None
    marker_at: datetime | None = None
    cpu_delta: float | None = None
    launch_shape: str | None = None
    process_present: bool | None = None
    unreadable: list[str] = field(default_factory=list)


def _value(row: Any, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _boot_artifact(path: str) -> bool:
    if path in BOOT_ARTIFACTS:
        return True
    return path.startswith(".claude/skills/") and path.endswith("/SKILL.md")


def refresh_integration_refs(
    worktree: Path, run=subprocess.run
) -> bool:
    """Refresh the shared integration ref once for a reconciler tick.

    ``False`` is data, not a swallowed warning: the caller must mark every
    affected code-unit reading with :data:`INTEGRATION_REF_REFRESH`.
    """
    try:
        result = run(
            [
                "git",
                "fetch",
                "--quiet",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            cwd=Path(worktree),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class ActivityReader:
    """Injectable reader used by the scheduler and by hermetic tests."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        repo_root: Path | None = None,
        proc: Path | None = None,
        adapters: Path | None = None,
        home: Path | None = None,
        run=subprocess.run,
    ):
        self.db_path = Path(db_path or DB_PATH)
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.proc = Path(proc or PROC)
        self.adapters = Path(adapters or ADAPTERS)
        self.home = Path(home or Path.home())
        self.run_command = run
        self._cpu_samples: dict[tuple[tuple[int, int], ...], int] = {}

    @staticmethod
    def _mark(evidence: Evidence, name: str) -> None:
        if name not in evidence.unreadable:
            evidence.unreadable.append(name)

    def _worktree(self, shell: Any) -> Path:
        explicit = _value(shell, "worktree")
        if explicit:
            return Path(explicit)
        if _value(shell, "flavor") == "admin":
            return self.repo_root
        return self.repo_root / ".sc-worktrees" / str(
            _value(shell, "shortname", "")
        ).lower()

    def _database_inputs(
        self, evidence: Evidence, shell: Any, unit: Any
    ) -> str | None:
        shell_id = _value(shell, "shell_id")
        sprint_doc_id = _value(unit, "sprint_doc_id")
        try:
            con = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=2
            )
            con.row_factory = sqlite3.Row
        except (OSError, sqlite3.Error):
            for name in ("epoch", "session", "durable_write", "result_row"):
                self._mark(evidence, name)
            if unit is None:
                self._mark(evidence, "state_changed_at")
            return None

        harness = None
        archive_id = None
        planner_session = None
        if unit is None:
            try:
                rows = con.execute(
                    "SELECT b.sprint_doc_id, b.armed_at "
                    "FROM sprint_planner_bindings b "
                    "JOIN documents d ON d.document_id=b.sprint_doc_id "
                    "WHERE b.binding_id=("
                    " SELECT MAX(b2.binding_id) FROM sprint_planner_bindings b2"
                    " WHERE b2.sprint_doc_id=b.sprint_doc_id"
                    ") AND b.planner_shell_id=? AND d.frozen=0 "
                    "AND EXISTS (SELECT 1 FROM sprint_units u "
                    "            WHERE u.sprint_doc_id=b.sprint_doc_id) "
                    "ORDER BY b.sprint_doc_id",
                    (shell_id,),
                ).fetchall()
                # U9 can render several live planner roles, but Evidence carries
                # one sprint scope.  Never collapse several bindings to a
                # confident answer for whichever row happens to sort newest.
                if len(rows) == 1:
                    sprint_doc_id = rows[0]["sprint_doc_id"]
                    evidence.state_changed_at = _timestamp(rows[0]["armed_at"])
                    if evidence.state_changed_at is None:
                        self._mark(evidence, "state_changed_at")
                else:
                    sprint_doc_id = None
                    if len(rows) > 1:
                        self._mark(evidence, AMBIGUOUS_PLANNER_BINDING)
                    for name in (
                        "durable_write",
                        "result_row",
                        "state_changed_at",
                    ):
                        self._mark(evidence, name)
            except sqlite3.Error:
                self._mark(evidence, "durable_write")
                self._mark(evidence, "result_row")
                self._mark(evidence, "state_changed_at")

        try:
            if unit is None:
                row = con.execute(
                    "SELECT session_id, created_at, harness FROM interface_sessions "
                    "WHERE shell_id=? ORDER BY session_id DESC LIMIT 1",
                    (shell_id,),
                ).fetchone()
                evidence.epoch = _timestamp(row["created_at"]) if row else None
                harness = row["harness"] if row else None
                planner_session = row["session_id"] if row else None
            else:
                row = con.execute(
                    "SELECT archive_id, started_at, harness "
                    "FROM shell_memory_archives "
                    "WHERE shell_id=? AND started_at IS NOT NULL "
                    "ORDER BY started_at DESC, archive_id DESC LIMIT 1",
                    (shell_id,),
                ).fetchone()
                evidence.epoch = _timestamp(row["started_at"]) if row else None
                harness = row["harness"] if row else None
                archive_id = row["archive_id"] if row else None
            if evidence.epoch is None:
                self._mark(evidence, "epoch")
        except sqlite3.Error:
            self._mark(evidence, "epoch")

        try:
            if unit is None and planner_session is not None:
                row = con.execute(
                    "SELECT ended_at, end_reason, harness "
                    "FROM interface_sessions WHERE session_id=?",
                    (planner_session,),
                ).fetchone()
            elif archive_id is not None:
                row = con.execute(
                    "SELECT ended_at, end_reason, harness "
                    "FROM interface_sessions WHERE shell_id=? AND archive_id=? "
                    "ORDER BY session_id DESC LIMIT 1",
                    (shell_id, archive_id),
                ).fetchone()
            else:
                row = None
            if row:
                evidence.session_ended_at = _timestamp(row["ended_at"])
                evidence.session_end_reason = row["end_reason"]
                harness = harness or row["harness"]
        except sqlite3.Error:
            self._mark(evidence, "session")

        if sprint_doc_id is not None:
            try:
                row = con.execute(
                    "SELECT MAX(ts) FROM ("
                    " SELECT created_at AS ts FROM shell_messages"
                    "  WHERE from_shell_id=? AND sprint_doc_id=?"
                    " UNION ALL"
                    " SELECT updated_at AS ts FROM sprint_units"
                    "  WHERE updated_by_shell_id=? AND sprint_doc_id=?"
                    ")",
                    (shell_id, sprint_doc_id, shell_id, sprint_doc_id),
                ).fetchone()
                evidence.last_durable_write_at = _timestamp(row[0] if row else None)
            except sqlite3.Error:
                self._mark(evidence, "durable_write")

            try:
                rows = con.execute(
                    "SELECT body, created_at FROM shell_messages "
                    "WHERE from_shell_id=? AND sprint_doc_id=? AND kind='result' "
                    "ORDER BY created_at DESC, message_id DESC",
                    (shell_id, sprint_doc_id),
                ).fetchall()
                if unit is not None:
                    terminal_marks = ",".join(
                        "?" for _ in TERMINAL_UNIT_STATES
                    )
                    active_count = con.execute(
                        "SELECT COUNT(*) FROM sprint_units "
                        "WHERE sprint_doc_id=? "
                        "AND (dev_shell_id=? OR reviewer_shell_id=?) "
                        f"AND state NOT IN ({terminal_marks})",
                        (
                            sprint_doc_id,
                            shell_id,
                            shell_id,
                            *TERMINAL_UNIT_STATES,
                        ),
                    ).fetchone()[0]
                    if active_count != 1:
                        seq = str(_value(unit, "seq", ""))
                        rows = [
                            row
                            for row in rows
                            if self._names_unit(row["body"], seq)
                        ]
                evidence.last_result_row_at = _timestamp(
                    rows[0]["created_at"] if rows else None
                )
            except sqlite3.Error:
                self._mark(evidence, "result_row")
        con.close()
        return harness

    @staticmethod
    def _names_unit(body: str, seq: str) -> bool:
        if not seq:
            return False
        return re.search(
            rf"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]\.)"
            rf"{re.escape(seq)}(?![A-Za-z0-9_]|\.[A-Za-z0-9_])",
            body or "",
        ) is not None

    def _git(
        self, worktree: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            ["git", *args],
            cwd=worktree,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def _branch_present(
        self, evidence: Evidence, worktree: Path, branch: str
    ) -> tuple[bool | None, bool]:
        try:
            head = self._git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
            on_head = head.returncode == 0 and head.stdout.strip() == branch
            if on_head:
                return True, True
            remote = self._git(
                worktree,
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{branch}",
            )
        except (OSError, subprocess.SubprocessError):
            self._mark(evidence, "branch")
            return None, False
        if remote.returncode == 0:
            return True, False
        if remote.returncode == 2:
            return False, False
        self._mark(evidence, "branch")
        return None, False

    @staticmethod
    def _status_entries(raw: str) -> list[tuple[str, str]]:
        parts = raw.split("\0")
        out: list[tuple[str, str]] = []
        i = 0
        while i < len(parts):
            item = parts[i]
            i += 1
            if not item:
                continue
            code, path = item[:2], item[3:]
            out.append((code, path))
            if "R" in code or "C" in code:
                i += 1  # porcelain -z follows a rename/copy with the old path
        return out

    def _git_inputs(
        self, evidence: Evidence, worktree: Path, on_declared_head: bool
    ) -> None:
        if not evidence.edits_code:
            return
        epoch = evidence.epoch
        branch = evidence.branch_declared
        if branch is None:
            return
        try:
            status = self._git(
                worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"
            )
        except (OSError, subprocess.SubprocessError):
            self._mark(evidence, "git_state")
            return
        if status.returncode != 0:
            self._mark(evidence, "git_state")
            return

        entries = self._status_entries(status.stdout)
        evidence.dirty = bool(entries)
        if epoch is None:
            return
        dirty_times: list[datetime] = []
        for code, rel in entries:
            if code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
                continue
            if _boot_artifact(rel):
                continue
            if code != "??" and not any(c in "MADRC" for c in code):
                continue
            if "D" in code:
                self._mark(evidence, UNTIMED_DELETE_RENAME)
                continue
            try:
                changed_at = _mtime(worktree / rel)
            except OSError:
                if "R" in code:
                    self._mark(evidence, UNTIMED_DELETE_RENAME)
                continue
            if changed_at >= epoch:
                dirty_times.append(changed_at)
            elif "R" in code:
                self._mark(evidence, UNTIMED_DELETE_RENAME)

        revision = "HEAD" if on_declared_head else f"refs/remotes/origin/{branch}"
        try:
            contribution = f"refs/remotes/origin/main..{revision}"
            log = self._git(worktree, "log", contribution, "--format=%aI")
        except (OSError, subprocess.SubprocessError):
            log = None
        author_times: list[datetime] = []
        if log is None or log.returncode != 0:
            self._mark(evidence, "commits")
        else:
            for line in log.stdout.splitlines():
                authored = _timestamp(line.strip())
                if authored is not None and authored >= epoch:
                    author_times.append(authored)
            evidence.commits_since_epoch = len(author_times)

        try:
            files = self._git(
                worktree,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            )
            if files.returncode != 0:
                raise OSError(files.stderr)
            mtimes = []
            for rel in files.stdout.split("\0"):
                if not rel:
                    continue
                try:
                    mtimes.append(_mtime(worktree / rel))
                except OSError:
                    continue
            evidence.newest_mtime = max(mtimes, default=None)
        except (OSError, subprocess.SubprocessError):
            self._mark(evidence, "newest_mtime")

        work_times = dirty_times + author_times
        evidence.last_work_at = max(work_times, default=None)

    def _adapter_specs(self) -> dict[str, dict]:
        specs = {}
        for directory in self.adapters.iterdir():
            path = directory / "adapter.json"
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
            harness = data.get("harness")
            if harness:
                specs[harness] = data
        return specs

    @staticmethod
    def _argv_prefix(argv: Sequence[str], wanted: Sequence[str]) -> bool:
        if not argv or not wanted:
            return False
        actual = [Path(argv[0]).name, *argv[1:]]
        expected = [Path(wanted[0]).name, *wanted[1:]]
        return actual[: len(expected)] == expected

    def _shape(self, argv: Sequence[str], spec: dict) -> str | None:
        headless = spec.get("headless") or {}
        hlaunch = headless.get("launch") or []
        launch = spec.get("launch") or []
        prompt_flag = headless.get("prompt_flag")
        if self._argv_prefix(argv, hlaunch) and (
            hlaunch != launch or not prompt_flag or prompt_flag in argv
        ):
            return "headless"
        if prompt_flag and prompt_flag in argv and self._argv_prefix(argv, launch):
            return "headless"
        if self._argv_prefix(argv, launch):
            return "interactive"
        return None

    @staticmethod
    def _stat(path: Path) -> tuple[int, int, int, int]:
        text = path.read_text()
        rest = text[text.rindex(")") + 2 :].split()
        return int(rest[1]), int(rest[11]), int(rest[12]), int(rest[19])

    def _process_inputs(
        self, evidence: Evidence, worktree: Path
    ) -> str | None:
        try:
            specs = self._adapter_specs()
        except (OSError, ValueError, json.JSONDecodeError):
            self._mark(evidence, "process")
            return None
        if not self.proc.is_dir():
            self._mark(evidence, "process")
            return None

        binaries: dict[str, str] = {}
        for harness, spec in specs.items():
            launches = [spec.get("launch") or [], (spec.get("headless") or {}).get("launch") or []]
            for launch in launches:
                if launch:
                    binaries[Path(launch[0]).name[:15]] = harness
            for alias in spec.get("comm_aliases") or []:
                binaries[Path(alias).name[:15]] = harness

        stats: dict[int, tuple[int, int, int, int]] = {}
        matches: list[tuple[int, int, str, str, int]] = []
        unreadable_harness = False
        unmatched_target = False
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stats[pid] = self._stat(entry / "stat")
                comm = (entry / "comm").read_text().strip()
            except (OSError, ValueError, IndexError):
                continue
            harness = binaries.get(comm)
            if harness is None:
                continue
            try:
                cwd = Path(os.readlink(entry / "cwd")).resolve()
                argv = [
                    item.decode(errors="replace")
                    for item in (entry / "cmdline").read_bytes().split(b"\0")
                    if item
                ]
            except OSError:
                unreadable_harness = True
                continue
            if cwd != worktree.resolve():
                continue
            shape = self._shape(argv, specs[harness])
            if shape is None:
                unmatched_target = True
                continue
            _ppid, utime, stime, start = stats[pid]
            matches.append((pid, start, harness, shape, utime + stime))

        if not matches:
            if unmatched_target:
                evidence.process_present = True
                self._mark(evidence, "process_binding")
                return None
            if unreadable_harness:
                self._mark(evidence, "process")
                return None
            evidence.process_present = False
            return None
        evidence.process_present = True
        identities = {(m[2], m[3]) for m in matches}
        if len(matches) != 1 or len(identities) != 1:
            self._mark(evidence, "process_binding")
            return None

        root_pid, start, harness, shape, _root_ticks = matches[0]
        evidence.launch_shape = shape
        descendants = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _u, _s, _start) in stats.items():
                if pid not in descendants and ppid in descendants:
                    descendants.add(pid)
                    changed = True
        total = sum(stats[pid][1] + stats[pid][2] for pid in descendants)
        key = tuple(sorted((pid, stats[pid][3]) for pid in descendants))
        previous = self._cpu_samples.get(key)
        self._cpu_samples[key] = total
        if previous is not None:
            evidence.cpu_delta = float(max(total - previous, 0))
        return harness

    def _claude_marker(
        self, worktree: Path, epoch: datetime
    ) -> datetime | None:
        root = Path(
            os.environ.get("CLAUDE_CONFIG_DIR") or self.home / ".claude"
        ) / "projects"
        encoded = "".join(c if c.isalnum() else "-" for c in str(worktree))
        main = root / encoded
        containers = [main] if main.is_dir() else []
        if root.is_dir():
            containers.extend(
                p
                for p in root.iterdir()
                if p.is_dir()
                and re.fullmatch(
                    rf"-tmp-claude-\d+-{re.escape(encoded)}-[^-].*-scratchpad",
                    p.name,
                )
            )
        if not containers:
            return None
        mtimes = []
        for container in containers:
            for path in container.glob("*.jsonl"):
                stamp = _mtime(path)
                if stamp >= epoch:
                    mtimes.append(stamp)
        return max(mtimes, default=None)

    @staticmethod
    def _last_turn_cwd(path: Path) -> str | None:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            return (record.get("payload") or {}).get("cwd")
        return None

    def _codex_marker(
        self, worktree: Path, epoch: datetime, now: datetime
    ) -> datetime | None:
        root = Path(os.environ.get("CODEX_HOME") or self.home / ".codex") / "sessions"
        cutoff = max(epoch, now - CODEX_CANDIDATE_WINDOW)
        hits = []
        for path in root.glob("*/*/*/rollout-*.jsonl"):
            stamp = _mtime(path)
            if stamp < cutoff:
                continue
            if self._last_turn_cwd(path) == str(worktree):
                hits.append(stamp)
        return hits[0] if len(hits) == 1 else None

    def _kimi_marker(
        self, worktree: Path, epoch: datetime
    ) -> datetime | None:
        root = self.home / ".kimi-code" / "sessions"
        hits = []
        for session in root.glob("wd_*/session_*"):
            state = json.loads(
                (session / "state.json").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
            if not isinstance(state, dict) or state.get("workDir") != str(worktree):
                continue
            stamp = _mtime(session / "agents" / "main" / "wire.jsonl")
            if stamp >= epoch:
                hits.append(stamp)
        return hits[0] if len(hits) == 1 else None

    def _opencode_marker(
        self, worktree: Path, epoch: datetime
    ) -> datetime | None:
        base = Path(os.environ.get("XDG_DATA_HOME") or self.home / ".local/share")
        path = base / "opencode" / "opencode.db"
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT time_updated FROM session WHERE directory=? "
                "AND time_updated>=?",
                (str(worktree), int(epoch.timestamp() * 1000)),
            ).fetchall()
        finally:
            con.close()
        if len(rows) != 1:
            return None
        return datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)

    def _marker_input(
        self,
        evidence: Evidence,
        harness: str | None,
        worktree: Path,
        now: datetime,
    ) -> None:
        if harness == "vibe":
            return
        if harness is None or evidence.epoch is None:
            self._mark(evidence, "marker")
            return
        try:
            if harness == "claude":
                marker = self._claude_marker(worktree, evidence.epoch)
            elif harness == "codex":
                marker = self._codex_marker(worktree, evidence.epoch, now)
            elif harness == "kimi":
                marker = self._kimi_marker(worktree, evidence.epoch)
            elif harness == "opencode":
                marker = self._opencode_marker(worktree, evidence.epoch)
            else:
                marker = None
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
            marker = None
        evidence.marker_at = marker
        if marker is None:
            self._mark(evidence, "marker")

    def read(self, shell: Any, unit: Any, now: Any) -> Evidence:
        """Return all readable observations; never classify and never raise."""
        evidence = Evidence()
        evidence.branch_declared = _value(unit, "branch")
        evidence.edits_code = (
            unit is not None and evidence.branch_declared is not None
        )
        if unit is not None:
            evidence.state_changed_at = _timestamp(
                _value(unit, "state_changed_at")
            )
            if evidence.state_changed_at is None:
                self._mark(evidence, "state_changed_at")
        current = _timestamp(now) or datetime.now(timezone.utc)
        worktree = self._worktree(shell)

        try:
            self._database_inputs(evidence, shell, unit)
        except Exception:  # noqa: BLE001 — complete value, never an escape
            for name in ("epoch", "session", "durable_write", "result_row"):
                self._mark(evidence, name)
            if unit is None:
                self._mark(evidence, "state_changed_at")

        on_declared_head = False
        if evidence.branch_declared is not None:
            try:
                present, on_declared_head = self._branch_present(
                    evidence, worktree, evidence.branch_declared
                )
                evidence.branch_present = present
            except Exception:  # noqa: BLE001 — one unreadable field, not an escape
                self._mark(evidence, "branch")

        try:
            self._git_inputs(evidence, worktree, on_declared_head)
        except Exception:  # noqa: BLE001 — one unreadable field, not an escape
            self._mark(evidence, "git_state")

        try:
            process_harness = self._process_inputs(evidence, worktree)
        except Exception:  # noqa: BLE001 — one unreadable field, not an escape
            process_harness = None
            self._mark(evidence, "process")

        # A marker is current-session evidence only when argv bound the live
        # harness process.  A DB row can corroborate the name, but must never
        # resurrect a stale transcript after the process has gone.
        marker_harness = process_harness if evidence.process_present else None
        self._marker_input(evidence, marker_harness, worktree, current)
        evidence.unreadable.sort()
        return evidence


_DEFAULT_READER = ActivityReader()


def read(shell: Any, unit: Any, now: Any) -> Evidence:
    """Module contract: ``read(shell, unit, now) -> Evidence``."""
    return _DEFAULT_READER.read(shell, unit, now)
