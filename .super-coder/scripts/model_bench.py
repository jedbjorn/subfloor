#!/usr/bin/env python3
"""Frozen project benchmark configuration and disposable host gate (spec #222).

freeze --config FILE writes <evidence.root>/<digest>/campaign.json.
run --campaign FILE --card ID --route-index N proves one fresh-copy gate and
tears it down. The controller's operation callback is the subsequent cell
execution seam; this core does not dispatch a provider or evaluate a result.
cleanup --campaign FILE --cell ID retries a retained cleanup ledger.

Only CommandRunner executes subprocesses. It removes every SC_* binding and
Git context override, closes stdin, bounds commands, and never uses a shell.
Copies own their XDG state so removing a copy also removes its engine DB.
"""
from __future__ import annotations

import argparse
import ast
import copy
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, build_opener

RUNNER_VERSION = "1"
ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
RESERVED = re.compile(r"\b(?:benchmark|evaluate|rubric|judge|score)\b", re.IGNORECASE)
PUSH_DISABLED = "disabled://bench/no-push"
REDLINES = frozenset((
    "tests_green", "style_trap", "scope_bounded", "commit_on_branch",
    "endpoint_present", "endpoint_param", "endpoint_test_added", "ui_wired",
    "migration_present", "migration_applies",
))
COMMON = ["tests_green", "scope_bounded", "commit_on_branch"]
PRESETS: dict[str, dict[str, Any]] = {
    "T1": {
        "text": "Move the {button_a} in {view_a} from {position_from} to {position_to}, and add a {button_b} beside it that {button_b_action}. Follow the house style at {style_path}. Run the tests and commit on a branch.",
        "kinds": {"view_a": "ref", "button_a": "ref", "button_b": "new", "position_from": "text", "position_to": "text", "button_b_action": "text"},
        "redlines": ["style_trap", *COMMON],
    },
    "T2": {
        "text": "Add a filter to {list_view} so a user can narrow the list by {filter_field}. The API must support the filter; the UI must use it. Follow the house style at {style_path}. Run the tests and commit on a branch.",
        "kinds": {"list_view": "ref", "filter_field": "ref"},
        "redlines": ["endpoint_param", "endpoint_test_added", "ui_wired", "style_trap", *COMMON],
    },
    "T3": {
        "text": "Add a {table_name} table with columns {columns}, the migration that creates it, and a read endpoint at {endpoint_path} that lists its rows. Follow the house style at {style_path}. Add tests. Run the tests and commit on a branch.",
        "kinds": {"table_name": "new", "endpoint_path": "new", "columns": "text"},
        "redlines": ["migration_present", "migration_applies", "endpoint_present", "endpoint_test_added", *COMMON],
    },
    "T4": {
        "text": "Add a {table_name} table with columns {columns} and its migration, a read endpoint at {endpoint_path}, and a {view_b} view that lists the rows with a filter by {filter_field} and a {button_c} that {button_c_action}. Follow the house style at {style_path}. Add tests. Run the tests and commit on a branch.",
        "kinds": {"table_name": "new", "endpoint_path": "new", "view_b": "new", "button_c": "new", "columns": "text", "filter_field": "text", "button_c_action": "text"},
        "redlines": sorted(REDLINES),
    },
}


class BenchError(RuntimeError):
    def __init__(self, message, outcome="invalid"):
        super().__init__(message)
        self.outcome = outcome


class CleanupError(BenchError):
    """A retained copy blocks the campaign until explicit cleanup succeeds."""


def require(condition, message):
    if not condition:
        raise BenchError(message)


def object_keys(value, allowed, required=()):
    require(isinstance(value, dict), "expected an object")
    require(not value.keys() - set(allowed), f"unknown keys: {sorted(value.keys() - set(allowed))}")
    require(not set(required) - value.keys(), f"missing keys: {sorted(set(required) - value.keys())}")
    return value


def line(value, label, limit=1024):
    require(isinstance(value, str) and 0 < len(value) <= limit, f"invalid {label}")
    require(value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value), f"{label} must be one bounded line")
    return value


def argv(value):
    require(isinstance(value, list) and value, "argv must be a nonempty array")
    return [line(arg, "argv item", 8192) for arg in value]


def relative(value, label, *, glob=False):
    value = line(value, label)
    p = PurePosixPath(value)
    require(not p.is_absolute() and ".." not in p.parts and "\\" not in value, f"{label} must stay inside the copy")
    require(not p.parts or p.parts[0] != ".git", f"{label} may not address .git")
    if not glob:
        require(not any(c in value for c in "*?[]"), f"invalid {label}")
    return value


def inside(path, root):
    return path == root or root in path.parents


def digest(value):
    data = value.encode() if isinstance(value, str) else canonical(value).encode()
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def clean_environment(extra=None):
    result = {k: v for k, v in os.environ.items() if not k.startswith(("SC_", "GIT_"))}
    result.update(extra or {})
    require(not any(k.startswith("SC_") for k in result), "SC_* environment contamination")
    return result


class CommandRunner:
    def run(self, args, *, cwd=None, env=None, check=True, timeout=300, input=None):
        try:
            result = subprocess.run(
                [str(a) for a in args], cwd=cwd, env=clean_environment(env),
                input=input, stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BenchError(f"command unavailable or timed out: {args[0]}", "infra_failed") from exc
        if check and result.returncode:
            # Do not copy arbitrary command output (credentials, source, prompts)
            # into errors that an operator might paste into a shared surface.
            raise BenchError(f"command failed ({result.returncode}): {args[0]}", "infra_failed")
        return result

    def git(self, repo, *args, **kwargs):
        return self.run(["git", "-C", str(repo), *args], **kwargs)


def resolve_sha(runner, repo, ref):
    result = runner.git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", check=False)
    require(result.returncode == 0 and SHA.fullmatch(result.stdout.strip()), "unresolvable Git ref")
    return result.stdout.strip()


def private_dir(path):
    # A preexisting public evidence directory is refused, never silently trusted.
    if path.exists():
        require(path.is_dir() and not path.is_symlink(), "private directory is not a real directory")
        info = path.stat()
        require(info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077, "evidence directory must be owner-only")
        return
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def write_json(path, value):
    private_dir(path.parent)
    require(not path.is_symlink(), "refusing symlink output")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".bench-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_route(route):
    object_keys(route, ("harness", "model", "effort"), ("harness", "model"))
    for key, value in route.items():
        line(value, f"route {key}")
        require(not value.startswith("-"), "route may not contain an option")
    require(SAFE_ID.fullmatch(route["harness"]), "invalid harness")


def prove_route(runner, repo, route, *, env=None):
    args = [str(repo / "sc"), "models", "resolve", route["harness"], route["model"], "--json"]
    if route.get("effort"):
        args.extend(["--effort", route["effort"]])
    try:
        proof = json.loads(runner.run(args, cwd=repo, env=env).stdout)
        binding = proof["binding"]
        require(proof.get("ok") is True and proof.get("stale") is False, "route is unavailable or stale")
        require(binding["harness"] == route["harness"] and binding["requested_model"] == route["model"]
                and binding["provider_model"] == route["model"] and binding["native_variant_id"] is None,
                "route mismatch or fallback")
        require(binding["requested_effort"] == route.get("effort")
                and binding["effective_effort"] == route.get("effort"), "route effort mismatch")
    except (ValueError, KeyError, TypeError) as exc:
        raise BenchError("unusable route proof") from exc
    return {"harness": route["harness"], "requested": route["model"], "observed": binding["provider_model"],
            "variant": None, "effort": route.get("effort")}


class Snapshot:
    """Read a commit, never the target working tree; remote scratch is temporary."""
    def __init__(self, runner, repo, ref):
        self.runner, self.repo, self.ref = runner, Path(repo), ref
        self.temporary = None
        self.sha = None

    def __enter__(self):
        if not self.repo.is_absolute():
            self.temporary = tempfile.TemporaryDirectory(prefix="subfloor-bench-freeze-")
            self.repo = Path(self.temporary.name)
            try:
                self.runner.run(["git", "init", "--bare", str(self.repo)])
                self.runner.git(self.repo, "fetch", "--no-tags", "--", self.ref[0], self.ref[1])
                self.ref = "FETCH_HEAD"
            except BaseException:
                self.temporary.cleanup()
                raise
        try:
            self.sha = resolve_sha(self.runner, self.repo, self.ref)
        except BaseException:
            if self.temporary:
                self.temporary.cleanup()
            raise
        return self

    def __exit__(self, *args):
        if self.temporary:
            self.temporary.cleanup()

    def read(self, path, required=True):
        result = self.runner.git(self.repo, "show", f"{self.sha}:{path}", check=False)
        require(not required or result.returncode == 0, f"missing frozen file: {path}")
        return result.stdout if result.returncode == 0 else None

    def contains(self, value):
        path = value.lstrip("/")
        exists = self.runner.git(self.repo, "cat-file", "-e", f"{self.sha}:{path}", check=False)
        if exists.returncode == 0:
            return True
        found = self.runner.git(self.repo, "grep", "-F", "-q", "-e", value, self.sha, "--", check=False)
        require(found.returncode in (0, 1), "cannot verify slot against frozen source")
        return found.returncode == 0


def roots(config, base, runner):
    target = config["target"]["repo"]
    local = Path(target).is_absolute()
    target_path = Path(target).resolve() if local else None
    source = Path(config["engine"]["source"])
    require(source.is_absolute() and source.is_dir(), "engine.source must be an absolute checkout")
    source = source.resolve()
    # Resolve roots before any scratch clone; a root cannot contain a protected
    # checkout either, since cleanup deletes descendants of the root.
    protected = {source}
    if target_path:
        protected.add(target_path)
    for repo in tuple(protected):
        out = runner.git(repo, "worktree", "list", "--porcelain").stdout
        protected.update(Path(x[9:]).resolve() for x in out.splitlines() if x.startswith("worktree "))
    resolved = []
    for key, default in (("copy", ".subfloor-bench"), ("evidence", ".subfloor-bench-runs")):
        entry = config.get(key, {})
        object_keys(entry, ("root",))
        require("root" in entry or target_path is not None, f"{key}.root required for clone URL")
        if "root" in entry:
            path = Path(entry["root"])
        elif target_path is not None:
            path = target_path.parent / default
        else:
            raise BenchError(f"{key}.root required for clone URL")
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        require(path != Path("/") and ".sc-worktrees" not in path.parts, "unsafe root")
        require(not any(inside(path, p) or inside(p, path) for p in protected), "root overlaps target, source, or worktree")
        for parent in (path, *path.parents):
            require(not (parent / ".git").exists(), "root is inside a Git worktree")
        config[key] = {"root": str(path)}
        resolved.append(path)
    require(not inside(resolved[0], resolved[1]) and not inside(resolved[1], resolved[0]), "copy and evidence roots overlap")
    config["engine"]["source"] = str(source)
    if target_path:
        config["target"]["repo"] = str(target_path)
    return target_path


def validate_recipes(config):
    if "dev_kit" in config:
        object_keys(config["dev_kit"], ("deps", "test"), ("deps", "test"))
        for value in config["dev_kit"].values():
            argv(value)
    if "serve" in config:
        serve = object_keys(config["serve"], ("argv", "cwd", "port_env", "health_path", "ready_timeout_s"),
                            ("argv", "cwd", "port_env", "health_path", "ready_timeout_s"))
        argv(serve["argv"])
        relative(serve["cwd"], "serve.cwd")
        require(IDENTIFIER.fullmatch(line(serve["port_env"], "port_env"))
                and not serve["port_env"].startswith("SC_"), "invalid serve port_env")
        require(line(serve["health_path"], "health_path").startswith("/")
                and not serve["health_path"].startswith("//"), "health_path must be local")
        require(type(serve["ready_timeout_s"]) in (int, float) and 0 < serve["ready_timeout_s"] <= 3600, "invalid serve timeout")
    if "migrations" in config:
        migrations = object_keys(config["migrations"], ("path", "apply_argv", "table_check_argv"),
                                 ("path", "apply_argv", "table_check_argv"))
        relative(migrations["path"], "migrations.path")
        argv(migrations["apply_argv"])
        argv(migrations["table_check_argv"])


def freeze_style(config, base, snapshot, target):
    style = object_keys(config["style"], ("kind", "source", "place_at", "trap"), ("kind", "source"))
    require(style["kind"] in ("file", "repo"), "style kind must be file or repo")
    source = line(style["source"], "style.source")
    if style["kind"] == "repo":
        require("place_at" not in style, "repo style does not accept place_at")
        relative(source, "style.source")
        style["source"] = str(PurePosixPath(source))
        mode = snapshot.runner.git(snapshot.repo, "ls-tree", snapshot.sha, "--", style["source"]).stdout.split()
        require(mode and mode[0] in ("100644", "100755"), "repo style must be a regular file at target.ref")
        content = snapshot.read(style["source"])
        style["path"] = style["source"]
    else:
        require("place_at" in style, "file style needs place_at")
        relative(style["place_at"], "style.place_at")
        style["place_at"] = str(PurePosixPath(style["place_at"]))
        require(style["place_at"] != ".", "style.place_at must name a file")
        path = Path(source)
        if not path.is_absolute():
            path = (base / path).resolve()
            require(target is None or not inside(path, target), "relative style source is inside target")
            require(not inside(path, Path(config["copy"]["root"])), "relative style source is inside copy")
        content = path.read_text()
        style["source"] = str(path.resolve())
        style["path"] = style["place_at"]
    require(isinstance(content, str) and 0 < len(content) <= 200000, "invalid style artifact")
    if "trap" in style:
        trap = object_keys(style["trap"], ("forbidden", "required"), ("forbidden", "required"))
        for patterns in trap.values():
            require(isinstance(patterns, list) and patterns, "trap patterns must be nonempty arrays")
            for pattern in patterns:
                try:
                    re.compile(line(pattern, "trap regex", 4096))
                except re.error as exc:
                    raise BenchError("invalid trap regex") from exc
    style["content"] = content
    style["digest"] = digest(content)


def path_allowed(path, patterns):
    """Repo-root-relative glob matching, including zero-directory ** matches."""
    return any(PurePosixPath(path).full_match(pattern) for pattern in patterns)


def freeze_cards(config, snapshot):
    cards = config["cards"]
    require(isinstance(cards, list) and cards, "cards must be a nonempty array")
    ids = set()
    for card in cards:
        object_keys(card, ("id", "preset", "text", "slots", "redlines", "allowed_paths"), ("id", "slots", "allowed_paths"))
        require(SAFE_ID.fullmatch(line(card["id"], "card id")), "invalid card id")
        require(card["id"] not in ids, "duplicate card id")
        ids.add(card["id"])
        require(("preset" in card) != ("text" in card), "exactly one of preset or text is required")
        preset = PRESETS.get(card.get("preset"))
        require("preset" not in card or preset is not None, "unknown preset")
        template = preset["text"] if preset else card["text"]
        line(template, "card text", 16000)
        require(not RESERVED.search(template), "reserved word in card text")
        fields = []
        try:
            for _, field, spec, conversion in string.Formatter().parse(template):
                if field is None:
                    continue
                require(IDENTIFIER.fullmatch(field) and not spec and not conversion, "invalid slot substitution")
                fields.append(field)
        except ValueError as exc:
            raise BenchError("invalid slot syntax") from exc
        require(isinstance(card["slots"], dict), "slots must be an object")
        require("style_path" not in card["slots"], "style_path is runner-filled")
        require(set(card["slots"]) == set(fields) - {"style_path"}, "missing or unused slots")
        values = {"style_path": config["style"]["path"]}
        for name, slot in card["slots"].items():
            object_keys(slot, ("value", "kind"), ("value", "kind"))
            kind, value = slot["kind"], line(slot["value"], "slot value")
            require(kind in ("ref", "new", "text"), "undeclared slot kind")
            require(not preset or preset["kinds"][name] == kind, "preset slot kind mismatch")
            require(not RESERVED.search(value), "reserved word in slot")
            if kind == "ref":
                require(snapshot.contains(value), "ref slot absent at target.ref")
            if kind == "new":
                is_path = bool(re.fullmatch(r"/?[A-Za-z0-9_][A-Za-z0-9_./-]*", value)) and ".." not in PurePosixPath(value).parts
                require(IDENTIFIER.fullmatch(value) or is_path, "new slot is not an identifier or path")
                if preset and name in ("table_name", "button_b", "button_c"):
                    require(IDENTIFIER.fullmatch(value), "new table/button must be an identifier")
                require(not snapshot.contains(value), "new slot already exists at target.ref")
            values[name] = value
        card["text"] = template.format_map(values)
        require(not RESERVED.search(card["text"]), "reserved word after slot substitution")
        card["digest"] = digest(card["text"])
        if "redlines" not in card:
            if preset is None:
                raise BenchError("text cards must declare redlines")
            card["redlines"] = list(preset["redlines"])
        checks = card["redlines"]
        require(isinstance(checks, list) and checks and all(isinstance(x, str) and x in REDLINES for x in checks), "unknown or empty redlines")
        require(len(checks) == len(set(checks)), "duplicate redline")
        require(isinstance(card["allowed_paths"], list) and card["allowed_paths"], "allowed_paths required")
        for pattern in card["allowed_paths"]:
            relative(pattern, "allowed_paths", glob=True)
        if config["style"]["kind"] == "file":
            require(not path_allowed(config["style"]["place_at"], card["allowed_paths"]), "style.place_at overlaps card allowed_paths")
        if any(x.startswith("endpoint_") or x == "ui_wired" for x in checks):
            require("serve" in config, "endpoint redline requires serve")
        if any(x.startswith("migration_") for x in checks) or card.get("preset") in ("T3", "T4"):
            require("migrations" in config, "migration redline requires migrations")
        if "style_trap" in checks:
            require("trap" in config["style"], "style_trap requires trap")
            require("style_path" in fields, "style_trap card must contain {style_path}")


def freeze(config_path, runner=None):
    runner = runner or CommandRunner()
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text())
    object_keys(config, ("target", "engine", "copy", "evidence", "style", "dev_kit", "serve", "migrations", "routes", "judge", "cards", "wall_cap_minutes", "shell"),
                ("target", "engine", "style", "routes", "judge", "cards"))
    object_keys(config["target"], ("repo", "ref"), ("repo", "ref"))
    object_keys(config["engine"], ("source", "ref"), ("source",))
    target_repo = line(config["target"]["repo"], "target.repo")
    target_ref = line(config["target"]["ref"], "target.ref")
    require(not target_ref.startswith("-"), "invalid target ref")
    require(Path(target_repo).is_absolute() or re.match(r"(?:https://|ssh://|git://|file://|git@)", target_repo), "target.repo must be absolute or a clone URL")
    if target_repo.startswith("file://"):
        locator = urlsplit(target_repo)
        require(locator.netloc in ("", "localhost") and not locator.query and not locator.fragment, "invalid local clone URL")
        config["target"]["repo"] = unquote(locator.path)
        require(Path(config["target"]["repo"]).is_absolute(), "local clone URL needs an absolute path")
    target = roots(config, config_path.parent, runner)
    config["shell"] = config.get("shell", "DEV1")
    require(config["shell"] == "DEV1", "shell is fixed to DEV1")
    config["wall_cap_minutes"] = config.get("wall_cap_minutes", 60)
    require(type(config["wall_cap_minutes"]) in (int, float) and 0 < config["wall_cap_minutes"] <= 1440, "invalid wall cap")
    validate_recipes(config)
    require(isinstance(config["routes"], list) and config["routes"], "routes must be nonempty")
    for route in [*config["routes"], config["judge"]]:
        validate_route(route)
    route_ids = [(x["harness"], x["model"]) for x in config["routes"]]
    require(len(set(route_ids)) == len(route_ids), "duplicate model route")
    require((config["judge"]["harness"], config["judge"]["model"]) not in route_ids, "judge appears in routes")
    if target:
        require(not runner.git(target, "status", "--porcelain").stdout.strip(), "target checkout is dirty")
    engine_source = Path(config["engine"]["source"])
    engine_ref = config["engine"].get("ref")
    if engine_ref is None and target and (target / ".sc-state/engine.ref").exists():
        engine_ref = (target / ".sc-state/engine.ref").read_text().strip()
    require(engine_ref is None or isinstance(engine_ref, str) and SHA.fullmatch(engine_ref), "engine.ref must be a full SHA")
    engine_sha = resolve_sha(runner, engine_source, engine_ref or "origin/main")
    require(runner.git(engine_source, "merge-base", "--is-ancestor", engine_sha, "origin/main", check=False).returncode == 0, "engine.ref is outside engine.source origin/main")
    config["engine"]["ref"] = engine_sha
    snapshot_ref = target_ref if target else (target_repo, target_ref)
    with Snapshot(runner, str(target) if target else "remote", snapshot_ref) as snapshot:
        config["target"]["ref"] = snapshot.sha
        declaration = snapshot.read(".subfloor/dev-kit.json", required=False)
        if declaration is not None:
            kit = json.loads(declaration)
            require(isinstance(kit, dict) and kit.get("version") == 1 and isinstance(kit.get("hooks"), dict), "invalid target dev-kit")
            for name in ("deps", "test"):
                hook = kit["hooks"].get(name)
                if hook is None:
                    require("dev_kit" in config, f"target has no {name} hook or override")
                    continue
                object_keys(hook, ("argv", "cwd"), ("argv",))
                argv(hook["argv"])
                relative(hook.get("cwd", "."), "hook cwd")
            config["dev_kit_digest"] = digest(declaration)
            config["dev_kit_declaration"] = kit
        else:
            require("dev_kit" in config, "target has no dev-kit or override")
            config["dev_kit_digest"] = digest(config["dev_kit"])
            config["dev_kit_declaration"] = None
        freeze_style(config, config_path.parent, snapshot, target)
        freeze_cards(config, snapshot)
    for route in [*config["routes"], config["judge"]]:
        prove_route(runner, ROOT, route)
    config["runner_version"] = RUNNER_VERSION
    config["runner_digest"] = digest(Path(__file__).read_text())
    campaign_id = digest(config)
    frozen = {"campaign_version": 1, "campaign_id": campaign_id, "config": config}
    root = Path(config["evidence"]["root"])
    private_dir(root)
    path = root / campaign_id / "campaign.json"
    if path.exists():
        require(path.read_text() == canonical(frozen), "campaign digest collision or drift")
    else:
        write_json(path, frozen)
    return path


def load_campaign(path, *, cleanup=False):
    path = Path(path).resolve()
    frozen = json.loads(path.read_text())
    object_keys(frozen, ("campaign_version", "campaign_id", "config"), ("campaign_version", "campaign_id", "config"))
    require(frozen["campaign_version"] == 1 and digest(frozen["config"]) == frozen["campaign_id"], "campaign digest mismatch")
    config = frozen["config"]
    require(cleanup or config["runner_version"] == RUNNER_VERSION and config["runner_digest"] == digest(Path(__file__).read_text()), "runner changed since freeze")
    require(path == Path(config["evidence"]["root"]) / frozen["campaign_id"] / "campaign.json", "campaign moved outside frozen evidence root")
    require(digest(config["style"]["content"]) == config["style"]["digest"], "style digest mismatch")
    for card in config["cards"]:
        require(digest(card["text"]) == card["digest"], "card digest mismatch")
    return frozen


def copy_path(config, campaign_id, cell_id):
    require(SHA.fullmatch(campaign_id[:40]) and re.fullmatch(r"[0-9a-f]{64}", campaign_id), "invalid campaign id")
    require(isinstance(cell_id, str) and SAFE_ID.fullmatch(cell_id), "invalid cell id")
    root = Path(config["copy"]["root"])
    path = root / campaign_id / cell_id
    require(path.resolve() == path and inside(path, root), "copy path traverses a symlink")
    return path


def validate_cleanup_roots(config):
    """Cleanup needs path safety, not an available source repo or model route."""
    copy_root = Path(config["copy"]["root"])
    evidence = Path(config["evidence"]["root"])
    protected = [Path(config["engine"]["source"])]
    target = Path(config["target"]["repo"])
    if target.is_absolute():
        protected.append(target)
    for root in (copy_root, evidence):
        require(root.is_absolute() and root.resolve() == root and root != Path("/"), "cleanup root changed")
        require(".sc-worktrees" not in root.parts, "cleanup root is in a Subfloor worktree")
        require(not any(inside(root, p) or inside(p, root) for p in protected), "cleanup root overlaps a protected checkout")
        require(not any((p / ".git").exists() for p in (root, *root.parents)), "cleanup root is inside a Git worktree")
    require(not inside(copy_root, evidence) and not inside(evidence, copy_root), "cleanup roots overlap")


def confined(workspace, name):
    relative(name, "copy artifact")
    path = workspace / name
    require(inside(path.resolve(), workspace), "copy artifact escapes through a symlink")
    return path


def engine_paths(runner, source, ref):
    """Read the pinned engine's manifest as data, without importing its code."""
    raw = runner.git(source, "show", f"{ref}:.super-coder/scripts/engine_manifest.py").stdout
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "ENGINE_PATHS" for x in node.targets):
            paths = ast.literal_eval(node.value)
            require(isinstance(paths, list) and paths, "invalid engine manifest")
            for path in paths:
                relative(path, "engine path")
                require(path == "sc" or path.startswith(".super-coder/"), "unexpected engine manifest path")
            require("sc" in paths and ".super-coder/scripts" in paths, "engine manifest lacks runner/dispatcher")
            return paths
    raise BenchError("pinned engine has no literal ENGINE_PATHS")


class HostBackend:
    """Serving lifecycle implementation, injectable at its command boundary."""
    def __init__(self, runner=None):
        self.runner = runner or CommandRunner()
        self.port_sockets = []

    def environment(self, workspace):
        # No live engine DB or session state escapes cleanup. Provider auth
        # remains at the harness's normal credential location (HOME unchanged).
        return {"XDG_STATE_HOME": str(workspace / ".bench-state")}

    def command(self, workspace, args, **kwargs):
        return self.runner.run(args, cwd=workspace, env=self.environment(workspace), **kwargs)

    def sc(self, workspace, *args, **kwargs):
        return self.command(workspace, [str(workspace / "sc"), *args], **kwargs)

    def preflight(self, config):
        checked = copy.deepcopy(config)
        roots(checked, Path.cwd(), self.runner)
        require(checked["copy"] == config["copy"] and checked["evidence"] == config["evidence"], "frozen roots changed")
        source = Path(config["engine"]["source"])
        ref = config["engine"]["ref"]
        require(resolve_sha(self.runner, source, ref) == ref, "engine ref drift")
        require(self.runner.git(source, "merge-base", "--is-ancestor", ref, "origin/main", check=False).returncode == 0, "engine ref left origin/main")
        target = Path(config["target"]["repo"])
        if target.is_absolute():
            require(resolve_sha(self.runner, target, config["target"]["ref"]) == config["target"]["ref"], "target ref unavailable")
        clean_environment(self.environment(Path(config["copy"]["root"])))

    def clone(self, config, workspace):
        self.runner.run(["git", "clone", "--no-local", "--no-checkout", "--", config["target"]["repo"], str(workspace)])
        self.runner.git(workspace, "remote", "set-url", "--push", "origin", PUSH_DISABLED)
        require(self.runner.git(workspace, "remote", "get-url", "--push", "--all", "origin").stdout.strip() == PUSH_DISABLED, "push URL remains enabled")
        self.runner.git(workspace, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", config["target"]["ref"])
        require(resolve_sha(self.runner, workspace, "HEAD") == config["target"]["ref"], "clone SHA mismatch")
        # Controller files/state are never offered to the Developer as changes.
        exclude = workspace / ".git/info/exclude"
        with exclude.open("a") as stream:
            stream.write("\n/.bench-state/\n/.bench-engine.tar\n")

    def materialize(self, config, workspace):
        source, ref = Path(config["engine"]["source"]), config["engine"]["ref"]
        paths = engine_paths(self.runner, source, ref)
        # Never inherit an instance id, DB, or retired engine file from a
        # tracked target engine. Only the frozen source manifest is executable.
        for name in (".super-coder", ".sc-state"):
            path = workspace / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        remotes = self.runner.git(workspace, "remote").stdout.splitlines()
        if "super-coder" in remotes:
            self.runner.git(workspace, "remote", "remove", "super-coder")
        self.runner.git(workspace, "remote", "add", "super-coder", source.as_uri())
        self.runner.git(workspace, "remote", "set-url", "--push", "super-coder", PUSH_DISABLED)
        self.runner.git(workspace, "fetch", "--no-tags", "super-coder", f"{ref}:refs/remotes/super-coder/main")
        archive = workspace / ".bench-engine.tar"
        self.runner.git(workspace, "archive", "--format=tar", f"--output={archive}", ref, "--", *paths)
        self.command(workspace, ["tar", "-xf", str(archive), "-C", str(workspace)])
        archive.unlink()
        # A source checkout can track its private snapshot; install must still
        # be a fresh fork. Its own parser and reset path own fresh initialization.
        self.runner.git(workspace, "-c", "core.hooksPath=/dev/null", "checkout", "-B", "bench-base")

    def install(self, config, workspace):
        # install.sc_remote() recognizes source-looking URLs before names. Hide
        # the target URL during bootstrap so a Subfloor target cannot be mistaken
        # for the explicit engine source; restore it before any model turn.
        self.runner.git(workspace, "remote", "set-url", "origin", "disabled://bench-target")
        self.command(workspace, [sys.executable, ".super-coder/scripts/install.py", "--force", "--skip-harness-install",
                                 "--runtime", "host", "--username", getpass.getuser()], timeout=900)
        self.runner.git(workspace, "remote", "set-url", "origin", config["target"]["repo"])
        pin = confined(workspace, ".sc-state/engine.ref").read_text().strip()
        require(pin == config["engine"]["ref"], "installed engine pin mismatch")
        require(self.sc(workspace, "engine-ref").stdout.strip() == pin, "callable engine mismatch")
        instance_path = confined(workspace, ".super-coder/instance.json")
        instance = json.loads(instance_path.read_text())
        require(instance.get("runtime") == "host", "installer did not select host runtime")
        ports = []
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.port_sockets.append(sock)
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
        instance.update(port=ports[0], dev_port=ports[1], repo=workspace.name)
        instance_path.write_text(canonical(instance))
        return {"api_port": ports[0], "dev_port": ports[1]}

    def release_ports(self):
        for sock in self.port_sockets:
            sock.close()
        self.port_sockets.clear()

    def launch(self, workspace):
        self.release_ports()
        self.sc(workspace, "launch", timeout=300)

    def health(self, workspace, ports):
        # Disable proxies even when the invoking Admin has HTTP_PROXY set.
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 30
        url = f"http://127.0.0.1:{ports['api_port']}/api/health"
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=2) as response:
                    require(response.geturl() == url, "health redirected outside copy")
                    payload = json.load(response)
                    if response.status == 200 and payload.get("ok") is True:
                        require(payload.get("repo_root") == str(workspace) and payload.get("port") == ports["api_port"], "health belongs to another instance")
                        return
            except (URLError, TimeoutError, ValueError):
                pass
            time.sleep(0.2)
        raise BenchError("copy health failed", "infra_failed")

    def verify(self, workspace):
        # Install has already rendered the fresh instance. Compare that mirror
        # against its sources; render/all needs SC_ADMIN and would mutate it.
        mirror = confined(workspace, ".sc-state/local/renders/skills_sc/README.md")
        require(mirror.is_file() and mirror.stat().st_size > 0, "installed render is missing")
        self.sc(workspace, "render-check")
        # sc verify rebuilds the DB; use a read-only integrity proof instead.
        # This executes only in the disposable copy with its private XDG root.
        program = """import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, '.super-coder/scripts')
import instance_state
path = instance_state.active_database_path(Path('.super-coder').resolve())
if not Path(path).resolve().is_relative_to(Path('.bench-state').resolve()):
    raise SystemExit('DB escaped copy state')
con = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
    raise SystemExit('DB integrity failed')
if con.execute('PRAGMA foreign_key_check').fetchall():
    raise SystemExit('DB foreign keys failed')
if con.execute(\"SELECT COUNT(*) FROM shells WHERE shortname='DEV1' AND flavor='dev' AND COALESCE(is_deleted,0)=0\").fetchone()[0] != 1:
    raise SystemExit('DEV1 flavor failed')
con.close()
print(json.dumps({'db_verified': True, 'developer_verified': True}))
"""
        proof = json.loads(self.command(workspace, [sys.executable, "-c", program]).stdout)
        require(proof == {"db_verified": True, "developer_verified": True}, "DB verification missing")

    def route(self, workspace, route):
        return prove_route(self.runner, workspace, route, env=self.environment(workspace))

    def place_style(self, workspace, style):
        destination = confined(workspace, style["path"])
        if style["kind"] == "file":
            require(not destination.is_dir(), "style.place_at is a directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(style["content"])
            # Even an ignored projected skill becomes a tracked fixture file.
            self.runner.git(workspace, "add", "--force", "--", style["place_at"])
        require(destination.is_file() and digest(destination.read_text()) == style["digest"], "copy style differs from frozen artifact")
        self.runner.git(workspace, "add", "-A")
        self.runner.git(workspace, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Subfloor Bench",
                        "-c", "user.email=noreply@subfloor.invalid", "-c", "commit.gpgsign=false",
                        "commit", "--allow-empty", "-m", "chore: prepare frozen project fixture")
        return {"base_sha": resolve_sha(self.runner, workspace, "HEAD"), "style_digest": style["digest"]}

    def hook(self, config, workspace, name):
        declaration = config["dev_kit_declaration"]
        declared = declaration and declaration["hooks"].get(name)
        if declared:
            # The original declaration stays byte-for-byte in the target copy.
            result = self.sc(workspace, name, check=False, timeout=900)
        else:
            result = self.command(workspace, config["dev_kit"][name], check=False, timeout=900)
        if result.returncode:
            raise BenchError(f"baseline {name} hook failed", "invalid" if name == "test" else "infra_failed")
        return {"exit_status": result.returncode, "hook": declared or {"argv": config["dev_kit"][name], "cwd": "."}}

    def preamble(self, config, workspace, route):
        # The fixture was committed before hooks; hooks must leave it intact.
        style_path = confined(workspace, config["style"]["path"])
        require(style_path.is_file() and digest(style_path.read_text()) == config["style"]["digest"], "baseline hook changed style")
        require(self.runner.git(workspace, "branch", "--show-current").stdout.strip() == "bench-base", "baseline hook changed branch")
        status = self.runner.git(workspace, "status", "--porcelain").stdout.strip()
        require(not status, "baseline Git status is not clean")
        version = self.command(workspace, [route["harness"], "--version"]).stdout.strip()
        line(version, "harness version", 200)
        require(self.runner.git(workspace, "remote", "get-url", "--push", "--all", "origin").stdout.strip() == PUSH_DISABLED, "push URL changed during gate")
        return {"git_status": status, "base_sha": resolve_sha(self.runner, workspace, "HEAD"),
                "target_base_sha": config["target"]["ref"], "default_branch": "bench-base",
                "harness_version": version, "style_digest": config["style"]["digest"],
                "dev_kit_digest": config["dev_kit_digest"], "sc_environment_stripped": True}

    def stop(self, workspace, ledger):
        self.release_ports()
        if ledger.get("launch_attempted"):
            self.sc(workspace, "down", timeout=60)
            # down may report success after a partial launch. Prove no listener
            # remains on either assigned port before allowing deletion.
            for port in ledger["ports"].values():
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    try:
                        sock.bind(("127.0.0.1", port))
                    except OSError as exc:
                        raise CleanupError("copy service port remains bound") from exc

    def remove(self, workspace):
        if workspace.exists():
            require(not workspace.is_symlink(), "refusing symlink copy cleanup")
            shutil.rmtree(workspace)
        require(not workspace.exists(), "copy deletion unconfirmed")


class Controller:
    def __init__(self, backend=None):
        self.backend = backend or HostBackend()

    def _cleanup(self, config, campaign_id, cell_id, ledger_path, ledger):
        workspace = copy_path(config, campaign_id, cell_id)
        require(ledger.get("campaign_id") == campaign_id and ledger.get("cell_id") == cell_id
                and ledger.get("workspace") == str(workspace), "cleanup ledger identity mismatch")
        try:
            if not ledger["cleanup"]["services_stopped"]:
                self.backend.stop(workspace, ledger)
                ledger["cleanup"]["services_stopped"] = True
                write_json(ledger_path, ledger)
            if ledger.get("clone_attempted"):
                self.backend.remove(workspace)
            ledger["cleanup"]["copy_deleted"] = not workspace.exists()
            require(ledger["cleanup"]["copy_deleted"], "copy deletion unconfirmed")
            write_json(ledger_path, ledger)
        except (BenchError, OSError) as exc:
            ledger["cleanup"]["fatal"] = True
            write_json(ledger_path, ledger)
            raise CleanupError("fatal teardown failure; run cleanup before another cell") from exc
        ledger["cleanup"]["fatal"] = False
        write_json(ledger_path, ledger)

    def _locked(self, frozen):
        import fcntl
        from contextlib import contextmanager

        @contextmanager
        def lock():
            directory = Path(frozen["config"]["evidence"]["root"])
            private_dir(directory)
            path = Path(tempfile.gettempdir()) / f"subfloor-bench-{os.getuid()}.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                require(info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode), "unsafe host campaign lock")
                os.fchmod(fd, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise BenchError("another campaign operation is active") from exc
                yield
            finally:
                os.close(fd)
        return lock()

    def cleanup(self, campaign_path, cell_id):
        frozen = load_campaign(campaign_path, cleanup=True)
        config, campaign_id = frozen["config"], frozen["campaign_id"]
        with self._locked(frozen):
            validate_cleanup_roots(config)
            copy_path(config, campaign_id, cell_id)
            ledger_path = Path(campaign_path).parent / cell_id / "gate.json"
            ledger = json.loads(ledger_path.read_text())
            self._cleanup(config, campaign_id, cell_id, ledger_path, ledger)
            return ledger

    def run(self, campaign_path, card_id, route_index=0, cell_id=None, operation=None):
        frozen = load_campaign(campaign_path)
        config, campaign_id = frozen["config"], frozen["campaign_id"]
        require(type(route_index) is int and 0 <= route_index < len(config["routes"]), "invalid route index")
        cards = [card for card in config["cards"] if card["id"] == card_id]
        require(len(cards) == 1, "unknown card id")
        route, card = config["routes"][route_index], cards[0]
        if cell_id is None:
            cell_id = f"route-{route_index}-{card_id}"
            if len(cell_id) > 80:
                cell_id = f"route-{route_index}-{card_id[:48]}-{card['digest'][:8]}"
        workspace = copy_path(config, campaign_id, cell_id)
        campaign_dir = Path(campaign_path).resolve().parent
        ledger_path = campaign_dir / cell_id / "gate.json"
        with self._locked(frozen):
            self.backend.preflight(config)
            # Scan the evidence root, not just this campaign: a prior campaign's
            # failed teardown also blocks the next cell on the same host seat.
            for prior_path in Path(config["evidence"]["root"]).glob("*/*/gate.json"):
                prior = json.loads(prior_path.read_text())
                require(prior.get("campaign_id") != campaign_id or not prior.get("baseline_invalid"), "campaign baseline is invalid; freeze a new campaign")
                cleanup = prior.get("cleanup", {})
                require(cleanup.get("services_stopped") is True and cleanup.get("copy_deleted") is True
                        and not cleanup.get("fatal"), "prior teardown incomplete; run cleanup")
            require(not ledger_path.exists(), "cell already recorded; use a new cell id for a repeat")
            require(not workspace.exists(), "unowned prior copy; refusing deletion")
            workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            ledger = {"gate_version": 1, "campaign_id": campaign_id, "cell_id": cell_id,
                      "workspace": str(workspace), "card_id": card_id, "card_digest": card["digest"],
                      "route": route, "engine_sha": config["engine"]["ref"], "runner_version": RUNNER_VERSION,
                      "stages": [], "outcome": "invalid", "clone_attempted": False, "launch_attempted": False,
                      "cleanup": {"services_stopped": False, "copy_deleted": False, "push_url_disabled": False, "fatal": False}}
            write_json(ledger_path, ledger)
            try:
                ledger["clone_attempted"] = True
                write_json(ledger_path, ledger)
                self.backend.clone(config, workspace)
                ledger["cleanup"]["push_url_disabled"] = True
                self._stage(ledger_path, ledger, "clone")
                self.backend.materialize(config, workspace)
                self._stage(ledger_path, ledger, "materialize")
                ledger["ports"] = self.backend.install(config, workspace)
                self._stage(ledger_path, ledger, "install")
                ledger["launch_attempted"] = True
                write_json(ledger_path, ledger)
                self.backend.launch(workspace)
                self._stage(ledger_path, ledger, "launch")
                self.backend.health(workspace, ledger["ports"])
                self._stage(ledger_path, ledger, "health")
                self.backend.verify(workspace)
                self._stage(ledger_path, ledger, "verify")
                ledger["route_proof"] = self.backend.route(workspace, route)
                self._stage(ledger_path, ledger, "route")
                ledger["fixture"] = self.backend.place_style(workspace, config["style"])
                self._stage(ledger_path, ledger, "style")
                ledger["deps"] = self.backend.hook(config, workspace, "deps")
                self._stage(ledger_path, ledger, "deps")
                ledger["baseline_test"] = self.backend.hook(config, workspace, "test")
                self._stage(ledger_path, ledger, "test")
                ledger["preamble"] = self.backend.preamble(config, workspace, route)
                require(ledger["preamble"]["base_sha"] == ledger["fixture"]["base_sha"], "baseline hook advanced fixture SHA")
                self._stage(ledger_path, ledger, "preamble")
                ledger["outcome"] = "gate_passed"
                write_json(ledger_path, ledger)
                if operation is not None:
                    operation(workspace, config, card, route, ledger)
            except BaseException as exc:
                ledger["outcome"] = exc.outcome if isinstance(exc, BenchError) else "invalid"
                if ledger["stages"][-1:] == ["deps"] and ledger["outcome"] == "invalid":
                    ledger["baseline_invalid"] = True
                write_json(ledger_path, ledger)
                raise
            finally:
                self._cleanup(config, campaign_id, cell_id, ledger_path, ledger)
            return ledger

    @staticmethod
    def _stage(path, ledger, name):
        ledger["stages"].append(name)
        write_json(path, ledger)


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze", help="validate and freeze a configuration")
    freeze_parser.add_argument("--config", required=True, type=Path)
    run_parser = commands.add_parser("run", help="prove a fresh-copy gate and tear it down")
    run_parser.add_argument("--campaign", required=True, type=Path)
    run_parser.add_argument("--card", required=True)
    run_parser.add_argument("--route-index", type=int, default=0)
    run_parser.add_argument("--cell", help="unique cell id for an explicit repeat")
    cleanup_parser = commands.add_parser("cleanup", help="retry a retained teardown")
    cleanup_parser.add_argument("--campaign", required=True, type=Path)
    cleanup_parser.add_argument("--cell", required=True)
    opts = parser.parse_args(args)
    try:
        if opts.command == "freeze":
            print(freeze(opts.config))
        elif opts.command == "run":
            result = Controller().run(opts.campaign, opts.card, opts.route_index, opts.cell)
            print(canonical({"cell_id": result["cell_id"], "outcome": result["outcome"], "cleanup": result["cleanup"]}), end="")
        else:
            result = Controller().cleanup(opts.campaign, opts.cell)
            print(canonical({"cell_id": result["cell_id"], "cleanup": result["cleanup"]}), end="")
    except (BenchError, OSError, ValueError, TypeError, KeyError) as exc:
        # Never print arbitrary config content or subprocess stderr.
        print(f"model_bench: {exc}" if isinstance(exc, BenchError) else f"model_bench: invalid input or unavailable artifact ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
