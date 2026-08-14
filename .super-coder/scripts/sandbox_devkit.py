"""Build and validate engine-base and fork-extension sandbox images.

The engine owns the base image and image identity.  A fork may supply only the
declared Dockerfile/context that extends the exact local base image.  This
module deliberately knows nothing about the packages in that Dockerfile.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devkit import Declaration, DevkitConfigError, load_declaration

IMAGE_PREFIX = "super-coder"
BASE_IMAGE_HISTORY = 2
HEX_REF = re.compile(r"\A[0-9a-f]{40,64}\Z")
SAFE_EPOCH = re.compile(r"\A[0-9A-Za-z_.:-]+\Z")
BASE_LABELS = (
    "sc.image_kind",
    "sc.engine_ref",
    "sc.harness_epoch",
    "sc.engine_dockerfile_digest",
    "sc.build_identity",
)
EXTENSION_LABELS = (
    "sc.image_kind",
    "sc.engine_ref",
    "sc.harness_epoch",
    "sc.declaration_digest",
    "sc.fork_identity",
    "sc.dockerfile_digest",
)
MOUNT_MARKER = "SC_DEVKIT_MOUNTS"
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
    declaration_digest: str
    dockerfile_digest: str
    base_tag: str
    runtime_tag: str
    base_labels: dict[str, str]
    runtime_labels: dict[str, str]
    user: str
    uid: str
    gid: str

    @property
    def extends_base(self) -> bool:
        return self.declaration is not None and self.declaration.sandbox is not None


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
    final_index, final_from = from_positions[-1]
    if min(arg_positions) > final_index:
        raise SandboxImageError(
            "sandbox.dockerfile: ARG SC_BASE_IMAGE must precede the final FROM"
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
    if declaration is not None and declaration.sandbox is not None:
        _validate_extension_dockerfile(declaration.sandbox.dockerfile)

    engine_ref = _engine_ref(checkout, engine)
    base_dockerfile_digest = _digest_file(engine / "Dockerfile", "engine Dockerfile")
    install_identity = _sha256_text(str(engine.parent.resolve(strict=True)))
    declaration_digest = (
        _sha256_text(declaration.canonical_json) if declaration is not None else "absent"
    )
    dockerfile_digest = (
        _digest_file(declaration.sandbox.dockerfile, "sandbox.dockerfile")
        if declaration is not None and declaration.sandbox is not None
        else "none"
    )
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
            ],
            separators=(",", ":"),
        )
    )
    base_tag = f"{IMAGE_PREFIX}-base:{base_key[:20]}"
    runtime_tag = (
        f"{IMAGE_PREFIX}-sandbox-{install_identity[:20]}:latest"
        if declaration is not None and declaration.sandbox is not None
        else base_tag
    )
    base_labels = {
        "sc.image_kind": "engine-base",
        "sc.engine_ref": engine_ref,
        "sc.harness_epoch": harness_epoch,
        "sc.engine_dockerfile_digest": base_dockerfile_digest,
        "sc.build_identity": build_identity,
    }
    runtime_labels = (
        {
            "sc.image_kind": "fork-extension",
            "sc.engine_ref": engine_ref,
            "sc.harness_epoch": harness_epoch,
            "sc.declaration_digest": declaration_digest,
            "sc.fork_identity": install_identity,
            "sc.dockerfile_digest": dockerfile_digest,
        }
        if declaration is not None and declaration.sandbox is not None
        else base_labels
    )
    return ImagePlan(
        checkout=checkout,
        engine=engine,
        declaration=declaration,
        engine_ref=engine_ref,
        harness_epoch=harness_epoch,
        installation_identity=install_identity,
        declaration_digest=declaration_digest,
        dockerfile_digest=dockerfile_digest,
        base_tag=base_tag,
        runtime_tag=runtime_tag,
        base_labels=base_labels,
        runtime_labels=runtime_labels,
        user=user,
        uid=uid,
        gid=gid,
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


def build_images(plan: ImagePlan, *, runner: Runner = subprocess.run) -> str:
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
        f"SC_GITHUB_HOST_TRUST_B64={trust_b64}",
        "--build-arg",
        f"SC_GITHUB_HOST_TRUST_SHA256={trust_digest}",
        *_label_arguments(plan.base_labels),
        str(plan.checkout),
    ]
    _run(base_command, runner=runner)
    base_id = _require_labels(plan.base_tag, plan.base_labels, runner=runner)
    if not plan.extends_base:
        retire_superseded_base_images(plan, base_id, runner=runner)
        return plan.base_tag

    assert plan.declaration is not None
    assert plan.declaration.sandbox is not None
    sandbox = plan.declaration.sandbox
    extension_command = [
        "docker",
        "build",
        "-t",
        plan.runtime_tag,
        "-f",
        str(sandbox.dockerfile),
        "--build-arg",
        f"SC_BASE_IMAGE={base_id}",
        *_label_arguments(plan.runtime_labels),
        str(sandbox.context),
    ]
    _run(extension_command, runner=runner)
    runtime_id = _require_labels(plan.runtime_tag, plan.runtime_labels, runner=runner)
    # Retire extension children before their now-unreferenced base parents.
    retire_superseded_runtime_images(plan, runtime_id, runner=runner)
    retire_superseded_base_images(plan, base_id, runner=runner)
    return plan.runtime_tag


def preflight_image(plan: ImagePlan, *, runner: Runner = subprocess.run) -> str:
    try:
        _require_labels(plan.runtime_tag, plan.runtime_labels, runner=runner)
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


def docker_run(
    plan: ImagePlan,
    arguments: Sequence[str],
    *,
    runner: Runner = subprocess.run,
) -> None:
    if arguments.count(MOUNT_MARKER) != 1:
        raise SandboxImageError(
            f"docker-run arguments must contain one {MOUNT_MARKER} marker"
        )
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
    command = (
        "docker",
        "run",
        *arguments[:marker],
        *mount_arguments,
        *arguments[marker + 1 :],
    )
    # `docker run -d` prints only the container ID.  Suppress that transport
    # detail here so callers can leave provisioning output visible.
    _run(command, runner=runner, capture=True)


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


def provisioning_payload(
    plan: ImagePlan,
    *,
    seat: str,
    container: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    if plan.declaration is None or plan.declaration.provision is None:
        raise SandboxImageError("no fork provisioning declared")
    provision = plan.declaration.provision
    hook = plan.declaration.hooks[provision.hook]
    tagged_image = _inspect(plan.runtime_tag, runner=runner)
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
                f"image {plan.runtime_tag!r} is {tagged_image['id']}"
            )
        image = _inspect(container_image_id, runner=runner)
    labels = image["labels"]
    mismatches = [
        key
        for key, value in plan.runtime_labels.items()
        if labels.get(key) != value
    ]
    if mismatches:
        raise SandboxImageError(
            f"cannot provision with stale or foreign image {plan.runtime_tag!r}: "
            + ", ".join(mismatches)
        )
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
            "path": str(plan.declaration.path.relative_to(plan.checkout)),
            "sha256": _input_digest(plan.declaration.path, "dev-kit declaration"),
        }
    ]
    if hook.resolved_executable is not None:
        automatic_inputs.append(
            {
                "path": str(hook.resolved_executable.relative_to(plan.checkout)),
                "sha256": _input_digest(
                    hook.resolved_executable, "provision hook executable"
                ),
            }
        )
    return {
        "format_version": 1,
        "declaration": plan.declaration.canonical_json,
        "image": {
            "id": image["id"],
            "labels": {key: labels[key] for key in sorted(plan.runtime_labels)},
            "tag": plan.runtime_tag,
        },
        "seat": seat,
        "checkout": str(plan.checkout),
        "checkout_identity": _sha256_text(str(plan.checkout)),
        "hook": {
            "name": hook.name,
            "argv": list(hook.argv),
            "cwd": hook.cwd_declared,
            "canonical_cwd": str(hook.cwd),
        },
        "inputs": inputs,
        "automatic_inputs": automatic_inputs,
    }


def provisioning_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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


def provision_checkout(
    plan: ImagePlan,
    container: str,
    *,
    seat: str = "docker",
    runner: Runner = subprocess.run,
    git_runner: Runner = subprocess.run,
    lock_timeout: float = 30.0,
    emit: bool = True,
    _held_root: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    if plan.declaration is None or plan.declaration.provision is None:
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
        payload = provisioning_payload(
            plan, seat=seat, container=container, runner=runner
        )
        fingerprint = provisioning_fingerprint(payload)
        receipt_path = root / "ready.json"
        receipt = _read_receipt(receipt_path)
        if (
            receipt is not None
            and receipt.get("state") == "ready"
            and receipt.get("fingerprint") == fingerprint
        ):
            return {
                "state": "ready",
                "reused": True,
                "fingerprint": fingerprint,
                "receipt": str(receipt_path),
            }

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
            "format_version": 1,
            "fingerprint": fingerprint,
            "started_at": started_at,
            "ended_at": _utc_now(),
            "image_id": payload["image"]["id"],
            "image_tag": plan.runtime_tag,
            "checkout_identity": checkout_identity,
            "hook": payload["hook"],
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
        receipt = {
            "format_version": 1,
            "state": "ready",
            "fingerprint": fingerprint,
            "created_at": metadata["ended_at"],
            "attempt": str(metadata_path.relative_to(root)),
            "image_id": payload["image"]["id"],
            "image_labels": payload["image"]["labels"],
            "checkout_identity": checkout_identity,
        }
        _atomic_json(receipt_path, receipt)
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
    if plan.declaration is None or plan.declaration.provision is None:
        _remove_container(container, runner=runner)
        docker_run(plan, arguments, runner=runner)
        return {"state": "not_declared"}
    root, checkout_identity = _artifact_root(plan, git_runner=git_runner)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_descriptor = _acquire_lock(root / "provision.lock", lock_timeout)
    try:
        _remove_container(container, runner=runner)
        docker_run(plan, arguments, runner=runner)
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
    if plan.declaration is None or plan.declaration.provision is None:
        return {"state": "not_declared", "ready": True}
    root, _checkout_identity = _artifact_root(plan, git_runner=git_runner)
    if not root.is_dir():
        return {
            "state": "not_ready",
            "ready": False,
            "reason": "no successful provisioning receipt",
        }
    lock_descriptor = _acquire_lock(root / "provision.lock", lock_timeout)
    try:
        payload = provisioning_payload(
            plan, seat=seat, container=container, runner=runner
        )
        fingerprint = provisioning_fingerprint(payload)
        receipt = _read_receipt(root / "ready.json")
        if (
            receipt is not None
            and receipt.get("state") == "ready"
            and receipt.get("fingerprint") == fingerprint
        ):
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

    image_list = _run(
        (
            "docker",
            "image",
            "ls",
            "--filter",
            f"label=sc.fork_identity={identity}",
            "--filter",
            "label=sc.image_kind=fork-extension",
            "--format",
            "{{.ID}}",
        ),
        runner=runner,
        capture=True,
    )
    for image_id in sorted(set(filter(None, image_list.splitlines()))):
        inspected = _inspect(image_id, runner=runner)
        labels = inspected["labels"]
        if (
            labels.get("sc.fork_identity") != identity
            or labels.get("sc.image_kind") != "fork-extension"
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
        "build",
        "preflight",
        "docker-run",
        "launch-container",
        "provision",
        "ready",
    }
    if len(argv) < 7 or argv[0] not in commands:
        raise SandboxImageError(
            "usage: sandbox_devkit.py <image-name|build|preflight|docker-run|launch-container|provision|ready> "
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


def main(argv: Sequence[str]) -> int:
    command, checkout, engine, epoch, user, uid, gid, extra = _arguments(argv)
    plan = image_plan(checkout, engine, epoch, user=user, uid=uid, gid=gid)
    if command not in {"docker-run", "launch-container", "provision", "ready"} and extra:
        raise SandboxImageError(f"{command} does not accept trailing arguments")
    if command == "image-name":
        print(plan.runtime_tag)
    elif command == "build":
        build_images(plan)
        print(f"dev-kit image state: ready — built {plan.runtime_tag}")
    elif command == "preflight":
        preflight_image(plan)
        print(f"dev-kit image state: ready — current {plan.runtime_tag}")
    elif command == "docker-run":
        if not extra or extra[0] != "--":
            raise SandboxImageError("docker-run requires -- before Docker arguments")
        docker_run(plan, extra[1:])
    elif command == "launch-container":
        if len(extra) < 3 or extra[1] != "--":
            raise SandboxImageError(
                "launch-container requires <container> -- <Docker arguments>"
            )
        result = launch_container(plan, extra[0], extra[2:])
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


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(cli, sys.argv[1:]))
