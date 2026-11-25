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

# 有 .venv 就用它，否则退回系统解释器（CI 用后者）
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
ALEMBIC ?= $(if $(wildcard .venv/bin/alembic),.venv/bin/alembic,alembic)
PYTEST ?= $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,pytest)
DATABASE_URL ?= sqlite:///data/app.db
APP_ENV ?= development
SESSION_TTL_HOURS ?= 8
export DATABASE_URL APP_ENV SESSION_TTL_HOURS

.PHONY: install migrate seed test lint typecheck cov check smoke backup restore-check run hooks branch

install:
	$(PYTHON) -m pip install -r requirements.txt

migrate:
	mkdir -p data
	$(ALEMBIC) upgrade head

seed:
	$(PYTHON) bin/seed.py

test:
	$(PYTEST) -q

hooks:
	@mkdir -p .git/hooks && cp scripts/pre-commit-guard.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit && echo "pre-commit 守卫已安装"

branch:
	@git rev-parse --abbrev-ref HEAD

lint:
	$(PYTHON) -m ruff check app bin tests scripts

typecheck:
	$(PYTHON) -m mypy

cov:
	$(PYTEST) --cov=app --cov-report=term-missing --cov-fail-under=80

check:
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test
	$(MAKE) smoke

backup:
	$(PYTHON) bin/backup.py

restore-check:
	$(PYTHON) bin/restore_check.py

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

smoke:
	@bash scripts/smoke.sh "$(PYTHON)" "$(ALEMBIC)"
