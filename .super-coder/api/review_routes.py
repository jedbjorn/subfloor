"""Authenticated, read-only Git and pull-request review resources.

The browser operator selects only durable conversation/target identities.  The
server resolves every worktree and Git ref from owned database rows, performs
bounded external reads outside transactions, then revalidates the target
snapshot before returning it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import conversation_git_targets
import conversation_routes
import db_driver
import git_review
from github_pull_requests import (
    GitHubPullRequestReader,
    GitHubReadError,
    PullRequest,
    lifecycle_status,
)

DB_PATH = conversation_routes.DB_PATH
REMOTE_TTL_SECONDS = 30.0
REMOTE_CACHE_LIMIT = 256
MAX_PAGE_SIZE = 200
READER_FACTORY = GitHubPullRequestReader
CACHE_FACTORY = git_review.MergedPatchCache

_CONVERSATION_TARGETS = re.compile(
    r"^/api/conversations/(cv_[0-9a-f]{32})/review-targets$"
)
_TARGET_RESOURCE = re.compile(
    r"^/api/review-targets/(gt_[0-9a-f]{32})/(files|diff|commits)$"
)
_CONVERSATION_OBSERVATIONS = re.compile(
    r"^/api/conversations/(cv_[0-9a-f]{32})/review-observations$"
)
_OBSERVATION_RESOURCE = re.compile(
    r"^/api/review-observations/([0-9a-f]{64})/(patch|shell-file)$"
)
_REMOTE_ATTEMPTS: dict[str, float] = {}
_REMOTE_RESULTS: dict[
    str,
    tuple[dict[int, PullRequest], str, str | None],
] = {}
_FILE_STATUSES = frozenset(
    ("added", "modified", "deleted", "renamed", "untracked", "conflict")
)
OBSERVATION_CACHE_LIMIT = 64
OBSERVATION_KEY_CACHE_LIMIT = 512
MAX_SHELL_FILES = 256
MAX_SHELL_FILE_BYTES = 512 * 1024
MAX_SHELL_TOTAL_BYTES = 2 * 1024 * 1024
FETCHER = git_review.fetch_origin_main
_OBSERVATION_LOCK = threading.Lock()
_OBSERVATIONS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_OBSERVATION_KEYS: OrderedDict[
    tuple[int, str, str], dict[str, Any]
] = OrderedDict()

ApiError = conversation_routes.ApiError


def _db():
    return db_driver.connect(str(DB_PATH))


def _query(raw: str, allowed: set[str]) -> dict[str, str]:
    try:
        parsed = parse_qs(
            raw,
            keep_blank_values=True,
            max_num_fields=20,
        )
    except ValueError as exc:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "query contains too many parameters",
        ) from exc
    unknown = sorted(set(parsed) - allowed)
    repeated = sorted(key for key, values in parsed.items() if len(values) != 1)
    if unknown or repeated:
        fields = sorted(set(unknown + repeated))
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "unknown or repeated query parameter(s): " + ", ".join(fields),
            {"fields": fields},
        )
    return {key: values[0] for key, values in parsed.items()}


def _limit(query: dict[str, str]) -> int:
    raw = query.get("limit", "100")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "limit must be an integer") from exc
    if value < 1 or value > MAX_PAGE_SIZE:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"limit must be between 1 and {MAX_PAGE_SIZE}",
        )
    return value


def _cursor_encode(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _cursor_decode(raw: str, kind: str) -> dict[str, Any]:
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        value = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(422, "VALIDATION_ERROR", "cursor is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or value.get("kind") != kind
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
        or not isinstance(value.get("fingerprint"), str)
    ):
        raise ApiError(422, "VALIDATION_ERROR", "cursor is invalid")
    return value


def _etag_response(headers, etag_value: str, body: dict[str, Any]):
    if headers.get("If-None-Match") == etag_value:
        return (
            304,
            [("Cache-Control", "no-store"), ("ETag", etag_value)],
            b"",
        )
    return conversation_routes._json(200, body, [("ETag", etag_value)])


def _conversation(con, conversation_id: str, owner_user_id: int):
    row = con.execute(
        "SELECT conversation_id,shell_id,harness,worktree FROM conversations "
        "WHERE conversation_id=? AND owner_user_id=?",
        (conversation_id, owner_user_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "CONVERSATION_NOT_FOUND", "conversation does not exist")
    return row


def _target_select() -> str:
    return (
        "SELECT t.*,c.worktree FROM conversation_git_targets t "
        "JOIN conversations c ON c.conversation_id=t.conversation_id "
        "WHERE t.target_id=? AND c.owner_user_id=?"
    )


def _target(con, target_id: str, owner_user_id: int) -> dict[str, Any]:
    row = con.execute(_target_select(), (target_id, owner_user_id)).fetchone()
    if row is None:
        raise ApiError(
            404,
            "REVIEW_TARGET_NOT_FOUND",
            "review target does not exist",
        )
    return dict(row)


def _targets(con, conversation_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            "SELECT * FROM conversation_git_targets WHERE conversation_id=? "
            "ORDER BY last_seen_at DESC,target_id",
            (conversation_id,),
        ).fetchall()
    ]


def _snapshot(row: dict[str, Any]) -> str:
    return git_review.fingerprint(
        {
            key: row.get(key)
            for key in (
                "target_id",
                "conversation_id",
                "worktree",
                "base_ref",
                "latest_head_sha",
                "pr_number",
                "pr_head_sha",
                "pr_state",
                "merge_sha",
                "patch_artifact",
                "patch_sha256",
            )
        }
    )


def _revalidate(target_id: str, owner_user_id: int, expected: str) -> None:
    con = _db()
    try:
        row = _target(con, target_id, owner_user_id)
    finally:
        con.close()
    if _snapshot(row) != expected:
        raise ApiError(
            409,
            "REVIEW_TARGET_CHANGED",
            "review target changed while it was being read",
        )


def _cached_pull_request(row: dict[str, Any]) -> PullRequest | None:
    if row.get("pr_number") is None or row.get("pr_state") is None:
        return None
    return PullRequest(
        number=int(row["pr_number"]),
        head_ref=row["branch_name"],
        base_ref=row.get("base_ref"),
        head_sha=row.get("pr_head_sha"),
        state=row["pr_state"],
        merged_at=row.get("merged_at"),
        merge_sha=row.get("merge_sha"),
        title=row.get("pr_title"),
        url=row.get("pr_url"),
        review_decision=None,
        checks=None,
        checks_failed=False,
    )


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _associate_source(
    local_rows: list[dict[str, Any]],
    pull_request: PullRequest,
    worktree: str,
) -> dict[str, Any] | None:
    branch_rows = [
        row for row in local_rows if row["branch_name"] == pull_request.head_ref
    ]
    if not branch_rows:
        return None
    exact = [
        row
        for row in branch_rows
        if pull_request.head_sha
        in {row["first_head_sha"], row["latest_head_sha"]}
    ]
    if exact:
        return max(
            exact,
            key=lambda row: (row["last_seen_at"], row["target_id"]),
        )
    if pull_request.head_sha is None:
        return None
    compatible = []
    for row in branch_rows:
        try:
            if git_review.commit_is_ancestor(
                worktree,
                row["first_head_sha"],
                pull_request.head_sha,
            ) or git_review.commit_is_ancestor(
                worktree,
                pull_request.head_sha,
                row["latest_head_sha"],
            ):
                compatible.append(row)
        except git_review.ReviewError:
            continue
    if not compatible:
        return None
    return max(
        compatible,
        key=lambda row: (row["last_seen_at"], row["target_id"]),
    )


def _persist_remote(
    conversation_id: str,
    worktree: str,
    owner_user_id: int,
    pull_requests: list[PullRequest],
) -> None:
    con = _db()
    try:
        current = con.execute(
            "SELECT worktree FROM conversations WHERE conversation_id=? "
            "AND owner_user_id=?",
            (conversation_id, owner_user_id),
        ).fetchone()
        if current is None or current["worktree"] != worktree:
            raise ApiError(
                409,
                "REVIEW_TARGET_CHANGED",
                "conversation review source changed during refresh",
            )
        observed_rows = _targets(con, conversation_id)
    finally:
        con.close()
    observed_existing = {
        int(row["pr_number"]): row
        for row in observed_rows
        if row["pr_number"] is not None
    }
    observed_local = [
        row for row in observed_rows if row["pr_number"] is None
    ]
    sources = {
        pull_request.number: (
            _associate_source(observed_local, pull_request, worktree)
            or observed_existing.get(pull_request.number)
        )
        for pull_request in pull_requests
    }

    con = _db()
    try:
        with db_driver.write_transaction(con, "conversation.review_targets.refresh"):
            current = con.execute(
                "SELECT worktree FROM conversations WHERE conversation_id=? "
                "AND owner_user_id=?",
                (conversation_id, owner_user_id),
            ).fetchone()
            if current is None or current["worktree"] != worktree:
                raise ApiError(
                    409,
                    "REVIEW_TARGET_CHANGED",
                    "conversation review source changed during refresh",
                )
            rows = _targets(con, conversation_id)
            existing = {
                int(row["pr_number"]): row
                for row in rows
                if row["pr_number"] is not None
            }
            now = _stamp()
            for pull_request in pull_requests:
                row = existing.get(pull_request.number)
                source = sources[pull_request.number] or row
                if source is None:
                    continue
                if source["pr_number"] is None:
                    current_source = next(
                        (
                            item
                            for item in rows
                            if item["target_id"] == source["target_id"]
                            and item["pr_number"] is None
                            and item["branch_name"] == source["branch_name"]
                            and item["first_head_sha"] == source["first_head_sha"]
                            and item["latest_head_sha"] == source["latest_head_sha"]
                        ),
                        None,
                    )
                    if current_source is None:
                        if row is not None:
                            source = row
                        else:
                            raise ApiError(
                                409,
                                "REVIEW_TARGET_CHANGED",
                                "local review identity changed during refresh",
                            )
                    else:
                        source = current_source
                if row is None:
                    con.execute(
                        "INSERT INTO conversation_git_targets "
                        "(conversation_id,branch_name,base_ref,first_head_sha,"
                        "latest_head_sha,pr_number,pr_head_sha,pr_state,merge_sha,"
                        "merged_at,pr_url,pr_title,first_seen_at,last_seen_at,"
                        "remote_refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            conversation_id,
                            pull_request.head_ref,
                            pull_request.base_ref or source["base_ref"],
                            source["first_head_sha"],
                            source["latest_head_sha"],
                            pull_request.number,
                            pull_request.head_sha,
                            pull_request.state,
                            pull_request.merge_sha,
                            pull_request.merged_at,
                            pull_request.url,
                            pull_request.title,
                            source["first_seen_at"],
                            now,
                            now,
                        ),
                    )
                else:
                    con.execute(
                        "UPDATE conversation_git_targets SET base_ref=?,"
                        "latest_head_sha=?,pr_head_sha=?,pr_state=?,merge_sha=?,"
                        "merged_at=?,pr_url=?,pr_title=?,last_seen_at=?,"
                        "remote_refreshed_at=? "
                        "WHERE target_id=?",
                        (
                            pull_request.base_ref or row["base_ref"],
                            source["latest_head_sha"],
                            pull_request.head_sha,
                            pull_request.state,
                            pull_request.merge_sha,
                            pull_request.merged_at,
                            pull_request.url,
                            pull_request.title,
                            now,
                            now,
                            row["target_id"],
                        ),
                    )
    finally:
        con.close()


def _refresh_remote(
    conversation_id: str,
    worktree: str,
    owner_user_id: int,
    *,
    force: bool,
) -> tuple[dict[int, PullRequest], str, str | None]:
    now = time.monotonic()
    last = _REMOTE_ATTEMPTS.get(conversation_id)
    if not force and last is not None and now - last < REMOTE_TTL_SECONDS:
        return _REMOTE_RESULTS.get(conversation_id, ({}, "cached", None))
    try:
        pull_requests = READER_FACTORY(worktree).list()
    except GitHubReadError as exc:
        result = ({}, "unavailable", str(exc)[:240])
        _remember_remote(conversation_id, now, result)
        return result
    _persist_remote(conversation_id, worktree, owner_user_id, pull_requests)
    result = ({item.number: item for item in pull_requests}, "fresh", None)
    _remember_remote(conversation_id, now, result)
    return result


def _remember_remote(
    conversation_id: str,
    observed_at: float,
    result: tuple[dict[int, PullRequest], str, str | None],
) -> None:
    _REMOTE_ATTEMPTS[conversation_id] = observed_at
    _REMOTE_RESULTS[conversation_id] = result
    while len(_REMOTE_ATTEMPTS) > REMOTE_CACHE_LIMIT:
        oldest = min(_REMOTE_ATTEMPTS, key=_REMOTE_ATTEMPTS.__getitem__)
        _REMOTE_ATTEMPTS.pop(oldest, None)
        _REMOTE_RESULTS.pop(oldest, None)


def _target_summary(
    row: dict[str, Any],
    workspace: git_review.WorkspaceProjection,
    fresh_remote: dict[int, PullRequest],
) -> dict[str, Any]:
    pr_number = row["pr_number"]
    current = (
        row["branch_name"] == workspace.branch
        and row["latest_head_sha"] == workspace.head_sha
    )
    pull_request = (
        fresh_remote.get(int(pr_number))
        if pr_number is not None
        else None
    ) or _cached_pull_request(row)
    if pull_request is not None:
        lifecycle = lifecycle_status(pull_request)
        freshness = (
            "fresh"
            if int(pr_number) in fresh_remote
            else "cached"
        )
        kind = "pull_request"
    else:
        lifecycle = "pushed" if current and workspace.pushed else "local"
        freshness = "not_applicable"
        kind = "workspace" if current else "local_branch"
    core: dict[str, Any] = {
        "target_id": row["target_id"],
        "kind": kind,
        "branch": row["branch_name"],
        "head_sha": (
            pull_request.head_sha
            if pull_request is not None
            else row["latest_head_sha"]
        ),
        "pr_number": pr_number,
        "lifecycle": lifecycle,
        "local_fingerprint": workspace.fingerprint if current else None,
        "remote_freshness": freshness,
    }
    summary = {
        **core,
        "base_ref": row["base_ref"],
        "title": pull_request.title if pull_request is not None else None,
        "url": pull_request.url if pull_request is not None else None,
        "freshness": {
            "local": "fresh" if current else "stored",
            "remote": freshness,
        },
        "facts": {
            "checked_out": current,
            "dirty": bool(workspace.files) if current else False,
            "pushed": workspace.pushed if current else None,
            "ahead": workspace.ahead if current else None,
            "behind": workspace.behind if current else None,
            "cleanup_pending": lifecycle == "pr_merged" and current,
        },
    }
    digest = git_review.fingerprint(summary)
    return {
        **summary,
        "fingerprint": digest,
        "etag": git_review.etag(digest),
    }


def _selected_target(items: list[dict[str, Any]]) -> str | None:
    priority = {
        "pr_open": 1,
        "checks_failed": 1,
        "workspace": 2,
        "pushed": 3,
        "local": 4,
        "pr_merged": 5,
        "pr_closed": 6,
    }
    if not items:
        return None

    def target_priority(item: dict[str, Any]) -> int:
        facts = item["facts"]
        if item["kind"] == "workspace" and (
            facts["dirty"]
            or (facts["ahead"] or 0) > 0
            or facts["pushed"] is False
        ):
            return 0
        return priority.get(
            item["kind"]
            if item["kind"] == "workspace"
            else item["lifecycle"],
            10,
        )

    return min(
        enumerate(items),
        key=lambda pair: (
            target_priority(pair[1]),
            pair[0],
        ),
    )[1]["target_id"]


def _list_targets(
    conversation_id: str,
    owner_user_id: int,
    query: dict[str, str],
    headers,
):
    refresh = query.get("refresh")
    if refresh not in {None, "remote"}:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "refresh must be remote when provided",
        )
    con = _db()
    try:
        conversation = _conversation(con, conversation_id, owner_user_id)
        worktree = str(conversation["worktree"])
    finally:
        con.close()

    conversation_git_targets.safely_observe_and_persist(
        DB_PATH,
        conversation_id,
    )
    workspace = git_review.collect_workspace(worktree)
    fresh_remote, remote_freshness, remote_error = _refresh_remote(
        conversation_id,
        worktree,
        owner_user_id,
        force=refresh == "remote",
    )
    con = _db()
    try:
        current = _conversation(con, conversation_id, owner_user_id)
        if str(current["worktree"]) != worktree:
            raise ApiError(
                409,
                "REVIEW_TARGET_CHANGED",
                "conversation review source changed while it was being read",
            )
        rows = _targets(con, conversation_id)
    finally:
        con.close()
    items = [
        _target_summary(row, workspace, fresh_remote)
        for row in rows
    ]
    items.sort(
        key=lambda item: (
            not item["facts"]["checked_out"],
            item["kind"] != "pull_request",
            -(item["pr_number"] or 0),
            item["target_id"],
        )
    )
    body = {
        "conversation_id": conversation_id,
        "items": items,
        "selected_target_id": _selected_target(items),
        "freshness": {
            "local": "fresh",
            "remote": remote_freshness,
            "remote_error": remote_error,
        },
        "git_fingerprint": workspace.fingerprint,
    }
    response_etag = git_review.etag(git_review.fingerprint(body))
    return _etag_response(headers, response_etag, body)


def _pull_request_for_target(
    row: dict[str, Any],
) -> tuple[PullRequest, str]:
    cached = _cached_pull_request(row)
    if cached is None:
        raise ApiError(
            409,
            "REVIEW_TARGET_UNAVAILABLE",
            "pull-request target has no cached identity",
        )
    try:
        return READER_FACTORY(row["worktree"]).get(cached.number), "fresh"
    except GitHubReadError:
        return cached, "cached"


def _persist_artifact(
    row: dict[str, Any],
    owner_user_id: int,
    artifact: git_review.CacheArtifact,
) -> None:
    expected = _snapshot(row)
    con = _db()
    try:
        with db_driver.write_transaction(con, "conversation.review_patch.cache"):
            current = _target(con, row["target_id"], owner_user_id)
            if _snapshot(current) != expected:
                raise ApiError(
                    409,
                    "REVIEW_TARGET_CHANGED",
                    "review target changed while its patch was cached",
                )
            con.execute(
                "UPDATE conversation_git_targets SET patch_artifact=?,"
                "patch_sha256=? WHERE target_id=?",
                (
                    artifact.relative_path,
                    artifact.sha256,
                    row["target_id"],
                ),
            )
    finally:
        con.close()


def _canonical_projection(
    row: dict[str, Any],
    owner_user_id: int,
) -> tuple[git_review.CanonicalPatchSet, str]:
    pull_request, freshness = _pull_request_for_target(row)
    artifact = (
        git_review.CacheArtifact(row["patch_artifact"], row["patch_sha256"])
        if row["patch_artifact"] is not None
        else None
    )
    try:
        read = git_review.read_canonical_pr_patch(
            READER_FACTORY(row["worktree"]),
            pull_request,
            repository=row["worktree"],
            cache=CACHE_FACTORY(),
            cached_artifact=artifact,
        )
    except GitHubReadError as exc:
        raise ApiError(
            503,
            "REVIEW_REMOTE_UNAVAILABLE",
            "canonical pull-request patch is unavailable",
        ) from exc
    if read.artifact is not None and read.artifact != artifact:
        _persist_artifact(row, owner_user_id, read.artifact)
        row["patch_artifact"] = read.artifact.relative_path
        row["patch_sha256"] = read.artifact.sha256
    return git_review.parse_canonical_patch(read.patch), read.freshness or freshness


def _file_projection(
    row: dict[str, Any],
    owner_user_id: int,
    scope: str,
):
    if scope == "local":
        selected = row["pr_head_sha"] or row["latest_head_sha"]
        return git_review.local_only_files(row["worktree"], selected), "local"
    if row["pr_number"] is not None:
        return _canonical_projection(row, owner_user_id)
    if not row["base_ref"]:
        raise ApiError(409, "REVIEW_REF_MISSING", "review target has no base ref")
    current = git_review.collect_workspace(row["worktree"])
    include_worktree = (
        row["branch_name"] == current.branch
        and row["latest_head_sha"] == current.head_sha
    )
    return (
        git_review.review_files(
            row["worktree"],
            row["base_ref"],
            head_ref=row["latest_head_sha"],
            include_worktree=include_worktree,
        ),
        "local",
    )


def _filters(query: dict[str, str]) -> tuple[tuple[str, ...], str | None]:
    raw_status = query.get("status")
    statuses = tuple(filter(None, (raw_status or "").split(",")))
    if any(status not in _FILE_STATUSES for status in statuses):
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "status contains an unsupported value",
        )
    path = query.get("path")
    if path is not None:
        path = git_review.validate_review_path(path)
    return statuses, path


def _files(
    row: dict[str, Any],
    owner_user_id: int,
    query: dict[str, str],
    headers,
):
    scope = query.get("scope", "review")
    if scope not in {"review", "local"}:
        raise ApiError(422, "VALIDATION_ERROR", "scope must be review or local")
    limit = _limit(query)
    statuses, path_filter = _filters(query)
    projection, freshness = _file_projection(row, owner_user_id, scope)
    expected = _snapshot(row)
    files = [
        item
        for item in projection.files
        if (not statuses or item.status in statuses)
        and (path_filter is None or item.path.startswith(path_filter))
    ]
    filter_fingerprint = git_review.fingerprint(
        {
            "target_id": row["target_id"],
            "projection": projection.fingerprint,
            "statuses": statuses,
            "path": path_filter,
            "scope": scope,
        }
    )
    offset = 0
    if "cursor" in query:
        cursor = _cursor_decode(query["cursor"], "review_files")
        if cursor["fingerprint"] != filter_fingerprint:
            raise ApiError(
                409,
                "REVIEW_TARGET_CHANGED",
                "file projection changed after the cursor was issued",
            )
        offset = cursor["offset"]
        if offset > len(files):
            raise ApiError(422, "VALIDATION_ERROR", "cursor is invalid")
    page = files[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = (
        _cursor_encode(
            {
                "v": 1,
                "kind": "review_files",
                "offset": next_offset,
                "fingerprint": filter_fingerprint,
            }
        )
        if next_offset < len(files)
        else None
    )
    _revalidate(row["target_id"], owner_user_id, expected)
    body = {
        "target_id": row["target_id"],
        "scope": scope,
        "items": [
            {
                "file_id": "rf_" + git_review.fingerprint(
                    {
                        "target_id": row["target_id"],
                        "path": item.path,
                        "old_path": item.old_path,
                    }
                )[:32],
                **asdict(item),
            }
            for item in page
        ],
        "files_truncated": projection.files_truncated,
        "fingerprint": filter_fingerprint,
        "freshness": freshness,
        "next_cursor": next_cursor,
    }
    response_etag = git_review.etag(
        git_review.fingerprint(
            {
                "projection": filter_fingerprint,
                "offset": offset,
                "limit": limit,
                "items": [item.path for item in page],
            }
        )
    )
    return _etag_response(headers, response_etag, body)


def _diff(
    row: dict[str, Any],
    owner_user_id: int,
    query: dict[str, str],
    headers,
):
    scope = query.get("scope")
    if scope not in {"review", "local"}:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "scope is required and must be review or local",
        )
    path = git_review.validate_review_path(query.get("path", ""))
    projection, freshness = _file_projection(row, owner_user_id, scope)
    expected = _snapshot(row)
    selected = next((item for item in projection.files if item.path == path), None)
    if selected is None:
        raise ApiError(
            422,
            "REVIEW_PATH_INVALID",
            "path is not part of the current review projection",
        )
    if row["pr_number"] is not None and scope == "review":
        canonical = projection
        text = canonical.file_patches.get(path)
        patch = git_review.PatchProjection(
            text=text,
            sha256=(
                hashlib.sha256(text.encode("utf-8")).hexdigest()
                if text is not None
                else None
            ),
            truncated=canonical.patch_truncated,
            binary=selected.binary,
            unavailable_reason="binary" if selected.binary else None,
            etag=git_review.etag(git_review.fingerprint(text)),
        )
    else:
        old_ref = (
            projection.merge_base_sha
            if scope == "review"
            else projection.base_sha
        )
        if old_ref is None:
            raise ApiError(409, "REVIEW_REF_MISSING", "review base is unavailable")
        current = git_review.collect_workspace(row["worktree"])
        include_worktree = (
            row["branch_name"] == current.branch
            and (
                scope == "local"
                or row["latest_head_sha"] == current.head_sha
            )
        )
        patch = git_review.read_file_patch(
            row["worktree"],
            old_ref,
            path,
            new_ref=None if include_worktree else projection.head_sha,
        )
    if row["pr_number"] is None or scope == "local":
        current_projection, _ = _file_projection(row, owner_user_id, scope)
        if current_projection.fingerprint != projection.fingerprint:
            raise ApiError(
                409,
                "REVIEW_TARGET_CHANGED",
                "review files changed while the patch was being read",
            )
    _revalidate(row["target_id"], owner_user_id, expected)
    body = {
        "target_id": row["target_id"],
        "scope": scope,
        "path": path,
        "patch": patch.text,
        "sha256": patch.sha256,
        "truncated": patch.truncated,
        "binary": patch.binary,
        "unavailable_reason": patch.unavailable_reason,
        "freshness": freshness,
        "fingerprint": projection.fingerprint,
    }
    response_etag = git_review.etag(
        git_review.fingerprint(
            {
                "target_id": row["target_id"],
                "scope": scope,
                "path": path,
                "projection": projection.fingerprint,
                "patch": patch.sha256,
                "truncated": patch.truncated,
                "binary": patch.binary,
            }
        )
    )
    return _etag_response(headers, response_etag, body)


def _commits(
    row: dict[str, Any],
    owner_user_id: int,
    query: dict[str, str],
    headers,
):
    limit = _limit(query)
    if not row["base_ref"]:
        raise ApiError(409, "REVIEW_REF_MISSING", "review target has no base ref")
    expected = _snapshot(row)
    head_ref = row["pr_head_sha"] or row["latest_head_sha"]
    projection = git_review.review_commits(
        row["worktree"],
        row["base_ref"],
        head_ref=head_ref,
    )
    cursor_fingerprint = git_review.fingerprint(
        {
            "target_id": row["target_id"],
            "projection": projection.fingerprint,
        }
    )
    offset = 0
    if "cursor" in query:
        cursor = _cursor_decode(query["cursor"], "review_commits")
        if cursor["fingerprint"] != cursor_fingerprint:
            raise ApiError(
                409,
                "REVIEW_TARGET_CHANGED",
                "commit projection changed after the cursor was issued",
            )
        offset = cursor["offset"]
        if offset > len(projection.commits):
            raise ApiError(422, "VALIDATION_ERROR", "cursor is invalid")
    page = projection.commits[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = (
        _cursor_encode(
            {
                "v": 1,
                "kind": "review_commits",
                "offset": next_offset,
                "fingerprint": cursor_fingerprint,
            }
        )
        if next_offset < len(projection.commits)
        else None
    )
    _revalidate(row["target_id"], owner_user_id, expected)
    body = {
        "target_id": row["target_id"],
        "items": [asdict(item) for item in page],
        "commits_truncated": projection.commits_truncated,
        "fingerprint": projection.fingerprint,
        "next_cursor": next_cursor,
    }
    response_etag = git_review.etag(
        git_review.fingerprint(
            {
                "projection": projection.fingerprint,
                "offset": offset,
                "limit": limit,
                "items": [item.sha for item in page],
            }
        )
    )
    return _etag_response(headers, response_etag, body)


def _skill_names(con, shell_id: int) -> list[str]:
    return [
        str(row["name"])
        for row in con.execute(
            "SELECT sk.name FROM shell_skills ss "
            "JOIN skills sk ON sk.skill_id=ss.skill_id "
            "WHERE ss.shell_id=? AND sk.is_deleted=0 ORDER BY sk.name",
            (shell_id,),
        ).fetchall()
    ]


def _skill_roots(harness: str) -> list[str]:
    adapter = Path(__file__).resolve().parents[1] / "adapters" / harness / "adapter.json"
    roots = [".claude/skills"]
    try:
        payload = json.loads(adapter.read_text())
    except (OSError, json.JSONDecodeError):
        return roots
    declared = payload.get("skill_dirs")
    if isinstance(declared, list) and all(isinstance(item, str) for item in declared):
        roots = declared
    return list(dict.fromkeys(roots))


def _shell_source(worktree: Path, relative_path: str) -> dict[str, Any]:
    try:
        safe_path = git_review.validate_review_path(relative_path)
    except git_review.ReviewError:
        return {"available": False, "error": "REVIEW_PATH_INVALID"}
    candidate = worktree / safe_path
    try:
        direct = candidate.lstat()
    except OSError:
        return {"available": False, "error": "REVIEW_SHELL_FILE_UNAVAILABLE"}
    if stat.S_ISLNK(direct.st_mode):
        return {"available": False, "error": "REVIEW_SHELL_FILE_UNAVAILABLE"}
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        return {"available": False, "error": "REVIEW_SHELL_FILE_UNAVAILABLE"}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return {"available": False, "error": "REVIEW_SHELL_FILE_UNAVAILABLE"}
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve(strict=True)
            opened.relative_to(worktree)
        except (OSError, RuntimeError, ValueError):
            return {"available": False, "error": "REVIEW_SHELL_FILE_UNAVAILABLE"}
        if info.st_size > MAX_SHELL_FILE_BYTES:
            return {
                "available": False,
                "error": "REVIEW_SHELL_FILE_TOO_LARGE",
                "bytes": info.st_size,
            }
        chunks: list[bytes] = []
        remaining = MAX_SHELL_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        return {"available": False, "error": "REVIEW_SHELL_FILE_UNAVAILABLE"}
    finally:
        os.close(descriptor)
    if len(raw) > MAX_SHELL_FILE_BYTES:
        return {
            "available": False,
            "error": "REVIEW_SHELL_FILE_TOO_LARGE",
            "bytes": len(raw),
        }
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"available": False, "error": "REVIEW_SHELL_FILE_NOT_TEXT"}
    if "\0" in body:
        return {"available": False, "error": "REVIEW_SHELL_FILE_NOT_TEXT"}
    return {
        "available": True,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "body": body,
    }


def _shell_projection(
    worktree_raw: str,
    harness: str,
    skill_names: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        worktree = Path(worktree_raw).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise git_review.ReviewError(
            "REVIEW_WORKTREE_MISSING",
            "Conversation worktree is unavailable",
        ) from exc
    sources: list[dict[str, Any]] = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        sources.append(
            {
                "kind": "boot",
                "name": name,
                "path": name,
                **_shell_source(worktree, name),
            }
        )
    skill_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    roots = _skill_roots(harness)
    for skill_name in skill_names:
        if not skill_pattern.fullmatch(skill_name):
            continue
        for root in roots:
            relative = f"{root}/{skill_name}/SKILL.md"
            sources.append(
                {
                    "kind": "skill",
                    "name": skill_name,
                    "path": relative,
                    **_shell_source(worktree, relative),
                }
            )
    sources = sources[:MAX_SHELL_FILES]
    total = 0
    for source in sources:
        if not source.get("available"):
            continue
        total += int(source["bytes"])
        if total > MAX_SHELL_TOTAL_BYTES:
            source.pop("body", None)
            source.pop("sha256", None)
            source["available"] = False
            source["error"] = "REVIEW_SHELL_FILES_TOO_LARGE"

    public: list[dict[str, Any]] = []
    internal: list[dict[str, Any]] = []
    for source in (item for item in sources if item["kind"] == "boot"):
        descriptor = {
            key: source.get(key)
            for key in ("kind", "name", "available", "bytes", "sha256", "error")
            if source.get(key) is not None
        }
        descriptor["paths"] = [source["path"]]
        public.append(descriptor)
        internal.append({**descriptor, "bodies": [source.get("body")]})

    for skill_name in skill_names:
        items = [
            item for item in sources
            if item["kind"] == "skill" and item["name"] == skill_name
        ]
        available = [item for item in items if item.get("available")]
        digests = {item["sha256"] for item in available}
        mismatch = len(digests) > 1
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in available:
            groups.setdefault(item["sha256"], []).append(item)
        for digest, group in groups.items():
            descriptor = {
                "kind": "skill",
                "name": skill_name,
                "available": True,
                "bytes": group[0]["bytes"],
                "sha256": digest,
                "paths": [item["path"] for item in group],
                "mismatch": mismatch,
            }
            public.append(descriptor)
            internal.append(
                {**descriptor, "bodies": [item["body"] for item in group]}
            )
        for item in (entry for entry in items if not entry.get("available")):
            descriptor = {
                "kind": "skill",
                "name": skill_name,
                "available": False,
                "paths": [item["path"]],
                "error": item.get("error", "REVIEW_SHELL_FILE_UNAVAILABLE"),
                "mismatch": mismatch,
            }
            public.append(descriptor)
            internal.append({**descriptor, "bodies": [None]})
    return public, internal


def _observation_fingerprint(
    conversation_id: str,
    projection: git_review.WorktreeProjection,
    shell_files: list[dict[str, Any]],
) -> str:
    return git_review.fingerprint(
        {
            "conversation_id": conversation_id,
            "projection": projection.fingerprint,
            "shell_files": shell_files,
        }
    )


def _file_descriptor(
    fingerprint_value: str,
    section: str,
    item: git_review.FileProjection,
) -> dict[str, Any]:
    file_id = "rf_" + git_review.fingerprint(
        {"fingerprint": fingerprint_value, "section": section, "path": item.path}
    )[:32]
    return {**asdict(item), "file_id": file_id}


def _build_observation(
    conversation: dict[str, Any],
    owner_user_id: int,
    *,
    fetch: git_review.FetchProjection,
) -> dict[str, Any]:
    worktree = str(conversation["worktree"])
    projection = git_review.project_current_worktree(worktree)
    con = _db()
    try:
        skill_names = _skill_names(con, int(conversation["shell_id"]))
    finally:
        con.close()
    shell_public, shell_internal = _shell_projection(
        worktree,
        str(conversation["harness"]),
        skill_names,
    )
    digest = _observation_fingerprint(
        str(conversation["conversation_id"]),
        projection,
        shell_public,
    )
    dirty = [_file_descriptor(digest, "dirty", item) for item in projection.dirty]
    branch_files = [
        _file_descriptor(digest, "branch", item) for item in projection.branch_files
    ]
    file_map = {
        item["file_id"]: {"section": section, "path": item["path"]}
        for section, items in (("dirty", dirty), ("branch", branch_files))
        for item in items
    }
    shell_map: dict[str, dict[str, Any]] = {}
    for index, (public, internal) in enumerate(zip(shell_public, shell_internal)):
        shell_id = "sf_" + git_review.fingerprint(
            {"fingerprint": digest, "index": index, "paths": public["paths"]}
        )[:32]
        public["file_id"] = shell_id
        internal["file_id"] = shell_id
        if public.get("available"):
            shell_map[shell_id] = internal
    observed_at = datetime.now(timezone.utc).isoformat()
    body = {
        "conversation_id": conversation["conversation_id"],
        "fingerprint": digest,
        "observed_at": observed_at,
        "fetch": {
            "fresh": fetch.fresh,
            "error": fetch.error,
            "base_stale": not fetch.fresh and projection.base_available,
        },
        "status": {
            "branch": projection.branch,
            "head_sha": projection.head_sha,
            "base_sha": projection.base_sha,
            "base_available": projection.base_available,
            "dirty_count": len(dirty),
            "ahead_count": projection.visible_ahead,
            "behind": projection.behind,
        },
        "changes": {
            "dirty": dirty,
            "branch": branch_files,
            "commits": [asdict(item) for item in projection.commits],
            "files_truncated": projection.files_truncated,
            "commits_truncated": projection.commits_truncated,
        },
        "shell_files": shell_public,
        "no_code_changes": not dirty and not branch_files and not projection.commits,
    }
    return {
        "conversation_id": conversation["conversation_id"],
        "owner_user_id": owner_user_id,
        "shell_id": int(conversation["shell_id"]),
        "harness": str(conversation["harness"]),
        "worktree": worktree,
        "projection": projection,
        "fingerprint": digest,
        "file_map": file_map,
        "shell_map": shell_map,
        "body": body,
    }


def _remember_observation(snapshot: dict[str, Any]) -> None:
    digest = snapshot["fingerprint"]
    _OBSERVATIONS.setdefault(digest, snapshot)
    _OBSERVATIONS.move_to_end(digest)
    while len(_OBSERVATIONS) > OBSERVATION_CACHE_LIMIT:
        old_digest, _old = _OBSERVATIONS.popitem(last=False)
        for key, value in list(_OBSERVATION_KEYS.items()):
            if value["fingerprint"] == old_digest:
                _OBSERVATION_KEYS.pop(key, None)


def _create_observation(
    conversation_id: str,
    owner_user_id: int,
    idempotency_key: str,
):
    key = (owner_user_id, conversation_id, idempotency_key)
    with _OBSERVATION_LOCK:
        cached = _OBSERVATION_KEYS.get(key)
        if cached is not None:
            _OBSERVATION_KEYS.move_to_end(key)
            return conversation_routes._json(200, cached["body"])
        con = _db()
        try:
            row = _conversation(con, conversation_id, owner_user_id)
            conversation = dict(row)
        finally:
            con.close()
        fetch = FETCHER(str(conversation["worktree"]))
        snapshot = _build_observation(conversation, owner_user_id, fetch=fetch)
        _remember_observation(snapshot)
        _OBSERVATION_KEYS[key] = {
            "fingerprint": snapshot["fingerprint"],
            "body": snapshot["body"],
        }
        _OBSERVATION_KEYS.move_to_end(key)
        while len(_OBSERVATION_KEYS) > OBSERVATION_KEY_CACHE_LIMIT:
            _OBSERVATION_KEYS.popitem(last=False)
        return conversation_routes._json(201, snapshot["body"])


def _observation(digest: str, owner_user_id: int) -> dict[str, Any]:
    snapshot = _OBSERVATIONS.get(digest)
    if snapshot is None or snapshot["owner_user_id"] != owner_user_id:
        raise ApiError(
            404,
            "REVIEW_OBSERVATION_NOT_FOUND",
            "review observation does not exist",
        )
    _OBSERVATIONS.move_to_end(digest)
    return snapshot


def _observation_source(snapshot: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    con = _db()
    try:
        row = _conversation(
            con,
            str(snapshot["conversation_id"]),
            int(snapshot["owner_user_id"]),
        )
        conversation = dict(row)
        skill_names = _skill_names(con, int(conversation["shell_id"]))
    finally:
        con.close()
    if (
        str(conversation["worktree"]) != snapshot["worktree"]
        or int(conversation["shell_id"]) != snapshot["shell_id"]
        or str(conversation["harness"]) != snapshot["harness"]
    ):
        raise ApiError(
            409,
            "REVIEW_SNAPSHOT_CHANGED",
            "review source changed after the observation",
        )
    return conversation, skill_names


def _ensure_observation_current(snapshot: dict[str, Any]) -> None:
    _conversation_row, skill_names = _observation_source(snapshot)
    projection = git_review.project_current_worktree(snapshot["worktree"])
    shell_public, _internal = _shell_projection(
        snapshot["worktree"],
        snapshot["harness"],
        skill_names,
    )
    current = _observation_fingerprint(
        snapshot["conversation_id"],
        projection,
        shell_public,
    )
    if current != snapshot["fingerprint"]:
        raise ApiError(
            409,
            "REVIEW_SNAPSHOT_CHANGED",
            "worktree changed after the observation",
        )


def _observation_patch(snapshot: dict[str, Any], query: dict[str, str], headers):
    file_id = query.get("file") or ""
    selected = snapshot["file_map"].get(file_id)
    if selected is None:
        raise ApiError(422, "REVIEW_PATH_INVALID", "review file is not in the snapshot")
    _ensure_observation_current(snapshot)
    projection = snapshot["projection"]
    if selected["section"] == "dirty":
        patch = git_review.read_file_patch(
            snapshot["worktree"],
            projection.head_sha,
            selected["path"],
        )
    else:
        patch = git_review.read_file_patch(
            snapshot["worktree"],
            projection.merge_base_sha,
            selected["path"],
            new_ref=projection.head_sha,
        )
    _ensure_observation_current(snapshot)
    body = {
        "fingerprint": snapshot["fingerprint"],
        "file_id": file_id,
        "section": selected["section"],
        "patch": patch.text,
        "sha256": patch.sha256,
        "truncated": patch.truncated,
        "binary": patch.binary,
        "unavailable_reason": patch.unavailable_reason,
    }
    return _etag_response(headers, patch.etag, body)


def _observation_shell_file(snapshot: dict[str, Any], query: dict[str, str], headers):
    file_id = query.get("file") or ""
    selected = snapshot["shell_map"].get(file_id)
    if selected is None:
        raise ApiError(
            422,
            "REVIEW_PATH_INVALID",
            "shell file is not in the snapshot",
        )
    _observation_source(snapshot)
    bodies = selected["bodies"]
    _observation_source(snapshot)
    response_etag = git_review.etag(selected["sha256"])
    return _etag_response(
        headers,
        response_etag,
        {
            "fingerprint": snapshot["fingerprint"],
            "file_id": file_id,
            "paths": selected["paths"],
            "body": bodies[0],
            "mismatch": selected.get("mismatch", False),
        },
    )


_REVIEW_ERROR_STATUS = {
    "REVIEW_TARGET_NOT_FOUND": 404,
    "REVIEW_WORKTREE_MISSING": 409,
    "REVIEW_NOT_A_GIT_REPOSITORY": 409,
    "REVIEW_REF_MISSING": 409,
    "REVIEW_REMOTE_UNAVAILABLE": 503,
    "REVIEW_PATH_INVALID": 422,
    "REVIEW_DIFF_TOO_LARGE": 413,
    "REVIEW_TARGET_CHANGED": 409,
    "REVIEW_TARGET_UNAVAILABLE": 503,
    "REVIEW_SNAPSHOT_CHANGED": 409,
    "REVIEW_OBSERVATION_NOT_FOUND": 404,
    "REVIEW_SHELL_FILE_TOO_LARGE": 413,
    "REVIEW_SHELL_FILES_TOO_LARGE": 413,
    "REVIEW_SHELL_FILE_NOT_TEXT": 415,
}


def handle(method: str, path: str, headers_raw: str, raw_body: bytes):
    """Dispatch authenticated historical and current-worktree review reads."""
    headers = conversation_routes._parse_headers(headers_raw)
    if not conversation_routes._host_ok(headers):
        return conversation_routes._err(403, "HOST_FORBIDDEN", "Host is not allowed")
    parsed = urlparse(path)
    conversation_match = _CONVERSATION_TARGETS.fullmatch(parsed.path)
    target_match = _TARGET_RESOURCE.fullmatch(parsed.path)
    observation_match = _CONVERSATION_OBSERVATIONS.fullmatch(parsed.path)
    observation_resource_match = _OBSERVATION_RESOURCE.fullmatch(parsed.path)
    if all(
        match is None
        for match in (
            conversation_match,
            target_match,
            observation_match,
            observation_resource_match,
        )
    ):
        return conversation_routes._err(404, "NOT_FOUND", "route does not exist")
    expected_method = "POST" if observation_match is not None else "GET"
    if method != expected_method:
        return conversation_routes._err(
            405,
            "METHOD_NOT_ALLOWED",
            "review method is not allowed",
            headers=[("Allow", expected_method)],
        )
    if not conversation_routes._mutation_site_ok(headers):
        return conversation_routes._err(
            403,
            "NOT_SAME_ORIGIN",
            "cross-site review request rejected",
        )
    try:
        con = _db()
        try:
            operator = conversation_routes._operator(con, headers)
            if conversation_match is not None or observation_match is not None:
                matched_conversation = conversation_match or observation_match
                _conversation(
                    con,
                    matched_conversation.group(1),
                    operator["user_id"],
                )
            elif target_match is not None:
                row = _target(con, target_match.group(1), operator["user_id"])
        finally:
            con.close()
        if observation_match is not None:
            _query(parsed.query, set())
            if raw_body:
                raise ApiError(422, "VALIDATION_ERROR", "request body must be empty")
            idempotency_key = conversation_routes._idempotency_key(headers)
            return _create_observation(
                observation_match.group(1),
                operator["user_id"],
                idempotency_key,
            )
        if observation_resource_match is not None:
            snapshot = _observation(
                observation_resource_match.group(1),
                operator["user_id"],
            )
            query = _query(parsed.query, {"file"})
            if observation_resource_match.group(2) == "patch":
                return _observation_patch(snapshot, query, headers)
            return _observation_shell_file(snapshot, query, headers)
        if conversation_match is not None:
            query = _query(parsed.query, {"refresh"})
            return _list_targets(
                conversation_match.group(1),
                operator["user_id"],
                query,
                headers,
            )
        resource = target_match.group(2)
        if resource == "files":
            query = _query(
                parsed.query,
                {"scope", "cursor", "limit", "status", "path"},
            )
            return _files(row, operator["user_id"], query, headers)
        if resource == "diff":
            query = _query(parsed.query, {"scope", "path"})
            return _diff(row, operator["user_id"], query, headers)
        query = _query(parsed.query, {"cursor", "limit"})
        return _commits(row, operator["user_id"], query, headers)
    except ApiError as exc:
        return conversation_routes._api_error(exc)
    except git_review.ReviewError as exc:
        return conversation_routes._err(
            _REVIEW_ERROR_STATUS.get(exc.code, 503),
            exc.code,
            str(exc),
        )
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return conversation_routes._err(
                503,
                "DATABASE_BUSY",
                "database is busy; retry the request",
            )
        return conversation_routes._err(
            500,
            "INTERNAL_ERROR",
            "review request failed",
        )
    except Exception:  # noqa: BLE001 - request isolation returns a safe envelope
        return conversation_routes._err(
            500,
            "INTERNAL_ERROR",
            "review request failed",
        )
