# super-coder make aliases — convenience wrappers around ./sc.
#
# The dispatcher ./sc is the canonical interface; these targets only delegate.
# This file travels with the engine, so `make launch` / `make enter` work in a
# fork exactly as in the source repo — WITHOUT super-coder ever owning or
# clobbering the fork's Makefile (#13): install wires a fork to *include* this
# file (or auto-creates a one-line Makefile when the fork has none); it never
# overwrites a Makefile you already have. Delete the include and you lose
# nothing — `./sc <cmd>` is identical.
#
#   make launch         build + start the docker sandbox (+ review GUI)
#   make enter          attach an interactive session (pick shell + harness)
#   make enter s=ap01   attach + boot the 'ap01' shell directly
#   make down           stop the sandbox
#   make update         fetch + materialize the engine, reconcile in place
#   make update-harnesses  update claude + opencode + codex + vibe to latest
#   make rollback       sound undo of a bad update (restore DB + engine)
#   make deps           install this fork's deps into the bind-mounted .venv + node_modules
#   make test           run backend (.venv pytest / unittest) + UI (vitest) suites
#   make sc ARGS=health run any ./sc subcommand (passthrough)
#
SC := ./sc
.PHONY: launch enter down build logs serve health ports verify update update-harnesses rollback snapshot render map install sc deps test

launch:            ; $(SC) launch
enter:             ; $(SC) $(if $(s),enter-$(s),enter)
down:              ; $(SC) down
build:             ; $(SC) build
logs:              ; $(SC) logs
serve:             ; $(SC) serve
health:            ; $(SC) health
ports:             ; $(SC) ports
verify:            ; $(SC) verify
update:            ; $(SC) update
update-harnesses:  ; $(SC) update-harnesses
rollback:          ; $(SC) rollback
snapshot: ; $(SC) snapshot
render:   ; $(SC) render $(ARGS)
map:      ; $(SC) map
install:  ; $(SC) install
deps:     ; $(SC) deps
test:     ; $(SC) test
sc:       ; $(SC) $(ARGS)
