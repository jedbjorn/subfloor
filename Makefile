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
#   make dos-l / dos-launch  start host services + review GUI
#   make dos-e / dos-enter   boot a bare-metal session (pick shell + harness)
#   make dos-e s=cc          attach + boot the 'cc' shell directly
#   make dos-r / dos-restart DB backup, then restart host services
#   make dos-d / dos-down    stop host services
#   make dos-u / dos-update  pin + materialize the sibling subfloor engine
#   make dos-h / dos-help    list / describe all commands
include .super-coder/aliases.mk

# sc-cachy is an installation, not an engine-source fork. Keep its two local
# policy choices outside the replaceable .super-coder tree: engine updates come
# from the sibling subfloor clone through an installed-mode adapter, and
# lifecycle/session commands run directly on this host. Target-specific SC
# values reuse the upstream alias recipes.
SC_ENGINE_UPDATE := sh scripts_sc/update_engine.sh
SC_HOST := sh scripts_sc/host_sc.sh

dos-update dos-u: SC := $(SC_ENGINE_UPDATE)
dos-enter dos-e dos-launch dos-l dos-restart dos-r dos-down dos-d dos-logs: SC := $(SC_HOST)

# Friendly default: bare `make` and `make help` print the command chart, instead
# of running the first included target (dos-enter, which attaches a session).
# Kept here rather than in aliases.mk so the bare `help` name never propagates
# into a fork where it could collide with the fork's own targets. dos-h points on
# to `make dos-help` for the full list.
.DEFAULT_GOAL := help
.PHONY: help
help: dos-h
