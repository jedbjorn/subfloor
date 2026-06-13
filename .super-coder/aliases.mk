# super-coder make aliases — convenience wrappers around ./sc.
#
# The dispatcher ./sc is the canonical interface; these targets only delegate.
# This file travels with the engine, so the aliases work in a fork exactly as in
# the source repo — WITHOUT super-coder ever owning or clobbering the fork's
# Makefile (#13): install wires a fork to *include* this file (or auto-creates a
# one-line Makefile when the fork has none); it never overwrites a Makefile you
# already have. Delete the include and you lose nothing — `./sc <cmd>` is identical.
#
# Every target is `sc-`prefixed so including this file can't collide with your
# own `test` / `build` / `install` targets. The house standard: the hot commands
# get a one-letter short form too; everything else is long-form only.
#
#   HOT (short + long):
#   make sc-e   / sc-enter     attach a session (pick shell + harness); sc-e s=ap01 boots one
#   make sc-l   / sc-launch    build + start the docker sandbox (+ review GUI)
#   make sc-r   / sc-restart   down + launch — recreate the sandbox fresh
#   make sc-d   / sc-down      stop the sandbox
#   make sc-u   / sc-update    fetch + materialize the engine, reconcile in place
#   make sc-t   / sc-test      backend (pytest/unittest) + UI (vitest) suites
#   make sc-h                  list the commands
#   make sc-help               list + describe the commands
#
#   LONG-ONLY: sc-build sc-logs sc-serve sc-health sc-ports sc-verify sc-map
#              sc-render sc-snapshot sc-deps sc-install sc-rollback
#              sc-update-harnesses · passthrough: make sc ARGS=health
#
SC := ./sc
.PHONY: sc-e sc-enter sc-l sc-launch sc-r sc-restart sc-d sc-down sc-u sc-update \
        sc-t sc-test sc-h sc-help sc-build sc-logs sc-serve sc-health sc-ports \
        sc-verify sc-map sc-render sc-snapshot sc-deps sc-install sc-rollback \
        sc-update-harnesses sc

# Hot commands — long form, with a one-letter alias delegating to it.
sc-enter:            ; $(SC) $(if $(s),enter-$(s),enter)
sc-e: sc-enter
sc-launch:           ; $(SC) launch
sc-l: sc-launch
sc-restart:          ; $(SC) restart
sc-r: sc-restart
sc-down:             ; $(SC) down
sc-d: sc-down
sc-update:           ; $(SC) update
sc-u: sc-update
sc-test:             ; $(SC) test
sc-t: sc-test

# Long-only commands.
sc-build:            ; $(SC) build
sc-logs:             ; $(SC) logs
sc-serve:            ; $(SC) serve
sc-health:           ; $(SC) health
sc-ports:            ; $(SC) ports
sc-verify:           ; $(SC) verify
sc-map:              ; $(SC) map
sc-render:           ; $(SC) render $(ARGS)
sc-snapshot:         ; $(SC) snapshot
sc-deps:             ; $(SC) deps
sc-install:          ; $(SC) install
sc-rollback:         ; $(SC) rollback
sc-update-harnesses: ; $(SC) update-harnesses

# Passthrough: run any ./sc subcommand — make sc ARGS=health
sc:                  ; $(SC) $(ARGS)

# Help — sc-h lists, sc-help lists + describes.
sc-h:
	@echo "sc-e(nter) sc-l(aunch) sc-r(estart) sc-d(own) sc-u(pdate) sc-t(est) sc-h/sc-help"
	@echo "long-only: sc-build sc-logs sc-serve sc-health sc-ports sc-verify sc-map sc-render sc-snapshot sc-deps sc-install sc-rollback sc-update-harnesses | passthrough: make sc ARGS=<cmd>"
sc-help:
	@echo "super-coder make aliases — every target delegates to ./sc"
	@echo ""
	@echo "  hot (short + long):"
	@echo "    sc-e  / sc-enter     attach a session (pick shell + harness); sc-e s=ap01 boots one"
	@echo "    sc-l  / sc-launch    build + start the docker sandbox (+ review GUI)"
	@echo "    sc-r  / sc-restart   down + launch — recreate the sandbox fresh"
	@echo "    sc-d  / sc-down      stop the sandbox"
	@echo "    sc-u  / sc-update    fetch + materialize the engine, reconcile in place"
	@echo "    sc-t  / sc-test      backend (pytest/unittest) + UI (vitest) suites"
	@echo "    sc-h                 list commands   ·   sc-help   this view"
	@echo ""
	@echo "  long-only: sc-build sc-logs sc-serve sc-health sc-ports sc-verify"
	@echo "             sc-map sc-render sc-snapshot sc-deps sc-install sc-rollback sc-update-harnesses"
	@echo "  passthrough: make sc ARGS=<subcommand>   (e.g. make sc ARGS=doctor)"
