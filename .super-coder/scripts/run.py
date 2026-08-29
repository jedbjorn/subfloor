#!/usr/bin/env python3
"""Launch a shell against this repo.

super-coder is forked into ONE repo, so a shell works the repo root — no
per-shell workdir, no cross-repo cwd confusion (that is the whole inversion).

Flow:
    1. username-only auth (v1: no password challenge — pick a name)
    2. pick a shell (arg shortname · --first · interactive picker)
    3. open a session archive row
    4. compose the boot artifact and dual-write CLAUDE.md + AGENTS.md at root
       (dev-flavor shells: write to their worktree root, not the repo root)
    5. exec the harness  (skipped when RENDER_ONLY=1 — used to verify headless)

Usage:
    python3 .super-coder/scripts/run.py [shortname] [--first]
    python3 .super-coder/scripts/run.py --host-admin [admin-shortname]
    RENDER_ONLY=1 python3 .super-coder/scripts/run.py --first   # render, don't exec

Interactive and headless launches share this direct boot path. `./sc enter`
dispatches here for a human session; `./sc run` supplies `--headless`.

Headless (`./sc run <shortname> [-p "<prompt>"] [--harness <h>] [-m <model>]
[--effort <level>]`):
the same render-then-exec path minus the picker and the TTY. The harness runs
non-interactively via its adapter's `headless` block (claude -p · codex exec ·
opencode run), streams a final message, and exits. Headless launches keep the
inbox-drain default. A liveness
guard refuses a shell whose worktree already hosts a live session (one shell,
one session). Harness + model resolve: explicit flags → the shell's
flavor_defaults.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "render"))
import flat  # noqa: E402
from compose import compose_boot  # noqa: E402

sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import callable_floor  # noqa: E402
import conversation_boot  # noqa: E402
import db_driver  # noqa: E402
import devkit  # noqa: E402
import git_freshness  # noqa: E402
import git_prune  # noqa: E402  — boot-time prune of provably-merged local branches
import global_pointer  # noqa: E402
import install  # noqa: E402  — reuse its canonical HARNESS_BIN (one source of truth)
import opencode_config  # noqa: E402  — one locked owner for opencode.json
import ports as ports_mod  # noqa: E402  — derive the per-fork API base URL
import sandbox_devkit  # noqa: E402  — readiness receipt identity contract
import seed_skills  # noqa: E402  — boot-time self-heal of stale engine skills
import shell_liveness  # noqa: E402  — headless boot's one-shell-one-session guard
import skill_projection  # noqa: E402  — exact bounded harness skill mirrors
import style  # noqa: E402  — launcher ANSI; degrades to plain text off-TTY

sys.path.insert(0, str(ENGINE / "api"))
import model_catalog  # noqa: E402  — HARNESS_PROVIDER: one source for harness → provider
import route_transport  # noqa: E402  — binding-to-harness transport projection

ADAPTERS = ENGINE / "adapters"
PROC_SELF_STAT = Path("/proc/self/stat")   # H-25: our own start ticks, pre-exec

DEFAULT_HEADLESS_PROMPT = "Check your inbox and act on your unread messages."
SESSION_OPEN_RETRY_DELAYS_S = (0.1, 0.3)
VENV_PROBE_TIMEOUT_S = 3
VENV_PROBE_SCRIPT = (
    "import json, sys; "
    "print(json.dumps({'version': list(sys.version_info[:2]), "
    "'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))"
)

DEV_TOOL_HOOKS = ("deps", "test", "lint", "typecheck")
DEV_TOOL_BASELINE = ("curl", "node", "npm", "pytest", "rg", "sqlite3", "uv")


def _dev_tool_status_matches(
    status: dict, declaration: devkit.Declaration, checkout: Path, identity: str
) -> bool:
    """Return whether lifecycle status names this checkout and declaration."""
    declaration_digest = hashlib.sha256(
        declaration.canonical_json.encode()
    ).hexdigest()
    package_digest = sandbox_devkit._declaration_package_digest(declaration)
    try:
        engine_ref = sandbox_devkit._engine_ref(checkout, ENGINE)
    except sandbox_devkit.SandboxImageError:
        return False
    return (
        status.get("format_version") == 1
        and status.get("checkout_identity") == identity
        and status.get("declaration_digest") == declaration_digest
        and status.get("package_digest") == package_digest
        and status.get("engine_ref") == engine_ref
    )


def _dev_tool_receipt_matches(
    status: dict, declaration: devkit.Declaration, checkout: Path, identity: str
) -> bool:
    """Return whether canonical ready evidence matches current tracked inputs."""
    return bool(
        _dev_tool_status_matches(status, declaration, checkout, identity)
        and sandbox_devkit.persisted_readiness_matches(
            checkout, ENGINE, declaration, status
        )
    )


def collect_dev_tools(
    checkout: Path,
    launch_mode: str,
    *,
    repair: bool = False,
    environment: dict[str, str] | None = None,
) -> dict:
    """Collect bounded dev-kit facts for the exact checkout receiving boot."""
    if launch_mode not in {"container", "host"}:
        raise ValueError(f"unsupported launch mode: {launch_mode}")
    environment = dict(os.environ if environment is None else environment)
    checkout = checkout.resolve(strict=True)
    evidence_root = checkout / ".sc-state" / "local" / "dev-kit"
    identity = hashlib.sha256(str(checkout).encode()).hexdigest()
    status_path = evidence_root / identity[:20] / "status.json"
    baseline = (
        {name: "engine-supplied" for name in DEV_TOOL_BASELINE}
        if launch_mode == "container"
        else {
            name: (
                "available"
                if shutil.which(name, path=environment.get("PATH"))
                else "unavailable"
            )
            for name in DEV_TOOL_BASELINE
        }
    )
    port = environment.get("SC_DEV_PORT")
    dev_port = "unavailable"
    if port:
        dev_port = (
            f"127.0.0.1:{port}"
            if launch_mode == "host"
            else f"0.0.0.0:{port} -> 127.0.0.1:{port}"
        )
    database_url = environment.get("DATABASE_URL")
    if database_url is None:
        app_database = "unavailable"
    elif not database_url.strip():
        app_database = "invalid (empty DATABASE_URL)"
    else:
        app_database = "configured (URL withheld)"
    common = {
        "checkout": str(checkout),
        "seat": launch_mode,
        "evidence": str(status_path if status_path.exists() else evidence_root),
        "logs": str(checkout / ".sc-state" / "local" / "devkit-logs"),
        "baseline": baseline,
        "dev_port": dev_port,
        "app_database": app_database,
    }
    if repair:
        return {
            **common,
            "state": "repair",
            "declaration": "`.subfloor/dev-kit.json` (repair seat; no readiness claim)",
            "hooks": {},
            "sandbox": "unknown during repair",
            "provision": "repair in progress",
        }

    try:
        declaration = devkit.load_declaration(checkout)
    except devkit.DevkitConfigError as exc:
        return {
            **common,
            "state": "invalid",
            "detail": f"Declaration validation failed: {exc}",
            "declaration": "`.subfloor/dev-kit.json` (invalid)",
            "hooks": {},
            "sandbox": "unavailable",
            "provision": "unavailable",
        }
    if declaration is None:
        return {
            **common,
            "state": "absent",
            "declaration": "`.subfloor/dev-kit.json` (absent)",
            "hooks": {},
            "sandbox": "absent",
            "provision": "absent",
        }

    hooks = {}
    available_hooks = 0
    for name in DEV_TOOL_HOOKS:
        hook = declaration.hooks.get(name)
        if hook is None:
            continue
        executable = hook.resolved_executable
        if executable is None and launch_mode == "container":
            available = hook.executable in DEV_TOOL_BASELINE
        elif executable is None:
            found = shutil.which(hook.executable, path=environment.get("PATH"))
            executable = Path(found) if found else None
            available = bool(executable)
        else:
            available = bool(executable.is_file() and os.access(executable, os.X_OK))
        available_hooks += int(available)
        hooks[name] = {
            "state": "configured" if available else "unavailable",
            "cwd": hook.cwd_declared,
            "executable": hook.executable,
        }

    sandbox = declaration.sandbox
    sandbox_state = "absent"
    if sandbox is not None:
        parts = []
        if sandbox.has_extension:
            parts.append(f"declared (`{sandbox.dockerfile_declared}`)")
        if sandbox.packages is not None:
            parts.append("native packages declared")
        sandbox_state = "; ".join(parts) or "declared (no extension)"
    provision_state = (
        f"declared via `{declaration.provision.hook}`"
        if declaration.provision is not None
        else "absent"
    )
    state = "ready" if hooks and available_hooks == len(hooks) else "declared"
    needs_receipt = bool(
        launch_mode == "container"
        and (
            declaration.provision is not None
            or (sandbox is not None and (sandbox.has_extension or sandbox.packages is not None))
        )
    )
    if needs_receipt:
        status = None
        if status_path.is_file():
            try:
                status = json.loads(status_path.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError):
                state = "failed"
        if status is None and state != "failed":
            state = "stale"
        elif status is not None:
            current_status = _dev_tool_status_matches(
                status, declaration, checkout, identity
            )
            if not current_status:
                state = "stale"
            elif status.get("native_packages") == "advisory":
                state = "advisory"
            elif status.get("state") == "failed" or status.get("core_runtime") == "failed":
                state = "failed"
            elif status.get("fork_readiness") == "ready" and _dev_tool_receipt_matches(
                status, declaration, checkout, identity
            ):
                state = "ready"
            else:
                state = "stale"

    return {
        **common,
        "state": state,
        "declaration": "`.subfloor/dev-kit.json` (valid)",
        "hooks": hooks,
        "sandbox": sandbox_state,
        "provision": provision_state,
    }


class VenvEligibility(NamedTuple):
    bin_dir: Path | None
    failure: str | None


def _headless_effort_args(hcfg: dict, effort: "str | None",
                          harness: str = "?") -> list[str]:
    if not effort or effort == route_transport.route_bindings.DEFAULT_EFFORT:
        # Model default: no effort transport — the harness default governs.
        return []
    ecfg = hcfg.get("effort") or {}
    if ecfg.get("flag"):
        return [ecfg["flag"], effort]
    if ecfg.get("config_flag") and ecfg.get("config_key"):
        return [ecfg["config_flag"], f'{ecfg["config_key"]}="{effort}"']
    if ecfg.get("env"):
        return []
    raise ValueError(
        f"harness '{harness}' cannot apply effort '{effort}'")


def headless_effort_env(adapter: dict, effort: "str | None") -> dict[str, str]:
    ecfg = ((adapter.get("headless") or {}).get("effort") or {})
    if not effort or effort == route_transport.route_bindings.DEFAULT_EFFORT:
        return {}
    return {ecfg["env"]: effort} if ecfg.get("env") else {}


def default_headless_effort(adapter: dict) -> "str | None":
    """Use high only when the adapter has an effort transport."""
    if adapter.get("harness") in route_transport.route_bindings.LIVE_NATIVE_HARNESSES:
        return None
    return "high" if ((adapter.get("headless") or {}).get("effort")) else None


class ResolvedHeadlessRoute(NamedTuple):
    harness: str
    provider: str | None
    model: str | None
    effort: str | None


def resolve_headless_route(
    *,
    harness: str,
    adapter: dict,
    flavor_model: "str | None",
    model: "str | None",
    effort: "str | None",
) -> ResolvedHeadlessRoute:
    """Resolve and validate the immutable route stored for a headless turn.

    Explicit model and effort strings are validated without normalization so
    the caller's selected bytes remain part of the durable route.  Defaults
    are applied here, before any request is hashed or conversation is stored.
    """
    if not isinstance(harness, str) or not harness.strip():
        raise ValueError("harness must be a non-empty string")
    resolved_model = model if model is not None else flavor_model
    if not isinstance(resolved_model, str) or not resolved_model.strip():
        raise ValueError(
            f"harness '{harness}' cannot resolve a model: no model was supplied "
            "and no flavor default exists for it; supply an explicit model"
        )
    if effort is not None and (
        not isinstance(effort, str) or not effort.strip()
    ):
        raise ValueError("effort must be a non-empty string when supplied")
    resolved_effort = (
        effort if effort is not None else default_headless_effort(adapter)
    )
    validate_headless_request(adapter, resolved_model, resolved_effort)
    return ResolvedHeadlessRoute(
        harness=harness,
        provider=session_provider(harness, resolved_model),
        model=resolved_model,
        effort=resolved_effort,
    )


def resolve_bound_headless_route(
    *,
    harness: str,
    model: "str | None",
    effort: "str | None",
    binding: dict,
    binding_digest: str,
) -> ResolvedHeadlessRoute:
    """Consume one already-resolved binding without applying defaults."""
    route_transport.route_bindings.validate_binding(binding)
    if route_transport.route_bindings.digest_json(binding) != binding_digest:
        raise route_transport.route_bindings.RouteResolutionError(
            "thinking_evidence_missing",
            "Route binding digest does not match its canonical content",
            {},
        )
    if (
        binding["harness"] != harness
        or binding["requested_model"] != model
        or binding["requested_effort"] != effort
    ):
        raise ValueError(
            "versioned route binding disagrees with the requested "
            "harness, model, or effort"
        )
    return ResolvedHeadlessRoute(
        harness=harness,
        provider=session_provider(harness, binding["requested_model"]),
        model=binding["requested_model"],
        effort=binding["effective_effort"],
    )


def validate_headless_request(adapter: dict, model: "str | None",
                              effort: "str | None") -> None:
    hcfg = adapter.get("headless") or {}
    harness = adapter.get("harness", "?")
    if not hcfg.get("launch") and not hcfg.get("engine_script"):
        raise ValueError(f"harness '{harness}' has no headless adapter")
    if model and not hcfg.get("model_flag"):
        raise ValueError(
            f"harness '{harness}' cannot apply requested model '{model}'")
    _headless_effort_args(hcfg, effort, harness)


def headless_command(
    adapter: dict,
    prompt: str,
    model: "str | None" = None,
    sandbox_flags: "list[str] | None" = None,
    effort: "str | None" = None,
    transport: "route_transport.TransportProjection | None" = None,
    conversation_owned: bool = False,
) -> "list[str] | None":
    """The non-interactive exec argv from the adapter's `headless` block —
    launch prefix + model flag + sandbox flags + the prompt as the final
    positional. None when the harness declares no headless block (e.g. vibe,
    which takes no model from the launch seam — see the spec's non-goals)."""
    hcfg = adapter.get("headless")
    if (
        conversation_owned
        and (adapter.get("surfaces") or {}).get("browser") is True
        and (adapter.get("conversation") or {}).get("driver")
    ):
        return []
    if not hcfg or not (hcfg.get("launch") or hcfg.get("engine_script")):
        return None
    if transport is not None:
        if transport.harness != adapter.get("harness"):
            raise ValueError("route transport does not match the selected harness")
        model = transport.model
        effort = transport.effort
    validate_headless_request(adapter, model, None if transport else effort)
    engine_script = hcfg.get("engine_script")
    if engine_script:
        if (
            not isinstance(engine_script, str)
            or Path(engine_script).name != engine_script
            or not engine_script.endswith(".py")
        ):
            raise ValueError("headless engine_script must be one Python basename")
        script_path = ENGINE / "scripts" / engine_script
        if not script_path.is_file():
            raise ValueError(f"headless engine script is missing: {engine_script}")
        cmd = [sys.executable, str(script_path)]
    else:
        cmd = list(hcfg["launch"])
    managed_mcp = managed_mcp_injection(adapter)
    if managed_mcp:
        cmd += list(managed_mcp.get("launch_args") or [])
    if model:
        cmd += [hcfg["model_flag"], model]
    cmd += (
        list(transport.argument_tail)
        if transport is not None
        else _headless_effort_args(hcfg, effort, adapter.get("harness", "?"))
    )
    cmd += list(sandbox_flags or [])
    if hcfg.get("prompt_flag"):
        cmd += [hcfg["prompt_flag"], prompt]
    else:
        cmd.append(prompt)
    return cmd


def load_adapter(harness: str) -> dict:
    """The harness-specific seam (adapters/<harness>/adapter.json): launch argv,
    which files to emit at the repo root, and extra launch env."""
    path = ADAPTERS / harness / "adapter.json"
    if not path.is_file():
        raise ValueError("harness selector is not shipped")
    return json.loads(path.read_text())


def require_harness_surface(adapter: dict, surface: str) -> None:
    """Reject a manifest's explicit unsupported surface before launch setup."""
    declared = adapter.get("surfaces")
    if isinstance(declared, dict) and declared.get(surface) is False:
        harness = adapter.get("harness", "unknown")
        label = surface.replace("_", "-")
        raise ValueError(f"harness '{harness}' does not support {label}")


def interactive_launch(adapter: dict) -> dict | None:
    """Return the adapter's supported interactive launch contract."""
    surfaces = adapter.get("surfaces") or {}
    if surfaces.get("terminal") is True:
        return {
            "kind": "terminal",
            "launch": adapter.get("launch") or [adapter.get("harness", "unknown")],
        }
    return None


def linked_vm_configured() -> bool:
    """Match ``sc_vm_broker_configured``: a truthy persisted vm block."""
    return bool(ports_mod.resolve(persist=False).get("vm"))


def managed_mcp_injection(adapter: dict) -> dict | None:
    """Return one adapter's validated managed streamable-HTTP MCP recipe.

    The adapter owns the harness-specific representation. The launcher only
    consumes optional argv and JSON-merge fragments, so adding a harness never
    grows a harness-name switch here.
    """
    if not linked_vm_configured():
        return None
    streamable = (adapter.get("mcp") or {}).get("streamable_http") or {}
    if not streamable.get("supported"):
        return None
    managed = streamable.get("managed_server")
    if not isinstance(managed, dict):
        raise ValueError(
            f"harness '{adapter.get('harness', '?')}' declares streamable HTTP MCP "
            "support without a managed_server recipe"
        )
    if not managed.get("name") or not managed.get("url"):
        raise ValueError(
            f"harness '{adapter.get('harness', '?')}' has an incomplete managed "
            "streamable HTTP MCP recipe"
        )
    if not managed.get("launch_args") and not managed.get("merge_json"):
        raise ValueError(
            f"harness '{adapter.get('harness', '?')}' has no managed MCP injection"
        )
    return managed


def emit_adapter(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Copy the adapter's harness-specific config files (e.g. opencode.json) to
    `root` (the working directory). These are emitted artifacts (gitignored),
    regenerated each launch from the tracked template in the adapter dir."""
    adir = ADAPTERS / adapter["harness"]
    written = []
    for fname in adapter.get("emit", []):
        src = adir / fname
        if src.exists():
            dst = root / fname
            dst.parent.mkdir(parents=True, exist_ok=True)  # fname may be nested (e.g. .codex/hooks.json)
            if adapter["harness"] == "opencode" and fname == "opencode.json":
                try:
                    template = json.loads(src.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise opencode_config.OpenCodeConfigError(
                        "HARNESS_CONFIG_INVALID",
                        f"OpenCode template is invalid: {exc}",
                    ) from exc
                opencode_config.emit_template(root, template)
            else:
                atomic_write(dst, src.read_text())
            written.append(fname)
    return written


def render_harness_skills(con: sqlite3.Connection, shell_id: int,
                          work_dir: Path, adapter: dict) -> dict:
    """Render exact shell grants into every skill directory consumed by the
    selected harness. Adapters default to the Claude-compatible path and may
    add a native path where compatibility discovery is incomplete."""
    skill_dirs = list(dict.fromkeys([
        ".claude/skills",
        *(adapter.get("skill_dirs") or []),
    ]))
    try:
        summary = skill_projection.reconcile_shell(
            con, shell_id, work_dir, ensure_dirs=skill_dirs
        )
    except skill_projection.ProjectionError as exc:
        raise LaunchError(str(exc)) from exc
    summary["dirs"] = list(skill_dirs)
    return summary


def resolve_opencode_plugins(work_dir: Path) -> None:
    """Rewrite opencode.json `plugin` entries that point into the engine to
    ABSOLUTE paths. The template registers
    `./.super-coder/adapters/opencode/protect-default-branch.js` — relative to the
    opencode.json location (the worktree root). A fork gitignores .super-coder/,
    so from a shell worktree that path does not exist and opencode silently loads
    NO plugin → the branch-guard never runs. Same trap the hooks fell into;
    resolve to the installed engine (verified: opencode loads plugins by absolute
    path). No-op when no engine-relative plugin entry is present (e.g. the source
    repo, where the relative path already resolves)."""
    if not (work_dir / "opencode.json").exists():
        return

    def update(cfg: dict) -> None:
        plugins = cfg.get("plugin")
        if not isinstance(plugins, list):
            return
        cfg["plugin"] = [
            str(ENGINE / plugin.split(".super-coder/", 1)[1])
            if isinstance(plugin, str) and ".super-coder/" in plugin
            else plugin
            for plugin in plugins
        ]

    opencode_config.mutate(work_dir, "resolve-plugins", update)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base (patch wins on scalar conflicts);
    mutates and returns base. Nested dicts merge key-wise so a fork's other
    settings survive."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _merge_json_spec(spec: dict, root: Path = REPO_ROOT) -> list[str]:
    """Deep-merge each {repo-relative-path: patch} into that project-scoped JSON
    file under `root`, preserving any keys the fork already set. Writes the same
    bytes when the patch is already present, so re-running produces no git churn."""
    touched = []
    for rel, patch in (spec or {}).items():
        dst = root / rel
        if rel == "opencode.json":
            opencode_config.merge_json(
                root, patch, operation="merge-json"
            )
            touched.append(rel)
            continue
        cur: dict = {}
        if dst.exists():
            try:
                cur = json.loads(dst.read_text())
            except (json.JSONDecodeError, OSError):
                print(f"  ! {rel} is not valid JSON — leaving it untouched")
                continue
        _deep_merge(cur, patch)
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dst, json.dumps(cur, indent=2) + "\n")
        touched.append(rel)
    return touched


def apply_merge_json(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Always-on config patches the adapter declares at top-level `merge_json`
    (distinct from sandbox.merge_json, which is sandbox-only). Used to install
    engine-managed, gitignored harness config every launch — e.g. claude's
    PreToolUse branch-guard hook in .claude/settings.local.json (kept out of the
    fork's tracked .claude/settings.json so fork-owned config is never clobbered)."""
    return _merge_json_spec(adapter.get("merge_json") or {}, root)


def apply_managed_mcp(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Apply the supported adapter's generated JSON MCP fragment, if any."""
    managed = managed_mcp_injection(adapter)
    return _merge_json_spec((managed or {}).get("merge_json") or {}, root)


def apply_sandbox(adapter: dict, root: Path = REPO_ROOT) -> list[str]:
    """Sandbox-only: elevate harness permissions to allow-all when booting
    INSIDE the docker sandbox (SC_SANDBOX, set by `sc launch`'s docker run). The
    container is the safety boundary, so permission prompts inside it are pure
    friction; the no-docker host escape hatch (`./sc boot` with SC_SANDBOX
    unset) keeps normal prompts. Each adapter declares sandbox.merge_json:
    {repo-relative-path: patch}; we deep-merge the patch into that
    project-scoped file (preserving any keys the fork set)."""
    if not os.environ.get("SC_SANDBOX"):
        return []
    return _merge_json_spec((adapter.get("sandbox") or {}).get("merge_json") or {}, root)


def execution_mode() -> str:
    """Name the seat that the current launcher process actually occupies."""
    return "container" if os.environ.get("SC_SANDBOX") else "host"


def shell_work_dir(
    shortname: "str | None",
    flavor: "str | None",
    *,
    root: "Path | None" = None,
) -> Path:
    """The one worktree rule, shared by every boot path (interactive CLI,
    headless `sc run`, Interface exec): the admin flavor boots at the repo
    root (it maintains `main` itself); every other shell — dev, planner,
    reviewer alike — gets an isolated git worktree at
    `.sc-worktrees/<shortname>` on branch `shell/<shortname>`."""
    root = root or REPO_ROOT
    if shortname and flavor != "admin":
        return root / ".sc-worktrees" / shortname.lower()
    return root


def ensure_worktree(work_dir: Path, shortname: str) -> None:
    """Create a git worktree for a shell at work_dir on branch shell/<shortname>.

    An existing directory is repaired before reuse. Git stores absolute paths
    on both sides of a linked-worktree relationship, so moving a whole fork
    leaves the directory present but its ``.git`` file and the main repo's
    ``worktrees/<name>/gitdir`` pointing at the old location. ``git worktree
    repair`` is lossless: it rewrites those links without touching the branch,
    index, or working tree. Creates the branch from HEAD if it doesn't exist
    yet; checks it out if it does. Exits with a clear message on git failure.
    """
    if work_dir.exists():
        repair = subprocess.run(
            [
                "git", "-C", str(REPO_ROOT), "worktree", "repair",
                str(work_dir),
            ],
            capture_output=True,
            text=True,
        )
        if repair.returncode != 0:
            sys.exit(
                f"FATAL: could not repair existing worktree at {work_dir}:\n"
                f"{repair.stderr.strip()}"
            )
        probe = subprocess.run(
            ["git", "-C", str(work_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            sys.exit(
                f"FATAL: existing shell worktree at {work_dir} is not usable "
                f"after repair:\n{probe.stderr.strip()}"
            )
        if repair.stdout.strip() or repair.stderr.strip():
            print(f"→ worktree: repaired Git links for {shortname} at {work_dir}")
        return
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    branch = f"shell/{shortname.lower()}"
    existing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "branch", "--list", branch],
        capture_output=True, text=True,
    )
    branch_exists = bool(existing.stdout.strip())
    cmd = ["git", "-C", str(REPO_ROOT), "worktree", "add", str(work_dir)]
    cmd += [branch] if branch_exists else ["-b", branch]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"FATAL: could not create worktree at {work_dir}:\n{result.stderr.strip()}")


def link_worktree_map(work_dir: Path) -> "str | None":
    """Point a shell worktree's compatibility map path at the active map DB.

    The dr_* repo map is a single derived cache at the main repo root (built by
    `./sc map`; read by map_db.py / compose.py via __file__, so writers + the
    renderer already resolve the root correctly). But boot.md and the map skills
    tell shells to query `sqlite3 .sc-state/map.db` — a CWD-relative path. From a
    worktree that file doesn't exist, and the sqlite3 CLI CREATES an empty one on
    open, which then shadows the root map for that worktree ('no such table:
    dr_section'). A symlink makes the documented path resolve to the real root DB
    from every worktree; sqlite keeps its -wal/-shm next to the resolved (root)
    file, so no stray sidecars land in the worktree. We do NOT commit the cache
    (it's a derived binary; the authored layer is tracked as map_content.sql).

    Healed every boot: an empty/stale shadow left by a pre-fix session — or stray
    -wal/-shm sidecars — are cleared and replaced with the symlink. A dangling
    link (root not mapped yet) is fine: the first query creates the DB at the
    root, where `./sc map` then populates it."""
    sc_state = work_dir / ".sc-state"
    link = sc_state / "map.db"
    target = artifact_policy.map_db_path()
    try:
        sc_state.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.readlink() == target:
                return None
            link.unlink()
        elif link.exists():
            link.unlink()  # empty/stale per-worktree shadow — root is canonical
        for sidecar in ("map.db-wal", "map.db-shm"):
            p = sc_state / sidecar
            if p.exists() and not p.is_symlink():
                p.unlink()
        link.symlink_to(target)
    except OSError as e:
        return f"→ map link: skipped ({e})"
    return None


def trust_codex_worktree(work_dir: Path) -> "str | None":
    """Mark a codex shell's worktree as a trusted project in codex's config, so
    its project-local .codex/hooks.json (the branch-guard) actually LOADS.

    codex loads project-local hooks ONLY when the project's .codex/ layer is
    trusted, and trust is keyed per-directory. Shells run in worktrees, which are
    NOT the trusted main root — so without this the branch-guard never loads and
    codex worktree shells run with NO edit-time guard. (Verified: interactive
    codex fires the PreToolUse hook iff the project is trusted; `codex exec` runs
    no hooks at all, and `--dangerously-bypass-hook-trust` only skips per-hook
    hash review, not this layer-load trust.)

    Idempotent text-append to $CODEX_HOME/config.toml (default ~/.codex). This is
    the one place the engine writes under the codex home — additive project-trust
    only, never auth/history (an FnB-approved deviation from the otherwise
    hands-off ~/.codex policy)."""
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    cfg = home / "config.toml"
    header = f'[projects."{work_dir}"]'
    try:
        text = cfg.read_text() if cfg.exists() else ""
        if header in text:
            return None  # already trusted (codex writes exactly this stanza)
        home.mkdir(parents=True, exist_ok=True)
        sep = "" if (not text or text.endswith("\n")) else "\n"
        with cfg.open("a") as f:
            f.write(f'{sep}\n{header}\ntrust_level = "trusted"\n')
        return f"→ codex: trusted worktree layer (hooks load) → {cfg}"
    except OSError as e:
        return f"→ codex trust: skipped ({e})"


def sync_worktree(work_dir: Path, shortname: str, flavor: str | None = None) -> str:
    """Project one shell checkout; mutate only a clean expected shell base."""
    reviewer = flavor == "reviewer"
    projection = git_freshness.project(
        work_dir,
        policy=(
            git_freshness.TARGET_REVIEWER_HEAD
            if reviewer
            else git_freshness.TARGET_ISOLATED_SHELL
        ),
        expected_branch=f"shell/{shortname.lower()}",
        allow_auto_advance=not reviewer,
    )
    return git_freshness.render(projection)


def main_checkout_note(repo_root: Path) -> str:
    """Project the live engine checkout without ever rewriting it."""
    return git_freshness.render(
        git_freshness.project(
            repo_root,
            policy=git_freshness.TARGET_LIVE_ENGINE,
        )
    )


def declared_work_repo_note(repo_root: Path = REPO_ROOT) -> str | None:
    """Project a separately declared work repo independently from substrate."""
    raw = install.work_repo()
    if not raw:
        return None
    target = Path(raw).expanduser()
    try:
        if target.resolve() == repo_root.resolve():
            return None
    except OSError:
        pass
    return git_freshness.render(
        git_freshness.project(
            target,
            policy=git_freshness.TARGET_SHARED_WORK,
        )
    )


def ensure_harness_path() -> None:
    """Prepend the dirs where the official installers drop harness binaries onto
    this process's PATH, so detection (shutil.which) and exec (execvpe) agree
    with what `./sc install` / `./sc ensure-harness` installed.

    The opencode installer drops its binary in ~/.opencode/bin and only edits a
    shell rc — a dir a fresh launch shell does NOT carry on PATH. Without this,
    detect_harnesses() silently never offers opencode even though ensure-harness
    reported it installed: install.py trusts HARNESS_BIN, the launcher trusted
    PATH only, and they disagreed. Reuse install.HARNESS_BIN so there is one
    source for where a harness lives.

    In the sandbox this is a no-op: the image's ENV PATH already carries every
    baked binary dir, and folding host dirs in is actively wrong for kimi —
    host `~/.kimi-code` (its bin/ + config in one dir) is bind-mounted for
    creds, and prepending its bin/ would shadow the image's own kimi binary
    with an incompatible host binary."""
    if os.environ.get("SC_SANDBOX"):
        return
    try:
        bin_dirs = [p.parent for p in install.HARNESS_BIN.values()]
    except Exception:
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    add = [str(d) for d in bin_dirs if d.is_dir() and str(d) not in parts]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + parts)


def detect_harnesses() -> list[str]:
    """Interactive harnesses installable right now, in adapter-dir order.

    Terminal adapters use their ordinary launch command. Browser-backed
    adapters use the command from their explicit interactive contract.
    """
    if not ADAPTERS.exists():
        return []
    found = []
    for d in sorted(ADAPTERS.iterdir()):
        cfg = d / "adapter.json"
        if not (d.is_dir() and cfg.exists()):
            continue
        try:
            adapter = json.loads(cfg.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        interactive = interactive_launch(adapter)
        if interactive is None:
            continue
        cmd = (interactive.get("launch") or [d.name])[0]
        if shutil.which(cmd):
            found.append(adapter.get("harness", d.name))
    return found


def pick_harness(detected: list[str], default: str, first: bool) -> str | None:
    """Resolve the harness when no explicit override (--harness / HARNESS) was
    given. Returns None when nothing is detected so the caller can fall back to
    instance.json/'claude' — preserving the old silent behavior on a host with
    no harness CLI on PATH (headless verify, CI). The pick is per-launch only:
    nothing is written back, so two terminals can boot the same fork on
    different harnesses in parallel."""
    if not detected:
        return None
    if len(detected) == 1:
        return detected[0]
    dflt = default if default in detected else detected[0]
    # --first and non-TTY (verify/CI) never prompt — take the default silently.
    if first or not sys.stdin.isatty():
        return dflt
    print(f"\n{style.bold('Harness:')}")
    for i, h in enumerate(detected, 1):
        mark = style.dim("  (default)") if h == dflt else ""
        name = style.bold(h) if h == dflt else h
        print(f"  {style.dim(f'{i}.')} {name}{mark}")
    while True:
        choice = input(f"\nPick (1-{len(detected)}, Enter for {dflt}): ").strip()
        if not choice:
            return dflt
        if choice.isdigit() and 1 <= int(choice) <= len(detected):
            return detected[int(choice) - 1]
        print("  invalid choice")


def _configured_harness() -> str | None:
    cfg = ENGINE / "instance.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("harness")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def open_db():
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(
            f"FATAL: no usable DB at {DB_PATH}.\n"
            f"  Rebuild it from text:  ./sc rebuild"
        )
    con = db_driver.connect(DB_PATH)
    con.execute("SELECT 1 FROM shells LIMIT 1")  # smoke
    return con


def browser_conversation_active(con, shell_id: int) -> bool:
    """Does an open browser chat own this shell's single session slot?"""
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM conversations WHERE shell_id=? "
            "AND state!='closed'",
            (shell_id,),
        ).fetchone()
        return row is not None and int(row[0]) > 0
    except db_driver.OperationalError as exc:
        # An older, not-yet-migrated fork has no conversation tables. Its
        # existing CLI launch path must remain usable. Other DB failures must
        # stay fail-closed instead of silently permitting a second surface.
        if "no such table: conversations" in str(exc):
            return False
        raise
    except (IndexError, KeyError, TypeError, ValueError):
        # Mock/partial databases used by launcher tests do not assert a lease.
        return False


def browser_conversation_shell_ids(con) -> set[int]:
    """Shells whose single session slot is owned by an open browser chat."""
    try:
        rows = con.execute(
            "SELECT DISTINCT shell_id FROM conversations WHERE state!='closed'"
        ).fetchall()
        return {int(row[0]) for row in rows}
    except db_driver.OperationalError as exc:
        if "no such table: conversations" in str(exc):
            return set()
        raise


# ── Auth (username-only) ────────────────────────────────────────────────────

def authenticate(con, interactive: bool = True):
    # SC_USER env wins; else prompt on a TTY; else (headless: `./sc verify`, CI)
    # default to the first active user so launch doesn't EOFError without a TTY.
    # `interactive=False` (an `./sc run` headless boot) never prompts even on a
    # TTY — the caller is usually another shell's session, not an operator.
    username = os.environ.get("SC_USER")
    if not username:
        if interactive and sys.stdin.isatty():
            username = input("Username: ").strip()
        else:
            row = con.execute(
                "SELECT username FROM users WHERE is_active=1 ORDER BY user_id LIMIT 1"
            ).fetchone()
            username = row["username"] if row else None
    if not username:
        sys.exit("aborted — no user (set SC_USER or provision a user)")
    row = con.execute(
        "SELECT user_id, username FROM users "
        "WHERE LOWER(username)=LOWER(?) AND is_active=1",
        (username,),
    ).fetchone()
    if row is None:
        sys.exit(f"no active user '{username}'")
    return row


# ── Shell selection ─────────────────────────────────────────────────────────

def list_shells(con, user_id: int) -> list:
    shells = [dict(row) for row in con.execute(
        "SELECT shell_id, display_name, shortname, mandate, is_shared, flavor, "
        "current_state FROM shells "
        "WHERE (user_id=? OR is_shared=1) AND COALESCE(is_deleted,0)=0 "
        "ORDER BY flavor IS NULL, flavor, shell_id",
        (user_id,),
    ).fetchall()]
    browser_shell_ids = browser_conversation_shell_ids(con)
    for shell in shells:
        shell["browser_active"] = shell["shell_id"] in browser_shell_ids
    return shells


def select_host_admin(con, requested: "str | None" = None) -> dict:
    """Resolve the installation's sole active Admin without a shell picker.

    The migration-backed unique index makes the multi-row branch unreachable on
    a healthy floor, but keeping the launch check explicit gives an older or
    half-migrated installation a deterministic repair error instead of silently
    choosing an identity.
    """
    rows = [dict(row) for row in con.execute(
        "SELECT shell_id, display_name, shortname, mandate, is_shared, flavor, "
        "current_state FROM shells WHERE flavor='admin' "
        "AND COALESCE(is_deleted,0)=0 ORDER BY shell_id"
    ).fetchall()]
    if not rows:
        raise LaunchError(
            "no active Admin exists; repair the installation before launching"
        )
    if len(rows) != 1:
        ids = ", ".join(str(row["shell_id"]) for row in rows)
        raise LaunchError(
            f"Admin singleton invariant is not converged (active shell ids: {ids}); "
            "run the pending engine migration or repair the live DB"
        )
    chosen = rows[0]
    if requested and (chosen["shortname"] or "").lower() != requested.lower():
        raise LaunchError(
            f"shell '{requested}' is not the sole active Admin "
            f"('{chosen['shortname'] or chosen['shell_id']}')"
        )
    return chosen


def require_host_harness(adapter: dict, harness: str) -> None:
    """Refuse a host Admin boot before opening a durable session."""
    command = str((adapter.get("launch") or [harness])[0])
    if shutil.which(command):
        return
    raise LaunchError(
        f"host harness '{command}' is not installed; run ./sc ensure-harness "
        "or use make dos-e for the container Admin route"
    )


def flavor_defaults(con) -> dict:
    """Flavor launch defaults with per-harness model and Thinking intent.
    The (flavor, harness) matrix: each flavor names a model per harness, and one
    harness is the picker default (is_default). Empty if the table is absent
    (older fork mid-migration) so the launcher degrades to its prior behavior
    rather than failing."""
    has_effort = True
    try:
        rows = con.execute(
            "SELECT flavor,harness,model,effort,is_default FROM flavor_defaults")
    except db_driver.OperationalError:
        has_effort = False
        try:
            rows = con.execute(
                "SELECT flavor,harness,model,is_default FROM flavor_defaults")
        except db_driver.OperationalError:
            return {}
    out: dict = {}
    for r in rows:
        fd = out.setdefault(r["flavor"], {
            "default_harness": None, "models": {}, "efforts": {},
        })
        fd["models"][r["harness"]] = r["model"]
        fd["efforts"][r["harness"]] = r["effort"] if has_effort else None
        if r["is_default"]:
            fd["default_harness"] = r["harness"]
    return out


def _default_label(defaults: dict, flavor: str | None) -> str:
    """Picker annotation: the harness (+ short model id) a shell of this flavor
    boots with by default, so the operator knows which harness to launch if they
    forget. Blank for bespoke shells with no flavor default."""
    fd = defaults.get(flavor)
    if not fd or not fd["default_harness"]:
        return ""
    harness = fd["default_harness"]
    model = fd["models"].get(harness)
    return harness + (f" · {model.split('/')[-1]}" if model else "")


def _shell_status(shell, snap: "dict | None") -> str:
    """Styled picker status derived from liveness."""
    if shell["flavor"] == "admin":
        label, paint = "Exempt", style.dim
    elif dict(shell).get("browser_active"):
        label, paint = "BROWSER", style.red
    elif not snap or not snap.get("supported"):
        label, paint = "Unknown", style.dim
    else:
        state = shell_liveness.session_state(shell["shortname"] or "", snap)
        if state == "busy":
            label, paint = "Busy", style.amber
        elif state == "orphan":
            label, paint = "Orphaned", style.red
        elif snap.get("indeterminate"):
            label, paint = "Unknown", style.dim
        else:
            label, paint = "Available", style.green
    return f"{paint(label)}{' ' * (12 - len(label))}"


def confirm_live(shell, snap: "dict | None") -> bool:
    """Interactive twin of the headless liveness refusal: booting a shell whose
    worktree already hosts a live session runs two sessions against one tree +
    one memory row set, so warn and put the call to the operator. True → boot
    (dormant, admin-exempt, no snapshot, or the operator said yes)."""
    if not snap or shell["flavor"] == "admin" or not shell["shortname"]:
        return True
    state = shell_liveness.session_state(shell["shortname"], snap)
    if state is None:
        return True
    pids, orphans = shell_liveness.orphan_split(shell["shortname"], snap)
    if state == "orphan":
        print(style.yellow(
            f"\n  ⚠ {shell['shortname']} slot is held by an ORPHANED session "
            f"(pid {', '.join(map(str, orphans))} — terminal closed / parent "
            f"gone)."))
        print(style.dim(
            f"    Verify it is idle (`ps -o etime=,stat= -p {orphans[0]}`; no "
            f"busy children), `kill` it, then boot. An orphan can still be "
            f"mid-work — never kill unverified."))
    else:
        print(style.yellow(
            f"\n  ⚠ {shell['shortname']} already has a live session "
            f"(pid {', '.join(map(str, pids))}) — one shell, one session."))
    return input("  Boot anyway? [y/N]: ").strip().lower() in ("y", "yes")


def pick_shell(shells: list, requested: str | None,
               first: bool, defaults: dict | None = None,
               snap: "dict | None" = None):
    defaults = defaults or {}
    if not shells:
        sys.exit("FATAL: no shells available to this user.")
    if requested:
        # Case-insensitive: auto-names are upper (DEV3) but `./sc launch-dev3` works.
        chosen = next((s for s in shells if (s["shortname"] or "").lower()
                       == requested.lower()), None)
        if chosen is None:
            avail = ", ".join(s["shortname"] or "?" for s in shells)
            sys.exit(f"no shell '{requested}'. Available: {avail}")
        return chosen
    if first or not sys.stdin.isatty():
        return shells[0]
    # Interactive picker — shells grouped by flavor, each group labelled. The
    # pick number is the row's 1-based position in the already-grouped list, so
    # it always reads 1, 2, 3… down the screen. shell_id is global and
    # non-contiguous, so showing it here made the numbering jump around within a
    # group; position tracks the display order instead.
    print(style.dim(f"\n{'#':>3}  {'Name':<16}{'Shortname':<14}{'Status':<12}"
                    f"{'Default (harness · model)'}"))
    _sentinel = object()
    cur_flavor: object = _sentinel
    for n, s in enumerate(shells, 1):
        if s["flavor"] != cur_flavor:
            cur_flavor = s["flavor"]
            print(f"\n{style.accent(cur_flavor or '(bespoke)')}")
        num = style.dim(f"{n:>3}")
        name = style.bold("{:<16}".format(s["display_name"] or ""))
        short = "{:<14}".format(s["shortname"] or "")
        print(f"{num}  {name}{short}{_shell_status(s, snap)}"
              f"{style.dim(_default_label(defaults, s['flavor']))}")
    if snap and snap.get("indeterminate"):
        print(style.dim(f"\n  ⚠ {snap['indeterminate']} harness process(es) "
                        f"with unreadable cwd — liveness markers are partial."))
    while True:
        choice = input("\nPick (#): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(shells):
            chosen = shells[int(choice) - 1]
            if not confirm_live(chosen, snap):
                continue          # operator declined — back to the picker
            return chosen
        print("  invalid choice")


# ── Session archive ─────────────────────────────────────────────────────────

def _is_unused(narrative: str) -> bool:
    """A freshly-opened session whose narrative is still just the 'Session start'
    stub (no work appended). Detected by a single timestamp entry."""
    return (narrative or "").count("\n[") <= 1


def session_provider(harness: str, model: "str | None") -> "str | None":
    """Boot-time provider for the archive row. OpenCode model IDs may be
    provider-prefixed ("ollama-cloud/<model>") — the prefix wins; otherwise
    use the harness's home provider (claude→anthropic, codex→openai,
    vibe→mistral).
    model_catalog maps kimi→"kimi-for-coding" for the model datalist, but its
    wire.jsonl reports provider="kimi" natively — pin that value here (ahead of
    the map lookup) so boot-row and sweep-row providers agree."""
    if harness in model_catalog.PREFIXED_HARNESSES and model and "/" in model:
        return model.split("/", 1)[0]
    if harness == "kimi":
        return "kimi"
    return model_catalog.HARNESS_PROVIDER.get(harness)


class SessionOpenError(RuntimeError):
    """A bounded session-open refusal that is safe to show to the operator."""


def _is_db_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _write_session(con, shell_id: int, life: dict,
                   life_cols: list[str]) -> tuple[str, int]:
    # Reuse the active session if it was opened but never used (e.g. install
    # opened session 0001, or a prior launch did no work) — avoids phantom empty
    # sessions and the incidental first-snapshot diff. The reused stub becomes
    # THIS launch's session, so its lifecycle is overwritten with this launch's.
    active = con.execute(
        "SELECT active_archive_id FROM shells WHERE shell_id=?", (shell_id,)
    ).fetchone()[0]
    if active:
        row = con.execute(
            "SELECT archive_id, session_id, full_narrative FROM shell_memory_archives "
            "WHERE archive_id=?", (active,)
        ).fetchone()
        # …but a stub that actually LAUNCHED a harness session is not unused:
        # attributed usage rows prove real spend under this archive's lifecycle,
        # and reusing it would overwrite that lifecycle with this boot's (three
        # headless one-shots once collapsed into one kimi-flavored archive).
        # The pre-session sweep runs before this, so attribution is current.
        if row and _is_unused(row["full_narrative"]) and not con.execute(
                "SELECT 1 FROM session_token_usage WHERE archive_id=? LIMIT 1",
                (row["archive_id"],)).fetchone():
            con.execute(
                f"UPDATE shell_memory_archives SET {', '.join(c + '=?' for c in life_cols)} "
                "WHERE archive_id=?",
                [life.get(c) for c in life_cols] + [row["archive_id"]])
            return row["session_id"], row["archive_id"]

    last = con.execute(
        "SELECT MAX(CAST(session_id AS INTEGER)) FROM shell_memory_archives WHERE shell_id=?",
        (shell_id,),
    ).fetchone()[0]
    session_id = f"{(last or 0) + 1:04d}"
    today, now_hm = str(date.today()), datetime.now().strftime("%H:%M")
    narrative = (f"# {session_id} | {today} | session opened\n\n"
                 f"## Narrative\n\n[{now_hm}] Session start.\n")
    cur = con.execute(
        "INSERT INTO shell_memory_archives "
        f"(shell_id, session_id, date, full_narrative, {', '.join(life_cols)}) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' for _ in life_cols)})",
        [shell_id, session_id, today, narrative] + [life.get(c) for c in life_cols],
    )
    archive_id = cur.lastrowid
    con.execute("UPDATE shells SET active_archive_id=? WHERE shell_id=?",
                (archive_id, shell_id))
    return session_id, archive_id


def open_session(con, shell_id: int,
                 lifecycle: "dict | None" = None) -> tuple[str, int]:
    """Atomically open a lifecycle archive, with bounded lock retries.

    `lifecycle` carries the launch telemetry persisted onto the archive row
    (started_at/harness/provider/model). ended_at is
    NOT written here: run.py execs the harness, so no code runs at exit; the
    analytics sweep backfills it from harness session data.

    Launch connections enter with no transaction. BEGIN IMMEDIATE obtains the
    writer reservation before the read/allocate/write sequence, so a retry
    always restarts from fresh state and can never leave a half-open archive.
    shell_factory calls inside its own write transaction; preserve its existing
    transaction and commit contract rather than nesting BEGIN.
    """
    life = {"started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **(lifecycle or {})}
    life_cols = ["started_at", "harness", "provider", "model"]
    delays = SESSION_OPEN_RETRY_DELAYS_S if not con.in_transaction else ()
    attempts = len(delays) + 1

    for attempt in range(attempts):
        try:
            if not con.in_transaction:
                con.execute("BEGIN IMMEDIATE")
            result = _write_session(con, shell_id, life, life_cols)
            con.commit()
            return result
        except db_driver.OperationalError as exc:
            con.rollback()
            if not _is_db_busy(exc):
                raise
            if attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            wait_ms = con.execute("PRAGMA busy_timeout").fetchone()[0]
            raise SessionOpenError(
                f"engine DB remained busy across {attempts} bounded "
                f"session-open attempt(s) (up to {wait_ms} ms each); no session "
                "or archive was created. Retry after the concurrent engine write "
                "finishes; if this persists, inspect the engine API/worker logs "
                "for a long-running DB writer."
            ) from exc
        except Exception:
            con.rollback()
            raise

    raise AssertionError("unreachable")


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


# ── Programmatic launch (Interface pane entrypoint, spec #20) ──────────────

class LaunchError(Exception):
    """A prepare_launch refusal: bad shell, unresolvable route, or a harness
    that cannot take the requested model/effort. The caller (interface_exec)
    maps this to its own exit code; it never reaches the operator as a
    traceback."""


class LaunchPlan(NamedTuple):
    """Everything an engine-driven launcher needs to BECOME the harness:
    the exec argv, the fully-injected env, the cwd to exec from, and the
    session identifiers the launcher reports back to the API."""
    argv: list[str]
    env: dict[str, str]
    cwd: str
    session_id: str
    archive_id: int
    harness: str
    model: "str | None"
    effort: "str | None"
    cli_version: "str | None"
    boot_content: str


def cleanup_before_launch(
    con: sqlite3.Connection,
    shell: dict,
    *,
    current_leased_run_id: "int | None" = None,
) -> None:
    """Resolve one shell's older successful-Sprint cleanup before reuse."""
    if (
        os.environ.get("RENDER_ONLY")
        or shell.get("flavor") == "admin"
        or not shell.get("shortname")
    ):
        return
    import sprint_cleanup

    shell_id = int(shell["shell_id"])
    store = sprint_cleanup.SprintCleanupTargetStore(con)
    if store.unresolved_worktree((shell_id,)) is None:
        return
    receipt = sprint_cleanup.SprintCleanupExecutor(
        store,
        current_leased_run_id=current_leased_run_id,
    ).run_next(
        f"launcher:{os.getpid()}:{shell_id}",
        shell_id=shell_id,
    )
    blocker = store.unresolved_worktree((shell_id,))
    if blocker is None:
        return
    raise LaunchError(
        f"prior Sprint {blocker.sprint_id} cleanup is {blocker.state} for "
        f"{blocker.path_label} (last_safe_fact={blocker.last_safe_fact}; "
        f"launcher_result={receipt.state}); run `sc sprint cleanup-status "
        f"--sprint {blocker.sprint_id}` and, after correcting a failure, "
        f"`sc sprint cleanup --sprint {blocker.sprint_id} --key "
        "<stable-retry-key>`; retry launch after cleanup succeeds"
    )


def _cli_version(binary: str) -> "str | None":
    """Best-effort `<binary> --version` first line, for the session_start
    hook's cli_version telemetry. Cheap and never load-bearing: any failure
    (not on PATH, slow, odd output) degrades to None."""
    path = shutil.which(binary)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"],
                             capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    lines = (out.stdout or out.stderr or "").strip().splitlines()
    return lines[0].strip() if lines else None


def _probe_worktree_venv(work_dir: Path) -> VenvEligibility:
    """Read-only proof that the assigned worktree owns a viable Python 3.14 venv."""
    venv = work_dir / ".venv"
    project_bin = venv / "bin"
    if venv.is_symlink():
        return VenvEligibility(None, "symlinked .venv root")
    if not project_bin.exists() and not project_bin.is_symlink():
        return VenvEligibility(None, None)
    if not project_bin.is_dir():
        return VenvEligibility(None, ".venv/bin is not a directory")

    try:
        assigned_root = work_dir.resolve(strict=True)
        selected_venv = venv.resolve(strict=True)
        selected_bin = project_bin.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return VenvEligibility(
            None, f"unresolvable .venv containment ({exc})"
        )
    if selected_venv != assigned_root / ".venv":
        return VenvEligibility(
            None, ".venv resolves outside assigned worktree"
        )
    if selected_bin != selected_venv / "bin":
        return VenvEligibility(
            None, ".venv/bin resolves outside assigned .venv"
        )

    python = project_bin / "python"
    try:
        executable = python.resolve(strict=True)
    except FileNotFoundError:
        condition = (
            "dangling .venv/bin/python symlink"
            if python.is_symlink()
            else "missing .venv/bin/python"
        )
        return VenvEligibility(None, condition)
    except (OSError, RuntimeError) as exc:
        return VenvEligibility(
            None, f"unresolvable .venv/bin/python ({exc})"
        )

    if not executable.is_file() or not os.access(executable, os.X_OK):
        return VenvEligibility(
            None, ".venv/bin/python is not an executable regular file"
        )

    try:
        completed = subprocess.run(
            [str(python), "-I", "-S", "-c", VENV_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=VENV_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return VenvEligibility(
            None, f"Python probe timed out after {VENV_PROBE_TIMEOUT_S} seconds"
        )
    except OSError as exc:
        return VenvEligibility(None, f"Python probe could not execute ({exc})")

    if completed.returncode != 0:
        return VenvEligibility(
            None, f"Python probe exited {completed.returncode}"
        )
    try:
        report = json.loads(completed.stdout)
        version = report["version"]
        prefix = report["prefix"]
        base_prefix = report["base_prefix"]
        if (
            not isinstance(version, list)
            or len(version) != 2
            or not all(isinstance(part, int) for part in version)
            or not isinstance(prefix, str)
            or not isinstance(base_prefix, str)
        ):
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return VenvEligibility(None, "Python probe returned an invalid report")

    if version != [3, 14]:
        return VenvEligibility(
            None, f"Python 3.14 is required; found {version[0]}.{version[1]}"
        )

    try:
        selected_prefix = venv.resolve()
        reported_prefix = Path(prefix).resolve()
    except (OSError, RuntimeError):
        return VenvEligibility(
            None, f"Python reported foreign prefix {prefix}"
        )
    if reported_prefix != selected_prefix:
        return VenvEligibility(
            None, f"Python reported foreign prefix {prefix}"
        )
    if base_prefix == prefix:
        return VenvEligibility(None, "Python reported no virtual environment")

    return VenvEligibility(project_bin, None)


def _shell_path(work_dir: Path, inherited: str) -> str:
    """Put a proven project environment ahead of baseline tools."""
    entries = [str(REPO_ROOT)]
    eligibility = _probe_worktree_venv(work_dir)
    if eligibility.bin_dir is not None:
        entries.append(str(eligibility.bin_dir))
    elif eligibility.failure is not None:
        print(
            f"WARNING: {work_dir}: ignoring unusable .venv: "
            f"{eligibility.failure}; run `sc deps` from this worktree to "
            "rebuild its fork-owned environment.",
            file=sys.stderr,
        )
    if inherited:
        entries.append(inherited)
    return os.pathsep.join(entries)


def prepare_launch(*, shell_id: int, harness: "str | None" = None,
                   model: "str | None" = None, effort: "str | None" = None,
                   headless_prompt: "str | None" = None,
                   conversation_owned: bool = False,
                   current_leased_run_id: "int | None" = None,
                   route_binding: "dict | None" = None,
                   binding_digest: "str | None" = None,
                   boot: "conversation_boot.BootDirective | None" = None,
                   ) -> LaunchPlan:
    """Prepare a launch exactly as main() would, without any TTY.

    A browser chat uses the normal harness, model, effort, permission,
    worktree, render, boot, and archive
    paths. This is that path as a library call: it runs main()'s boot
    pipeline step-for-step — authenticate, session archive, worktree
    ensure/sync, boot-doc + skill render, adapter emit + config merges,
    sandbox permission flags, env injection — and returns the plan instead
    of exec'ing. What it deliberately does NOT do: pickers (harness/model
    resolve from the argument or the shell's flavor_defaults, never a
    prompt), the liveness confirm (the Interface reservation capability is
    the gate — the caller refuses before this runs), the banner/spinner/
    boot summary, tab titles, and git_prune (a hygiene sweep for human
    boots; an engine-driven pane launch must never delete branches).

    ``conversation_owned`` lets a declared native browser adapter reuse this
    preparation path without inventing a CLI one-shot command; the conversation
    adapter owns dispatch and the returned argv is deliberately empty.

    ``boot`` carries an explicit conversation launch mode (spec #163): with a
    BootDirective the boot document is the conversation's one committed
    snapshot (bound on start, reused or restored on resume) instead of a
    fresh composition on every call; without it, behavior is unchanged —
    compose fresh and write on every launch.

    Raises LaunchError on a refusal it owns; shared helpers that predate
    this seam (open_db, authenticate, ensure_worktree) still sys.exit, so
    callers must also treat SystemExit as a refusal."""
    headless = headless_prompt is not None
    if not os.environ.get("RENDER_ONLY"):
        global_pointer.write_global_pointers()
    con = open_db()
    # Same best-effort skill heal as main() — compose's SKILLS block reads
    # what this repairs. RENDER_ONLY never mutates, here as there.
    if not os.environ.get("RENDER_ONLY"):
        try:
            seed_skills.sync_engine_skills(con)
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass

    user = authenticate(con, interactive=False)
    fdefaults = flavor_defaults(con)
    # The shell pick, non-interactively: the reservation names the shell_id;
    # it must be one this user could have picked (own or shared, not deleted).
    row = con.execute(
        "SELECT shell_id, display_name, shortname, mandate, is_shared, flavor, "
        "current_state FROM shells "
        "WHERE shell_id=? AND (user_id=? OR is_shared=1) "
        "AND COALESCE(is_deleted,0)=0",
        (shell_id, user["user_id"]),
    ).fetchone()
    if row is None:
        con.close()
        raise LaunchError(
            f"shell_id {shell_id} is not launchable by '{user['username']}' "
            "(unknown, deleted, or neither owned nor shared)")
    chosen = dict(row)

    # Harness route, picker-free: the reservation's harness wins; else this
    # flavor's default harness; else instance.json / 'claude' — the same
    # fallback chain main() feeds its picker as the default.
    fdef = fdefaults.get(chosen["flavor"])
    ensure_harness_path()
    harness = (harness or (fdef["default_harness"] if fdef else None)
               or _configured_harness() or "claude")
    try:
        adapter = load_adapter(harness)
    except ValueError as exc:
        con.close()
        raise LaunchError(str(exc)) from exc

    # Model route: an explicit model wins; else the (flavor, harness) cell,
    # exactly main()'s flavor_defaults routing. An explicit effort wins; the
    # persisted flavor effort is the next fallback before the adapter default.
    flavor_model = fdef["models"].get(harness) if fdef else None
    flavor_effort = (fdef.get("efforts") or {}).get(harness) if fdef else None
    if (route_binding is None) != (binding_digest is None):
        con.close()
        raise LaunchError("route binding and digest must be supplied together")
    if headless and route_binding is not None:
        try:
            resolved_route = resolve_bound_headless_route(
                harness=harness,
                model=model,
                effort=effort,
                binding=route_binding,
                binding_digest=binding_digest,
            )
        except (
            ValueError,
            route_transport.route_bindings.RouteResolutionError,
        ) as exc:
            con.close()
            raise LaunchError(str(exc)) from exc
        session_model = resolved_route.model
        session_effort = resolved_route.effort
    elif headless:
        try:
            resolved_route = resolve_headless_route(
                harness=harness,
                adapter=adapter,
                flavor_model=flavor_model,
                model=model,
                effort=effort if effort is not None else flavor_effort,
            )
        except ValueError as e:
            con.close()
            raise LaunchError(str(e)) from e
        session_model = resolved_route.model
        session_effort = resolved_route.effort
    else:
        session_model = model or flavor_model
        session_effort = effort

    try:
        cleanup_before_launch(
            con,
            chosen,
            current_leased_run_id=current_leased_run_id,
        )
    except LaunchError:
        con.close()
        raise

    # Pre-session analytics sweep — same correctness dependency as main():
    # open_session's stub-reuse check relies on the previous boot's usage
    # already being attributed. Best-effort; RENDER_ONLY never mutates.
    if not os.environ.get("RENDER_ONLY"):
        try:
            import analytics
            analytics.sweep(quiet=True)
        except Exception:
            pass

    try:
        session_id, archive_id = open_session(con, shell_id, lifecycle={
            "harness": harness,
            "provider": session_provider(harness, session_model),
            "model": session_model,
        })
    except SessionOpenError as exc:
        con.close()
        raise LaunchError(str(exc)) from exc

    full = con.execute(
        "SELECT shell_id, display_name, shortname, partner, role, mandate, "
        "current_state, system_prompt, connections, flavor, api_key FROM shells WHERE shell_id=?",
        (shell_id,),
    ).fetchone()
    api_port = ports_mod.resolve().get("port")

    # Worktree: identical rule to main() — every non-admin shell boots in
    # .sc-worktrees/<shortname>; admin boots at the repo root.
    work_dir = shell_work_dir(chosen["shortname"], chosen["flavor"])
    sync_note = None
    if work_dir != REPO_ROOT:
        ensure_worktree(work_dir, chosen["shortname"])
        sync_note = sync_worktree(
            work_dir, chosen["shortname"], chosen["flavor"]
        )
        link_worktree_map(work_dir)
        if harness == "codex":
            trust_codex_worktree(work_dir)
    # Every shell — including admin at the repo root — is told whether the tree
    # its ./sc resolves from is current. Read-only; never syncs main.
    floor_note = main_checkout_note(REPO_ROOT)
    work_repo_note = declared_work_repo_note(REPO_ROOT)

    launch_mode = execution_mode()
    repair_mode = bool(os.environ.get("SC_DEVKIT_REPAIR"))
    content = conversation_boot.resolve_boot(
        con,
        boot,
        compose=lambda: compose_boot(
            con, full, user, session_id, archive_id,
            work_dir=work_dir if work_dir != REPO_ROOT else None,
            sync_note=sync_note,
            floor_note=floor_note,
            work_repo_note=work_repo_note,
            source_mode=install.is_source_repo(),
            devkit_declared=(work_dir / ".subfloor" / "dev-kit.json").is_file(),
            devkit_repair=repair_mode,
            dev_tools=collect_dev_tools(
                work_dir, launch_mode, repair=repair_mode
            ),
            api_key=full["api_key"],
            api_port=api_port,
            launch_mode=launch_mode),
    )
    render_harness_skills(
        con, full["shell_id"], work_dir, adapter
    )
    con.close()
    if boot is None:
        # Interactive/legacy launches: fresh boot doc on every launch.
        for name in ("CLAUDE.md", "AGENTS.md"):
            atomic_write(work_dir / name, content)
    else:
        # Conversation launches: exact committed bytes, untouched when the
        # files already match, atomically restored when they do not.
        conversation_boot.write_boot_files(work_dir, content)

    # Adapter seam: emitted config, plugin path resolution, always-on and
    # sandbox-only JSON merges — the same permission/config files a normal
    # boot writes.
    emit_adapter(adapter, work_dir)
    resolve_opencode_plugins(work_dir)
    apply_merge_json(adapter, work_dir)
    apply_managed_mcp(adapter, work_dir)
    apply_sandbox(adapter, work_dir)

    route_projection = None
    if headless and route_binding is not None:
        try:
            route_projection = route_transport.project(
                route_binding,
                binding_digest,
                expected_harness=harness,
                worktree=work_dir,
                interface="headless",
            )
        except (
            route_transport.route_bindings.RouteResolutionError,
            opencode_config.OpenCodeConfigError,
        ) as exc:
            raise LaunchError(str(exc)) from exc

    # Interactive model routing (main()'s non-headless block): the adapter
    # declares a launch flag or a config-file key for the resolved model.
    model_args: list[str] = []
    mcfg = adapter.get("model") or {}
    if headless:
        pass
    elif session_model and mcfg.get("flag"):
        model_args = [mcfg["flag"], session_model]
    elif session_model and mcfg.get("file"):
        mfile = work_dir / mcfg["file"]
        if mfile.exists():
            key = mcfg.get("key", "model")
            if mfile.name == "opencode.json":
                opencode_config.merge_json(
                    work_dir, {key: session_model}, operation="set-model"
                )
            else:
                try:
                    cfg = json.loads(mfile.read_text())
                except (json.JSONDecodeError, OSError):
                    cfg = {}
                cfg[key] = session_model
                atomic_write(mfile, json.dumps(cfg, indent=2) + "\n")

    # Sandbox elevation, main()'s split kept: launch_flags for the TUI,
    # headless_flags for a non-interactive plan, plus sandbox-only env.
    sandbox_flags: list[str] = []
    sandbox_env: dict[str, str] = {}
    if os.environ.get("SC_SANDBOX"):
        scfg = adapter.get("sandbox") or {}
        key = "headless_flags" if headless else "launch_flags"
        sandbox_flags = list(scfg.get(key) or [])
        sandbox_env = {k: str(v) for k, v in (scfg.get("env") or {}).items()}

    name_args: list[str] = []
    ncfg = adapter.get("name") or {}
    if not headless and ncfg.get("flag") and full["display_name"]:
        name_args = [ncfg["flag"], full["display_name"]]

    if headless:
        argv = headless_command(
            adapter, headless_prompt, session_model,
            sandbox_flags, session_effort, transport=route_projection,
            conversation_owned=conversation_owned,
        )
        if argv is None:
            raise LaunchError(f"harness '{harness}' has no headless adapter")
    else:
        managed = managed_mcp_injection(adapter)
        argv = (
            list(adapter.get("launch") or [harness])
            + list((managed or {}).get("launch_args") or [])
            + name_args + model_args + sandbox_flags
        )

    # Env injection, verbatim from main()'s exec block: adapter env, sandbox
    # env, effort env, then the engine's own SC_* contract + PATH prepend.
    effort_env = (
        route_projection.env()
        if route_projection is not None
        else (headless_effort_env(adapter, session_effort) if headless else {})
    )
    env = {**os.environ, **{k: str(v) for k, v in adapter.get("env", {}).items()},
           **sandbox_env, **effort_env}
    env["SC_SHELL_FLAVOR"] = chosen["flavor"] or ""
    env["SC_API_TOKEN"] = full["api_key"] or ""
    env["SC_API_BASE"] = f"http://127.0.0.1:{api_port}" if api_port else ""
    env["SC_SHELL_ID"] = str(chosen["shell_id"])
    env["SC_SHELL_SHORTNAME"] = chosen["shortname"]
    env["SC_ENGINE_DIR"] = str(ENGINE)
    env["SC_HARNESS"] = harness
    env["SC_SHELL_WORKTREE"] = str(work_dir)
    env["SC_ROOT"] = str(REPO_ROOT)
    env["PATH"] = _shell_path(work_dir, env.get("PATH", ""))

    return LaunchPlan(argv=argv, env=env, cwd=str(work_dir),
                      session_id=session_id, archive_id=archive_id,
                      harness=harness, model=session_model,
                      effort=session_effort,
                      cli_version=_cli_version(argv[0]) if argv else None,
                      boot_content=content)


# ── Main ────────────────────────────────────────────────────────────────────

def _port_listening(port: int) -> bool:
    """Is anything serving on 127.0.0.1:port right now? Best-effort, fast."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def review_gui_panel(api_port: int, has_key: bool) -> str:
    """The one URL the operator always needs — every fork serves the review
    GUI on its own port, so every boot restates it, prominently."""
    url = f"http://127.0.0.1:{api_port}"
    status = (style.green("up") if _port_listening(api_port)
              else style.yellow("not running — ./sc serve"))
    token = "SC_API_TOKEN set" if has_key else "no api key"
    return style.panel([
        f"{style.bold('Review GUI')}  {style.cyan(style.bold(url))}",
        f"{status}{style.dim(' · api on the same port · ' + token)}",
    ])


def main() -> None:
    source_repo = install.is_source_repo()
    tracked_engine = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "--error-unmatch",
            ".super-coder/scripts/run.py",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    owns_engine = (
        source_repo
        or tracked_engine
        or (REPO_ROOT / ".sc-state" / "ejected").is_file()
    )
    callable_floor.require_callable_floor(
        REPO_ROOT,
        expected_ref=(
            None if owns_engine else callable_floor.read_engine_ref(REPO_ROOT)
        ),
        allow_unpinned=owns_engine,
        context="session launch",
    )
    raw_args = sys.argv[1:]
    host_admin = "--host-admin" in raw_args
    if host_admin and os.environ.get("SC_SANDBOX"):
        sys.exit(
            "sc admin: host Admin launch is unavailable inside the sandbox; "
            "run make dos-admin from a host terminal"
        )
    if not os.environ.get("RENDER_ONLY") and not host_admin:
        global_pointer.write_global_pointers()
    args = raw_args
    first = "--first" in args
    headless = "--headless" in args
    # --harness <name> / --harness=<name> forces the harness and skips the
    # picker; its value must not be mistaken for the shell shortname positional.
    # Headless adds -p/--prompt and -m/--model (value-taking, same rule).
    flag_harness = None
    flag_model = None
    flag_effort = None
    prompt = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--first", "--headless", "--host-admin"):
            i += 1
            continue
        if a == "--harness":
            flag_harness = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in ("-m", "--model"):
            flag_model = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a == "--effort":
            flag_effort = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in ("-p", "--prompt"):
            prompt = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--harness="):
            flag_harness = a.split("=", 1)[1]
        elif a.startswith("--model="):
            flag_model = a.split("=", 1)[1]
        elif a.startswith("--effort="):
            flag_effort = a.split("=", 1)[1]
        elif a.startswith("--prompt="):
            prompt = a.split("=", 1)[1]
        elif a.startswith("-"):
            sys.exit(f"session launch: unknown option {a!r}")
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    requested = positional[0] if positional else None
    if host_admin and (headless or len(positional) > 1):
        sys.exit("usage: ./sc admin [admin-shortname] [--harness <h>]")
    if headless and not requested:
        sys.exit('usage: ./sc run <shortname> [-p "<prompt>"] [--harness <h>] '
                 '[-m <model>] [--effort <level>]')

    # Wordmark banner — interactive boots only; headless/verify logs stay clean.
    if not headless and not os.environ.get("RENDER_ONLY") and sys.stdin.isatty():
        print(style.banner(REPO_ROOT.name))

    try:
        con = open_db()
    except (SystemExit, Exception) as exc:
        if not host_admin:
            raise
        detail = str(exc).strip() or type(exc).__name__
        sys.exit(
            f"sc admin: cannot open the live engine DB at {DB_PATH}: {detail}\n"
            "Use the global repair-mode instructions to repair or rebuild that exact DB, "
            "then retry make dos-admin."
        )
    # Self-heal stale engine skills before anything this boot reads them
    # (compose's SKILLS block, render_skill_md). A DB stranded by an in-place
    # `0001` regen repairs itself from assets/skills/ instead of needing a manual
    # `./sc rebuild`. Project-local skills are never touched (no upstream to lag).
    # Skipped under RENDER_ONLY: headless verify must not mutate, and it rebuilds
    # fresh anyway. Best-effort — a heal failure never blocks a launch.
    heal_note = None
    if not os.environ.get("RENDER_ONLY"):
        try:
            healed = seed_skills.sync_engine_skills(con)
            if healed:
                heal_note = f"{len(healed)} stale engine skill(s) → {', '.join(healed)}"
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            heal_note = None

    user = authenticate(con, interactive=not headless)
    fdefaults = flavor_defaults(con)
    # Liveness snapshot for the interactive picker: one /proc pass (ms) so the
    # boot list can show shell status — BROWSER / Busy / Orphaned /
    # Available / Exempt — and
    # confirm before booting into a live worktree. Headless keeps its own lazy
    # compute below; non-TTY boots (--first, piped) can't confirm, so no snap.
    snap = (shell_liveness.compute()
            if not host_admin and not headless and sys.stdin.isatty() else None)
    if host_admin:
        try:
            chosen = select_host_admin(con, requested)
        except LaunchError as exc:
            con.close()
            sys.exit(f"sc admin: {exc}")
    else:
        launchable = list_shells(con, user["user_id"])
        chosen = pick_shell(launchable, requested, first, fdefaults, snap)
    if browser_conversation_active(con, chosen["shell_id"]):
        con.close()
        sys.exit(
            f"shell '{chosen['shortname']}': Browser chat is open. "
            "Close it in Interface before starting a CLI session."
        )
    # Direct interactive boots (`./sc enter dev3`) skip the picker and its
    # confirm — run the same guard here. Picker path already confirmed.
    if requested and not headless and not confirm_live(chosen, snap):
        sys.exit(f"aborted — shell '{chosen['shortname']}' has a live session "
                 f"(one shell, one session; see shell_liveness)")

    # Liveness guard (headless): one shell, one session. A headless boot
    # into a worktree that already hosts a live harness would run two sessions
    # of the same shell against one tree + one memory row set. Interactive
    # boots warn + confirm (above); `sc run` is scripted, so it refuses.
    # Admin boots at the repo root (no worktree signal), so it isn't guarded.
    if headless and chosen["shortname"] and chosen["flavor"] != "admin":
        snap = shell_liveness.compute()
        if snap.get("supported") and shell_liveness.is_active(chosen["shortname"], snap):
            pids, orphans = shell_liveness.orphan_split(chosen["shortname"], snap)
            if pids and len(orphans) == len(pids):
                # The slot-holder outlived its terminal/parent — still refuse
                # (it may be mid-work), but name the fix instead of a dead end.
                sys.exit(
                    f"sc run: shell '{chosen['shortname']}' slot is held by an "
                    f"ORPHANED session (pid {', '.join(map(str, orphans))} — "
                    f"terminal closed / parent gone). Verify it is idle "
                    f"(`ps -o etime=,stat= -p <pid>`; no busy children), "
                    f"`kill <pid>`, then re-run. An orphan can still be "
                    f"mid-work — never kill unverified.")
            sys.exit(f"sc run: shell '{chosen['shortname']}' already has a live "
                     f"session — one shell, one session (see shell_liveness)")
        if snap.get("supported") and snap.get("indeterminate"):
            print(f"→ liveness: {snap['indeterminate']} unreadable harness process(es) — "
                  f"proceeding, but liveness was indeterminate")

    # This shell's flavor default (advisory): the harness it boots with. The
    # model is resolved AFTER the harness pick — a flavor names a model PER
    # harness, so the model tracks whichever harness the operator lands on. Both
    # are overridable — the flavor default only sets the fallback, never a lock.
    fdef = fdefaults.get(chosen["flavor"])
    flavor_harness = fdef["default_harness"] if fdef else None

    # Harness pick, right after the shell pick: an explicit --harness / HARNESS
    # override wins silently; otherwise offer the harnesses on PATH when more
    # than one is present (per-launch, never persisted), falling back to this
    # shell's flavor default, then the fork's instance.json value / 'claude'.
    # Fold the installer bin dirs onto PATH first so detection sees an installed-
    # but-not-yet-on-PATH harness (e.g. opencode in ~/.opencode/bin).
    ensure_harness_path()
    default_harness = flavor_harness or _configured_harness() or "claude"
    harness = (flag_harness or os.environ.get("HARNESS")
               or pick_harness(detect_harnesses(), default_harness, first or headless)
               or default_harness)

    # Resolve + validate the complete headless route before opening a session.
    # Explicit CLI intent wins over the persisted per-flavor Thinking level.
    flavor_model = fdef["models"].get(harness) if fdef else None
    flavor_effort = (fdef.get("efforts") or {}).get(harness) if fdef else None
    try:
        adapter = load_adapter(harness)
        require_harness_surface(adapter, "one_shot" if headless else "terminal")
    except ValueError as exc:
        con.close()
        prefix = "sc run" if headless else "session launch"
        sys.exit(f"{prefix}: {exc}")
    if host_admin:
        try:
            require_host_harness(adapter, harness)
        except LaunchError as exc:
            con.close()
            sys.exit(f"sc admin: {exc}")
        if not os.environ.get("RENDER_ONLY"):
            global_pointer.write_global_pointers()
    if headless:
        try:
            resolved_route = resolve_headless_route(
                harness=harness,
                adapter=adapter,
                flavor_model=flavor_model,
                model=flag_model,
                effort=(
                    flag_effort if flag_effort is not None else flavor_effort
                ),
            )
        except ValueError as e:
            sys.exit(f"sc run: {e}")
        session_model = resolved_route.model
        session_effort = resolved_route.effort
    else:
        session_model = flavor_model
        session_effort = None

    try:
        cleanup_before_launch(con, chosen)
    except LaunchError as exc:
        con.close()
        prefix = "sc run" if headless else "session launch"
        sys.exit(f"{prefix}: {exc}")

    feedback = not headless and not os.environ.get("RENDER_ONLY")
    map_note = None
    trust_note = None
    with style.spinner("sweeping analytics", enabled=feedback) as spinner:
        # Now that the harness is known, resolve THIS flavor's model for it (the
        # (flavor, harness) cell). None when the flavor has no entry for the chosen
        # harness (e.g. opencode as a manual fallback) — then the harness picks its own.
        # Pre-session analytics sweep (doc #11): pull harness-side usage data into
        # session_token_usage + backfill the PREVIOUS session's ended_at. MUST run
        # before open_session — the stub-reuse check there relies on the previous
        # boot's session being attributed to its archive already. Incremental
        # (mtime-gated), so steady-state cost is near zero; the first-ever sweep of
        # harness history is the one large pass. Best-effort like the prune — a
        # broken parser must never block a boot. Skipped under RENDER_ONLY
        # (headless verify must not mutate).
        sweep_note = None
        if not os.environ.get("RENDER_ONLY"):
            try:
                import analytics
                s = analytics.sweep(quiet=True)
                if s["inserted"] or s["updated"]:
                    sweep_note = (f"{s['inserted']} new, {s['updated']} refreshed "
                                  f"session-usage row(s)")
            except Exception:
                sweep_note = None

        spinner.label = "opening session"
        # The model this launch will actually route (headless resolves via flags →
        # flavor default; interactive routes the flavor default). None = the harness
        # picks its own — recorded as NULL, honest about what we know at boot.
        try:
            session_id, archive_id = open_session(con, chosen["shell_id"], lifecycle={
                "harness": harness,
                "provider": session_provider(harness, session_model),
                "model": session_model,
            })
        except SessionOpenError as exc:
            con.close()
            prefix = "sc run" if headless else "session launch"
            sys.exit(f"{prefix}: {exc}")

        full = con.execute(
            "SELECT shell_id, display_name, shortname, partner, role, mandate, "
            "current_state, system_prompt, connections, flavor, api_key FROM shells WHERE shell_id=?",
            (chosen["shell_id"],),
        ).fetchone()
        api_port = ports_mod.resolve().get("port")

        # Every shell gets an isolated git worktree so parallel shells can work on
        # separate branches without clobbering each other — planner/reviewer commit
        # their own artifacts (specs, snapshots, state) there too. All artifacts
        # (CLAUDE.md, AGENTS.md, skills, harness config) land in the worktree root;
        # the harness is exec'd from there. The ONE exception is the admin flavor:
        # it maintains `main` itself (engine updates, migrations, applying approved
        # patches), so it boots in the repo root — no worktree, no shell/* branch.
        # The branch-guard exempts it via SC_SHELL_FLAVOR (exported at exec below).
        work_dir = shell_work_dir(chosen["shortname"], chosen["flavor"])
        sync_note = None
        if work_dir != REPO_ROOT:
            spinner.label = "syncing worktree"
            ensure_worktree(work_dir, chosen["shortname"])
            sync_note = sync_worktree(
                work_dir, chosen["shortname"], chosen["flavor"]
            )
            map_note = link_worktree_map(work_dir)
            if harness == "codex":
                trust_note = trust_codex_worktree(work_dir)
        # Read-only floor check for EVERY shell, admin included — see
        # main_checkout_note. The tree ./sc resolves from is not the shell's own.
        floor_note = main_checkout_note(REPO_ROOT)
        work_repo_note = declared_work_repo_note(REPO_ROOT)

        # Repo-global branch hygiene: delete local branches whose PR is provably
        # merged (git_hygiene's `stale` set — gh-confirmed MERGED, never a base or a
        # checked-out branch). The unattended subset of the git_cleanup skill, run
        # once per boot from whichever shell launches next. Best-effort and silent:
        # soft-fails so it never blocks a launch, and surfaces a line only when it
        # actually removed something. Skipped under RENDER_ONLY (headless verify must
        # not mutate) and opt-out-able per fork via SC_NO_AUTOPRUNE=1.
        prune_note = None
        if not os.environ.get("SC_NO_AUTOPRUNE") and not os.environ.get("RENDER_ONLY"):
            spinner.label = "pruning merged branches"
            try:
                prune_note = git_prune.status_line(git_prune.prune(fetch=False))
            except Exception:
                prune_note = None

        spinner.label = "rendering boot doc + skills"
        launch_mode = execution_mode()
        repair_mode = bool(os.environ.get("SC_DEVKIT_REPAIR"))
        content = compose_boot(con, full, user, session_id, archive_id,
                               work_dir=work_dir if work_dir != REPO_ROOT else None,
                               sync_note=sync_note,
                               floor_note=floor_note,
                               work_repo_note=work_repo_note,
                               source_mode=install.is_source_repo(),
                               devkit_declared=(work_dir / ".subfloor" / "dev-kit.json").is_file(),
                               devkit_repair=repair_mode,
                               dev_tools=collect_dev_tools(
                                   work_dir, launch_mode, repair=repair_mode
                               ),
                               api_key=full["api_key"],
                               api_port=api_port,
                               launch_mode=launch_mode)

        # Render this shell's granted skills to every directory declared by the
        # selected harness — gitignored and rebuilt per boot.
        skills = render_harness_skills(
            con, full["shell_id"], work_dir, adapter
        )
        con.close()

        # One compose, two outputs — Claude Code reads CLAUDE.md, the AGENTS.md
        # harnesses read AGENTS.md. Both at the working directory root.
        for name in ("CLAUDE.md", "AGENTS.md"):
            atomic_write(work_dir / name, content)

    if map_note:
        print(map_note)
    if trust_note:
        print(trust_note)

    print(f"\n→ booted {style.bold(full['display_name'])} "
          f"(shell_id={full['shell_id']}, session={session_id})")
    if work_dir != REPO_ROOT:
        print(f"→ worktree: {work_dir}")
        print(f"→ sync: {sync_note}")
    elif chosen["flavor"] == "admin":
        print("→ working dir: repo root (admin — maintains main directly)")
    if heal_note:
        print(f"→ heal: {heal_note}")
    if prune_note:
        print(f"→ prune: {prune_note}")
    if sweep_note:
        print(f"→ analytics: {sweep_note}")
    print(f"→ wrote {work_dir / 'CLAUDE.md'}")
    print(f"→ wrote {work_dir / 'AGENTS.md'}")
    if headless and api_port and full["api_key"]:
        print(f"→ api: http://127.0.0.1:{api_port} (SC_API_TOKEN set)")
    print(f"→ skills: {len(skills['written'])} changed "
          f"({len(skills['deleted'])} deleted), "
          f"{len(skills['skipped'])} unchanged → "
          f"{', '.join(skills['dirs'])}")

    # Harness was resolved up front (override / picker / default); the adapter
    # seam owns the launch command + any harness-specific config to emit.
    emitted = emit_adapter(adapter, work_dir)
    resolve_opencode_plugins(work_dir)  # engine-relative plugin path → absolute (loads in worktrees)
    print(f"→ harness: {style.bold(harness)} "
          f"(reads {adapter.get('boot_artifact', 'AGENTS.md')})")
    if emitted:
        print(f"→ emitted {', '.join(emitted)}")

    # Flavor model default: route the model to the harness the operator picked.
    # The adapter declares HOW it takes a model — a launch flag (claude/codex:
    # `--model <id>`) or a config-file key (opencode: opencode.json "model"). A
    # NULL flavor model, or a harness declaring neither, skips this. Still
    # overridable in-session / via the harness's own `-m`. Headless resolves
    # its model separately (flags → flavor default) through the headless
    # block's model_flag, so this interactive routing is skipped there.
    model_args: list[str] = []
    mcfg = adapter.get("model") or {}
    if headless:
        pass
    elif flavor_model and mcfg.get("flag"):
        model_args = [mcfg["flag"], flavor_model]
        print(f"→ model: {flavor_model} (flavor default for {chosen['flavor']})")
    elif flavor_model and mcfg.get("file"):
        mfile = work_dir / mcfg["file"]
        if mfile.exists():
            key = mcfg.get("key", "model")
            if mfile.name == "opencode.json":
                opencode_config.merge_json(
                    work_dir, {key: flavor_model}, operation="set-model"
                )
            else:
                try:
                    cfg = json.loads(mfile.read_text())
                except (json.JSONDecodeError, OSError):
                    cfg = {}
                cfg[key] = flavor_model
                atomic_write(mfile, json.dumps(cfg, indent=2) + "\n")
            print(f"→ model: {flavor_model} (flavor default for {chosen['flavor']})")
    merged = apply_merge_json(adapter, work_dir)
    if merged:
        print(f"→ harness config → {', '.join(merged)}")
    managed_files = apply_managed_mcp(adapter, work_dir)
    if managed_files:
        print(f"→ managed MCP → {', '.join(managed_files)}")
    sandboxed = apply_sandbox(adapter, work_dir)
    if sandboxed:
        print(f"→ sandbox: allow-all permissions → {', '.join(sandboxed)}")

    # Sandbox-only launch flags — e.g. codex's approval/sandbox bypass, safe
    # because the container is the safety boundary. The no-docker host path keeps
    # the harness's normal prompts (SC_SANDBOX unset). The two flag sets are
    # disjoint by launch mode: `launch_flags` for interactive, `headless_flags`
    # for headless — a non-interactive run can't answer a permission prompt (it
    # auto-denies and the worker silently stalls), so e.g. claude gets its bypass
    # flag there; codex declares the same flag in both. They are NOT folded
    # together because a harness's interactive flag can be invalid headless —
    # `kimi -p` hard-errors on `--yolo`/`--auto` (prompt mode is always
    # auto-permission, no flag needed).
    sandbox_flags: list[str] = []
    sandbox_env: dict[str, str] = {}
    if os.environ.get("SC_SANDBOX"):
        scfg = adapter.get("sandbox") or {}
        key = "headless_flags" if headless else "launch_flags"
        sandbox_flags = list(scfg.get(key) or [])
        if sandbox_flags:
            print(f"→ sandbox: launch flags → {' '.join(sandbox_flags)}")
        # Sandbox-only launch env — e.g. claude's IS_SANDBOX=1, required because
        # the rootless container runs the harness as uid 0 and claude refuses
        # bypass-permissions mode as root unless the env marks it as sandboxed.
        sandbox_env = {k: str(v) for k, v in (scfg.get("env") or {}).items()}
        if sandbox_env:
            print(f"→ sandbox: launch env → {' '.join(sandbox_env)}")

    # Headless: resolve the non-interactive argv now (before RENDER_ONLY) so a
    # render-only run still validates the adapter + prints what would exec.
    headless_cmd = None
    if headless:
        hmodel = session_model  # resolved up front (persisted on the archive row)
        effective_prompt = prompt or DEFAULT_HEADLESS_PROMPT
        headless_cmd = headless_command(
            adapter, effective_prompt, hmodel, sandbox_flags,
            session_effort)
        if headless_cmd is None:
            sys.exit(f"sc run: harness '{harness}' has no headless adapter — "
                     f"use claude, codex, opencode, or kimi")
        if hmodel:
            src = "explicit -m" if flag_model else f"flavor default for {chosen['flavor']}"
            print(f"→ model: {hmodel} ({src})")
        print(f"→ effort: {session_effort}")
        print(f"→ headless prompt: {effective_prompt[:120]}")

    # Close the boot summary with the review GUI — the link lives in a different
    # place per fork, so every interactive boot restates it where it can't be
    # missed. Headless/verify keep the plain `→ api:` line instead.
    if not headless and not os.environ.get("RENDER_ONLY") and api_port:
        print(f"\n{review_gui_panel(api_port, bool(full['api_key']))}")

    if os.environ.get("RENDER_ONLY"):
        print("→ RENDER_ONLY set — not exec'ing the harness.")
        return

    # --name labels the session in the harness prompt box, resume picker, and
    # the terminal title — the cross-terminal way to show which shell you're in
    # (Konsole's tab is patched separately, since it ignores the program title).
    # Adapter-declared, so only harnesses that support it (claude) get the flag.
    name_args: list[str] = []
    ncfg = adapter.get("name") or {}
    if not headless and ncfg.get("flag") and full["display_name"]:
        name_args = [ncfg["flag"], full["display_name"]]

    managed = managed_mcp_injection(adapter)
    cmd = (
        headless_cmd
        if headless
        else (
            (adapter.get("launch") or [harness])
            + list((managed or {}).get("launch_args") or [])
            + name_args + model_args + sandbox_flags
        )
    )
    effort_env = headless_effort_env(adapter, session_effort) if headless else {}
    env = {**os.environ, **{k: str(v) for k, v in adapter.get("env", {}).items()},
           **sandbox_env, **effort_env}
    # The booted shell's flavor, inherited by everything the harness spawns.
    # branch-guard.sh reads it to exempt the admin shell (which works on main
    # by mandate); like SC_PROTECTED_BRANCHES it's a guardrail, not a boundary.
    env["SC_SHELL_FLAVOR"] = chosen["flavor"] or ""
    # The final exec environment is the authority boundary for every public
    # harness surface.  Never consume an inherited/deleted shell identity.
    env["SC_SHELL_ID"] = str(chosen["shell_id"])
    env["SC_SHELL_SHORTNAME"] = chosen["shortname"]
    env["SC_API_TOKEN"] = full["api_key"] or ""
    env["SC_API_BASE"] = f"http://127.0.0.1:{api_port}" if api_port else ""
    # Optional fast-path for the branch-guard hooks: the absolute engine path, so
    # they skip the `git rev-parse --git-common-dir` walk. NOT load-bearing — the
    # hooks resolve the engine env-independently (a fork gitignores .super-coder/,
    # so it is absent from worktrees; a worktree-relative path failed open). This
    # just saves a subshell per edit on the normal launch path.
    env["SC_ENGINE_DIR"] = str(ENGINE)
    env["SC_HARNESS"] = harness
    # The shell's HOME worktree — the dir we exec the harness from (below). The
    # branch-guard reads it to judge "outside your worktree" against the assigned
    # tree, not the live cwd: a shell whose cwd has drifted to the repo root (to
    # run a root-level command) is still working correctly when it edits into its
    # own worktree, and must not be warned. For admin this is REPO_ROOT, but admin
    # exits the guard earlier via SC_SHELL_FLAVOR, so it never reads this.
    env["SC_SHELL_WORKTREE"] = str(work_dir)
    # cwd-proofing (the recurring "my edits vanished" trap). The engine + its live
    # DBs sit at the MAIN worktree root, but the harness is exec'd from the shell's
    # own worktree (os.chdir(work_dir) below). Historically a shell would `cd` to the
    # root for a convenient `./sc …` call — and because Bash cwd persists, every
    # LATER bare git/grep then silently targeted the main tree (a different branch),
    # so the shell's own worktree edits looked gone. Kill the trigger structurally:
    # export the root and prepend it to PATH so `sc …` resolves bare from ANY cwd,
    # and raw DB reads can address the engine by $SC_ROOT — no `cd` ever needed. One
    # invariant ("never cd; address the engine by path") instead of per-command
    # vigilance. Works in both the docker sandbox and the no-docker host path since
    # run.py is the single exec chokepoint for every harness.
    env["SC_ROOT"] = str(REPO_ROOT)
    env["PATH"] = _shell_path(work_dir, env.get("PATH", ""))
    # Operator-declared shared dirs that all shells may write into without
    # branch-guard warnings — host-level handoff/screenshot folders. Set
    # SC_SHARED_DIRS (space-separated absolute paths) in the launch environment;
    # run.py passes it through automatically (via {**os.environ} above), so no
    # explicit assignment is needed. This comment documents it as a first-class
    # supported env var alongside SC_PROTECTED_BRANCHES and SC_SHELL_WORKTREE.
    if not headless:
        set_terminal_tab_title(full["display_name"])
    # H-25: claim the pid we are ABOUT to become for this shell.
    record_launch(full["shell_id"], work_dir, harness, headless=headless)
    os.chdir(work_dir)
    print(f"→ exec {' '.join(cmd)}\n")
    os.execvpe(cmd[0], cmd, env)


def _self_start_ticks() -> "int | None":
    """This process's start time — /proc/<pid>/stat field 22.

    The discriminator that makes a recorded pid safe to trust later: a pid on
    its own is a reusable integer, so a record naming a dead worker would
    resurrect as a false "working" the moment the kernel handed that number to
    something unrelated. execvpe does not reset the value — it belongs to the
    PROCESS, not to the program image — so reading it here, before the exec,
    describes the harness that is about to replace us.

    comm (field 2) may contain spaces and parens, so the split starts after the
    final ')': rest[0] is state (field 3), rest[19] is starttime (field 22) —
    the same indexing shell_liveness._stat_fields already uses.
    """
    try:
        data = (PROC_SELF_STAT).read_text()
    except OSError:
        return None
    rest = data[data.rfind(")") + 2:].split()
    try:
        return int(rest[19])
    except (IndexError, ValueError):
        return None


def record_launch(shell_id: int, work_dir: Path, harness: "str | None",
                  *, headless: bool) -> None:
    """Record the harness pid this launch is about to become (spec #76 H-25).

    HEADLESS ONLY, deliberately — hence the parameter rather than a caller-side
    `if`: the rule is the requirement, so it is stated and tested here. A
    headless worker runs detached — no controlling TTY, launcher gone,
    ppid==1 — so the
    lineage scan can only ever call it 'detached' and project 'unreconciled';
    between relaunches nothing holds the worktree at all and the same shell
    projects 'available'. The record is what replaces that inference. An
    INTERACTIVE boot has a live parent, which the lineage scan reads correctly
    today, and recording one would suppress a TRUE orphan verdict — a closed
    terminal's survivor would arrive pre-claimed and the operator would never be
    told to clear it. So the claim covers exactly the launches whose lineage is
    unreadable, and no others.

    One row per shell, upserted: the newest launch is the only claim, which is
    what "re-stamped on each relaunch" means. Accepted cost, stated in the spec's
    own terms: run.py execs and never returns, so nothing clears the row at exit
    — a worker that has finished for good keeps reading expected-but-absent
    until its next launch. That is the honest reading ("the record claimed a
    worker here; it is gone") and it is the evidence row feature #27's compare
    consumes; deciding whether an absence is a FAULT stays with #27.

    Best-effort: a failed stamp must never block a boot. The only consequence is
    that this session falls back to the lineage scan — where it was before.
    """
    if not headless:
        return
    ticks = _self_start_ticks()
    if ticks is None:
        return                       # non-Linux / unreadable — no claim to make
    try:
        con = open_db()
    except SystemExit:
        return
    try:
        con.execute(
            "INSERT INTO shell_launch_records "
            "(shell_id, pid, start_ticks, worktree, harness, launched_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(shell_id) DO UPDATE SET "
            " pid=excluded.pid, start_ticks=excluded.start_ticks,"
            " worktree=excluded.worktree, harness=excluded.harness,"
            " launched_at=excluded.launched_at",
            (shell_id, os.getpid(), ticks, str(work_dir), harness,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        con.commit()
    except (db_driver.OperationalError, db_driver.IntegrityError):
        # An un-migrated fork (no such table) or a busy writer. Neither is worth
        # a boot failure; the lineage fallback still answers.
        con.rollback()
    finally:
        con.close()


def set_terminal_tab_title(name: str) -> None:
    """Best-effort: pin this Konsole tab's title to the shell's name.

    Konsole's default tab format is ``%d : %n`` (dir : program), which ignores
    the window-title escapes the harness emits — so the tab never shows which
    shell you're talking to. We run *inside* the shell's Konsole session, so we
    set that session's tab title format to a literal over DBus (the same thing
    the GUI "Rename Tab" does). It persists for the tab and survives the
    harness's own title updates. No-op outside Konsole or if qdbus is absent.

    Non-Konsole terminals get the name via the harness itself (e.g. claude's
    ``--name`` writes it into the window title); this only patches Konsole's
    tab, which the standard title escapes can't reach.
    """
    svc = os.environ.get("KONSOLE_DBUS_SERVICE")
    sess = os.environ.get("KONSOLE_DBUS_SESSION")
    if not (svc and sess and name):
        return
    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    if not qdbus:
        return
    for ctx in ("0", "1"):  # 0 = local, 1 = remote (ssh) tab-title context
        try:
            subprocess.run(
                [qdbus, svc, sess,
                 "org.kde.konsole.Session.setTabTitleFormat", ctx, name],
                check=False, capture_output=True, timeout=3,
            )
        except Exception:
            pass


if __name__ == "__main__":
    from cli_entry import run_cli

    run_cli(main)
