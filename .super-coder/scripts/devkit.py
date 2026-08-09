"""Validate a fork-owned v1 dev-kit declaration.

The declaration is policy owned by the invoking checkout.  This module only
parses that policy and proves that every declared repository path stays inside
the checkout.  Execution lives in the runner layered on top of these immutable
models.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

DECLARATION_PATH = Path(".subfloor/dev-kit.json")
HOOK_NAMES = frozenset(("deps", "test", "lint", "typecheck"))
MOUNT_NAME = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,47}\Z")


class DevkitConfigError(RuntimeError):
    """The fork declaration exists but is not a valid v1 declaration."""


@dataclass(frozen=True)
class Hook:
    name: str
    argv: tuple[str, ...]
    cwd_declared: str
    cwd: Path
    executable_kind: str
    executable: str
    resolved_executable: Path | None


@dataclass(frozen=True)
class Provision:
    hook: str
    inputs_declared: tuple[str, ...]
    inputs: tuple[Path, ...]


@dataclass(frozen=True)
class SandboxMount:
    name: str
    target_declared: str
    target: Path


@dataclass(frozen=True)
class Sandbox:
    dockerfile_declared: str
    dockerfile: Path
    context_declared: str
    context: Path
    mounts: tuple[SandboxMount, ...]


@dataclass(frozen=True)
class Declaration:
    path: Path
    checkout: Path
    hooks: Mapping[str, Hook]
    provision: Provision | None
    sandbox: Sandbox | None
    canonical_json: str


def _error(field: str, message: str) -> DevkitConfigError:
    return DevkitConfigError(f"{field}: {message}")


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise _error(field, "must be an object")
    return value


def _keys(
    value: Mapping[str, Any],
    field: str,
    *,
    allowed: Sequence[str],
    required: Sequence[str] = (),
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _error(field, f"unknown key {unknown[0]!r}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise _error(field, f"missing required key {missing[0]!r}")


def _string(value: Any, field: str) -> str:
    if type(value) is not str or value == "":
        raise _error(field, "must be a non-empty string")
    if "\x00" in value:
        raise _error(field, "must not contain NUL")
    return value


def _string_array(value: Any, field: str) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise _error(field, "must be a non-empty array")
    return tuple(_string(item, f"{field}[{index}]") for index, item in enumerate(value))


def _is_absolute(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _contained(checkout: Path, candidate: Path, field: str) -> Path:
    resolved = candidate.resolve(strict=False)
    try:
        common = Path(os.path.commonpath((str(checkout), str(resolved))))
    except ValueError as exc:
        raise _error(field, "must stay inside the invoking checkout") from exc
    if common != checkout:
        raise _error(field, "must stay inside the invoking checkout")
    return resolved


def _repo_path(
    checkout: Path,
    declared: Any,
    field: str,
    *,
    base: Path | None = None,
    kind: str | None = None,
) -> tuple[str, Path]:
    value = _string(declared, field)
    if _is_absolute(value):
        raise _error(field, "must be relative to the invoking checkout")
    resolved = _contained(checkout, (base or checkout) / value, field)
    if kind == "directory" and not resolved.is_dir():
        raise _error(field, "must resolve to an existing directory")
    if kind == "file" and not resolved.is_file():
        raise _error(field, "must resolve to an existing file")
    return value, resolved


def _hook(checkout: Path, name: str, value: Any) -> Hook:
    field = f"$.hooks.{name}"
    item = _object(value, field)
    _keys(item, field, allowed=("argv", "cwd"), required=("argv",))
    argv = _string_array(item["argv"], f"{field}.argv")
    cwd_declared, cwd = _repo_path(
        checkout, item.get("cwd", "."), f"{field}.cwd", kind="directory"
    )
    executable = argv[0]
    if "/" not in executable:
        if _is_absolute(executable):
            raise _error(f"{field}.argv[0]", "absolute executable paths are forbidden")
        return Hook(name, argv, cwd_declared, cwd, "path", executable, None)
    if _is_absolute(executable):
        raise _error(f"{field}.argv[0]", "absolute executable paths are forbidden")
    _, resolved = _repo_path(
        checkout, executable, f"{field}.argv[0]", base=cwd
    )
    return Hook(name, argv, cwd_declared, cwd, "relative", executable, resolved)


def _provision(
    checkout: Path, value: Any, hooks: Mapping[str, Hook]
) -> Provision:
    field = "$.provision"
    item = _object(value, field)
    _keys(item, field, allowed=("hook", "inputs"), required=("hook",))
    hook = _string(item["hook"], f"{field}.hook")
    if hook not in hooks:
        raise _error(f"{field}.hook", f"must name a declared hook, got {hook!r}")
    inputs_value = item.get("inputs", [])
    if type(inputs_value) is not list:
        raise _error(f"{field}.inputs", "must be an array")
    declared = []
    resolved = []
    for index, input_value in enumerate(inputs_value):
        item_field = f"{field}.inputs[{index}]"
        input_declared, input_path = _repo_path(
            checkout, input_value, item_field, kind="file"
        )
        declared.append(input_declared)
        resolved.append(input_path)
    return Provision(hook, tuple(declared), tuple(resolved))


def _sandbox(checkout: Path, value: Any) -> Sandbox:
    field = "$.sandbox"
    item = _object(value, field)
    _keys(
        item,
        field,
        allowed=("dockerfile", "context", "mounts"),
        required=("dockerfile",),
    )
    dockerfile_declared, dockerfile = _repo_path(
        checkout, item["dockerfile"], f"{field}.dockerfile", kind="file"
    )
    default_context = str(Path(dockerfile_declared).parent)
    context_declared, context = _repo_path(
        checkout,
        item.get("context", default_context),
        f"{field}.context",
        kind="directory",
    )
    mounts_value = item.get("mounts", [])
    if type(mounts_value) is not list:
        raise _error(f"{field}.mounts", "must be an array")
    mounts = []
    names = set()
    for index, mount_value in enumerate(mounts_value):
        mount_field = f"{field}.mounts[{index}]"
        mount = _object(mount_value, mount_field)
        _keys(mount, mount_field, allowed=("name", "target"), required=("name", "target"))
        name = _string(mount["name"], f"{mount_field}.name")
        if not MOUNT_NAME.fullmatch(name):
            raise _error(
                f"{mount_field}.name",
                "must be 1-48 lowercase letters, digits, underscores, or hyphens",
            )
        if name in names:
            raise _error(f"{mount_field}.name", f"duplicate mount name {name!r}")
        names.add(name)
        target_declared, target = _repo_path(
            checkout, mount["target"], f"{mount_field}.target"
        )
        if target.exists() and not target.is_dir():
            raise _error(f"{mount_field}.target", "must resolve to a directory")
        protected = (
            checkout / ".git",
            checkout / ".super-coder",
            checkout / ".sc-state",
            checkout / DECLARATION_PATH,
        )
        if any(
            target == path
            or target in path.parents
            or path in target.parents
            for path in protected
        ):
            raise _error(
                f"{mount_field}.target",
                "must not overlap Git metadata, engine state, or the declaration",
            )
        for prior in mounts:
            if (
                target == prior.target
                or target in prior.target.parents
                or prior.target in target.parents
            ):
                raise _error(
                    f"{mount_field}.target",
                    f"must not overlap mount target {prior.target_declared!r}",
                )
        mounts.append(SandboxMount(name, target_declared, target))
    return Sandbox(
        dockerfile_declared,
        dockerfile,
        context_declared,
        context,
        tuple(mounts),
    )


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> Mapping[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DevkitConfigError(f"$: duplicate key {key!r}")
        result[key] = value
    return result


def load_declaration(checkout: Path) -> Declaration | None:
    """Load and validate ``checkout/.subfloor/dev-kit.json``.

    ``None`` is the compatible absent state.  Any existing declaration that
    cannot be read or validated raises :class:`DevkitConfigError`.
    """
    try:
        root = checkout.resolve(strict=True)
    except OSError as exc:
        raise _error("$checkout", f"cannot resolve invoking checkout: {exc}") from exc
    if not root.is_dir():
        raise _error("$checkout", "must resolve to an existing directory")
    path = root / DECLARATION_PATH
    if not path.exists():
        return None
    _contained(root, path, "$")
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except DevkitConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise _error("$", f"malformed JSON at line {exc.lineno}, column {exc.colno}") from exc
    except (OSError, UnicodeError) as exc:
        raise _error("$", f"cannot read {DECLARATION_PATH}: {exc}") from exc

    document = _object(value, "$")
    _keys(
        document,
        "$",
        allowed=("version", "hooks", "provision", "sandbox"),
        required=("version",),
    )
    if type(document["version"]) is not int or document["version"] != 1:
        raise _error("$.version", "must be integer 1")

    hooks_value = _object(document.get("hooks", {}), "$.hooks")
    unknown_hooks = sorted(set(hooks_value) - HOOK_NAMES)
    if unknown_hooks:
        raise _error("$.hooks", f"unknown hook {unknown_hooks[0]!r}")
    hooks = {name: _hook(root, name, item) for name, item in hooks_value.items()}
    provision = (
        _provision(root, document["provision"], hooks)
        if "provision" in document
        else None
    )
    sandbox = _sandbox(root, document["sandbox"]) if "sandbox" in document else None
    canonical_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return Declaration(path, root, hooks, provision, sandbox, canonical_json)


def invoking_checkout(invocation_root: Path) -> Path:
    """Resolve the exact Git checkout containing the invoked ``sc``."""
    try:
        result = subprocess.run(
            ("git", "-C", str(invocation_root), "rev-parse", "--show-toplevel"),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise _error("$checkout", f"cannot run git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "git rev-parse failed"
        raise _error("$checkout", detail)
    value = result.stdout.rstrip("\n")
    if not value or "\n" in value:
        raise _error("$checkout", "git returned an invalid toplevel path")
    try:
        checkout = Path(value).resolve(strict=True)
    except OSError as exc:
        raise _error("$checkout", f"cannot resolve Git toplevel: {exc}") from exc
    if not checkout.is_dir():
        raise _error("$checkout", "Git toplevel must be a directory")
    return checkout


def _resolve_executable(hook: Hook, environment: Mapping[str, str]) -> Path:
    field = f"$.hooks.{hook.name}.argv[0]"
    if hook.executable_kind == "relative":
        executable = hook.resolved_executable
    else:
        found = shutil.which(hook.executable, path=environment.get("PATH"))
        executable = Path(found).resolve(strict=False) if found else None
    if executable is None or not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError(f"{field}: executable {hook.executable!r} is unavailable")
    return executable


def run_hook(
    invocation_root: Path,
    hook_name: str,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run one validated hook directly and return its exact child status."""
    checkout = invoking_checkout(invocation_root)
    declaration = load_declaration(checkout)
    if declaration is None:
        print(
            f"dev-kit hook state: absent — {DECLARATION_PATH} absent; "
            f"hook {hook_name!r} not configured",
            file=sys.stderr,
        )
        return 78
    hook = declaration.hooks.get(hook_name)
    if hook is None:
        print(
            f"dev-kit hook state: absent — hook {hook_name!r} not configured",
            file=sys.stderr,
        )
        return 78

    child_environment = dict(environment if environment is not None else os.environ)
    seat = "docker" if child_environment.get("SC_SANDBOX") else "host"
    child_environment.update(
        {
            "SC_DEVKIT_ROOT": str(checkout),
            "SC_DEVKIT_SEAT": seat,
            "SC_DEVKIT_HOOK": hook_name,
        }
    )
    requested = (*hook.argv, *arguments)
    print(f"dev-kit checkout: {checkout}", file=sys.stderr)
    print(f"dev-kit seat: {seat}", file=sys.stderr)
    print(f"dev-kit cwd: {hook.cwd}", file=sys.stderr)
    print(f"dev-kit argv: {shlex.join(requested)}", file=sys.stderr)
    try:
        executable = _resolve_executable(hook, child_environment)
    except OSError as exc:
        print(f"dev-kit hook state: failed — start failed: {exc}", file=sys.stderr)
        return 126

    command = (str(executable), *hook.argv[1:], *arguments)
    print(f"dev-kit executable: {executable}", file=sys.stderr)
    try:
        completed = subprocess.run(
            command,
            cwd=hook.cwd,
            env=child_environment,
            check=False,
        )
    except OSError as exc:
        print(
            f"dev-kit hook state: failed — start failed for {executable}: {exc}",
            file=sys.stderr,
        )
        return 126
    if completed.returncode < 0:
        return 128 - completed.returncode
    if completed.returncode == 0:
        print(f"dev-kit hook state: ready — {hook_name!r}", file=sys.stderr)
    else:
        print(
            f"dev-kit hook state: failed — {hook_name!r} exited "
            f"{completed.returncode}",
            file=sys.stderr,
        )
    return completed.returncode


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3 or argv[0] != "run" or argv[2] not in HOOK_NAMES:
        raise DevkitConfigError(
            "usage: devkit.py run <invoking-checkout> <deps|test|lint|typecheck> [args...]"
        )
    return run_hook(Path(argv[1]), argv[2], argv[3:])


def cli(argv: Sequence[str]) -> int:
    try:
        return main(argv)
    except DevkitConfigError as exc:
        print(f"dev-kit hook state: invalid — {exc}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(cli, sys.argv[1:]))
