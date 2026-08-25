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
RECEIVER_TYPED_FILESYSTEM_METHODS = {"copy", "open", "remove", "replace", "walk"}
FILESYSTEM_READ_CALLS = {
    "os.access", "os.fstat", "os.lstat", "os.pread", "os.read", "os.readlink",
    "os.stat", "os.walk", "shutil.which",
}
FILESYSTEM_WRITE_CALLS = {
    "os.fchmod", "os.fdopen", "os.fsync", "os.link", "os.open",
    "os.replace", "os.unlink", "os.write", "shutil.copy2",
    "shutil.copytree", "shutil.rmtree",
}
PROCESS_CALLS = {
    "asyncio.create_task", "asyncio.ensure_future", "asyncio.gather",
    "asyncio.new_event_loop", "asyncio.run", "asyncio.set_event_loop",
    "asyncio.sleep", "asyncio.to_thread", "asyncio.wait_for", "os.chdir",
    "os.close", "os.dup2", "os.kill", "os.killpg",
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
    "asyncio.open_connection", "asyncio.start_server",
    "http.server.ThreadingHTTPServer",
    "http.client.HTTPConnection", "http.client.HTTPSConnection",
    "httpx.get", "httpx.post", "requests.delete", "requests.get",
    "requests.patch", "requests.post", "requests.put", "socket.create_connection",
    "socket.socket", "urllib.request.urlopen",
}
DB_CALLS = {
    "db_driver.connect": "direct_db",
    "db_driver.write_transaction": "direct_db_write",
    "sqlite3.connect": "direct_db",
}
DB_NEUTRAL_CALLS = {
    "db_driver.IntegrityError", "db_driver.OperationalError",
    "db_driver.is_busy_error",
}
DB_RECEIVER_NAMES = {"con", "connection", "cur", "dst", "eng", "self.con"}
DB_READ_METHODS = {"fetchall", "fetchmany", "fetchone"}
DB_WRITE_METHODS = {"commit", "executemany", "executescript", "rollback"}
DISCOVERY_CALLS = {
    "os.environ.get": "credential_discovery",
    "os.getcwd": "identity_discovery",
    "os.geteuid": "identity_discovery",
    "os.getpid": "identity_discovery",
    "socket.gethostname": "identity_discovery",
}
BOUNDED_NEUTRAL_RISKY_CALLS = {
    "asyncio.Event", "asyncio.get_running_loop", "http.client.parse_headers",
    "os.fsdecode", "os.path.abspath", "os.path.basename",
    "os.path.commonpath", "os.path.expanduser", "os.path.normpath",
    "os.pathsep.join", "requests.append", "requests.items",
    "shutil.ignore_patterns", "socket.inet_ntoa", "sqlite3.OperationalError",
    "subprocess.CalledProcessError", "subprocess.CompletedProcess",
    "subprocess.TimeoutExpired", "urllib.parse.parse_qs",
    "urllib.parse.unquote", "urllib.parse.urlencode", "urllib.parse.urlparse",
    "urllib.parse.urlsplit", "urllib.parse.urlunsplit",
    "urllib.request.Request",
}
RISKY_ROOTS = {
    "asyncio", "http", "httpx", "multiprocessing", "os", "pty",
    "db_driver", "requests", "shutil", "socket", "sqlite3", "subprocess",
}
AUTHORIZED_LITERAL_SUBCOMMAND_PATHS = {
    ".super-coder/scripts/job.py",
    ".super-coder/scripts/mem.py",
    ".super-coder/scripts/pr_cli.py",
    ".super-coder/scripts/sprint_cli.py",
    ".super-coder/scripts/visual_qa.py",
    ".super-coder/scripts/vm.py",
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
                if name.asname:
                    aliases[name.asname] = name.name
                else:
                    root = name.name.split(".", 1)[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
    return aliases


def _raw_callee(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _raw_callee(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _callee(node: ast.AST, aliases: dict[str, str]) -> str | None:
    raw = _raw_callee(node)
    if raw is None:
        return None
    local, separator, suffix = raw.partition(".")
    imported = aliases.get(local)
    if imported is None:
        return raw
    if raw == imported or raw.startswith(f"{imported}."):
        return raw
    return imported + (f".{suffix}" if separator else "")


def _open_mode(node: ast.Call) -> str:
    candidates = list(node.args[1:2])
    candidates.extend(
        keyword.value for keyword in node.keywords if keyword.arg == "mode"
    )
    for value in candidates:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return "r"


def _risky_classification(callee: str) -> str | None:
    if callee in DB_CALLS:
        return DB_CALLS[callee]
    if callee in DB_NEUTRAL_CALLS:
        return "identity_neutral_read_only"
    if callee in API_CALLS:
        return "api_effect"
    if callee in PROCESS_CALLS:
        return "process_effect"
    if callee in FILESYSTEM_READ_CALLS or callee == "os.path.exists":
        return "filesystem_read"
    if callee == "os.path.realpath":
        return "filesystem_read"
    if callee in FILESYSTEM_WRITE_CALLS or callee == "os.chmod":
        return "filesystem_write"
    if callee in DISCOVERY_CALLS:
        return DISCOVERY_CALLS[callee]
    if callee in BOUNDED_NEUTRAL_RISKY_CALLS:
        return "identity_neutral_read_only"
    return None


def _categories(callee: str, node: ast.Call, *, path_receiver: bool = False) -> set[str]:
    method = callee.rsplit(".", 1)[-1]
    categories: set[str] = set()
    if callee.split(".", 1)[0] in RISKY_ROOTS:
        classification = _risky_classification(callee)
        if classification and classification != "identity_neutral_read_only":
            categories.add(classification)
        return categories
    if callee in DB_CALLS:
        categories.add(DB_CALLS[callee])
    if callee in API_CALLS or method in {"urlopen", "request"}:
        categories.add("api_effect")
    if callee in PROCESS_CALLS or (
        callee.startswith("subprocess.") and method not in {"list2cmdline"}
    ):
        categories.add("process_effect")
    if callee == "open":
        mode = _open_mode(node)
        categories.add("filesystem_write" if set(mode) & set("wax+") else "filesystem_read")
    if method in FILESYSTEM_READ_METHODS and (
        path_receiver or method not in RECEIVER_TYPED_FILESYSTEM_METHODS
    ):
        categories.add("filesystem_read")
    if method in FILESYSTEM_WRITE_METHODS and (
        path_receiver or method not in RECEIVER_TYPED_FILESYSTEM_METHODS
    ):
        categories.add("filesystem_write")
    if callee == "os.open":
        categories.update({"filesystem_read", "filesystem_write"})
    return categories


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.Name, ast.Attribute)):
        raw = _raw_callee(node)
        return {raw} if raw else set()
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in node.elts))
    return set()


def _annotation_is_path(node: ast.AST | None, aliases: dict[str, str]) -> bool:
    return node is not None and _callee(node, aliases) in {"Path", "pathlib.Path"}


def _expression_is_path(
    node: ast.AST, aliases: dict[str, str], path_receivers: set[str]
) -> bool:
    raw = _raw_callee(node)
    if raw in path_receivers:
        return True
    if isinstance(node, ast.Call):
        callee = _callee(node.func, aliases)
        if callee in {"Path", "pathlib.Path"}:
            return True
        if isinstance(node.func, ast.Attribute):
            receiver = _raw_callee(node.func.value)
            return receiver in path_receivers and node.func.attr in {
                "absolute", "expanduser", "resolve", "with_name", "with_stem",
                "with_suffix",
            }
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _expression_is_path(node.left, aliases, path_receivers)
    return False


def _db_execute_category(node: ast.Call) -> str:
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return "direct_db_write"
    sql = node.args[0].value
    if not isinstance(sql, str):
        return "direct_db_write"
    operation = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    return "direct_db_read" if operation in {"EXPLAIN", "PRAGMA", "SELECT"} else "direct_db_write"


def _python_calls(path: Path) -> Iterable[tuple[str, set[str], int, int]]:
    tree = ast.parse(path.read_text())
    aliases = _imports(tree)
    path_receivers: set[str] = set()
    db_receivers = set(DB_RECEIVER_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if _annotation_is_path(argument.annotation, aliases):
                    path_receivers.add(argument.arg)
        if isinstance(node, ast.AnnAssign) and _annotation_is_path(node.annotation, aliases):
            path_receivers.update(_target_names(node.target))
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            targets = (
                set().union(*(_target_names(target) for target in node.targets))
                if isinstance(node, ast.Assign)
                else _target_names(node.target)
            )
            if _expression_is_path(value, aliases, path_receivers):
                before = len(path_receivers)
                path_receivers.update(targets)
                changed |= len(path_receivers) != before
            if isinstance(value, ast.Call):
                callee = _callee(value.func, aliases)
                receiver = (
                    _raw_callee(value.func.value)
                    if isinstance(value.func, ast.Attribute)
                    else None
                )
                if callee in DB_CALLS or (
                    receiver in db_receivers
                    and value.func.attr in {"cursor", "execute"}
                ):
                    before = len(db_receivers)
                    db_receivers.update(targets)
                    changed |= len(db_receivers) != before
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node.func, aliases)
        if not callee:
            continue
        if isinstance(node.func, ast.Attribute):
            receiver = _raw_callee(node.func.value)
            method = node.func.attr
            if receiver in db_receivers:
                if method == "execute":
                    category = _db_execute_category(node)
                    yield f"db_connection.execute.{category.rsplit('_', 1)[-1]}", {category}, node.lineno, node.col_offset
                    continue
                if method in DB_READ_METHODS:
                    yield f"db_connection.{method}", {"direct_db_read"}, node.lineno, node.col_offset
                    continue
                if method in DB_WRITE_METHODS:
                    yield f"db_connection.{method}", {"direct_db_write"}, node.lineno, node.col_offset
                    continue
            path_receiver = _expression_is_path(
                node.func.value, aliases, path_receivers
            )
        else:
            path_receiver = False
        yield callee, _categories(callee, node, path_receiver=path_receiver), node.lineno, node.col_offset


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
    risky_classification: dict[str, str] = {}
    risky_callsites: dict[str, str] = {}
    effect_callsites: dict[str, str] = {}
    unclassified_risky_calls: list[str] = []
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
        for callee, categories, line, column in _python_calls(path):
            root_name = callee.split(".", 1)[0]
            if root_name in RISKY_ROOTS:
                classification = _risky_classification(callee)
                if classification is None:
                    unclassified_risky_calls.append(
                        f"{callee} at {relative}:{line}"
                    )
                    continue
                risky[callee].add(relative)
                risky_classification[callee] = classification
                callsite = f"{relative}:{line}:{column}:{callee}"
                risky_callsites[callsite] = (
                    f"{classification}|"
                    + (
                        "identity_neutral_read_only"
                        if classification == "identity_neutral_read_only"
                        else "dsh_shell_authorized"
                    )
                )
            for category in categories:
                vocabulary[category].add(callee)
                effect_paths[category].add(relative)
            if categories:
                callsite = f"{relative}:{line}:{column}:{callee}"
                effect_callsites[callsite] = (
                    f"{','.join(sorted(categories))}|dsh_shell_authorized"
                )

    if unclassified_risky_calls:
        raise ValueError(
            "unclassified risky calls:\n" + "\n".join(unclassified_risky_calls)
        )

    signal_names = set(sc_signals)
    classified = (
        PRE_PROVENANCE_SELECTORS | CREDENTIAL_SELECTORS | IDENTITY_SELECTORS
    )
    literal_subcommand_policy = {}
    for path, counters in sorted(subparsers.items()):
        default = (
            "dsh_shell_authorized"
            if path in AUTHORIZED_LITERAL_SUBCOMMAND_PATHS
            else "refused"
        )
        literal_subcommand_policy[path] = {
            subcommand: (
                "refused"
                if path == ".super-coder/scripts/job.py"
                and subcommand == "_supervise"
                else default
            )
            for subcommand in counters
        }
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
        "literal_subcommand_policy": literal_subcommand_policy,
        "effect_call_vocabulary": {
            name: sorted(calls) for name, calls in sorted(vocabulary.items())
        },
        "direct_effect_signal_inventory": {
            name: sorted(paths) for name, paths in sorted(effect_paths.items())
        },
        "risky_call_inventory": {
            name: sorted(paths) for name, paths in sorted(risky.items())
        },
        "risky_call_classification": dict(sorted(risky_classification.items())),
        "risky_callsite_inventory": dict(sorted(risky_callsites.items())),
        "effect_callsite_inventory": dict(sorted(effect_callsites.items())),
        "effect_callsite_policy_rule": (
            "every effect, credential-discovery, and identity-discovery callsite "
            "may execute under DSH only after its public route resolves to "
            "dsh_shell_authorized and the complete guard succeeds; bounded neutral "
            "calls are identity_neutral_read_only; refused and neutral routes must "
            "reach zero protected callsites"
        ),
        "effect_detector_vocabulary": {
            "filesystem_read_methods": sorted(FILESYSTEM_READ_METHODS),
            "filesystem_write_methods": sorted(FILESYSTEM_WRITE_METHODS),
            "receiver_typed_filesystem_methods": sorted(
                RECEIVER_TYPED_FILESYSTEM_METHODS
            ),
            "process_calls": sorted(PROCESS_CALLS),
            "api_calls": sorted(API_CALLS),
            "db_calls": dict(sorted(DB_CALLS.items())),
            "db_neutral_calls": sorted(DB_NEUTRAL_CALLS),
            "db_receiver_names": sorted(DB_RECEIVER_NAMES),
            "db_read_methods": sorted(DB_READ_METHODS),
            "db_write_methods": sorted(DB_WRITE_METHODS),
            "discovery_calls": dict(sorted(DISCOVERY_CALLS.items())),
            "bounded_neutral_risky_calls": sorted(BOUNDED_NEUTRAL_RISKY_CALLS),
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
