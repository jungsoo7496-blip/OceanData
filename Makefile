# 개발 명령 (tech.md 4장). Python 명령은 backend/.venv를 쓴다: make venv
PYTHON ?= python3
VENV := backend/.venv
VPY := $(VENV)/bin/python
COMPOSE := docker compose --env-file .env -f deploy/compose/docker-compose.yml
# 통합 시험은 이 DB 서버에 임시 DB를 만들었다 지운다 (소유자 계정)
TEST_DATABASE_URL ?= postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine

.PHONY: venv up down logs migrate seed verify-audit sample-video test test-unit test-integration test-acceptance \
        lint typecheck security license-check secrets-check web-check contracts-check traceability \
        traceability-write ci agent-build agent-test

venv:
	cd backend && uv venv -q .venv && uv pip install -q -p .venv -e ".[dev]" -r ../tools/requirements.txt

up:
	tools/compose_up.sh

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

migrate:
	$(COMPOSE) run --rm migrate

seed:
	$(COMPOSE) run --rm migrate python -m app.cli seed

verify-audit:
	$(COMPOSE) run --rm migrate python -m app.cli verify-audit

sample-video:
	tools/sample_video.sh var/samples

test: test-unit web-check

test-unit:
	cd backend && .venv/bin/pytest -q tests/unit

test-integration:
	cd backend && TEST_DATABASE_URL="$(TEST_DATABASE_URL)" .venv/bin/pytest -q -m "integration and not acceptance"

test-acceptance:
	cd backend && TEST_DATABASE_URL="$(TEST_DATABASE_URL)" .venv/bin/pytest -q -m acceptance

lint:
	cd backend && .venv/bin/ruff check app tests
	cd web && npm run -s lint

typecheck:
	cd backend && .venv/bin/mypy app
	cd web && npm run -s typecheck

security:
	cd backend && .venv/bin/bandit -q -ll -r app -x app/db/migrations
	cd backend && .venv/bin/pip-audit --skip-editable

license-check:
	$(VPY) tools/check_licenses.py

secrets-check:
	$(VPY) tools/check_secrets.py

web-check:
	cd web && npm run -s lint && npm run -s typecheck

contracts-check:
	$(PYTHON) tools/validate_contracts.py

traceability:
	$(PYTHON) tools/check_traceability.py

traceability-write:
	$(PYTHON) tools/check_traceability.py --write

ci: contracts-check traceability lint typecheck test-unit security license-check secrets-check

agent-build:
	cd agent && go build -o bin/marine-agent ./cmd/marine-agent && GOOS=windows go build -o bin/marine-agent.exe ./cmd/marine-agent

agent-test:
	cd agent && go vet ./... && go test -race ./...
