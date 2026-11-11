"""Build and validate engine-base, native-package, and extension images.

The engine owns every image identity and proof.  A fork may declare a bounded
APT package set and an exact Git-tracked Dockerfile context; failures in that
native capability never make the proven core engine baseline unavailable.
"""
from __future__ import annotations

import base64
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runtime_flags
import sandbox_resources
from devkit import AptPackage, Declaration, DevkitConfigError, load_declaration

IMAGE_PREFIX = "super-coder"
DEFAULT_PARENT_IMAGE = "python:3.14-slim"
PACKAGE_CONTRACT_VERSION = 1
READINESS_CONTRACT_VERSION = 2
CONTEXT_CONTRACT_VERSION = 1
BASE_IMAGE_HISTORY = 2
HEX_REF = re.compile(r"\A[0-9a-f]{40,64}\Z")
SAFE_EPOCH = re.compile(r"\A[0-9A-Za-z_.:-]+\Z")
BASE_LABELS = (
    "sc.image_kind",
    "sc.engine_ref",
    "sc.harness_epoch",
    "sc.engine_dockerfile_digest",
    "sc.build_identity",
    "sc.parent_ref",
    "sc.parent_id",
)
EXTENSION_LABELS = (
    "sc.image_kind",
    "sc.engine_ref",
    "sc.harness_epoch",
    "sc.declaration_digest",
    "sc.fork_identity",
    "sc.dockerfile_digest",
    "sc.context_digest",
    "sc.parent_id",
    "sc.engine_base_id",
    "sc.package_digest",
    "sc.package_layer_id",
)
MOUNT_MARKER = "SC_DEVKIT_MOUNTS"
# The host paths under the operator's home that `./sc launch` bind-mounts,
# writable, into the sandbox: harness state homes and the Claude profile file.
# This is the ONLY list. The dispatcher's `docker run` names them through
# HOME_MOUNT_MARKER, expanded in `docker_run` below, and host-side code that
# must stay out of the sandbox's reach (api/engine_ops.py) excludes exactly
# these. Add a mount here and both follow.
HOME_BIND_MOUNTS = (
    ".claude",
    ".claude.json",
    ".config/opencode",
    ".local/share/opencode",
    ".codex",
    ".vibe",
    ".kimi-code",
)
HOME_MOUNT_MARKER = "SC_HOME_MOUNTS"
# Where the session-hash directory (`auth_sessions_dir`) appears inside the
# sandbox. A fixed path, so the in-container review server needs no state-home
# derivation of its own; mounted read-only by `docker_run`, and by nothing else.
AUTH_SESSIONS_TARGET = "/run/super-coder/auth-sessions"
GITHUB_HOST_TRUST = Path("assets/github_known_hosts")


class SandboxImageError(RuntimeError):
    """The requested sandbox image is invalid, stale, or unavailable."""


class SandboxPrerequisiteError(SandboxImageError):
    """A host prerequisite could not be invoked."""


class ProvisionFailed(SandboxImageError):
    """A declared provision hook completed unsuccessfully."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ImagePlan:
    checkout: Path
    engine: Path
    declaration: Declaration | None
    engine_ref: str
    harness_epoch: str
    installation_identity: str
    parent_ref: str
    declaration_digest: str
    package_digest: str
    dockerfile_digest: str
    base_tag: str
    package_layer_tag: str
    package_tag: str
    runtime_tag: str
    base_labels: dict[str, str]
    runtime_labels: dict[str, str]
    user: str
    uid: str
    gid: str
    candidate_error: str | None = None

    @property
    def extends_base(self) -> bool:
        return (
            self.declaration is not None
            and self.declaration.sandbox is not None
            and self.declaration.sandbox.has_extension
        )

    @property
    def has_package_contract(self) -> bool:
        sandbox = self.declaration.sandbox if self.declaration is not None else None
        return bool(
            sandbox is not None
            and (sandbox.packages is not None or sandbox.package_error is not None)
        )

    @property
    def packages(self) -> tuple[AptPackage, ...]:
        sandbox = self.declaration.sandbox if self.declaration is not None else None
        if sandbox is None or sandbox.packages is None:
            return ()
        return sandbox.packages.apt


@dataclass(frozen=True)
class VolumePlan:
    logical_name: str
    volume_name: str
    target: Path
    labels: dict[str, str]


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _digest_file(path: Path, field: str) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise SandboxImageError(f"{field}: cannot read {path}: {exc}") from exc


def _engine_ref(checkout: Path, engine: Path) -> str:
    pin = checkout / ".sc-state" / "engine.ref"
    try:
        value = pin.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if HEX_REF.fullmatch(value):
        return value
    try:
        done = subprocess.run(
            ("git", "-C", str(engine.parent), "rev-parse", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(
            f"cannot run git to resolve engine ref: {exc}"
        ) from exc
    fallback = done.stdout.strip()
    if done.returncode != 0 or not HEX_REF.fullmatch(fallback):
        detail = done.stderr.strip() or "no valid .sc-state/engine.ref or Git HEAD"
        raise SandboxImageError(f"cannot resolve engine ref: {detail}")
    return fallback


def _validate_extension_dockerfile(path: Path) -> None:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SandboxImageError(f"sandbox.dockerfile: cannot read {path}: {exc}") from exc
    instructions = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        instructions.append(line)
    arg_positions = [
        index
        for index, line in enumerate(instructions)
        if re.fullmatch(r"ARG\s+SC_BASE_IMAGE(?:\s*=.*)?", line, re.IGNORECASE)
    ]
    from_positions = [
        (index, line)
        for index, line in enumerate(instructions)
        if re.match(r"FROM\s+", line, re.IGNORECASE)
    ]
    if not arg_positions:
        raise SandboxImageError(
            "sandbox.dockerfile: must declare ARG SC_BASE_IMAGE before its final FROM"
        )
    if not from_positions:
        raise SandboxImageError("sandbox.dockerfile: must contain a FROM instruction")
    _, final_from = from_positions[-1]
    first_from_index = from_positions[0][0]
    if min(arg_positions) > first_from_index:
        raise SandboxImageError(
            "sandbox.dockerfile: ARG SC_BASE_IMAGE must be global (before the first FROM)"
        )
    if not re.fullmatch(
        r"FROM\s+(?:--platform=\S+\s+)?\$\{SC_BASE_IMAGE\}(?:\s+AS\s+\S+)?",
        final_from,
        re.IGNORECASE,
    ):
        raise SandboxImageError(
            "sandbox.dockerfile: final stage must use FROM ${SC_BASE_IMAGE}"
        )


def image_plan(
    checkout: Path,
    engine: Path,
    harness_epoch: str,
    *,
    user: str,
    uid: str,
    gid: str,
) -> ImagePlan:
    checkout = checkout.resolve(strict=True)
    engine = engine.resolve(strict=True)
    if not SAFE_EPOCH.fullmatch(harness_epoch):
        raise SandboxImageError("harness epoch contains unsupported characters")
    try:
        declaration = load_declaration(checkout)
    except DevkitConfigError as exc:
        raise SandboxImageError(f"invalid dev-kit declaration: {exc}") from exc
    candidate_error = None
    if (
        declaration is not None
        and declaration.sandbox is not None
        and declaration.sandbox.dockerfile is not None
    ):
        try:
            _validate_extension_dockerfile(declaration.sandbox.dockerfile)
        except SandboxImageError as exc:
            if declaration.sandbox.packages is None and declaration.sandbox.package_error is None:
                raise
            candidate_error = str(exc)

    engine_ref = _engine_ref(checkout, engine)
    base_dockerfile_digest = _digest_file(engine / "Dockerfile", "engine Dockerfile")
    install_identity = _sha256_text(str(engine.parent.resolve(strict=True)))
    parent_ref = os.environ.get("SC_PARENT_IMAGE", DEFAULT_PARENT_IMAGE).strip()
    if not parent_ref or any(character.isspace() for character in parent_ref):
        raise SandboxImageError("SC_PARENT_IMAGE must be one non-empty image reference")
    declaration_digest = (
        _sha256_text(declaration.canonical_json) if declaration is not None else "absent"
    )
    dockerfile_digest = (
        _digest_file(declaration.sandbox.dockerfile, "sandbox.dockerfile")
        if declaration is not None
        and declaration.sandbox is not None
        and declaration.sandbox.dockerfile is not None
        else "none"
    )
    sandbox = declaration.sandbox if declaration is not None else None
    if sandbox is not None and sandbox.packages is not None:
        package_digest = _sha256_text(
            json.dumps(sandbox.packages.canonical_atoms, separators=(",", ":"))
        )
    elif sandbox is not None and sandbox.package_error is not None:
        package_digest = "invalid"
        candidate_error = candidate_error or sandbox.package_error
    else:
        package_digest = "none"
    build_identity = _sha256_text(
        json.dumps([user, uid, gid], separators=(",", ":"))
    )
    base_key = _sha256_text(
        json.dumps(
            [
                engine_ref,
                harness_epoch,
                base_dockerfile_digest,
                build_identity,
                parent_ref,
            ],
            separators=(",", ":"),
        )
    )
    base_tag = f"{IMAGE_PREFIX}-base:{base_key[:20]}"
    base_labels = {
        "sc.image_kind": "engine-base",
        "sc.engine_ref": engine_ref,
        "sc.harness_epoch": harness_epoch,
        "sc.engine_dockerfile_digest": base_dockerfile_digest,
        "sc.build_identity": build_identity,
        "sc.parent_ref": parent_ref,
    }
    package_layer_tag = f"{IMAGE_PREFIX}-package-layer-{install_identity[:20]}:latest"
    package_tag = f"{IMAGE_PREFIX}-packages-{install_identity[:20]}:latest"
    runtime_labels = (
        {
            "sc.image_kind": "fork-extension" if sandbox and sandbox.has_extension else "fork-packages",
            "sc.engine_ref": engine_ref,
            "sc.harness_epoch": harness_epoch,
            "sc.declaration_digest": declaration_digest,
            "sc.fork_identity": install_identity,
            "sc.dockerfile_digest": dockerfile_digest,
            "sc.package_digest": package_digest,
            "sc.build_identity": build_identity,
            "sc.readiness_contract": str(READINESS_CONTRACT_VERSION),
            "sc.package_contract": str(PACKAGE_CONTRACT_VERSION),
            "sc.context_contract": str(CONTEXT_CONTRACT_VERSION),
        }
        if declaration is not None and declaration.sandbox is not None
        else base_labels
    )
    runtime_tag = (
        f"{IMAGE_PREFIX}-sandbox-{install_identity[:20]}:latest"
        if sandbox is not None and sandbox.has_extension
        else package_tag
        if sandbox is not None and (sandbox.packages is not None or sandbox.package_error is not None)
        else base_tag
    )
    return ImagePlan(
        checkout=checkout,
        engine=engine,
        declaration=declaration,
        engine_ref=engine_ref,
        harness_epoch=harness_epoch,
        installation_identity=install_identity,
        parent_ref=parent_ref,
        declaration_digest=declaration_digest,
        package_digest=package_digest,
        dockerfile_digest=dockerfile_digest,
        base_tag=base_tag,
        package_layer_tag=package_layer_tag,
        package_tag=package_tag,
        runtime_tag=runtime_tag,
        base_labels=base_labels,
        runtime_labels=runtime_labels,
        user=user,
        uid=uid,
        gid=gid,
        candidate_error=candidate_error,
    )


def _run(command: Sequence[str], *, runner: Runner, capture: bool = False) -> str:
    try:
        done = runner(
            tuple(command),
            check=False,
            text=True,
            **({"capture_output": True} if capture else {}),
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(f"cannot run {command[0]}: {exc}") from exc
    if done.returncode != 0:
        detail = ((done.stderr or "") if capture else "").strip()
        suffix = f": {detail}" if detail else ""
        raise SandboxImageError(
            f"command failed ({done.returncode}): {' '.join(command)}{suffix}"
        )
    return (done.stdout or "").strip() if capture else ""


def _label_arguments(labels: dict[str, str]) -> list[str]:
    result = []
    for key in sorted(labels):
        result.extend(("--label", f"{key}={labels[key]}"))
    return result


def _inspect(image: str, *, runner: Runner) -> dict[str, Any]:
    raw = _run(("docker", "image", "inspect", image), runner=runner, capture=True)
    try:
        value = json.loads(raw)
        item = value[0]
        image_id = item["Id"]
        created = item.get("Created")
        labels = item.get("Config", {}).get("Labels") or {}
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise SandboxImageError(f"docker returned invalid inspection for {image}") from exc
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise SandboxImageError(f"docker returned invalid image ID for {image}")
    if not isinstance(labels, dict):
        raise SandboxImageError(f"docker returned invalid labels for {image}")
    return {"id": image_id, "created": created, "labels": labels}


def _require_labels(image: str, expected: dict[str, str], *, runner: Runner) -> str:
    inspected = _inspect(image, runner=runner)
    labels = inspected["labels"]
    mismatches = [
        key for key, value in expected.items() if labels.get(key) != value
    ]
    if mismatches:
        detail = ", ".join(
            f"{key}={labels.get(key)!r} (need {expected[key]!r})" for key in mismatches
        )
        raise SandboxImageError(f"sandbox image {image!r} is stale or foreign: {detail}")
    return str(inspected["id"])


def _retire_superseded_images(
    *,
    image_kind: str,
    identity_label: str,
    identity_value: str,
    current_id: str,
    keep_prior: int,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Best-effort retirement of superseded images in one ownership scope."""
    if keep_prior < 0:
        raise ValueError("keep_prior must be non-negative")
    try:
        raw = _run(
            (
                "docker", "image", "ls", "--all",
                "--filter", f"label=sc.image_kind={image_kind}",
                "--filter", f"label={identity_label}={identity_value}",
                "--format", "{{.ID}}",
            ),
            runner=runner,
            capture=True,
        )
        candidates: dict[str, str] = {}
        for listed_id in sorted(set(filter(None, raw.splitlines()))):
            inspected = _inspect(listed_id, runner=runner)
            labels = inspected["labels"]
            if (
                labels.get("sc.image_kind") != image_kind
                or labels.get(identity_label) != identity_value
            ):
                print(
                    "dev-kit image retention: skipped image whose ownership "
                    f"labels changed: {listed_id}",
                    file=sys.stderr,
                )
                continue
            image_id = str(inspected["id"])
            if image_id == current_id:
                continue
            created = inspected.get("created")
            if not isinstance(created, str) or not created:
                print(
                    "dev-kit image retention: skipped image with no creation "
                    f"timestamp: {image_id}",
                    file=sys.stderr,
                )
                continue
            candidates[image_id] = created

        newest_first = sorted(
            candidates.items(), key=lambda item: (item[1], item[0]), reverse=True
        )
        removed = []
        for image_id, _created in newest_first[keep_prior:]:
            try:
                _run(
                    ("docker", "image", "rm", image_id),
                    runner=runner,
                    capture=True,
                )
            except SandboxImageError as exc:
                print(
                    f"dev-kit image retention: kept {image_id}: {exc}",
                    file=sys.stderr,
                )
                continue
            removed.append(image_id)
        if removed:
            print(
                f"dev-kit image retention: removed {len(removed)} "
                f"superseded {image_kind} image(s)"
            )
        return removed
    except SandboxImageError as exc:
        print(f"dev-kit image retention: skipped: {exc}", file=sys.stderr)
        return []


def retire_superseded_base_images(
    plan: ImagePlan,
    current_id: str,
    *,
    keep_prior: int = BASE_IMAGE_HISTORY,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Retain current and recent shared engine-base generations."""
    return _retire_superseded_images(
        image_kind="engine-base",
        identity_label="sc.build_identity",
        identity_value=plan.base_labels["sc.build_identity"],
        current_id=current_id,
        keep_prior=keep_prior,
        runner=runner,
    )


def retire_superseded_runtime_images(
    plan: ImagePlan,
    current_id: str,
    *,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Retire old extension generations owned by this exact fork."""
    return _retire_superseded_images(
        image_kind="fork-extension",
        identity_label="sc.fork_identity",
        identity_value=plan.runtime_labels["sc.fork_identity"],
        current_id=current_id,
        keep_prior=0,
        runner=runner,
    )


def retire_superseded_package_images(
    plan: ImagePlan,
    current_id: str,
    *,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Retire old native-package generations owned by this exact fork."""
    return _retire_superseded_images(
        image_kind="fork-packages",
        identity_label="sc.fork_identity",
        identity_value=plan.runtime_labels["sc.fork_identity"],
        current_id=current_id,
        keep_prior=0,
        runner=runner,
    )


def retire_superseded_package_layers(
    plan: ImagePlan,
    current_id: str,
    *,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Retire old intermediate package layers owned by this exact fork."""
    return _retire_superseded_images(
        image_kind="fork-package-layer",
        identity_label="sc.fork_identity",
        identity_value=plan.runtime_labels["sc.fork_identity"],
        current_id=current_id,
        keep_prior=0,
        runner=runner,
    )


def _resolve_parent(plan: ImagePlan, *, runner: Runner, allow_pull: bool) -> str:
    try:
        return str(_inspect(plan.parent_ref, runner=runner)["id"])
    except SandboxPrerequisiteError:
        raise
    except SandboxImageError:
        if not allow_pull:
            raise SandboxImageError(
                f"--no-build cannot resolve local parent {plan.parent_ref!r}"
            )
    _run(("docker", "pull", plan.parent_ref), runner=runner, capture=True)
    return str(_inspect(plan.parent_ref, runner=runner)["id"])


def _base_expected(plan: ImagePlan, parent_id: str) -> dict[str, str]:
    return {**plan.base_labels, "sc.parent_id": parent_id}


def _runtime_expected(
    plan: ImagePlan,
    *,
    image_kind: str,
    parent_id: str,
    base_id: str,
    package_layer_id: str,
    context_digest: str,
) -> dict[str, str]:
    return {
        **plan.runtime_labels,
        "sc.image_kind": image_kind,
        "sc.parent_id": parent_id,
        "sc.engine_base_id": base_id,
        "sc.package_layer_id": package_layer_id,
        "sc.context_digest": context_digest,
    }


def _prove_baseline(image_id: str, *, runner: Runner) -> None:
    _run(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-c",
            "exit 0",
        ),
        runner=runner,
        capture=True,
    )


def _git_bytes(
    command: Sequence[str], *, runner: Runner = subprocess.run
) -> bytes:
    try:
        completed = runner(
            tuple(command), check=False, text=False, capture_output=True
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(f"cannot run git: {exc}") from exc
    if completed.returncode != 0:
        raw = completed.stderr or b""
        detail = raw.decode("utf-8", errors="replace").strip()
        raise SandboxImageError(detail or "git command failed")
    value = completed.stdout or b""
    return value if isinstance(value, bytes) else value.encode()


def _tracked_context(
    plan: ImagePlan, *, git_runner: Runner = subprocess.run
) -> tuple[str, bytes, str]:
    assert plan.declaration is not None and plan.declaration.sandbox is not None
    sandbox = plan.declaration.sandbox
    if sandbox.context is None or sandbox.dockerfile is None:
        raise SandboxImageError("sandbox extension context is absent")
    context = sandbox.context
    try:
        context_relative = context.relative_to(plan.checkout).as_posix()
        dockerfile_relative = sandbox.dockerfile.relative_to(context).as_posix()
    except ValueError as exc:
        raise SandboxImageError(
            "sandbox.dockerfile must be inside sandbox.context"
        ) from exc
    raw = _git_bytes(
        (
            "git",
            "-C",
            str(plan.checkout),
            "ls-files",
            "--cached",
            "--stage",
            "-z",
            "--",
            context_relative,
        ),
        runner=git_runner,
    )
    records: list[tuple[str, str, Path, bytes, str | None]] = []
    total = 0
    for encoded in filter(None, raw.split(b"\0")):
        try:
            header, encoded_path = encoded.split(b"\t", 1)
            mode, _object_id, stage = header.decode("ascii").split(" ")
            repo_path = encoded_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise SandboxImageError("tracked context contains malformed Git index data") from exc
        if stage != "0":
            raise SandboxImageError(f"tracked context path is unmerged: {repo_path}")
        absolute = plan.checkout / repo_path
        try:
            relative = absolute.relative_to(context).as_posix()
        except ValueError as exc:
            raise SandboxImageError(f"tracked context path escapes context: {repo_path}") from exc
        normalized = Path(relative).as_posix()
        if relative in {"", "."} or normalized != relative or relative.startswith("../"):
            raise SandboxImageError(f"tracked context path is not normalized: {repo_path}")
        if len(relative.encode("utf-8")) > 512:
            raise SandboxImageError(f"tracked context path exceeds 512 bytes: {repo_path}")
        if mode not in {"100644", "100755", "120000"}:
            raise SandboxImageError(f"tracked context mode {mode} is unsupported: {repo_path}")
        if mode == "120000":
            try:
                target = os.readlink(absolute)
            except OSError as exc:
                raise SandboxImageError(f"tracked symlink is unreadable: {repo_path}: {exc}") from exc
            target_bytes = target.encode("utf-8")
            resolved = (absolute.parent / target).resolve(strict=False)
            if resolved != context and context not in resolved.parents:
                raise SandboxImageError(f"tracked symlink escapes context: {repo_path}")
            content = target_bytes
            link = target
        else:
            try:
                info = absolute.stat()
                content = absolute.read_bytes()
            except OSError as exc:
                raise SandboxImageError(f"tracked context path is unreadable: {repo_path}: {exc}") from exc
            if not stat.S_ISREG(info.st_mode):
                raise SandboxImageError(f"tracked context path is not a regular file: {repo_path}")
            if len(content) > 32 * 1024 * 1024:
                raise SandboxImageError(f"tracked context file exceeds 32 MiB: {repo_path}")
            total += len(content)
            if total > 128 * 1024 * 1024:
                raise SandboxImageError("tracked context exceeds 128 MiB aggregate bytes")
            link = None
        records.append((relative, mode, absolute, content, link))
    if len(records) > 4096:
        raise SandboxImageError("tracked context exceeds 4096 entries")
    records.sort(key=lambda item: item[0].encode("utf-8"))
    if dockerfile_relative not in {item[0] for item in records}:
        raise SandboxImageError("sandbox.dockerfile must be Git-tracked inside its context")
    manifest = [
        {
            "path": relative,
            "mode": mode,
            "length": len(content),
            "sha256": _sha256_bytes(content),
        }
        for relative, mode, _absolute, content, _link in records
    ]
    digest_input = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    for _relative, _mode, _absolute, content, _link in records:
        digest_input += content
    context_digest = _sha256_bytes(digest_input)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for relative, mode, _absolute, content, link in records:
            info = tarfile.TarInfo(relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if mode == "120000":
                info.type = tarfile.SYMTYPE
                info.linkname = link or ""
                info.mode = 0o777
                info.size = 0
                tar.addfile(info)
            else:
                info.mode = 0o755 if mode == "100755" else 0o644
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return context_digest, archive.getvalue(), dockerfile_relative


def _run_archive_build(command: Sequence[str], archive: bytes, *, runner: Runner) -> None:
    try:
        completed = runner(
            tuple(command),
            check=False,
            text=False,
            capture_output=True,
            input=archive,
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(f"cannot run docker: {exc}") from exc
    if completed.returncode != 0:
        raw = completed.stderr or b""
        detail = raw.decode("utf-8", errors="replace").strip()
        raise SandboxImageError(
            f"command failed ({completed.returncode}): docker build: {detail}"
        )


def _apt_dockerfile(packages: Sequence[AptPackage], user: str) -> str:
    install = ["apt-get", "install", "-y", "--no-install-recommends"]
    install.extend(package.atom for package in packages)
    return "\n".join(
        (
            "ARG SC_BASE_IMAGE",
            "FROM ${SC_BASE_IMAGE}",
            "USER root",
            'RUN ["apt-get", "update"]',
            "RUN " + json.dumps(install, separators=(",", ":")),
            'RUN ["sh", "-c", "rm -rf /var/lib/apt/lists/*"]',
            f"USER {user}",
            "",
        )
    )


def _build_package_layer(
    plan: ImagePlan,
    labels: dict[str, str],
    *,
    runner: Runner,
) -> str:
    with tempfile.TemporaryDirectory(prefix="sc-native-packages-") as empty_context:
        command = [
            "docker",
            "build",
            "-t",
            plan.package_layer_tag,
            "-f",
            "-",
            "--build-arg",
            f"SC_BASE_IMAGE={plan.base_tag}",
            *_label_arguments(labels),
            empty_context,
        ]
        try:
            completed = runner(
                tuple(command),
                check=False,
                text=True,
                capture_output=True,
                input=_apt_dockerfile(plan.packages, plan.user),
            )
        except OSError as exc:
            raise SandboxPrerequisiteError(f"cannot run docker: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            raise SandboxImageError(
                f"package_layer_build failed ({completed.returncode}): {detail}"
            )
    return _require_labels(plan.package_layer_tag, labels, runner=runner)


def _build_package_runtime(
    plan: ImagePlan,
    labels: dict[str, str],
    *,
    runner: Runner,
) -> str:
    """Wrap a package-only layer so its immutable ID can be labeled exactly."""
    with tempfile.TemporaryDirectory(prefix="sc-native-runtime-") as empty_context:
        command = [
            "docker",
            "build",
            "-t",
            plan.package_tag,
            "-f",
            "-",
            "--build-arg",
            f"SC_BASE_IMAGE={plan.package_layer_tag}",
            *_label_arguments(labels),
            empty_context,
        ]
        try:
            completed = runner(
                tuple(command),
                check=False,
                text=True,
                capture_output=True,
                input="ARG SC_BASE_IMAGE\nFROM ${SC_BASE_IMAGE}\n",
            )
        except OSError as exc:
            raise SandboxPrerequisiteError(f"cannot run docker: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            raise SandboxImageError(
                f"package_runtime_build failed ({completed.returncode}): {detail}"
            )
    return _require_labels(plan.package_tag, labels, runner=runner)


def _prove_packages(
    image_id: str, packages: Sequence[AptPackage], *, runner: Runner
) -> tuple[list[dict[str, str]], str, tuple[str, ...]]:
    names = tuple(package.name for package in packages)
    command = (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/usr/bin/dpkg-query",
        image_id,
        "--show",
        "--showformat=${binary:Package}\t${Architecture}\t${Version}\t${Status}\n",
        *names,
    )
    try:
        completed = runner(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxImageError("package_proof timed out after 60s") from exc
    except OSError as exc:
        raise SandboxPrerequisiteError(f"cannot run docker: {exc}") from exc
    if completed.returncode != 0:
        raise SandboxImageError(
            f"package_proof failed ({completed.returncode}): {(completed.stderr or '').strip()}"
        )
    by_name: dict[str, list[dict[str, str]]] = {}
    for line in (completed.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise SandboxImageError("package_proof returned malformed output")
        binary_name, architecture, version, status_text = fields
        canonical_name = binary_name
        if ":" in binary_name:
            canonical_name, qualifier = binary_name.rsplit(":", 1)
            if qualifier != architecture:
                raise SandboxImageError("package_proof returned a mismatched architecture qualifier")
        row = {
            "name": canonical_name,
            "architecture": architecture,
            "version": version,
            "status": status_text,
        }
        by_name.setdefault(canonical_name, []).append(row)
    observed = []
    for package in packages:
        rows_for_name = by_name.get(package.name, [])
        if len(rows_for_name) != 1:
            raise SandboxImageError(
                f"package_proof expected one row for {package.name!r}; got {len(rows_for_name)}"
            )
        row = rows_for_name[0]
        if row["status"] != "install ok installed":
            raise SandboxImageError(f"package_proof status is not installed for {package.name!r}")
        if package.version is not None and row["version"] != package.version:
            raise SandboxImageError(
                f"package_proof version mismatch for {package.name!r}: "
                f"{row['version']!r} != {package.version!r}"
            )
        observed.append(row)
    if set(by_name) != set(names):
        raise SandboxImageError("package_proof returned undeclared or missing package rows")
    proof_digest = _sha256_text(
        json.dumps(observed, sort_keys=True, separators=(",", ":"))
    )
    return observed, proof_digest, command


def _source_commit_for(checkout: Path) -> tuple[str, bool]:
    commit = _run(
        ("git", "-C", str(checkout), "rev-parse", "HEAD"),
        runner=subprocess.run,
        capture=True,
    )
    dirty = _run(
        (
            "git",
            "-C",
            str(checkout),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ),
        runner=subprocess.run,
        capture=True,
    )
    return commit, not bool(dirty)


def _source_commit(plan: ImagePlan) -> tuple[str, bool]:
    return _source_commit_for(plan.checkout)


def _status_path(plan: ImagePlan) -> tuple[Path, str]:
    checkout_identity = _sha256_text(str(plan.checkout))
    root = (
        plan.checkout
        / ".sc-state"
        / "local"
        / "dev-kit"
        / checkout_identity[:20]
    )
    return root / "status.json", checkout_identity


def _read_status(plan: ImagePlan) -> dict[str, Any] | None:
    path, _identity = _status_path(plan)
    return _read_receipt(path)


def _selected_tag(plan: ImagePlan) -> str:
    status = _read_status(plan)
    if (
        status is not None
        and status.get("engine_ref") == plan.engine_ref
        and status.get("declaration_digest") == plan.declaration_digest
        and status.get("package_digest") == plan.package_digest
        and isinstance(status.get("selected_tag"), str)
    ):
        return status["selected_tag"]
    return plan.runtime_tag


def _write_status(plan: ImagePlan, value: dict[str, Any]) -> dict[str, Any]:
    root, checkout_identity = _artifact_root(plan)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "status.json"
    payload = {
        "format_version": 1,
        "checkout_identity": checkout_identity,
        "updated_at": _utc_now(),
        "engine_ref": plan.engine_ref,
        "declaration_digest": plan.declaration_digest,
        "package_digest": plan.package_digest,
        **value,
    }
    _atomic_json(path, payload)
    return payload


def _container_identity(container: str | None, *, runner: Runner) -> str | None:
    if not container:
        return None
    try:
        value = _run(
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}\t{{.Image}}",
                container,
            ),
            runner=runner,
            capture=True,
        )
    except SandboxImageError:
        return None
    running, separator, image_id = value.partition("\t")
    if running != "true" or not separator or not image_id.startswith("sha256:"):
        return None
    return image_id


def _instance_port(plan: ImagePlan) -> int | None:
    try:
        value = json.loads((plan.engine / "instance.json").read_text())
        port = value.get("port")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return port if type(port) is int and 1 <= port <= 65535 else None


def _intent_root(plan: ImagePlan) -> Path:
    root = plan.engine.parent / ".sc-state" / "local" / "runtime-flags"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _next_generation(plan: ImagePlan, source: str) -> int:
    root = _intent_root(plan)
    lock = _acquire_lock(root / "generation.lock", 30.0)
    try:
        path = root / "generations.json"
        current = _read_receipt(path) or {}
        generation = int(current.get(source, 0)) + 1
        current[source] = generation
        _atomic_json(path, current)
        return generation
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _persist_intent(
    plan: ImagePlan, source: str, body: dict[str, Any]
) -> dict[str, Any]:
    root = _intent_root(plan) / "pending"
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / f"{source}-{body['generation']:020d}.json"
    envelope = {"source_key": source, "body": body}
    _atomic_json(path, envelope)
    port = _instance_port(plan)
    if port is None:
        return {"state": "pending", "path": str(path)}
    try:
        status, receipt = runtime_flags.put_via_api(
            plan.engine.parent, port, source, body
        )
    except (OSError, RuntimeError, urllib.error.URLError):
        return {"state": "pending", "path": str(path)}
    if status in {200, 201} or (
        status == 409
        and receipt.get("error", {}).get("code") == "stale_generation"
    ):
        path.unlink(missing_ok=True)
        return {"state": "stored", **receipt}
    return {"state": "pending", "path": str(path), "api": receipt}


def reconcile_pending(plan: ImagePlan) -> list[dict[str, Any]]:
    pending = _intent_root(plan) / "pending"
    if not pending.is_dir() or _instance_port(plan) is None:
        return []
    results = []
    for path in sorted(
        candidate
        for candidate in pending.iterdir()
        if candidate.is_file() and candidate.suffix == ".json"
    ):
        value = _read_receipt(path)
        if not value or not isinstance(value.get("body"), dict):
            continue
        result = _persist_intent(plan, value["source_key"], value["body"])
        results.append(result)
    return results


def _advisory_fallback(
    plan: ImagePlan,
    *,
    base_id: str,
    parent_id: str,
    container: str | None,
    classification: str,
    detail: str,
    image_ids: dict[str, str],
    evidence_path: str,
    runner: Runner,
) -> str:
    existing_id = _container_identity(container, runner=runner)
    selected_runtime = "existing_unchanged" if existing_id else "engine_baseline"
    selected_tag = plan.runtime_tag if existing_id else plan.base_tag
    cutover = "unchanged" if existing_id else "baseline_fallback"
    status_fields = {
            "core_runtime": "ready",
            "native_packages": "advisory",
            "fork_readiness": "degraded",
            "selected_runtime": selected_runtime,
            "selected_tag": selected_tag,
            "selected_image_id": existing_id or base_id,
            "package_receipt": "stale",
            "cutover": cutover,
            "classification": classification,
            "detail": detail[:2000],
            "parent_id": parent_id,
            "engine_base_id": base_id,
    }
    try:
        status = _write_status(plan, status_fields)
    except (OSError, SandboxImageError) as exc:
        status = {
            "checkout_identity": _sha256_text(str(plan.checkout)),
            **status_fields,
        }
        detail = f"{detail}; local advisory evidence persistence failed: {exc}"
        print(f"dev-kit advisory persistence: {exc}", file=sys.stderr)
    try:
        commit, clean = _source_commit(plan)
    except SandboxImageError:
        commit, clean = "unavailable", False
    source = runtime_flags.source_key(
        plan.installation_identity, status["checkout_identity"]
    )
    try:
        generation = _next_generation(plan, source)
    except (OSError, SandboxImageError) as exc:
        print(f"dev-kit advisory generation persistence: {exc}", file=sys.stderr)
        return selected_tag
    advisory = {
        "checkout_identity": status["checkout_identity"],
        "source_commit": commit,
        "source_tracked_clean": clean,
        "declaration_digest": plan.declaration_digest,
        "package_digest": plan.package_digest,
        "failing_atoms": [package.atom for package in plan.packages],
        "classification": classification,
        "detail": detail[:2000],
        "image_ids": image_ids,
        "evidence_path": evidence_path,
        "core_runtime": "ready",
        "native_packages": "advisory",
        "fork_readiness": "degraded",
        "selected_runtime": selected_runtime,
        "cutover": cutover,
        "remedy": runtime_flags.REMEDY,
    }
    body = {
        "state": "open",
        "source_kind": runtime_flags.SOURCE_KIND,
        "generation": generation,
        "evidence_digest": runtime_flags.canonical_digest(advisory),
        "advisory": advisory,
    }
    try:
        receipt = _persist_intent(plan, source, body)
    except (OSError, RuntimeError, SandboxImageError) as exc:
        receipt = {"state": "persistence_failed", "detail": str(exc)[:1000]}
        print(f"dev-kit advisory intent persistence: {exc}", file=sys.stderr)
    status["advisory"] = {
        "source_key": source,
        "generation": generation,
        **receipt,
    }
    try:
        _write_status(plan, {key: value for key, value in status.items() if key not in {"format_version", "checkout_identity", "updated_at"}})
    except (OSError, SandboxImageError):
        pass
    return selected_tag


def _maybe_clear_advisory(
    plan: ImagePlan,
    status: dict[str, Any],
    *,
    receipt_path: str | None,
    runner: Runner,
    container: str | None = None,
) -> dict[str, Any] | None:
    advisory = status.get("advisory")
    if not isinstance(advisory, dict):
        return None
    source = advisory.get("source_key")
    failed_generation = advisory.get("generation")
    if not isinstance(source, str) or type(failed_generation) is not int:
        return None
    commit, clean = _source_commit(plan)
    if not clean:
        return None
    if plan.has_package_contract:
        proof = _read_receipt(_status_path(plan)[0].with_name("package-proof.json")) or {}
        if not receipt_path or receipt_path == "none":
            return None
        clearance_kind = "current_contract"
        package_layer_id = proof.get("package_layer_id", "none")
        requested = proof.get("requested", [])
        observed = proof.get("observed", [])
        proof_digest = proof.get("proof_digest", "none")
        package_receipt = receipt_path
    else:
        clearance_kind = (
            "declaration_absent" if plan.declaration is None else "packages_removed"
        )
        package_layer_id = "none"
        requested = []
        observed = []
        proof_digest = "none"
        package_receipt = "none"
    generation = _next_generation(plan, source)
    clearance = {
        "clearance_kind": clearance_kind,
        "source_commit": commit,
        "source_tracked_clean": True,
        "failed_generation": failed_generation,
        "old_declaration_digest": status.get("advisory_declaration_digest", "unknown"),
        "current_declaration_digest": plan.declaration_digest,
        "baseline_id": status.get("engine_base_id", "none"),
        "extension_id": status.get("final_image_id", "none") if plan.extends_base else "none",
        "package_layer_id": package_layer_id,
        "requested": requested,
        "observed": observed,
        "proof_digest": proof_digest,
        "package_receipt": package_receipt,
        "evidence": str(_status_path(plan)[0]),
        "cutover_owed": bool(
            plan.has_package_contract
            and _container_identity(container, runner=runner)
            != status.get("selected_image_id")
        ),
    }
    body = {
        "state": "resolved",
        "source_kind": runtime_flags.SOURCE_KIND,
        "generation": generation,
        "evidence_digest": runtime_flags.canonical_digest(clearance),
        "clearance": clearance,
    }
    return _persist_intent(plan, source, body)


def build_images(
    plan: ImagePlan,
    *,
    runner: Runner = subprocess.run,
    git_runner: Runner = subprocess.run,
    container: str | None = None,
) -> str:
    previous_status = _read_status(plan) or {}
    reconcile_pending(plan)
    parent_id = _resolve_parent(plan, runner=runner, allow_pull=True)
    trust_path = plan.engine / GITHUB_HOST_TRUST
    try:
        trust_bytes = trust_path.read_bytes()
    except OSError as exc:
        raise SandboxImageError(
            f"engine GitHub host trust: cannot read {trust_path}: {exc}"
        ) from exc
    if not trust_bytes.strip():
        raise SandboxImageError("engine GitHub host trust is empty")
    trust_digest = _sha256_bytes(trust_bytes)
    trust_b64 = base64.b64encode(trust_bytes).decode("ascii")
    base_command = [
        "docker",
        "build",
        "-t",
        plan.base_tag,
        "-f",
        str(plan.engine / "Dockerfile"),
        "--build-arg",
        f"SC_USER={plan.user}",
        "--build-arg",
        f"SC_UID={plan.uid}",
        "--build-arg",
        f"SC_GID={plan.gid}",
        "--build-arg",
        f"SC_HARNESS_EPOCH={plan.harness_epoch}",
        "--build-arg",
        f"SC_PARENT_IMAGE={plan.parent_ref}",
        "--build-arg",
        f"SC_GITHUB_HOST_TRUST_B64={trust_b64}",
        "--build-arg",
        f"SC_GITHUB_HOST_TRUST_SHA256={trust_digest}",
        *_label_arguments(_base_expected(plan, parent_id)),
        str(plan.checkout),
    ]
    _run(base_command, runner=runner)
    base_labels = _base_expected(plan, parent_id)
    base_id = _require_labels(plan.base_tag, base_labels, runner=runner)
    _prove_baseline(base_id, runner=runner)
    if not plan.extends_base and not plan.has_package_contract:
        provision_declared = bool(
            plan.declaration is not None and plan.declaration.provision is not None
        )
        ready = _write_status(plan, {
            "core_runtime": "ready", "native_packages": "not_declared",
            "fork_readiness": "degraded" if provision_declared else "not_declared",
            "selected_runtime": "engine_baseline",
            "selected_tag": plan.base_tag, "selected_image_id": base_id,
            "package_receipt": "pending" if provision_declared else "none",
            "cutover": "baseline",
            "parent_id": parent_id, "engine_base_id": base_id,
            "advisory": previous_status.get("advisory"),
            "advisory_declaration_digest": previous_status.get("declaration_digest"),
        })
        clearance = _maybe_clear_advisory(
            plan, ready, receipt_path="none", runner=runner, container=container
        )
        if clearance is not None:
            ready["advisory"] = None
            ready["clearance"] = clearance
            _write_status(plan, {key: value for key, value in ready.items() if key not in {"format_version", "checkout_identity", "updated_at", "engine_ref", "declaration_digest", "package_digest"}})
        retire_superseded_base_images(plan, base_id, runner=runner)
        return plan.base_tag
    assert plan.declaration is not None and plan.declaration.sandbox is not None
    package_id = "none"
    context_digest = "none"
    final_tag = plan.base_tag
    final_id = base_id
    proof: list[dict[str, str]] = []
    proof_digest = "none"
    proof_command: tuple[str, ...] = ()
    try:
        if plan.candidate_error:
            raise SandboxImageError(plan.candidate_error)
        if plan.has_package_contract:
            package_labels = _runtime_expected(
                plan,
                image_kind="fork-package-layer",
                parent_id=parent_id,
                base_id=base_id,
                package_layer_id="self",
                context_digest="none",
            )
            package_id = _build_package_layer(plan, package_labels, runner=runner)
            final_tag, final_id = plan.package_layer_tag, package_id
        if plan.extends_base:
            context_digest, archive, dockerfile_relative = _tracked_context(
                plan, git_runner=git_runner
            )
            extension_labels = _runtime_expected(
                plan,
                image_kind="fork-extension",
                parent_id=parent_id,
                base_id=base_id,
                package_layer_id=package_id,
                context_digest=context_digest,
            )
            extension_command = [
                "docker",
                "build",
                "-t",
                plan.runtime_tag,
                "-f",
                dockerfile_relative,
                "--build-arg",
                f"SC_BASE_IMAGE={final_tag}",
                *_label_arguments(extension_labels),
                "-",
            ]
            _run_archive_build(extension_command, archive, runner=runner)
            final_tag = plan.runtime_tag
            final_id = _require_labels(final_tag, extension_labels, runner=runner)
        elif plan.has_package_contract:
            runtime_labels = _runtime_expected(
                plan,
                image_kind="fork-packages",
                parent_id=parent_id,
                base_id=base_id,
                package_layer_id=package_id,
                context_digest="none",
            )
            final_tag = plan.package_tag
            final_id = _build_package_runtime(plan, runtime_labels, runner=runner)
        if plan.has_package_contract:
            proof, proof_digest, proof_command = _prove_packages(
                final_id, plan.packages, runner=runner
            )
    except SandboxPrerequisiteError:
        raise
    except SandboxImageError as exc:
        if not plan.has_package_contract:
            raise
        return _advisory_fallback(
            plan,
            base_id=base_id,
            parent_id=parent_id,
            container=container,
            classification="native_package_candidate",
            detail=str(exc),
            image_ids={"parent": parent_id, "engine_base": base_id, "package_layer": package_id, "candidate": final_id},
            evidence_path=str(_status_path(plan)[0]),
            runner=runner,
        )

    package_state = {
        "requested": [package.atom for package in plan.packages],
        "observed": proof,
        "proof_digest": proof_digest,
        "proof_argv": list(proof_command),
        "parent_id": parent_id,
        "engine_base_id": base_id,
        "package_layer_id": package_id,
        "context_digest": context_digest,
        "final_tag": final_tag,
        "final_image_id": final_id,
    }
    try:
        proof_root, _checkout_identity = _artifact_root(plan, git_runner=git_runner)
        proof_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        proof_path = proof_root / "package-proof.json"
        _atomic_json(proof_path, package_state)
    except (OSError, SandboxImageError) as exc:
        if not plan.has_package_contract:
            raise
        return _advisory_fallback(
            plan,
            base_id=base_id,
            parent_id=parent_id,
            container=container,
            classification="package_receipt",
            detail=str(exc),
            image_ids={
                "parent": parent_id,
                "engine_base": base_id,
                "package_layer": package_id,
                "candidate": final_id,
            },
            evidence_path=str(_status_path(plan)[0]),
            runner=runner,
        )
    ready_status = {
        "core_runtime": "ready",
        "native_packages": "ready" if plan.has_package_contract else "not_declared",
        "fork_readiness": "ready" if (plan.has_package_contract or plan.declaration.provision) else "not_declared",
        "selected_runtime": "package_complete" if plan.has_package_contract else "engine_baseline",
        "selected_tag": final_tag,
        "selected_image_id": final_id,
        "package_receipt": "pending" if plan.declaration.provision else "current",
        "cutover": "package_complete" if plan.has_package_contract else "baseline",
        "advisory": previous_status.get("advisory"),
        "advisory_declaration_digest": previous_status.get("declaration_digest"),
        **package_state,
    }
    try:
        _write_status(plan, ready_status)
    except (OSError, SandboxImageError) as exc:
        if not plan.has_package_contract:
            raise
        return _advisory_fallback(
            plan,
            base_id=base_id,
            parent_id=parent_id,
            container=container,
            classification="package_receipt",
            detail=str(exc),
            image_ids={
                "parent": parent_id,
                "engine_base": base_id,
                "package_layer": package_id,
                "candidate": final_id,
            },
            evidence_path=str(_status_path(plan)[0]),
            runner=runner,
        )
    if plan.has_package_contract and plan.declaration.provision is None:
        try:
            package_receipt = provision_checkout(
                plan,
                None,
                runner=runner,
                git_runner=git_runner,
                emit=False,
            )
        except SandboxPrerequisiteError:
            raise
        except SandboxImageError as exc:
            return _advisory_fallback(
                plan,
                base_id=base_id,
                parent_id=parent_id,
                container=container,
                classification="package_receipt",
                detail=str(exc),
                image_ids={"parent": parent_id, "engine_base": base_id, "package_layer": package_id, "candidate": final_id},
                evidence_path=str(_status_path(plan)[0]),
                runner=runner,
            )
        ready_status["package_receipt"] = {
            "fingerprint": package_receipt["fingerprint"],
            "path": package_receipt["receipt"],
        }
        stored_status = _read_status(plan) or {}
        if stored_status.get("clearance") is not None:
            ready_status["advisory"] = None
            ready_status["clearance"] = stored_status["clearance"]
        _write_status(plan, ready_status)
    elif not plan.has_package_contract:
        clearance = _maybe_clear_advisory(
            plan, ready_status, receipt_path="none", runner=runner, container=container
        )
        if clearance is not None:
            ready_status["advisory"] = None
            ready_status["clearance"] = clearance
            _write_status(plan, ready_status)
    # Retire extension children before their now-unreferenced base parents.
    if plan.extends_base:
        retire_superseded_runtime_images(plan, final_id, runner=runner)
    elif plan.has_package_contract:
        retire_superseded_package_images(plan, final_id, runner=runner)
    if plan.has_package_contract:
        retire_superseded_package_layers(plan, package_id, runner=runner)
    retire_superseded_base_images(plan, base_id, runner=runner)
    return final_tag


def preflight_image(
    plan: ImagePlan,
    *,
    runner: Runner = subprocess.run,
    git_runner: Runner = subprocess.run,
    container: str | None = None,
) -> str:
    reconcile_pending(plan)
    try:
        _inspect(plan.base_tag, runner=runner)
    except SandboxPrerequisiteError:
        raise
    except SandboxImageError as exc:
        raise SandboxImageError(
            f"--no-build cannot reuse {plan.base_tag!r}: {exc}. "
            "Run ./sc build, then retry with --no-build."
        ) from exc
    parent_id = _resolve_parent(plan, runner=runner, allow_pull=False)
    base_id = _require_labels(
        plan.base_tag, _base_expected(plan, parent_id), runner=runner
    )
    _prove_baseline(base_id, runner=runner)
    status = _read_status(plan)
    if plan.has_package_contract:
        try:
            if (
                status is None
                or status.get("native_packages") != "ready"
                or status.get("engine_ref") != plan.engine_ref
                or status.get("declaration_digest") != plan.declaration_digest
                or status.get("package_digest") != plan.package_digest
                or status.get("parent_id") != parent_id
                or status.get("engine_base_id") != base_id
            ):
                raise SandboxImageError("current package capability status is absent or stale")
            context_digest = "none"
            if plan.extends_base:
                context_digest, _archive, _dockerfile = _tracked_context(
                    plan, git_runner=git_runner
                )
            if status.get("context_digest") != context_digest:
                raise SandboxImageError("tracked extension context differs from current evidence")
            selected_tag = str(status.get("selected_tag") or "")
            if selected_tag != plan.runtime_tag:
                raise SandboxImageError("selected package tag differs from the declared candidate")
            package_id = status.get("package_layer_id")
            if not isinstance(package_id, str) or not package_id.startswith("sha256:"):
                raise SandboxImageError("package layer image ID is absent from current evidence")
            expected = _runtime_expected(
                plan,
                image_kind="fork-extension" if plan.extends_base else "fork-packages",
                parent_id=parent_id,
                base_id=base_id,
                package_layer_id=package_id,
                context_digest=context_digest,
            )
            selected_id = _require_labels(
                selected_tag,
                expected,
                runner=runner,
            )
            if selected_id != status.get("selected_image_id"):
                raise SandboxImageError("selected package image ID differs from current evidence")
            proof, digest, _command = _prove_packages(
                selected_id, plan.packages, runner=runner
            )
            if digest != status.get("proof_digest") or proof != status.get("observed"):
                raise SandboxImageError("selected package proof differs from current evidence")
            readiness_state = readiness(
                plan,
                container,
                runner=runner,
                git_runner=git_runner,
            )
            if readiness_state.get("state") != "ready":
                raise SandboxImageError(
                    readiness_state.get("reason")
                    or "current package capability receipt is absent or stale"
                )
            return selected_tag
        except SandboxPrerequisiteError:
            raise
        except SandboxImageError as exc:
            return _advisory_fallback(
                plan,
                base_id=base_id,
                parent_id=parent_id,
                container=container,
                classification="stale_no_build",
                detail=str(exc),
                image_ids={"parent": parent_id, "engine_base": base_id},
                evidence_path=str(_status_path(plan)[0]),
                runner=runner,
            )
    try:
        expected = plan.runtime_labels
        if plan.extends_base:
            context_digest, _archive, _dockerfile = _tracked_context(
                plan, git_runner=git_runner
            )
            expected = _runtime_expected(
                plan,
                image_kind="fork-extension",
                parent_id=parent_id,
                base_id=base_id,
                package_layer_id="none",
                context_digest=context_digest,
            )
        _require_labels(plan.runtime_tag, expected, runner=runner)
    except SandboxPrerequisiteError:
        raise
    except SandboxImageError as exc:
        raise SandboxImageError(
            f"--no-build cannot reuse {plan.runtime_tag!r}: {exc}. "
            "Run ./sc build, then retry with --no-build."
        ) from exc
    return plan.runtime_tag


def volume_plans(plan: ImagePlan) -> tuple[VolumePlan, ...]:
    if plan.declaration is None or plan.declaration.sandbox is None:
        return ()
    result = []
    for mount in plan.declaration.sandbox.mounts:
        target_digest = _sha256_text(mount.target_declared)
        name = (
            f"{IMAGE_PREFIX}-devkit-{plan.installation_identity[:20]}-"
            f"{mount.name}-{target_digest[:10]}"
        )
        labels = {
            "sc.resource_kind": "devkit-volume",
            "sc.fork_identity": plan.installation_identity,
            "sc.mount_name": mount.name,
            "sc.mount_target": mount.target_declared,
        }
        result.append(VolumePlan(mount.name, name, mount.target, labels))
    return tuple(result)


def _inspect_volume(name: str, *, runner: Runner) -> dict[str, Any] | None:
    try:
        done = runner(
            ("docker", "volume", "inspect", name),
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(f"cannot run docker: {exc}") from exc
    if done.returncode != 0:
        return None
    try:
        item = json.loads(done.stdout)[0]
        labels = item.get("Labels") or {}
    except (json.JSONDecodeError, IndexError, TypeError, AttributeError) as exc:
        raise SandboxImageError(f"docker returned invalid volume inspection for {name}") from exc
    if not isinstance(labels, dict):
        raise SandboxImageError(f"docker returned invalid volume labels for {name}")
    return {"labels": labels}


def ensure_volumes(
    plan: ImagePlan, *, runner: Runner = subprocess.run
) -> tuple[VolumePlan, ...]:
    volumes = volume_plans(plan)
    for volume in volumes:
        inspected = _inspect_volume(volume.volume_name, runner=runner)
        if inspected is None:
            command = ["docker", "volume", "create"]
            command.extend(_label_arguments(volume.labels))
            command.append(volume.volume_name)
            _run(command, runner=runner)
            inspected = _inspect_volume(volume.volume_name, runner=runner)
        if inspected is None:
            raise SandboxImageError(
                f"Docker did not create declared volume {volume.volume_name!r}"
            )
        actual = inspected["labels"]
        mismatches = [
            key for key, value in volume.labels.items() if actual.get(key) != value
        ]
        if mismatches:
            detail = ", ".join(
                f"{key}={actual.get(key)!r} (need {volume.labels[key]!r})"
                for key in mismatches
            )
            raise SandboxImageError(
                f"declared volume {volume.volume_name!r} is foreign: {detail}"
            )
        _run(
            (
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "chown",
                "--mount",
                f"type=volume,src={volume.volume_name},dst=/sc-devkit-volume",
                plan.runtime_tag,
                f"{plan.uid}:{plan.gid}",
                "/sc-devkit-volume",
            ),
            runner=runner,
        )
    return volumes


def _mount_value(value: str) -> str:
    if not any(character in value for character in ',"\\'):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def home_bind_paths(home: str | os.PathLike[str] | None = None) -> tuple[Path, ...]:
    """Absolute host paths of every home bind mount the launcher makes."""
    base = Path(home if home is not None else os.environ.get("HOME") or Path.home())
    return tuple(base / relative for relative in HOME_BIND_MOUNTS)


def _owner_state_home(environ: dict[str, str] | None = None) -> Path:
    """The owner's XDG state home, from the environment given."""
    env = os.environ if environ is None else environ
    configured = (env.get("XDG_STATE_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    base = (env.get("HOME") or "").strip()
    return (Path(base) if base else Path.home()) / ".local" / "state"


def install_key(repo_root: str | os.PathLike[str]) -> str:
    """`<repo name>-<hash of the checkout path>`: this install's name in the
    owner's state home. From the checkout's own path, never a file in it."""
    root = Path(repo_root)
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", root.name) or "repo"
    return f"{name}-{key}"


def engine_ops_state_dir(
    repo_root: str | os.PathLike[str],
    environ: dict[str, str] | None = None,
) -> Path:
    """Where the engine-ops service of the install at `repo_root` keeps its
    host-private state (api/engine_ops.py), the sign-in credential store
    (api/host_auth.py) included. A pure path: nothing is created.

    Named from the checkout's own path under the owner's state home, so no
    file in the checkout takes part in choosing it.
    """
    return (_owner_state_home(environ) / "subfloor" / "engine-ops"
            / install_key(repo_root))


def auth_sessions_dir(
    repo_root: str | os.PathLike[str],
    environ: dict[str, str] | None = None,
) -> Path:
    """Where the host auth authority publishes the session-hash list of the
    install at `repo_root` for the review server (api/auth_sessions.py).

    A sibling of the engine-ops state collection, never inside it: this
    directory is the ONE thing of the authority's that is mounted into the
    sandbox (read-only, at AUTH_SESSIONS_TARGET), and it holds no secret.
    A pure path: nothing is created.
    """
    return (_owner_state_home(environ) / "subfloor" / "auth-sessions"
            / install_key(repo_root))


def paths_overlap(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    """Whether two paths, links resolved, are the same or one contains the other."""
    a = Path(os.path.realpath(first))
    b = Path(os.path.realpath(second))
    return a == b or a in b.parents or b in a.parents


# ── The launcher's `docker run` grammar ───────────────────────────────────────
# Every option the launcher passes to `docker run`, and whether it takes a
# value. This is a grammar for ONE caller family — the dispatcher's sandbox
# launch, the GitHub-auth expansion (sandbox_github_auth.py), the resource
# arguments (sandbox_resources.py), and the mounts added in `docker_run` — not
# an emulation of Docker's command line. The mount guard walks the option
# region with it, so an option it does not know cannot be skipped over:
# it is refused. A launcher change that needs a new option adds it here, and
# tests/test_sandbox_devkit_images.py fails until it does.
#
# Only these exact spellings are accepted. `--opt=value`, attached short
# values (`-vSRC:DST`), short-flag clusters (`-dv`), and `--` are all refused:
# the launcher emits none of them, and each is a way for an argument to mean
# something to Docker that it does not mean here.
RUN_OPTIONS: dict[str, bool] = {
    "-d": False,
    "--init": False,
    "--name": True,
    "--restart": True,
    "--network": True,
    "--user": True,
    "-e": True,
    "-w": True,
    "-p": True,
    "--tmpfs": True,
    "--memory": True,
    "--memory-swap": True,
    "-v": True,
    "--mount": True,
}
# `--mount` types Docker knows. Only `bind` names a host path.
_MOUNT_TYPES = frozenset({"bind", "volume", "tmpfs", "npipe", "cluster", "image"})
_MOUNT_SOURCE_KEYS = ("source", "src")
_MOUNT_TARGET_KEYS = ("target", "dst", "destination")
_MOUNT_BARE_KEYS = frozenset({
    "readonly", "ro", "volume-nocopy", "bind-nonrecursive", "bind-create-src",
    "tmpfs-noexec",
})
_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _refuse(reason: str) -> SandboxImageError:
    return SandboxImageError(
        f"refusing to launch: {reason}, so the mounts cannot be checked "
        "against the engine-ops service's private state directory."
    )


def _host_path(source: str, cwd: str | os.PathLike[str], what: str) -> str:
    """A bind source as an absolute host path; relative ones are against `cwd`."""
    if not source or source != source.strip() or any(ord(c) < 32 for c in source):
        raise _refuse(f"{what} has an empty, padded, or unprintable source")
    path = Path(source)
    if not path.is_absolute():
        path = Path(cwd) / path
    return os.path.normpath(path)


def _truthy(content: str) -> bool:
    return content.strip().lower() in ("true", "1")


def _mount_bind(
    value: str, cwd: str | os.PathLike[str],
) -> tuple[str, bool, str] | None:
    """`(host path, read-only, target)` of one `--mount` value, or None when
    it binds no host path.

    Docker trims the whole value, reads it as one CSV record of `key=value`
    fields (or a few bare words), lowercases keys and the type, takes `src`
    for `source`, and defaults the type to `volume`. This does the same and
    refuses whatever it cannot read that way.
    """
    what = f"--mount {value!r}"
    value = value.strip()
    try:
        records = list(csv.reader([value], strict=True))
    except csv.Error as exc:
        raise _refuse(f"{what} is not a readable mount") from exc
    if len(records) != 1 or not records[0]:
        raise _refuse(f"{what} is not a readable mount")
    fields: dict[str, str] = {}
    bare: list[str] = []
    for field in records[0]:
        key, separator, content = field.partition("=")
        key = key.strip().lower()
        if not separator:
            bare.append(key)
        fields[key] = content          # a repeated key: the last one wins, as in Docker
    explicit = bool(fields.get("type", "").strip())
    kind = fields.get("type", "").strip().lower() or "volume"
    if kind not in _MOUNT_TYPES:
        raise _refuse(f"{what} has an unknown type")
    if explicit and kind != "bind":
        # Stated not to be a bind: it names no host path, whatever else the
        # value carries (the dev-kit volumes are spelled this way).
        return None
    # A bind, or a value that leaves the type to the default: it must read
    # cleanly. Docker takes a few options as bare words and requires a
    # target; anything else is an error there, and here.
    if any(key not in _MOUNT_BARE_KEYS for key in bare):
        raise _refuse(f"{what} has a field that is not key=value")
    if not any(fields.get(key) for key in _MOUNT_TARGET_KEYS):
        raise _refuse(f"{what} has no target")
    if kind != "bind":
        return None
    sources = [fields[key] for key in _MOUNT_SOURCE_KEYS if key in fields]
    if len(sources) != 1:
        raise _refuse(f"{what} does not name exactly one source")
    # Read-only only when it plainly says so: the bare word, or a true value.
    readonly = any(
        key in fields and (key in bare or _truthy(fields[key]))
        for key in ("readonly", "ro"))
    target = next(fields[key] for key in _MOUNT_TARGET_KEYS if fields.get(key))
    return _host_path(sources[0], cwd, what), readonly, target


def _mount_source(value: str, cwd: str | os.PathLike[str]) -> str | None:
    """The host path of one `--mount` value, or None when it binds none."""
    bind = _mount_bind(value, cwd)
    return None if bind is None else bind[0]


def _volume_bind(
    value: str, cwd: str | os.PathLike[str],
) -> tuple[str, bool, str] | None:
    """`(host path, read-only, target)` of one `-v` value, or None when it
    binds no host path.

    Docker's rule: `SRC:DST[:opts]`; a source that starts with `/` or `.` is a
    host path (a relative one is against the working directory), a source that
    is a volume name is a named volume, and a value with no `:` is an anonymous
    volume at an absolute container path. Anything else is refused.
    """
    what = f"-v {value!r}"
    parts = value.split(":")
    if len(parts) == 1:
        if not value.startswith("/") or value != value.strip():
            raise _refuse(f"{what} is neither SRC:DST nor a container path")
        return None
    source = parts[0]
    if source.startswith(("/", ".")):
        options = parts[2].split(",") if len(parts) > 2 else []
        return _host_path(source, cwd, what), "ro" in options, parts[1]
    if _VOLUME_NAME.match(source):
        return None
    raise _refuse(f"{what} has a source that is neither a path nor a volume name")


def _volume_source(value: str, cwd: str | os.PathLike[str]) -> str | None:
    """The host path of one `-v` value, or None when it binds none."""
    bind = _volume_bind(value, cwd)
    return None if bind is None else bind[0]


def run_options(arguments: Sequence[str]) -> list[tuple[str, str | None]]:
    """The options of a `docker run` argument list, read with RUN_OPTIONS.

    Walks the option region only: from the start (after a leading `docker
    run`) up to the image, which is the first argument that is not an option
    or an option's value. Everything from the image on is the container's
    command and is never inspected. An option that is not in RUN_OPTIONS, in
    exactly its spelling there, raises SandboxImageError naming it.
    """
    arguments = list(arguments)
    index = 2 if arguments[:2] == ["docker", "run"] else 0
    options: list[tuple[str, str | None]] = []
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-"):
            break                               # the image: the options end here
        if argument not in RUN_OPTIONS:
            raise _refuse(
                f"the docker run option {argument!r} is not in the launcher's "
                "option table (sandbox_devkit.RUN_OPTIONS; add it there with "
                "whether it takes a value — joined `--opt=value`, attached, "
                "and clustered spellings are not accepted)")
        index += 1
        if not RUN_OPTIONS[argument]:
            options.append((argument, None))
            continue
        if index >= len(arguments):
            raise _refuse(f"the docker run option {argument!r} has no value")
        options.append((argument, arguments[index]))
        index += 1
    return options


def bind_mounts(
    arguments: Sequence[str],
    cwd: str | os.PathLike[str] | None = None,
) -> list[tuple[str, bool, str]]:
    """Every bind mount of a `docker run` argument list, as
    `(absolute host path, read-only, container target)`.

    Read from the option region with the launcher's grammar (`run_options`).
    Raises SandboxImageError for an option outside the grammar or a mount
    value that cannot be classified.
    """
    base = os.getcwd() if cwd is None else cwd
    mounts: list[tuple[str, bool, str]] = []
    for option, value in run_options(arguments):
        if option == "-v":
            bind = _volume_bind(value, base)
        elif option == "--mount":
            bind = _mount_bind(value, base)
        else:
            continue
        if bind is not None:
            mounts.append(bind)
    return mounts


def bind_sources(
    arguments: Sequence[str],
    cwd: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Every host path a `docker run` argument list bind-mounts, absolute.

    Read-only binds count: a read still leaks. Raises SandboxImageError for an
    option outside the grammar or a mount value that cannot be classified.
    """
    return [source for source, _readonly, _target in bind_mounts(arguments, cwd)]


def guard_engine_ops_state(
    arguments: Sequence[str],
    repo_root: str | os.PathLike[str],
    environ: dict[str, str] | None = None,
) -> None:
    """Refuse a `docker run` that would put the engine-ops state in a sandbox,
    or let a sandbox write the session-hash list.

    The engine-ops service (api/engine_ops.py) decides which GUI origin may
    start `./sc update` / `restart`, and who is signed in, from files in its
    private directory, so a sandbox must be able to neither write nor read
    them. This is checked here because this is the one place that sees the
    final argument list: every bind, however it was built (the checkout, the
    instance state, interpreter mounts, the home mounts, anything added
    later), is resolved and compared with that directory in both directions.
    A mount that contains it, lies inside it, or is one of its files stops the
    launch.

    The session-hash directory (`auth_sessions_dir`) is the opposite case: it
    is meant to be in the sandbox, so that the review server there can
    recognise a signed-in browser. It may be reached by exactly one mount: the
    directory itself, read-only, at AUTH_SESSIONS_TARGET. Any other mount that
    overlaps it (a writable one, a parent, a file in it, another target) stops
    the launch: a sandbox that could write that list could sign itself in to
    the review API. And the two directories must not overlap each other.
    """
    state = engine_ops_state_dir(repo_root, environ)
    sessions = auth_sessions_dir(repo_root, environ)
    if paths_overlap(state, sessions):
        raise SandboxImageError(
            f"refusing to launch: the session-hash directory {sessions} "
            f"(→ {os.path.realpath(sessions)}) overlaps the engine-ops "
            f"service's private state directory {state} "
            f"(→ {os.path.realpath(state)}). The first is mounted into the "
            "sandbox and the second must never be."
        )
    for source, readonly, target in bind_mounts(arguments):
        if paths_overlap(source, state):
            raise SandboxImageError(
                f"refusing to launch: the mount {source} "
                f"(→ {os.path.realpath(source)}) overlaps the engine-ops "
                f"service's private state directory {state} "
                f"(→ {os.path.realpath(state)}). That directory holds the GUI "
                "origin allowed to start engine update/restart and the "
                "operator's sign-in credentials, and must stay "
                "out of the sandbox. Move the mounted path, or set "
                "XDG_STATE_HOME to a directory outside every sandbox mount."
            )
        if not paths_overlap(source, sessions):
            continue
        exact = os.path.realpath(source) == os.path.realpath(sessions)
        if not (exact and readonly and target == AUTH_SESSIONS_TARGET):
            raise SandboxImageError(
                f"refusing to launch: the mount {source} "
                f"(→ {os.path.realpath(source)}) overlaps the session-hash "
                f"directory {sessions} (→ {os.path.realpath(sessions)}). "
                "The sandbox may see that directory only as the launcher's "
                f"own read-only mount at {AUTH_SESSIONS_TARGET}; a sandbox "
                "that could write it could sign itself in to the review API. "
                "Move the mounted path, or set XDG_STATE_HOME to a directory "
                "outside every sandbox mount."
            )


def ensure_auth_sessions_dir(
    repo_root: str | os.PathLike[str],
    environ: dict[str, str] | None = None,
) -> Path:
    """Create the session-hash directory (owner-only) and return it.

    Docker creates a missing bind source itself, as root; the launcher makes
    it first so that it is the operator's. Each level it creates is 0700, and
    the leaf must be a real directory owned by this user.
    """
    target = auth_sessions_dir(repo_root, environ)
    home = _owner_state_home(environ)
    try:
        home.mkdir(parents=True, exist_ok=True)
        path = home
        for part in target.relative_to(home).parts:
            path = path / part
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
        info = os.lstat(target)
    except OSError as exc:
        raise SandboxImageError(
            f"cannot create the session-hash directory {target}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SandboxImageError(
            f"the session-hash directory {target} is not a directory owned "
            "by this user")
    if info.st_mode & 0o077:
        os.chmod(target, 0o700)
    return target


def auth_sessions_mount(
    repo_root: str | os.PathLike[str],
    environ: dict[str, str] | None = None,
) -> list[str]:
    """The one mount that shows the session-hash directory to the sandbox.

    The source is written as one CSV field, quoted whole when it holds a comma
    or a quote, which is how Docker and this module's own reader take it.
    """
    source = f"src={ensure_auth_sessions_dir(repo_root, environ)}"
    if any(character in source for character in ',"'):
        source = '"' + source.replace('"', '""') + '"'
    return ["--mount",
            f"type=bind,{source},dst={AUTH_SESSIONS_TARGET},readonly"]


def expand_home_mounts(arguments: Sequence[str]) -> list[str]:
    """Replace HOME_MOUNT_MARKER with one `-v path:path` per home bind mount."""
    expanded: list[str] = []
    for argument in arguments:
        if argument != HOME_MOUNT_MARKER:
            expanded.append(argument)
            continue
        for path in home_bind_paths():
            expanded.extend(("-v", f"{path}:{path}"))
    return expanded


def docker_run(
    plan: ImagePlan,
    arguments: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    memory_policy: sandbox_resources.MemoryPolicy | None = None,
) -> None:
    if arguments.count(MOUNT_MARKER) != 1:
        raise SandboxImageError(
            f"docker-run arguments must contain one {MOUNT_MARKER} marker"
        )
    arguments = expand_home_mounts(arguments)
    volumes = ensure_volumes(plan, runner=runner)
    marker = arguments.index(MOUNT_MARKER)
    mount_arguments = []
    for volume in volumes:
        mount_arguments.extend(
            (
                "--mount",
                (
                    "type=volume,src="
                    f"{volume.volume_name},dst={_mount_value(str(volume.target))}"
                ),
            )
        )
    if memory_policy is None:
        resource_arguments, memory_policy = sandbox_resources.docker_arguments(
            plan.engine / "instance.json",
            runner=runner,
        )
    else:
        value = str(memory_policy.bytes)
        resource_arguments = ("--memory", value, "--memory-swap", value)
    command = (
        "docker",
        "run",
        *arguments[:marker],
        *resource_arguments,
        *mount_arguments,
        # The session-hash list the in-sandbox review server verifies browser
        # sessions against: read-only, and the only thing of the host auth
        # authority's that the sandbox sees.
        *auth_sessions_mount(plan.engine.parent),
        *arguments[marker + 1 :],
    )
    # The final argument list, every mount in it: nothing reaches a sandbox
    # that overlaps the engine-ops service's private state, and the
    # session-hash directory is reached only by the read-only mount above.
    guard_engine_ops_state(command, plan.engine.parent)
    # `docker run -d` prints only the container ID.  Suppress that transport
    # detail here so callers can leave provisioning output visible.
    _run(command, runner=runner, capture=True)
    print(
        "sandbox resources: memory "
        f"{sandbox_resources.human_size(memory_policy.bytes)} hard limit; swap disabled"
    )


def enforce_container_resources(
    plan: ImagePlan,
    container: str,
    *,
    runner: Runner = subprocess.run,
) -> sandbox_resources.MemoryPolicy:
    """Apply current policy to a preserved container without recreating it."""
    policy = sandbox_resources.resolve_memory(
        plan.engine / "instance.json",
        runner=runner,
    )
    value = str(policy.bytes)
    _run(
        (
            "docker",
            "update",
            "--memory",
            value,
            "--memory-swap",
            value,
            container,
        ),
        runner=runner,
        capture=True,
    )
    print(
        "sandbox resources: memory "
        f"{sandbox_resources.human_size(policy.bytes)} hard limit; swap disabled"
    )
    return policy


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_root(
    plan: ImagePlan, *, git_runner: Runner = subprocess.run
) -> tuple[Path, str]:
    checkout_identity = _sha256_text(str(plan.checkout))
    base = plan.checkout / ".sc-state" / "local" / "dev-kit"
    resolved = base.resolve(strict=False)
    if resolved != plan.checkout and plan.checkout not in resolved.parents:
        raise SandboxImageError(
            f"local dev-kit artifact root escapes the checkout: {resolved}"
        )
    try:
        ignored = git_runner(
            (
                "git",
                "-C",
                str(plan.checkout),
                "check-ignore",
                "-q",
                "--no-index",
                str(base),
            ),
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(
            f"cannot run git to verify local artifact ignore policy: {exc}"
        ) from exc
    if ignored.returncode != 0:
        raise SandboxImageError(
            f"local dev-kit artifact root is not ignored: {base}; restore the "
            "managed /.sc-state/local/ rule before provisioning"
        )
    return base / checkout_identity[:20], checkout_identity


def _input_digest(path: Path, field: str) -> str:
    return _digest_file(path, field)


def _provision_contract(
    declaration: Declaration, checkout: Path
) -> dict[str, Any] | None:
    if declaration.provision is None:
        return None
    provision = declaration.provision
    hook = declaration.hooks[provision.hook]
    inputs = [
        {
            "path": declared,
            "sha256": _input_digest(path, f"provision.inputs[{index}]"),
        }
        for index, (declared, path) in enumerate(
            zip(provision.inputs_declared, provision.inputs)
        )
    ]
    automatic_inputs = [
        {
            "path": str(declaration.path.relative_to(checkout)),
            "sha256": _input_digest(declaration.path, "dev-kit declaration"),
        }
    ]
    if hook.resolved_executable is not None:
        automatic_inputs.append(
            {
                "path": str(hook.resolved_executable.relative_to(checkout)),
                "sha256": _input_digest(
                    hook.resolved_executable, "provision hook executable"
                ),
            }
        )
    return {
        "name": hook.name,
        "argv": list(hook.argv),
        "cwd": hook.cwd_declared,
        "canonical_cwd": str(hook.cwd),
        "inputs": inputs,
        "automatic_inputs": automatic_inputs,
    }


def _capability_payload(
    plan: ImagePlan,
    *,
    seat: str,
    container: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    if plan.declaration is None:
        raise SandboxImageError("no fork readiness declared")
    status = _read_status(plan)
    if status is None and not plan.has_package_contract:
        status = {
            "core_runtime": "ready",
            "selected_tag": plan.runtime_tag,
            "parent_id": "legacy",
            "engine_base_id": "legacy",
            "package_layer_id": "none",
            "context_digest": "legacy",
        }
    if status is None or status.get("core_runtime") != "ready":
        raise SandboxImageError("current lifecycle status is absent or stale")
    tag = str(status.get("selected_tag") or plan.runtime_tag)
    tagged_image = _inspect(tag, runner=runner)
    image = tagged_image
    if container is not None:
        container_image_id = _run(
            ("docker", "inspect", "--format", "{{.Image}}", container),
            runner=runner,
            capture=True,
        )
        if not container_image_id.startswith("sha256:"):
            raise SandboxImageError(
                f"docker returned invalid image identity for container {container!r}"
            )
        if container_image_id != tagged_image["id"]:
            raise SandboxImageError(
                f"container {container!r} runs {container_image_id}, but current "
                f"image {tag!r} is {tagged_image['id']}"
            )
        image = _inspect(container_image_id, runner=runner)
    labels = image["labels"]
    expected = dict(plan.runtime_labels)
    if tag == plan.base_tag:
        expected = plan.base_labels
    mismatches = [key for key, value in expected.items() if labels.get(key) != value]
    if mismatches:
        raise SandboxImageError(
            f"cannot prove readiness with stale or foreign image {tag!r}: "
            + ", ".join(mismatches)
        )
    provision_payload = _provision_contract(plan.declaration, plan.checkout)
    commit, clean = _source_commit(plan)
    proof = _read_receipt(_status_path(plan)[0].with_name("package-proof.json")) or {}
    return {
        "format_version": READINESS_CONTRACT_VERSION,
        "seat": seat,
        "checkout": str(plan.checkout),
        "checkout_identity": _sha256_text(str(plan.checkout)),
        "source_commit": commit,
        "source_tracked_clean": clean,
        "declaration_digest": plan.declaration_digest,
        "contracts": {
            "readiness": READINESS_CONTRACT_VERSION,
            "packages": PACKAGE_CONTRACT_VERSION,
            "context": CONTEXT_CONTRACT_VERSION,
        },
        "image": {
            "id": image["id"],
            "labels": {key: labels[key] for key in sorted(labels)},
            "tag": tag,
            "parent_id": status.get("parent_id", "none"),
            "engine_base_id": status.get("engine_base_id", "none"),
            "package_layer_id": status.get("package_layer_id", "none"),
        },
        "packages": {
            "contract_version": PACKAGE_CONTRACT_VERSION,
            "requested": proof.get("requested", []),
            "observed": proof.get("observed", []),
            "proof_digest": proof.get("proof_digest", "none"),
        },
        "extension": {
            "dockerfile_digest": plan.dockerfile_digest,
            "context_digest": status.get("context_digest", "none"),
        },
        "provision": provision_payload,
    }


def provisioning_payload(
    plan: ImagePlan,
    *,
    seat: str,
    container: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    if plan.declaration is None or plan.declaration.provision is None:
        raise SandboxImageError("no fork provisioning declared")
    return _capability_payload(plan, seat=seat, container=container, runner=runner)


def provisioning_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def readiness_receipt_matches(
    receipt: dict[str, Any] | None, fingerprint: str
) -> bool:
    """Validate the canonical ready-receipt envelope for one fingerprint."""
    return bool(
        receipt is not None
        and receipt.get("format_version") == READINESS_CONTRACT_VERSION
        and receipt.get("state") == "ready"
        and receipt.get("fingerprint") == fingerprint
    )


def _declaration_package_digest(declaration: Declaration) -> str:
    sandbox = declaration.sandbox
    if sandbox is not None and sandbox.packages is not None:
        return _sha256_text(
            json.dumps(sandbox.packages.canonical_atoms, separators=(",", ":"))
        )
    if sandbox is not None and sandbox.package_error is not None:
        return "invalid"
    return "none"


def persisted_readiness_matches(
    checkout: Path,
    engine: Path,
    declaration: Declaration,
    status: dict[str, Any],
) -> bool:
    """Validate stored readiness against current tracked inputs without Docker."""
    checkout = checkout.resolve(strict=True)
    engine = engine.resolve(strict=True)
    identity = _sha256_text(str(checkout))
    declaration_digest = _sha256_text(declaration.canonical_json)
    package_digest = _declaration_package_digest(declaration)
    try:
        engine_ref = _engine_ref(checkout, engine)
        current_commit, clean = _source_commit_for(checkout)
        provision = _provision_contract(declaration, checkout)
    except SandboxImageError:
        return False
    if (
        status.get("format_version") != 1
        or status.get("checkout_identity") != identity
        or status.get("declaration_digest") != declaration_digest
        or status.get("package_digest") != package_digest
        or status.get("engine_ref") != engine_ref
        or clean is not True
    ):
        return False

    ready_path = (
        checkout
        / ".sc-state"
        / "local"
        / "dev-kit"
        / identity[:20]
        / "ready.json"
    )
    package_receipt = status.get("package_receipt")
    if not isinstance(package_receipt, dict):
        return False
    if package_receipt.get("path") != str(ready_path):
        return False
    fingerprint = package_receipt.get("fingerprint")
    if not isinstance(fingerprint, str):
        return False
    receipt = _read_receipt(ready_path)
    if not readiness_receipt_matches(receipt, fingerprint):
        return False
    assert receipt is not None
    if (
        receipt.get("checkout_identity") != identity
        or receipt.get("source_commit") != current_commit
        or receipt.get("source_tracked_clean") is not True
    ):
        return False

    image = receipt.get("image")
    packages = receipt.get("packages")
    if not isinstance(image, dict) or not isinstance(packages, dict):
        return False
    labels = image.get("labels")
    if not isinstance(labels, dict):
        return False
    expected_labels = {"sc.engine_ref": engine_ref}
    if labels.get("sc.image_kind") == "engine-base":
        try:
            expected_labels["sc.engine_dockerfile_digest"] = _digest_file(
                engine / "Dockerfile", "engine Dockerfile"
            )
        except SandboxImageError:
            return False
    else:
        sandbox = declaration.sandbox
        dockerfile_digest = (
            _digest_file(sandbox.dockerfile, "sandbox.dockerfile")
            if sandbox is not None and sandbox.dockerfile is not None
            else "none"
        )
        expected_labels.update(
            {
                "sc.declaration_digest": declaration_digest,
                "sc.dockerfile_digest": dockerfile_digest,
                "sc.package_digest": package_digest,
                "sc.readiness_contract": str(READINESS_CONTRACT_VERSION),
                "sc.package_contract": str(PACKAGE_CONTRACT_VERSION),
                "sc.context_contract": str(CONTEXT_CONTRACT_VERSION),
            }
        )
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        return False
    status_image_fields = {
        "selected_tag": ("tag", None),
        "selected_image_id": ("id", None),
        "parent_id": ("parent_id", "none"),
        "engine_base_id": ("engine_base_id", "none"),
        "package_layer_id": ("package_layer_id", "none"),
    }
    if any(
        status.get(status_key, default) != image.get(image_key)
        for status_key, (image_key, default) in status_image_fields.items()
    ):
        return False
    requested = (
        list(declaration.sandbox.packages.canonical_atoms)
        if declaration.sandbox is not None
        and declaration.sandbox.packages is not None
        else []
    )
    if (
        packages.get("contract_version") != PACKAGE_CONTRACT_VERSION
        or packages.get("requested") != requested
    ):
        return False

    dockerfile_digest = (
        _digest_file(
            declaration.sandbox.dockerfile,
            "sandbox.dockerfile",
        )
        if declaration.sandbox is not None
        and declaration.sandbox.dockerfile is not None
        else "none"
    )
    payload = {
        "format_version": READINESS_CONTRACT_VERSION,
        "seat": "docker",
        "checkout": str(checkout),
        "checkout_identity": identity,
        "source_commit": current_commit,
        "source_tracked_clean": True,
        "declaration_digest": declaration_digest,
        "contracts": {
            "readiness": READINESS_CONTRACT_VERSION,
            "packages": PACKAGE_CONTRACT_VERSION,
            "context": CONTEXT_CONTRACT_VERSION,
        },
        "image": image,
        "packages": packages,
        "extension": {
            "dockerfile_digest": dockerfile_digest,
            "context_digest": status.get("context_digest", "none"),
        },
        "provision": provision,
    }
    return readiness_receipt_matches(
        receipt, provisioning_fingerprint(payload)
    )


def _acquire_lock(path: Path, timeout: float) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise SandboxImageError(
                    f"timed out waiting {timeout:g}s for provisioning lock {path}"
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _classification(status: int) -> str:
    return {
        64: "invalid_configuration",
        78: "not_configured",
        126: "start_failure",
    }.get(status, "hook_failure")


def _record_ready_status(
    plan: ImagePlan,
    payload: dict[str, Any],
    fingerprint: str,
    receipt_path: Path,
    *,
    runner: Runner,
    container: str | None,
) -> None:
    status_payload = _read_status(plan) or {
        "core_runtime": "ready",
        "native_packages": "ready" if plan.has_package_contract else "not_declared",
        "selected_runtime": (
            "package_complete" if plan.has_package_contract else "engine_baseline"
        ),
        "selected_tag": payload["image"]["tag"],
        "selected_image_id": payload["image"]["id"],
        "cutover": "package_complete" if plan.has_package_contract else "baseline",
        "parent_id": payload["image"]["parent_id"],
        "engine_base_id": payload["image"]["engine_base_id"],
        "package_layer_id": payload["image"]["package_layer_id"],
        "context_digest": payload["extension"]["context_digest"],
    }
    status_payload["fork_readiness"] = "ready"
    status_payload["package_receipt"] = {
        "fingerprint": fingerprint,
        "path": str(receipt_path),
    }
    clearance = _maybe_clear_advisory(
        plan,
        status_payload,
        receipt_path=str(receipt_path),
        runner=runner,
        container=container,
    )
    if clearance is not None:
        status_payload["advisory"] = None
        status_payload["clearance"] = clearance
    _write_status(
        plan,
        {
            key: value
            for key, value in status_payload.items()
            if key
            not in {
                "format_version",
                "checkout_identity",
                "updated_at",
                "engine_ref",
                "declaration_digest",
                "package_digest",
            }
        },
    )


def provision_checkout(
    plan: ImagePlan,
    container: str | None,
    *,
    seat: str = "docker",
    runner: Runner = subprocess.run,
    git_runner: Runner = subprocess.run,
    lock_timeout: float = 30.0,
    emit: bool = True,
    _held_root: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    if plan.declaration is None or (
        plan.declaration.provision is None and not plan.has_package_contract
    ):
        return {"state": "not_declared"}
    if _held_root is None:
        root, checkout_identity = _artifact_root(plan, git_runner=git_runner)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_descriptor = _acquire_lock(root / "provision.lock", lock_timeout)
    else:
        root, checkout_identity = _held_root
        lock_descriptor = None
    attempts = root / "attempts"
    attempts.mkdir(mode=0o700, exist_ok=True)
    try:
        payload = _capability_payload(
            plan, seat=seat, container=container, runner=runner
        )
        if payload["source_tracked_clean"] is not True:
            raise SandboxImageError(
                "capability receipt requires the current tracked commit to be clean"
            )
        fingerprint = provisioning_fingerprint(payload)
        receipt_path = root / "ready.json"
        receipt = _read_receipt(receipt_path)
        if readiness_receipt_matches(receipt, fingerprint):
            _record_ready_status(
                plan,
                payload,
                fingerprint,
                receipt_path,
                runner=runner,
                container=container,
            )
            return {
                "state": "ready",
                "reused": True,
                "fingerprint": fingerprint,
                "receipt": str(receipt_path),
            }

        if plan.declaration.provision is None:
            ended_at = _utc_now()
            attempt_reference = "package-proof.json"
        else:
            if container is None:
                raise SandboxImageError("provisioning requires a running container")
            started_at = _utc_now()
            attempt_name = f"{time.time_ns()}-{os.getpid()}"
            log_path = attempts / f"{attempt_name}.log"
            metadata_path = attempts / f"{attempt_name}.json"
            provision = plan.declaration.provision
            hook = plan.declaration.hooks[provision.hook]
            command = (
                "docker",
                "exec",
                container,
                "python3",
                str(plan.engine / "scripts" / "devkit.py"),
                "run",
                str(plan.checkout),
                hook.name,
            )
            try:
                completed = runner(
                    command,
                    check=False,
                    text=True,
                    capture_output=True,
                )
            except OSError as exc:
                completed = subprocess.CompletedProcess(command, 126, "", str(exc))
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            log_path.write_text(
                "# stdout\n" + stdout + "\n# stderr\n" + stderr,
                encoding="utf-8",
            )
            os.chmod(log_path, 0o600)
            if emit:
                if stdout:
                    print(stdout, end="" if stdout.endswith("\n") else "\n")
                if stderr:
                    print(
                        stderr,
                        end="" if stderr.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
            status = int(completed.returncode)
            metadata = {
                "format_version": READINESS_CONTRACT_VERSION,
                "fingerprint": fingerprint,
                "started_at": started_at,
                "ended_at": _utc_now(),
                "image_id": payload["image"]["id"],
                "image_tag": payload["image"]["tag"],
                "checkout_identity": checkout_identity,
                "hook": payload["provision"],
                "status": status,
                "classification": "success" if status == 0 else _classification(status),
                "log": str(log_path.relative_to(root)),
            }
            _atomic_json(metadata_path, metadata)
            if status != 0:
                raise ProvisionFailed(
                    f"provision hook {hook.name!r} failed with status {status}; "
                    f"evidence: {metadata_path}",
                    status if 0 < status <= 255 else 1,
                )
            ended_at = metadata["ended_at"]
            attempt_reference = str(metadata_path.relative_to(root))
        receipt = {
            "format_version": READINESS_CONTRACT_VERSION,
            "state": "ready",
            "fingerprint": fingerprint,
            "created_at": ended_at,
            "source_commit": payload["source_commit"],
            "source_tracked_clean": True,
            "checkout_identity": checkout_identity,
            "image": payload["image"],
            "packages": payload["packages"],
            "provision": payload["provision"],
            "attempt": attempt_reference,
        }
        _atomic_json(receipt_path, receipt)
        _record_ready_status(
            plan,
            payload,
            fingerprint,
            receipt_path,
            runner=runner,
            container=container,
        )
        return {
            "state": "ready",
            "reused": False,
            "fingerprint": fingerprint,
            "receipt": str(receipt_path),
        }
    finally:
        if lock_descriptor is not None:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)


def _remove_container(container: str, *, runner: Runner) -> None:
    try:
        runner(
            ("docker", "rm", "-f", container),
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise SandboxPrerequisiteError(f"cannot run docker: {exc}") from exc


def launch_container(
    plan: ImagePlan,
    container: str,
    arguments: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    git_runner: Runner = subprocess.run,
    lock_timeout: float = 30.0,
    emit: bool = True,
) -> dict[str, Any]:
    # Resolve and validate host capacity before any existing container is
    # removed. A malformed or unsafe operator override must leave the healthy
    # sandbox intact rather than failing halfway through the cutover.
    memory_policy = sandbox_resources.resolve_memory(
        plan.engine / "instance.json",
        runner=runner,
    )
    status = _read_status(plan)
    if status is not None and status.get("native_packages") == "advisory":
        if (
            status.get("selected_runtime") == "existing_unchanged"
            and _container_identity(container, runner=runner)
        ):
            enforce_container_resources(plan, container, runner=runner)
            return {
                "state": "advisory",
                "preserved": True,
                "core_runtime": "ready",
                "native_packages": "advisory",
                "fork_readiness": "degraded",
            }
        baseline = replace(
            plan,
            runtime_tag=plan.base_tag,
            runtime_labels=plan.base_labels,
        )
        _remove_container(container, runner=runner)
        docker_run(
            baseline,
            arguments,
            runner=runner,
            memory_policy=memory_policy,
        )
        return {
            "state": "advisory",
            "preserved": False,
            "core_runtime": "ready",
            "native_packages": "advisory",
            "fork_readiness": "degraded",
        }
    selected_tag = str((status or {}).get("selected_tag") or plan.runtime_tag)
    selected = replace(plan, runtime_tag=selected_tag)
    if plan.declaration is None or (
        plan.declaration.provision is None and not plan.has_package_contract
    ):
        _remove_container(container, runner=runner)
        docker_run(
            selected,
            arguments,
            runner=runner,
            memory_policy=memory_policy,
        )
        return {"state": "not_declared"}
    if plan.declaration.provision is None:
        _remove_container(container, runner=runner)
        docker_run(
            selected,
            arguments,
            runner=runner,
            memory_policy=memory_policy,
        )
        return readiness(
            plan, container, runner=runner, git_runner=git_runner
        )
    root, checkout_identity = _artifact_root(plan, git_runner=git_runner)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_descriptor = _acquire_lock(root / "provision.lock", lock_timeout)
    try:
        _remove_container(container, runner=runner)
        docker_run(
            selected,
            arguments,
            runner=runner,
            memory_policy=memory_policy,
        )
        return provision_checkout(
            plan,
            container,
            runner=runner,
            git_runner=git_runner,
            emit=emit,
            _held_root=(root, checkout_identity),
        )
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def readiness(
    plan: ImagePlan,
    container: str | None = None,
    *,
    seat: str = "docker",
    runner: Runner = subprocess.run,
    git_runner: Runner = subprocess.run,
    lock_timeout: float = 30.0,
) -> dict[str, Any]:
    if plan.declaration is None or (
        plan.declaration.provision is None and not plan.has_package_contract
    ):
        return {"state": "not_declared", "ready": True}
    status = _read_status(plan)
    if status is not None and status.get("native_packages") == "advisory":
        return {
            "state": "advisory",
            "ready": True,
            "core_runtime": "ready",
            "native_packages": "advisory",
            "fork_readiness": "degraded",
            "reason": status.get("detail"),
            "advisory": status.get("advisory"),
        }
    root, _checkout_identity = _artifact_root(plan, git_runner=git_runner)
    if not root.is_dir():
        return {
            "state": "not_ready",
            "ready": False,
            "reason": "no successful provisioning receipt",
        }
    lock_descriptor = _acquire_lock(root / "provision.lock", lock_timeout)
    try:
        payload = _capability_payload(
            plan, seat=seat, container=container, runner=runner
        )
        fingerprint = provisioning_fingerprint(payload)
        receipt = _read_receipt(root / "ready.json")
        if readiness_receipt_matches(receipt, fingerprint):
            return {
                "state": "ready",
                "ready": True,
                "fingerprint": fingerprint,
                "receipt": str(root / "ready.json"),
            }
        return {
            "state": "not_ready",
            "ready": False,
            "fingerprint": fingerprint,
            "reason": "successful provisioning receipt is absent or stale",
        }
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def cleanup_owned_resources(
    engine: Path, *, runner: Runner = subprocess.run
) -> list[str]:
    engine = engine.resolve(strict=True)
    identity = _sha256_text(str(engine.parent.resolve(strict=True)))
    removed = []
    volume_list = _run(
        (
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label=sc.fork_identity={identity}",
            "--filter",
            "label=sc.resource_kind=devkit-volume",
            "--format",
            "{{.Name}}",
        ),
        runner=runner,
        capture=True,
    )
    for name in sorted(filter(None, volume_list.splitlines())):
        inspected = _inspect_volume(name, runner=runner)
        labels = inspected["labels"] if inspected is not None else {}
        if (
            labels.get("sc.fork_identity") != identity
            or labels.get("sc.resource_kind") != "devkit-volume"
        ):
            raise SandboxImageError(
                f"refusing to remove volume {name!r}: ownership labels changed"
            )
        _run(("docker", "volume", "rm", name), runner=runner)
        removed.append(f"docker volume {name}")

    owned_images: dict[str, str] = {}
    for image_kind in ("fork-extension", "fork-packages", "fork-package-layer"):
        image_list = _run(
            (
                "docker",
                "image",
                "ls",
                "--filter",
                f"label=sc.fork_identity={identity}",
                "--filter",
                f"label=sc.image_kind={image_kind}",
                "--format",
                "{{.ID}}",
            ),
            runner=runner,
            capture=True,
        )
        for image_id in filter(None, image_list.splitlines()):
            owned_images[image_id] = image_kind
    for image_id in sorted(owned_images):
        inspected = _inspect(image_id, runner=runner)
        labels = inspected["labels"]
        if (
            labels.get("sc.fork_identity") != identity
            or labels.get("sc.image_kind") != owned_images[image_id]
        ):
            raise SandboxImageError(
                f"refusing to remove image {image_id!r}: ownership labels changed"
            )
        _run(("docker", "image", "rm", image_id), runner=runner)
        removed.append(f"docker image {image_id}")
    return removed


def _arguments(
    argv: Sequence[str],
) -> tuple[str, Path, Path, str, str, str, str, tuple[str, ...]]:
    commands = {
        "image-name",
        "cutover",
        "build",
        "preflight",
        "docker-run",
        "enforce-resources",
        "launch-container",
        "provision",
        "ready",
    }
    if len(argv) < 7 or argv[0] not in commands:
        raise SandboxImageError(
            "usage: sandbox_devkit.py <image-name|cutover|build|preflight|docker-run|enforce-resources|launch-container|provision|ready> "
            "<checkout> <engine> <harness-epoch> <user> <uid> <gid> [-- ARGS...]"
        )
    return (
        argv[0],
        Path(argv[1]),
        Path(argv[2]),
        argv[3],
        argv[4],
        argv[5],
        argv[6],
        tuple(argv[7:]),
    )


def _emit_lifecycle_status(plan: ImagePlan) -> None:
    status = _read_status(plan)
    if status is None:
        return
    print(f"dev-kit core runtime: {status.get('core_runtime', 'unknown')}")
    print(f"dev-kit native packages: {status.get('native_packages', 'unknown')}")
    print(f"dev-kit fork readiness: {status.get('fork_readiness', 'unknown')}")
    print(f"dev-kit selected runtime: {status.get('selected_runtime', 'unknown')}")
    print(f"dev-kit cutover: {status.get('cutover', 'unknown')}")
    if status.get("native_packages") == "ready":
        print(f"dev-kit package layer: {status.get('package_layer_id', 'unknown')}")
        print(f"dev-kit package proof: {status.get('proof_digest', 'unknown')}")
        print(f"dev-kit package receipt: {status.get('package_receipt', 'unknown')}")
    if status.get("native_packages") == "advisory":
        print(f"dev-kit package classification: {status.get('classification', 'unknown')}")
        advisory = status.get("advisory") or {}
        print(
            "dev-kit advisory: "
            f"{advisory.get('flag_id', advisory.get('source_key', 'pending'))} "
            f"generation={advisory.get('generation', 'unknown')} "
            f"state={advisory.get('state', 'pending')}"
        )
        print(f"dev-kit evidence: {_status_path(plan)[0]}")
        print(runtime_flags.REMEDY)


def _emit_image_state(plan: ImagePlan, selected: str, action: str) -> None:
    status = _read_status(plan) or {}
    if status.get("native_packages") == "advisory":
        print(
            "dev-kit image state: advisory — "
            f"{status.get('classification', 'unknown')}; "
            f"selected {status.get('selected_runtime', 'unknown')} {selected}"
        )
        return
    print(f"dev-kit image state: ready — {action} {selected}")


def main(argv: Sequence[str]) -> int:
    command, checkout, engine, epoch, user, uid, gid, extra = _arguments(argv)
    plan = image_plan(checkout, engine, epoch, user=user, uid=uid, gid=gid)
    if command not in {
        "build",
        "preflight",
        "docker-run",
        "enforce-resources",
        "launch-container",
        "provision",
        "ready",
    } and extra:
        raise SandboxImageError(f"{command} does not accept trailing arguments")
    if command == "image-name":
        print(_selected_tag(plan))
    elif command == "cutover":
        status = _read_status(plan) or {}
        print(status.get("cutover", "unknown"))
    elif command == "build":
        if len(extra) > 1:
            raise SandboxImageError("build accepts at most one container name")
        selected = build_images(plan, container=extra[0] if extra else None)
        _emit_image_state(plan, selected, "built")
        _emit_lifecycle_status(plan)
    elif command == "preflight":
        if len(extra) > 1:
            raise SandboxImageError("preflight accepts at most one container name")
        selected = preflight_image(plan, container=extra[0] if extra else None)
        _emit_image_state(plan, selected, "current")
        _emit_lifecycle_status(plan)
    elif command == "docker-run":
        if not extra or extra[0] != "--":
            raise SandboxImageError("docker-run requires -- before Docker arguments")
        docker_run(plan, extra[1:])
    elif command == "enforce-resources":
        if len(extra) != 1:
            raise SandboxImageError("enforce-resources requires one container name")
        enforce_container_resources(plan, extra[0])
    elif command == "launch-container":
        if len(extra) < 3 or extra[1] != "--":
            raise SandboxImageError(
                "launch-container requires <container> -- <Docker arguments>"
            )
        result = launch_container(plan, extra[0], extra[2:])
        if result["state"] == "not_declared":
            print("dev-kit provision state: absent — no fork provisioning declared")
        elif result["state"] == "advisory":
            print("dev-kit provision state: skipped — native package capability is advisory")
            _emit_lifecycle_status(plan)
        elif result["reused"]:
            print(
                "dev-kit provision state: ready — current receipt "
                f"({result['fingerprint'][:12]})"
            )
        else:
            print(
                "dev-kit provision state: ready — receipt written "
                f"({result['fingerprint'][:12]})"
            )
    elif command == "provision":
        if len(extra) != 1:
            raise SandboxImageError("provision requires one container name")
        result = provision_checkout(plan, extra[0])
        if result["state"] == "not_declared":
            print("dev-kit provision state: absent — no fork provisioning declared")
        elif result["reused"]:
            print(
                "dev-kit provision state: ready — current receipt "
                f"({result['fingerprint'][:12]})"
            )
        else:
            print(
                "dev-kit provision state: ready — receipt written "
                f"({result['fingerprint'][:12]})"
            )
    else:
        if len(extra) != 1:
            raise SandboxImageError("ready requires one container name")
        result = readiness(plan, extra[0])
        if result["state"] == "not_declared":
            print("dev-kit provision state: absent — no fork provisioning declared")
        elif result["state"] == "advisory":
            print("dev-kit provision state: skipped — native package capability is advisory")
            _emit_lifecycle_status(plan)
        elif result["ready"]:
            print(
                "dev-kit provision state: ready — current receipt "
                f"({result['fingerprint'][:12]})"
            )
        else:
            print(
                "dev-kit provision state: stale — " + result["reason"],
                file=sys.stderr,
            )
            return 1
    return 0


def cli(argv: Sequence[str]) -> int:
    try:
        return main(argv)
    except ProvisionFailed as exc:
        print(f"dev-kit provision state: failed — {exc}", file=sys.stderr)
        return exc.status
    except (OSError, SandboxPrerequisiteError) as exc:
        print(f"dev-kit prerequisite error — {exc}", file=sys.stderr)
        return 1
    except SandboxImageError as exc:
        print(f"dev-kit state: invalid — {exc}", file=sys.stderr)
        return 1
    except sandbox_resources.SandboxResourceError as exc:
        print(f"dev-kit state: invalid — sandbox resources: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(cli, sys.argv[1:]))
