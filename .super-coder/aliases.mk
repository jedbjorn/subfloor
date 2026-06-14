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
#   make dos-r   / dos-restart   down + launch — recreate the sandbox fresh
#   make dos-d   / dos-down      stop the sandbox
#   make dos-u   / dos-update    fetch + materialize the engine, reconcile in place
#   make dos-t   / dos-test      backend (pytest/unittest) + UI (vitest) suites
#   make dos-h                   list the commands
#   make dos-help                list + describe the commands
#
#   LONG-ONLY: dos-build dos-logs dos-serve dos-health dos-ports dos-verify dos-map
#              dos-render dos-snapshot dos-deps dos-install dos-rollback
#              dos-update-harnesses · passthrough: make dos ARGS=health
#
SC := ./sc
.PHONY: dos-e dos-enter dos-l dos-launch dos-r dos-restart dos-d dos-down dos-u dos-update \
        dos-t dos-test dos-h dos-help dos-build dos-logs dos-serve dos-health dos-ports \
        dos-verify dos-map dos-render dos-snapshot dos-deps dos-install dos-rollback \
        dos-update-harnesses dos

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

# Passthrough: run any ./sc subcommand — make dos ARGS=health
dos:                  ; $(SC) $(ARGS)

# Help — dos-h lists, dos-help lists + describes.
dos-h:
	@echo "dos-e(nter) dos-l(aunch) dos-r(estart) dos-d(own) dos-u(pdate) dos-t(est) dos-h/dos-help"
	@echo "long-only: dos-build dos-logs dos-serve dos-health dos-ports dos-verify dos-map dos-render dos-snapshot dos-deps dos-install dos-rollback dos-update-harnesses | passthrough: make dos ARGS=<cmd>"
dos-help:
	@echo "super-coder make aliases — every target delegates to ./sc (designs-OS 'dos-' standard)"
	@echo ""
	@echo "  hot (short + long):"
	@echo "    dos-e  / dos-enter     attach a session (pick shell + harness); dos-e s=ap01 boots one"
	@echo "    dos-l  / dos-launch    build + start the docker sandbox (+ review GUI)"
	@echo "    dos-r  / dos-restart   down + launch — recreate the sandbox fresh"
	@echo "    dos-d  / dos-down      stop the sandbox"
	@echo "    dos-u  / dos-update    fetch + materialize the engine, reconcile in place"
	@echo "    dos-t  / dos-test      backend (pytest/unittest) + UI (vitest) suites"
	@echo "    dos-h                  list commands   ·   dos-help   this view"
	@echo ""
	@echo "  long-only: dos-build dos-logs dos-serve dos-health dos-ports dos-verify"
	@echo "             dos-map dos-render dos-snapshot dos-deps dos-install dos-rollback dos-update-harnesses"
	@echo "  passthrough: make dos ARGS=<subcommand>   (e.g. make dos ARGS=doctor)"
