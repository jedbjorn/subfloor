"""Install the engine-owned user-global boot pointers declared by adapters."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SENTINEL = "<!-- managed by super-coder — hand edits are overwritten -->"


def _managed_content(template: str) -> str:
    """Insert the ownership sentinel as the template's second line."""
    lines = template.splitlines(keepends=True)
    if not lines:
        raise ValueError("global pointer template is empty")
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    return f"{lines[0]}{SENTINEL}{newline}{''.join(lines[1:])}"


def _target_path(
    adapter: dict,
    relative: Path,
    home: Path,
    environ: Mapping[str, str],
) -> Path:
    """Resolve one HOME-relative declaration, including Codex's home override."""
    if (
        adapter.get("harness") == "codex"
        and environ.get("CODEX_HOME")
        and relative.parts
        and relative.parts[0] == ".codex"
    ):
        return Path(environ["CODEX_HOME"]).expanduser() / Path(*relative.parts[1:])
    return home / relative


def _atomic_replace(path: Path, content: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _backup_unmanaged(path: Path, content: bytes) -> Path | None:
    backup = path.with_name(f"{path.name}.pre-sc.bak")
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    return backup


def write_global_pointers(
    engine: Path = ENGINE,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """Converge every declared pointer without blocking install or launch.

    Missing config directories mean that harness is not active on this host and
    are skipped. Existing unmanaged files are adopted only after a one-time
    backup; symlinks are never followed or replaced.
    """
    env = os.environ if environ is None else environ
    if env.get("IS_SANDBOX") or env.get("SC_SANDBOX"):
        return []

    if home is None:
        raw_home = env.get("HOME")
        if not raw_home:
            return []
        root = Path(raw_home).expanduser()
    else:
        root = home
    try:
        desired = _managed_content(
            (engine / "templates" / "global_pointer.md").read_text()
        ).encode()
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"  ⚠ global pointer: template unavailable ({exc})")
        return []

    written: list[Path] = []
    for config_path in sorted((engine / "adapters").glob("*/adapter.json")):
        try:
            adapter = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ⚠ global pointer: skipped {config_path} ({exc})")
            continue

        declared = adapter.get("global_pointer")
        if not declared:
            continue
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts:
            print(f"  ⚠ global pointer: invalid HOME-relative path {declared!r}")
            continue

        target = _target_path(adapter, relative, root, env)
        if not target.parent.is_dir():
            continue
        if target.is_symlink():
            print(f"  ⚠ global pointer: left symlink untouched → {target}")
            continue

        try:
            existing = target.read_bytes() if target.exists() else None
            if existing == desired:
                continue
            if existing is not None:
                lines = existing.splitlines()
                managed = len(lines) > 1 and lines[1].decode(errors="replace") == SENTINEL
                if not managed:
                    backup = _backup_unmanaged(target, existing)
                    if backup is not None:
                        print(f"  → global pointer: adopted {target}; backup → {backup}")
            _atomic_replace(target, desired)
        except OSError as exc:
            print(f"  ⚠ global pointer: skipped {target} ({exc})")
            continue
        written.append(target)
    return written
