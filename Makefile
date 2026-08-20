DATABASE_URL ?= postgresql://asx:asx@localhost:5432/asx

.PHONY: test test-all migrate reprocess ops-report monitor install

install:
	pip install -e ".[dev]"

migrate:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli migrate

test:
	DATABASE_URL=$(DATABASE_URL) python -m pytest

# The suite with NOTHING SKIPPED. Without a database 44 tests skip, and a
# skipped test is indistinguishable from a passing one in the summary line —
# which is how two green local runs shipped two red CI runs. This is what CI
# actually runs; run it before saying the suite passes.
test-all:
	./tools/dev-postgres.sh start >/dev/null
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli migrate
	DATABASE_URL=$(DATABASE_URL) python -m pytest -q

# The only path for fixing systematic parse errors (SPEC §3).
# Usage: make reprocess PARSER=app3y SINCE=2026-01-01
reprocess:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli reprocess --parser=$(PARSER) $(if $(SINCE),--since=$(SINCE),) $(if $(APPLY),--apply,)

ops-report:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli ops-report

monitor:
	DATABASE_URL=$(DATABASE_URL) python -m asx.cli monitor
