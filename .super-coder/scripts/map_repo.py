#!/usr/bin/env python3
"""Map the shell work surface into the dr_* catalogue.

Normally super-coder lives inside the project it maps. An external-work install
instead declares ``work_repo`` in ``instance.json``: this maps that project
while retaining the map database and generated state in the home repo.

The catalogue is a DERIVED CACHE of the repo, not authored content: it is NOT
snapshotted. Re-run any time the repo changes:

    ./sc map          # or: python3 .super-coder/scripts/map_repo.py

Idempotent. dr_repo / dr_dependency / dr_env are wiped + repopulated; dr_filepath
is UPSERTed by path so cartographer-authored `desc` survives the auto-remap hook,
with vanished paths pruned. dr_section (authored) is left untouched, and seeded
from top-level dirs only when empty. v1 maps files / deps / env; per-file
descriptions + sections are the B5 navigation layer.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import artifact_policy  # noqa: E402
from engine_paths import is_generated_install_path  # noqa: E402
import map_db  # noqa: E402 — sibling module in scripts/ (on sys.path for script + importers)
import toml_compat  # noqa: E402

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent


def _resolve_map_root() -> Path:
    """Return the project tree to scan, never the home-state location."""
    import install

    work_repo = install.work_repo()
    candidate = Path(work_repo) if work_repo else None
    return candidate if candidate and candidate.is_dir() else REPO_ROOT


MAP_ROOT = _resolve_map_root()
# Per-fork map tuning, authored by the cartographer (see the `cartographer`
# skill). Tracked fork-owned state, kept OUTSIDE the gitignored engine dir (B7)
# so a wholesale engine refresh never touches it. Absent → built-in defaults
# only. The legacy in-engine path is read as a one-release fallback.
CONFIG_PATH = artifact_policy.map_config_path()
CONFIG_PATH_LEGACY = ENGINE / "map.config.json"

# Built-in defaults. A fork's map.config.json EXTENDS the skip sets and may add
# role_overrides — it never shrinks these (so the engine dirs below stay hidden).
SKIP_DIRS = {".git", ".sc-worktrees", "node_modules", ".super-coder", ".sc-state",
             ".venv", "venv",
             "__pycache__", ".svelte-kit", "dist", "build", ".next", "target",
             "vendor", ".idea", ".vscode", "coverage", ".pytest_cache"}
SKIP_FILES: set[str] = set()
MAX_FILES = 20000  # backstop for huge trees; logs if hit


@dataclass(frozen=True)
class MapRefreshResult:
    files: int
    dependencies: int
    env_vars: int
    truncated: bool
    extractor_summaries: tuple[str, ...]
    map_root: Path
    mapped_at: str

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


def path_is_skipped(
    rel_parts: tuple[str, ...],
    skip_dirs: set[str],
    skip_files: set[str],
) -> bool:
    """One testable exclusion predicate for the derived repo catalogue."""
    relative = PurePosixPath(*rel_parts)
    return (
        is_generated_install_path(relative)
        or any(part in skip_dirs for part in rel_parts)
        or (bool(rel_parts) and rel_parts[-1] in skip_files)
    )


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


def load_config() -> dict:
    """Read the per-fork map.config.json, if any. Shape (all keys optional):
        {"skip_dirs": [...], "skip_files": [...],
         "role_overrides": [{"prefix": "cmd/", "role": "code"},
                            {"glob": "*.proto", "role": "code"}]}
    Malformed config is a warning, not a failure — fall back to defaults so a
    bad edit never breaks the auto-remap hooks."""
    artifact_policy.prepare_local_state()
    tracked = REPO_ROOT / ".sc-state" / "map.config.json"
    path = CONFIG_PATH if CONFIG_PATH.exists() else (
        tracked if tracked.exists() else CONFIG_PATH_LEGACY
    )
    if not path.exists():
        return {}
    try:
        cfg = json.loads(path.read_text())
        if not isinstance(cfg, dict):
            raise ValueError("top-level JSON must be an object")
        return cfg
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"map_repo: ignoring {path.name} ({e}) — using defaults")
        return {}


def apply_role_override(rel: str, role: str, overrides: list[dict]) -> str:
    """First matching override wins. `prefix` matches the repo-relative path;
    `glob` matches the filename (fnmatch). Returns the original role if none
    match or the override is malformed."""
    name = rel.rsplit("/", 1)[-1]
    for ov in overrides:
        if not isinstance(ov, dict) or not ov.get("role"):
            continue
        if "prefix" in ov and rel.startswith(ov["prefix"]):
            return ov["role"]
        if "glob" in ov and fnmatch.fnmatch(name, ov["glob"]):
            return ov["role"]
    return role


def count_lines(p: Path) -> int | None:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def git(*args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(MAP_ROOT), *args],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def is_source_repo() -> bool:
    """In a fork, .super-coder is infrastructure (skip it). In the SOURCE repo
    the engine IS the project, so map it too. Names canonical in
    install.SOURCE_REPO_NAMES (super-coder → subfloor rename: both valid)."""
    from install import SOURCE_REPO_NAMES
    url = git("remote", "get-url", "origin")
    return bool(url) and (url.rstrip("/").split("/")[-1].removesuffix(".git")
                          in SOURCE_REPO_NAMES)


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
    if not toml_compat.AVAILABLE:
        return out
    try:
        data = toml_compat.loads(p.read_text())
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
    if not toml_compat.AVAILABLE:
        return out
    try:
        data = toml_compat.loads(p.read_text())
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


def seed_sections(con: sqlite3.Connection) -> None:
    """Seed dr_section from the repo's top-level directories — ONLY when the table
    is empty, so the cartographer's curated sections (and any loaded from the
    snapshot on rebuild) are never overwritten. Each top-level dir becomes one
    section (`name=dir`, `path_prefix=dir/`, description NULL-until-curated).
    Root-level files match no prefix and surface in the synthetic Repository Root
    group at render time.
    The cartographer renames / merges / splits / describes from here."""
    if con.execute("SELECT COUNT(*) FROM dr_section").fetchone()[0]:
        return
    dirs = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(path, 1, instr(path, '/') - 1) AS top "
        "FROM dr_filepath WHERE instr(path, '/') > 0 ORDER BY top")]
    for i, d in enumerate(dirs):
        con.execute(
            "INSERT OR IGNORE INTO dr_section (name, path_prefix, description, sort_order) "
            "VALUES (?, ?, NULL, ?)", (d, d + "/", i))


def run_extractors(con: sqlite3.Connection, repo_root: Path, cfg: dict) -> list[str]:
    """Run fork-owned extractor plug-ins after the core map pass.

    The engine maps the generic 80% (files/deps/env). The semantic, per-repo
    dimensions — HTTP endpoints, the app DB schema, UI routes — vary by stack, so
    a fork owns them as drop-in modules in `.sc-state/map_extractors/*.py` (kept
    outside the gitignored engine dir, so `./sc update` never clobbers them). The
    cartographer adopts the right one for this repo's stack (reference extractors
    ship in the engine's `templates/map_extractors/`).

    Contract: each module defines `extract(con, repo_root, cfg) -> str`. `con` is
    the live MAP db (dr_filepath is already populated + committed, so an extractor
    reads it to find its inputs); it DELETEs + repopulates its own dr_* table(s),
    like the core does for derived tables. The returned string is a short summary
    for the map log. Each call is guarded — a broken extractor is logged and
    skipped, never failing the map (the auto-remap hook must stay robust)."""
    ext_dir = repo_root / ".sc-state" / "map_extractors"
    if not ext_dir.is_dir():
        return []
    summaries: list[str] = []
    for index, path in enumerate(sorted(ext_dir.glob("*.py"))):
        if path.name.startswith("_"):
            continue
        savepoint = f"extractor_{index}"
        con.execute(f"SAVEPOINT {savepoint}")
        try:
            spec = importlib.util.spec_from_file_location(f"map_ext_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "extract", None)
            if fn is None:
                summaries.append(f"{path.stem}: no extract() — skipped")
                con.execute(f"ROLLBACK TO {savepoint}")
                con.execute(f"RELEASE {savepoint}")
                continue
            result = fn(con, repo_root, cfg) or "ok"
            con.execute(f"RELEASE {savepoint}")
            summaries.append(f"{path.stem}: {result}")
        except Exception as e:  # noqa: BLE001 — an extractor must never fail the map
            con.execute(f"ROLLBACK TO {savepoint}")
            con.execute(f"RELEASE {savepoint}")
            summaries.append(f"{path.stem}: FAILED ({e})")
    con.commit()
    return summaries


def refresh() -> MapRefreshResult:
    # The map lives in its OWN db (.sc-state/map.db), not shell_db.db. connect()
    # creates + schema-applies a fresh one and seeds its authored layer (sections
    # from map_content.sql, or the pre-split engine DB on first run post-split).
    con = map_db.connect()
    cfg = load_config()
    # Config EXTENDS the defaults (never shrinks them); .super-coder stays mapped
    # only in the source repo. role_overrides retag files after default inference.
    skip = (SKIP_DIRS | set(cfg.get("skip_dirs") or [])) - (
        {".super-coder"} if is_source_repo() else set())
    skip_files = SKIP_FILES | set(cfg.get("skip_files") or [])
    overrides = cfg.get("role_overrides") or []
    try:
        # Derived tables with no authored content — wiped + repopulated each run.
        for t in ("dr_repo", "dr_dependency", "dr_env"):
            con.execute(f"DELETE FROM {t}")
        # dr_filepath carries cartographer-authored `desc`, so it is NEVER blind-
        # wiped (a post-checkout hook on a working shell would otherwise destroy
        # it). Track the paths seen this run; UPSERT keeps `desc` for surviving
        # paths, and we prune only the paths that vanished from the repo.
        con.execute("CREATE TEMP TABLE _seen (path TEXT PRIMARY KEY)")

        files = deps = envs = 0
        truncated = False
        for p in sorted(MAP_ROOT.rglob("*")):
            rel_parts = p.relative_to(MAP_ROOT).parts
            if path_is_skipped(rel_parts, skip, skip_files) or not p.is_file():
                continue
            if files >= MAX_FILES:
                truncated = True
                break
            rel = "/".join(rel_parts)
            ext = p.suffix.lower()
            lang = LANG.get(ext)
            role = apply_role_override(rel, infer_role(rel, ext, lang), overrides)
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            con.execute(
                "INSERT INTO dr_filepath (path, ext, lang, role, bytes, lines) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "ext=excluded.ext, lang=excluded.lang, role=excluded.role, "
                "bytes=excluded.bytes, lines=excluded.lines",  # desc untouched → preserved
                (rel, ext or None, lang, role, size,
                 count_lines(p) if (lang or role in ("doc", "config")) else None))
            con.execute("INSERT OR IGNORE INTO _seen (path) VALUES (?)", (rel,))
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

        # Prune paths that vanished from the repo (their authored desc goes with
        # them — correct). Surviving paths kept their desc via the UPSERT above.
        con.execute("DELETE FROM dr_filepath WHERE path NOT IN (SELECT path FROM _seen)")
        con.execute("DROP TABLE _seen")
        seed_sections(con)

        mapped_at = datetime.now().isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO dr_repo (repo_id, name, root, remote, vcs, default_branch, "
            "file_count, mapped_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (MAP_ROOT.name, str(MAP_ROOT), git("remote", "get-url", "origin"),
             "git" if (MAP_ROOT / ".git").exists() else None,
             git("rev-parse", "--abbrev-ref", "HEAD"), files,
             mapped_at))
        con.commit()
        # Fork-owned semantic extractors (endpoints / db schema / routes), if any.
        ext_summaries = run_extractors(con, MAP_ROOT, cfg)
        status = {
            "mapped_at": mapped_at,
            "outcomes": [
                {
                    "module": summary.split(":", 1)[0],
                    "status": (
                        "FAIL" if "FAILED" in summary or "no extract()" in summary
                        else "PASS"
                    ),
                    "summary": summary,
                }
                for summary in ext_summaries
            ],
        }
        artifact_policy.atomic_write_text(
            artifact_policy.map_extractor_status_path(),
            json.dumps(status, indent=2, sort_keys=True) + "\n",
        )
        return MapRefreshResult(
            files=files,
            dependencies=deps,
            env_vars=envs,
            truncated=truncated,
            extractor_summaries=tuple(ext_summaries),
            map_root=MAP_ROOT,
            mapped_at=mapped_at,
        )
    finally:
        con.close()


def main() -> int:
    result = refresh()
    msg = (
        f"map_repo: {result.files} files, {result.dependencies} deps, "
        f"{result.env_vars} env vars → dr_* ({result.map_root.name})"
    )
    if result.truncated:
        msg += f"  ⚠ stopped at MAX_FILES={MAX_FILES}"
    print(msg)
    for summary in result.extractor_summaries:
        print(f"map_repo: extractor {summary}")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
