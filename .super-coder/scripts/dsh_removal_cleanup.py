#!/usr/bin/env python3
"""Fresh-process cleanup for a target that removes the DSH runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
MANIFEST = ENGINE / "assets/dsh-removal/removal-manifest-v1.json"
CUTOVER_CONTRACT = "sc-dsh-removal-cutover-v1"
RECEIPT_CONTRACT = "sc-dsh-cutover-receipt-v1"
CLEANUP_CONTRACT = "sc-dsh-cleanup-receipt-v1"


class CleanupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise CleanupError(f"{label} must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    data = (json.dumps(dict(value), indent=2, sort_keys=True) + "\n").encode()
    with pending.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _tracked_plan(manifest: Mapping[str, Any], root: Path) -> tuple[list[dict], list[str]]:
    planned: list[dict] = []
    errors: list[str] = []
    root = root.resolve()
    rows = manifest.get("tracked_artifacts")
    if not isinstance(rows, list):
        return [], ["tracked_artifacts must be a list"]
    for row in rows:
        if not isinstance(row, dict):
            errors.append("tracked artifact row must be an object")
            continue
        relative = row.get("path")
        expected = row.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            errors.append("tracked artifact path/digest is invalid")
            continue
        path = root / relative
        if path.is_symlink():
            errors.append(f"tracked artifact is a symlink: {relative}")
            continue
        resolved = path.resolve(strict=False)
        if not _inside(resolved, root):
            errors.append(f"tracked artifact escapes fork root: {relative}")
            continue
        if not path.exists():
            planned.append({"path": relative, "action": "absent", "sha256": expected})
            continue
        if not path.is_file():
            errors.append(f"tracked artifact is not a file: {relative}")
            continue
        actual = _sha256(path)
        if actual != expected:
            errors.append(f"tracked artifact digest mismatch: {relative}")
            continue
        planned.append({"path": relative, "action": "delete", "sha256": expected})
    return planned, errors


def _generated_plan(
    manifest: Mapping[str, Any],
    root: Path,
    ownership: Any,
) -> tuple[list[dict], list[str]]:
    planned: list[dict] = []
    errors: list[str] = []
    engine = (root / ".super-coder").resolve()
    rows = manifest.get("generated_artifacts")
    if not isinstance(rows, list):
        return [], ["generated_artifacts must be a list"]
    if not isinstance(ownership, list):
        return [], ["cutover receipt has no generated ownership snapshot"]
    by_declared = {
        row.get("declared_path"): row for row in ownership if isinstance(row, dict)
    }
    if len(by_declared) != len(ownership):
        return [], ["cutover generated ownership snapshot is ambiguous"]
    for row in rows:
        if not isinstance(row, dict):
            errors.append("generated artifact row must be an object")
            continue
        relative = row.get("path")
        kind = row.get("kind")
        if not isinstance(relative, str) or not isinstance(kind, str):
            errors.append("generated artifact path/kind is invalid")
            continue
        lexical = Path(relative)
        if lexical.is_absolute() or ".." in lexical.parts:
            errors.append(f"generated artifact path is unsafe: {relative}")
            continue
        captured = by_declared.get(relative)
        if not isinstance(captured, dict) or captured.get("kind") != kind:
            errors.append(f"generated artifact lacks exact cutover ownership: {relative}")
            continue
        captured_paths = captured.get("paths")
        roots = captured.get("roots")
        directories = captured.get("directories", [])
        if (
            not isinstance(captured_paths, list)
            or not isinstance(roots, list)
            or not isinstance(directories, list)
            or not all(isinstance(value, str) for value in directories)
        ):
            errors.append(f"generated ownership shape is invalid: {relative}")
            continue
        files: list[str] = []
        for item in captured_paths:
            if not isinstance(item, dict) or item.get("type") != "file":
                errors.append(f"generated ownership file row is invalid: {relative}")
                continue
            captured_path = item.get("path")
            digest = item.get("sha256")
            if not isinstance(captured_path, str) or not isinstance(digest, str):
                errors.append(f"generated ownership file row is invalid: {relative}")
                continue
            path = root / captured_path
            if (
                path.is_symlink()
                or not _inside(path.resolve(strict=False), engine)
                or not path.is_file()
                or _sha256(path) != digest
            ):
                errors.append(f"generated artifact changed after quiescence: {captured_path}")
                continue
            files.append(captured_path)
        safe_roots: list[str] = []
        for captured_root in roots:
            if not isinstance(captured_root, str):
                errors.append(f"generated ownership root is invalid: {relative}")
                continue
            path = root / captured_root
            if path.is_symlink() or not _inside(path.resolve(strict=False), engine) or not path.is_dir():
                errors.append(f"generated ownership root changed: {captured_root}")
                continue
            actual_files = {
                str(child.relative_to(root))
                for child in path.rglob("*")
                if child.is_file() or child.is_symlink()
            }
            expected_files = {name for name in files if _inside(root / name, path)}
            actual_directories = {
                str(child.relative_to(root))
                for child in path.rglob("*")
                if child.is_dir() and not child.is_symlink()
            }
            expected_directories = {
                name for name in directories if _inside(root / name, path)
            }
            if (
                actual_files != expected_files
                or actual_directories != expected_directories
            ):
                errors.append(f"generated bounded artifact has unexpected child: {captured_root}")
                continue
            safe_roots.append(captured_root)
        if not files and not safe_roots:
            if "<fork-id>" in relative:
                parent = root / relative.split("<fork-id>", 1)[0]
                if parent.exists() and any(parent.iterdir()):
                    errors.append(f"generated artifact appeared after quiescence: {relative}")
            elif (root / relative).exists():
                errors.append(f"generated artifact appeared after quiescence: {relative}")
        planned.append(
            {
                "action": "delete" if files or safe_roots else "absent",
                "directories": directories,
                "files": files,
                "kind": kind,
                "path": relative,
                "roots": safe_roots,
            }
        )
    return planned, errors


def _restore_tracked(
    rows: list[dict], compatibility_ref: str, *, root: Path
) -> list[str]:
    errors: list[str] = []
    for row in rows:
        relative = row["path"]
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{compatibility_ref}:{relative}"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            errors.append(f"cannot restore tracked artifact: {relative}")
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + ".restore")
        with pending.open("wb") as handle:
            handle.write(result.stdout)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        if _sha256(path) != row["sha256"]:
            errors.append(f"restored tracked artifact digest mismatch: {relative}")
    return errors


def cleanup(
    target_ref: str,
    cutover_receipt_path: Path,
    cleanup_receipt_path: Path,
    *,
    root: Path = REPO_ROOT,
    manifest_path: Path = MANIFEST,
) -> dict[str, Any]:
    manifest = _load(manifest_path, "target removal manifest")
    cutover = manifest.get("cutover")
    if not isinstance(cutover, dict) or cutover.get("contract") != CUTOVER_CONTRACT:
        raise CleanupError("target removal manifest has no valid cutover contract")
    receipt = _load(cutover_receipt_path, "cutover receipt")
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    compatibility_ref = cutover.get("minimum_floor_ref")
    if (
        receipt.get("contract") != RECEIPT_CONTRACT
        or receipt.get("target_ref") != target_ref
        or receipt.get("compatibility_ref") != compatibility_ref
        or receipt.get("manifest_sha256") != manifest_digest
    ):
        raise CleanupError("cutover receipt does not match target removal manifest")

    tracked, errors = _tracked_plan(manifest, root)
    generated, generated_errors = _generated_plan(
        manifest, root, receipt.get("generated_ownership")
    )
    errors.extend(generated_errors)
    if errors:
        refused = {
            "compatibility_ref": compatibility_ref,
            "contract": CLEANUP_CONTRACT,
            "errors": errors,
            "generated": generated,
            "manifest_sha256": manifest_digest,
            "status": "refused",
            "target_ref": target_ref,
            "tracked": tracked,
        }
        _atomic_json(cleanup_receipt_path, refused)
        raise CleanupError("; ".join(errors))

    deleted_tracked: list[dict] = []
    try:
        for row in tracked:
            if row["action"] == "delete":
                (root / row["path"]).unlink()
                deleted_tracked.append(row)
        for row in generated:
            if row["action"] != "delete":
                continue
            for relative in row["files"]:
                (root / relative).unlink()
            for relative in sorted(
                row["directories"],
                key=lambda value: len(Path(value).parts),
                reverse=True,
            ):
                (root / relative).rmdir()
            for relative in sorted(
                row["roots"], key=lambda value: len(Path(value).parts), reverse=True
            ):
                tree = root / relative
                tree.rmdir()
    except OSError as exc:
        restore_errors = _restore_tracked(
            deleted_tracked, str(compatibility_ref), root=root
        )
        detail = [f"cleanup delete failed: {exc}", *restore_errors]
        _atomic_json(
            cleanup_receipt_path,
            {
                "compatibility_ref": compatibility_ref,
                "contract": CLEANUP_CONTRACT,
                "errors": detail,
                "generated": generated,
                "manifest_sha256": manifest_digest,
                "status": "restored" if not restore_errors else "restore-failed",
                "target_ref": target_ref,
                "tracked": tracked,
            },
        )
        raise CleanupError("; ".join(detail)) from exc

    complete = {
        "compatibility_ref": compatibility_ref,
        "contract": CLEANUP_CONTRACT,
        "errors": [],
        "generated": generated,
        "manifest_sha256": manifest_digest,
        "status": "complete",
        "target_ref": target_ref,
        "tracked": tracked,
    }
    _atomic_json(cleanup_receipt_path, complete)
    return complete


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-ref", required=True)
    parser.add_argument("--cutover-receipt", type=Path, required=True)
    parser.add_argument("--cleanup-receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cleanup(
            args.target_ref,
            args.cutover_receipt,
            args.cleanup_receipt,
        )
    except CleanupError as exc:
        print(f"dsh removal cleanup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
