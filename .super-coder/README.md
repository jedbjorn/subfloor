# .super-coder/ — engine reference

**Posture: current architecture reference.** This page describes the engine for
source maintainers and fork Administrators. Start with the
[user guide](../docs/README.md) for installation and everyday work.

## Source, dependency, and private state

In this source repository, `.super-coder/` is tracked project source. Changes
land through branches and reviewed PRs. In an installed fork it is a gitignored
materialized dependency, pinned by `.sc-state/engine.ref`; update it through the
host Admin lifecycle instead of editing engine files in place.

System source propagates to forks. Instance identity, conversations, memory and
local skill bodies do not. A fresh clone creates a new instance.

| Surface | Owner and location | Purpose |
|---|---|---|
| Engine schema and migrations | `schema.sql`, `migrations/` | Public system structure and ordered upgrades |
| Live DB | Private XDG instance root, `shell_db.db` | Authoritative instance state |
| Canonical snapshot | Same private root, `content.sql` | Serialized instance content used by recovery/rebuild |
| Backups | Same private root, `db_backups/` | Lifecycle recovery evidence |
| Instance configuration | Gitignored `instance.json` | Opaque instance identity, runtime and ports |
| Fork provenance | `.sc-state/engine.ref`, `.sc-state/engine.source` | Tracked dependency pin and source |
| Local visibility | Ignored `.sc-state/local/` | Map configuration, skill retirement and generated renders |
| Boot and skill mirrors | Ignored harness-native files | Derived context for the selected shell |

[`scripts/instance_state.py`](scripts/instance_state.py) owns path resolution.
The private namespace is beneath `${XDG_STATE_HOME:-$HOME/.local/state}` in
`subfloor/instances/<instance_id>/`. Production consumers use its active-path
selectors, including relocation receipt checks. Fresh bound installs select
private state before building the first DB. Legacy copies and incomplete
relocation require the named Admin recovery procedure; do not infer the active
DB from whichever file happens to exist.

Ordinary downstream shells receive API-backed control-plane commands and a
restricted execution view. General engine SQL and direct private-state
inspection are Admin-only. Source maintainers can read tracked engine code,
schema and migrations because those are the project; that does not grant a
Dev shell direct live-state maintenance authority. See
[`scripts/execution_view.py`](scripts/execution_view.py).

The fork application's own data remains distinct from engine memory. The
`dr_*` repository catalogue describes project files; it is neither engine
memory nor the application's database.

## Lifecycle owners

| Source | Responsibility |
|---|---|
| `scripts/install.py`, `scripts/init_fork.py` | Install the selected runtime and seed the ten-shell roster |
| `scripts/update.py`, `scripts/state_relocation.py` | Materialize the engine, reconcile migrations and handle guarded relocation |
| `scripts/rollback.py` | Restore a compatible engine and DB pair |
| `scripts/remove.py`, `scripts/eject.py` | Guarded removal or deliberate ownership of engine source |
| `scripts/rebuild.py`, `scripts/migrate.py` | Rebuild from system source and active snapshot, or apply pending migrations |
| `scripts/snapshot.py` | Serialize instance content to the canonical private snapshot |
| `scripts/run.py`, `scripts/shell_liveness.py` | Select a shell and route, render context, enforce ownership, launch the harness |

Use `subfloor admin` from the host checkout for maintenance. Exact command
syntax belongs to `sc help --all` and each command's `--help`. An API failure is
not permission for an ordinary shell to open the database directly.

## Render and skills

The render pipeline is one-way: authoritative state becomes derived files.
[`render/compose.py`](render/compose.py) builds boot context;
[`render/flat.py`](render/flat.py) creates local visibility and skill mirrors.
Unchanged outputs are skipped. Edit the owning API surface or tracked source,
not generated boot files, `_sc` documents or skill mirrors.

Boot context is written as `CLAUDE.md` and `AGENTS.md`; adapter-specific discovery
selects the appropriate file and skill directory. All five adapters consume
this common substrate. Flat documents and roadmap renders remain ignored
local visibility, not a Git publication mechanism.

Engine skill bodies are authored under `assets/skills/`; `scripts/seed_skills.py`
maintains their distribution catalogue. Planner owns DB-canonical fork-local
skill authoring and grants through `sc skill`. Local bodies and grants survive
update/rebuild. Admin owns engine catalogue maintenance and guarded recovery.
Every-session procedures live in boot, conditional procedures in skills, and
exact syntax in tool help.

## Harness adapters

Each [`adapters/`](adapters/) directory carries its own `adapter.json` and
README. The manifest declares terminal, one-shot, browser and Sprint surfaces,
launch flags, model/effort routing, conversation contract and capability bounds.
Support is surface-specific: Vibe ships a terminal/one-shot adapter but does not
advertise browser or Sprint support. Consult each current manifest rather than
assuming every harness implements every surface.

- [Claude](adapters/claude/README.md)
- [Codex](adapters/codex/README.md)
- [OpenCode](adapters/opencode/README.md)
- [Vibe](adapters/vibe/README.md)
- [Kimi](adapters/kimi/README.md)

The common launcher and `scripts/conversation_adapters/` implement those
contracts. Branch protection combines harness-specific edit guards where
available with the universal Git pre-commit backstop. Browser and CLI sessions
cannot own the same shell concurrently.

## API, browser UI, and runbooks

`api/server.py` serves the JSON API, static vanilla-JavaScript `ui/` and event
stream on the instance's loopback port. The ten tabs include Chats and Sprints;
Shells is the default landing tab. The sandbox lifecycle runs the server in its
container; the host runtime supervises the same server as a host process.

Ordinary browser controls expose operational fields, roadmap, flags and
unfrozen documents. Seed and L&S remain shell-owned; frozen documents reject
edits. Saving locally writes the private snapshot and local renders, never a
public Git snapshot. Browser authentication and API authority are described in
[the interface trust boundary](docs/interface-trust-boundary.md).

Focused runbooks under [`docs/`](docs/) cover harness freshness and the optional
Windows VM, tailnet, PM2 and app-DB brokers. Their applicability labels separate
current operation from architecture and historical migration procedures. The
[DeepSeek removal procedure](../docs/deepseek-harness-removal.md) retains its
certified exact-ref checkpoints for Admin recovery.
