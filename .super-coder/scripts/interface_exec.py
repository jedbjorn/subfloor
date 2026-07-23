#!/usr/bin/env python3
"""Interface pane entrypoint — the generation-capability launch (spec #20,
sprint 25 seq 5).

The engine API reserves a generation, writes a single-use launch token
(mode 0600) to `.super-coder/run/interface/launch-<session_id>.json`, and
points a private tmux pane's shell line at this script. This process then:

    1. reads + validates the token — the reservation capability. Anything
       unreadable, unparsable, or missing fields refuses (exit 2) BEFORE
       any archive row exists: no capability, no launch, no session.
    2. deletes the token (single use — a second invocation refuses).
    3. chdirs into the token's worktree.
    4. prepares the launch through run.py's `prepare_launch` — the NORMAL
       harness/model/effort/permission/worktree/render/boot/archive path
       (exit 3 on refusal), then merges the harness's authenticated
       lifecycle hook config (interface_hooks — never replacing fork/user
       hooks) and injects the emitter's per-generation credentials into
       the launch env;
    5. confirms identity to the API with an entrypoint session_start hook
       callback, which promotes the reservation reserved→occupied (exit 4
       when the API is unreachable or rejects — fail closed: never start an
       unmanaged harness; an unpromoted reservation expires into
       unreconciled for the operator). This is IDENTITY only: lifecycle
       idle + composer clean wait for the harness's own provider
       session_start hook (seq 7 hardening);
    6. execvpe's the harness TUI — this process BECOMES the harness,
       keeping the pane PID (the pane shell line exec's all the way down).

The hook_token is a credential: it is never printed, logged, or echoed in
an error message.

Usage:
    python3 .super-coder/scripts/interface_exec.py <token_file>
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
RUN_DIR = ENGINE / "run" / "interface"  # gitignored runtime home
sys.path.insert(0, str(ENGINE / "scripts"))
import run as run_mod  # noqa: E402
import interface_hooks  # noqa: E402

# Fields the reservation capability must carry (harness/model/effort are
# optional route hints — prepare_launch resolves their defaults).
REQUIRED_FIELDS = ("session_id", "shell_id", "generation", "hook_token",
                   "api_port", "worktree")

POST_RETRIES = 3
POST_TIMEOUT_S = 5
POST_BACKOFF_S = 1


class TokenError(Exception):
    """The reservation capability is absent or malformed — exit 2."""


def _read_token(path: Path) -> dict:
    """Read + validate the launch token. Error messages name the failure,
    never the file's contents (the hook_token must not leak)."""
    try:
        raw = path.read_text()
    except OSError:
        raise TokenError("no launch reservation (token file unreadable) — "
                         "a chat starts from the engine UI, not by hand")
    try:
        token = json.loads(raw)
    except json.JSONDecodeError:
        raise TokenError("launch reservation is not valid JSON")
    if not isinstance(token, dict):
        raise TokenError("launch reservation is malformed (not an object)")
    missing = [f for f in REQUIRED_FIELDS if token.get(f) in (None, "")]
    if missing:
        raise TokenError("launch reservation is missing field(s): "
                         + ", ".join(missing))
    return token


def _start_ticks() -> "int | None":
    """Field 22 of /proc/self/stat (process start time, clock ticks since
    boot) — with the PID, an exact process-identity proof for the API.
    Field 2 (comm) may contain spaces/parens, so parse AFTER the last ')'.
    Best-effort: None when unreadable."""
    try:
        stat = Path("/proc/self/stat").read_text()
        # After the last ')' the fields resume at 3 (state); field 22 is
        # index 19 of that remainder.
        return int(stat[stat.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _post_session_start(api_port: int, hook_token: str, body: dict,
                        retries: int = POST_RETRIES,
                        timeout: float = POST_TIMEOUT_S,
                        backoff: float = POST_BACKOFF_S,
                        sleep=time.sleep) -> bool:
    """POST the session_start hook. Retry only on transport failure; an
    HTTP response — even a rejection — is a definitive answer (a 4xx will
    not heal in 1s, and re-POSTing a rejected capability is noise). The
    token travels only in the Authorization header; errors report the
    status, never the request."""
    url = f"http://127.0.0.1:{api_port}/api/interface/hook-callbacks"
    data = json.dumps(body).encode()
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Authorization": f"Bearer {hook_token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
                print(f"interface-exec: session_start rejected "
                      f"(HTTP {resp.status})", file=sys.stderr)
                return False
        except urllib.error.HTTPError as e:
            print(f"interface-exec: session_start rejected "
                  f"(HTTP {e.code})", file=sys.stderr)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            print(f"interface-exec: session_start attempt {attempt}/{retries} "
                  f"failed ({reason})", file=sys.stderr)
            if attempt < retries:
                sleep(backoff)
    return False


def _exec(argv: list[str], env: dict[str, str]) -> None:
    """The exec seam — injectable so tests can capture instead of exec."""
    os.execvpe(argv[0], argv, env)


def main(argv: "list[str] | None" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: interface_exec.py <token_file>", file=sys.stderr)
        return 2

    token_path = Path(args[0])
    try:
        token = _read_token(token_path)
    except TokenError as e:
        print(f"interface-exec: {e}", file=sys.stderr)
        return 2
    # Single use: the capability is consumed the moment it parses.
    try:
        token_path.unlink()
    except OSError:
        pass

    worktree = Path(token["worktree"])
    if not worktree.is_dir():
        print(f"interface-exec: worktree {worktree} is not a directory",
              file=sys.stderr)
        return 2
    os.chdir(worktree)

    try:
        plan = run_mod.prepare_launch(
            shell_id=int(token["shell_id"]),
            harness=token.get("harness"),
            model=token.get("model"),
            effort=token.get("effort"))
    except SystemExit as e:
        print(f"interface-exec: launch refused ({e})", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"interface-exec: launch preparation failed "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        return 3

    # Lifecycle hooks (sprint 25 seq 7): merge this harness's authenticated
    # hook config WITHOUT replacing fork/user hooks, and hand the emitter
    # its per-generation credentials through the launch env — the token
    # never touches a config file, argv, or stderr. A harness whose hooks
    # can't install still launches (ordinary chat unaffected); without the
    # provider session_start the session simply never becomes wake-armable.
    hook_install = interface_hooks.install(
        plan.harness, Path(plan.cwd), run_dir=RUN_DIR,
        session_id=int(token["session_id"]), cli_version=plan.cli_version)
    argv = list(plan.argv) + hook_install["argv"]
    env = dict(plan.env)
    env["SC_INTERFACE_HOOK_TOKEN"] = token["hook_token"]
    env["SC_INTERFACE_SHELL_ID"] = str(token["shell_id"])
    env["SC_INTERFACE_GENERATION"] = str(token["generation"])

    body = {
        "shell_id": int(token["shell_id"]),
        "generation": int(token["generation"]),
        "hook_seq": 1,
        "event": "session_start",
        "source": "entrypoint",
        "archive_id": plan.archive_id,
        "pid": os.getpid(),
        "start_ticks": _start_ticks(),
    }
    if plan.cli_version:
        body["cli_version"] = plan.cli_version
    if not _post_session_start(int(token["api_port"]), token["hook_token"],
                               body):
        # Fail closed: an unmanaged harness must never start. The
        # unpromoted reservation expires into unreconciled; the operator
        # reconciles it.
        print("interface-exec: could not confirm session with the engine "
              "API — not starting the harness", file=sys.stderr)
        return 4

    os.chdir(plan.cwd)
    _exec(argv, env)
    return 0  # unreachable: _exec replaces the process


if __name__ == "__main__":
    sys.exit(main())
