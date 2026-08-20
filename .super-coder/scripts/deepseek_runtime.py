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
INSTALL_TIMEOUT = 3600
MAX_DIAGNOSTIC_CHARS = 4096
SENSITIVE_ENV = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
SECRET_TEXT = (
    re.compile(r"(?i)(DEEPSEEK_API_KEY\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{8,}"),
)
LIFECYCLE_METHODS = frozenset(
    {"session/start", "session/cancel", "session/inspect", "session/reconcile", "shutdown"}
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
        if raw["schema_version"] != 2:
            raise ValueError("unsupported schema_version")
        if raw["python_minimum"] != "3.10":
            raise ValueError("python_minimum must preserve the 3.10 carrier floor")
        sdk = raw["sdk"]
        runtime = raw["runtime"]
        carrier = raw["carrier"]
        source = raw["source"]
        patch = raw["patch"]
        build = raw["build"]
        composition = raw["composition"]
        if not isinstance(sdk, dict) or not isinstance(runtime, dict):
            raise ValueError("sdk/runtime evidence must be objects")
        if sdk["distribution"] != "deepseek-harness-sdk":
            raise ValueError("unexpected SDK distribution")
        if runtime["distribution"] != "deepseek-harness-runtime-bin":
            raise ValueError("unexpected runtime distribution")
        if sdk["version"] != runtime["version"]:
            raise ValueError("SDK/runtime versions must match exactly")
        if not all(isinstance(item, dict) for item in (carrier, source, patch, build)):
            raise ValueError("carrier provenance must contain object evidence")
        if carrier["protocol"] != "super-coder-deepseek-lifecycle-v1":
            raise ValueError("unexpected lifecycle carrier protocol")
        if carrier["acquisition"] != "verified-source-build":
            raise ValueError("carrier acquisition must remain verified-source-build")
        if source["commit"] != "bb4ca698d63714e753f5621b07400e6ebb0b5d97":
            raise ValueError("unexpected DeepSeek source commit")
        if patch["protocol"] != carrier["protocol"]:
            raise ValueError("patch and carrier protocol identities differ")
        if runtime["platforms"] != ["macos-arm64", "linux-arm64", "linux-x64"]:
            raise ValueError("runtime platform contract drifted")
        if not isinstance(composition, dict):
            raise ValueError("composition evidence must be an object")
        if composition["path"] != "assets/deepseek/cordis.yml":
            raise ValueError("composition path escaped the engine-owned asset")
        for evidence, expected_path, code in (
            (composition, "assets/deepseek/cordis.yml", "HARNESS_COMPOSITION_DRIFT"),
            (patch, "assets/deepseek/deepseek-harness-bb4ca698-lifecycle.patch", "HARNESS_RUNTIME_ARTIFACT_DRIFT"),
            (source, "assets/deepseek/LICENSE.deepseek-harness", "HARNESS_RUNTIME_ARTIFACT_DRIFT"),
            (build, "scripts/build_deepseek_carrier.py", "HARNESS_RUNTIME_ARTIFACT_DRIFT"),
        ):
            path_key = "license_path" if evidence is source else "path"
            digest_key = "license_sha256" if evidence is source else "sha256"
            if evidence[path_key] != expected_path:
                raise ValueError(f"carrier asset path must be {expected_path}")
            observed_digest = _sha256(ENGINE / str(evidence[path_key]))
            expected_digest = str(evidence[digest_key])
            if observed_digest != expected_digest:
                raise DeepSeekRuntimeError(
                    code, f"{expected_path} digest {observed_digest} != {expected_digest}"
                )
    except DeepSeekRuntimeError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
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
import importlib.resources
import json
import sys
carrier = json.loads(importlib.resources.files("deepseek_harness_runtime").joinpath("deepseek-harness-runtime.json").read_text())["carrier"]
print(json.dumps({
    "python": list(sys.version_info[:3]),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "sdk": importlib.metadata.version("deepseek-harness-sdk"),
    "runtime": importlib.metadata.version("deepseek-harness-runtime-bin"),
    "carrier": carrier,
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
        carrier_evidence = evidence["carrier"]
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
    carrier = manifest["carrier"]
    source = manifest["source"]
    assert all(isinstance(item, dict) for item in (sdk, runtime, carrier, source))
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
    if not isinstance(carrier_evidence, dict) or carrier_evidence != {
        "protocol": carrier["protocol"],
        "sourceCommit": source["commit"],
    }:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_ARTIFACT_DRIFT",
            detail="installed runtime lacks the exact lifecycle carrier identity",
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
    if not python.is_file():
        marker = container_incompatibility_marker(python)
        if marker.is_file():
            return read_container_incompatibility(
                marker, python=python, manifest=manifest
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


def container_runtime_platform(architecture: str) -> str | None:
    """Map a Linux machine architecture to an rc7 runtime wheel family."""
    normalized = architecture.strip().lower()
    return {
        "amd64": "linux-x64",
        "x86_64": "linux-x64",
        "arm64": "linux-arm64",
        "aarch64": "linux-arm64",
    }.get(normalized)


def carrier_runtime_platform(
    *, system: str | None = None, architecture: str | None = None
) -> str | None:
    import platform

    observed_system = (system or platform.system()).strip().lower()
    observed_architecture = (architecture or platform.machine()).strip().lower()
    family = (
        "macos"
        if observed_system in {"darwin", "macos"}
        else "linux" if observed_system == "linux" else None
    )
    arch = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(observed_architecture)
    return f"{family}-{arch}" if family and arch else None


def container_incompatibility_marker(python: Path) -> Path:
    return python.parent.parent.with_suffix(".unavailable.json")


def _write_container_incompatibility(
    marker: Path,
    *,
    architecture: str,
    detail: str,
    manifest: Mapping[str, object],
) -> None:
    sdk = manifest["sdk"]
    assert isinstance(sdk, dict)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    payload = {
        "error": "HARNESS_RUNTIME_INCOMPATIBLE",
        "detail": detail,
        "architecture": architecture,
        "sdk_version": sdk["version"],
    }
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(payload, target, separators=(",", ":"), sort_keys=True)
            target.write("\n")
        os.replace(temporary, marker)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_container_incompatibility(
    marker: Path,
    *,
    python: Path,
    manifest: Mapping[str, object] | None = None,
) -> RuntimeStatus:
    manifest = load_runtime_manifest() if manifest is None else manifest
    try:
        evidence = json.loads(marker.read_text())
        if evidence["error"] != "HARNESS_RUNTIME_INCOMPATIBLE":
            raise ValueError("unexpected container incompatibility code")
        detail = str(evidence["detail"])
        architecture = str(evidence["architecture"])
        sdk = manifest["sdk"]
        assert isinstance(sdk, dict)
        if evidence["sdk_version"] != sdk["version"]:
            raise ValueError("container marker version does not match runtime manifest")
    except (AssertionError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_EVIDENCE_INVALID",
            detail=sanitize_diagnostic(f"invalid container marker {marker}: {exc}"),
            python=python,
            manifest=manifest,
        )
    return _status(
        available=False,
        error="HARNESS_RUNTIME_INCOMPATIBLE",
        detail=f"{detail} (architecture: {architecture})",
        python=python,
        manifest=manifest,
    )


def _write_install_evidence(
    root: Path,
    *,
    bootstrap_python: Path,
    status: RuntimeStatus,
    manifest: Mapping[str, object],
    artifacts: Mapping[str, object],
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
        "declared_sdk_artifact": manifest["sdk"],
        "declared_runtime_artifacts": manifest["runtime"],
        "built_artifacts": artifacts,
    }
    target = root / "install-evidence.json"
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    target.chmod(0o600)


def _load_built_artifacts(
    directory: Path, *, manifest: Mapping[str, object]
) -> tuple[dict[str, object], tuple[Path, Path]]:
    build = manifest["build"]
    source = manifest["source"]
    patch = manifest["patch"]
    sdk = manifest["sdk"]
    runtime = manifest["runtime"]
    assert all(isinstance(item, dict) for item in (build, source, patch, sdk, runtime))
    evidence_path = directory / str(build["artifact_evidence"])
    try:
        evidence = json.loads(evidence_path.read_text())
        platform = carrier_runtime_platform()
        if evidence["schema_version"] != 1 or evidence["platform"] != platform:
            raise ValueError("artifact platform evidence does not match this host")
        if evidence["source_commit"] != source["commit"]:
            raise ValueError("artifact source commit drifted")
        if evidence["source_archive_sha256"] != source["archive_sha256"]:
            raise ValueError("artifact source archive drifted")
        if evidence["patch_sha256"] != patch["sha256"]:
            raise ValueError("artifact patch drifted")
        if evidence["build_recipe_sha256"] != build["sha256"]:
            raise ValueError("artifact build recipe drifted")
        if evidence["build_tools"] != {
            "node": build["node_version"],
            "pnpm": build["pnpm_version"],
            "uv": build["uv_version"],
            "source_date_epoch": build["source_date_epoch"],
        }:
            raise ValueError("artifact build tool evidence drifted")
        records = evidence["artifacts"]
        if not isinstance(records, list) or len(records) != 2:
            raise ValueError("artifact evidence must contain exactly two wheels")
        wheels: list[Path] = []
        distributions = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("artifact record must be an object")
            filename = str(record["filename"])
            if Path(filename).name != filename or not filename.endswith(".whl"):
                raise ValueError("artifact filename is unsafe")
            wheel = directory / filename
            if _sha256(wheel) != record["sha256"] or wheel.stat().st_size != record["size"]:
                raise ValueError(f"artifact digest or size drifted: {filename}")
            distributions.add(filename.split("-", 1)[0])
            wheels.append(wheel)
        expected = {
            str(sdk["distribution"]).replace("-", "_"),
            str(runtime["distribution"]).replace("-", "_"),
        }
        if distributions != expected:
            raise ValueError(f"artifact distributions {sorted(distributions)} != {sorted(expected)}")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeepSeekRuntimeError(
            "HARNESS_RUNTIME_ARTIFACT_DRIFT", str(exc)
        ) from exc
    return evidence, (wheels[0], wheels[1])


def _install_carrier_at(
    target_python: Path,
    *,
    bootstrap: Path,
    runner=subprocess.run,
    manifest: Mapping[str, object] | None = None,
) -> RuntimeStatus:
    manifest = load_runtime_manifest() if manifest is None else manifest
    sdk = manifest["sdk"]
    assert isinstance(sdk, dict)
    target_root = target_python.parent.parent
    if target_root.exists():
        return _status(
            available=False,
            error="HARNESS_RUNTIME_PARTIAL",
            detail=f"refusing to replace partial carrier: {target_root}",
            python=target_python,
            manifest=manifest,
        )
    target_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{sdk['version']}-", dir=target_root.parent))
    try:
        build = manifest["build"]
        assert isinstance(build, dict)
        artifacts_dir = temporary / "artifacts"
        commands = (
            [str(bootstrap), "-m", "venv", str(temporary)],
            [
                str(bootstrap),
                str(ENGINE / str(build["path"])),
                "--output-dir",
                str(artifacts_dir),
            ],
        )
        for argv in commands:
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
        artifact_evidence, wheels = _load_built_artifacts(
            artifacts_dir, manifest=manifest
        )
        for argv in (
            [
                str(temporary / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "pydantic>=2.12,<3",
            ],
            [
                str(temporary / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                *(str(wheel) for wheel in wheels),
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
            artifacts=artifact_evidence,
        )
        shutil.rmtree(artifacts_dir)
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


def prepare_container_carrier(
    target_root: Path,
    *,
    architecture: str,
    bootstrap_python: Path = Path(sys.executable),
    runner=subprocess.run,
) -> RuntimeStatus:
    """Install on supported Linux images; mark optional incompatibility otherwise."""
    manifest = load_runtime_manifest()
    target_python = target_root / "bin" / "python"
    marker = container_incompatibility_marker(target_python)
    platform = container_runtime_platform(architecture)
    if platform is None:
        detail = f"pinned DeepSeek carrier has no build target for {architecture or 'unknown'}"
        _write_container_incompatibility(
            marker,
            architecture=architecture or "unknown",
            detail=detail,
            manifest=manifest,
        )
        return read_container_incompatibility(
            marker, python=target_python, manifest=manifest
        )
    bootstrap, observed = discover_bootstrap_python(
        env={"SC_DEEPSEEK_BOOTSTRAP_PYTHON": str(bootstrap_python)},
        runner=runner,
    )
    if bootstrap is None:
        detail = f"container has no Python 3.10+ carrier interpreter: {observed}"
        _write_container_incompatibility(
            marker,
            architecture=architecture,
            detail=detail,
            manifest=manifest,
        )
        return read_container_incompatibility(
            marker, python=target_python, manifest=manifest
        )
    status = _install_carrier_at(
        target_python,
        bootstrap=bootstrap,
        runner=runner,
        manifest=manifest,
    )
    if status.available and marker.exists():
        marker.unlink()
    return status


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
    bootstrap, observed = discover_bootstrap_python(env=env, runner=runner)
    if bootstrap is None:
        return _status(
            available=False,
            error="HARNESS_RUNTIME_INCOMPATIBLE",
            detail=observed,
            python=target_python,
        )
    return _install_carrier_at(
        target_python,
        bootstrap=bootstrap,
        runner=runner,
    )


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


def provider_request_options(
    *, thinking: str, reasoning_effort: str
) -> dict[str, str]:
    """Return the exact immutable wire patch consumed by the pinned carrier."""
    if thinking not in {"omit", "enabled", "disabled"}:
        raise DeepSeekRuntimeError(
            "HARNESS_PROVIDER_OPTION_INVALID", f"unsupported thinking option: {thinking}"
        )
    if reasoning_effort not in {"omit", "low", "high", "max"}:
        raise DeepSeekRuntimeError(
            "HARNESS_PROVIDER_OPTION_INVALID",
            f"unsupported reasoning effort: {reasoning_effort}",
        )
    load_runtime_manifest()
    return {"thinking": thinking, "reasoningEffort": reasoning_effort}


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
    container_install = "--install-container-carrier" in argv
    if container_install:
        index = argv.index("--install-container-carrier")
        try:
            target_root = Path(argv[index + 1])
            architecture = argv[index + 2]
        except IndexError:
            raise SystemExit(
                "deepseek-runtime: --install-container-carrier requires "
                "<absolute-target-root> <architecture>"
            )
        if not target_root.is_absolute():
            raise SystemExit(
                "deepseek-runtime: container carrier target must be absolute"
            )
        status = prepare_container_carrier(
            target_root, architecture=architecture
        )
    else:
        status = ensure_carrier() if "--ensure" in argv else runtime_status()
    if "--json" in argv:
        print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
    else:
        observed = status.sdk_version or "—"
        detail = "available" if status.available else status.error
        print(f"deepseek {observed} · {detail}")
    if not container_install:
        return 0
    return 0 if status.available or status.error == "HARNESS_RUNTIME_INCOMPATIBLE" else 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
