"""Validated Visual QA state manifests and trusted GitHub evidence publication.

No application code is executed here. The publisher consumes only a bounded
JSON manifest and PNG files from the unprivileged capture job.
"""

from __future__ import annotations

import base64
import json
import re
import struct
from pathlib import Path

MAX_IMAGES = 100
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")


class EvidenceError(ValueError):
    """A malformed or untrusted evidence bundle."""


def read_manifest(path: Path) -> dict:
    if (any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES):
        raise EvidenceError("QA manifest must be a regular JSON file under 1 MiB")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise EvidenceError("QA manifest must be a JSON object")
    return raw


def label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise EvidenceError("QA labels must be nonempty strings of at most 500 characters")
    return value


def png_file(root: Path, name: object) -> tuple[bytes, int, int]:
    if root.is_symlink():
        raise EvidenceError("QA evidence cannot contain symlinks")
    if not isinstance(name, str):
        raise EvidenceError("QA image path must be a string")
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in {"..", "."} for p in path.parts):
        raise EvidenceError("QA image path must stay inside the gallery")
    if path.suffix != ".png" or len(name) > 250 or "\\" in name:
        raise EvidenceError("QA evidence accepts only relative .png paths")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise EvidenceError("QA evidence cannot contain symlinks")
    if not target.is_file() or target.stat().st_size > MAX_IMAGE_BYTES:
        raise EvidenceError("QA image must be a regular file under 8 MiB")
    data = target.read_bytes()
    if len(data) < 33 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise EvidenceError("QA image is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if not 0 < width <= 20000 or not 0 < height <= 200000:
        raise EvidenceError("QA image dimensions are invalid")
    return data, width, height


def validate_rows(rows: object, root: Path) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > MAX_IMAGES:
        raise EvidenceError("QA report must contain at most 100 states/routes")
    normalized = []
    total = 0
    count = 0
    names = set()
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceError("QA state must be an object")
        name = label(row.get("route", row.get("name")))
        if name in names:
            raise EvidenceError("QA state names must be unique")
        names.add(name)
        captures = row.get("captures")
        if not isinstance(captures, list) or not captures:
            raise EvidenceError("Each QA state needs captures")
        variants = set()
        checked = []
        for capture in captures:
            count += 1
            if count > MAX_IMAGES or not isinstance(capture, dict):
                raise EvidenceError("QA report accepts at most 100 captures")
            variant = label(capture.get("name"))
            if variant in variants:
                raise EvidenceError("QA variant names must be unique within a state")
            variants.add(variant)
            ok = capture.get("ok")
            if type(ok) is not bool:
                raise EvidenceError("QA capture ok must be a boolean")
            error = capture.get("error")
            if error is not None:
                error = label(error)
            item = {"name": variant, "ok": ok, "error": error}
            for key in ("viewport_width", "viewport_height"):
                value = capture.get(key)
                if type(value) is not int or not 0 < value <= 20000:
                    raise EvidenceError(f"QA {key} must be an integer between 1 and 20000")
                item[key] = value
            image = capture.get("image")
            written = capture.get("image_written", image is not None)
            if type(written) is not bool or (ok and not written):
                raise EvidenceError("Successful QA captures must have an image")
            item["image_written"] = written
            item["image"] = image if written else None
            if written:
                data, width, height = png_file(root, image)
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise EvidenceError("QA evidence exceeds 64 MiB")
                item.update(image_width=width, image_height=height)
            else:
                item.update(image_width=item["viewport_width"], image_height=item["viewport_height"])
            checked.append(item)
        normalized.append({"route": name, "ok": all(c["ok"] for c in checked), "captures": checked})
    return normalized


def scenario_summary(raw: dict, gallery: Path) -> dict:
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise EvidenceError("QA scenario manifest version must be 1")
    if raw.get("data_mode") not in {"stubbed", "real", "static"}:
        raise EvidenceError("QA scenario data_mode must be stubbed, real, or static")
    rows = validate_rows(raw.get("states"), gallery)
    if not rows:
        raise EvidenceError("QA scenario manifest must contain at least one state")
    failed = sum(not row["ok"] for row in rows)
    return {"mode": "scenarios", "data_mode": raw["data_mode"],
            "outcome": "failed" if failed else "passed", "routes_total": len(rows),
            "routes_failed": failed, "routes": rows}


def validated_report(raw: dict, gallery: Path) -> dict:
    if type(raw.get("version")) is not int or raw["version"] != 1 or raw.get("outcome") not in {"passed", "failed", "neutral"}:
        raise EvidenceError("Invalid QA report version/outcome")
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        raise EvidenceError("QA report is missing capture metadata")
    for key in ("source_head_sha", "tested_sha"):
        if not isinstance(metadata.get(key), str) or not SHA.fullmatch(metadata[key]):
            raise EvidenceError(f"QA {key} must be a full commit SHA")
    for key in ("run_id", "run_attempt", "pr_number"):
        if type(metadata.get(key)) is not int or metadata[key] < 1:
            raise EvidenceError(f"QA {key} must be a positive integer")
    if raw.get("mode", "routes") not in {"routes", "scenarios"}:
        raise EvidenceError("Invalid QA report mode")
    if raw.get("data_mode", "unspecified") not in {"stubbed", "real", "static", "unspecified"}:
        raise EvidenceError("Invalid QA report data mode")
    rows = validate_rows(raw.get("routes"), gallery)
    if raw["outcome"] == "passed" and not rows:
        raise EvidenceError("Passed QA report cannot be empty")
    if raw["outcome"] == "neutral" and rows:
        raise EvidenceError("Skipped QA report cannot contain captures")
    if raw.get("mode", "routes") == "routes":
        for row in rows:
            row["ok"] = any(c["ok"] for c in row["captures"])
    result = {"version": 1, "outcome": raw["outcome"],
              "metadata": {key: metadata[key] for key in ("source_head_sha", "tested_sha", "pr_number", "run_id", "run_attempt")},
              "mode": raw.get("mode", "routes"), "data_mode": raw.get("data_mode", "unspecified"),
              "routes": rows, "routes_total": len(rows),
              "routes_failed": sum(not row["ok"] for row in rows)}
    for key in ("error", "reason"):
        if raw.get(key):
            result[key] = label(raw[key])
    if result["mode"] == "scenarios" and result["routes_failed"]:
        result["outcome"] = "failed"
    if rows and result["routes_failed"] == len(rows):
        result["outcome"] = "failed"
    return result


def publish_branch(report: dict, gallery: Path, repo_url: str, token: str, requester) -> str:
    """Git-data API creates an independent, append-only evidence branch."""
    from urllib.error import HTTPError

    ref = f"heads/qa-evidence/pr-{report['metadata']['pr_number']}"
    try:
        previous = requester("GET", f"{repo_url}/git/ref/{ref}", token)["object"]["sha"]
    except HTTPError as exc:
        if exc.code != 404:
            raise
        previous = None
    tree = []
    images = {c["image"] for row in report["routes"] for c in row["captures"] if c["image_written"]}
    for name in sorted(images):
        data, _, _ = png_file(gallery, name)
        blob = requester("POST", f"{repo_url}/git/blobs", token,
                         {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        tree.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree.append({"path": "summary.json", "mode": "100644", "type": "blob",
                 "content": json.dumps(report, indent=2) + "\n"})
    created = requester("POST", f"{repo_url}/git/trees", token, {"tree": tree})
    commit = requester("POST", f"{repo_url}/git/commits", token,
                       {"message": f"Visual QA evidence for PR #{report['metadata']['pr_number']} at {report['metadata']['source_head_sha']}",
                        "tree": created["sha"], "parents": [previous] if previous else []})
    if previous:
        requester("PATCH", f"{repo_url}/git/refs/{ref}", token,
                  {"sha": commit["sha"], "force": False})
    else:
        requester("POST", f"{repo_url}/git/refs", token,
                  {"ref": f"refs/{ref}", "sha": commit["sha"]})
    return commit["sha"]


def publish_report(gallery: Path, event: dict, env: dict, requester=None) -> bool:
    """Publish data only when it matches an authoritative workflow_run event."""
    from urllib.error import HTTPError
    from urllib.parse import quote

    import visual_qa as qa

    requester = requester or qa._github_request
    run = event.get("workflow_run", {})
    repo = env.get("GITHUB_REPOSITORY", "")
    if run.get("event") != "pull_request" or run.get("head_repository", {}).get("full_name") != repo:
        print("visual-qa: inline publication skipped for non-PR or external-fork capture; use the artifact")
        return True
    report = validated_report(read_manifest(gallery / "summary.json"), gallery)
    meta = report["metadata"]
    if (meta["run_id"], meta["run_attempt"], meta["source_head_sha"]) != (
            run.get("id"), run.get("run_attempt", 1), run.get("head_sha")):
        raise EvidenceError("QA report does not match the originating workflow run")
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise EvidenceError("QA publication requires GITHUB_TOKEN")
    repo_url = f"{env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')}/repos/{repo}"
    pr_url = f"{repo_url}/pulls/{meta['pr_number']}"

    def current_head():
        pr = requester("GET", pr_url, token)
        return (pr.get("state") == "open" and pr.get("head", {}).get("sha") == meta["source_head_sha"]
                and pr.get("head", {}).get("ref") == run.get("head_branch")
                and pr.get("head", {}).get("repo", {}).get("full_name") == repo
                and pr.get("base", {}).get("repo", {}).get("full_name") == repo)

    if not current_head():
        print("visual-qa: stale or closed PR capture skipped")
        return True
    # A rerun of an old run must not replace a newer run at the same head.
    try:
        old_file = requester("GET", f"{repo_url}/contents/summary.json?ref=qa-evidence/pr-{meta['pr_number']}", token)
        old_meta = json.loads(base64.b64decode(old_file["content"]))["metadata"]
        if (old_meta["run_id"], old_meta["run_attempt"]) > (meta["run_id"], meta["run_attempt"]):
            print("visual-qa: newer capture already published; skipping this run")
            return True
    except HTTPError as exc:
        if exc.code != 404:
            raise

    if run.get("conclusion") != "success" and report["outcome"] == "passed":
        report.update(outcome="failed", error="Capture workflow did not complete successfully")
    if report["outcome"] != "neutral":
        commit = publish_branch(report, gallery, repo_url, token, requester)
        root = f"{env.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')}/{repo}/blob/{commit}"
        for row in report["routes"]:
            for capture in row["captures"]:
                if capture["image_written"]:
                    capture["image_url"] = f"{root}/{quote(capture['image'], safe='/')}?raw=true"
    if not current_head():
        print("visual-qa: PR head changed during image upload; report skipped")
        return True
    comment_env = dict(env, GITHUB_RUN_ID=str(meta["run_id"]))
    body = qa.build_comment(report, environ=comment_env)
    qa.write_step_summary(body, environ=env)
    # Build a minimal PR context, independent of untrusted artifact metadata.
    posted = qa.post_sticky_comment(body, environ=comment_env, requester=requester,
                                    pr_number=meta["pr_number"], author_login="github-actions[bot]")
    if not posted:
        raise EvidenceError("Inline QA report could not be posted; capture artifact remains available")
    return True
