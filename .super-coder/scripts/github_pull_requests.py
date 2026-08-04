"""Bounded, read-only GitHub pull-request collection.

This is the shared normalization seam for git hygiene, browser Diff review,
and later PR-aware engine surfaces.  It invokes only ``gh pr`` read commands;
callers decide whether branch-name discovery is sufficient or an exact PR
number is required.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LIST_LIMIT = 300
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
GITHUB_TIMEOUT_SECONDS = 20.0
_FIELDS = (
    "number,headRefName,baseRefName,baseRefOid,headRefOid,state,mergedAt,mergeCommit,"
    "title,url,reviewDecision,statusCheckRollup"
)
_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
_HEX = frozenset("0123456789abcdef")
_FAILED_CHECKS = frozenset(
    {
        "ACTION_REQUIRED",
        "CANCELLED",
        "ERROR",
        "FAILURE",
        "STALE",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
)
_PENDING_CHECKS = frozenset(
    {"EXPECTED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
)
_SUCCESSFUL_CHECKS = frozenset({"NEUTRAL", "SKIPPED", "SUCCESS"})
_READ_ONLY_ENV = {
    **os.environ,
    "GH_PROMPT_DISABLED": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


class GitHubReadError(RuntimeError):
    """A sanitized, retryable failure from the read-only GitHub seam."""


class GitHubResponseTooLarge(GitHubReadError):
    """GitHub returned more data than the review service accepts."""


def _run_read_process(
    args: list[str],
    *,
    cwd: str,
    stdin: Any,
    capture_output: bool,
    text: bool,
    timeout: float,
    check: bool,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run one read in its own group and reap the group on timeout."""
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
    completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            args,
            output=stdout,
            stderr=stderr,
        )
    return completed


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_ref: str
    base_ref: str | None
    head_sha: str | None
    state: str
    merged_at: str | None
    merge_sha: str | None
    title: str | None
    url: str | None
    review_decision: str | None
    checks: str | None
    checks_failed: bool
    base_sha: str | None = None

    def hygiene_dict(self) -> dict[str, int | str]:
        """The deliberately small projection consumed by git hygiene."""
        return {"number": self.number, "state": self.state}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _sha_value(value: Any) -> str | None:
    text = _bounded_text(value, 64)
    if text is None:
        return None
    text = text.lower()
    if len(text) not in {40, 64} or any(char not in _HEX for char in text):
        raise GitHubReadError("GitHub PR response has an invalid commit SHA")
    return text


def _check_state(raw: Any) -> tuple[str | None, bool]:
    if not isinstance(raw, list):
        return None, False
    states: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        state = item.get("conclusion") or item.get("state") or item.get("status")
        if state:
            states.append(str(state).upper())
    if any(state in _FAILED_CHECKS for state in states):
        return "FAILURE", True
    if any(state in _PENDING_CHECKS for state in states):
        return "PENDING", False
    if "COMPLETED" in states:
        return "PENDING", False
    if any(state not in _SUCCESSFUL_CHECKS for state in states):
        return "PENDING", False
    return ("SUCCESS", False) if states else (None, False)


def normalize_pull_request(raw: dict[str, Any]) -> PullRequest:
    """Normalize both ``gh pr list/view`` payloads and deterministic fixtures."""
    try:
        number = int(raw["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubReadError("GitHub PR response has no valid number") from exc
    if number < 1:
        raise GitHubReadError("GitHub PR response has no valid number")

    head_ref = _bounded_text(raw.get("headRefName"), 1024)
    state = str(raw.get("state") or "").upper()
    if not head_ref or state not in _STATES:
        raise GitHubReadError("GitHub PR response has invalid identity or state")

    merge = raw.get("mergeCommit")
    merge_sha = merge.get("oid") if isinstance(merge, dict) else None
    checks, checks_failed = _check_state(raw.get("statusCheckRollup"))
    return PullRequest(
        number=number,
        head_ref=head_ref,
        base_ref=_bounded_text(raw.get("baseRefName"), 1024),
        head_sha=_sha_value(raw.get("headRefOid")),
        state=state,
        merged_at=_bounded_text(raw.get("mergedAt"), 64),
        merge_sha=_sha_value(merge_sha),
        title=_bounded_text(raw.get("title"), 500),
        url=_bounded_text(raw.get("url"), 2048),
        review_decision=_bounded_text(raw.get("reviewDecision"), 64),
        checks=checks,
        checks_failed=checks_failed,
        base_sha=_sha_value(raw.get("baseRefOid")),
    )


def newest_by_branch(
    pull_requests: Iterable[PullRequest],
) -> dict[str, PullRequest]:
    """Collapse discovery results for hygiene only.

    Review callers retain the complete list because PR number, not branch name,
    is their durable identity.
    """
    result: dict[str, PullRequest] = {}
    for pull_request in pull_requests:
        previous = result.get(pull_request.head_ref)
        if previous is None or pull_request.number > previous.number:
            result[pull_request.head_ref] = pull_request
    return result


def lifecycle_status(pull_request: PullRequest) -> str:
    if pull_request.state == "MERGED":
        return "pr_merged"
    if pull_request.state == "CLOSED":
        return "pr_closed"
    if pull_request.checks_failed:
        return "checks_failed"
    return "pr_open"


def _safe_error(stderr: Any) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    line = str(stderr or "").strip().splitlines()
    return line[0][:240] if line else "GitHub read failed"


class GitHubPullRequestReader:
    """Execute bounded ``gh`` reads from one repository."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        repository: str | None = None,
        runner: Callable[..., Any] = _run_read_process,
        timeout: float = GITHUB_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.repository = repository.strip() if repository else None
        self.runner = runner
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def _repo_args(self) -> list[str]:
        return ["--repo", self.repository] if self.repository else []

    def _run(self, args: list[str]) -> str:
        try:
            result = self.runner(
                args,
                cwd=str(self.repo_root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=self.timeout,
                check=False,
                env=_READ_ONLY_ENV,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubReadError(f"GitHub read unavailable: {exc}") from exc

        stdout = result.stdout or b""
        if isinstance(stdout, str):
            encoded = stdout.encode("utf-8")
            text = stdout
        else:
            encoded = bytes(stdout)
            text = encoded.decode("utf-8", errors="replace")
        if len(encoded) > self.max_response_bytes:
            raise GitHubResponseTooLarge("GitHub response exceeded the hard limit")
        if result.returncode != 0:
            raise GitHubReadError(_safe_error(result.stderr))
        return text

    def list(self) -> list[PullRequest]:
        raw = self._run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "all",
                "--limit",
                str(LIST_LIMIT),
                "--json",
                _FIELDS,
                *self._repo_args(),
            ]
        )
        try:
            payload = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise GitHubReadError("GitHub PR list returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise GitHubReadError("GitHub PR list returned an invalid payload")
        return [
            normalize_pull_request(item) for item in payload if isinstance(item, dict)
        ]

    def get(self, number: int) -> PullRequest:
        if not isinstance(number, int) or number < 1:
            raise ValueError("PR number must be a positive integer")
        raw = self._run(
            ["gh", "pr", "view", str(number), "--json", _FIELDS,
             *self._repo_args()]
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubReadError("GitHub PR read returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubReadError("GitHub PR read returned an invalid payload")
        return normalize_pull_request(payload)

    def patch(self, number: int) -> str:
        if not isinstance(number, int) or number < 1:
            raise ValueError("PR number must be a positive integer")
        return self._run(
            ["gh", "pr", "diff", str(number), "--patch", *self._repo_args()]
        )
