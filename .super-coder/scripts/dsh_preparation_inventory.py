#!/usr/bin/env python3
"""Render the exact-base DSH command, authority, and effect inventory."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

SC_SIGNAL = re.compile(r"\bSC_[A-Z][A-Z0-9_]*\b")
FILESYSTEM_READ_METHODS = {
    "access", "exists", "glob", "is_dir", "is_file", "is_symlink",
    "iterdir", "lstat", "open", "read_bytes", "read_text", "readlink",
    "resolve", "rglob", "samefile", "scandir", "stat", "walk",
}
FILESYSTEM_WRITE_METHODS = {
    "chmod", "chown", "copy", "copy2", "copyfile", "copymode",
    "copystat", "hardlink_to", "link", "makedirs", "mkdir", "move",
    "remove", "removedirs", "rename", "renames", "replace", "rmdir",
    "rmtree", "symlink", "symlink_to", "touch", "unlink", "write_bytes",
    "write_text",
}
PROCESS_CALLS = {
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "multiprocessing.Process", "os.exec", "os.execl", "os.execle",
    "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp",
    "os.execvpe", "os.fork", "os.forkpty", "os.popen", "os.posix_spawn",
    "os.posix_spawnp", "os.spawnl", "os.spawnle", "os.spawnlp",
    "os.spawnlpe", "os.spawnv", "os.spawnve", "os.spawnvp",
    "os.spawnvpe", "os.system", "pty.spawn", "subprocess.Popen",
    "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput", "subprocess.run",
}
API_CALLS = {
    "http.client.HTTPConnection", "http.client.HTTPSConnection",
    "httpx.get", "httpx.post", "requests.delete", "requests.get",
    "requests.patch", "requests.post", "requests.put", "socket.create_connection",
    "urllib.request.urlopen",
}
DB_CALLS = {"sqlite3.connect"}
RISKY_ROOTS = {
    "asyncio", "http", "httpx", "multiprocessing", "os", "pty",
    "requests", "shutil", "socket", "sqlite3", "subprocess", "urllib",
}
PRE_PROVENANCE_SELECTORS = {"SC_CALLER_ROOT", "SC_DISPATCH"}
CREDENTIAL_SELECTORS = {
    "SC_API_BASE", "SC_API_TOKEN", "SC_DATABASE_URL", "SC_GH_TOKEN",
    "SC_MEM_AS", "SC_MEM_CREDENTIAL_FILE", "SC_MEM_CRED_DIR", "SC_RO_DSN",
    "SC_RO_ENVFILE",
}
IDENTITY_SELECTORS = {
    "SC_ADMIN", "SC_ENGINE_DIR", "SC_GID", "SC_HARNESS", "SC_ROOT",
    "SC_SHELL_FLAVOR", "SC_SHELL_ID", "SC_SHELL_SHORTNAME",
    "SC_SHELL_WORKTREE", "SC_UID", "SC_USER",
}
SHELL_EFFECT_PATTERNS = {
    "api_effect": re.compile(r"\b(?:curl|wget)\b"),
    "direct_db": re.compile(r"\bsqlite3\b"),
    "filesystem_read": re.compile(
        r"\b(?:cat|cd|dirname|find|pwd|readlink|realpath|stat|test)\b|"
        r"\[\s+-(?:[defrL])\s"
    ),
    "filesystem_write": re.compile(
        r"\b(?:chmod|chown|cp|install|ln|mkdir|mv|rm|touch)\b|(?:^|[^<])>>?"
    ),
    "process_effect": re.compile(
        r"\b(?:bash|curl|docker|exec|git|make|node|pm2|python3?|sh|sudo|systemctl)\b"
    ),
}


def source_files(root: Path) -> list[Path]:
    files = [root / "sc"]
    for base in (root / ".super-coder/scripts", root / ".super-coder/api"):
        files.extend(
            path for path in base.rglob("*")
            if path.is_file() and path.suffix in {".py", ".sh"}
        )
    return sorted(set(files))


def _imports(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split(".")[0]] = name.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
    return aliases


def _callee(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _callee(node.value, aliases)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _open_mode(node: ast.Call) -> str:
    candidates = list(node.args[1:2])
    candidates.extend(
        keyword.value for keyword in node.keywords if keyword.arg == "mode"
    )
    for value in candidates:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return "r"


def _categories(callee: str, node: ast.Call) -> set[str]:
    method = callee.rsplit(".", 1)[-1]
    categories: set[str] = set()
    if callee in DB_CALLS:
        categories.add("direct_db")
    if callee in API_CALLS or method in {"urlopen", "request"}:
        categories.add("api_effect")
    if callee in PROCESS_CALLS or (
        callee.startswith("subprocess.") and method not in {"list2cmdline"}
    ):
        categories.add("process_effect")
    if callee == "open":
        mode = _open_mode(node)
        categories.add("filesystem_write" if set(mode) & set("wax+") else "filesystem_read")
    if method in FILESYSTEM_READ_METHODS:
        categories.add("filesystem_read")
    if method in FILESYSTEM_WRITE_METHODS:
        categories.add("filesystem_write")
    if callee == "os.open":
        categories.update({"filesystem_read", "filesystem_write"})
    return categories


def _python_calls(path: Path) -> Iterable[tuple[str, set[str]]]:
    tree = ast.parse(path.read_text())
    aliases = _imports(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node.func, aliases)
        if callee:
            yield callee, _categories(callee, node)


def _literal_subparsers(path: Path) -> Counter[str]:
    tree = ast.parse(path.read_text())
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_parser":
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            values.append(node.args[0].value)
    return Counter(values)


def build_inventory(root: Path) -> dict[str, object]:
    files = source_files(root)
    sc_signals: dict[str, set[str]] = defaultdict(set)
    effect_paths: dict[str, set[str]] = defaultdict(set)
    vocabulary: dict[str, set[str]] = defaultdict(set)
    risky: dict[str, set[str]] = defaultdict(set)
    source_hashes: dict[str, str] = {}
    subparsers: dict[str, dict[str, int]] = {}

    for path in files:
        relative = str(path.relative_to(root))
        source = path.read_text()
        source_hashes[relative] = hashlib.sha256(source.encode()).hexdigest()
        for name in SC_SIGNAL.findall(source):
            sc_signals[name].add(relative)
        if path.suffix != ".py":
            for category, pattern in SHELL_EFFECT_PATTERNS.items():
                if pattern.search(source):
                    effect_paths[category].add(relative)
            continue
        counters = _literal_subparsers(path)
        if counters:
            subparsers[relative] = dict(sorted(counters.items()))
        for callee, categories in _python_calls(path):
            root_name = callee.split(".", 1)[0]
            if root_name in RISKY_ROOTS:
                risky[callee].add(relative)
            for category in categories:
                vocabulary[category].add(callee)
                effect_paths[category].add(relative)

    signal_names = set(sc_signals)
    classified = (
        PRE_PROVENANCE_SELECTORS | CREDENTIAL_SELECTORS | IDENTITY_SELECTORS
    )
    return {
        "source_sha256_inventory": source_hashes,
        "direct_sc_signal_inventory": {
            name: sorted(paths) for name, paths in sorted(sc_signals.items())
        },
        "ambient_sc_policy": {
            "pre_provenance_refused": sorted(PRE_PROVENANCE_SELECTORS),
            "credential_selection_refused": sorted(CREDENTIAL_SELECTORS),
            "identity_selection_refused": sorted(IDENTITY_SELECTORS),
            "effect_configuration_refused": sorted(signal_names - classified),
            "classification": "ambient values are refused before protected effect under managed DSH; trusted values may be derived only after the guard",
        },
        "literal_subparser_counters": subparsers,
        "effect_call_vocabulary": {
            name: sorted(calls) for name, calls in sorted(vocabulary.items())
        },
        "direct_effect_signal_inventory": {
            name: sorted(paths) for name, paths in sorted(effect_paths.items())
        },
        "risky_call_inventory": {
            name: sorted(paths) for name, paths in sorted(risky.items())
        },
        "effect_detector_vocabulary": {
            "filesystem_read_methods": sorted(FILESYSTEM_READ_METHODS),
            "filesystem_write_methods": sorted(FILESYSTEM_WRITE_METHODS),
            "process_calls": sorted(PROCESS_CALLS),
            "api_calls": sorted(API_CALLS),
            "db_calls": sorted(DB_CALLS),
            "risky_roots": sorted(RISKY_ROOTS),
            "shell_patterns": {
                name: pattern.pattern
                for name, pattern in sorted(SHELL_EFFECT_PATTERNS.items())
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--contract", type=Path)
    args = parser.parse_args()
    inventory = build_inventory(args.root.resolve())
    if args.contract is not None:
        contract = json.loads(args.contract.read_text())
        contract.pop("direct_identity_signal_inventory", None)
        for name, value in inventory.items():
            contract[name] = value
        args.contract.write_text(f"{json.dumps(contract, indent=2)}\n")
        return 0
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
