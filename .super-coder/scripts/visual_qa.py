#!/usr/bin/env python3
"""Capture advisory viewport screenshots for fork applications.

The engine deliberately has no Playwright dependency. ``ci`` installs the
pinned package and Chromium into the ephemeral workflow environment; ``run``
expects the local dev kit to provide them. Everything outside the capture
adapter is stdlib-only so the engine suite remains hermetic.
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO


ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
CONFIG_RELATIVE = Path(".sc-state/visual-qa.json")
DEFAULT_GALLERY = Path("gallery")
PLAYWRIGHT_VERSION = "1.54.0"
DEFAULT_VIEWPORTS = (
    {"name": "mobile", "width": 375, "height": 812},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "desktop", "width": 1440, "height": 900},
)


class VisualQaError(RuntimeError):
    """A clear, operator-actionable visual-QA contract failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _integer(value: object, key: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualQaError(f"config key '{key}' must be an integer")
    if not minimum <= value <= maximum:
        raise VisualQaError(
            f"config key '{key}' must be between {minimum} and {maximum}"
        )
    return value


def _string_list(value: object, key: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise VisualQaError(f"config key '{key}' must be a list of non-empty strings")
    if not allow_empty and not value:
        raise VisualQaError(f"config key '{key}' must not be empty")
    return list(value)


def _validate_viewports(value: object) -> list[dict[str, object]]:
    if value == "default":
        return [dict(viewport) for viewport in DEFAULT_VIEWPORTS]
    if not isinstance(value, list) or not value:
        raise VisualQaError(
            "config key 'viewports' must be 'default' or a non-empty list"
        )

    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for index, viewport in enumerate(value):
        if not isinstance(viewport, dict):
            raise VisualQaError(f"viewports[{index}] must be an object")
        unknown = set(viewport) - {"name", "width", "height"}
        if unknown:
            raise VisualQaError(
                f"viewports[{index}] has unknown keys: {', '.join(sorted(unknown))}"
            )
        name = viewport.get("name")
        if not isinstance(name, str) or not name.strip():
            raise VisualQaError(f"viewports[{index}].name must be a non-empty string")
        if name in names:
            raise VisualQaError(f"viewport name '{name}' is duplicated")
        names.add(name)
        normalized.append(
            {
                "name": name,
                "width": _integer(
                    viewport.get("width"),
                    f"viewports[{index}].width",
                    minimum=1,
                    maximum=10000,
                ),
                "height": _integer(
                    viewport.get("height"),
                    f"viewports[{index}].height",
                    minimum=1,
                    maximum=10000,
                ),
            }
        )
    return normalized


def validate_config(raw: object) -> dict[str, object]:
    """Validate and normalize the fork-owned configuration."""
    if not isinstance(raw, dict):
        raise VisualQaError("visual-qa config must contain a JSON object")

    known = {
        "cwd",
        "setup",
        "serve",
        "port",
        "ready_path",
        "ready_timeout_s",
        "settle_ms",
        "routes",
        "viewports",
        "paths",
        "services",
        "artifact_retention_days",
    }
    unknown = set(raw) - known
    if unknown:
        raise VisualQaError(f"unknown config keys: {', '.join(sorted(unknown))}")

    serve = raw.get("serve")
    if not isinstance(serve, str) or not serve.strip():
        raise VisualQaError("config key 'serve' is required and must be a string")

    routes = _string_list(raw.get("routes"), "routes", allow_empty=False)
    for route in routes:
        if not route.startswith("/"):
            raise VisualQaError(f"route '{route}' must start with '/'")

    cwd = raw.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd.strip():
        raise VisualQaError("config key 'cwd' must be a non-empty string")
    cwd_path = Path(cwd)
    if cwd_path.is_absolute() or ".." in cwd_path.parts:
        raise VisualQaError("config key 'cwd' must stay within the fork checkout")

    ready_path = raw.get("ready_path", "/")
    if not isinstance(ready_path, str) or not ready_path.startswith("/"):
        raise VisualQaError("config key 'ready_path' must start with '/'")

    services = _string_list(raw.get("services", []), "services")
    if services not in ([], ["postgres"]):
        raise VisualQaError(
            "config key 'services' supports only [] or ['postgres'] in v1"
        )

    paths_value = raw.get("paths")
    paths = None if paths_value is None else _string_list(paths_value, "paths")

    return {
        "cwd": cwd,
        "setup": _string_list(raw.get("setup", []), "setup"),
        "serve": serve,
        "port": _integer(raw.get("port", 4173), "port", minimum=1, maximum=65535),
        "ready_path": ready_path,
        "ready_timeout_s": _integer(
            raw.get("ready_timeout_s", 120),
            "ready_timeout_s",
            minimum=1,
            maximum=1800,
        ),
        "settle_ms": _integer(
            raw.get("settle_ms", 500), "settle_ms", minimum=0, maximum=60000
        ),
        "routes": routes,
        "viewports": _validate_viewports(raw.get("viewports", "default")),
        "paths": paths,
        "services": services,
        "artifact_retention_days": _integer(
            raw.get("artifact_retention_days", 14),
            "artifact_retention_days",
            minimum=1,
            maximum=90,
        ),
    }


def load_config(repo: Path = REPO_ROOT) -> dict[str, object] | None:
    path = repo / CONFIG_RELATIVE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise VisualQaError(
            f"invalid JSON in {CONFIG_RELATIVE}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise VisualQaError(f"cannot read {CONFIG_RELATIVE}: {exc}") from exc
    return validate_config(raw)


def should_skip(paths: list[str] | None, changed: list[str] | None) -> bool:
    """Return true only for a resolved diff with no configured path match."""
    if not paths or changed is None:
        return False
    return not any(
        fnmatch.fnmatchcase(path, pattern) for path in changed for pattern in paths
    )


def pr_changed_paths(
    repo: Path = REPO_ROOT,
    *,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str] | None:
    """Resolve a PR diff; return None when it cannot be trusted.

    Checkout actions commonly produce a shallow merge checkout. Fetching the
    current base tip gives a deterministic comparison without relying on the
    merge commit's missing parent objects. Failure means capture, never skip.
    """
    env = os.environ if environ is None else environ
    base = env.get("GITHUB_BASE_REF", "").strip()
    if not base:
        return None
    fetch = runner(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", base],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if fetch.returncode:
        print(
            f"visual-qa: could not resolve PR base '{base}'; capturing instead of skipping",
            file=sys.stderr,
        )
        return None
    diff = runner(
        ["git", "diff", "--name-only", "FETCH_HEAD", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if diff.returncode:
        print(
            "visual-qa: could not read PR diff; capturing instead of skipping",
            file=sys.stderr,
        )
        return None
    return [line for line in diff.stdout.splitlines() if line]


def _run_shell(command: str, *, cwd: Path, env: dict[str, str], log: TextIO) -> None:
    print(f"$ {command}", file=log, flush=True)
    completed = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode:
        raise VisualQaError(f"setup command failed ({completed.returncode}): {command}")


def run_setup(
    config: dict[str, object], repo: Path, env: dict[str, str], log: TextIO
) -> None:
    cwd = repo / str(config["cwd"])
    if not cwd.is_dir():
        raise VisualQaError(f"configured cwd does not exist: {config['cwd']}")
    for command in config["setup"]:
        _run_shell(str(command), cwd=cwd, env=env, log=log)


@dataclass
class ServiceHandle:
    container: str

    def stop(self, log: TextIO) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.container],
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )


def start_postgres(env: dict[str, str], log: TextIO) -> ServiceHandle:
    if not shutil.which("docker"):
        raise VisualQaError("services includes postgres but docker is not available")
    name = f"subfloor-visual-qa-{os.getpid()}"
    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        "POSTGRES_USER=sc",
        "-e",
        "POSTGRES_PASSWORD=sc",
        "-e",
        "POSTGRES_DB=sc",
        "-p",
        "127.0.0.1::5432",
        "postgres:17",
    ]
    started = subprocess.run(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if started.returncode:
        raise VisualQaError("postgres service failed to start")
    handle = ServiceHandle(name)
    try:
        port_result = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            text=True,
            capture_output=True,
        )
        if port_result.returncode or ":" not in port_result.stdout:
            raise VisualQaError("postgres service did not publish a host port")
        port = port_result.stdout.strip().rsplit(":", 1)[1]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "sc", "-d", "sc"],
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if ready.returncode == 0:
                env["DATABASE_URL"] = f"postgresql://sc:sc@127.0.0.1:{port}/sc"
                return handle
            time.sleep(1)
        raise VisualQaError("postgres service did not become ready within 60s")
    except Exception:
        handle.stop(log)
        raise


def start_services(
    config: dict[str, object], env: dict[str, str], log: TextIO
) -> list[ServiceHandle]:
    handles: list[ServiceHandle] = []
    if config["services"] == ["postgres"]:
        handles.append(start_postgres(env, log))
    return handles


def start_server(
    config: dict[str, object], repo: Path, env: dict[str, str], log: TextIO
) -> subprocess.Popen[str]:
    cwd = repo / str(config["cwd"])
    command = str(config["serve"]).replace("{port}", str(config["port"]))
    print(f"$ {command}", file=log, flush=True)
    try:
        return subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise VisualQaError(f"serve command could not start: {exc}") from exc


def stop_server(server: subprocess.Popen[str]) -> None:
    if server.poll() is not None:
        return
    try:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(server.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        server.wait()


def wait_until_ready(
    url: str,
    *,
    timeout_s: int,
    server: subprocess.Popen[str],
    opener: Callable[..., object] = urllib.request.urlopen,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = "no response"
    while time.monotonic() < deadline:
        exit_code = server.poll()
        if exit_code is not None:
            raise VisualQaError(
                f"serve command exited before readiness (code {exit_code})"
            )
        try:
            with opener(url, timeout=5) as response:  # type: ignore[attr-defined]
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise VisualQaError(
        f"app did not return HTTP 200 at {url} within {timeout_s}s ({last_error})"
    )


@contextmanager
def ci_app(
    config: dict[str, object], repo: Path, log: TextIO
) -> Iterator[tuple[str, dict[str, str]]]:
    env = dict(os.environ)
    services: list[ServiceHandle] = []
    server: subprocess.Popen[str] | None = None
    try:
        services = start_services(config, env, log)
        run_setup(config, repo, env, log)
        server = start_server(config, repo, env, log)
        base_url = f"http://127.0.0.1:{config['port']}"
        wait_until_ready(
            base_url + str(config["ready_path"]),
            timeout_s=int(config["ready_timeout_s"]),
            server=server,
        )
        yield base_url, env
    finally:
        if server is not None:
            stop_server(server)
        for service in reversed(services):
            service.stop(log)


def prepare_gallery(gallery: Path) -> None:
    if gallery.exists():
        shutil.rmtree(gallery)
    gallery.mkdir(parents=True)


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip("/"))
    return slug.strip("-.") or fallback


class PlaywrightCapture:
    """Lazy Playwright adapter; imported only in real capture runs."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None

    def __enter__(self) -> "PlaywrightCapture":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise VisualQaError(
                "Playwright is not installed; run `pip install "
                f"playwright=={PLAYWRIGHT_VERSION} && playwright install chromium`"
            ) from exc
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:
            self._playwright.stop()
            raise VisualQaError(
                "Chromium could not start; run `playwright install chromium`"
            ) from exc
        return self

    def __exit__(self, *_args: object) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def capture(
        self,
        url: str,
        viewport: dict[str, object],
        output: Path,
        *,
        settle_ms: int,
        timeout_ms: int,
    ) -> dict[str, object]:
        if self._browser is None:
            raise VisualQaError("Playwright capture session was not started")
        page = self._browser.new_page(
            viewport={"width": viewport["width"], "height": viewport["height"]}
        )
        status: int | None = None
        error: str | None = None
        screenshot_written = False
        dimensions = {
            "width": int(viewport["width"]),
            "height": int(viewport["height"]),
        }
        try:
            try:
                response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                status = response.status if response is not None else None
                if status != 200:
                    error = (
                        f"HTTP {status}" if status is not None else "no HTTP response"
                    )
            except Exception as exc:
                error = f"navigation: {exc}"

            try:
                page.wait_for_timeout(settle_ms)
                measured = page.evaluate(
                    """() => ({
                        width: Math.max(document.documentElement.scrollWidth,
                                        document.body?.scrollWidth || 0),
                        height: Math.max(document.documentElement.scrollHeight,
                                         document.body?.scrollHeight || 0)
                    })"""
                )
                if isinstance(measured, dict):
                    dimensions = {
                        "width": int(measured.get("width") or viewport["width"]),
                        "height": int(measured.get("height") or viewport["height"]),
                    }
                output.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output), full_page=True)
                screenshot_written = True
            except Exception as exc:
                detail = f"screenshot: {exc}"
                error = f"{error}; {detail}" if error else detail
        finally:
            page.close()

        return {
            "ok": status == 200 and screenshot_written,
            "status": status,
            "error": error,
            "image_width": dimensions["width"],
            "image_height": dimensions["height"],
            "image_written": screenshot_written,
        }


def _target_url(base_url: str, route: str) -> str:
    return base_url.rstrip("/") + "/" + route.lstrip("/")


def capture_gallery(
    config: dict[str, object],
    base_url: str,
    gallery: Path,
    *,
    capture_factory: Callable[[], object] = PlaywrightCapture,
) -> dict[str, object]:
    """Capture route × viewport results and assemble the artifact gallery."""
    gallery.mkdir(parents=True, exist_ok=True)
    route_rows: list[dict[str, object]] = []
    used_slugs: set[str] = set()

    with capture_factory() as capture:  # type: ignore[attr-defined]
        for route in config["routes"]:
            route_text = str(route)
            base_slug = _slug(route_text.split("?", 1)[0], "root")
            slug = base_slug
            suffix = 2
            while slug in used_slugs:
                slug = f"{base_slug}-{suffix}"
                suffix += 1
            used_slugs.add(slug)

            captures: list[dict[str, object]] = []
            for viewport in config["viewports"]:
                viewport = dict(viewport)
                image = Path(slug) / f"{_slug(str(viewport['name']), 'viewport')}.png"
                try:
                    result = capture.capture(  # type: ignore[attr-defined]
                        _target_url(base_url, route_text),
                        viewport,
                        gallery / image,
                        settle_ms=int(config["settle_ms"]),
                        timeout_ms=int(config["ready_timeout_s"]) * 1000,
                    )
                except Exception as exc:
                    result = {
                        "ok": False,
                        "status": None,
                        "error": f"capture: {exc}",
                        "image_width": int(viewport["width"]),
                        "image_height": int(viewport["height"]),
                        "image_written": False,
                    }
                captures.append(
                    {
                        "name": viewport["name"],
                        "viewport_width": viewport["width"],
                        "viewport_height": viewport["height"],
                        "image": image.as_posix(),
                        **result,
                    }
                )
            route_rows.append(
                {
                    "route": route_text,
                    "slug": slug,
                    "ok": any(bool(result["ok"]) for result in captures),
                    "captures": captures,
                }
            )

    failed_routes = sum(not bool(route["ok"]) for route in route_rows)
    summary: dict[str, object] = {
        "generated_at": _utc_now(),
        "outcome": "failed" if failed_routes == len(route_rows) else "passed",
        "base_url": base_url,
        "routes_total": len(route_rows),
        "routes_failed": failed_routes,
        "routes": route_rows,
    }
    write_gallery(gallery, summary)
    return summary


def write_gallery(gallery: Path, summary: dict[str, object]) -> None:
    gallery.mkdir(parents=True, exist_ok=True)
    (gallery / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (gallery / "index.html").write_text(build_gallery_html(summary))


def build_gallery_html(summary: dict[str, object]) -> str:
    outcome = html.escape(str(summary.get("outcome", "unknown")))
    reason = html.escape(str(summary.get("reason") or summary.get("error") or ""))
    sections: list[str] = []
    for route in summary.get("routes", []):
        route = dict(route)
        cards: list[str] = []
        for capture in route.get("captures", []):
            capture = dict(capture)
            image = html.escape(str(capture["image"]), quote=True)
            name = html.escape(str(capture["name"]))
            mark = "✓" if capture.get("ok") else "✗"
            detail = html.escape(str(capture.get("error") or "HTTP 200"))
            picture = (
                f'<a href="{image}"><img src="{image}" alt="{name}"></a>'
                if capture.get("image_written")
                else '<div class="missing">No screenshot</div>'
            )
            cards.append(
                f"<article><h3>{mark} {name}</h3>{picture}"
                f"<p>{capture.get('image_width')}×{capture.get('image_height')} · {detail}</p></article>"
            )
        sections.append(
            f"<section><h2>{html.escape(str(route['route']))}</h2>"
            f'<div class="grid">{"".join(cards)}</div></section>'
        )
    if not sections:
        sections.append(f"<p>{reason}</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Subfloor Visual QA</title><style>
body{{font:15px system-ui,sans-serif;margin:2rem;background:#111;color:#eee}}a{{color:inherit}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}}
article{{background:#1d1d1d;padding:1rem;border-radius:.5rem}}img{{width:100%;height:auto;background:white}}
.missing{{min-height:12rem;display:grid;place-items:center;background:#2b1717}}p{{color:#bbb}}
</style></head><body><h1>Visual QA · {outcome}</h1>{"".join(sections)}</body></html>
"""


def result_summary(
    gallery: Path, outcome: str, *, reason: str | None = None, error: str | None = None
) -> dict[str, object]:
    summary: dict[str, object] = {
        "generated_at": _utc_now(),
        "outcome": outcome,
        "routes_total": 0,
        "routes_failed": 0,
        "routes": [],
    }
    if reason:
        summary["reason"] = reason
    if error:
        summary["error"] = error
    write_gallery(gallery, summary)
    return summary


def _github_links(environ: dict[str, str]) -> tuple[str | None, str | None]:
    server = environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = environ.get("GITHUB_REPOSITORY")
    run_id = environ.get("GITHUB_RUN_ID")
    if not repo or not run_id:
        return None, None
    run = f"{server}/{repo}/actions/runs/{run_id}"
    return run, f"{run}#artifacts"


def build_comment(
    summary: dict[str, object], *, environ: dict[str, str] | None = None
) -> str:
    env = dict(os.environ) if environ is None else environ
    lines = ["<!-- subfloor-visual-qa -->"]
    outcome = summary.get("outcome")
    if outcome == "neutral":
        lines.append(
            f"### ◻ Visual QA skipped\n\n{summary.get('reason', 'No capture needed.')}"
        )
    elif outcome == "failed" and not summary.get("routes"):
        lines.append(
            f"### ✗ Visual QA could not run\n\n{summary.get('error', 'Unknown error')}"
        )
        if summary.get("boot_log_tail"):
            tail = str(summary["boot_log_tail"]).replace("```", "` ` `")
            lines.extend(
                [
                    "",
                    "<details><summary>Boot log tail</summary>",
                    "",
                    "```text",
                    tail,
                    "```",
                    "</details>",
                ]
            )
    else:
        failed = int(summary.get("routes_failed", 0))
        total = int(summary.get("routes_total", 0))
        if outcome == "failed":
            lines.append(f"### ✗ Visual QA: all {total} routes failed")
        elif failed:
            lines.append(
                f"### ✓ Visual QA captured · {failed}/{total} routes need review"
            )
        else:
            lines.append(f"### ✓ Visual QA captured · {total}/{total} routes served")

        routes = list(summary.get("routes", []))
        if routes:
            names = [str(item["name"]) for item in routes[0]["captures"]]
            lines.extend(
                [
                    "",
                    "| Route | " + " | ".join(names) + " |",
                    "| --- | " + " | ".join("---" for _ in names) + " |",
                ]
            )
            for route in routes:
                cells = []
                for capture in route["captures"]:
                    mark = "✓" if capture["ok"] else "✗"
                    if capture.get("image_written"):
                        size = f"{capture['image_width']}×{capture['image_height']}"
                    else:
                        size = "no image"
                    cells.append(f"{mark} {size}")
                lines.append(f"| `{route['route']}` | " + " | ".join(cells) + " |")

    run_url, artifact_url = _github_links(env)
    links = []
    if artifact_url:
        links.append(f"[Download `visual-qa-gallery`]({artifact_url})")
    if run_url:
        links.append(f"[workflow run]({run_url})")
    if links:
        lines.extend(["", " · ".join(links)])
    lines.extend(
        [
            "",
            "This capture-only check is advisory for visual content. Screenshots are in the artifact; inline thumbnails are not available in v1.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _github_request(
    method: str, url: str, token: str, payload: dict[str, object] | None = None
) -> object:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
    return json.loads(body) if body else {}


def _pull_request_number(environ: dict[str, str]) -> int | None:
    event_path = environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None
    try:
        event = json.loads(Path(event_path).read_text())
        number = event.get("pull_request", {}).get("number") or event.get("number")
        return int(number) if number else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def post_sticky_comment(
    body: str,
    *,
    environ: dict[str, str] | None = None,
    requester: Callable[..., object] = _github_request,
) -> bool:
    """Create or update the one PR comment; all failures are non-fatal."""
    env = dict(os.environ) if environ is None else environ
    token = env.get("GITHUB_TOKEN", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    number = _pull_request_number(env)
    if not token or not repo or number is None:
        print("visual-qa: no writable pull-request context; sticky comment skipped")
        return False
    api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    try:
        comments_url = f"{api}/repos/{repo}/issues/{number}/comments"
        page = 1
        existing_id = None
        while True:
            comments = requester(
                "GET", f"{comments_url}?per_page=100&page={page}", token
            )
            if not isinstance(comments, list):
                raise VisualQaError("GitHub comments response was not a list")
            for comment in comments:
                if "<!-- subfloor-visual-qa -->" in str(comment.get("body", "")):
                    existing_id = comment.get("id")
                    break
            if existing_id is not None or len(comments) < 100:
                break
            page += 1
        if existing_id is not None:
            requester(
                "PATCH",
                f"{api}/repos/{repo}/issues/comments/{existing_id}",
                token,
                {"body": body},
            )
        else:
            requester("POST", comments_url, token, {"body": body})
        return True
    except Exception as exc:
        print(
            f"visual-qa: sticky comment could not be posted ({exc}); artifact remains available",
            file=sys.stderr,
        )
        return False


def write_step_summary(body: str, *, environ: dict[str, str] | None = None) -> None:
    env = dict(os.environ) if environ is None else environ
    target = env.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    try:
        with Path(target).open("a") as summary:
            summary.write(body.replace("<!-- subfloor-visual-qa -->\n", ""))
    except OSError as exc:
        print(f"visual-qa: could not write GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)


def publish_result(
    summary: dict[str, object], *, environ: dict[str, str] | None = None
) -> str:
    body = build_comment(summary, environ=environ)
    write_step_summary(body, environ=environ)
    post_sticky_comment(body, environ=environ)
    return body


def install_playwright(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    commands = (
        [sys.executable, "-m", "pip", "install", f"playwright=={PLAYWRIGHT_VERSION}"],
        [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
    )
    for command in commands:
        completed = runner(command)
        if completed.returncode:
            raise VisualQaError(
                f"Playwright setup failed ({completed.returncode}): {' '.join(command)}"
            )


def _package_candidates(repo: Path) -> list[Path]:
    candidates = [repo / "package.json", *sorted(repo.glob("*/package.json"))]
    return [
        path
        for path in candidates
        if not any(
            part in {"node_modules", "vendor", ".sc-worktrees"} for part in path.parts
        )
    ]


def detect_init_config(repo: Path = REPO_ROOT) -> dict[str, object]:
    package_path = None
    package: dict[str, object] = {}
    for candidate in _package_candidates(repo):
        try:
            value = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            package_path, package = candidate, value
            break

    if package_path is None:
        return {
            "cwd": ".",
            "setup": [],
            "serve": "python3 -m http.server {port} --bind 127.0.0.1",
            "port": 4173,
            "ready_path": "/",
            "ready_timeout_s": 120,
            "settle_ms": 500,
            "routes": ["/"],
            "viewports": "default",
            "paths": ["**/*.html", "static/**", "public/**"],
            "services": [],
            "artifact_retention_days": 14,
        }

    cwd_path = package_path.parent.relative_to(repo)
    cwd = cwd_path.as_posix() or "."
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    package_dir = package_path.parent
    install = (
        "npm ci" if (package_dir / "package-lock.json").exists() else "npm install"
    )
    setup = [install]
    if "build" in scripts:
        setup.append("npm run build")
    if "preview" in scripts:
        serve = "npm run preview -- --port {port} --host 127.0.0.1"
    elif "dev" in scripts:
        serve = "npm run dev -- --port {port} --host 127.0.0.1"
    elif "start" in scripts:
        serve = "PORT={port} npm start"
    else:
        serve = "python3 -m http.server {port} --bind 127.0.0.1"

    prefix = "" if cwd == "." else f"{cwd}/"
    return {
        "cwd": cwd,
        "setup": setup,
        "serve": serve,
        "port": 4173,
        "ready_path": "/",
        "ready_timeout_s": 120,
        "settle_ms": 500,
        "routes": ["/"],
        "viewports": "default",
        "paths": [f"{prefix}src/**", f"{prefix}static/**", f"{prefix}package.json"],
        "services": [],
        "artifact_retention_days": 14,
    }


def cmd_init(_args: argparse.Namespace, *, repo: Path = REPO_ROOT) -> int:
    target = repo / CONFIG_RELATIVE
    if target.exists():
        print(f"visual-qa: {CONFIG_RELATIVE} already exists; left unchanged")
        return 0
    config = detect_init_config(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n")
    print(f"visual-qa: wrote {CONFIG_RELATIVE}")
    print("  review routes, setup, and serve before committing it")
    return 0


def _default_local_url(environ: dict[str, str]) -> str:
    port = environ.get("SC_DEV_PORT", "").strip()
    if not port:
        raise VisualQaError(
            "SC_DEV_PORT is unset; pass `--url http://127.0.0.1:<port>`"
        )
    try:
        _integer(int(port), "SC_DEV_PORT", minimum=1, maximum=65535)
    except (ValueError, VisualQaError) as exc:
        raise VisualQaError(f"SC_DEV_PORT is not a valid port: {port}") from exc
    return f"http://127.0.0.1:{port}"


def cmd_run(
    args: argparse.Namespace,
    *,
    repo: Path = REPO_ROOT,
    environ: dict[str, str] | None = None,
    capture_factory: Callable[[], object] = PlaywrightCapture,
) -> int:
    config = load_config(repo)
    if config is None:
        raise VisualQaError(
            f"visual QA is not configured; run `./sc visual-qa init` to create {CONFIG_RELATIVE}"
        )
    env = dict(os.environ) if environ is None else environ
    base_url = args.url or _default_local_url(env)
    output = Path(args.output)
    if output.is_absolute() or ".." in output.parts or output in (Path("."), Path("")):
        raise VisualQaError("--output must be a non-root path within the fork checkout")
    gallery = repo / output
    prepare_gallery(gallery)
    summary = capture_gallery(
        config, base_url, gallery, capture_factory=capture_factory
    )
    failed = int(summary["routes_failed"])
    total = int(summary["routes_total"])
    print(
        f"visual-qa: gallery written to {gallery} ({total - failed}/{total} routes served)"
    )
    return 1 if summary["outcome"] == "failed" else 0


def _log_tail(path: Path, lines: int = 30) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def cmd_ci(
    _args: argparse.Namespace,
    *,
    repo: Path = REPO_ROOT,
    environ: dict[str, str] | None = None,
    changed_paths: Callable[..., list[str] | None] = pr_changed_paths,
    installer: Callable[[], None] = install_playwright,
    app_context: Callable[..., object] = ci_app,
    capture_factory: Callable[[], object] = PlaywrightCapture,
) -> int:
    env = dict(os.environ) if environ is None else environ
    gallery = repo / DEFAULT_GALLERY
    prepare_gallery(gallery)
    try:
        config = load_config(repo)
    except VisualQaError as exc:
        summary = result_summary(gallery, "failed", error=str(exc))
        publish_result(summary, environ=env)
        print(f"visual-qa: {exc}", file=sys.stderr)
        return 1

    if config is None:
        summary = result_summary(
            gallery,
            "neutral",
            reason="Visual QA is not configured — run `./sc visual-qa init`.",
        )
        publish_result(summary, environ=env)
        print("visual-qa: not configured — neutral pass")
        return 0

    changed = changed_paths(repo, environ=env)
    if should_skip(config["paths"], changed):
        summary = result_summary(
            gallery, "neutral", reason="No configured app paths changed."
        )
        publish_result(summary, environ=env)
        print("visual-qa: no app paths changed — neutral pass")
        return 0

    boot_log = gallery / "boot.log"
    try:
        installer()
        with boot_log.open("w") as log:
            with app_context(config, repo, log) as (base_url, _app_env):
                summary = capture_gallery(
                    config,
                    base_url,
                    gallery,
                    capture_factory=capture_factory,
                )
    except Exception as exc:
        error = (
            str(exc) if isinstance(exc, VisualQaError) else f"unexpected error: {exc}"
        )
        tail = _log_tail(boot_log)
        summary = result_summary(gallery, "failed", error=error)
        if tail:
            summary["boot_log_tail"] = tail
            write_gallery(gallery, summary)
        publish_result(summary, environ=env)
        print(f"visual-qa: {error}", file=sys.stderr)
        return 1

    publish_result(summary, environ=env)
    failed = int(summary["routes_failed"])
    total = int(summary["routes_total"])
    print(f"visual-qa: captured {total - failed}/{total} serving routes")
    return 1 if summary["outcome"] == "failed" else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="./sc visual-qa",
        description="Capture advisory viewport screenshots for a fork app.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    ci = commands.add_parser("ci", help="boot the configured app and capture it in CI")
    ci.set_defaults(func=cmd_ci)
    run = commands.add_parser("run", help="capture an already-running local app")
    run.add_argument(
        "--url", help="app base URL (default: http://127.0.0.1:$SC_DEV_PORT)"
    )
    run.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GALLERY,
        help="gallery directory (default: gallery)",
    )
    run.set_defaults(func=cmd_run)
    init = commands.add_parser("init", help="scaffold .sc-state/visual-qa.json")
    init.set_defaults(func=cmd_init)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except VisualQaError as exc:
        print(f"visual-qa: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
