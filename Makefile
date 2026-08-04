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
#   make dos-u / dos-update  full upstream update (see bare-metal layer below)
#   make dos-h / dos-help    list / describe all commands
include .super-coder/aliases.mk

# ── bare-metal layer (this fork only) ─────────────────────────────────────────
# sc-cachy is a remote-less fork of subfloor itself; upstream arrives by
# local-path fetch from the sibling clone. dos-pull runs the deterministic
# pull: DB backup → ff sibling subfloor main from GitHub → fetch + merge here
# (conflict guidance printed, make stops before update). Hooking it as a
# prerequisite makes `make dos-u` the whole procedure:
#   make dos-u   pull + merge upstream, then ./sc update (aliases.mk recipe)
#   make dos-r   restart host services (own DB backup), then verify /api/health
# Engine-reconcile only (no pull): ./sc update
.PHONY: dos-pull
dos-pull: ; sh scripts_sc/pull_subfloor.sh
dos-update: dos-pull

# Friendly default: bare `make` and `make help` print the command chart, instead
# of running the first included target (dos-enter, which attaches a session).
# Kept here rather than in aliases.mk so the bare `help` name never propagates
# into a fork where it could collide with the fork's own targets. dos-h points on
# to `make dos-help` for the full list.
.DEFAULT_GOAL := help
.PHONY: help
help: dos-h
