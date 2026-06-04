#!/usr/bin/env python3
"""Map the host repo into the dr_* catalogue — how the shell reads its repo.

super-coder lives INSIDE a host repo. This walks that repo and records what's
there — files (with language + role), dependencies (from the common manifests),
and env-var names — into the dr_* tables, so the shell queries the catalogue
instead of grepping blind (see the `surface_catalogue` skill).

The catalogue is a DERIVED CACHE of the repo, not authored content: it is NOT
snapshotted. Re-run any time the repo changes:

    make map          # or: python3 .super-coder/scripts/map_repo.py

Idempotent — clears and repopulates dr_*. v1 maps files / deps / env; the
semantic tables (APIs, db, pages) are a later pass.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"

SKIP_DIRS = {".git", "node_modules", ".super-coder", ".venv", "venv",
             "__pycache__", ".svelte-kit", "dist", "build", ".next", "target",
             "vendor", ".claude", ".idea", ".vscode", "coverage", ".pytest_cache"}
MAX_FILES = 20000  # backstop for huge trees; logs if hit

LANG = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".svelte": "Svelte", ".vue": "Vue", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".java": "Java", ".kt": "Kotlin", ".c": "C", ".h": "C", ".cpp": "C++",
    ".cs": "C#", ".php": "PHP", ".swift": "Swift", ".sh": "Shell", ".bash": "Shell",
    ".sql": "SQL", ".md": "Markdown", ".mdx": "Markdown", ".json": "JSON",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI",
    ".css": "CSS", ".scss": "SCSS", ".html": "HTML", ".xml": "XML",
    ".txt": "Text", ".cfg": "Config", ".env": "Env",
}
CODE_LANGS = {"Python", "JavaScript", "TypeScript", "Svelte", "Vue", "Go", "Rust",
              "Ruby", "Java", "Kotlin", "C", "C++", "C#", "PHP", "Swift", "Shell", "SQL"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}


def infer_role(path: str, ext: str, lang: str | None) -> str:
    p = path.lower()
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith(".env"):
        return "env"
    if "test" in p or "spec" in p and lang in CODE_LANGS:
        return "test"
    if ext in (".md", ".mdx", ".rst", ".txt"):
        return "doc"
    if ext in CONFIG_EXTS or name in ("dockerfile", "makefile"):
        return "config"
    if lang in CODE_LANGS:
        return "code"
    return "asset"


def count_lines(p: Path) -> int | None:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def git(*args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def is_source_repo() -> bool:
    """In a fork, .super-coder is infrastructure (skip it). In the super-coder
    SOURCE repo the engine IS the project, so map it too."""
    url = git("remote", "get-url", "origin")
    return bool(url) and url.rstrip("/").split("/")[-1].removesuffix(".git") == "super-coder"


# ── Dependency parsers (best-effort; each guarded) ───────────────────────────

def deps_package_json(p: Path) -> list[tuple]:
    out = []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return out
    for key, kind in (("dependencies", "runtime"), ("devDependencies", "dev")):
        for name, ver in (data.get(key) or {}).items():
            out.append(("npm", name, str(ver), kind, "package.json"))
    return out


def deps_requirements(p: Path) -> list[tuple]:
    out = []
    try:
        for line in p.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*([=<>!~]=?.*)?$", line)
            if m:
                out.append(("pip", m.group(1), (m.group(2) or "").strip(), "runtime", p.name))
    except OSError:
        pass
    return out


def deps_pyproject(p: Path) -> list[tuple]:
    out = []
    try:
        import tomllib
        data = tomllib.loads(p.read_text())
    except Exception:
        return out
    for dep in (data.get("project", {}).get("dependencies") or []):
        name = re.split(r"[=<>!~ \[]", dep, 1)[0]
        out.append(("pip", name, dep[len(name):].strip(), "runtime", "pyproject.toml"))
    poetry = data.get("tool", {}).get("poetry", {})
    for name, ver in (poetry.get("dependencies") or {}).items():
        if name.lower() != "python":
            out.append(("poetry", name, str(ver), "runtime", "pyproject.toml"))
    return out


def deps_go_mod(p: Path) -> list[tuple]:
    out = []
    try:
        for m in re.finditer(r"^\s*([\w./\-]+)\s+(v[\w.\-]+)", p.read_text(), re.M):
            out.append(("go", m.group(1), m.group(2), "runtime", "go.mod"))
    except OSError:
        pass
    return out


def deps_cargo(p: Path) -> list[tuple]:
    out = []
    try:
        import tomllib
        data = tomllib.loads(p.read_text())
    except Exception:
        return out
    for name, ver in (data.get("dependencies") or {}).items():
        out.append(("cargo", name, ver if isinstance(ver, str) else str(ver), "runtime", "Cargo.toml"))
    return out


MANIFESTS = {
    "package.json": deps_package_json, "requirements.txt": deps_requirements,
    "pyproject.toml": deps_pyproject, "go.mod": deps_go_mod, "Cargo.toml": deps_cargo,
}
ENV_FILES = (".env.example", ".env.sample", ".env.template", ".env.dist")
ENV_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")


def main() -> int:
    if not DB_PATH.exists():
        sys.exit("map_repo: no DB — run `make rebuild` (or `make install`) first.")
    con = sqlite3.connect(DB_PATH)
    skip = SKIP_DIRS - ({".super-coder"} if is_source_repo() else set())
    try:
        for t in ("dr_repo", "dr_filepath", "dr_dependency", "dr_env"):
            con.execute(f"DELETE FROM {t}")

        files = deps = envs = 0
        truncated = False
        for p in sorted(REPO_ROOT.rglob("*")):
            rel_parts = p.relative_to(REPO_ROOT).parts
            if any(part in skip for part in rel_parts):
                continue
            if not p.is_file():
                continue
            if files >= MAX_FILES:
                truncated = True
                break
            rel = "/".join(rel_parts)
            ext = p.suffix.lower()
            lang = LANG.get(ext)
            role = infer_role(rel, ext, lang)
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            con.execute(
                "INSERT INTO dr_filepath (path, ext, lang, role, bytes, lines) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rel, ext or None, lang, role, size,
                 count_lines(p) if (lang or role in ("doc", "config")) else None))
            files += 1

            name = p.name
            if name in MANIFESTS:
                for row in MANIFESTS[name](p):
                    con.execute(
                        "INSERT INTO dr_dependency (manager, name, version, kind, source_file) "
                        "VALUES (?, ?, ?, ?, ?)", row)
                    deps += 1
            if name in ENV_FILES:
                for line in p.read_text(errors="ignore").splitlines():
                    m = ENV_RE.match(line)
                    if m:
                        con.execute("INSERT INTO dr_env (name, source_file) VALUES (?, ?)",
                                    (m.group(1), rel))
                        envs += 1

        con.execute(
            "INSERT INTO dr_repo (repo_id, name, root, remote, vcs, default_branch, "
            "file_count, mapped_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (REPO_ROOT.name, str(REPO_ROOT), git("remote", "get-url", "origin"),
             "git" if (REPO_ROOT / ".git").exists() else None,
             git("rev-parse", "--abbrev-ref", "HEAD"), files,
             datetime.now().isoformat(timespec="seconds")))
        con.commit()
        msg = f"map_repo: {files} files, {deps} deps, {envs} env vars → dr_* ({REPO_ROOT.name})"
        if truncated:
            msg += f"  ⚠ stopped at MAX_FILES={MAX_FILES}"
        print(msg)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
