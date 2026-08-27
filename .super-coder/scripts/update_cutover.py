#!/usr/bin/env python3
"""Pre-materialization compatibility-floor cutover for DSH removal.

The installed updater owns this module. It inspects bounded metadata from the
target Git object before target engine bytes can replace the compatibility
floor, proves the exact floor marker, captures the WAL-safe restore point,
quiesces the old runtime, and writes the receipt consumed by the target's
fresh-process cleanup hook.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
STATE_DIR = REPO_ROOT / ".sc-state"
FLOOR_DECLARATION_PATH = (
    ".super-coder/assets/dsh-removal/compatibility-floor-v1.json"
)
TARGET_MANIFEST_PATH = ".super-coder/assets/dsh-removal/removal-manifest-v1.json"
FLOOR_MARKER = STATE_DIR / "local/dsh-removal/compatibility-floor.json"
CUTOVER_RECEIPT = STATE_DIR / "local/dsh-removal/cutover-receipt.json"
CLEANUP_RECEIPT = STATE_DIR / "local/dsh-removal/cleanup-receipt.json"
FLOOR_DECLARATION_CONTRACT = "sc-dsh-compatibility-floor-declaration-v1"
FLOOR_MARKER_CONTRACT = "sc-dsh-compatibility-floor-v1"
CUTOVER_CONTRACT = "sc-dsh-removal-cutover-v1"
RECEIPT_CONTRACT = "sc-dsh-cutover-receipt-v1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CutoverError(RuntimeError):
    """A removal target cannot safely cross the compatibility boundary."""


@dataclass(frozen=True)
class CutoverPlan:
    target_ref: str
    compatibility_ref: str
    cleanup_hook: str
    manifest_sha256: str


@dataclass(frozen=True)
class PreparedCutover:
    plan: CutoverPlan
    backup_path: Path | None
    review_service: tuple[str, str] | None
    prior_runtime_state: Mapping[str, Any]


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    encoded = (json.dumps(dict(value), indent=2, sort_keys=True) + "\n").encode()
    with pending.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _git_object(ref: str, path: str, *, repo_root: Path = REPO_ROOT) -> bytes | None:
    commit = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if commit.returncode != 0:
        raise CutoverError(f"cannot inspect target ref {ref[:12]}")
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "--name-only", ref, "--", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise CutoverError(
            f"cannot inspect target cutover metadata {path} at {ref[:12]}"
        )
    if listed.stdout.strip() != path:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CutoverError(
            f"cannot read target cutover metadata {path} at {ref[:12]}"
        )
    return result.stdout


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"{label} must be a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_target(ref: str, *, repo_root: Path = REPO_ROOT) -> CutoverPlan | None:
    """Read a removal target's bounded contract without materializing it."""
    if not _SHA_RE.fullmatch(ref):
        raise CutoverError("target ref must be an exact 40-character SHA")
    raw = _git_object(ref, TARGET_MANIFEST_PATH, repo_root=repo_root)
    if raw is None:
        return None
    manifest = _json_object(raw, label="target DSH removal manifest")
    cutover = manifest.get("cutover")
    if cutover is None:
        return None
    if not isinstance(cutover, dict) or cutover.get("contract") != CUTOVER_CONTRACT:
        raise CutoverError("target DSH removal cutover contract is missing or invalid")
    if set(cutover) != {"cleanup_hook", "contract", "minimum_floor_ref"}:
        raise CutoverError("target DSH removal cutover contract has unknown fields")
    compatibility_ref = cutover.get("minimum_floor_ref")
    if not isinstance(compatibility_ref, str) or not _SHA_RE.fullmatch(
        compatibility_ref
    ):
        raise CutoverError("target DSH removal minimum_floor_ref must be an exact SHA")
    cleanup_hook = cutover.get("cleanup_hook")
    if cleanup_hook != ".super-coder/scripts/dsh_removal_cleanup.py":
        raise CutoverError("target DSH removal cleanup_hook is not the supported hook")
    return CutoverPlan(
        target_ref=ref,
        compatibility_ref=compatibility_ref,
        cleanup_hook=cleanup_hook,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


def target_declares_compatibility_floor(
    ref: str, *, repo_root: Path = REPO_ROOT
) -> bool:
    raw = _git_object(ref, FLOOR_DECLARATION_PATH, repo_root=repo_root)
    if raw is None:
        return False
    declaration = _json_object(raw, label="compatibility floor declaration")
    expected = {
        "contract": FLOOR_DECLARATION_CONTRACT,
        "fresh_process_cleanup_hook": True,
        "pre_materialization_hook": True,
        "schema_version": 1,
    }
    if declaration != expected:
        raise CutoverError("compatibility floor declaration is not exact")
    return True


def install_compatibility_marker(
    ref: str,
    *,
    repo_root: Path = REPO_ROOT,
    marker_path: Path | None = None,
) -> bool:
    """Record the first successfully published compatibility floor once."""
    marker_path = marker_path or FLOOR_MARKER
    if not target_declares_compatibility_floor(ref, repo_root=repo_root):
        return False
    if marker_path.exists():
        marker = _json_object(
            marker_path.read_bytes(), label="installed compatibility floor marker"
        )
        if (
            marker.get("contract") != FLOOR_MARKER_CONTRACT
            or not _SHA_RE.fullmatch(str(marker.get("engine_ref", "")))
            or marker.get("pre_materialization_hook") is not True
            or marker.get("fresh_process_cleanup_hook") is not True
        ):
            raise CutoverError("installed compatibility floor marker is invalid")
        return False
    _atomic_json(
        marker_path,
        {
            "contract": FLOOR_MARKER_CONTRACT,
            "engine_ref": ref,
            "fresh_process_cleanup_hook": True,
            "pre_materialization_hook": True,
        },
    )
    return True


def require_compatibility_floor(
    plan: CutoverPlan, *, marker_path: Path | None = None
) -> dict[str, Any]:
    marker_path = marker_path or FLOOR_MARKER
    try:
        marker = _json_object(
            marker_path.read_bytes(), label="installed compatibility floor marker"
        )
    except OSError as exc:
        raise CutoverError(
            "DSH removal requires compatibility floor "
            f"{plan.compatibility_ref}; update to that exact ref first. "
            "If target bytes were already overlaid, run ./sc rollback --engine-only."
        ) from exc
    expected = {
        "contract": FLOOR_MARKER_CONTRACT,
        "engine_ref": plan.compatibility_ref,
        "fresh_process_cleanup_hook": True,
        "pre_materialization_hook": True,
    }
    if marker != expected:
        raise CutoverError(
            "DSH removal compatibility marker does not match required floor "
            f"{plan.compatibility_ref}; no target state was published."
        )
    return marker


def _read_runtime_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError(f"cannot verify DSH runtime ownership record: {path}") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"DSH runtime ownership record is not an object: {path}")
    return value


def capture_generated_ownership(
    plan: CutoverPlan,
    runtime_state: Mapping[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
) -> list[dict[str, Any]]:
    """Freeze exact post-quiescence generated paths into the cutover receipt."""
    raw = _git_object(plan.target_ref, TARGET_MANIFEST_PATH, repo_root=repo_root)
    if raw is None or hashlib.sha256(raw).hexdigest() != plan.manifest_sha256:
        raise CutoverError("target removal manifest changed during cutover")
    manifest = _json_object(raw, label="target DSH removal manifest")
    rows = manifest.get("generated_artifacts")
    if not isinstance(rows, list):
        raise CutoverError("target generated_artifacts must be a list")
    engine_root = (repo_root / ".super-coder").resolve()
    captured: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise CutoverError("target generated artifact row must be an object")
        declared = row.get("path")
        kind = row.get("kind")
        if not isinstance(declared, str) or not isinstance(kind, str):
            raise CutoverError("target generated artifact path/kind is invalid")
        lexical = Path(declared)
        if lexical.is_absolute() or ".." in lexical.parts:
            raise CutoverError(f"target generated artifact path is unsafe: {declared}")
        resolved_names: list[str] = []
        if "<fork-id>" in declared:
            fork_id = runtime_state.get("fork_id")
            if isinstance(fork_id, str) and fork_id and "/" not in fork_id:
                resolved_names.append(declared.replace("<fork-id>", fork_id))
        else:
            resolved_names.append(declared)
        paths: list[dict[str, Any]] = []
        roots: list[str] = []
        directories: list[str] = []
        for relative in resolved_names:
            path = repo_root / relative
            if path.is_symlink() or not path.resolve(strict=False).is_relative_to(
                engine_root
            ):
                raise CutoverError(f"generated artifact is unsafe: {relative}")
            if not path.exists():
                continue
            if kind == "exact-file":
                if not path.is_file():
                    raise CutoverError(f"generated exact artifact is not a file: {relative}")
                paths.append(
                    {"path": relative, "sha256": _sha256_file(path), "type": "file"}
                )
                continue
            if kind not in {"bounded-tree", "marked-bounded-tree"} or not path.is_dir():
                raise CutoverError(f"generated bounded artifact shape is invalid: {relative}")
            roots.append(relative)
            for child in sorted(path.rglob("*")):
                child_relative = str(child.relative_to(repo_root))
                if child.is_symlink() or not child.resolve(strict=False).is_relative_to(
                    path.resolve()
                ):
                    raise CutoverError(
                        f"generated bounded artifact contains unsafe child: {child_relative}"
                    )
                if child.is_dir():
                    directories.append(child_relative)
                    continue
                if not child.is_file():
                    raise CutoverError(
                        f"generated bounded artifact contains special child: {child_relative}"
                    )
                paths.append(
                    {
                        "path": child_relative,
                        "sha256": _sha256_file(child),
                        "type": "file",
                    }
                )
        captured.append(
            {
                "declared_path": declared,
                "kind": kind,
                "directories": directories,
                "paths": paths,
                "roots": roots,
            }
        )
    return captured


def quiesce_dsh() -> tuple[dict[str, Any], dict[str, Any]]:
    """Close admission and stop only identities verified by the DSH owner."""
    import deepseek_web

    state_path = ENGINE / "run/deepseek-web.json"
    before = _read_runtime_state(state_path)
    try:
        with deepseek_web._service_lock():
            outcome = deepseek_web._stop_unlocked()
    except deepseek_web.DeepSeekWebError as exc:
        raise CutoverError(f"DSH quiescence refused: {exc.code}: {exc.detail}") from exc
    for field in ("service_port", "relay_port"):
        port = before.get(field)
        if isinstance(port, int) and deepseek_web._tcp_ready("127.0.0.1", port):
            raise CutoverError(f"DSH quiescence left owned port {port} reachable")
    return before, dict(outcome)


def prepare_cutover(
    plan: CutoverPlan,
    *,
    current_ref: str,
    backup: Callable[[], Path | None],
    stop_review: Callable[[], tuple[str, str] | None],
    start_review: Callable[[tuple[str, str] | None], None] | None = None,
    quiesce: Callable[[], tuple[dict[str, Any], dict[str, Any]]] = quiesce_dsh,
    receipt_path: Path | None = None,
    marker_path: Path | None = None,
    repo_root: Path = REPO_ROOT,
    capture_ownership: Callable[
        [CutoverPlan, Mapping[str, Any]], list[dict[str, Any]]
    ]
    | None = None,
) -> PreparedCutover:
    """Establish the durable pre-materialization cutover boundary."""
    receipt_path = receipt_path or CUTOVER_RECEIPT
    require_compatibility_floor(plan, marker_path=marker_path)
    if current_ref != plan.compatibility_ref:
        raise CutoverError(
            f"installed engine ref {current_ref or '<missing>'} is not required "
            f"compatibility floor {plan.compatibility_ref}"
        )
    backup_path = backup()
    if backup_path is None:
        raise CutoverError("DSH removal requires a live WAL-safe database backup")
    review_service = stop_review()
    try:
        before, outcome = quiesce()
        generated_ownership = (
            capture_ownership(plan, before)
            if capture_ownership is not None
            else capture_generated_ownership(plan, before, repo_root=repo_root)
        )
        identities = {
            key: before.get(key)
            for key in (
                "web_pid",
                "web_start_ticks",
                "service_port",
                "relay_pid",
                "relay_start_ticks",
                "relay_port",
            )
        }
        _atomic_json(
            receipt_path,
            {
                "backup_path": str(backup_path),
                "compatibility_ref": current_ref,
                "contract": RECEIPT_CONTRACT,
                "dsh_outcome": outcome,
                "generated_ownership": generated_ownership,
                "manifest_sha256": plan.manifest_sha256,
                "prior_running": bool(before),
                "process_identities": identities,
                "review_service": list(review_service) if review_service else None,
                "target_ref": plan.target_ref,
            },
        )
    except BaseException:
        if start_review is not None:
            start_review(review_service)
        raise
    return PreparedCutover(plan, backup_path, review_service, before)


def run_cleanup(
    prepared: PreparedCutover,
    *,
    engine: Path = ENGINE,
    receipt_path: Path | None = None,
    cleanup_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Run target cleanup through newly materialized code in a fresh process."""
    receipt_path = receipt_path or CUTOVER_RECEIPT
    cleanup_receipt_path = cleanup_receipt_path or CLEANUP_RECEIPT
    script = engine.parent / prepared.plan.cleanup_hook
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--target-ref",
            prepared.plan.target_ref,
            "--cutover-receipt",
            str(receipt_path),
            "--cleanup-receipt",
            str(cleanup_receipt_path),
        ],
        cwd=engine.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CutoverError(f"target DSH cleanup failed: {detail}")
    try:
        receipt = _json_object(
            cleanup_receipt_path.read_bytes(), label="target cleanup receipt"
        )
    except OSError as exc:
        raise CutoverError("target DSH cleanup did not publish its receipt") from exc
    if (
        receipt.get("contract") != "sc-dsh-cleanup-receipt-v1"
        or receipt.get("target_ref") != prepared.plan.target_ref
        or receipt.get("compatibility_ref") != prepared.plan.compatibility_ref
        or receipt.get("manifest_sha256") != prepared.plan.manifest_sha256
        or receipt.get("status") != "complete"
    ):
        raise CutoverError("target DSH cleanup receipt does not match the cutover")
    return receipt


def record_recovery(
    prepared: PreparedCutover,
    *,
    status: str,
    detail: str,
    receipt_path: Path | None = None,
) -> None:
    receipt_path = receipt_path or CUTOVER_RECEIPT
    receipt = _json_object(receipt_path.read_bytes(), label="cutover receipt")
    receipt["recovery"] = {"detail": detail, "status": status}
    _atomic_json(receipt_path, receipt)


def installed_removal_ready(
    *,
    engine_ref_path: Path | None = None,
    manifest_path: Path | None = None,
    cleanup_receipt_path: Path | None = None,
) -> bool:
    """Prove a materialized removal floor completed cleanup and publication."""
    engine_ref_path = engine_ref_path or (STATE_DIR / "engine.ref")
    manifest_path = manifest_path or (REPO_ROOT / TARGET_MANIFEST_PATH)
    cleanup_receipt_path = cleanup_receipt_path or CLEANUP_RECEIPT
    try:
        manifest = _json_object(
            manifest_path.read_bytes(), label="materialized DSH removal manifest"
        )
    except OSError:
        return True
    cutover = manifest.get("cutover")
    if cutover is None:
        return True
    if not isinstance(cutover, dict) or cutover.get("contract") != CUTOVER_CONTRACT:
        return False
    current_ref = _read_exact_ref(engine_ref_path)
    if current_ref is None:
        return False
    try:
        receipt = _json_object(
            cleanup_receipt_path.read_bytes(), label="installed cleanup receipt"
        )
    except OSError:
        return False
    return (
        receipt.get("contract") == "sc-dsh-cleanup-receipt-v1"
        and receipt.get("target_ref") == current_ref
        and receipt.get("compatibility_ref") == cutover.get("minimum_floor_ref")
        and receipt.get("manifest_sha256")
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        and receipt.get("status") == "complete"
    )


def _read_exact_ref(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if _SHA_RE.fullmatch(value) else None


def admit_dispatch(argv: list[str]) -> int:
    """Block ordinary commands on a legacy half-materialized removal floor."""
    if installed_removal_ready():
        return 0
    command = argv[0] if argv else ""
    if command in {"rollback", "update"}:
        return 0
    print(
        "sc: DSH removal target is half-adopted; ordinary launch is refused. "
        "Run ./sc rollback --engine-only; ./sc update remains available only "
        "to report the required compatibility floor.",
        file=sys.stderr,
    )
    return 78


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--admit-dispatch" not in argv:
        print("usage: update_cutover.py --admit-dispatch -- <sc args>", file=sys.stderr)
        return 2
    index = argv.index("--admit-dispatch")
    remaining = argv[index + 1 :]
    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    return admit_dispatch(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
