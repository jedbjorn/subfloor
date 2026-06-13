# super-coder — convenience targets. All logic lives in ./sc (the dispatcher);
# these only delegate. `sc` deliberately owns the engine so it travels with a
# fork (install.py checks out `.super-coder` + `sc`, NOT this Makefile) — so this
# file is source-repo ergonomics only, never propagates, and never clobbers a
# fork's own Makefile. Delete it and you lose nothing: `./sc <cmd>` is identical.
#
# The targets live in .super-coder/aliases.mk — the single source of truth
# shared with forks (install wires a fork to include the same file). Edit the
# aliases there, not here.
#
#   make sc-l / sc-launch  build + start the docker sandbox
#   make sc-e / sc-enter   attach an interactive session (pick shell + harness)
#   make sc-e s=cc         attach + boot the 'cc' shell directly
#   make sc-r / sc-restart down + launch — recreate the sandbox fresh
#   make sc-d / sc-down    stop the sandbox
#   make sc-h / sc-help    list / describe all commands
include .super-coder/aliases.mk
