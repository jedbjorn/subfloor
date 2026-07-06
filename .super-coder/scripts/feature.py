#!/usr/bin/env python3
"""./sc feature — the front door to the engine's opt-in features.

Opt-ins have always existed as two mechanisms that compose:

    1. an `instance.json` block (pg / vm / ts / pm2) — enables the INFRASTRUCTURE
       (sidecar container, host-side broker) for this fork;
    2. `common=0` skill grants — puts the PROCEDURE in the right shells' hands
       (the skills ship to every fork's catalogue but auto-grant to none).

This command makes the pair first-class: `list` shows every feature and the
state of both halves; `enable` grants the feature's skills to the flavors that
own them (and creates the config block where that is scriptable); `disable`
reverses it. The mechanisms underneath are unchanged — feature.py only
orchestrates them, so everything here can still be done by hand.

The vm/ts blocks are NOT auto-created: they carry host-specific, operator-
verified config (a linked VM, a tailnet scope) that `enable` cannot invent —
it grants the skills and prints exactly how to link. `pg` needs no host input
(the sidecar is fully derived), so its block IS auto-created. A feature may
also be procedure-only (`block: None`) — no infrastructure half at all, just
grants; `app-deploy` is one.

Grants land in shell_skills, which is fork memory — enable/disable therefore
re-snapshots, so `.sc-state/content.sql` stays current and a rebuild keeps the
grants.

Usage:
    ./sc feature                      # = list
    ./sc feature list
    ./sc feature enable  <name>       # pg · windows · tailnet · app-deploy
    ./sc feature disable <name>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
INSTANCE = ENGINE / "instance.json"
PY = sys.executable

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402

# The registry. `block` is the instance.json key (None = procedure-only, no
# infrastructure half); `block_auto` says whether enable may create it (only
# when it needs no operator-supplied host config). `grants` maps skill name →
# the flavors whose shells own that procedure. `link` is the printed how-to
# for the operator-supplied blocks / procedure-only next steps.
FEATURES: dict[str, dict] = {
    "pg": {
        "title": "Postgres sidecar (app-only)",
        "block": "pg",
        "block_auto": True,
        "grants": {"test_authoring_pg": ["dev", "reviewer"],
                   "query_authoring_pg": ["dev", "reviewer", "planner"]},
        "next": ["./sc launch   # starts the sidecar + forwards DATABASE_URL "
                 "(or: ./sc pg-up)"],
    },
    "windows": {
        "title": "Windows Test VM (link-only)",
        "block": "vm",
        "block_auto": False,
        "grants": {"windows_devkit": ["dev", "reviewer"],
                   "windows_vm_gui": ["dev", "reviewer"],
                   "configure_winbox": ["admin"]},
        "link": ["link your VM: GUI → Scripts → 'Windows Test VM' wizard "
                 "(live-checks each field), or hand-fill the `vm` block in "
                 ".super-coder/instance.json — see README → 'Windows Test VM'",
                 "./sc launch   # brings the vm-broker up once a VM is linked"],
    },
    "tailnet": {
        "title": "Tailnet broker",
        "block": "ts",
        "block_auto": False,
        "grants": {"tailscale": ["devops"]},
        "link": ["hand-fill the `ts` block in .super-coder/instance.json "
                 "(allowed_hosts is the fail-closed scope) — see README → "
                 "'Tailnet broker'",
                 "./sc launch   # brings the ts-broker up once a tailnet is linked"],
    },
    "pm2": {
        "title": "pm2 broker (host process stack)",
        "block": "pm2",
        "block_auto": False,
        "grants": {"pm2": ["admin", "devops"]},
        "link": ["hand-fill the `pm2` block in .super-coder/instance.json "
                 "(processes is the fail-closed scope; health_url optional; "
                 "stop/start stay gated behind allow_lifecycle) — see README "
                 "→ 'pm2 broker'",
                 "./sc launch   # brings the pm2-broker up once a stack is linked"],
    },
    "app-deploy": {
        "title": "App deploy ritual (admin-authored)",
        "block": None,          # procedure-only — nothing to link in instance.json
        "block_auto": False,
        "grants": {"app_deploy_setup": ["admin"]},
        "link": ["run the app_deploy_setup skill in your admin shell — it "
                 "authors this repo's own project-local `deploy` skill "
                 "(migration dirs, backup, ff-only sync, migrate, restart) "
                 "and grants it to every shell"],
    },
}


def _instance() -> dict:
    if not INSTANCE.exists():
        return {}
    try:
        return json.loads(INSTANCE.read_text())
    except json.JSONDecodeError:
        sys.exit(f"feature: {INSTANCE} is not valid JSON — fix it first.")


def _write_instance(cfg: dict) -> None:
    INSTANCE.write_text(json.dumps(cfg, indent=2) + "\n")


def grant(con, skill: str, flavors: list[str]) -> int:
    """Grant `skill` to every live shell of the given flavors. Idempotent."""
    q = ",".join("?" for _ in flavors)
    cur = con.execute(
        f"INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
        f"SELECT s.shell_id, k.skill_id FROM shells s, skills k "
        f"WHERE COALESCE(s.is_deleted,0)=0 AND s.flavor IN ({q}) "
        f"AND k.name=? AND k.is_deleted=0",
        (*flavors, skill))
    return cur.rowcount


def revoke(con, skill: str, flavors: list[str]) -> int:
    """Revoke `skill` from shells of the given flavors — only the grants enable
    would have made; a grant to a bespoke/other-flavor shell is left alone."""
    q = ",".join("?" for _ in flavors)
    cur = con.execute(
        f"DELETE FROM shell_skills WHERE skill_id IN "
        f"(SELECT skill_id FROM skills WHERE name=? AND is_deleted=0) "
        f"AND shell_id IN (SELECT shell_id FROM shells "
        f"WHERE COALESCE(is_deleted,0)=0 AND flavor IN ({q}))",
        (skill, *flavors))
    return cur.rowcount


def _grant_state(con, skill: str) -> list[str]:
    """['dev(2)', 'reviewer(1)'] — live shells holding the skill, by flavor."""
    rows = con.execute(
        "SELECT COALESCE(s.flavor,'bespoke'), COUNT(*) FROM shell_skills g "
        "JOIN shells s ON s.shell_id=g.shell_id "
        "JOIN skills k ON k.skill_id=g.skill_id "
        "WHERE k.name=? AND COALESCE(s.is_deleted,0)=0 AND k.is_deleted=0 "
        "GROUP BY 1 ORDER BY 1", (skill,)).fetchall()
    return [f"{flavor}({n})" for flavor, n in rows]


def _snapshot() -> None:
    """Grants are fork memory — persist them to the tracked serialization."""
    env = {**os.environ, "SC_ADMIN": "1"}
    r = subprocess.run([PY, str(ENGINE / "scripts" / "snapshot.py")], env=env)
    if r.returncode != 0:
        print("⚠ snapshot failed — grants are live in the DB but "
              ".sc-state/content.sql is stale; run ./sc snapshot", file=sys.stderr)


def cmd_list() -> int:
    cfg = _instance()
    con = db_driver.connect(DB_PATH) if DB_PATH.exists() else None
    print("opt-in features — enable with: ./sc feature enable <name>\n")
    for name, f in FEATURES.items():
        blk = f["block"]
        if blk is None:
            blk_state = "— none needed (procedure-only)"
        else:
            linked = blk in cfg
            blk_state = f"✓ `{blk}` linked" if linked else (
                f"✗ `{blk}` block absent" + ("" if f["block_auto"] else " (operator-linked)"))
        print(f"  {name:10} {f['title']}")
        print(f"             config: {blk_state}")
        for skill in f["grants"]:
            held = _grant_state(con, skill) if con else []
            state = " ".join(held) if held else "none"
            print(f"             skill:  {skill} → {state}")
        print()
    if con:
        con.close()
    return 0


def _resolve(name: str) -> dict:
    f = FEATURES.get(name)
    if not f:
        sys.exit(f"feature: unknown feature '{name}' "
                 f"(have: {', '.join(FEATURES)})")
    return f


def cmd_enable(name: str) -> int:
    f = _resolve(name)
    if not DB_PATH.exists():
        sys.exit("feature: no live DB — run ./sc rebuild (or ./sc install) first.")
    print(f"→ enable {name} — {f['title']}")

    con = db_driver.connect(DB_PATH)
    try:
        granted = 0
        for skill, flavors in f["grants"].items():
            n = grant(con, skill, flavors)
            granted += n
            state = " ".join(_grant_state(con, skill)) or "none"
            missing = [fl for fl in flavors if con.execute(
                "SELECT COUNT(*) FROM shells WHERE flavor=? AND COALESCE(is_deleted,0)=0",
                (fl,)).fetchone()[0] == 0]
            note = f"  (no live {'/'.join(missing)} shell yet — create one and re-run)" if missing else ""
            print(f"  skill {skill} → {state}{note}")
        con.commit()
    finally:
        con.close()

    cfg = _instance()
    blk = f["block"]
    if blk is None:
        print("  config: none needed (procedure-only) — next steps:")
        for step in f.get("link", []):
            print(f"    - {step}")
    elif blk in cfg:
        print(f"  config `{blk}` already linked in instance.json")
    elif f["block_auto"]:
        cfg[blk] = {}
        _write_instance(cfg)
        print(f"  config `{blk}` added to instance.json")
    else:
        print(f"  config `{blk}` is operator-linked — next steps:")
        for step in f.get("link", []):
            print(f"    - {step}")

    if granted:
        _snapshot()
    for step in f.get("next", []):
        print(f"  next: {step}")
    return 0


def cmd_disable(name: str) -> int:
    f = _resolve(name)
    print(f"→ disable {name} — {f['title']}")

    if DB_PATH.exists():
        con = db_driver.connect(DB_PATH)
        try:
            revoked = 0
            for skill, flavors in f["grants"].items():
                n = revoke(con, skill, flavors)
                revoked += n
                print(f"  skill {skill}: revoked {n} grant(s) "
                      f"(flavors: {', '.join(flavors)}; other shells untouched)")
            con.commit()
        finally:
            con.close()
        if revoked:
            _snapshot()

    cfg = _instance()
    blk = f["block"]
    if blk is None:
        print("  config: none to remove (procedure-only)")
    elif blk in cfg:
        del cfg[blk]
        _write_instance(cfg)
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
    raise SystemExit(main(sys.argv[1:]))
