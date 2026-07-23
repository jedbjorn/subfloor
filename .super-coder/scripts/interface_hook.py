#!/usr/bin/env python3
"""Interface lifecycle hook emitter (spec #20 Harness Hooks, sprint 25
seq 7, task #83).

Invoked BY the harness (claude/codex/kimi) through the hook config
interface_hooks.install() merged at launch. It turns one native hook
firing into one authenticated contract callback:

    {"shell_id", "generation", "hook_seq", "event", "pid",
     "source": "provider"}

The callback contract DISCARDS content: the native payload arrives on
stdin (prompt text, tool input, transcripts) and is never read here — the
registered commands already redirect </dev/null — and nothing but the
fields above is sent. The bearer token, shell, and generation come from
the launch env the pane entrypoint injected (SC_INTERFACE_*), never from
config files or argv.

hook_seq is allocated from a per-generation counter file under the engine
run dir. The flock is held through the POST (flag #50, decisions #28/#31):
allocation order IS commit order, so a concurrent hook can never commit
first and strand the earlier event as a stale-sequence rejection. The
entrypoint's pre-exec session_start is always seq 1, so the counter starts
at 1 and the first harness-side hook issues 2. A crash between allocation
and POST leaves a gap; the receiver rejects only <= last, so gaps are
safe (a lost hook is a missed event, never a replay).

The emitter NEVER blocks the harness: several contract events ride awaited
native hooks (UserPromptSubmit/Stop/SessionEnd), so it posts with a short
timeout, retries once on transport failure, and exits 0 either way (the
registered commands also `|| true`). Failures go to stderr only, without
the token.

Usage (registered by interface_hooks; not a human surface):
    python3 interface_hook.py --event <contract-event> --pid <harness-pid>
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import interface_hooks  # noqa: E402

POST_TIMEOUT_S = 3
POST_RETRIES = 2  # one retry on transport failure only


class EmitError(Exception):
    """A local refusal (bad env, unknown event) — logged, exit 0 regardless."""


def _run_dir() -> Path:
    override = os.environ.get("SC_INTERFACE_RUN_DIR")
    return Path(override) if override else ENGINE / "run" / "interface"


def next_hook_seq(run_dir: Path, shell_id: int, generation: int) -> int:
    """Allocate the next durable hook sequence for this generation.

    The counter file starts at 1 (the entrypoint's pre-exec session_start
    is always seq 1), so the first harness-side hook issues 2. flock makes
    concurrent native hooks (kimi runs matches in parallel) allocate
    strictly monotonic sequences."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"hook-seq-{shell_id}-{generation}.seq"
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        nxt = _alloc_seq(fh)
        fcntl.flock(fh, fcntl.LOCK_UN)
    return nxt


def _alloc_seq(fh) -> int:
    """Allocate + persist the next sequence on an already-flocked counter
    file. The counter is written BEFORE the POST: a crash leaves a gap
    (safe — the receiver rejects only <= last), never a duplicate."""
    fh.seek(0)
    raw = fh.read().strip()
    last = int(raw) if raw else 1  # 1 = the entrypoint's session_start
    nxt = last + 1
    fh.seek(0)
    fh.truncate()
    fh.write(f"{nxt}\n")
    fh.flush()
    os.fsync(fh.fileno())
    return nxt


def emit_locked(run_dir: Path, shell_id: int, generation: int, body: dict,
                api_base: str, token: str) -> bool:
    """Allocate the hook sequence AND post the callback while holding the
    counter flock through the POST (flag #50, decisions #28/#31 — HARD
    seq-8 requirement): the flock serializes COMMIT, not just allocation.
    Without it two concurrent hooks allocated in order could POST out of
    order; the later seq would commit first and the earlier hook would be
    rejected as stale — a dropped prompt_submit strands its wake batch
    'submitting' with RESTART-ONLY recovery. Holding the lock through the
    POST makes allocation order == commit order, so that state is
    unreachable. The POST is short-timeout and the harness is local; the
    worst-case wait for a concurrent hook is bounded by POST_TIMEOUT_S."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"hook-seq-{shell_id}-{generation}.seq"
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            body["hook_seq"] = _alloc_seq(fh)
            return post_callback(api_base, token, body)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def post_callback(api_base: str, token: str, body: dict,
                  retries: int = POST_RETRIES,
                  timeout: float = POST_TIMEOUT_S,
                  sleep=time.sleep) -> bool:
    """POST the callback. Retries only on transport failure; an HTTP
    response — even a rejection — is definitive (a replayed sequence or
    stale generation will not heal in 300ms). Never logs the token."""
    url = f"{api_base.rstrip('/')}/api/interface/hook-callbacks"
    data = json.dumps(body).encode()
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ok = 200 <= resp.status < 300
                if not ok:
                    print(f"interface-hook: {body['event']} rejected "
                          f"(HTTP {resp.status})", file=sys.stderr)
                return ok
        except urllib.error.HTTPError as e:
            print(f"interface-hook: {body['event']} rejected (HTTP {e.code})",
                  file=sys.stderr)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            print(f"interface-hook: {body['event']} attempt "
                  f"{attempt}/{retries} failed ({reason})", file=sys.stderr)
            if attempt < retries:
                sleep(0.3)
    return False


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--pid", type=int, default=None)
    args = ap.parse_args(argv)

    # NOTE: stdin (the native hook payload — prompt/tool content) is
    # deliberately never read. The contract carries no content.
    try:
        if args.event not in interface_hooks.EVENTS:
            raise EmitError(f"unknown contract event {args.event!r}")
        token = os.environ.get("SC_INTERFACE_HOOK_TOKEN") or ""
        shell_id = os.environ.get("SC_INTERFACE_SHELL_ID") or ""
        generation = os.environ.get("SC_INTERFACE_GENERATION") or ""
        api_base = os.environ.get("SC_API_BASE") or ""
        if not token or not shell_id or not generation or not api_base:
            raise EmitError("SC_INTERFACE_HOOK_TOKEN / SC_INTERFACE_SHELL_ID "
                            "/ SC_INTERFACE_GENERATION / SC_API_BASE unset — "
                            "not an Interface-managed session")
        body = {"shell_id": int(shell_id), "generation": int(generation),
                "event": args.event, "source": "provider"}
        if args.pid is not None:
            body["pid"] = args.pid
        # Seq allocation AND the POST ride one flock (flag #50) — commit
        # order can no longer invert allocation order.
        emit_locked(_run_dir(), int(shell_id), int(generation), body,
                    api_base, token)
    except EmitError as e:
        print(f"interface-hook: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — never break the harness
        print(f"interface-hook: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
