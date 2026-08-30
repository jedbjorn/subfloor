#!/usr/bin/env python3
"""./sc feature — the front door to optional engine infrastructure.

Each feature controls one `instance.json` block that enables an engine-supplied
sidecar or host broker. The engine exposes the mechanism and its link boundary;
fork-specific operating procedure belongs in a DB-canonical local skill designed
by the Planner with `fork_skill_design`.

The vm/ts/pm2 blocks carry operator-verified host configuration and therefore
remain link-only. Postgres needs no host input, so its block can be created
directly.

Usage:
    ./sc feature                      # = list
    ./sc feature list
    ./sc feature enable  <name>       # pg · windows · tailnet · pm2
    ./sc feature disable <name>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import instance_state

ENGINE = Path(__file__).resolve().parents[1]
INSTANCE = ENGINE / "instance.json"

# `block_auto` is true only when enable needs no operator-supplied host config.
# `link` describes the supported boundary for operator-linked infrastructure.
FEATURES: dict[str, dict] = {
    "pg": {
        "title": "Postgres sidecar (app-only)",
        "block": "pg",
        "block_auto": True,
        "next": [
            (
                "./sc launch   # starts the sidecar + forwards DATABASE_URL "
                "(or: ./sc pg-up)"
            )
        ],
    },
    "windows": {
        "title": "Windows Test VM (link-only)",
        "block": "vm",
        "block_auto": False,
        "link": [
            (
                "link your VM: GUI → Scripts → 'Windows Test VM' wizard "
                "(live-checks each field), or hand-fill the `vm` block in "
                ".super-coder/instance.json — see README → 'Windows Test VM'"
            ),
            "./sc launch   # brings the vm-broker up once a VM is linked",
        ],
    },
    "tailnet": {
        "title": "Tailnet broker",
        "block": "ts",
        "block_auto": False,
        "link": [
            (
                "hand-fill the `ts` block in .super-coder/instance.json "
                "(allowed_hosts is the fail-closed scope) — see README → "
                "'Tailnet broker'"
            ),
            "./sc launch   # brings the ts-broker up once a tailnet is linked",
        ],
    },
    "pm2": {
        "title": "pm2 broker (host process stack)",
        "block": "pm2",
        "block_auto": False,
        "link": [
            (
                "hand-fill the `pm2` block in .super-coder/instance.json "
                "(processes is the fail-closed scope; health_url optional; "
                "stop/start stay gated behind allow_lifecycle) — see README "
                "→ 'pm2 broker'"
            ),
            "./sc launch   # brings the pm2-broker up once a stack is linked",
        ],
    },
}


def _instance() -> dict:
    if not INSTANCE.exists():
        return {}
    try:
        return json.loads(INSTANCE.read_text())
    except json.JSONDecodeError:
        sys.exit(f"feature: {INSTANCE} is not valid JSON — fix it first.")


def _update_instance(
    changes: dict[str, object], *, remove: tuple[str, ...] = ()
) -> None:
    instance_state.merge_instance_config(INSTANCE, changes, remove=remove)


def cmd_list() -> int:
    cfg = _instance()
    print("opt-in features — enable with: ./sc feature enable <name>\n")
    for name, f in FEATURES.items():
        blk = f["block"]
        linked = blk in cfg
        blk_state = f"✓ `{blk}` linked" if linked else (
            f"✗ `{blk}` block absent" + ("" if f["block_auto"] else " (operator-linked)"))
        print(f"  {name:10} {f['title']}")
        print(f"             config: {blk_state}")
        print("             guidance: fork-local via Planner `fork_skill_design`")
        print()
    return 0


def _resolve(name: str) -> dict:
    f = FEATURES.get(name)
    if not f:
        sys.exit(f"feature: unknown feature '{name}' "
                 f"(have: {', '.join(FEATURES)})")
    return f


def cmd_enable(name: str) -> int:
    f = _resolve(name)
    print(f"→ enable {name} — {f['title']}")

    cfg = _instance()
    blk = f["block"]
    if blk in cfg:
        print(f"  config `{blk}` already linked in instance.json")
    elif f["block_auto"]:
        _update_instance({blk: {}})
        print(f"  config `{blk}` added to instance.json")
    else:
        print(f"  config `{blk}` is operator-linked — next steps:")
        for step in f.get("link", []):
            print(f"    - {step}")

    for step in f.get("next", []):
        print(f"  next: {step}")
    print("  guidance: describe fork-specific operation with Planner `fork_skill_design`")
    return 0


def cmd_disable(name: str) -> int:
    f = _resolve(name)
    print(f"→ disable {name} — {f['title']}")

    cfg = _instance()
    blk = f["block"]
    if blk in cfg:
        _update_instance({}, remove=(blk,))
        print(f"  config `{blk}` removed from instance.json")
        if name == "pg":
            print("  note: a running sidecar keeps running — stop it with ./sc pg-down "
                  "(the data volume is retained)")
    else:
        print(f"  config `{blk}` was not linked")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        return cmd_list()
    if cmd in ("enable", "disable"):
        if len(argv) < 2:
            sys.exit(f"feature: {cmd} needs a feature name "
                     f"({', '.join(FEATURES)})")
        return cmd_enable(argv[1]) if cmd == "enable" else cmd_disable(argv[1])
    sys.exit(f"feature: unknown subcommand '{cmd}' (list · enable · disable)")


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
