#!/usr/bin/env python3
"""Windows guest provisioning profile — `.subfloor/winbox.json` resolver.

The fork declares WHAT its Windows test guest needs; the engine ships a
resolved copy of that declaration to the guest and runs `provision.ps1`
against it. Every key is optional, so a fork with no file still adopts a
guest with sane defaults:

    winget_manifest  winget-manifest.json at the repo root when it exists
    checks           []          verification commands, run over ssh
    mcp              true        install + register Windows-MCP in the guest
    mcp_port         8000        guest loopback port for Windows-MCP
    workspace        C:\\SubfloorTest   guest working directory

`load()` resolves and validates; `parse_report()` reads back the single JSON
line `provision.ps1` prints last. Both raise `WinboxConfigError(code, message)`
so callers can surface the engine's structured error vocabulary unchanged.

This module is import-only (no CLI): `vm.py` uses it for the toolchain check
and the push default, and `vm adopt` uses it to build the guest payload.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG_RELATIVE_PATH = Path(".subfloor") / "winbox.json"
DEFAULT_MANIFEST_NAME = "winget-manifest.json"
DEFAULT_WORKSPACE = "C:\\SubfloorTest"
DEFAULT_MCP_PORT = 8000

# A guest path is absolute when it names a drive (C:\...) or a UNC share
# (\\server\share). Anything else would resolve against whatever directory the
# guest session happens to start in, which is not a contract we can keep.
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:\\|\\\\[^\\]+\\)")

_FIELDS = ("winget_manifest", "checks", "mcp", "mcp_port", "workspace")


class WinboxConfigError(Exception):
    """A typed failure resolving or reading a winbox profile."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def defaults(repo_root: Path) -> dict:
    """The profile a fork that declares nothing still gets."""
    manifest = (Path(repo_root) / DEFAULT_MANIFEST_NAME).resolve()
    return {
        "winget_manifest": str(manifest) if manifest.is_file() else None,
        "checks": [],
        "mcp": True,
        "mcp_port": DEFAULT_MCP_PORT,
        "workspace": DEFAULT_WORKSPACE,
        "source": None,
    }


def config_path(repo_root: Path) -> Path:
    return Path(repo_root) / CONFIG_RELATIVE_PATH


def load(repo_root: Path) -> dict:
    """Resolve `.subfloor/winbox.json` under repo_root into a full profile.

    Returns {winget_manifest (absolute path str or None), checks, mcp,
    mcp_port, workspace, source (the file that supplied it, or None)}.
    Raises WinboxConfigError for an unreadable file, a bad shape, or a
    declared winget manifest that is not a file inside the repo.
    """
    root = Path(repo_root).resolve()
    resolved = defaults(root)
    path = config_path(root)
    if not path.is_file():
        return resolved

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WinboxConfigError(
            "winbox_config_unreadable", f"{path} could not be read: {exc}"
        ) from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WinboxConfigError(
            "winbox_config_invalid", f"{path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise WinboxConfigError(
            "winbox_config_invalid", f"{path} must hold a JSON object"
        )
    unknown = sorted(set(raw) - set(_FIELDS))
    if unknown:
        raise WinboxConfigError(
            "winbox_config_invalid",
            f"{path} has unknown key(s): {', '.join(unknown)}; "
            f"known keys: {', '.join(_FIELDS)}",
        )

    resolved["source"] = str(path)
    if "checks" in raw:
        resolved["checks"] = _checks(raw["checks"], path)
    if "mcp" in raw:
        resolved["mcp"] = _flag(raw["mcp"], "mcp", path)
    if "mcp_port" in raw:
        resolved["mcp_port"] = _port(raw["mcp_port"], "mcp_port", path)
    if "workspace" in raw:
        resolved["workspace"] = _workspace(raw["workspace"], path)
    if "winget_manifest" in raw:
        resolved["winget_manifest"] = _manifest(raw["winget_manifest"], root, path)
    return resolved


def guest_payload(profile: dict) -> dict:
    """The winbox.json the host ships INTO the guest.

    The guest never sees host paths: the manifest arrives beside
    provision.ps1 in the workspace, so it is named by basename only.
    """
    manifest = profile.get("winget_manifest")
    return {
        "winget_manifest": Path(manifest).name if manifest else None,
        "checks": list(profile.get("checks") or []),
        "mcp": bool(profile.get("mcp", True)),
        "mcp_port": int(profile.get("mcp_port", DEFAULT_MCP_PORT)),
        "workspace": str(profile.get("workspace") or DEFAULT_WORKSPACE),
    }


def parse_report(stdout: str) -> dict:
    """Extract and validate provision.ps1's trailing JSON report line.

    provision.ps1 may print anything it likes (winget is chatty); the LAST
    line that parses as a JSON object is the report. Absent or malformed is
    a failure, never a silent pass.
    """
    if not isinstance(stdout, str):
        raise WinboxConfigError(
            "winbox_report_missing", "provisioning produced no output to parse"
        )
    report = None
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            report = parsed
            break
    if report is None:
        raise WinboxConfigError(
            "winbox_report_missing",
            "provisioning output held no JSON report line "
            '({"steps":[...],"ok":bool} expected last on stdout)',
        )
    if not isinstance(report.get("ok"), bool):
        raise WinboxConfigError(
            "winbox_report_invalid", "provisioning report 'ok' must be a boolean"
        )
    steps = report.get("steps")
    if not isinstance(steps, list):
        raise WinboxConfigError(
            "winbox_report_invalid", "provisioning report 'steps' must be a list"
        )
    clean_steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WinboxConfigError(
                "winbox_report_invalid",
                f"provisioning report step {index} must be an object",
            )
        name = step.get("name")
        ok = step.get("ok")
        detail = step.get("detail", "")
        if not isinstance(name, str) or not name.strip():
            raise WinboxConfigError(
                "winbox_report_invalid",
                f"provisioning report step {index} needs a non-empty 'name'",
            )
        if not isinstance(ok, bool):
            raise WinboxConfigError(
                "winbox_report_invalid",
                f"provisioning report step '{name}' needs a boolean 'ok'",
            )
        if not isinstance(detail, str):
            raise WinboxConfigError(
                "winbox_report_invalid",
                f"provisioning report step '{name}' 'detail' must be a string",
            )
        clean_steps.append({"name": name, "ok": ok, "detail": detail})
    return {"steps": clean_steps, "ok": bool(report["ok"])}


# -- field validators --------------------------------------------------------

def _checks(value: object, path: Path) -> list[str]:
    if not isinstance(value, list):
        raise WinboxConfigError(
            "winbox_config_invalid", f"{path}: 'checks' must be a list of strings"
        )
    checks = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise WinboxConfigError(
                "winbox_config_invalid",
                f"{path}: checks[{index}] must be a non-empty string",
            )
        checks.append(entry.strip())
    return checks


def _flag(value: object, field: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise WinboxConfigError(
            "winbox_config_invalid", f"{path}: '{field}' must be true or false"
        )
    return value


def _port(value: object, field: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise WinboxConfigError(
            "winbox_config_invalid",
            f"{path}: '{field}' must be an integer 1..65535",
        )
    return value


def _workspace(value: object, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WinboxConfigError(
            "winbox_config_invalid", f"{path}: 'workspace' must be a non-empty string"
        )
    workspace = value.strip()
    if not _WINDOWS_ABSOLUTE.match(workspace):
        raise WinboxConfigError(
            "winbox_config_invalid",
            f"{path}: 'workspace' must be an absolute Windows path "
            f"(C:\\... or \\\\server\\share): {workspace!r}",
        )
    trimmed = workspace.rstrip("\\")
    return trimmed if trimmed else workspace


def _manifest(value: object, root: Path, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WinboxConfigError(
            "winbox_config_invalid",
            f"{path}: 'winget_manifest' must be a non-empty string or null",
        )
    declared = Path(value.strip())
    manifest = declared if declared.is_absolute() else (root / declared)
    manifest = manifest.resolve()
    if not (manifest == root or manifest.is_relative_to(root)):
        raise WinboxConfigError(
            "winbox_config_invalid",
            f"{path}: 'winget_manifest' must resolve inside the repo: {value}",
        )
    if not manifest.is_file():
        raise WinboxConfigError(
            "winbox_manifest_missing",
            f"{path}: declared winget_manifest not found: {manifest}",
        )
    return str(manifest)
