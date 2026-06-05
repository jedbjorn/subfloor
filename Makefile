# super-coder — convenience targets. All logic lives in ./sc (the dispatcher);
# these only delegate. `sc` deliberately owns the engine so it travels with a
# fork (install.py checks out `.super-coder` + `sc`, NOT this Makefile) — so this
# file is source-repo ergonomics only, never propagates, and never clobbers a
# fork's own Makefile. Delete it and you lose nothing: `./sc <cmd>` is identical.
#
#   make launch            build + start the docker sandbox
#   make enter             attach an interactive session (pick shell + harness)
#   make enter s=cc        attach + boot the 'cc' shell directly
#   make down              stop the sandbox
.PHONY: launch enter down build logs serve health ports verify install

launch:  ; ./sc launch
enter:   ; ./sc $(if $(s),enter-$(s),enter)
down:    ; ./sc down
build:   ; ./sc build
logs:    ; ./sc logs
serve:   ; ./sc serve
health:  ; ./sc health
ports:   ; ./sc ports
verify:  ; ./sc verify
install: ; ./sc install
