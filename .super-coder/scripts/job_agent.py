"""Subagent mechanics for `sc job`: command, result capture, worktree, environment.

A subagent is one headless harness run launched by the job supervisor (spec
#273). Nothing here names a harness: every harness-specific fact is read from
the adapter manifest's `headless` block, including its optional `agent` keys,
and the model and effort are resolved by the code that serves `sc run`.

The command is built before the run is registered, so the run id is not yet
known. Arguments that depend on the run's evidence directory carry the
`{run_dir}` token, and a brief delivered as an argument is the `{brief}` token,
so the ledger records the command without the brief's text. `materialize`
replaces both once the run exists, and only at the positions the engine placed
them: a model, effort, session or branch that happens to spell a token is
passed through as the caller wrote it. The supervisor is another process, so
those positions travel to it in the run's `meta.json` as the spawn plan
(`spawn_plan`, `materialize_argv`); it never searches the argv for a token.

The worktree and the child environment are mechanics, not containment
(decision #427): a subagent acts under its launching shell's identity with
exactly that shell's authority.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import project_root
import run as run_mod
from runs import (
    AGENT_BRIEF_MAX,
    AGENT_FACTS_FILE,
    AGENT_RESULT_FILE,
    AGENT_WORKTREE_PREFIX,
    harness_name,
    route_value,
)

RUN_DIR = "{run_dir}"
BRIEF = "{brief}"
# While a command is assembled the engine's own tokens carry a control
# character no caller value may hold, so their positions are unambiguous.
_MARK = "\x1f"
BRIEF_FILE = "brief.md"
LAST_MESSAGE_FILE = "last-message.md"
STDERR_FILE = "stderr.log"
STDERR_TAIL = 65536
WHOLE_REPORT_MAX = 16 * 1024 * 1024
NO_RESULT = "This run ended without a final message.\n"
RUN_VARIABLE = "SC_JOB_AGENT_RUN"
SEAT_VARIABLE = "SC_SEAT"
DEFAULT_EFFORT = run_mod.route_transport.route_bindings.DEFAULT_EFFORT
_AGENT_KEYS = ("brief", "result", "session", "resume")
_RESULT_SOURCES = ("stdout_json", "stdout_jsonl", "file")


class AgentRefusal(ValueError):
    """A launch refused before anything is registered, created or started."""


class AgentCommand(NamedTuple):
    harness: str
    requested_model: str
    requested_effort: str | None
    effective_effort: str
    argv: list[str]
    env: dict[str, str]
    env_unset: tuple[str, ...]
    stdin: str | None
    result: dict
    session: dict
    session_ref: str | None
    resumes: str | None
    cli_version: str | None
    brief_index: int | None = None
    run_dir_indexes: tuple[int, ...] = ()

    def materialize(self, run_dir: Path) -> tuple[list[str], Path | None]:
        """The argv to spawn and the file to open as standard input.

        Only the positions recorded at build time are replaced; every other
        argument is passed exactly as recorded.
        """
        return materialize_argv(self.argv, run_dir, spawn_plan(self))


def spawn_plan(command: AgentCommand) -> dict:
    """What the supervisor needs to start this command, as plain data.

    It is written to the run's `meta.json`, beside the recorded argv. The
    token positions are the ones recorded when the command was built.
    """
    return {
        "harness": command.harness,
        "stdin": command.stdin is not None,
        "brief_index": command.brief_index,
        "run_dir_indexes": list(command.run_dir_indexes),
        "env": dict(command.env),
        "env_unset": list(command.env_unset),
        "result": dict(command.result),
        "session": dict(command.session),
        "session_ref": command.session_ref,
    }


def materialize_argv(argv: list[str], run_dir: Path, plan: dict) -> tuple[list[str], Path | None]:
    """Fill the engine's tokens at their recorded positions, and nowhere else.

    A plan that does not fit the argv is refused: a position outside it, or a
    position that does not hold the token the engine placed there.
    """
    argv = list(argv)
    brief_index = plan.get("brief_index")
    run_dir_indexes = plan.get("run_dir_indexes") or []
    positions = [*run_dir_indexes, *([] if brief_index is None else [brief_index])]
    if any(type(i) is not int or not 0 <= i < len(argv) for i in positions):
        raise ValueError("spawn plan names a position outside the recorded command")
    for index in run_dir_indexes:
        if not argv[index].startswith(RUN_DIR):
            raise ValueError("spawn plan and recorded command disagree on the run directory")
        argv[index] = argv[index].replace(RUN_DIR, str(run_dir), 1)
    if brief_index is not None:
        if argv[brief_index] != BRIEF:
            raise ValueError("spawn plan and recorded command disagree on the brief")
        argv[brief_index] = (run_dir / BRIEF_FILE).read_text()
    return argv, (run_dir / BRIEF_FILE if plan.get("stdin") else None)


def _disabled(env: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        value.strip().lower()
        for value in env.get("SC_DISABLED_HARNESSES", "").split(",")
        if value.strip()
    )


def agent_keys(adapter: dict) -> dict:
    """The adapter's subagent keys, or a refusal naming the first one missing."""
    harness = adapter.get("harness", "?")
    headless = adapter.get("headless") or {}
    agent = headless.get("agent")
    if not isinstance(agent, dict):
        raise AgentRefusal(
            f"harness '{harness}' cannot run a subagent: its adapter declares no "
            "`headless.agent` key"
        )
    for key in _AGENT_KEYS[:3]:
        if not isinstance(agent.get(key), dict):
            raise AgentRefusal(
                f"harness '{harness}' cannot run a subagent: its adapter declares no "
                f"`headless.agent.{key}` key"
            )
    if agent["brief"].get("delivery") not in ("stdin", "argument"):
        raise AgentRefusal(
            f"harness '{harness}': `headless.agent.brief.delivery` must be stdin or argument"
        )
    if agent["result"].get("source") not in _RESULT_SOURCES:
        raise AgentRefusal(
            f"harness '{harness}': `headless.agent.result.source` must be one of "
            f"{', '.join(_RESULT_SOURCES)}"
        )
    if agent["session"].get("source") not in ("pinned", "event"):
        raise AgentRefusal(
            f"harness '{harness}': `headless.agent.session.source` must be pinned or event"
        )
    return agent


def build_command(
    harness: str,
    model: str | None,
    effort: str | None = None,
    *,
    resume_session: str | None = None,
    adapter: dict | None = None,
    env: Mapping[str, str] | None = None,
    which=shutil.which,
    cli_version=None,
) -> AgentCommand:
    """Resolve one subagent launch into argv, environment and stdin source.

    Every refusal is raised here, before the caller registers a run. Effort is
    optional: omitted, no effort flag, config key or variable is sent and the
    harness's own default for the model applies. `sc run` instead falls back to
    a flavor effort and then to `high`; the difference is deliberate.
    """
    env = os.environ if env is None else env
    if not isinstance(harness, str) or not harness.strip():
        raise AgentRefusal("a subagent requires --harness")
    if not isinstance(model, str) or not model.strip():
        raise AgentRefusal(
            f"a subagent requires --model: name the model for harness '{harness}' exactly"
        )
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise AgentRefusal("effort must be a non-empty level when given")
    # Shape first: these values become argv elements and, for some harnesses,
    # the inside of a quoted config value.
    try:
        harness_name(harness)
        route_value(model, "model")
        route_value(effort, "effort", limit=64, required=False)
    except ValueError as exc:
        raise AgentRefusal(str(exc)) from exc
    if adapter is None:
        try:
            adapter = run_mod.load_adapter(harness)
        except ValueError as exc:
            raise AgentRefusal(f"harness '{harness}' is not shipped: {exc}") from exc
    headless = adapter.get("headless")
    if not isinstance(headless, dict) or not headless.get("launch"):
        raise AgentRefusal(f"harness '{harness}' has no headless adapter; it cannot run a subagent")
    try:
        run_mod.require_harness_surface(adapter, "one_shot")
    except ValueError as exc:
        raise AgentRefusal(str(exc)) from exc
    if harness.lower() in _disabled(env):
        raise AgentRefusal(f"harness '{harness}' is disabled by SC_DISABLED_HARNESSES")
    agent = agent_keys(adapter)
    binary = headless["launch"][0]
    if which(binary) is None:
        raise AgentRefusal(f"harness '{harness}' is not installed: `{binary}` is not on PATH")

    # An omitted effort is the model default, never the `sc run` fallback.
    try:
        route = run_mod.resolve_headless_route(
            harness=harness,
            adapter=adapter,
            flavor_model=None,
            model=model,
            effort=effort if effort is not None else DEFAULT_EFFORT,
        )
    except ValueError as exc:
        raise AgentRefusal(str(exc)) from exc

    flags = list(run_mod.launch_mode_flags(adapter, headless=True))
    command_adapter = adapter
    resume = agent.get("resume") if resume_session is not None else None
    session_ref = None
    tail: list[str] = []
    if resume_session is not None:
        if not isinstance(resume, dict) or not (resume.get("flag") or resume.get("subcommand")):
            raise AgentRefusal(
                f"harness '{harness}' cannot continue a subagent: its adapter declares "
                "no `headless.agent.resume` key"
            )
        try:
            route_value(resume_session, "session reference")
        except ValueError as exc:
            raise AgentRefusal(
                f"the earlier run recorded no usable session reference: {exc}"
            ) from exc
        session_ref = resume_session
        if resume.get("subcommand"):
            # A resume subcommand has its own option set: its declared launch
            # flags replace the headless ones, and the session leads the tail.
            command_adapter = {
                **adapter,
                "headless": {**headless, "launch": [*headless["launch"], *resume["subcommand"]]},
            }
            flags = list(resume.get("launch_flags") or [])
            tail = [resume_session]
        else:
            flags += [resume["flag"], resume_session]
    elif agent["session"]["source"] == "pinned":
        session_ref = str(uuid.uuid4())
        flags += [agent["session"]["flag"], session_ref]
    result = agent["result"]
    flags += list(result.get("flags") or [])
    if result["source"] == "file":
        flags += [result["file_flag"], f"{_MARK}{RUN_DIR}/{LAST_MESSAGE_FILE}"]

    try:
        argv = run_mod.headless_command(
            command_adapter, _MARK + BRIEF, model=route.model, launch_flags=flags,
            effort=route.effort,
        )
    except ValueError as exc:
        raise AgentRefusal(str(exc)) from exc
    assert argv and argv[-1] == _MARK + BRIEF  # the brief is always the final positional
    stdin = None
    if agent["brief"]["delivery"] == "stdin":
        stdin = f"{RUN_DIR}/{BRIEF_FILE}"
        argv = argv[:-1] + tail
        if agent["brief"].get("argument"):
            argv.append(agent["brief"]["argument"])
    else:
        argv = argv[:-1] + tail + [_MARK + BRIEF]
    # Record where the engine's tokens sit, then show them without the mark.
    brief_index = next((i for i, arg in enumerate(argv) if arg == _MARK + BRIEF), None)
    run_dir_indexes = tuple(
        i for i, arg in enumerate(argv) if arg.startswith(_MARK + RUN_DIR)
    )
    argv = [arg[len(_MARK):] if arg.startswith(_MARK) else arg for arg in argv]

    effort_env = (headless.get("effort") or {}).get("env")
    child_env = run_mod.headless_effort_env(adapter, route.effort)
    return AgentCommand(
        harness=harness,
        requested_model=model,
        requested_effort=effort,
        effective_effort=route.effort or DEFAULT_EFFORT,
        argv=argv,
        env=child_env,
        # An inherited effort variable would otherwise outlive an omitted effort.
        env_unset=(effort_env,) if effort_env and not child_env else (),
        stdin=stdin,
        result=dict(result),
        session=dict(agent["session"]),
        session_ref=session_ref,
        resumes=resume_session,
        cli_version=(cli_version or run_mod._cli_version)(binary),
        brief_index=brief_index,
        run_dir_indexes=run_dir_indexes,
    )


def validate_brief(text: str) -> bytes:
    """A brief is non-empty and at most 64 KiB; checked before registration."""
    data = text.encode()
    if not text.strip():
        raise AgentRefusal("the brief is empty")
    if len(data) > AGENT_BRIEF_MAX:
        raise AgentRefusal(
            f"brief is {len(data)} bytes; the limit is {AGENT_BRIEF_MAX} bytes (64 KiB)"
        )
    if "\0" in text:
        raise AgentRefusal("the brief contains a NUL byte")
    return data


# ── result capture ───────────────────────────────────────────────────────────

def _field(record: object, path: str) -> object:
    for part in path.split("."):
        if not isinstance(record, dict):
            return None
        record = record.get(part)
    return record


def _records(stdout: str) -> list[dict]:
    """JSON objects, one per line; anything else on the stream is skipped."""
    found = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _matching(records: list[dict], spec: dict) -> list[dict]:
    select = spec.get("select") or {}
    return [r for r in records if all(r.get(k) == v for k, v in select.items())]


def read_result(spec: dict, stdout: str, run_dir: Path | None = None) -> str | None:
    """The subagent's final message, or None when the run produced none."""
    source = spec["source"]
    if source == "file":
        path = (run_dir / LAST_MESSAGE_FILE) if run_dir else None
        if path is None or not path.is_file():
            return None
        text = path.read_text(errors="replace")
        return text if text.strip() else None
    if source == "stdout_json":
        try:
            value = _field(json.loads(stdout), spec["field"])
        except ValueError:
            return None
    else:
        values = [_field(r, spec["field"]) for r in _matching(_records(stdout), spec)]
        values = [v for v in values if isinstance(v, str) and v.strip()]
        value = values[-1] if values else None
    return value if isinstance(value, str) and value.strip() else None


def read_report(
    result: dict, session: dict, log: Path, run_dir: Path, pinned: str | None = None
) -> tuple[str | None, str | None]:
    """The final message and the session reference, from one pass over the log.

    The log of a long run is its whole event stream, so it is read a line at a
    time and only the last message and the first session reference are kept.
    A harness that reports one JSON document over several lines is read whole,
    up to `WHOLE_REPORT_MAX`.
    """
    message = read_result(result, "", run_dir) if result["source"] == "file" else None
    found = pinned if session["source"] == "pinned" else None
    from_stream = result["source"] != "file"
    with log.open(errors="replace") as handle:
        for line in handle:
            wants_session = session["source"] == "event" and found is None
            if not from_stream and not wants_session:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if wants_session and _matching([record], session):
                value = _field(record, session["field"])
                if isinstance(value, str) and value.strip():
                    found = value
            if from_stream and _matching([record], result):
                value = _field(record, result["field"])
                if isinstance(value, str) and value.strip():
                    message = value
    if (
        message is None and result["source"] == "stdout_json"
        and log.stat().st_size <= WHOLE_REPORT_MAX
    ):
        message = read_result(result, log.read_text(errors="replace"), run_dir)
    return message, found


def read_session(spec: dict, stdout: str, pinned: str | None = None) -> str | None:
    """The harness session reference: the pinned one, or the first one reported."""
    if spec["source"] == "pinned":
        return pinned
    for record in _matching(_records(stdout), spec):
        value = _field(record, spec["field"])
        if isinstance(value, str) and value.strip():
            return value
    return None


# ── worktree ─────────────────────────────────────────────────────────────────

def worktree_path(run_id: int, root: Path | None = None) -> Path:
    root = root or project_root.ENGINE.parent
    return root / ".sc-worktrees" / f"{AGENT_WORKTREE_PREFIX}{int(run_id)}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def resolve_base(ref: str, cwd: Path) -> str:
    """The full commit id a base ref names in the launching shell's checkout."""
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
        raise AgentRefusal("--base must name a commit")
    found = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0 or not found.stdout.strip():
        raise AgentRefusal(f"--base {ref!r} does not name a commit in {cwd}")
    return found.stdout.strip()


def validate_branch(name: str, root: Path) -> str:
    """A new branch name git accepts and that no branch already holds."""
    if not isinstance(name, str) or name.startswith("-"):
        raise AgentRefusal("--branch must be a branch name")
    if _git(root, "check-ref-format", "--branch", name).returncode != 0:
        raise AgentRefusal(f"--branch {name!r} is not a valid branch name")
    if _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
        raise AgentRefusal(f"--branch {name!r} already exists; name a new branch")
    return name


def create_worktree(
    run_id: int,
    base_commit: str,
    branch: str | None = None,
    *,
    root: Path | None = None,
    path: Path | None = None,
    reuse_branch: bool = False,
) -> Path:
    """Create `.sc-worktrees/agent-<run_id>` at the base: detached, or on a new branch.

    The boot files are ignored by git, so the worktree carries none: a harness
    started there is a plain harness, never the launching shell.

    A continue recreates a removed worktree at the earlier run's `path`, and
    with `reuse_branch` checks out the branch that run created when it still
    exists.
    """
    root = root or project_root.ENGINE.parent
    path = path or worktree_path(run_id, root)
    if path.exists():
        raise RuntimeError(
            f"{path} already exists, left by an earlier run of this number; "
            "remove it and launch again"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["worktree", "add"]
    target = base_commit
    if branch and reuse_branch and _git(
        root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0:
        target = branch
    else:
        command += ["-b", branch] if branch else ["--detach"]
    made = _git(root, *command, str(path), target)
    if made.returncode != 0:
        raise RuntimeError(
            f"could not create subagent worktree {path}: {made.stderr.strip()}"
        )
    return path


def create_worktree_or_fail(
    run_id: int,
    base_commit: str,
    branch: str | None = None,
    *,
    submit,
    root: Path | None = None,
    path: Path | None = None,
    reuse_branch: bool = False,
) -> Path | None:
    """Create the worktree, or end the registered run as failed with the reason.

    The run is registered before its worktree exists, so a creation that fails
    must not leave a registered row with no process holding a slot. `submit`
    takes the terminal payload (the run's terminal route). Returns None when
    the run was ended.
    """
    try:
        return create_worktree(
            run_id, base_commit, branch, root=root, path=path, reuse_branch=reuse_branch
        )
    except (RuntimeError, OSError) as exc:
        submit({
            "state": "failed",
            "exit_code": 127,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "spawn_error": f"subagent not started: {exc}"[:256],
        })
        return None


def worktree_state(path: Path, base_commit: str) -> dict:
    """What a subagent left in its worktree: HEAD, and whether anything changed.

    `unchanged` is the removal rule: no uncommitted change (untracked files
    count; ignored ones do not) and HEAD still the base commit. A worktree
    that cannot be read is never reported unchanged.
    """
    if not path.is_dir():
        return {"exists": False, "head_commit": None, "uncommitted": None, "unchanged": False}
    head = _git(path, "rev-parse", "--verify", "--quiet", "HEAD")
    # The answer decides whether a worktree is deleted, so it does not depend
    # on configuration: untracked files are always listed
    # (`status.showUntrackedFiles`), submodule changes always count
    # (`diff.ignoreSubmodules`, `submodule.<name>.ignore`), and a user's
    # global excludes file hides nothing. Only the repository's own ignore
    # rules decide what is ignored, and an ignored file is not work.
    status = _git(
        path, "-c", "core.excludesFile=/dev/null", "status", "--porcelain",
        "--untracked-files=all", "--ignore-submodules=none", "--no-renames",
    )
    head_commit = head.stdout.strip() if head.returncode == 0 else None
    uncommitted = bool(status.stdout.strip()) if status.returncode == 0 else None
    return {
        "exists": True,
        "head_commit": head_commit,
        "uncommitted": uncommitted,
        "unchanged": uncommitted is False and head_commit == base_commit,
    }


def unpushed_commits(path: Path, base_commit: str) -> int | None:
    """Commits made since the base that no remote branch holds; None when unreadable."""
    found = _git(path, "rev-list", "--count", f"{base_commit}..HEAD", "--not", "--remotes")
    try:
        return int(found.stdout.strip()) if found.returncode == 0 else None
    except ValueError:
        return None


def remove_worktree(path: Path, root: Path | None = None) -> None:
    """Remove a subagent worktree the caller has already judged safe to remove."""
    root = root or path.parents[1]
    done = _git(root, "worktree", "remove", "--force", str(path))
    if done.returncode != 0 and path.exists():
        raise RuntimeError(f"could not remove subagent worktree {path}: {done.stderr.strip()}")
    _git(root, "worktree", "prune")


def write_result(run_dir: Path, message: str | None) -> int:
    """Write `result.md`; the size reported is the final message's, 0 when none."""
    (run_dir / AGENT_RESULT_FILE).write_text(message if message else NO_RESULT)
    return len(message.encode()) if message else 0


def write_facts(run_dir: Path, facts: dict) -> None:
    """Persist the run's facts beside its evidence, for recovery at terminal."""
    path = run_dir / AGENT_FACTS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(facts, sort_keys=True))
    os.replace(tmp, path)


def usable_session(value: object) -> str | None:
    """A session reference that can be an argument of a later resume, or None."""
    try:
        return route_value(value, "session_ref")
    except ValueError:
        return None


# ── child environment ────────────────────────────────────────────────────────

def child_environment(
    run_id: int,
    command: AgentCommand | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    plan: dict | None = None,
) -> dict[str, str]:
    """The launching shell's environment, as the subagent receives it.

    Identity is untouched: the API token, shell identity and git identity pass
    through, so the engine sees the subagent's calls as the launching shell's.
    Two pieces of mechanics differ. The seat marker is cleared, so a dev-kit
    hook the subagent runs stays in the foreground instead of becoming a
    background job that wakes the launching shell. The run id is set; it is
    read only for the nesting limit and for tagging usage with the run.
    """
    env = project_root.scrubbed(environ)
    env.pop(SEAT_VARIABLE, None)
    env.pop("SC_DEVKIT_RUN_ID", None)
    env[RUN_VARIABLE] = str(int(run_id))
    if command is not None:
        plan = spawn_plan(command)
    if plan is not None:
        for name in plan.get("env_unset") or ():
            env.pop(name, None)
        env.update(plan.get("env") or {})
    return env


def launched_from_run(environ: Mapping[str, str] | None = None) -> str | None:
    """The subagent run this process is inside, sent with every agent registration."""
    value = (os.environ if environ is None else environ).get(RUN_VARIABLE, "").strip()
    return value or None
