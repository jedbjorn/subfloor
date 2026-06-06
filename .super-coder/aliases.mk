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
#   make enter          boot a shell on the host (pick shell + harness)
#   make enter s=ap01   boot the 'ap01' shell directly
#   make launch         start the review GUI in the background
#   make down           stop the review GUI
#   make sc ARGS=health run any ./sc subcommand (passthrough)
#
SC := ./sc
.PHONY: launch enter down logs serve health ports verify update snapshot render map install sc

launch:   ; $(SC) launch
enter:    ; $(SC) $(if $(s),enter-$(s),enter)
down:     ; $(SC) down
logs:     ; $(SC) logs
serve:    ; $(SC) serve
health:   ; $(SC) health
ports:    ; $(SC) ports
verify:   ; $(SC) verify
update:   ; $(SC) update
snapshot: ; $(SC) snapshot
render:   ; $(SC) render $(ARGS)
map:      ; $(SC) map
install:  ; $(SC) install
sc:       ; $(SC) $(ARGS)
