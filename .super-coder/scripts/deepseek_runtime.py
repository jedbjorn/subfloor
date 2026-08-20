#!/usr/bin/env python3
"""Pinned, isolated carrier and per-conversation state for DeepSeek Harness.

The super-coder engine remains importable on Python 3.9. DeepSeek's official
SDK/runtime pair runs only through a separate Python 3.10+ virtual environment;
this module never imports the SDK into the engine process.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


ENGINE = Path(__file__).resolve().parents[1]
ASSET_ROOT = ENGINE / "assets" / "deepseek"
MANIFEST_PATH = ASSET_ROOT / "runtime.json"
RUN_ROOT = ENGINE / "run" / "deepseek"
MINIMUM_PYTHON = (3, 10)
PROBE_TIMEOUT = 10
INSTALL_TIMEOUT = 900
MAX_DIAGNOSTIC_CHARS = 4096
SENSITIVE_ENV = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
SECRET_TEXT = (
    re.compile(r"(?i)(DEEPSEEK_API_KEY\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{8,}"),
)


class DeepSeekRuntimeError(RuntimeError):
    """Stable, DeepSeek-only runtime failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class RuntimeStatus:
    available: bool
    enabled: bool
    error: str | None
    detail: str | None
    carrier_python: str | None
    python_version: str | None
    sdk_version: str | None
    runtime_version: str | None
    composition_sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ConversationLayout:
    conversation_key: str
    root: Path
    home: Path
    session_root: Path
    diagnostics: Path
    process_identity: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_runtime_manifest(path: Path = MANIFEST_PATH) -> dict[str, object]:
    """Load and validate the immutable carrier/composition evidence."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepSeekRuntimeError(
            "HARNESS_RUNTIME_MANIFEST_INVALID", f"cannot read {path}: {exc}"
        ) from exc
    try:
        if raw["schema_version"] != 1:
            raise ValueError("unsupported schema_version")
        if raw["python_minimum"] != "3.10":
            raise ValueError("python_minimum must preserve the 3.10 carrier floor")
        sdk = raw["sdk"]
        runtime = raw["runtime"]
        composition = raw["composition"]
        if not isinstance(sdk, dict) or not isinstance(runtime, dict):
            raise ValueError("sdk/runtime evidence must be objects")
        if sdk["distribution"] != "deepseek-harness-sdk":
            raise ValueError("unexpected SDK distribution")
        if runtime["distribution"] != "deepseek-harness-runtime-bin":
            raise ValueError("unexpected runtime distribution")
        if sdk["version"] != runtime["version"]:
            raise ValueError("SDK/runtime versions must match exactly")
        if not isinstance(composition, dict):
            raise ValueError("composition evidence must be an object")
        if composition["path"] != "assets/deepseek/cordis.yml":
            raise ValueError("composition path escaped the engine-owned asset")
        composition_path = ENGINE / str(composition["path"])
        expected_digest = str(composition["sha256"])
        observed_digest = _sha256(composition_path)
        if observed_digest != expected_digest:
            raise DeepSeekRuntimeError(
                "HARNESS_COMPOSITION_DRIFT",
                f"composition digest {observed_digest} != {expected_digest}",
            )
    except DeepSeekRuntimeError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DeepSeekRuntimeError(
            "HARNESS_RUNTIME_MANIFEST_INVALID", str(exc)
        ) from exc
    return raw


def disabled_harnesses(env: Mapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if env is None else env
    return frozenset(
        item.strip().lower()
        for item in env.get("SC_DISABLED_HARNESSES", "").split(",")
        if item.strip()
    )


def carrier_python(
    *, env: Mapping[str, str] | None = None, engine: Path = ENGINE
) -> Path:
    env = os.environ if env is None else env
    configured = env.get("SC_DEEPSEEK_CARRIER_PYTHON", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise DeepSeekRuntimeError(
                "HARNESS_RUNTIME_MISSING",
                "SC_DEEPSEEK_CARRIER_PYTHON must be an absolute path",
            )
        return candidate
    version = str(load_runtime_manifest()["sdk"]["version"])
    return engine / "run" / "deepseek" / "carriers" / version / "bin" / "python"


def _status(
    *,
    available: bool,
    enabled: bool = True,
    error: str | None = None,
    detail: str | None = None,
    python: Path | None = None,
    python_version: str | None = None,
    sdk_version: str | None = None,
    runtime_version: str | None = None,
    manifest: Mapping[str, object] | None = None,
) -> RuntimeStatus:
    manifest = load_runtime_manifest() if manifest is None else manifest
    composition = manifest["composition"]
    assert isinstance(composition, dict)
    return RuntimeStatus(
        available=available,
        enabled=enabled,
        error=error,
        detail=detail,
        carrier_python=str(python) if python else None,
        python_version=python_version,
        sdk_version=sdk_version,
        runtime_version=runtime_version,
        composition_sha256=str(composition["sha256"]),
    )


_CARRIER_PROBE = """
import importlib.metadata
import json
import sys
print(json.dumps({
    "python": list(sys.version_info[:3]),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "sdk": importlib.metadata.version("deepseek-harness-sdk"),
    "runtime": importlib.metadata.version("deepseek-harness-runtime-bin"),
}, separators=(",", ":")))
""".strip()


def sanitize_diagnostic(
    value: str,
    *,
    secrets: Sequence[str] = (),
    limit: int = MAX_DIAGNOSTIC_CHARS,
) -> str:
    bounded = value
    for secret in secrets:
        if secret:
            bounded = bounded.replace(secret, "[REDACTED]")
    for pattern in SECRET_TEXT:
        bounded = pattern.sub(
            lambda match: (
                (match.group(1) if match.lastindex else "") + "[REDACTED]"
            ),
            bounded,
        )
    if len(bounded) > limit:
        return bounded[: max(0, limit - 14)] + "…[truncated]"
    return bounded


def probe_carrier(
    python: Path,
    *,
    runner=subprocess.run,
    manifest: Mapping[str, object] | None = None,
) -> RuntimeStatus:
    manifest = load_runtime_manifest() if manifest is None else manifest
    if not python.is_file() or not os.access(python, os.X_OK):
        return _status(
            available=False,
            error="HARNESS_RUNTIME_MISSING",
            detail=f"isolated carrier is absent: {python}",
            python=python,
            manifest=manifest,
        )
    try:
        completed = runner(
            [str(python), "-I", "-c", _CARRIER_PROBE],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_MISSING",
            detail=sanitize_diagnostic(str(exc)),
            python=python,
            manifest=manifest,
        )
    if completed.returncode != 0:
        detail = sanitize_diagnostic(completed.stderr or completed.stdout or "probe failed")
        return _status(
            available=False,
            error="HARNESS_RUNTIME_MISSING",
            detail=detail,
            python=python,
            manifest=manifest,
        )
    try:
        evidence = json.loads(completed.stdout)
        version_tuple = tuple(int(item) for item in evidence["python"][:2])
        python_version = ".".join(str(item) for item in evidence["python"])
        sdk_version = str(evidence["sdk"])
        runtime_version = str(evidence["runtime"])
        isolated = evidence["prefix"] != evidence["base_prefix"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_EVIDENCE_INVALID",
            detail=sanitize_diagnostic(str(exc)),
            python=python,
            manifest=manifest,
        )
    if version_tuple < MINIMUM_PYTHON:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_INCOMPATIBLE",
            detail=f"carrier Python {python_version} is older than 3.10",
            python=python,
            python_version=python_version,
            sdk_version=sdk_version,
            runtime_version=runtime_version,
            manifest=manifest,
        )
    if not isolated:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_NOT_ISOLATED",
            detail="DeepSeek packages are not running from a virtual environment",
            python=python,
            python_version=python_version,
            sdk_version=sdk_version,
            runtime_version=runtime_version,
            manifest=manifest,
        )
    sdk = manifest["sdk"]
    runtime = manifest["runtime"]
    assert isinstance(sdk, dict) and isinstance(runtime, dict)
    if sdk_version != sdk["version"] or runtime_version != runtime["version"]:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_VERSION_MISMATCH",
            detail=(
                f"observed SDK/runtime {sdk_version}/{runtime_version}; "
                f"required {sdk['version']}/{runtime['version']}"
            ),
            python=python,
            python_version=python_version,
            sdk_version=sdk_version,
            runtime_version=runtime_version,
            manifest=manifest,
        )
    return _status(
        available=True,
        python=python,
        python_version=python_version,
        sdk_version=sdk_version,
        runtime_version=runtime_version,
        manifest=manifest,
    )


def runtime_status(
    *,
    env: Mapping[str, str] | None = None,
    engine: Path = ENGINE,
    runner=subprocess.run,
) -> RuntimeStatus:
    env = os.environ if env is None else env
    manifest = load_runtime_manifest()
    if "deepseek" in disabled_harnesses(env):
        return _status(
            available=False,
            enabled=False,
            error="HARNESS_DISABLED",
            detail="DeepSeek is disabled by SC_DISABLED_HARNESSES",
            manifest=manifest,
        )
    try:
        python = carrier_python(env=env, engine=engine)
    except DeepSeekRuntimeError as exc:
        return _status(
            available=False,
            error=exc.code,
            detail=exc.detail,
            manifest=manifest,
        )
    return probe_carrier(python, runner=runner, manifest=manifest)


_PYTHON_VERSION_PROBE = (
    "import json,sys; print(json.dumps(list(sys.version_info[:3])))"
)


def discover_bootstrap_python(
    *,
    env: Mapping[str, str] | None = None,
    candidates: Sequence[str] | None = None,
    runner=subprocess.run,
) -> tuple[Path | None, str | None]:
    """Find a Python 3.10+ interpreter without changing the engine floor."""
    env = os.environ if env is None else env
    explicit = env.get("SC_DEEPSEEK_BOOTSTRAP_PYTHON", "").strip()
    names = (
        [explicit]
        if explicit
        else list(candidates or ())
        or [sys.executable, "python3.14", "python3.13", "python3.12", "python3.11", "python3.10"]
    )
    seen: set[str] = set()
    observed: list[str] = []
    for name in names:
        resolved = shutil.which(name) if not Path(name).is_absolute() else name
        if not resolved:
            continue
        python = Path(resolved).resolve()
        if str(python) in seen:
            continue
        seen.add(str(python))
        try:
            completed = runner(
                [str(python), "-I", "-c", _PYTHON_VERSION_PROBE],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT,
                check=False,
            )
            version = json.loads(completed.stdout) if completed.returncode == 0 else None
            pair = tuple(int(item) for item in version[:2]) if version else ()
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError):
            pair = ()
            version = None
        if version:
            observed.append(f"{python}={'.'.join(str(item) for item in version)}")
        if pair >= MINIMUM_PYTHON:
            return python, "; ".join(observed)
    detail = "; ".join(observed) or "no Python 3.10+ candidate found"
    return None, detail


def _write_install_evidence(
    root: Path,
    *,
    bootstrap_python: Path,
    status: RuntimeStatus,
    manifest: Mapping[str, object],
) -> None:
    evidence = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "bootstrap_python": str(bootstrap_python),
        "carrier_python": status.carrier_python,
        "python_version": status.python_version,
        "sdk_version": status.sdk_version,
        "runtime_version": status.runtime_version,
        "composition_sha256": status.composition_sha256,
        "source": manifest["source"],
        "sdk": manifest["sdk"],
        "runtime": manifest["runtime"],
    }
    target = root / "install-evidence.json"
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    target.chmod(0o600)


def ensure_carrier(
    *,
    env: Mapping[str, str] | None = None,
    engine: Path = ENGINE,
    runner=subprocess.run,
) -> RuntimeStatus:
    """Best-effort bare-metal install; never makes core installation fail."""
    env = os.environ if env is None else env
    current = runtime_status(env=env, engine=engine, runner=runner)
    if not current.enabled or current.available:
        return current
    if env.get("SC_DEEPSEEK_CARRIER_PYTHON", "").strip():
        return current
    target_python = carrier_python(env=env, engine=engine)
    target_root = target_python.parent.parent
    if target_root.exists():
        return _status(
            available=False,
            error="HARNESS_RUNTIME_PARTIAL",
            detail=f"refusing to replace partial carrier: {target_root}",
            python=target_python,
        )
    bootstrap, observed = discover_bootstrap_python(env=env, runner=runner)
    if bootstrap is None:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_INCOMPATIBLE",
            detail=observed,
            python=target_python,
        )
    manifest = load_runtime_manifest()
    sdk = manifest["sdk"]
    assert isinstance(sdk, dict)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{sdk['version']}-", dir=target_root.parent))
    try:
        for argv in (
            [str(bootstrap), "-m", "venv", str(temporary)],
            [
                str(temporary / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                f"{sdk['distribution']}=={sdk['version']}",
            ],
        ):
            completed = runner(
                argv,
                capture_output=True,
                text=True,
                timeout=INSTALL_TIMEOUT,
                check=False,
            )
            if completed.returncode != 0:
                detail = sanitize_diagnostic(completed.stderr or completed.stdout or "install failed")
                return _status(
                    available=False,
                    error="HARNESS_RUNTIME_INSTALL_FAILED",
                    detail=detail,
                    python=target_python,
                    manifest=manifest,
                )
        installed = probe_carrier(
            temporary / "bin" / "python", runner=runner, manifest=manifest
        )
        if not installed.available:
            return installed
        _write_install_evidence(
            temporary,
            bootstrap_python=bootstrap,
            status=installed,
            manifest=manifest,
        )
        try:
            temporary.rename(target_root)
        except FileExistsError:
            return probe_carrier(target_python, runner=runner, manifest=manifest)
        return probe_carrier(target_python, runner=runner, manifest=manifest)
    except (OSError, subprocess.SubprocessError) as exc:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_INSTALL_FAILED",
            detail=sanitize_diagnostic(str(exc)),
            python=target_python,
            manifest=manifest,
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def conversation_layout(
    conversation_id: int | str,
    *,
    state_root: Path | None = None,
) -> ConversationLayout:
    identity = str(conversation_id).strip()
    if not identity:
        raise DeepSeekRuntimeError(
            "HARNESS_SESSION_ID_INVALID", "conversation identity is empty"
        )
    key = hashlib.sha256(f"super-coder:deepseek:{identity}".encode()).hexdigest()[:32]
    root = (state_root or (RUN_ROOT / "conversations")) / key
    return ConversationLayout(
        conversation_key=key,
        root=root,
        home=root / "home",
        session_root=root / "sessions",
        diagnostics=root / "diagnostics",
        process_identity=root / "process.json",
    )


def provision_conversation(layout: ConversationLayout) -> None:
    for path in (layout.root, layout.home, layout.session_root, layout.diagnostics):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def launch_environment(
    layout: ConversationLayout,
    *,
    worktree: Path,
    system_prompt: str,
    api_key: str,
    base_url: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the explicit child environment without touching personal DSH state."""
    if not api_key.strip():
        raise DeepSeekRuntimeError(
            "HARNESS_CREDENTIAL_MISSING", "DEEPSEEK_API_KEY is required"
        )
    if not system_prompt:
        raise DeepSeekRuntimeError(
            "HARNESS_BOOT_SNAPSHOT_MISSING", "stored boot document bytes are required"
        )
    if not worktree.is_absolute() or not worktree.is_dir():
        raise DeepSeekRuntimeError(
            "HARNESS_WORKTREE_MISMATCH", f"worktree is unavailable: {worktree}"
        )
    if base_url is not None and not base_url.startswith(("https://", "http://")):
        raise DeepSeekRuntimeError(
            "HARNESS_PROVIDER_CONFIG_INVALID", "DeepSeek base URL must be HTTP(S)"
        )
    load_runtime_manifest()
    provision_conversation(layout)
    child = dict(os.environ if base_env is None else base_env)
    for name in tuple(child):
        if name.startswith("DSH_") or name in {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}:
            child.pop(name, None)
    child.update(
        {
            "DSH_HOME": str(layout.home),
            "DSH_SESSION_ROOT": str(layout.session_root),
            "DSH_CORDIS_CONFIG": str(ENGINE / "assets" / "deepseek" / "cordis.yml"),
            "DSH_CWD": str(worktree.resolve()),
            "DSH_SYSTEM_PROMPT": system_prompt,
            "DEEPSEEK_API_KEY": api_key,
            "PYTHONNOUSERSITE": "1",
        }
    )
    if base_url:
        child["DEEPSEEK_BASE_URL"] = base_url
    return child


def redacted_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: "[REDACTED]" if SENSITIVE_ENV.search(name) else value
        for name, value in env.items()
    }


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int:
    if pid <= 0:
        raise DeepSeekRuntimeError("HARNESS_PROCESS_IDENTITY_INVALID", "pid must be positive")
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
        _prefix, separator, fields = stat.rpartition(")")
        if not separator:
            raise ValueError("missing comm terminator")
        return int(fields.split()[19])
    except (OSError, ValueError, IndexError) as exc:
        raise DeepSeekRuntimeError(
            "HARNESS_PROCESS_IDENTITY_MISSING", f"cannot identify pid {pid}: {exc}"
        ) from exc


def record_process_identity(
    layout: ConversationLayout,
    *,
    pid: int,
    start_ticks: int,
    argv: Sequence[str],
) -> dict[str, object]:
    if pid <= 0 or start_ticks <= 0:
        raise DeepSeekRuntimeError(
            "HARNESS_PROCESS_IDENTITY_INVALID", "pid and start ticks must be positive"
        )
    provision_conversation(layout)
    evidence = {
        "pid": pid,
        "start_ticks": start_ticks,
        "argv_sha256": hashlib.sha256(
            json.dumps(list(argv), separators=(",", ":")).encode()
        ).hexdigest(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = layout.root / f".process-{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(evidence, target, separators=(",", ":"), sort_keys=True)
            target.write("\n")
        os.replace(temporary, layout.process_identity)
        layout.process_identity.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return evidence


def main(argv: Sequence[str]) -> int:
    status = ensure_carrier() if "--ensure" in argv else runtime_status()
    if "--json" in argv:
        print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
    else:
        observed = status.sdk_version or "—"
        detail = "available" if status.available else status.error
        print(f"deepseek {observed} · {detail}")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
