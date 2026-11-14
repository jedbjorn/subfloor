"""Is an engine update available? The Engine drawer's update badge.

Served by the review API (`GET /api/engine-ops/update-check`), never by the
host-side engine-ops service: it reads the checkout and runs read-only git
there, which is the review API's own trust level and not the service's.

What `./sc update` would move, and what it is compared against:

* A fork pins its engine in `.sc-state/engine.ref` and fetches it from the
  locator in `.sc-state/engine.source`, branch `main` (the update verb's
  default). Installed = the pin; upstream = that branch's head.
* The source repo has no pin: `./sc update` fast-forwards the checkout to its
  upstream branch. Installed = HEAD; upstream = the `@{u}` branch's head.

The upstream head is read with `git ls-remote` — no objects are fetched and
nothing in the checkout changes. Installed equal to, or ahead of, upstream is
`current`; anything else is `available`. A failure is `unknown`, said with
its reason, never guessed. Answers are cached per installed ref, so opening
the drawer does not touch the network every time, and an update that moved
the ref is re-checked at once.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

import engine_ops

REPO_ROOT = engine_ops.REPO_ROOT
ENGINE_SOURCE = REPO_ROOT / ".sc-state" / "engine.source"
EJECTED_MARKER = REPO_ROOT / ".sc-state" / "ejected"
UPDATE_BRANCH = "main"          # `./sc update` with no --branch
TTL_SECONDS = 600
FAILURE_TTL_SECONDS = 60
GIT_TIMEOUT = 20
_SHA = re.compile(r"[0-9a-f]{40}")
_SOURCE = re.compile(r"(https|ssh|git|file)://\S+|git@[^:\s]+:\S+")

_lock = threading.Lock()
_cache: dict = {}               # {"key", "at", "ttl", "answer"}


class Unknown(Exception):
    """The check could not be made; the message says why."""


def _git(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "-c", "protocol.ext.allow=never", *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Unknown(f"git {args[0]} timed out after {GIT_TIMEOUT}s") from exc
    except OSError as exc:
        raise Unknown(f"git is unavailable: {exc}") from exc


def _remote_head(remote: str, branch: str) -> str:
    result = _git("ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise Unknown(detail[-1] if detail else f"git ls-remote {remote} failed")
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}" and _SHA.fullmatch(sha):
            return sha
    raise Unknown(f"{remote} has no branch {branch}")


def _target() -> tuple[str, str, str, str]:
    """(mode, installed, remote, branch) for what `./sc update` would move."""
    pin = engine_ops.engine_ref()
    if pin:
        try:
            source = ENGINE_SOURCE.read_text().strip()
        except OSError as exc:
            raise Unknown("no engine source is recorded (.sc-state/engine.source)") from exc
        if not _SOURCE.fullmatch(source):
            raise Unknown("the recorded engine source is not a git locator")
        return "fork", pin, source, UPDATE_BRANCH
    head = _git("rev-parse", "HEAD").stdout.strip()
    if not _SHA.fullmatch(head):
        raise Unknown("the checkout has no HEAD commit")
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    remote, _, branch = upstream.stdout.strip().partition("/")
    if upstream.returncode != 0 or not branch:
        raise Unknown("the checkout's branch tracks no upstream")
    return "source", head, remote, branch


def _compare(installed: str, upstream: str) -> str:
    if installed == upstream:
        return "current"
    # 0: upstream is already in installed (installed is ahead). 1: it is not.
    # Otherwise the upstream commit was never fetched here, so it is newer.
    ancestry = _git("merge-base", "--is-ancestor", upstream, installed)
    return "current" if ancestry.returncode == 0 else "available"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check() -> dict:
    """{state: available|current|unknown, installed, upstream, checked_at, ...}."""
    with _lock:
        if EJECTED_MARKER.exists():
            return {"state": "unknown", "reason": "this fork has ejected; "
                    "there is no upstream engine", "checked_at": _now()}
        try:
            mode, installed, remote, branch = _target()
        except Unknown as exc:
            return {"state": "unknown", "reason": str(exc), "checked_at": _now()}
        key = (mode, installed, remote, branch)
        cached = _cache.get("answer")
        if (cached and _cache["key"] == key
                and time.monotonic() - _cache["at"] < _cache["ttl"]):
            return cached
        answer = {"mode": mode, "installed": installed, "branch": branch,
                  "upstream": None, "checked_at": _now()}
        try:
            answer["upstream"] = _remote_head(remote, branch)
            answer["state"] = _compare(installed, answer["upstream"])
            ttl = TTL_SECONDS
        except Unknown as exc:
            answer.update(state="unknown", reason=str(exc))
            ttl = FAILURE_TTL_SECONDS
        _cache.update(key=key, at=time.monotonic(), ttl=ttl, answer=answer)
        return answer
