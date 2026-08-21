#!/usr/bin/env python3
"""Regression coverage for the repository-owned sandbox build context."""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / ".dockerignore"
DEEPSEEK_ASSETS = ".super-coder/assets/deepseek"
REQUIRED_SCRIPTS = {
    ".super-coder/scripts/build_deepseek_carrier.py",
    ".super-coder/scripts/cli_entry.py",
    ".super-coder/scripts/deepseek_carrier_worker.py",
    ".super-coder/scripts/deepseek_runtime.py",
    ".super-coder/scripts/deepseek_skill_resume_canary.py",
}
BOOTSTRAP_ENTRYPOINTS = {
    ".super-coder/scripts/cli_entry.py",
    ".super-coder/scripts/deepseek_runtime.py",
}
EXPECTED_RULES = [
    "*",
    "!.super-coder",
    "!.super-coder/scripts",
    "!.super-coder/scripts/deepseek_runtime.py",
    "!.super-coder/scripts/build_deepseek_carrier.py",
    "!.super-coder/scripts/cli_entry.py",
    "!.super-coder/scripts/deepseek_carrier_worker.py",
    "!.super-coder/scripts/deepseek_skill_resume_canary.py",
    "!.super-coder/assets",
    f"!{DEEPSEEK_ASSETS}",
    f"!{DEEPSEEK_ASSETS}/**",
]


def _rules() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _rule_matches(path: str, rule: str) -> bool:
    pattern = rule.lstrip("!").strip("/")
    return fnmatch.fnmatchcase(path, pattern)


def _is_copyable(path: str, rules: list[str]) -> bool:
    candidate = PurePosixPath(path)
    for current in (*candidate.parents[-2::-1], candidate):
        relative = current.as_posix()
        included = True
        for rule in rules:
            if _rule_matches(relative, rule):
                included = rule.startswith("!")
        if not included:
            return False
    return True


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return {
        path.decode("utf-8")
        for path in result.stdout.split(b"\0")
        if path
    }


def _manifest_script_paths(value: object) -> set[str]:
    if isinstance(value, (dict, list)):
        items = value.values() if isinstance(value, dict) else value
        paths: set[str] = set()
        for item in items:
            paths.update(_manifest_script_paths(item))
        return paths
    if isinstance(value, str) and value.startswith("scripts/") and value.endswith(".py"):
        return {f".super-coder/{value}"}
    return set()


def _materialize_bootstrap(tmp_path: Path) -> Path:
    bootstrap = tmp_path / "deepseek-bootstrap"
    scripts = bootstrap / "scripts"
    scripts.mkdir(parents=True)
    for source in sorted(REQUIRED_SCRIPTS):
        shutil.copy2(ROOT / source, scripts / Path(source).name)
    shutil.copytree(ROOT / DEEPSEEK_ASSETS, bootstrap / "assets" / "deepseek")
    return scripts


def test_sandbox_context_is_exact_default_deny_allowlist() -> None:
    rules = _rules()
    assert rules == EXPECTED_RULES

    tracked = _tracked_files()
    required_assets = {
        path
        for path in tracked
        if path.startswith(f"{DEEPSEEK_ASSETS}/")
    }
    assert required_assets
    expected_copyable = REQUIRED_SCRIPTS | required_assets
    assert expected_copyable <= tracked

    copyable = {path for path in tracked if _is_copyable(path, rules)}
    assert copyable == expected_copyable


def test_sandbox_context_rejects_mutable_and_secret_bearing_neighbors() -> None:
    rules = _rules()
    prohibited = {
        ".git/config",
        ".sc-state/content.sql",
        ".sc-worktrees/dev1/private-state",
        ".super-coder/assets/skills/sprint_dev/SKILL.md",
        ".super-coder/archives/session.json",
        ".super-coder/scripts/conversation_launch.py",
        ".super-coder/shell_db.db",
        "credentials.json",
        "shared/sprints/sprint-19/receipt.json",
    }

    assert {path for path in prohibited if _is_copyable(path, rules)} == set()


def test_isolated_bootstrap_resolves_cli_entry(tmp_path: Path) -> None:
    scripts = _materialize_bootstrap(tmp_path)

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    probe = """
import ast
import json
import sys
from pathlib import Path

scripts = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(scripts))
import cli_entry
import deepseek_runtime

tree = ast.parse(Path(deepseek_runtime.__file__).read_text())
imports_cli_entry = any(
    isinstance(node, ast.ImportFrom)
    and node.module == "cli_entry"
    and [(alias.name, alias.asname) for alias in node.names] == [("run_cli", None)]
    for node in ast.walk(tree)
)
if not imports_cli_entry:
    raise SystemExit("deepseek_runtime production CLI edge is missing")
print(json.dumps({
    "cli_entry": str(Path(cli_entry.__file__).resolve().relative_to(scripts)),
    "deepseek_runtime": str(
        Path(deepseek_runtime.__file__).resolve().relative_to(scripts)
    ),
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", probe, str(scripts)],
        cwd=tmp_path,
        env={**environment, "PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "cli_entry": "cli_entry.py",
        "deepseek_runtime": "deepseek_runtime.py",
    }


def test_isolated_bootstrap_validates_complete_manifest_closure(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / DEEPSEEK_ASSETS / "runtime.json").read_text())
    assert _manifest_script_paths(manifest) == REQUIRED_SCRIPTS - BOOTSTRAP_ENTRYPOINTS
    scripts = _materialize_bootstrap(tmp_path)

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    probe = """
import json
import sys
from pathlib import Path

scripts = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(scripts))
import deepseek_runtime

manifest = deepseek_runtime.load_runtime_manifest()
print(json.dumps({
    "build": manifest["build"]["path"],
    "canary": manifest["build"]["canary_path"],
    "runtime_module": str(
        Path(deepseek_runtime.__file__).resolve().relative_to(scripts)
    ),
    "worker": manifest["carrier"]["worker_path"],
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", probe, str(scripts)],
        cwd=tmp_path,
        env={**environment, "PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "build": "scripts/build_deepseek_carrier.py",
        "canary": "scripts/deepseek_skill_resume_canary.py",
        "runtime_module": "deepseek_runtime.py",
        "worker": "scripts/deepseek_carrier_worker.py",
    }
