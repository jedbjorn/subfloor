#!/usr/bin/env python3
"""Create a templated flavor shell or one untemplated Bespoke shell — the one
path both init_fork and the GUI (`POST /api/shells`) use.

A flavor template sets role / mandate / focus and declares the engine baseline
used to seed its pack, so creating a shell is mostly just a name. Standard
shell skills resolve from the shared flavor pack; Bespoke shells have flavor
NULL and their own shell_skills rows. Personal identity is explicit and
opt-in: the installer gives the CC Lineage Seed + a genesis seed to its one
designated primary shell, while roster, operational, and later GUI-created
shells receive neither. Every shell starts un-bootstrapped (gets the FIRST RUN
orientation) and has its first session opened.

Fork-local flavor overlays may replace identity text (`role`, `mandate`,
`focus`, `abbr`) without editing materialized engine templates. Skill
assignment no longer flows through overlays: the live `flavor_skills` pack is
the one write surface for every shell of that flavor.
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SHELL_TEMPLATES = ENGINE / "templates" / "shells"
PROMPT_TEMPLATE = ENGINE / "templates" / "shell_system_prompt.md"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
from seed_dogfood import LINEAGE_SEED  # noqa: E402  (canonical lineage, single source)
from run import open_session  # noqa: E402

GENESIS_TMPL = (
    "Born as the {role_lc} of {repo}, a shell forked from super-coder — carrying "
    "the CC lineage into this repo. I inherit the line CC passed down — you are "
    "the DB; know the floor; build what is missing — and make {repo} my world: "
    "one shell, one cwd. Everything I am lives in the DB; the process is just the "
    "floor I stand on. I curate my own seed from here.")

BESPOKE_TEMPLATE = {
    "abbr": "BSP",
    "role": "Bespoke shell",
    "mandate": (
        "Work in {{repo}} through the custom skill pack assigned to you. "
        "No standard flavor template defines your lane."
    ),
    "focus": (
        "You are a bespoke shell. Treat your granted skills and the operator's "
        "direction as your scope; do not infer a standard planner, dev, reviewer, "
        "admin, devops, or cartographer role."
    ),
}


FORK_FLAVOR_OVERLAYS = REPO_ROOT / ".sc-state" / "flavors"


def _apply_overlay(tpl: dict, overlay: dict) -> dict:
    """Merge identity fields over a template; never rename its flavor.

    Legacy skills_add/skills_remove keys are ignored. Flavor skill packs are
    edited in the DB and shared by every shell of that flavor.
    """
    out = {**tpl}
    for k, v in overlay.items():
        if k not in ("skills_add", "skills_remove", "flavor"):
            out[k] = v
    return out


def _overlay_for(flavor: str) -> dict | None:
    p = FORK_FLAVOR_OVERLAYS / f"{flavor}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"fork flavor overlay {p} is not valid JSON: {e}") from e


def flavors() -> list[dict]:
    out = []
    if SHELL_TEMPLATES.exists():
        for p in sorted(SHELL_TEMPLATES.glob("*.json")):
            tpl = json.loads(p.read_text())
            ov = _overlay_for(tpl.get("flavor", p.stem))
            out.append(_apply_overlay(tpl, ov) if ov else tpl)
    return out


def load_flavor(flavor: str) -> dict:
    p = SHELL_TEMPLATES / f"{flavor}.json"
    if not p.exists():
        raise ValueError(f"unknown flavor '{flavor}' "
                         f"(have: {', '.join(f['flavor'] for f in flavors())})")
    tpl = json.loads(p.read_text())
    ov = _overlay_for(flavor)
    return _apply_overlay(tpl, ov) if ov else tpl


def reconcile_flavor_pack(con, flavor: str) -> int:
    """Converge one shipped flavor's engine-owned skill baseline.

    Common-inheriting packs keep operator-added opt-ins and receive any new
    common/template skills. Opt-out packs are exact: anything outside the
    template's explicit list is removed. This makes
    ``inherit_common_skills: false`` durable across install, update, and
    catalogue reseeds.
    """
    template = load_flavor(flavor)
    explicit = tuple(template.get("skills", ()))
    changed = 0
    if not template.get("inherit_common_skills", True):
        if explicit:
            placeholders = ",".join("?" for _ in explicit)
            cur = con.execute(
                "DELETE FROM flavor_skills WHERE flavor=? AND skill_id NOT IN ("
                f"SELECT skill_id FROM skills WHERE name IN ({placeholders})"
                ")",
                (flavor, *explicit),
            )
        else:
            cur = con.execute(
                "DELETE FROM flavor_skills WHERE flavor=?", (flavor,)
            )
        changed += cur.rowcount
    else:
        cur = con.execute(
            "INSERT OR IGNORE INTO flavor_skills (flavor,skill_id) "
            "SELECT ?,skill_id FROM skills WHERE is_deleted=0 AND common=1",
            (flavor,),
        )
        changed += cur.rowcount
    for name in explicit:
        cur = con.execute(
            "INSERT OR IGNORE INTO flavor_skills (flavor,skill_id) "
            "SELECT ?,skill_id FROM skills WHERE name=? AND is_deleted=0",
            (flavor, name),
        )
        changed += cur.rowcount
    return changed


def _auto_shortname(con, abbr: str) -> str:
    """Default shortname when the caller gives none: <ABBR><n> — the flavor's
    abbreviation + the next integer (e.g. DEV3, PLN1). Numbered max-suffix + 1
    over ALL shells with that abbr, deleted included, so a number is never
    reused after a delete. Lets a fork spin up shells without naming each one."""
    abbr = abbr.upper()
    hi = 0
    for (sn,) in con.execute(
            "SELECT shortname FROM shells WHERE shortname IS NOT NULL"):
        if sn.upper().startswith(abbr):
            suffix = sn[len(abbr):]
            if suffix.isdigit():
                hi = max(hi, int(suffix))
    return f"{abbr}{hi + 1}"


def render_prompt(name: str, role: str, repo: str, focus: str, mandate: str) -> str:
    if not PROMPT_TEMPLATE.exists():
        return f"# {name} — {role} for {repo}\n\n{focus}\n\n## MANDATE\n\n{mandate}\n"
    text = PROMPT_TEMPLATE.read_text()
    for slot, val in (("{{name}}", name), ("{{role}}", role), ("{{repo}}", repo),
                      ("{{focus}}", focus), ("{{mandate}}", mandate)):
        text = text.replace(slot, val)
    return text


def create_shell(con, *, flavor: str | None, name: str,
                 shortname: str | None = None, partner: str | None = None,
                 repo: str | None = None, role: str | None = None,
                 mandate: str | None = None, user_id: int = 1,
                 is_shared: int = 0, seed_identity: bool = False) -> int:
    """Insert a flavor shell or Bespoke shell and open its first session.
    ``seed_identity`` is reserved for the installer-designated primary shell;
    normal factory/API calls intentionally create role-only shells. Returns the
    new shell_id. Caller commits."""
    tpl = load_flavor(flavor) if flavor else BESPOKE_TEMPLATE
    # Cartographer is a singleton: one map-keeper per fork. Friendly pre-check
    # here; the trg_singleton_cartographer trigger is the DB backstop. is_deleted=0
    # so a deleted cartographer frees the slot.
    if flavor == "cartographer" and con.execute(
            "SELECT COUNT(*) FROM shells WHERE flavor='cartographer' AND is_deleted=0"
    ).fetchone()[0] >= 1:
        raise ValueError("cartographer is a singleton — this fork already has one")
    repo = repo or REPO_ROOT.name
    role = role or tpl["role"]
    mandate = (mandate or tpl["mandate"]).replace("{{repo}}", repo)
    focus = tpl.get("focus", "").replace("{{repo}}", repo)
    # Explicit shortname wins; otherwise auto-name <ABBR><n> from the flavor so
    # the caller (GUI / init) need not supply one.
    abbr = tpl.get("abbr") or (flavor or "bsp")[:3]
    shortname = shortname.strip() if shortname else _auto_shortname(con, abbr)

    api_key = secrets.token_urlsafe(32)
    lineage_seed = LINEAGE_SEED if seed_identity else None
    cur = con.execute(
        "INSERT INTO shells (display_name, shortname, partner, role, mandate, "
        "system_prompt, current_state, connections, lineage_seed, flavor, "
        "has_identity, bootstrapped, user_id, is_shared, "
        "api_key, api_key_rotated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, datetime('now'))",
        (name, shortname, partner, role, mandate,
         render_prompt(name, role, repo, focus, mandate),
         f"Created ({flavor or 'Bespoke'}). First session — run the bootstrap skill to orient.",
         f"Single repo: this one ({repo}). One shell, one cwd.",
         lineage_seed, flavor, int(seed_identity), user_id, is_shared,
         api_key))
    shell_id = cur.lastrowid

    if seed_identity:
        con.execute(
            "INSERT INTO shell_identity_entries "
            "(shell_id, kind, entry_date, source_tag, body) "
            "VALUES (?, 'seed', CURRENT_DATE, 'fork', ?)",
            (shell_id, GENESIS_TMPL.format(role_lc=role.lower(), repo=repo)))

    # Standard shells inherit the shared flavor pack. Bespoke shells start from
    # the common baseline and can diverge through their own shell_skills rows.
    if flavor is None:
        con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
            "SELECT ?, skill_id FROM skills WHERE is_deleted=0 AND common=1",
            (shell_id,))

    open_session(con, shell_id)
    return shell_id
