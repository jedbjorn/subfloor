#!/usr/bin/env python3
"""Verify that the fork dispatcher and materialized engine are callable together."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


_SCRIPT_REF_RE = re.compile(r'"\$S/([^"$]+)"')
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def read_engine_ref(repo_root: Path) -> str | None:
    try:
        value = (repo_root / ".sc-state" / "engine.ref").read_text().strip()
    except OSError:
        return None
    return value if _SHA_RE.fullmatch(value) else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dispatcher_at_ref(repo_root: Path, ref: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:sc"],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _manifest_dispatcher_hash(repo_root: Path) -> str | None:
    path = repo_root / ".super-coder" / "engine.manifest"
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    digest = manifest.get("sc") if isinstance(manifest, dict) else None
    return digest if isinstance(digest, str) else None


def inspect_callable_floor(
    repo_root: Path,
    *,
    expected_ref: str | None,
    allow_unpinned: bool = False,
) -> tuple[str, ...]:
    """Return every dispatcher/engine mismatch without changing the floor."""
    dispatcher = repo_root / "sc"
    try:
        dispatcher_bytes = dispatcher.read_bytes()
    except OSError:
        return (f"dispatcher is missing or unreadable: {dispatcher}",)

    issues: list[str] = []
    if dispatcher_bytes.startswith(b"#!") and not os.access(dispatcher, os.X_OK):
        issues.append(f"dispatcher is not executable: {dispatcher}")

    if expected_ref is None:
        if not allow_unpinned:
            issues.append(".sc-state/engine.ref is missing or empty")
    else:
        target_dispatcher = _dispatcher_at_ref(repo_root, expected_ref)
        if target_dispatcher is not None:
            if dispatcher_bytes != target_dispatcher:
                issues.append(
                    f"dispatcher does not match engine ref {expected_ref[:12]}"
                )
        else:
            expected_hash = _manifest_dispatcher_hash(repo_root)
            if expected_hash is None:
                issues.append(
                    f"engine ref {expected_ref[:12]} is unavailable locally and "
                    "the materialized manifest has no dispatcher hash"
                )
            elif _sha256(dispatcher_bytes) != expected_hash:
                issues.append(
                    f"dispatcher does not match the materialized manifest for "
                    f"engine ref {expected_ref[:12]}"
                )

    text = dispatcher_bytes.decode(errors="replace")
    missing = sorted(
        {
            script
            for script in _SCRIPT_REF_RE.findall(text)
            if not (repo_root / ".super-coder" / "scripts" / script).is_file()
        }
    )
    if missing:
        issues.append(
            "dispatcher routes to missing engine script(s): " + ", ".join(missing)
        )
    return tuple(issues)


def require_callable_floor(
    repo_root: Path,
    *,
    expected_ref: str | None,
    allow_unpinned: bool = False,
    context: str,
) -> None:
    issues = inspect_callable_floor(
        repo_root,
        expected_ref=expected_ref,
        allow_unpinned=allow_unpinned,
    )
    if not issues:
        return
    detail = "\n".join(f"  - {issue}" for issue in issues)
    raise SystemExit(
        f"{context}: callable dispatcher/engine floor mismatch:\n{detail}\n"
        "  supported repair: run the update from the main checkout:\n"
        f"    cd {repo_root} && ./sc update"
    )
