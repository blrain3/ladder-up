.PHONY: help diagrams docs clean

help:
	@echo "make diagrams  # 由 puml 出 PNG"
	@echo "make docs      # 文档转 docx/pdf"
	@echo "make clean     # 清理中间产物"

diagrams:
	@bash scripts/render-diagrams.sh

docs:
	@bash scripts/build-docs.sh

clean:
	@rm -rf docs/_build
	@find . -name '*.tmp' -delete
	@echo "done"

PYTHON := .venv/bin/python
ALEMBIC := .venv/bin/alembic
PYTEST := .venv/bin/pytest
DATABASE_URL ?= sqlite:///data/app.db
APP_ENV ?= development
SESSION_TTL_HOURS ?= 8
export DATABASE_URL APP_ENV SESSION_TTL_HOURS

.PHONY: install migrate seed test smoke backup restore-check run

install:
	$(PYTHON) -m pip install -r requirements.txt

migrate:
	mkdir -p data
	$(ALEMBIC) upgrade head

seed:
	$(PYTHON) bin/seed.py

test:
	$(PYTEST) -q

backup:
	$(PYTHON) bin/backup.py

restore-check:
	$(PYTHON) bin/restore_check.py

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

smoke:
	@bash scripts/smoke.sh "$(PYTHON)" "$(ALEMBIC)"
