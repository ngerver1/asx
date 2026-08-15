DATABASE_URL ?= postgresql://asx:asx@localhost:5432/asx

.PHONY: test migrate reprocess ops-report monitor install

install:
	pip install -e ".[dev]"

migrate:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli migrate

test:
	DATABASE_URL=$(DATABASE_URL) python -m pytest

# The only path for fixing systematic parse errors (SPEC §3).
# Usage: make reprocess PARSER=app3y SINCE=2026-01-01
reprocess:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli reprocess --parser=$(PARSER) $(if $(SINCE),--since=$(SINCE),) $(if $(APPLY),--apply,)

ops-report:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli ops-report

monitor:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli monitor
