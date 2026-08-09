#!/usr/bin/env python3
"""One-time bridge for update behavior introduced after a fork's old floor.

``sc update`` executes the updater already installed in a fork. Materializing
a newer ``update.py`` changes the file on disk, but it cannot change the Python
module already running. The legacy updater does, however, launch the newly
materialized ``map_setup.py`` in a fresh process. That script invokes this
bridge before mapping so the first adoption run can perform path repairs that
otherwise would wait for a second update.

The bridge is needed exactly when the current engine ref contains this file but
the previous engine ref did not. A local marker prevents manual map-setup runs
from repeating broker restarts before the next engine update advances the
previous ref. A separate ref marker makes each newly materialized engine run
its own managed-skill sweep once, so new tombstones are interpreted by the new
projection code even when the parent updater was already loaded.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import sc_wrapper

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
STATE_DIR = REPO_ROOT / ".sc-state"
ENGINE_REF = STATE_DIR / "engine.ref"
ENGINE_REF_PREV = STATE_DIR / "engine.ref.prev"
MARKER = STATE_DIR / "local" / "update-compat-v1.done"
SKILL_SWEEP_MARKER = STATE_DIR / "local" / "update-compat-skill-sweep.ref"
BRIDGE_PATH = ".super-coder/scripts/update_compat.py"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _read_ref(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if _SHA_RE.fullmatch(value) else None


def _pending_update_ref() -> str | None:
    """Return a current updater's valid, not-yet-published target."""
    pending = os.environ.get("SC_UPDATE_TARGET_REF", "").strip()
    return pending if _SHA_RE.fullmatch(pending) else None


def _current_update_ref() -> str | None:
    """Prefer the not-yet-published target supplied by a current updater."""
    return _pending_update_ref() or _read_ref(ENGINE_REF)


def _installed_repo() -> bool:
    try:
        data = json.loads((ENGINE / "instance.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("installed_at"))


def reconcile_host_wrapper() -> None:
    """Adopt the host wrapper even on the first update to this engine floor."""
    if not _installed_repo():
        return
    try:
        result = sc_wrapper.register_install(REPO_ROOT)
    except sc_wrapper.WrapperError as exc:
        raise SystemExit(f"update compatibility: {exc}") from exc
    print(f"→ managed host sc wrapper: {result}")


def _ref_contains_bridge(ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{ref}:{BRIDGE_PATH}"],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0


def needs_legacy_bridge() -> tuple[bool, str | None]:
    """Return whether this is the first update across the bridge boundary."""
    if MARKER.is_file():
        return False, None
    current = _read_ref(ENGINE_REF)
    previous = _read_ref(ENGINE_REF_PREV)
    if current is None or previous is None:
        return False, None
    if not _ref_contains_bridge(current) or _ref_contains_bridge(previous):
        return False, None
    return True, current


def main() -> int:
    reconcile_host_wrapper()
    pending = _pending_update_ref()
    current = _current_update_ref()
    if current is not None:
        # Unlike the path-repair migration below, dispatcher coherence is a
        # standing invariant. Existing forks may already carry the v1 marker
        # while their tracked bootstrap still routes to a retired script.
        import update

        update.repair_callable_dispatcher(current)
        # A current updater publishes only after migrate + snapshot. Defer its
        # linked-worktree overlays until then so a crash cannot leave bytes that
        # neither the old pin nor a later target recognizes. A legacy updater
        # supplies no pending ref and has already published before this bridge.
        if pending is None:
            update.reconcile_linked_dispatchers(current)
        if _read_ref(SKILL_SWEEP_MARKER) != current:
            print(
                "→ update compatibility: reconcile managed skill projections "
                f"at {current[:12]}"
            )
            update.reconcile_skill_projections()
            SKILL_SWEEP_MARKER.parent.mkdir(parents=True, exist_ok=True)
            SKILL_SWEEP_MARKER.write_text(f"{current}\n")

    needed, current = needs_legacy_bridge()
    if not needed:
        return 0

    print("→ legacy update bridge: finish relocated-fork path repair")
    update.repair_git_worktrees()
    update.refresh_installed_brokers()

    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(f"{current}\n")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
