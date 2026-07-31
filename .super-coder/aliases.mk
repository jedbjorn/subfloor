# super-coder make aliases — convenience wrappers around ./sc.
#
# The dispatcher ./sc is the canonical interface; these targets only delegate.
# This file travels with the engine, so the aliases work in a fork exactly as in
# the source repo — WITHOUT super-coder ever owning or clobbering the fork's
# Makefile (#13): install wires a fork to *include* this file (or auto-creates a
# one-line Makefile when the fork has none); it never overwrites a Makefile you
# already have. Delete the include and you lose nothing — `./sc <cmd>` is identical.
#
# House command standard (designs-OS family): one prefix — `dos-` — across every
# substrate, so switching repos never changes the muscle memory. Every target is
# `dos-`prefixed so including this file can't collide with your own `test` /
# `build` / `install` targets. The hot commands get a one-letter short form too;
# everything else is long-form only. The `./sc` binary keeps its name (the engine
# layer is separate from the make surface).
#
#   HOT (short + long):
#   make dos-e   / dos-enter     attach a session (pick shell + harness); dos-e s=ap01 boots one
#   make dos-l   / dos-launch    build + start the docker sandbox (+ review GUI)
#   make dos-r   / dos-restart   confirm (YES) + DB backup, then down + launch
#   make dos-d   / dos-down      stop the sandbox
#   make dos-u   / dos-update    sync + materialize the engine, reconcile in place
#                                (source repo: reconciles FROM the checkout, so it
#                                 fast-forwards it first — being behind is the
#                                 normal case and just pulls. Never blocks: a tree
#                                 it cannot fast-forward warns that the floor will
#                                 be stale and updates anyway.
#                                 ARGS=--no-fetch opts out.)
#   make dos-t   / dos-test      backend (pytest/unittest) + UI (vitest) suites
#   make dos-url                 print the review GUI + dev-server URLs
#   make dos-h                   list the commands
#   make dos-help                list + describe the commands
#
#   LONG-ONLY: models/job; build/logs/serve/health/ports;
#              verify/map/render/snapshot/deps/install/rollback/
#              update-harnesses/harness-status/feature/eject/remove
#              passthrough: make dos ARGS=health
#
SC := ./sc
.PHONY: dos-e dos-enter dos-l dos-launch dos-r dos-restart dos-d dos-down dos-u dos-update \
        dos-t dos-test dos-h dos-help dos-url dos-build dos-logs dos-serve dos-health dos-ports \
        dos-verify dos-map dos-render dos-snapshot dos-deps dos-install dos-rollback \
        dos-update-harnesses dos-harness-status dos-feat dos-feature dos-eject dos-remove \
        dos-models dos-model-refresh dos-model-list \
        dos-model-resolve dos-job dos-setup dos

require-model-route = $(if $(and $(strip $(h)),$(strip $(m))),,$(error $@: requires h=<harness> m=<model> [s=<shell-shortname>]))
model-shell-arg = $(if $(strip $(s)),--shell $(s))

# Hot commands — long form, with a one-letter alias delegating to it.
dos-enter:            ; $(SC) $(if $(strip $(s)),enter-$(s),enter) $(ARGS)
dos-e: dos-enter
dos-launch:           ; $(SC) launch $(ARGS)
dos-l: dos-launch
dos-restart:          ; $(SC) restart $(ARGS)
dos-r: dos-restart
dos-down:             ; $(SC) down
dos-d: dos-down
dos-update:           ; $(SC) update $(ARGS)
dos-u: dos-update
dos-test:             ; $(SC) test $(ARGS)
dos-t: dos-test
# The links an operator loses when the boot summary scrolls away — derived per
# fork, never a fixed 8800 (decision #50).
dos-url:              ; $(SC) url

# Model catalogue and durable local jobs.
dos-models:           ; $(SC) models $(ARGS)
dos-model-refresh:    ; $(SC) models refresh
dos-model-list:       ; $(SC) models list $(h)
dos-model-resolve:    ; $(call require-model-route)$(SC) models resolve $(h) $(m) $(model-shell-arg)
dos-job:              ; $(SC) job $(ARGS)

# Long-only commands.
dos-build:            ; $(SC) build
dos-logs:             ; $(SC) logs
dos-serve:            ; $(SC) serve $(ARGS)
dos-health:           ; $(SC) health
dos-ports:            ; $(SC) ports
dos-verify:           ; $(SC) verify
dos-map:              ; $(SC) map $(ARGS)
dos-render:           ; $(SC) render $(ARGS)
dos-snapshot:         ; $(SC) snapshot
dos-deps:             ; $(SC) deps $(ARGS)
dos-install:          ; $(SC) install
dos-setup: dos-install
dos-rollback:         ; $(SC) rollback
dos-update-harnesses: ; $(SC) update-harnesses
# What the shells' harness CLIs actually are, in the sandbox — and whether the
# image owes a harness rebuild. The picker shows model ALIASES, never the CLI
# build that decides which models an alias can reach, so this is the only place
# that answers it.
dos-harness-status:   ; $(SC) harness-status
# Opt-in features: make dos-feat (list) · make dos-feat ARGS="enable pg"
dos-feature:          ; $(SC) feature $(ARGS)
dos-feat: dos-feature
# One-way divergence — interactive warning + typed confirmation in ./sc eject.
dos-eject:            ; $(SC) eject
# One-way uninstall — ./sc remove owns confirmation, backup, and safety gates.
dos-remove:           ; $(SC) remove $(ARGS)

# Passthrough: run any ./sc subcommand — make dos ARGS=health
dos:                  ; $(SC) $(ARGS)

# Help — dos-h is a quick chart of the hot commands; dos-help charts everything.
dos-h:
	@echo ""
	@echo "  super-coder — make dos-<cmd>   (delegates to ./sc)"
	@echo "  ┌───────┬─────────────┬──────────────────────────────────────────┐"
	@echo "  │ short │ long        │ what it does                             │"
	@echo "  ├───────┼─────────────┼──────────────────────────────────────────┤"
	@echo "  │ dos-e │ dos-enter   │ attach a session (pick shell + harness)  │"
	@echo "  │ dos-l │ dos-launch  │ build + start the docker sandbox + GUI   │"
	@echo "  │ dos-r │ dos-restart │ confirm + DB backup, then recreate fresh │"
	@echo "  │ dos-d │ dos-down    │ stop the sandbox                         │"
	@echo "  │ dos-u │ dos-update  │ update the engine in place               │"
	@echo "  │ dos-t │ dos-test    │ run backend + UI test suites             │"
	@echo "  │       │ dos-url     │ print the review GUI + dev-server URLs   │"
	@echo "  └───────┴─────────────┴──────────────────────────────────────────┘"
	@echo "  more:  make dos-help  (full list + long-only)   ·   make dos ARGS=<cmd>"
	@echo ""
dos-help:
	@echo ""
	@echo "  super-coder — make dos-<cmd>   every target delegates to ./sc"
	@echo ""
	@echo "  HOT"
	@echo "    dos-e / dos-enter [s=x]     pick/enter a shell, or enter x directly —"
	@echo "                                new chat, reattach a live one, or take the"
	@echo "                                writer role back from a dead GUI client"
	@echo "    dos-l / dos-launch          build + start the sandbox and review GUI"
	@echo "    dos-r / dos-restart         confirm + backup + fully restart (ARGS forwarded)"
	@echo "    dos-d / dos-down            stop and remove the sandbox"
	@echo "    dos-u / dos-update          update and reconcile the engine (ARGS forwarded)"
	@echo "    dos-t / dos-test            run backend + UI suites (ARGS forwarded)"
	@echo "    dos-url                     print the review GUI + dev-server URLs for this fork"
	@echo ""
	@echo "  MODELS + JOBS"
	@echo "    dos-model-refresh           refresh the local model catalogue"
	@echo "    dos-model-list [h=x]        list all routes or one harness"
	@echo "    dos-model-resolve h=x m=y   resolve an exact route; optional s=<shell>"
	@echo "    dos-models ARGS='<cmd>'     generic model catalogue command"
	@echo "    dos-job ARGS='<cmd>'         durable local job start/wait/list/status/tail/kill"
	@echo ""
	@echo "  MAINTENANCE"
	@echo "    dos-build                   (re)build the sandbox container image"
	@echo "    dos-logs                    follow the sandbox container's server logs"
	@echo "    dos-serve                   run the review layer (api + UI) in the foreground"
	@echo "    dos-health                  check the running server's /api/health endpoint"
	@echo "    dos-ports                   print this fork's api + dev-server ports (JSON)"
	@echo "    dos-verify                  rebuild + render + headless render-only boot proof"
	@echo "                                (an empty instance gets the fresh-fork init first)"
	@echo "    dos-map                     rescan the host repo into the dr_* catalogue"
	@echo "    dos-render                  render the DB to the flat _sc files (ARGS forwarded)"
	@echo "    dos-snapshot                serialize per-instance tables to ignored local content.sql, plus"
	@echo "                                any authored map sections to local map/content.sql"
	@echo "    dos-deps                    install python (.venv) + dev kit + node deps"
	@echo "                                (host-managed .venv: pip skipped, pins verified)"
	@echo "    dos-setup / dos-install     first-launch bootstrap: reqs, harness, first shell"
	@echo "    dos-rollback                undo a bad update — restore the DB + engine pair"
	@echo "                                (without engine.ref.prev: DB only, with a warning)"
	@echo "    dos-update-harnesses        refresh claude + opencode + codex + vibe + kimi in the sandbox image"
	@echo "                                (rolls the harness epoch + rebuilds; dos-r to run them)"
	@echo "    dos-harness-status          harness CLI versions in the sandbox + is a harness rebuild owed"
	@echo "    dos-feat / dos-feature      list/enable/disable opt-in features"
	@echo "    dos-eject                   ONE-WAY: own the engine"
	@echo "    dos-remove                  ONE-WAY: verified DB backup, then remove subfloor from this repo"
	@echo "                                (ARGS='--dry-run' previews; ARGS='--yes' skips only confirmation)"
	@echo ""
	@echo "    dos ARGS='<cmd>'            generic ./sc passthrough"
	@echo ""
