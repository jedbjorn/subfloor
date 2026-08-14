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
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from artifact_policy import devkit_log_root

DECLARATION_PATH = Path(".subfloor/dev-kit.json")
HOOK_NAMES = frozenset(("deps", "test", "lint", "typecheck"))
MOUNT_NAME = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,47}\Z")
APT_NAME = re.compile(r"\A[a-z0-9][a-z0-9+.-]{1,127}\Z")
APT_VERSION = re.compile(r"\A(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]{0,126}\Z")
APT_PACKAGE_LIMIT = 64
APT_ENTRY_BYTE_LIMIT = 256
APT_TOTAL_BYTE_LIMIT = 8192
COMPACT_HOOKS = frozenset(("test", "lint", "typecheck"))
OUTPUT_MODES = frozenset(("compact", "full"))
LOG_RETENTION = 20
SUCCESS_LINE_LIMIT = 80
SUCCESS_BYTE_LIMIT = 16 * 1024
FAILURE_LINE_LIMIT = 240
FAILURE_BYTE_LIMIT = 48 * 1024
ANSI_ESCAPE = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)
DIAGNOSTIC = re.compile(
    r"\b(?:error|errors|fail|failed|failure|fatal|panic|traceback|warning|warnings)\b",
    re.IGNORECASE,
)
DISPLAY_LINE_BYTE_LIMIT = 1024
DISPLAY_SCAN_BYTE_LIMIT = 4096
INTERRUPT_TERMINATE_TIMEOUT = 1.0


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
class AptPackage:
    name: str
    version: str | None

    @property
    def atom(self) -> str:
        return self.name if self.version is None else f"{self.name}={self.version}"


@dataclass(frozen=True)
class SandboxPackages:
    apt: tuple[AptPackage, ...]

    @property
    def canonical_atoms(self) -> tuple[str, ...]:
        return tuple(package.atom for package in self.apt)


@dataclass(frozen=True)
class Sandbox:
    dockerfile_declared: str | None
    dockerfile: Path | None
    context_declared: str | None
    context: Path | None
    mounts: tuple[SandboxMount, ...]
    packages: SandboxPackages | None
    package_error: str | None

    @property
    def has_extension(self) -> bool:
        return self.dockerfile is not None


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


def _sandbox_packages(value: Any) -> SandboxPackages:
    field = "$.sandbox.packages"
    item = _object(value, field)
    _keys(item, field, allowed=("apt",), required=("apt",))
    raw = item["apt"]
    if type(raw) is not list or not raw:
        raise _error(f"{field}.apt", "must be an array with 1-64 entries")
    if len(raw) > APT_PACKAGE_LIMIT:
        raise _error(f"{field}.apt", "must contain at most 64 entries")

    parsed: list[AptPackage] = []
    names: set[str] = set()
    total_bytes = 0
    for index, raw_atom in enumerate(raw):
        atom_field = f"{field}.apt[{index}]"
        atom = _string(raw_atom, atom_field)
        try:
            encoded = atom.encode("utf-8")
        except UnicodeError as exc:
            raise _error(atom_field, "must be valid UTF-8") from exc
        if len(encoded) < 2 or len(encoded) > APT_ENTRY_BYTE_LIMIT:
            raise _error(atom_field, "must contain 2-256 UTF-8 bytes")
        total_bytes += len(encoded)
        if total_bytes > APT_TOTAL_BYTE_LIMIT:
            raise _error(f"{field}.apt", "must contain at most 8192 UTF-8 bytes")
        if atom.count("=") > 1:
            raise _error(atom_field, "must contain at most one '='")
        name, separator, version = atom.partition("=")
        if not APT_NAME.fullmatch(name):
            raise _error(
                atom_field,
                "name must match [a-z0-9][a-z0-9+.-]{1,127}",
            )
        if separator and not APT_VERSION.fullmatch(version):
            raise _error(
                atom_field,
                "version must match ([0-9]+:)?[0-9][A-Za-z0-9.+~-]{0,126}",
            )
        if name in names:
            raise _error(atom_field, f"duplicate package name {name!r}")
        names.add(name)
        parsed.append(AptPackage(name, version if separator else None))
    parsed.sort(key=lambda package: package.name.encode("ascii"))
    return SandboxPackages(tuple(parsed))


def _sandbox(checkout: Path, value: Any) -> Sandbox:
    field = "$.sandbox"
    item = _object(value, field)
    _keys(
        item,
        field,
        allowed=("dockerfile", "context", "mounts", "packages"),
    )
    if "dockerfile" not in item and "packages" not in item:
        raise _error(field, "must contain 'dockerfile' or 'packages'")
    if "context" in item and "dockerfile" not in item:
        raise _error(f"{field}.context", "requires sandbox.dockerfile")

    dockerfile_declared: str | None = None
    dockerfile: Path | None = None
    context_declared: str | None = None
    context: Path | None = None
    if "dockerfile" in item:
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

    packages: SandboxPackages | None = None
    package_error: str | None = None
    if "packages" in item:
        try:
            packages = _sandbox_packages(item["packages"])
        except DevkitConfigError as exc:
            # Package-local invalidity is a capability advisory under Decision
            # #199.  Preserve the validated non-package envelope so the engine
            # baseline can still be built and selected without inference.
            package_error = str(exc)
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
        packages,
        package_error,
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


def _main_checkout(checkout: Path) -> Path:
    result = subprocess.run(
        ("git", "-C", str(checkout), "rev-parse", "--git-common-dir"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise _error("$checkout", "cannot resolve Git common directory")
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = checkout / common
    return common.resolve().parent


def _shell_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _sanitize_display(text: str) -> str:
    clean = ANSI_ESCAPE.sub(
        b"", text.encode("utf-8", errors="backslashreplace")
    ).decode("utf-8", errors="replace")
    visible: list[str] = []
    named = {"\n": r"\n", "\r": r"\r", "\b": r"\b", "\t": r"\t"}
    for character in clean:
        category = unicodedata.category(character)
        if category[0] != "C" and category not in {"Zl", "Zp"}:
            visible.append(character)
            continue
        if character in named:
            visible.append(named[character])
            continue
        codepoint = ord(character)
        if codepoint <= 0xFF:
            visible.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            visible.append(f"\\u{codepoint:04x}")
        else:
            visible.append(f"\\U{codepoint:08x}")
    return "".join(visible)


def _truncate_display(text: str) -> str:
    text = _sanitize_display(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= DISPLAY_LINE_BYTE_LIMIT:
        return text
    clipped = encoded[: DISPLAY_LINE_BYTE_LIMIT - 24]
    while clipped:
        try:
            prefix = clipped.decode("utf-8")
            break
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    else:
        prefix = ""
    return prefix + " … [line truncated]"


def _display_line(raw: bytes, *, source_truncated: bool = False) -> str:
    clean = ANSI_ESCAPE.sub(b"", raw.rstrip(b"\r\n"))
    text = clean.decode("utf-8", errors="replace")
    if source_truncated:
        text += " … [line truncated]"
    return _truncate_display(text)


@dataclass(frozen=True)
class LogScan:
    byte_count: int
    line_count: int
    head: tuple[tuple[int, str], ...]
    diagnostics: tuple[tuple[int, str], ...]
    tail: tuple[tuple[int, str], ...]


def _scan_log(path: Path, *, failed: bool) -> LogScan:
    head_limit = 40 if failed else 0
    diagnostic_limit = 80 if failed else 24
    tail_limit = 80 if failed else 40
    head: list[tuple[int, str]] = []
    diagnostics: list[tuple[int, str]] = []
    tail: deque[tuple[int, str]] = deque(maxlen=tail_limit)
    line_count = 0

    def record(raw: bytes, source_truncated: bool) -> None:
        nonlocal line_count
        line_count += 1
        displayed = _display_line(raw, source_truncated=source_truncated)
        item = (line_count, displayed)
        if len(head) < head_limit:
            head.append(item)
        if len(diagnostics) < diagnostic_limit and DIAGNOSTIC.search(displayed):
            diagnostics.append(item)
        tail.append(item)

    pending = bytearray()
    source_truncated = False
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            pieces = chunk.split(b"\n")
            for index, piece in enumerate(pieces):
                room = max(0, DISPLAY_SCAN_BYTE_LIMIT - len(pending))
                pending.extend(piece[:room])
                source_truncated = source_truncated or len(piece) > room
                if index < len(pieces) - 1:
                    record(bytes(pending), source_truncated)
                    pending.clear()
                    source_truncated = False
    if pending or source_truncated:
        record(bytes(pending), source_truncated)
    return LogScan(
        byte_count=path.stat().st_size,
        line_count=line_count,
        head=tuple(head),
        diagnostics=tuple(diagnostics),
        tail=tuple(tail),
    )


def _excerpt_lines(scan: LogScan, *, failed: bool) -> list[str]:
    sections = (
        (("head", scan.head), ("diagnostic digest", scan.diagnostics), ("tail", scan.tail))
        if failed
        else (("diagnostic digest", scan.diagnostics), ("tail", scan.tail))
    )
    lines: list[str] = []
    emitted: set[int] = set()
    for label, items in sections:
        unique = [(number, text) for number, text in items if number not in emitted]
        if not unique:
            continue
        lines.append(f"dev-kit {label}:")
        for number, value in unique:
            lines.append(f"  {number}: {value}")
            emitted.add(number)
    omitted = scan.line_count - len(emitted)
    if omitted > 0:
        lines.append(f"dev-kit excerpt omitted: {omitted} lines")
    elif scan.line_count == 0:
        lines.append("dev-kit excerpt: (empty output)")
    return lines


def _bounded_envelope(
    prefix: Sequence[str], excerpt: Sequence[str], recovery: str, *, failed: bool
) -> str:
    line_limit = FAILURE_LINE_LIMIT if failed else SUCCESS_LINE_LIMIT
    byte_limit = FAILURE_BYTE_LIMIT if failed else SUCCESS_BYTE_LIMIT
    prefix = [_truncate_display(line) for line in prefix]
    excerpt = [_truncate_display(line) for line in excerpt]
    recovery = _truncate_display(recovery)
    fixed = [*prefix, recovery]
    kept: list[str] = []
    for line in excerpt:
        candidate = [*prefix, *kept, line, recovery]
        rendered = "\n".join(candidate) + "\n"
        if len(candidate) > line_limit or len(rendered.encode("utf-8")) > byte_limit:
            break
        kept.append(line)
    if len(kept) < len(excerpt):
        marker = "dev-kit excerpt omitted: display bound reached"
        candidate = [*prefix, *kept, marker, recovery]
        rendered = "\n".join(candidate) + "\n"
        if len(candidate) <= line_limit and len(rendered.encode("utf-8")) <= byte_limit:
            kept.append(marker)
    result = "\n".join([*prefix, *kept, recovery]) + "\n"
    if len(fixed) > line_limit or len(result.encode("utf-8")) > byte_limit:
        raise RuntimeError("dev-kit envelope metadata exceeds its display bound")
    return result


def _prune_logs(directory: Path) -> None:
    dated = []
    for path in directory.glob("*.log"):
        try:
            dated.append((path.stat().st_mtime_ns, path.name, path))
        except FileNotFoundError:
            continue
    finalized = [item[2] for item in sorted(dated, reverse=True)]
    for old in finalized[LOG_RETENTION:]:
        old.unlink(missing_ok=True)


def _run_full(command: Sequence[str], hook: Hook, child_environment: Mapping[str, str]) -> int:
    completed = subprocess.run(
        command,
        cwd=hook.cwd,
        env=child_environment,
        check=False,
    )
    return _shell_status(completed.returncode)


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=INTERRUPT_TERMINATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_compact(
    checkout: Path,
    hook: Hook,
    command: Sequence[str],
    arguments: Sequence[str],
    child_environment: Mapping[str, str],
    seat: str,
) -> int:
    main_checkout = _main_checkout(checkout)
    directory = devkit_log_root(main_checkout) / hook.name
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{stamp}-{os.getpid()}-{uuid.uuid4().hex}.running"
    running = directory / name
    started = time.monotonic()
    with running.open("xb") as log:
        try:
            process = subprocess.Popen(
                command,
                cwd=hook.cwd,
                env=child_environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        except OSError:
            running.unlink(missing_ok=True)
            raise
        try:
            returncode = process.wait()
        except BaseException:
            _terminate_and_reap(process)
            log.flush()
            os.fsync(log.fileno())
            relative = running.relative_to(main_checkout)
            print(f"dev-kit log interrupted: {relative}", file=sys.stderr)
            raise
        log.flush()
        os.fsync(log.fileno())

    finalized = running.with_suffix(".log")
    running.replace(finalized)
    duration = time.monotonic() - started
    status = _shell_status(returncode)
    failed = status != 0
    scan = _scan_log(finalized, failed=failed)
    relative = finalized.relative_to(main_checkout)
    recovery_args = shlex.join(("./sc", hook.name, *arguments))
    prefix = [
        f"dev-kit checkout: {checkout}",
        f"dev-kit seat: {seat}",
        f"dev-kit hook: {hook.name}",
        f"dev-kit command: {shlex.join(command)}",
        f"dev-kit cwd: {hook.cwd}",
        f"dev-kit exit status: {status}",
        f"dev-kit duration: {duration:.3f}s",
        f"dev-kit output: {scan.byte_count} bytes, {scan.line_count} lines",
        f"dev-kit log: {relative}",
        (
            f"dev-kit hook state: failed — {hook.name!r} exited {status}"
            if failed
            else f"dev-kit hook state: ready — {hook.name!r}"
        ),
    ]
    recovery = f"dev-kit full output: SC_DEVKIT_OUTPUT=full {recovery_args}"
    sys.stderr.write(
        _bounded_envelope(prefix, _excerpt_lines(scan, failed=failed), recovery, failed=failed)
    )
    _prune_logs(directory)
    return status


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
    output_mode = child_environment.get("SC_DEVKIT_OUTPUT", "compact")
    if hook_name in COMPACT_HOOKS and output_mode not in OUTPUT_MODES:
        print(
            "dev-kit hook state: invalid — SC_DEVKIT_OUTPUT must be "
            "'compact' or 'full'",
            file=sys.stderr,
        )
        return 64
    requested = (*hook.argv, *arguments)
    if hook_name not in COMPACT_HOOKS or output_mode == "full":
        print(f"dev-kit checkout: {checkout}", file=sys.stderr)
        print(f"dev-kit seat: {seat}", file=sys.stderr)
        print(f"dev-kit cwd: {hook.cwd}", file=sys.stderr)
        print(f"dev-kit argv: {shlex.join(requested)}", file=sys.stderr)
    try:
        executable = _resolve_executable(hook, child_environment)
    except OSError as exc:
        if hook_name in COMPACT_HOOKS and output_mode == "compact":
            print(f"dev-kit checkout: {checkout}", file=sys.stderr)
            print(f"dev-kit seat: {seat}", file=sys.stderr)
            print(f"dev-kit cwd: {hook.cwd}", file=sys.stderr)
            print(f"dev-kit argv: {shlex.join(requested)}", file=sys.stderr)
        print(f"dev-kit hook state: failed — start failed: {exc}", file=sys.stderr)
        return 126

    command = (str(executable), *hook.argv[1:], *arguments)
    compact = hook_name in COMPACT_HOOKS and output_mode == "compact"
    try:
        if compact:
            status = _run_compact(
                checkout, hook, command, arguments, child_environment, seat
            )
        else:
            print(f"dev-kit executable: {executable}", file=sys.stderr)
            status = _run_full(command, hook, child_environment)
    except OSError as exc:
        print(
            f"dev-kit hook state: failed — start failed for {executable}: {exc}",
            file=sys.stderr,
        )
        return 126
    if not compact:
        if status == 0:
            print(f"dev-kit hook state: ready — {hook_name!r}", file=sys.stderr)
        else:
            print(
                f"dev-kit hook state: failed — {hook_name!r} exited "
                f"{status}",
                file=sys.stderr,
            )
    return status


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
