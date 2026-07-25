"""live_model — the model a session is using RIGHT NOW, read from its transcript.

The launch route (`interface_sessions.model_route`) records what a session was
STARTED with. An in-harness `/model` switch never reaches the engine, so the
launch route silently goes stale (decision #55, superseding #53). Harness
transcripts, however, carry per-message model ids — so an explicit `last_model`
derived from the last main-thread assistant message is truthful where the
launch label and the analytics aggregate are not (flag #136: the
`session_token_usage` schema degenerates to parser insertion order and cannot
express an A->B->A switch at all).

This package is that probe. It is a READER of third-party on-disk formats, the
same seam as `token_parsers` and under the same stance: version drift is
accepted, a probe that breaks gets fixed forward. It shares nothing with the
analytics sweep and reads no engine table — the two answer different questions
and decision #55 keeps them apart on purpose.

Contract — every harness module exposes:

    HARNESS = "<name>"
    def read(worktree) -> dict | None

  worktree  str|Path — the shell's exec cwd (`run.shell_work_dir`), the same
            path the harness itself was launched in.
  returns   {"model": str, "observed_at": str|None, "source": str} for the last
            MAIN-THREAD assistant message carrying an EXPLICIT model id, or
            None when the current session has no such record. None is a real
            answer (fresh session, unreadable dir, nothing but placeholders) —
            never an exception.

Rules, in decision #55's spirit — the claim this feature makes is exactly
"`live_model` is the model id of the last main-thread assistant message in the
session's transcript at `observed_at`", and nothing stronger:

  * ONLY an explicit per-message id counts. Never inferred from launch
    arguments, config defaults, or process names. A harness that records the
    model once per session (vibe's `config.active_model`) cannot express a
    switch and is `unsupported`, not degraded-guessed.
  * MAIN THREAD only, via each harness's own structural signal — claude keeps
    subagents in a subdirectory, kimi in `agents/agent-N/`, opencode in a child
    `session` row. A subagent's model must never be reported as the session's;
    a harness that cannot make the distinction is `unsupported` rather than
    sometimes-wrong.
  * The LAST explicit id wins, including a return to a model already seen
    (A->B->A). Never dedupe by model, never let a first occurrence win.
  * Placeholders are not model ids — see `placeholder()`.

Supported set is the 44-U0 scout's verdict, in real bytes:

  claude    `message.model` on every assistant record            supported
  kimi      `llm.request.modelAlias`, per request                supported
  opencode  `message.data.modelID`, 2939/2939 assistant rows     supported
  codex     `turn_context.payload.model` IS per-turn and explicit, but no
            rollout on any inspected host distinguishes a subagent turn from a
            main one (651 rollouts, zero agent-attribution keys), and codex
            could not be capture-run to settle it.               UNSUPPORTED
  vibe      session-level `config.active_model` only             UNSUPPORTED

`probe()` is the only entry point callers use. It is called from the Interface
shells route on the rail's 5s poll, so it carries a short TTL cache and — per
spec doc 44 — it NEVER raises: an unreadable transcript is a `none` verdict
logged once, not a 500 on a route that also carries every other shell.
"""
from __future__ import annotations

import sys
import time as _time

from token_parsers import iso_utc, norm_iso  # noqa: F401  (shared ISO helpers)

# Harnesses with a verified per-message model id AND a verified main-vs-subagent
# distinction. Everything else answers `unsupported` — an honest verdict.
SUPPORTED = ("claude", "kimi", "opencode")

CACHE_TTL_S = 5.0  # the rail poll's own period: one disk read per poll, not per shell-row

_cache: "dict[tuple, tuple]" = {}   # (harness, worktree) -> (expires_at, result)
_logged: "set[tuple]" = set()       # (harness, worktree, kind) — log once, not per poll


def _default_log(msg: str) -> None:
    print(f"[live_model] {msg}", file=sys.stderr, flush=True)


def placeholder(model) -> bool:
    """True when a record's `model` is present but is not a real model id.

    Claude writes the literal `<synthetic>` on locally-generated assistant
    records — "No response requested.", API-error stubs — and there are 50 such
    records in this host's corpus. A naive last-record read reports
    `<synthetic>` as the live model; the probe must skip it and keep walking
    back, exactly as it skips a malformed line. The rule is the shape, not the
    one value: an id wrapped in angle brackets is a placeholder the harness
    substituted for a model it never called.
    """
    if model is None:
        return True
    m = str(model).strip()
    if not m:
        return True
    return m.startswith("<") and m.endswith(">")


def _module(harness: str):
    if harness == "claude":
        from . import claude
        return claude
    if harness == "kimi":
        from . import kimi
        return kimi
    if harness == "opencode":
        from . import opencode
        return opencode
    return None


def _read(harness: str, worktree: str, log) -> "dict | None":
    """The cached per-harness read. Failure of ANY kind is a None answer.

    The boundary is deliberately broad and sits here rather than inside each
    module: this runs on the shells route, which projects every shell in the
    rail, so one unreadable transcript must cost one field and not the whole
    response. The harness modules already handle what they can name (missing
    dir, unreadable file, malformed record); this catches what they cannot —
    a permission change, a format drift that trips an attribute error, a
    corrupt SQLite page.
    """
    key = (harness, worktree)
    now = _time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    mod = _module(harness)
    try:
        result = mod.read(worktree) if mod is not None else None
    except Exception as e:  # noqa: BLE001 — see docstring: never raise on the route
        result = None
        mark = (harness, worktree, type(e).__name__)
        if mark not in _logged:
            _logged.add(mark)
            log(f"{harness}: probe failed for {worktree}: {type(e).__name__}: {e}")
    _cache[key] = (now + CACHE_TTL_S, result)
    return result


def cache_clear() -> None:
    """Drop the TTL cache and the log-once ledger (tests; process teardown)."""
    _cache.clear()
    _logged.clear()


def probe(harness, worktree, active_since=None, log=None) -> dict:
    """The live model for one shell's session.

    active_since  The session's last known activity from the ENGINE's side
                  (the Interface's `last_human_input_at`). A transcript
                  observation older than that means the operator has acted
                  since the last thing we can see, so the reading may already
                  be wrong -> `stale`, and the consumer falls back to the
                  launch label. Absent or unparseable -> freshness is not
                  asserted.

                  It arrives in the ENGINE's timestamp form, not a harness's:
                  `interface_broker._now()` is `SELECT datetime('now')`, which
                  writes `YYYY-MM-DD HH:MM:SS` — space-separated, no `Z` —
                  while `observed_at` is `norm_iso`'s `…THH:MM:SSZ`. Comparing
                  those two as strings is not a time comparison at all: `'T'`
                  sorts above `' '`, so a same-day activity AFTER the
                  observation compares as earlier and `stale` can never fire on
                  the one timescale a 5s-poll badge lives on. Both sides go
                  through `norm_iso` before any comparison; it accepts both
                  forms and returns the one canonical form (REV2 SC-165).

    Always returns the full shape; the verdict is what keeps the claim honest:

      ok           a main-thread explicit id, not older than `active_since`
      stale        found, but the session moved after we observed it
      none         supported harness, nothing to report yet (fresh session,
                   unreadable transcript, placeholders only)
      unsupported  this harness cannot express the answer (codex, vibe), or
                   the harness is unknown/absent
    """
    log = log or _default_log
    blank = {"last_model": None, "live_model_at": None, "source": None}
    if not harness or harness not in SUPPORTED:
        return {**blank, "verdict": "unsupported"}
    if not worktree:
        return {**blank, "verdict": "none"}
    result = _read(harness, str(worktree), log)
    if not result or not result.get("model"):
        return {**blank, "verdict": "none"}
    observed_at = result.get("observed_at")
    since = norm_iso(active_since)
    stale = bool(since and observed_at and observed_at < since)
    return {"last_model": result["model"], "live_model_at": observed_at,
            "source": result.get("source"),
            "verdict": "stale" if stale else "ok"}
