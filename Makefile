ENGINE := .super-coder
DB     := $(ENGINE)/shell_db.db
PY     := python3

.PHONY: help rebuild migrate snapshot render seed-skills launch launch-% verify clean-db

help:
	@echo "super-coder — forkable shell substrate"
	@echo ""
	@echo "  make rebuild             build $(DB) from schema + migrations + snapshot"
	@echo "  make migrate             apply pending migrations to an existing $(DB)"
	@echo "  make snapshot            dump per-instance tables -> $(ENGINE)/snapshot/content.sql"
	@echo "  make render              render tracked flat _sc files (specs/docs/skills/roadmap)"
	@echo "  make seed-skills         regenerate the skills seed migration from assets/skills/"
	@echo "  make launch              username auth + pick shell + render boot + exec harness"
	@echo "  make launch-<shortname>  boot that shell directly (skip picker)"
	@echo "  make verify              rebuild + flat render + render-only boot (headless proof)"
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

launch:
	@$(PY) $(ENGINE)/scripts/run.py

launch-%:
	@$(PY) $(ENGINE)/scripts/run.py $*

verify:
	@$(PY) $(ENGINE)/scripts/rebuild.py
	@$(PY) $(ENGINE)/scripts/render.py flat
	@RENDER_ONLY=1 $(PY) $(ENGINE)/scripts/run.py --first

clean-db:
	@rm -f $(DB) $(DB)-wal $(DB)-shm && echo "removed $(DB) (rebuild with: make rebuild)"
