#!/usr/bin/env python3
"""Refresh the repo map, then report every Cartographer completion lane."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

import artifact_policy
import map_notices
import map_repo
from engine_paths import is_generated_install_path


PASS = "PASS"
PENDING = "PENDING"
FAIL = "FAIL"
NOT_APPLICABLE = "N/A"


@dataclass(frozen=True)
class CheckRow:
    key: str
    label: str
    status: str
    evidence: tuple[str, ...]
    next_actions: tuple[str, ...]


@dataclass(frozen=True)
class ExtractorRecord:
    name: str
    target: Path
    installed_digest: str
    receipt: Path
    payload: dict[str, object]


class ApiUnavailable(RuntimeError):
    pass


def _row(
    key: str,
    label: str,
    status: str,
    evidence: list[str] | tuple[str, ...],
    actions: list[str] | tuple[str, ...] = (),
) -> CheckRow:
    return CheckRow(key, label, status, tuple(evidence), tuple(dict.fromkeys(actions)))


def _git(
    worktree: Path,
    *args: str,
    timeout: int = 12,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def _git_bytes(
    worktree: Path,
    *args: str,
    timeout: int = 12,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, b"", str(exc).encode())


def _api_get(path: str, environ: Mapping[str, str] | None = None) -> dict:
    env = environ if environ is not None else os.environ
    base = env.get("SC_API_BASE", "")
    token = env.get("SC_API_TOKEN", "")
    if not base or not token:
        raise ApiUnavailable("missing SC_API_BASE or SC_API_TOKEN")
    request = urllib.request.Request(
        base.rstrip("/") + path,
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ApiUnavailable(f"GET {path} returned HTTP {exc.code}") from exc
    except Exception as exc:
        raise ApiUnavailable(f"GET {path} failed: {exc}") from exc


def _open_map() -> sqlite3.Connection:
    path = artifact_policy.map_db_path()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _description_gaps(connection: sqlite3.Connection) -> list[str]:
    gaps: list[str] = []
    for row in connection.execute(
        "SELECT path, desc FROM dr_filepath ORDER BY path"
    ):
        description = row["desc"]
        if description is None or not str(description).strip():
            gaps.append(row["path"])
            continue
        base = PurePosixPath(row["path"]).name
        stem = PurePosixPath(row["path"]).stem
        lowered = str(description).lower()
        if len(stem) >= 5 and (
            lowered.endswith(base.lower()) or lowered.endswith(stem.lower())
        ):
            gaps.append(row["path"])
    return gaps


def check_live_map(
    refresh_result: map_repo.MapRefreshResult | None,
    refresh_error: str | None,
) -> CheckRow:
    boundary = "description validity is live-only; the authored snapshot does not persist it"
    failures: list[str] = []
    pending: list[str] = []
    actions: list[str] = []
    if refresh_error:
        failures.append(f"refresh failed: {refresh_error}")
        actions.append("sc map")
    actual_count = 0
    try:
        connection = _open_map()
    except sqlite3.Error as exc:
        failures.append(f"live map DB cannot be opened read-only: {exc}")
        actions.append("sc map")
        return _row("live_map", "Live map", FAIL, failures, actions)
    try:
        repo = connection.execute(
            "SELECT name, root, file_count FROM dr_repo WHERE repo_id=1"
        ).fetchone()
        actual_count = connection.execute("SELECT COUNT(*) FROM dr_filepath").fetchone()[0]
        if repo is None:
            failures.append("dr_repo has no repo_id=1 identity")
        else:
            expected_root = str(map_repo.MAP_ROOT.resolve())
            if repo["root"] != expected_root or repo["name"] != map_repo.MAP_ROOT.name:
                failures.append(
                    f"repo identity mismatch: name={repo['name']!r} root={repo['root']!r}"
                )
            if repo["file_count"] != actual_count or actual_count < 1:
                failures.append(
                    f"file count mismatch/empty: dr_repo={repo['file_count']} rows={actual_count}"
                )
        managed = [
            row["path"] for row in connection.execute(
                "SELECT path FROM dr_filepath ORDER BY path"
            ) if is_generated_install_path(row["path"])
        ]
        if managed:
            failures.extend(f"engine-managed path mapped: {path}" for path in managed)
        nested = [
            row[0] for row in connection.execute(
                "SELECT f.path FROM dr_filepath f "
                "WHERE instr(f.path, '/') > 0 AND NOT EXISTS "
                "(SELECT 1 FROM dr_section s "
                " WHERE f.path LIKE s.path_prefix || '%') ORDER BY f.path"
            )
        ]
        if nested:
            pending.extend(f"nested unsectioned: {path}" for path in nested)
            actions.append(
                "sc map-sql \"SELECT f.path FROM dr_filepath f WHERE "
                "instr(f.path,'/')>0 AND NOT EXISTS (SELECT 1 FROM dr_section s "
                "WHERE f.path LIKE s.path_prefix || '%') ORDER BY f.path\""
            )
        stale = [
            f"{row['name']} ({row['path_prefix']})" for row in connection.execute(
                "SELECT s.name, s.path_prefix FROM dr_section s "
                "WHERE NOT EXISTS (SELECT 1 FROM dr_filepath f "
                "WHERE f.path LIKE s.path_prefix || '%') ORDER BY s.name"
            )
        ]
        if stale:
            pending.extend(f"stale section: {section}" for section in stale)
            actions.append(
                "sc map-sql \"SELECT name,path_prefix FROM dr_section s WHERE "
                "NOT EXISTS (SELECT 1 FROM dr_filepath f WHERE "
                "f.path LIKE s.path_prefix || '%') ORDER BY name\""
            )
        empty_sections = [
            row["name"] for row in connection.execute(
                "SELECT name FROM dr_section WHERE path_prefix='' ORDER BY name"
            )
        ]
        if empty_sections:
            failures.extend(
                f"authored section has forbidden empty prefix: {name}"
                for name in empty_sections
            )
            actions.append(
                "sc map-sql-rw \"DELETE FROM dr_section WHERE path_prefix=''\""
            )
        descriptions = _description_gaps(connection)
        if descriptions:
            pending.extend(f"description missing/filler: {path}" for path in descriptions)
            actions.append(
                "sc map-sql \"SELECT path,desc FROM dr_filepath "
                "WHERE desc IS NULL ORDER BY path\""
            )
    except sqlite3.Error as exc:
        failures.append(f"live map check failed: {exc}")
        actions.append("sc map")
    finally:
        connection.close()
    if refresh_result is not None:
        if refresh_result.truncated:
            failures.append(f"map stopped at MAX_FILES={map_repo.MAX_FILES}")
        for summary in refresh_result.extractor_summaries:
            if "FAILED" in summary or "no extract()" in summary:
                failures.append(f"extractor failure: {summary}")
                module = summary.split(":", 1)[0]
                actions.append(
                    "sc map-extractor install "
                    f"\"$SC_SHELL_WORKTREE/.sc-state/map_extractors/{module}.py\""
                )
    if failures:
        return _row(
            "live_map", "Live map", FAIL,
            failures + pending + [boundary], actions,
        )
    if pending:
        return _row("live_map", "Live map", PENDING, pending + [boundary], actions)
    return _row(
        "live_map",
        "Live map",
        PASS,
        [f"refresh and {actual_count} mapped file rows are valid", boundary],
    )


def _snapshot_sections(path: Path) -> list[tuple[str, str, str | None, int]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE dr_section (section_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL UNIQUE, path_prefix TEXT NOT NULL, "
            "description TEXT, sort_order INTEGER NOT NULL DEFAULT 0)"
        )
        connection.executescript(path.read_text())
        return connection.execute(
            "SELECT name, path_prefix, description, sort_order "
            "FROM dr_section ORDER BY name"
        ).fetchall()
    finally:
        connection.close()


def check_authored_sections() -> CheckRow:
    try:
        connection = _open_map()
        try:
            live = connection.execute(
                "SELECT name, path_prefix, description, sort_order "
                "FROM dr_section ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return _row(
            "authored_sections", "Authored sections", FAIL,
            [f"cannot read live sections: {exc}"], ["sc map"],
        )
    if not live:
        return _row(
            "authored_sections", "Authored sections", NOT_APPLICABLE,
            ["the live map has no authored sections"],
        )
    snapshot = artifact_policy.map_content_path()
    if not snapshot.is_file():
        return _row(
            "authored_sections", "Authored sections", PENDING,
            [f"snapshot missing: {snapshot}"], ["Admin: ./sc snapshot"],
        )
    try:
        saved = _snapshot_sections(snapshot)
    except (OSError, sqlite3.Error) as exc:
        return _row(
            "authored_sections", "Authored sections", FAIL,
            [f"snapshot cannot be evaluated: {exc}"], ["Admin: ./sc snapshot"],
        )
    normalized_live = [tuple(row) for row in live]
    if normalized_live != saved:
        return _row(
            "authored_sections", "Authored sections", PENDING,
            [f"live rows ({len(live)}) differ from snapshot rows ({len(saved)})"],
            ["Admin: ./sc snapshot"],
        )
    return _row(
        "authored_sections", "Authored sections", PASS,
        [f"{len(live)} live rows equal {snapshot}"],
    )


def _read_receipt(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def check_extractor_install() -> tuple[CheckRow, dict[str, ExtractorRecord], set[str]]:
    target_dir = map_repo.MAP_ROOT / ".sc-state" / "map_extractors"
    receipt_dir = artifact_policy.map_extractor_receipts_dir()
    unsafe_roots: list[str] = []
    if target_dir.is_symlink():
        unsafe_roots.append(f"installed extractor directory is a symlink: {target_dir}")
    if receipt_dir.is_symlink():
        unsafe_roots.append(f"extractor receipt directory is a symlink: {receipt_dir}")
    targets = {
        path.name: path for path in sorted(target_dir.glob("*.py"))
        if not path.name.startswith("_")
    } if target_dir.is_dir() and not target_dir.is_symlink() else {}
    receipt_paths = {
        path.stem: path for path in sorted(receipt_dir.glob("*.json"))
    } if receipt_dir.is_dir() and not receipt_dir.is_symlink() else {}
    if unsafe_roots:
        return (
            _row(
                "extractor_install", "Extractor install", FAIL,
                unsafe_roots, ["repair the named map-local path, then run sc map finalize"],
            ),
            {},
            set(targets),
        )
    if not targets and not receipt_paths:
        return (
            _row(
                "extractor_install", "Extractor install", NOT_APPLICABLE,
                ["no installed extractor or receipt"],
            ),
            {},
            set(),
        )
    problems: list[str] = []
    actions: list[str] = []
    records: dict[str, ExtractorRecord] = {}
    for name, target in targets.items():
        if target.is_symlink() or not target.is_file():
            problems.append(f"installed extractor is not a regular file: {target}")
            continue
        installed_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        receipt = receipt_paths.get(Path(name).stem)
        payload = _read_receipt(receipt) if receipt is not None else None
        action = (
            "sc map-extractor install "
            f"\"$SC_SHELL_WORKTREE/.sc-state/map_extractors/{name}\""
        )
        if receipt is None:
            problems.append(f"missing receipt for {name}")
            actions.append(action)
            continue
        if payload is None:
            problems.append(f"malformed receipt: {receipt}")
            actions.append(action)
            continue
        if payload.get("extractor") != name:
            problems.append(f"receipt extractor mismatch for {name}")
            actions.append(action)
            continue
        records[name] = ExtractorRecord(name, target, installed_digest, receipt, payload)
        if payload.get("digest") != installed_digest:
            problems.append(
                f"receipt digest mismatch for {name}: "
                f"receipt={payload.get('digest')} installed={installed_digest}"
            )
            actions.append(action)
    for stem, receipt in receipt_paths.items():
        expected_name = f"{stem}.py"
        if expected_name not in targets:
            problems.append(f"stale receipt without installed extractor: {receipt}")
            actions.append(f"remove stale receipt after verifying: {receipt}")
    if problems:
        return (
            _row("extractor_install", "Extractor install", PENDING, problems, actions),
            records,
            set(targets),
        )
    return (
        _row(
            "extractor_install", "Extractor install", PASS,
            [f"{len(targets)} installed extractor receipt(s) match live bytes"],
        ),
        records,
        set(targets),
    )


def _source_location(record: ExtractorRecord) -> tuple[Path, str, Path] | None:
    raw_worktree = record.payload.get("source_worktree")
    raw_source = record.payload.get("source_path")
    if not isinstance(raw_worktree, str) or not isinstance(raw_source, str):
        return None
    relative = PurePosixPath(raw_source)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    expected = PurePosixPath(".sc-state", "map_extractors", record.name)
    if relative != expected:
        return None
    try:
        worktree = Path(raw_worktree).resolve(strict=True)
        candidate = worktree / Path(*relative.parts)
        resolved_source = candidate.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved_source.relative_to(worktree)
    except ValueError:
        return None
    return worktree, raw_source, candidate


def _source_state(record: ExtractorRecord) -> tuple[bool, list[str], list[str]]:
    location = _source_location(record)
    if location is None:
        return False, [f"{record.name}: source worktree/path is absent or unsafe"], [
            "sc map-extractor install "
            f"\"$SC_SHELL_WORKTREE/.sc-state/map_extractors/{record.name}\""
        ]
    worktree, source_path, source = location
    if source.is_symlink() or not source.is_file():
        return False, [f"{record.name}: source is not a regular file: {source}"], [
            f"restore {source}; sc map-extractor install \"{source}\""
        ]
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_digest != record.installed_digest:
        return False, [
            f"{record.name}: source digest {source_digest} != installed {record.installed_digest}"
        ], [f"sc map-extractor install \"{source}\""]
    top = _git(worktree, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != worktree:
        return False, [f"{record.name}: source worktree is not an exact Git root"], [
            f"place {source_path} in the Cartographer Git worktree, then reinstall"
        ]
    tracked = _git(worktree, "ls-files", "--error-unmatch", "--", source_path)
    status = _git(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "--", source_path
    )
    if tracked.returncode != 0:
        return False, [f"{record.name}: source is untracked: {source_path}"], [
            f"git -C \"{worktree}\" add -- \"{source_path}\""
        ]
    if status.returncode != 0 or status.stdout.strip():
        return False, [f"{record.name}: source is dirty: {source_path}"], [
            f"commit and push {source_path} from {worktree}"
        ]
    return True, [f"{record.name}: clean tracked source matches installed digest"], []


def check_extractor_source(
    records: dict[str, ExtractorRecord],
    target_names: set[str],
) -> CheckRow:
    if not target_names:
        return _row(
            "extractor_source", "Extractor source", NOT_APPLICABLE,
            ["no installed extractor"],
        )
    evidence: list[str] = []
    actions: list[str] = []
    passed = True
    for name in sorted(target_names):
        record = records.get(name)
        if record is None:
            passed = False
            evidence.append(f"{name}: no usable receipt identifies its source")
            actions.append(
                f"sc map-extractor install \"$SC_SHELL_WORKTREE/.sc-state/map_extractors/{name}\""
            )
            continue
        ok, details, next_actions = _source_state(record)
        passed = passed and ok
        evidence.extend(details)
        actions.extend(next_actions)
    return _row(
        "extractor_source", "Extractor source", PASS if passed else PENDING,
        evidence, actions,
    )


def _remote_default(worktree: Path) -> tuple[str, str] | None:
    result = _git(worktree, "ls-remote", "--symref", "origin", "HEAD")
    if result.returncode != 0:
        return None
    branch: str | None = None
    head_sha: str | None = None
    for line in result.stdout.splitlines():
        left, _, right = line.partition("\t")
        if left.startswith("ref: refs/heads/") and right == "HEAD":
            branch = left.removeprefix("ref: refs/heads/")
        elif right == "HEAD" and len(left) == 40:
            head_sha = left
    return (branch, head_sha) if branch and head_sha else None


def _default_contains_digest(
    worktree: Path,
    branch: str,
    source_path: str,
    digest: str,
) -> bool:
    history = _git(worktree, "rev-list", f"refs/remotes/origin/{branch}", "--", source_path)
    if history.returncode != 0:
        return False
    for commit in history.stdout.splitlines():
        body = _git_bytes(worktree, "show", f"{commit}:{source_path}")
        if body.returncode == 0 and hashlib.sha256(body.stdout).hexdigest() == digest:
            return True
    return False


def check_admin_handoff(
    records: dict[str, ExtractorRecord],
    target_names: set[str],
) -> CheckRow:
    if not target_names:
        return _row(
            "admin_handoff", "Admin handoff", NOT_APPLICABLE,
            ["no installed extractor source requires review"],
        )
    evidence: list[str] = []
    actions: list[str] = []
    passed = True
    for name in sorted(target_names):
        record = records.get(name)
        location = _source_location(record) if record is not None else None
        if record is None or location is None:
            passed = False
            evidence.append(f"{name}: source identity unavailable")
            actions.append(
                f"sc map-extractor install \"$SC_SHELL_WORKTREE/.sc-state/map_extractors/{name}\""
            )
            continue
        worktree, source_path, _ = location
        remote = _git(worktree, "remote", "get-url", "origin")
        default = _remote_default(worktree)
        if remote.returncode != 0 or default is None:
            passed = False
            evidence.append(f"{name}: origin/default branch is unverifiable")
            actions.append(f"verify Git origin for {worktree}, then run sc map finalize")
            continue
        branch, remote_sha = default
        local = _git(worktree, "rev-parse", f"refs/remotes/origin/{branch}")
        if local.returncode != 0 or local.stdout.strip() != remote_sha:
            passed = False
            evidence.append(f"{name}: local origin/{branch} does not match remote {remote_sha}")
            actions.append(f"git -C \"{worktree}\" fetch origin \"{branch}\"")
            continue
        if not _default_contains_digest(
            worktree, branch, source_path, record.installed_digest
        ):
            passed = False
            evidence.append(
                f"{name}: installed digest is not present at {source_path} on origin/{branch}"
            )
            actions.append(
                f"Admin: review and merge {source_path}; then git -C \"{worktree}\" "
                f"fetch origin \"{branch}\""
            )
            continue
        evidence.append(f"{name}: matching source is reachable from origin/{branch}")
    return _row(
        "admin_handoff", "Admin handoff", PASS if passed else PENDING,
        evidence, actions,
    )


def check_notices(
    api_get: Callable[[str], dict],
) -> tuple[CheckRow, CheckRow]:
    try:
        payload = api_get("/_sc/mem/messages")
    except Exception as exc:
        evidence = [f"memory API status unknown: {exc}"]
        actions = ["boot the Cartographer via ./sc enter, then run sc map finalize"]
        return (
            _row("shape_notices", "Shape notices", PENDING, evidence, actions),
            _row("notice_flags", "Notice flags", PENDING, evidence, actions),
        )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        evidence = ["memory API returned no message list"]
        actions = ["sc mem message check"]
        return (
            _row("shape_notices", "Shape notices", FAIL, evidence, actions),
            _row("notice_flags", "Notice flags", FAIL, evidence, actions),
        )
    valid_messages = [message for message in messages if isinstance(message, dict)]
    unread = [
        message for message in valid_messages
        if not message.get("read_at") and str(message.get("body", "")).startswith("shape:")
    ]
    unread_count = sum(not message.get("read_at") for message in valid_messages)
    bound_unknown = len(messages) == 50 and unread_count == 50
    if not unread and not bound_unknown:
        return (
            _row("shape_notices", "Shape notices", PASS, ["no unread shape notice"]),
            _row("notice_flags", "Notice flags", NOT_APPLICABLE, ["no unread shape notice"]),
        )
    notice_evidence: list[str] = []
    notice_actions: list[str] = ["sc mem message check"]
    flag_evidence: list[str] = []
    flag_actions: list[str] = []
    flags_pass = True
    if bound_unknown:
        notice_evidence.append("inbox reached the 50-unread-row API bound")
        flag_evidence.append("older unread shape notices cannot be ruled out")
        flags_pass = False
    for message in unread:
        message_id = message.get("message_id")
        try:
            notice = map_notices.parse_shape_notice(str(message.get("body", "")))
        except map_notices.ShapeNoticeError as exc:
            notice_evidence.append(f"message #{message_id}: malformed ({exc})")
            flag_evidence.append(f"message #{message_id}: flag identities unknown")
            flag_actions.append(f"repair or replace malformed shape notice #{message_id}")
            flags_pass = False
            continue
        notice_evidence.append(f"message #{message_id}: unread ({notice.paths})")
        for expected in notice.flags:
            try:
                flag_payload = api_get(f"/_sc/mem/flags/{expected.flag_id}")
                flag = flag_payload.get("flag") if isinstance(flag_payload, dict) else None
            except Exception as exc:
                flag = None
                flag_evidence.append(
                    f"message #{message_id}: {expected.flag_id}={expected.name} unavailable ({exc})"
                )
            if not isinstance(flag, dict):
                flags_pass = False
                flag_actions.append(f"sc mem get flags {expected.flag_id}")
                continue
            actual_name = flag.get("display_name")
            if actual_name != expected.name:
                flags_pass = False
                flag_evidence.append(
                    f"message #{message_id}: flag #{expected.flag_id} name mismatch "
                    f"({actual_name!r} != {expected.name!r})"
                )
                flag_actions.append(f"sc mem get flags {expected.flag_id}")
                continue
            resolved = bool(flag.get("resolved"))
            notes = str(flag.get("resolution_notes") or "").strip()
            if not resolved or not notes:
                flags_pass = False
                state = "open" if not resolved else "resolved without notes"
                flag_evidence.append(
                    f"message #{message_id}: {expected.flag_id}={expected.name} is {state}"
                )
                flag_actions.append(
                    f"sc mem flag close {expected.flag_id} --notes \"<verified map result>\""
                )
                continue
            flag_evidence.append(
                f"message #{message_id}: {expected.flag_id}={expected.name} resolved with notes"
            )
    notices = _row(
        "shape_notices", "Shape notices", PENDING,
        notice_evidence or ["unread shape notice state is bounded/unknown"],
        notice_actions,
    )
    flags = _row(
        "notice_flags", "Notice flags", PASS if flags_pass else PENDING,
        flag_evidence or ["unread notices reference flags: none"],
        flag_actions,
    )
    return notices, flags


def build_report(
    refresh_result: map_repo.MapRefreshResult | None,
    refresh_error: str | None,
    api_get: Callable[[str], dict] = _api_get,
) -> list[CheckRow]:
    try:
        live_row = check_live_map(refresh_result, refresh_error)
    except Exception as exc:
        live_row = _row(
            "live_map", "Live map", FAIL,
            [f"live-map evaluator failed: {exc}"], ["sc map"],
        )
    try:
        authored_row = check_authored_sections()
    except Exception as exc:
        authored_row = _row(
            "authored_sections", "Authored sections", FAIL,
            [f"authored-section evaluator failed: {exc}"], ["Admin: ./sc snapshot"],
        )
    try:
        install_row, records, target_names = check_extractor_install()
    except Exception as exc:
        install_row = _row(
            "extractor_install", "Extractor install", FAIL,
            [f"extractor-install evaluator failed: {exc}"],
            ["verify map-local extractor state, then run sc map finalize"],
        )
        records, target_names = {}, set()
        source_row = _row(
            "extractor_source", "Extractor source", PENDING,
            ["source status unknown because install evidence failed"],
            ["repair Extractor install first, then run sc map finalize"],
        )
        admin_row = _row(
            "admin_handoff", "Admin handoff", PENDING,
            ["remote durability unknown because install evidence failed"],
            ["repair Extractor install first, then run sc map finalize"],
        )
    else:
        try:
            source_row = check_extractor_source(records, target_names)
        except Exception as exc:
            source_row = _row(
                "extractor_source", "Extractor source", FAIL,
                [f"extractor-source evaluator failed: {exc}"],
                ["verify the receipt source path, then run sc map finalize"],
            )
        try:
            admin_row = check_admin_handoff(records, target_names)
        except Exception as exc:
            admin_row = _row(
                "admin_handoff", "Admin handoff", FAIL,
                [f"Admin-handoff evaluator failed: {exc}"],
                ["verify origin/default-branch access, then run sc map finalize"],
            )
    try:
        notices, flags = check_notices(api_get)
    except Exception as exc:
        evidence = [f"notice evaluator failed: {exc}"]
        actions = ["sc mem message check"]
        notices = _row("shape_notices", "Shape notices", FAIL, evidence, actions)
        flags = _row("notice_flags", "Notice flags", FAIL, evidence, actions)
    return [
        live_row,
        authored_row,
        install_row,
        source_row,
        admin_row,
        notices,
        flags,
    ]


def report_exit(rows: list[CheckRow]) -> tuple[str, int]:
    if any(row.status == FAIL for row in rows):
        return FAIL, 1
    if any(row.status == PENDING for row in rows):
        return PENDING, 2
    return PASS, 0


def render_human(rows: list[CheckRow]) -> str:
    overall, exit_code = report_exit(rows)
    lines = [f"MAP FINALIZE: {overall} (exit {exit_code})"]
    for row in rows:
        lines.append(f"[{row.status}] {row.label}")
        lines.extend(f"  - {item}" for item in row.evidence)
        lines.extend(f"  next: {action}" for action in row.next_actions)
    return "\n".join(lines)


def render_json(rows: list[CheckRow]) -> str:
    overall, exit_code = report_exit(rows)
    return json.dumps(
        {
            "exit_code": exit_code,
            "overall": overall,
            "rows": [asdict(row) for row in rows],
        },
        indent=2,
        sort_keys=True,
    )


def main(argv: list[str]) -> int:
    if argv in (["-h"], ["--help"]):
        print("usage: sc map finalize [--json]")
        return 0
    if argv not in ([], ["--json"]):
        print("map finalize: usage: sc map finalize [--json]", file=sys.stderr)
        return 1
    refresh_result: map_repo.MapRefreshResult | None = None
    refresh_error: str | None = None
    try:
        refresh_result = map_repo.refresh()
    except Exception as exc:
        refresh_error = str(exc)
    rows = build_report(refresh_result, refresh_error)
    print(render_json(rows) if argv == ["--json"] else render_human(rows))
    return report_exit(rows)[1]


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
