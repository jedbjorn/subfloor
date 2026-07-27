#!/usr/bin/env python3
"""Cross-harness lifecycle hook adapters (spec #20 Harness Hooks, sprint 25
seq 7, task #83).

One authenticated contract across Claude, Codex, and Kimi: native harness
hook events are mapped onto the engine's hook vocabulary and POSTed by
scripts/interface_hook.py (the emitter) with ONLY event, session,
generation, sequence, PID identity, and token — prompt, tool, transcript,
and terminal content is discarded at the emitter, never forwarded.

This module owns:
- the contract event vocabulary (EVENTS) and the mandatory set a harness
  must support before sprint wake may ARM (MANDATORY — a gap blocks arming,
  never an ordinary chat, spec #20 Harness Hooks);
- the per-harness capability table (CAPABILITIES) — which native events each
  installed CLI generation can actually deliver, the minimum version the
  mapping was verified against, and the honesty notes about each harness's
  readiness semantics;
- the per-harness hook-config installers (install) — hook configuration is
  MERGED without replacing fork or user hooks: claude rides a per-session
  `--settings` overlay file (additive by design, zero clobber), codex merges
  event groups into the emitted project `.codex/hooks.json` (preserving the
  fork's existing groups), kimi appends a marker-fenced block to the
  user-level `config.toml` (the only config file kimi 0.27 reads).

Verified against the installed CLIs (2026-07-23):
- claude 2.1.217/2.1.218: SessionStart (startup|resume; fires DURING
  startup, pre-prompt — see readiness notes), UserPromptSubmit, Stop,
  SessionEnd, StopFailure. PermissionRequest exists but has NO result event,
  and AskUserQuestion has none — per spec a harness lacking distinct
  approval/user-input hooks stays `busy` during the wait (safe), so those
  events are deliberately not mapped.
- codex 0.145.0: hooks feature stable+on; SessionStart, UserPromptSubmit,
  Stop, SessionEnd (present in the binary, undocumented on the hooks page).
  No approval-result, user-input, interrupt, or failure events.
  CORRECTED 2026-07-27 (flag #303, measured on a live 0.145.0 TUI seat):
  codex does NOT fire SessionStart during session init. It DEFERS it to the
  first user prompt submission — a turn-less seat emits nothing at all, and
  the first turn then fires SessionStart, UserPromptSubmit and Stop together,
  in that order. Config discovery and trust are NOT implicated: the project
  `.codex/hooks.json` is discovered, parsed, validated (codex clamps our
  SessionEnd timeout) and persisted into `[hooks.state]` regardless. Its
  readiness class is therefore `first_turn_gated` — see the readiness notes.
- kimi 0.27.0: full 16-event HookEngine; SessionStart is awaited as the
  FINAL step of session creation (the strongest readiness signal of the
  three), UserPromptSubmit, Stop, Interrupt, StopFailure, SessionEnd,
  PermissionRequest + PermissionResult (a real approval pair). No
  AskUserQuestion hook event.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

# ── Contract vocabulary ─────────────────────────────────────────────────────

EVENTS = (
    "session_start",      # mandatory — readiness (see CAPABILITIES readiness)
    "prompt_submit",      # mandatory
    "turn_stop",          # mandatory
    "session_end",        # mandatory
    "approval_wait",      # optional — absent → harness stays busy (safe)
    "approval_result",    # optional
    "user_input_wait",    # optional
    "interrupt",          # optional — user cancel; the turn is over
    "failure",            # optional — turn failed; the turn is over
)
MANDATORY = ("session_start", "prompt_submit", "turn_stop", "session_end")

# Sources the hook-callback route accepts. `entrypoint` = the pane
# entrypoint's pre-exec identity claim (interface_exec); it proves PID
# identity, promotes the reservation, and proves the PROCESS is up — it is
# never proof the provider handshaked. `provider` = a native harness hook
# delivered through the emitter; its session_start is the real provider
# readiness signal, and for every harness EXCEPT a 'first_turn_gated' one it
# is also the only thing that moves starting→idle. On a first_turn_gated
# harness that hook cannot arrive unbidden, so the entrypoint's weaker proof
# makes the move instead and is stamped separately (see readiness notes).
SOURCES = ("entrypoint", "provider")

EMITTER = "interface_hook.py"


def _emitter_command(event: str) -> str:
    """The hook command every adapter registers. Reads per-session
    credentials from the launch env (never baked into config), passes the
    harness's PID ($PPID — the hook spawns as the harness's child via a
    shell), and discards the native payload on stdin (</dev/null — prompt
    and tool content never crosses the contract). `|| true` keeps a dead
    engine endpoint from ever breaking the harness UX (awaited hooks fail
    open)."""
    return (f'python3 "$SC_ENGINE_DIR/scripts/{EMITTER}" '
            f'--event {event} --pid "$PPID" </dev/null || true')


# ── Per-harness capability table ─────────────────────────────────────────────
# events: contract events the harness can actually deliver.
# min_version: oldest CLI release this mapping was verified against —
# anything older is treated as incapable (fail closed for arming, the
# ordinary chat is unaffected).
# readiness: WHEN the harness's own session_start arrives, and therefore how
# strong it is as a start-READY proof. Three classes, strongest first:
#
#   'session_created'  — fires after session construction completes (kimi:
#                        the awaited final step of createMain).
#   'startup_hook'     — fires during startup, before the interactive prompt
#                        is proven painted (claude). Weaker: the process is
#                        up and the provider handshaked, but the TUI may not
#                        have painted yet.
#   'first_turn_gated' — does not arrive at all until a human submits the
#                        first turn (codex 0.145.0, flag #303, measured).
#
# No CLI offers a later native prompt-ready signal, so for the first two the
# wake gate's quiet debounce + submit-hook fence absorb the residual window.
#
# 'first_turn_gated' cannot be handled that way, because there is no signal to
# debounce FROM: a codex seat that exists to be woken would wait forever on a
# hook only a human turn can trigger, so wake never arms and the deadlock is
# silent (flag #303). Such a harness is instead promoted starting -> idle on
# the ENTRYPOINT's pre-exec claim, which proves the PROCESS is up and nothing
# more. That weaker proof is recorded in its OWN column (process_ready_at,
# migration 0108) and never aliased into provider_ready_at, so no reader that
# trusts provider_ready_at as "the provider handshaked" becomes wrong.
#
# Read that as "proceed on weak proof, upgrade to strong proof when it
# arrives", NOT as lowering the bar: the first real turn still fires
# session_start and still stamps true provider readiness. And if the provider
# really is down, the submit goes out and FAILS loudly (the persistent
# gate-failure alert carries the reason verbatim) instead of the status quo,
# which is never arming at all, silently, forever — the direction decision #76
# requires. Sprint 84 U7, decisions #98/#99.
FIRST_TURN_GATED = "first_turn_gated"

CAPABILITIES = {
    "claude": {
        "min_version": (2, 1, 217),
        "events": ("session_start", "prompt_submit", "turn_stop",
                   "session_end", "failure"),
        "readiness": "startup_hook",
        "degraded": ("no approval-result or user-input hook events — the "
                     "session stays busy during those waits (safe); "
                     "SessionStart fires during startup, pre-prompt"),
    },
    "codex": {
        "min_version": (0, 145, 0),
        "events": ("session_start", "prompt_submit", "turn_stop",
                   "session_end"),
        "readiness": FIRST_TURN_GATED,
        "degraded": ("no approval, user-input, interrupt, or failure hook "
                     "events — approval waits stay busy (safe); SessionEnd "
                     "is undocumented but present in 0.145.0; SessionStart is "
                     "NOT delivered until the first user turn (flag #303)"),
    },
    "kimi": {
        "min_version": (0, 14, 0),
        "events": ("session_start", "prompt_submit", "turn_stop",
                   "session_end", "approval_wait", "approval_result",
                   "interrupt", "failure"),
        "readiness": "session_created",
        "degraded": ("no AskUserQuestion hook event — structured input "
                     "waits stay busy (safe)"),
    },
}


def _parse_version(text: "str | None") -> "tuple[int, ...] | None":
    """First dotted numeric triple in a `--version` line (claude prints
    '2.1.217 (Claude Code)', codex 'codex-cli 0.145.0')."""
    if not text:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if m is None:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def capability(harness: "str | None",
               cli_version: "str | None" = None) -> dict:
    """What one harness (at one installed version) can deliver against the
    contract. `mandatory_ok` is the sprint-wake ARMING gate: a harness
    lacking a mandatory hook — or below the verified version — blocks
    arming only, never the chat itself."""
    cap = CAPABILITIES.get(harness or "")
    if cap is None:
        return {"harness": harness, "cli_version": cli_version,
                "mandatory_ok": False, "missing_mandatory": list(MANDATORY),
                "events": {}, "readiness": "none", "version_ok": False,
                "degraded": (f"no hook adapter for harness {harness!r}",)}
    version = _parse_version(cli_version)
    version_ok = version is not None and version >= cap["min_version"]
    supported = set(cap["events"])
    missing = [e for e in MANDATORY if e not in supported]
    return {"harness": harness, "cli_version": cli_version,
            "mandatory_ok": version_ok and not missing,
            "missing_mandatory": missing,
            "events": {e: (e in supported) for e in EVENTS},
            "readiness": cap["readiness"], "version_ok": version_ok,
            "degraded": cap["degraded"]}


# ── Hook-config installers (merge, never replace) ───────────────────────────

def _claude_overlay(run_dir: Path, session_id: int) -> Path:
    """Claude: a per-session `--settings` overlay file. --settings loads
    ADDITIONAL settings for this session only — user, project, and local
    hooks stay live alongside ours; nothing is rewritten. The overlay
    carries no secrets (credentials travel in the launch env)."""
    def group(event, matcher=None):
        g = {"hooks": [{"type": "command",
                        "command": _emitter_command(
                            {"SessionStart": "session_start",
                             "UserPromptSubmit": "prompt_submit",
                             "Stop": "turn_stop",
                             "StopFailure": "failure",
                             "SessionEnd": "session_end"}[event]),
                        "timeout": 5}]}
        if matcher:
            g["matcher"] = matcher
        return g

    overlay = {"hooks": {
        "SessionStart": [group("SessionStart", "startup|resume")],
        "UserPromptSubmit": [group("UserPromptSubmit")],
        "Stop": [group("Stop")],
        "StopFailure": [group("StopFailure")],
        "SessionEnd": [group("SessionEnd")],
    }}
    path = run_dir / f"claude-hooks-{session_id}.json"
    path.write_text(json.dumps(overlay, indent=2) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return path


_CODEX_EVENTS = (("SessionStart", "session_start", "startup|resume"),
                 ("UserPromptSubmit", "prompt_submit", None),
                 ("Stop", "turn_stop", None),
                 ("SessionEnd", "session_end", None))


def _codex_merge(work_dir: Path) -> bool:
    """Codex: merge our event groups into the project `.codex/hooks.json`
    the adapter emits each launch (emit runs before this in the launch
    pipeline). Existing groups — the fork's PreToolUse branch-guard — are
    preserved; our groups are (re)written by contract-event identity, so
    re-install is idempotent. An unparseable file is left untouched rather
    than clobbered. Project-layer hooks load because prepare_launch already
    trusts the worktree and launches with --dangerously-bypass-hook-trust.
    """
    path = work_dir / ".codex" / "hooks.json"
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return False
    hooks = cfg.setdefault("hooks", {})
    for native, event, matcher in _CODEX_EVENTS:
        group: dict = {"hooks": [{"type": "command",
                                  "command": _emitter_command(event),
                                  "timeout": 10}]}
        if matcher:
            group["matcher"] = matcher
        # Replace only groups that are already ours (same emitter command);
        # never disturb a group the fork/user wrote for the same event.
        existing = hooks.get(native) or []
        kept = [g for g in existing
                if EMITTER not in json.dumps(g)]
        kept.append(group)
        hooks[native] = kept
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)
    return True


_KIMI_EVENTS = (("SessionStart", "session_start", "startup|resume"),
                ("UserPromptSubmit", "prompt_submit", None),
                ("Stop", "turn_stop", None),
                ("Interrupt", "interrupt", None),
                ("StopFailure", "failure", None),
                ("SessionEnd", "session_end", "exit"),
                ("PermissionRequest", "approval_wait", None),
                ("PermissionResult", "approval_result", None))

_KIMI_BEGIN = "# >>> super-coder interface hooks (managed — do not edit)"
_KIMI_END = "# <<< super-coder interface hooks"


def _kimi_config_path() -> Path:
    home = os.environ.get("KIMI_CODE_HOME")
    return (Path(home) if home else Path.home() / ".kimi-code") / "config.toml"


def _kimi_block() -> str:
    """The managed block: one [[hooks]] table per native event. Kimi 0.27
    reads ONLY the user-level config.toml (no project config), and its
    hook schema is strict (event/matcher/command/timeout only). Values
    travel in the launch env, so one static block serves every session."""
    lines = [_KIMI_BEGIN]
    for native, event, matcher in _KIMI_EVENTS:
        lines.append("[[hooks]]")
        lines.append(f'event = "{native}"')
        if matcher:
            lines.append(f'matcher = "{matcher}"')
        lines.append(f"command = '{_emitter_command(event)}'")
        lines.append("timeout = 10")
    lines.append(_KIMI_END)
    return "\n".join(lines) + "\n"


def _kimi_merge() -> bool:
    """Kimi: append-or-replace a marker-fenced managed block in the user's
    config.toml. Everything outside the markers — the user's own hooks and
    settings — is preserved byte-for-byte; TOML array-of-tables is additive,
    so our tables never replace existing [[hooks]]."""
    path = _kimi_config_path()
    block = _kimi_block()
    try:
        cur = path.read_text() if path.exists() else ""
    except OSError:
        return False
    if _KIMI_BEGIN in cur and _KIMI_END in cur:
        head, rest = cur.split(_KIMI_BEGIN, 1)
        _, tail = rest.split(_KIMI_END, 1)
        new = head + block + tail.lstrip("\n")
        if new == cur:
            return True
    else:
        sep = "" if not cur or cur.endswith("\n") else "\n"
        new = f"{cur}{sep}\n{block}" if cur else block
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def install(harness: "str | None", work_dir: Path, *, run_dir: Path,
            session_id: int,
            cli_version: "str | None" = None) -> dict:
    """Install one session's lifecycle hook config for its harness.

    Returns {"installed": bool, "argv": [...], "capability": capability()}.
    `argv` carries launch-flag additions (claude's --settings overlay).
    Installed=False means the harness is unversioned/unknown or the config
    write failed: the chat still launches, the provider session_start
    simply never arrives — lifecycle stays `starting`, sprint wake can
    never arm on it (fail closed, ordinary chat unaffected).
    """
    cap = capability(harness, cli_version)
    result = {"installed": False, "argv": [], "capability": cap}
    if not cap["mandatory_ok"]:
        return result
    if harness == "claude":
        run_dir.mkdir(parents=True, exist_ok=True)
        overlay = _claude_overlay(run_dir, session_id)
        result.update(installed=True, argv=["--settings", str(overlay)])
    elif harness == "codex":
        result["installed"] = _codex_merge(work_dir)
    elif harness == "kimi":
        result["installed"] = _kimi_merge()
    return result
