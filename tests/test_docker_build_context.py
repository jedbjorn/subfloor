#!/usr/bin/env python3
"""Regression coverage for the repository-owned sandbox build context."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / ".dockerignore"


def test_sandbox_context_is_empty_and_default_deny() -> None:
    rules = [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert rules == ["*"]


def test_sandbox_dockerfile_copies_no_repository_paths() -> None:
    dockerfile = (ROOT / ".super-coder" / "Dockerfile").read_text()
    instructions = [
        line.strip().split(maxsplit=1)[0].upper()
        for line in dockerfile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "COPY" not in instructions
    assert "ADD" not in instructions
