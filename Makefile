ENGINE := .super-coder
DB     := $(ENGINE)/shell_db.db
PY     := python3

.PHONY: help rebuild migrate snapshot launch launch-% verify clean-db

help:
	@echo "super-coder — forkable shell substrate"
	@echo ""
	@echo "  make rebuild             build $(DB) from schema + migrations + snapshot"
	@echo "  make migrate             apply pending migrations to an existing $(DB)"
	@echo "  make snapshot            dump per-instance tables -> $(ENGINE)/snapshot/content.sql"
	@echo "  make launch              username auth + pick shell + render boot + exec harness"
	@echo "  make launch-<shortname>  boot that shell directly (skip picker)"
	@echo "  make verify              rebuild + render-only boot, print the artifact paths"
	@echo "  make clean-db            remove the rebuilt $(DB) (text serializations untouched)"

rebuild:
	@$(PY) $(ENGINE)/scripts/rebuild.py

migrate:
	@$(PY) $(ENGINE)/scripts/migrate.py $(DB)

snapshot:
	@$(PY) $(ENGINE)/scripts/snapshot.py

launch:
	@$(PY) $(ENGINE)/scripts/run.py

launch-%:
	@$(PY) $(ENGINE)/scripts/run.py $*

verify:
	@$(PY) $(ENGINE)/scripts/rebuild.py
	@RENDER_ONLY=1 $(PY) $(ENGINE)/scripts/run.py --first

clean-db:
	@rm -f $(DB) $(DB)-wal $(DB)-shm && echo "removed $(DB) (rebuild with: make rebuild)"
