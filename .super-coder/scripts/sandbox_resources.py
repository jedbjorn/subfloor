"""Engine-owned resource policy for the long-running Docker sandbox."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import instance_state

ENGINE = Path(__file__).resolve().parents[1]
INSTANCE = ENGINE / "instance.json"

BLOCK = "sandbox_resources"
MEMORY_KEY = "memory"
DEFAULT_LIMIT = 12 * 1024**3
MIN_LIMIT = 512 * 1024**2
MIB = 1024**2
SAFETY_NUMERATOR = 4
SAFETY_DENOMINATOR = 5
SIZE = re.compile(r"\A([1-9][0-9]*)([bkmgt]?)\Z", re.IGNORECASE)
UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}

Runner = Callable[..., subprocess.CompletedProcess[str]]
Emitter = Callable[[str], None]
LOGGER = logging.getLogger(__name__)
CGROUP_MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events.local")
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")


class SandboxResourceError(ValueError):
    """The sandbox cannot be launched under a trustworthy resource policy."""


@dataclass(frozen=True)
class MemoryPolicy:
    bytes: int
    daemon_bytes: int
    configured: str | None

    @property
    def source(self) -> str:
        return "configured" if self.configured is not None else "default"


def _event_value(path: Path, name: str) -> int:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SandboxResourceError(f"cannot read {path}: {exc}") from exc
    for row in rows:
        key, separator, raw = row.partition(" ")
        if key != name or not separator:
            continue
        try:
            return int(raw)
        except ValueError as exc:
            raise SandboxResourceError(
                f"{path} has invalid {name} value {raw!r}"
            ) from exc
    raise SandboxResourceError(f"{path} does not report {name}")


def _effective_cgroup_limit(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SandboxResourceError(f"cannot read {path}: {exc}") from exc
    if raw == "max":
        return "unlimited"
    try:
        value = int(raw)
    except ValueError as exc:
        raise SandboxResourceError(f"{path} has invalid value {raw!r}") from exc
    if value <= 0:
        raise SandboxResourceError(f"{path} has invalid value {value}")
    return human_size(value)


class OomKillWatcher:
    """Report cgroup-local OOM kills without changing kernel kill behavior."""

    def __init__(
        self,
        *,
        events_path: Path = CGROUP_MEMORY_EVENTS,
        memory_max_path: Path = CGROUP_MEMORY_MAX,
        interval: float = 1.0,
        emit: Emitter = LOGGER.warning,
    ) -> None:
        self.events_path = Path(events_path)
        self.memory_max_path = Path(memory_max_path)
        self.interval = interval
        self.emit = emit
        self._last: int | None = None
        self._unavailable_reported = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll(self) -> None:
        try:
            count = _event_value(self.events_path, "oom_kill")
            ceiling = _effective_cgroup_limit(self.memory_max_path)
        except SandboxResourceError as exc:
            if not self._unavailable_reported:
                self.emit(f"sandbox OOM diagnostic unavailable: {exc}")
                self._unavailable_reported = True
            return
        self._unavailable_reported = False
        if self._last is None:
            self._last = count
            return
        if count > self._last:
            killed = count - self._last
            noun = "process" if killed == 1 else "processes"
            self.emit(
                f"sandbox memory ceiling reached: kernel killed {killed} {noun} "
                f"at the {ceiling} hard limit (cgroup oom_kill={count})"
            )
        self._last = max(self._last, count)

    def _run(self) -> None:
        self.poll()
        while not self._stop.wait(self.interval):
            self.poll()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sandbox-oom-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 2))

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def parse_size(value: object) -> int:
    if not isinstance(value, str):
        raise SandboxResourceError("sandbox_resources.memory must be a string")
    matched = SIZE.fullmatch(value.strip())
    if matched is None:
        raise SandboxResourceError(
            "sandbox_resources.memory must be a positive integer followed by "
            "b, k, m, g, or t"
        )
    size = int(matched.group(1)) * UNITS[matched.group(2).lower()]
    if size < MIN_LIMIT:
        raise SandboxResourceError(
            f"sandbox_resources.memory must be at least {human_size(MIN_LIMIT)}"
        )
    return size


def human_size(value: int) -> str:
    for unit, divisor in (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", MIB)):
        if value >= divisor and value % divisor == 0:
            return f"{value // divisor} {unit}"
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GiB"
    return f"{value / MIB:.0f} MiB"


def _configured_memory(config_path: Path) -> tuple[str | None, int | None]:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise SandboxResourceError(f"cannot read {config_path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SandboxResourceError(f"{config_path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SandboxResourceError(f"{config_path} must contain a JSON object")
    block = payload.get(BLOCK)
    if block is None:
        return None, None
    if not isinstance(block, dict):
        raise SandboxResourceError(f"{BLOCK} must be a JSON object")
    unknown = sorted(set(block) - {MEMORY_KEY})
    if unknown:
        raise SandboxResourceError(f"{BLOCK} has unknown key {unknown[0]!r}")
    if MEMORY_KEY not in block:
        raise SandboxResourceError(f"{BLOCK} must contain {MEMORY_KEY!r}")
    configured = block[MEMORY_KEY]
    return configured if isinstance(configured, str) else None, parse_size(configured)


def docker_memory(*, runner: Runner = subprocess.run) -> int:
    command = ("docker", "info", "--format", "{{.MemTotal}}")
    try:
        completed = runner(
            command,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise SandboxResourceError(f"cannot inspect Docker memory: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise SandboxResourceError(f"cannot inspect Docker memory: {detail}")
    try:
        total = int(completed.stdout.strip())
    except ValueError as exc:
        raise SandboxResourceError(
            f"Docker returned invalid memory capacity {completed.stdout.strip()!r}"
        ) from exc
    if total <= 0:
        raise SandboxResourceError(f"Docker returned invalid memory capacity {total}")
    return total


def resolve_memory(
    config_path: Path = INSTANCE,
    *,
    runner: Runner = subprocess.run,
) -> MemoryPolicy:
    configured, requested = _configured_memory(Path(config_path))
    daemon_bytes = docker_memory(runner=runner)
    return _resolve_requested(configured, requested, daemon_bytes)


def _resolve_requested(
    configured: str | None,
    requested: int | None,
    daemon_bytes: int,
) -> MemoryPolicy:
    safe_max = (daemon_bytes * SAFETY_NUMERATOR // SAFETY_DENOMINATOR // MIB) * MIB
    if safe_max < MIN_LIMIT:
        raise SandboxResourceError(
            "Docker has too little memory to retain the sandbox safety reserve: "
            f"{human_size(daemon_bytes)} available"
        )
    if requested is not None and requested > safe_max:
        raise SandboxResourceError(
            f"configured sandbox memory {human_size(requested)} exceeds the safe "
            f"maximum {human_size(safe_max)} for Docker capacity "
            f"{human_size(daemon_bytes)}"
        )
    return MemoryPolicy(
        bytes=min(DEFAULT_LIMIT, safe_max) if requested is None else requested,
        daemon_bytes=daemon_bytes,
        configured=configured,
    )


def docker_arguments(
    config_path: Path = INSTANCE,
    *,
    runner: Runner = subprocess.run,
) -> tuple[tuple[str, ...], MemoryPolicy]:
    policy = resolve_memory(config_path, runner=runner)
    value = str(policy.bytes)
    return ("--memory", value, "--memory-swap", value), policy


def _write_memory(value: str | None, config_path: Path = INSTANCE) -> None:
    if not config_path.exists():
        raise SandboxResourceError(
            f"{config_path} does not exist — run ./sc install first"
        )
    if value is None:
        instance_state.merge_instance_config(config_path, {}, remove=(BLOCK,))
        return
    parse_size(value)
    instance_state.merge_instance_config(
        config_path,
        {BLOCK: {MEMORY_KEY: value.strip().lower()}},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sc sandbox-memory",
        description="Show or configure the hard memory ceiling for the Docker sandbox.",
    )
    parser.add_argument(
        "value",
        nargs="?",
        default="show",
        help="a size such as 12g, or 'default' to remove the override",
    )
    return parser


def main(
    argv: Sequence[str],
    *,
    config_path: Path = INSTANCE,
    runner: Runner = subprocess.run,
) -> int:
    args = _parser().parse_args(list(argv))
    value = args.value.strip().lower()
    try:
        if value not in {"show", "default"}:
            requested = parse_size(value)
            policy = _resolve_requested(
                value,
                requested,
                docker_memory(runner=runner),
            )
            _write_memory(value, config_path)
        elif value == "default":
            policy = _resolve_requested(
                None,
                None,
                docker_memory(runner=runner),
            )
            _write_memory(None, config_path)
        else:
            policy = resolve_memory(config_path, runner=runner)
    except SandboxResourceError as exc:
        print(f"sandbox-memory: {exc}", file=sys.stderr)
        return 2
    configured = f"override {policy.configured}" if policy.configured else "default"
    print(
        f"sandbox memory: {human_size(policy.bytes)} ({configured}; "
        f"Docker {human_size(policy.daemon_bytes)}; swap disabled)"
    )
    if value != "show":
        print("  takes effect when the sandbox is next launched or restarted")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
