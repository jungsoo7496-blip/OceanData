# 개발 명령 (tech.md 4장). Python 명령은 backend/.venv를 쓴다: make venv
VENV := backend/.venv
VPY := $(VENV)/bin/python
# 계약·추적 검사도 venv(tools/requirements.txt 포함)가 있으면 그것을 쓴다. 시스템 python3에 openapi-spec-validator가
# 없으면 계약 검사가 경고만 내고 구조 검사로 줄어든다. venv가 없으면 python3 (CI contracts 작업)
PYTHON ?= $(if $(wildcard $(VPY)),$(VPY),python3)
COMPOSE := docker compose --env-file .env -f deploy/compose/docker-compose.yml
# 통합 시험은 이 DB 서버에 임시 DB를 만들었다 지운다 (소유자 계정)
TEST_DATABASE_URL ?= postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine

.PHONY: venv up down logs migrate seed verify-audit sample-video test test-unit test-integration test-acceptance \
        lint typecheck security license-check secrets-check web-check contracts-check traceability \
        traceability-write ci agent-build agent-test bench verify-integrity sample-observations \
        release-gate release-notes sbom vuln-scan loadtest backup restore-test test-web-e2e agent-generate agent-generate-check \
        agent-lint agent-fuzz web-a11y runtime-venv

# --allow-existing: 다시 실행하면 기존 venv에 의존성만 갱신한다 (uv는 이 옵션 없이 기존 venv가 있으면 실패한다)
venv:
	cd backend && uv venv -q --allow-existing .venv && uv pip install -q -p .venv -e ".[dev]" -r ../tools/requirements.txt

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

test: test-unit agent-test web-check  # tech.md 4장: 단위 테스트 (Python·Go·TS)

test-unit:
	cd backend && .venv/bin/pytest -q tests/unit

test-integration:
	cd backend && TEST_DATABASE_URL="$(TEST_DATABASE_URL)" .venv/bin/pytest -q -m "integration and not acceptance"

# 수용시험 (17번): covers 마커가 있는 시험·수용시험을 돌리고 세부 ID별 보고서 var/acceptance.{json,md}를 만든다
test-acceptance:
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" $(VPY) tools/run_acceptance.py

# 릴리스 게이트 (17번 요구사항 6): make release-gate MILESTONE=M1 BASE_REF=<이전 태그>
release-gate: runtime-venv  # 엄격 라이선스 검사도 런타임 의존성만 본다 (RUNTIME_PYTHON)
	RUNTIME_PYTHON=var/runtime-venv/bin/python TEST_DATABASE_URL="$(TEST_DATABASE_URL)" MILESTONE="$(MILESTONE)" \
		BASE_REF="$(BASE_REF)" $(VPY) tools/release_gate.py

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

# CI와 같이 런타임 의존성만 설치한 환경(var/runtime-venv, 매번 새로) — 라이선스 검사는 이것을 본다.
# backend/.venv에는 개발 도구(moto·pip-audit 등)가 끌고 온 납품 밖 의존성이 있어 허용 목록 검사가 실패한다
runtime-venv:
	rm -rf var/runtime-venv && uv venv -q -p $(VPY) var/runtime-venv && uv pip install -q -p var/runtime-venv ./backend

license-check: runtime-venv
	$(VPY) tools/check_licenses.py --python var/runtime-venv/bin/python

secrets-check:
	$(VPY) tools/check_secrets.py

web-check:
	cd web && npm run -s lint && npm run -s typecheck && npm run -s check:api

# 09번 작업 15: 빌드한 웹을 계약 모양 합성 /v1 응답으로 띄워 역할별 화면을 axe(WCAG 2.0·2.1 A·AA, serious·critical 0)·
# 휴대폰 폭(390px 가로 넘침 없음)·링크 초점 표시로 검사한다. 실제 스택 불필요. 브라우저 준비는 web/e2e/README.md
web-a11y:
	cd web && npm run -s build && npm run -s test:a11y

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

# 퍼즈 시험: 신뢰할 수 없는 입력(카메라 파일·서버 응답·업데이트 파일)을 읽는 파서의 Fuzz* 대상을 모두 찾아 대상마다
# FUZZTIME 동안 돌린다 (공유 장비에서도 돌도록 작업자 2개). 찾은 실패 입력은 testdata/fuzz/<대상>/에 남겨 agent-test의
# 회귀 시험으로 쓴다. CI에는 넣지 않는다: make agent-fuzz FUZZTIME=90s
FUZZTIME ?= 15s
agent-fuzz:
	cd agent && for f in $$(grep -rl --include='*_test.go' '^func Fuzz' . | sort); do \
		for t in $$(sed -n 's/^func \(Fuzz[A-Za-z0-9_]*\)(.*/\1/p' $$f); do \
			go test -run='^$$' -fuzz="^$$t$$" -fuzztime=$(FUZZTIME) -parallel=2 $$(dirname $$f) || exit 1; \
		done; \
	done

# golangci-lint v2.15.0 (설정 agent/.golangci.yml). go.mod에 넣지 않고 go run으로 실행한다.
# 두 번째 실행은 린터를 이 컴퓨터용으로 빌드하고 분석만 GOOS=windows로 한다 (*_windows.go 포함).
agent-lint:
	cd agent && go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.15.0 run ./... && \
		go run -exec "env GOOS=windows" github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.15.0 run ./...

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
