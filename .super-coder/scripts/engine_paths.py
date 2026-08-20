#!/usr/bin/env python3
"""Exact repo-local paths emitted or wholly owned by subfloor."""
from __future__ import annotations

from pathlib import Path, PurePosixPath


GENERATED_INSTALL_FILES = (
    Path("CLAUDE.md"),
    Path("AGENTS.md"),
    Path("opencode.json"),
    Path(".claude/settings.local.json"),
    Path(".codex/hooks.json"),
    Path("roadmap_sc.md"),
)

GENERATED_INSTALL_DIRS = (
    Path(".claude/skills"),
    Path(".agents/skills"),
    Path(".opencode/skills"),
    Path("docs_sc"),
    Path("specs_sc"),
    Path("skills_sc"),
)

# Compatibility surface used by install/remove callers.
GENERATED_INSTALL_PATHS = GENERATED_INSTALL_FILES + GENERATED_INSTALL_DIRS
_GENERATED_INSTALL_FILE_SET = {
    PurePosixPath(path.as_posix()) for path in GENERATED_INSTALL_FILES
}
_GENERATED_INSTALL_DIR_SET = {
    PurePosixPath(path.as_posix()) for path in GENERATED_INSTALL_DIRS
}


def is_generated_install_path(relative: str | Path | PurePosixPath) -> bool:
    """Return whether a repo-relative path is an exact owned file or subtree."""
    raw = relative.as_posix() if isinstance(relative, Path) else str(relative)
    candidate = PurePosixPath(raw)
    return candidate in _GENERATED_INSTALL_FILE_SET or any(
        candidate == directory or directory in candidate.parents
        for directory in _GENERATED_INSTALL_DIR_SET
    )
