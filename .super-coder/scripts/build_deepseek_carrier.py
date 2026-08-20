#!/usr/bin/env python3
"""Reproduce the narrow super-coder DeepSeek lifecycle carrier from source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Mapping, Sequence


ENGINE = Path(__file__).resolve().parents[1]
MANIFEST = ENGINE / "assets" / "deepseek" / "runtime.json"
SOURCE_DATE_EPOCH = "1786963622"
COMMAND_TIMEOUT = 2700


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict[str, object]:
    payload = json.loads(MANIFEST.read_text())
    if payload.get("schema_version") != 2:
        raise RuntimeError("carrier build requires DeepSeek runtime manifest schema 2")
    return payload


def checked_run(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> None:
    print("deepseek-carrier-build:", " ".join(argv), flush=True)
    subprocess.run(
        list(argv),
        cwd=cwd,
        env=None if env is None else dict(env),
        timeout=COMMAND_TIMEOUT,
        check=True,
    )


def verify_source(source: Path, manifest: Mapping[str, object]) -> None:
    provenance = manifest["source"]
    build = manifest["build"]
    assert isinstance(provenance, dict) and isinstance(build, dict)
    package = json.loads((source / "package.json").read_text())
    if package.get("version") != "0.1.0-rc.7":
        raise RuntimeError("source package version is not 0.1.0-rc.7")
    for path, expected in (
        (source / "pnpm-lock.yaml", build["lock_sha256"]),
        (source / "LICENSE", provenance["license_sha256"]),
    ):
        observed = sha256(path)
        if observed != expected:
            raise RuntimeError(f"source drift for {path}: {observed} != {expected}")
    if (source / ".git").exists():
        observed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if observed != provenance["commit"]:
            raise RuntimeError(f"source commit {observed} != {provenance['commit']}")


def acquire_source(destination: Path, manifest: Mapping[str, object]) -> Path:
    provenance = manifest["source"]
    assert isinstance(provenance, dict)
    archive = destination / "source.tar.gz"
    digest = hashlib.sha256()
    with urllib.request.urlopen(str(provenance["archive_url"]), timeout=120) as response:
        with archive.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
    observed = digest.hexdigest()
    if observed != provenance["archive_sha256"]:
        raise RuntimeError(
            f"source archive drift: {observed} != {provenance['archive_sha256']}"
        )
    unpacked = destination / "source"
    unpacked.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if any(member.name.startswith(("/", "../")) or "/../" in member.name for member in members):
            raise RuntimeError("source archive contains an unsafe path")
        bundle.extractall(unpacked)
    roots = tuple(path for path in unpacked.iterdir() if path.is_dir())
    if len(roots) != 1:
        raise RuntimeError("source archive must contain exactly one root directory")
    return roots[0]


def patch_source(source: Path, manifest: Mapping[str, object]) -> None:
    patch = manifest["patch"]
    assert isinstance(patch, dict)
    patch_path = ENGINE / str(patch["path"])
    observed = sha256(patch_path)
    if observed != patch["sha256"]:
        raise RuntimeError(f"carrier patch drift: {observed} != {patch['sha256']}")
    checked_run(["git", "apply", "--check", str(patch_path)], cwd=source)
    checked_run(["git", "apply", str(patch_path)], cwd=source)
    metadata = json.loads(
        (source / "python" / "sdk-runtime" / "src" / "deepseek_harness_runtime"
         / "deepseek-harness-runtime.json").read_text()
    )
    carrier = metadata.get("carrier")
    if not isinstance(carrier, dict) or carrier.get("protocol") != patch["protocol"]:
        raise RuntimeError("patched runtime does not declare the required carrier protocol")


def platform_name() -> str:
    import platform

    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"x86_64", "amd64"} else None
    system = platform.system().lower()
    operating_system = "macos" if system == "darwin" else "linux" if system == "linux" else None
    if operating_system is None or architecture is None:
        raise RuntimeError(f"unsupported carrier build host: {system}-{machine}")
    return f"{operating_system}-{architecture}"


def build(source: Path, output: Path, manifest: Mapping[str, object]) -> dict[str, object]:
    build_evidence = manifest["build"]
    assert isinstance(build_evidence, dict)
    target = platform_name()
    environment = {
        **os.environ,
        "CI": "true",
        "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
        "PYTHONHASHSEED": "0",
    }
    pnpm = [
        "npx",
        "--yes",
        "--package",
        f"node@{build_evidence['node_version']}",
        "--package",
        f"pnpm@{build_evidence['pnpm_version']}",
        "--",
        "pnpm",
    ]
    checked_run([*pnpm, "install", "--frozen-lockfile"], cwd=source, env=environment)
    checked_run(
        [
            *pnpm,
            "exec",
            "vitest",
            "run",
            "packages/sdk/server/tests/server.spec.ts",
            "packages/llm/llm-deepseek/tests/serialize.spec.ts",
        ],
        cwd=source,
        env=environment,
    )
    checked_run(
        [*pnpm, "exec", "tsx", "scripts/build-exe-for-python-sdk.ts", f"--targets=node24-{target}"],
        cwd=source,
        env=environment,
    )
    build_venv = source.parent / "python-build"
    checked_run([sys.executable, "-m", "venv", str(build_venv)], cwd=source, env=environment)
    builder_python = build_venv / "bin" / "python"
    checked_run(
        [str(builder_python), "-m", "pip", "install", f"uv=={build_evidence['uv_version']}"],
        cwd=source,
        env=environment,
    )
    build_environment = {**environment, "PATH": f"{build_venv / 'bin'}{os.pathsep}{environment.get('PATH', '')}"}
    checked_run(
        [
            str(build_venv / "bin" / "uv"),
            "run",
            "--python",
            "3.10",
            "--group",
            "test",
            "--project",
            "python/sdk",
            "pytest",
            "python/sdk/tests/test_client.py",
            "-q",
        ],
        cwd=source,
        env=build_environment,
    )
    output.mkdir(parents=True, exist_ok=True)
    executable = source / "dist-exe" / f"dsh-jsonrpc-agent-pkg-{target}"
    for package in ("runtime", "sdk"):
        argv = [
            str(builder_python),
            "scripts/build-python-release.py",
            "--package",
            package,
            "--output-dir",
            str(output),
        ]
        if package == "runtime":
            argv.extend(["--platform", target, "--runtime-exe", str(executable)])
        checked_run(argv, cwd=source, env=build_environment)
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 2:
        raise RuntimeError(f"expected SDK and runtime wheels; found {[path.name for path in wheels]}")
    return {
        "schema_version": 1,
        "platform": target,
        "source_commit": manifest["source"]["commit"],
        "source_archive_sha256": manifest["source"]["archive_sha256"],
        "patch_sha256": manifest["patch"]["sha256"],
        "build_recipe_sha256": build_evidence["sha256"],
        "build_tools": {
            "node": build_evidence["node_version"],
            "pnpm": build_evidence["pnpm_version"],
            "uv": build_evidence["uv_version"],
            "source_date_epoch": build_evidence["source_date_epoch"],
        },
        "artifacts": [
            {"filename": path.name, "sha256": sha256(path), "size": path.stat().st_size}
            for path in wheels
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="verified exact-commit checkout")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    manifest = load_manifest()
    with tempfile.TemporaryDirectory(prefix="sc-deepseek-carrier-") as raw:
        workspace = Path(raw)
        if args.source is None:
            source = acquire_source(workspace, manifest)
        else:
            source = workspace / "source"
            shutil.copytree(
                args.source.resolve(),
                source,
                ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "lib", "dist*"),
            )
        verify_source(source, manifest)
        patch_source(source, manifest)
        if args.verify_only:
            print(json.dumps({"verified": True, "source_commit": manifest["source"]["commit"]}, sort_keys=True))
            return 0
        evidence = build(source, args.output_dir.resolve(), manifest)
    evidence_path = args.output_dir.resolve() / "deepseek-carrier-artifacts.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(evidence_path)
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
