---
title: Remove legacy DeepSeek Harness
tags: [deepseek, dsh, removal, upgrade]
date: 2026-08-28
project: subfloor
purpose: Exact-ref removal for existing forks
---

# Remove legacy DeepSeek Harness

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/deepseek-harness-removal.md)

## Before you start

This is the only supported removal path for an existing installation that has
ever run the DeepSeek Harness (DSH). Use these three immutable commits, in this
order:

| Floor | Exact ref | Purpose |
|---|---|---|
| Compatibility O | `75a4c9bf781812b4bcf33aedd7943887a87cfde2` | Installs the pre-materialization guard, readiness marker, WAL-safe backup, quiescence, ownership capture, and pair recovery |
| Declared purge R | `45b82930fd42a28ab41e14d7235f24a9c3772743` | Performs exact cleanup, transactional live-data purge, and schema rebaseline |
| Sentinel-free final F | `c73c1813ba2b2ace4e5f327d4340322ed6169ab4` | Removes the transitional machinery and leaves the current DSH-free engine |

> [!class4]
> Do not target moving `main`, skip R, substitute a newer commit, or use
> `--force`. Stop immediately on any non-zero command, missing checkpoint, or
> ref mismatch. Do not start the next hop until the current checkpoint passes.

Run from the installed fork's repository root as the operator who normally
updates it. Start with a clean tracked tree and a local update branch:

```bash
set -euo pipefail
SC_DSH_O=75a4c9bf781812b4bcf33aedd7943887a87cfde2
SC_DSH_R=45b82930fd42a28ab41e14d7235f24a9c3772743
SC_DSH_F=c73c1813ba2b2ace4e5f327d4340322ed6169ab4
SC_REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$SC_REPO_ROOT"
test -x ./sc
test -s .sc-state/engine.ref
test -z "$(git status --porcelain)"
git remote get-url super-coder >/dev/null
SC_DSH_START_REF="$(tr -d '\n' < .sc-state/engine.ref)"
printf 'starting engine ref: %s\n' "$SC_DSH_START_REF"
git switch -c chore/remove-legacy-dsh
```

If the installation is already at R, resume at the R checkpoint. If it is
already at F, run only the final checkpoint. A missing or malformed
`.sc-state/engine.ref` is not a reason to guess; stop and repair the ordinary
engine pin first.

## Install compatibility O

Select O explicitly. The updater fetches that exact reachable commit and does
not resolve the default branch:

```bash
SC_DSH_O=75a4c9bf781812b4bcf33aedd7943887a87cfde2
./sc update --ref "$SC_DSH_O"
test "$(tr -d '\n' < .sc-state/engine.ref)" = "$SC_DSH_O"
python3 - "$SC_DSH_O" <<'PY'
import json
import sys
from pathlib import Path

expected_ref = sys.argv[1]
path = Path(".sc-state/local/dsh-removal/compatibility-floor.json")
marker = json.loads(path.read_text())
assert marker == {
    "contract": "sc-dsh-compatibility-floor-v1",
    "engine_ref": expected_ref,
    "fresh_process_cleanup_hook": True,
    "pre_materialization_hook": True,
}, marker
print(f"compatibility marker ready: {path}")
PY
```

The ref and exact marker are both required. If update reports success but
either check fails, stop. Do not attempt R and do not edit the marker.

## Purge at R

Select R explicitly from the proven O floor:

```bash
SC_DSH_O=75a4c9bf781812b4bcf33aedd7943887a87cfde2
SC_DSH_R=45b82930fd42a28ab41e14d7235f24a9c3772743
./sc update --ref "$SC_DSH_R"
test "$(tr -d '\n' < .sc-state/engine.ref)" = "$SC_DSH_R"
```

After update completes—or when resuming an installation already pinned to
R—run the receipt checkpoint:

```bash
SC_DSH_O=75a4c9bf781812b4bcf33aedd7943887a87cfde2
SC_DSH_R=45b82930fd42a28ab41e14d7235f24a9c3772743
test "$(tr -d '\n' < .sc-state/engine.ref)" = "$SC_DSH_R"
python3 - "$SC_DSH_O" "$SC_DSH_R" <<'PY'
import json
import sys
from pathlib import Path

compatibility_ref, target_ref = sys.argv[1:]
root = Path(".sc-state/local/dsh-removal")
cutover = json.loads((root / "cutover-receipt.json").read_text())
cleanup = json.loads((root / "cleanup-receipt.json").read_text())
assert cutover["contract"] == "sc-dsh-cutover-receipt-v1", cutover
assert cutover["compatibility_ref"] == compatibility_ref, cutover
assert cutover["target_ref"] == target_ref, cutover
assert cutover.get("recovery") is None, cutover
assert isinstance(cutover.get("generated_ownership"), list), cutover
assert set(cutover.get("process_identities", {})) == {
    "relay_pid", "relay_port", "relay_start_ticks", "service_port",
    "web_pid", "web_start_ticks",
}, cutover
assert cutover.get("dsh_outcome", {}).get("relay") is True, cutover
assert cutover.get("dsh_outcome", {}).get("web") is True, cutover
assert isinstance(cutover.get("dsh_outcome", {}).get("stopped"), bool), cutover
assert Path(cutover["backup_path"]).is_file(), cutover
assert cleanup["contract"] == "sc-dsh-cleanup-receipt-v1", cleanup
assert cleanup["compatibility_ref"] == compatibility_ref, cleanup
assert cleanup["target_ref"] == target_ref, cleanup
assert cleanup["manifest_sha256"] == cutover["manifest_sha256"], cleanup
assert cleanup["status"] == "complete", cleanup
assert cleanup["errors"] == [], cleanup
print(f"purge receipts complete; matched backup: {cutover['backup_path']}")
PY
```

Verify the current live database through the read-only engine DB surface. A
passing first query prints nothing; the integrity checks print `ok` and then
nothing:

```bash
test -z "$(./sc sql "
SELECT 'flavor_defaults' WHERE EXISTS (
  SELECT 1 FROM flavor_defaults WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'model_routes' WHERE EXISTS (
  SELECT 1 FROM model_routes WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'analytics_parse_cache' WHERE EXISTS (
  SELECT 1 FROM analytics_parse_cache WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'session_token_usage' WHERE EXISTS (
  SELECT 1 FROM session_token_usage WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'shell_memory_archives' WHERE EXISTS (
  SELECT 1 FROM shell_memory_archives WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'shell_launch_records' WHERE EXISTS (
  SELECT 1 FROM shell_launch_records WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'conversations' WHERE EXISTS (
  SELECT 1 FROM conversations WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'sprint_participants' WHERE EXISTS (
  SELECT 1 FROM sprint_participants WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'sprint_route_bindings' WHERE EXISTS (
  SELECT 1 FROM sprint_participant_route_bindings
  WHERE lower(trim(harness))='deepseek'
);" )"
test "$(./sc sql 'PRAGMA integrity_check;')" = "ok"
test -z "$(./sc sql 'PRAGMA foreign_key_check;')"
```

These checks certify the live database only. They deliberately do not scan or
delete managed backup roots.

## Install final F

Only after every R checkpoint passes, select F explicitly:

```bash
SC_DSH_F=c73c1813ba2b2ace4e5f327d4340322ed6169ab4
./sc update --ref "$SC_DSH_F"
```

After update completes—or when resuming an installation already pinned to
F—run the final checkpoint:

```bash
SC_DSH_F=c73c1813ba2b2ace4e5f327d4340322ed6169ab4
test "$(tr -d '\n' < .sc-state/engine.ref)" = "$SC_DSH_F"
test ! -e .super-coder/assets/dsh-removal
test ! -e .super-coder/scripts/dsh_removal_cleanup.py
test ! -e .super-coder/scripts/update_cutover.py
test ! -e .super-coder/migrations/0237_purge_dsh_owned_data.sql
if rg -n -i 'deepseek_host_port|deepseek harness|\bdsh\b|dsh[-_]' \
  .super-coder \
  --glob '!instance.json' --glob '!shell_db.db*'; then
  echo 'active DSH code or generated engine content remains' >&2
  exit 1
fi
test -z "$(./sc sql "
SELECT 'flavor_defaults' WHERE EXISTS (
  SELECT 1 FROM flavor_defaults WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'model_routes' WHERE EXISTS (
  SELECT 1 FROM model_routes WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'conversations' WHERE EXISTS (
  SELECT 1 FROM conversations WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'sprint_participants' WHERE EXISTS (
  SELECT 1 FROM sprint_participants WHERE lower(trim(harness))='deepseek'
)
UNION ALL SELECT 'sprint_route_bindings' WHERE EXISTS (
  SELECT 1 FROM sprint_participant_route_bindings
  WHERE lower(trim(harness))='deepseek'
);" )"
test "$(./sc sql 'PRAGMA integrity_check;')" = "ok"
test -z "$(./sc sql 'PRAGMA foreign_key_check;')"
```

The code scan excludes only installation-local state. A legacy
`deepseek_host_port` member may still exist in `instance.json`; final code does
not expose, allocate, validate, migrate, display, or act on it. Do not rewrite
the file merely to remove that inert member.

Review the tracked update result and commit the final pin. Include only the
files the updater reports as managed changes:

```bash
git status --short
git add .sc-state/engine.ref sc
git diff --cached --check
git commit -m 'chore(engine): complete legacy DSH removal'
```

Restart through the installation's ordinary supervised lifecycle, then boot a
new shell. The final engine has no DSH selector, launch path, route, API/UI
surface, managed process, migration, or live DSH-owned record. DeepSeek-family
models configured through OpenCode remain OpenCode routes and retain their
exact provider, model, and option identifiers.

## Failure and re-entry

Never continue after a failed hop.

- **O fails or its marker is absent:** the starting pin remains authoritative.
  Correct the reported ordinary update problem and rerun the exact O command.
- **O to R fails before publication:** the cutover restores the compatibility
  engine and WAL-safe database as a matched pair. Confirm `engine.ref` is O.
  If target bytes were overlaid while the old DB stayed unchanged, run
  `./sc rollback --engine-only`, confirm O and its marker again, then rerun R.
- **R is published but a checkpoint fails:** do not target F. Preserve the
  receipts and backup, report the exact failed assertion, and recover the
  matched pair with `./sc rollback`. Re-enter at the ref that rollback reports;
  if it is pre-purge, repeat O then R.
- **R to F fails:** run `./sc rollback` to restore the previous engine/DB pair,
  confirm the resulting pin and database checks, then retry the exact F command.
- **A historical pre-purge backup is restored later:** it is no longer a
  certified final live database. Start this runbook again at O and complete R
  before targeting F.

Do not hand-edit a receipt, restore only one half of an engine/DB pair, delete a
backup to make a check pass, or substitute moving `main` for any exact ref.

## Ownership boundary

Subfloor removes only state it can prove it owns. It never uninstalls an
external DSH package and never deletes a user-owned DSH profile, session,
credential, package cache, or unmarked external cache. Those may remain on the
host, unused by Subfloor.

Historical DSH-bearing databases in Subfloor-managed backup roots are
non-current recovery inputs. They cannot make DSH selectable or invokable
because normal model, API, UI, launch, and process paths read only the current
live database; only an explicit rollback/restore selects a backup. Keep ordinary
retention policy. Do not purge those backups solely for DSH removal, and repeat
this runbook before certifying any restored pre-purge database as final.
