# 개발 명령 (tech.md 4장). Python 명령은 backend/.venv를 쓴다: make venv
PYTHON ?= python3
VENV := backend/.venv
VPY := $(VENV)/bin/python
COMPOSE := docker compose --env-file .env -f deploy/compose/docker-compose.yml
# 통합 시험은 이 DB 서버에 임시 DB를 만들었다 지운다 (소유자 계정)
TEST_DATABASE_URL ?= postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine

.PHONY: venv up down logs migrate seed verify-audit sample-video test test-unit test-integration test-acceptance \
        lint typecheck security license-check secrets-check web-check contracts-check traceability \
        traceability-write ci agent-build agent-test bench verify-integrity sample-observations \
        release-gate release-notes sbom vuln-scan loadtest backup restore-test test-web-e2e agent-generate agent-generate-check

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

# 수용시험 (17번): covers 마커가 있는 시험·수용시험을 돌리고 세부 ID별 보고서 var/acceptance.{json,md}를 만든다
test-acceptance:
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" $(VPY) tools/run_acceptance.py

# 릴리스 게이트 (17번 요구사항 6): make release-gate MILESTONE=M1 BASE_REF=<이전 태그>
release-gate:
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" MILESTONE="$(MILESTONE)" BASE_REF="$(BASE_REF)" $(VPY) tools/release_gate.py

release-notes:
	BASE_REF="$(BASE_REF)" $(VPY) tools/release_gate.py --notes

# SBOM·취약점 (17번 요구사항 4): syft·trivy가 설치돼 있어야 한다 (CI는 security 작업)
sbom:
	mkdir -p var && syft dir:. -o spdx-json > var/sbom.spdx.json && $(VPY) tools/check_sbom_licenses.py var/sbom.spdx.json

vuln-scan:
	trivy fs --scanners vuln --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed .

# 부하 시험 (T12, 수치는 실측 후): loadtest/README.md
loadtest:
	mkdir -p var && locust -f loadtest/locustfile.py --host $${LOAD_HOST:-http://localhost:8000} --headless \
		-u $${LOAD_USERS:-10} -r $${LOAD_RATE:-2} -t $${LOAD_TIME:-1m} --json > var/loadtest.json

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
	cd web && npm run -s lint && npm run -s typecheck && npm run -s check:api

# T02-g: 전용 DB·모의 S3·실제 브라우저를 만들고 탭 종료 후 multipart 재개를 확인한다.
test-web-e2e:
	$(VPY) tools/web_acceptance.py --out var/acceptance-results.jsonl

contracts-check:
	$(PYTHON) tools/validate_contracts.py

traceability:
	$(PYTHON) tools/check_traceability.py

traceability-write:
	$(PYTHON) tools/check_traceability.py --write

ci: contracts-check traceability lint typecheck test-unit security license-check secrets-check

agent-build:
	cd agent && go build -o bin/marine-agent ./cmd/marine-agent && GOOS=windows go build -o bin/marine-agent.exe ./cmd/marine-agent

agent-generate:
	$(VPY) tools/generate_agent_api.py

agent-generate-check:
	$(VPY) tools/generate_agent_api.py --check

# MQTT 시험은 MQTT_TEST_BROKER가 있을 때만 (docker compose --profile sensor up mosquitto)
agent-test:
	cd agent && go vet ./... && go test -race ./...

bench:
	cd backend && .venv/bin/python -m app.bench --out ../var/bench.json

# 계층형 백업·복원 시험 (08번, ADR-0021). pg_dump·pg_restore는 DB 서버와 같은 주버전이어야 한다
backup:
	$(COMPOSE) run --rm migrate python -m app.cli backup

restore-test:
	$(COMPOSE) run --rm migrate python -m app.cli restore-test --out /tmp/restore-test.json

verify-integrity:
	$(COMPOSE) run --rm migrate python -m app.cli verify-integrity

sample-observations:
	$(PYTHON) tools/sample_observations.py --device $${DEVICE:-dev_00000000000000000000000303} --out var/samples/obs
