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
#   make dos-u   / dos-update    fetch + materialize the engine, reconcile in place
#   make dos-t   / dos-test      backend (pytest/unittest) + UI (vitest) suites
#   make dos-h                   list the commands
#   make dos-help                list + describe the commands
#
#   LONG-ONLY: dos-build dos-logs dos-serve dos-health dos-ports dos-verify dos-map
#              dos-render dos-snapshot dos-deps dos-install dos-rollback
#              dos-update-harnesses dos-feat/dos-feature dos-eject
#              passthrough: make dos ARGS=health
#
SC := ./sc
.PHONY: dos-e dos-enter dos-l dos-launch dos-r dos-restart dos-d dos-down dos-u dos-update \
        dos-t dos-test dos-h dos-help dos-build dos-logs dos-serve dos-health dos-ports \
        dos-verify dos-map dos-render dos-snapshot dos-deps dos-install dos-rollback \
        dos-update-harnesses dos-feat dos-feature dos-eject dos

# Hot commands — long form, with a one-letter alias delegating to it.
dos-enter:            ; $(SC) $(if $(s),enter-$(s),enter)
dos-e: dos-enter
dos-launch:           ; $(SC) launch
dos-l: dos-launch
dos-restart:          ; $(SC) restart
dos-r: dos-restart
dos-down:             ; $(SC) down
dos-d: dos-down
dos-update:           ; $(SC) update
dos-u: dos-update
dos-test:             ; $(SC) test
dos-t: dos-test

# Long-only commands.
dos-build:            ; $(SC) build
dos-logs:             ; $(SC) logs
dos-serve:            ; $(SC) serve
dos-health:           ; $(SC) health
dos-ports:            ; $(SC) ports
dos-verify:           ; $(SC) verify
dos-map:              ; $(SC) map
dos-render:           ; $(SC) render $(ARGS)
dos-snapshot:         ; $(SC) snapshot
dos-deps:             ; $(SC) deps
dos-install:          ; $(SC) install
dos-rollback:         ; $(SC) rollback
dos-update-harnesses: ; $(SC) update-harnesses
# Opt-in features: make dos-feat (list) · make dos-feat ARGS="enable pg"
dos-feature:          ; $(SC) feature $(ARGS)
dos-feat: dos-feature
# One-way divergence — interactive warning + typed confirmation in ./sc eject.
dos-eject:            ; $(SC) eject

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
	@echo "  └───────┴─────────────┴──────────────────────────────────────────┘"
	@echo "  more:  make dos-help  (full list + long-only)   ·   make dos ARGS=<cmd>"
	@echo ""
dos-help:
	@echo ""
	@echo "  super-coder — make dos-<cmd>   every target delegates to ./sc  (designs-OS 'dos-' standard)"
	@echo "  ┌──────────────────────┬──────────────────────────────────────────────────────────┐"
	@echo "  │ HOT  (short + long)  │ what it does                                             │"
	@echo "  ├──────────────────────┼──────────────────────────────────────────────────────────┤"
	@echo "  │ dos-e  dos-enter     │ attach a session — pick shell + harness (dos-e s=ap01)   │"
	@echo "  │ dos-l  dos-launch    │ build + start the docker sandbox (server + review GUI)   │"
	@echo "  │ dos-r  dos-restart   │ confirm (YES) + DB backup -> down + launch (fresh)       │"
	@echo "  │ dos-d  dos-down      │ stop + remove the sandbox container                      │"
	@echo "  │ dos-u  dos-update    │ fetch + materialize the engine, reconcile in place       │"
	@echo "  │ dos-t  dos-test      │ backend (pytest/unittest) + UI (vitest) suites           │"
	@echo "  ├──────────────────────┼──────────────────────────────────────────────────────────┤"
	@echo "  │ MORE  (long-only)    │ what it does                                             │"
	@echo "  ├──────────────────────┼──────────────────────────────────────────────────────────┤"
	@echo "  │ dos-build            │ (re)build the sandbox image                              │"
	@echo "  │ dos-logs             │ tail the sandbox server logs                             │"
	@echo "  │ dos-serve            │ run the review layer (api + UI) in the foreground        │"
	@echo "  │ dos-health           │ curl the review layer's /api/health                      │"
	@echo "  │ dos-ports            │ show this fork's derived port                            │"
	@echo "  │ dos-verify           │ rebuild + render + render-only boot (headless proof)     │"
	@echo "  │ dos-map              │ scan the repo into the dr_* catalogue                    │"
	@echo "  │ dos-render ARGS=<x>  │ render tracked flat _sc files (specs/docs/skills)        │"
	@echo "  │ dos-snapshot         │ dump per-instance tables -> .sc-state/content.sql        │"
	@echo "  │ dos-deps             │ install python (.venv) + node deps into the bind-mount   │"
	@echo "  │ dos-install          │ first-launch bootstrap for a fork                        │"
	@echo "  │ dos-rollback         │ undo a bad update — restore DB + engine together         │"
	@echo "  │ dos-update-harnesses │ update claude + opencode + codex + vibe to latest        │"
	@echo "  │ dos-feat ARGS=<x>    │ opt-in features — list / enable pg·windows·tailnet       │"
	@echo "  │ dos-eject            │ ONE-WAY: own the engine — stop tracking upstream         │"
	@echo "  ├──────────────────────┼──────────────────────────────────────────────────────────┤"
	@echo "  │ dos ARGS=<cmd>       │ passthrough to any ./sc subcommand (make dos ARGS=doctor)│"
	@echo "  └──────────────────────┴──────────────────────────────────────────────────────────┘"
	@echo ""
