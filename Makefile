ENGINE := .super-coder
DB     := $(ENGINE)/shell_db.db
PY     := python3

ECO    := $(ENGINE)/ecosystem.config.cjs

.PHONY: help rebuild migrate snapshot render seed-skills init launch launch-% verify clean-db up down restart health serve ports

help:
	@echo "super-coder — forkable shell substrate"
	@echo ""
	@echo "  make rebuild             build $(DB) from schema + migrations + snapshot"
	@echo "  make migrate             apply pending migrations to an existing $(DB)"
	@echo "  make snapshot            dump per-instance tables -> $(ENGINE)/snapshot/content.sql"
	@echo "  make render              render tracked flat _sc files (specs/docs/skills/roadmap)"
	@echo "  make seed-skills         regenerate the skills seed migration from assets/skills/"
	@echo "  make init                seed a fresh fork's first user + shell (run once after install)"
	@echo "  make launch              username auth + pick shell + render boot + exec harness"
	@echo "  make launch-<shortname>  boot that shell directly (skip picker)"
	@echo "  make verify              rebuild + flat render + render-only boot (headless proof)"
	@echo "  make up / down / restart start/stop the localhost review layer (pm2, per-fork port)"
	@echo "  make serve               run the review layer in the foreground (no pm2)"
	@echo "  make health              curl the review layer's /api/health"
	@echo "  make ports               show this fork's derived port"
	@echo "  make clean-db            remove the rebuilt $(DB) (text serializations untouched)"

rebuild:
	@$(PY) $(ENGINE)/scripts/rebuild.py

migrate:
	@$(PY) $(ENGINE)/scripts/migrate.py $(DB)

snapshot:
	@$(PY) $(ENGINE)/scripts/snapshot.py

render:
	@$(PY) $(ENGINE)/scripts/render.py flat

seed-skills:
	@$(PY) $(ENGINE)/scripts/seed_skills.py

init:
	@$(PY) $(ENGINE)/scripts/init_fork.py

launch:
	@$(PY) $(ENGINE)/scripts/run.py

launch-%:
	@$(PY) $(ENGINE)/scripts/run.py $*

verify:
	@$(PY) $(ENGINE)/scripts/rebuild.py
	@$(PY) $(ENGINE)/scripts/render.py flat
	@RENDER_ONLY=1 $(PY) $(ENGINE)/scripts/run.py --first

ports:
	@$(PY) $(ENGINE)/scripts/ports.py show

up:
	@$(PY) $(ENGINE)/scripts/ports.py ensure >/dev/null
	@pm2 start $(ECO) && echo "→ review layer up at http://127.0.0.1:$$($(PY) $(ENGINE)/scripts/ports.py port)"

down:
	@pm2 delete $(ECO) 2>/dev/null && echo "→ review layer stopped" || echo "→ not running"

restart:
	@pm2 restart $(ECO)

serve:
	@$(PY) $(ENGINE)/api/server.py

health:
	@curl -s http://127.0.0.1:$$($(PY) $(ENGINE)/scripts/ports.py port)/api/health && echo ""

clean-db:
	@rm -f $(DB) $(DB)-wal $(DB)-shm && echo "removed $(DB) (rebuild with: make rebuild)"
