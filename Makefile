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
	@set -eu; \
	work=$$(mktemp -d); pid=; \
	trap 'if [ -n "$$pid" ]; then kill "$$pid" 2>/dev/null || true; wait "$$pid" || true; fi; rm -rf "$$work"' EXIT; \
	trap 'exit 1' INT TERM; \
	export DATABASE_URL="sqlite:///$$work/app.db"; \
	rm -f "$$work/app.db"; \
	$(ALEMBIC) upgrade head; \
	$(PYTHON) bin/seed.py; \
	$(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >"$$work/server.log" 2>&1 & pid=$$!; \
	for attempt in $$(seq 1 50); do \
		if ! kill -0 "$$pid" 2>/dev/null; then break; fi; \
		if ! curl --noproxy "*" -s --max-time 1 http://127.0.0.1:8000/health >/dev/null; then sleep 0.1; continue; fi; \
		break; \
	done; \
	if ! kill -0 "$$pid" 2>/dev/null || \
		! status=$$(curl --noproxy "*" -sS --max-time 10 -o "$$work/health.json" -w '%{http_code}' http://127.0.0.1:8000/health) || \
		[ "$$status" != 200 ]; then cat "$$work/server.log"; [ ! -f "$$work/health.json" ] || cat "$$work/health.json"; exit 1; fi; \
	$(PYTHON) -c 'import json, sys; assert json.load(open(sys.argv[1])) == {"code": 0, "message": "ok", "data": {"db": "ok", "migration": "0001"}}' "$$work/health.json"; \
	echo "Smoke PASS: GET /health HTTP $$status"
