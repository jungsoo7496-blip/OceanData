# 설계: 01 개발 기반

requirements.md를 기준으로 한 구현 설계다. 요구사항은 다시 쓰지 않는다.

## 구성
| 영역 | 구현 | 위치 |
|---|---|---|
| 개발 환경 | Compose: postgres(PostGIS+pgvector), seaweedfs(S3), keycloak, migrate(1회), api, worker, web (+ops 프로필: prometheus·grafana·mailpit) | `deploy/compose/` |
| 기동 검사 | `docker compose up --wait`, 실패 시 서비스 이름 출력 | `tools/compose_up.sh` |
| 버킷 | migrate 서비스가 `python -m app.cli init-buckets` 실행 (이름은 BUCKET_* 설정) | `app/cli.py` |
| 설정 | pydantic-settings. 누락·형식 오류는 키 이름만 출력하고 종료 코드 2 | `app/config.py` |
| 로그 | JSON(timestamp·level·request_id·organization_id·event). 민감 키 마스킹, URL 쿼리 제거, Bearer 제거 | `app/logging_setup.py` |
| 요청 ID·메트릭 | 순수 ASGI 미들웨어. 라우트 템플릿 단위 메트릭 | `app/middleware.py`, `app/metrics.py` |
| 오류 | RFC 9457. 처리 안 된 예외는 500 INTERNAL(내부 정보는 로그만), 검증 오류는 JSON Pointer | `app/errors.py` |
| DB | SQLAlchemy 2 + pg8000(BSD, ADR-0012), Alembic. 소유자 계정으로 마이그레이션, 앱 계정 분리 | `app/db/` |
| 포트 | Storage·Queue·Inference·Identity·Notifier·ExternalSource·ReportRenderer·Clock | `app/ports/` |
| 어댑터 | S3Storage, PgQueue(SKIP LOCKED), KeycloakIdentity(JWKS 캐시), Log/SmtpNotifier, SystemClock/FixedClock | `app/adapters/` |
| 워커 | 큐 폴링, 유형별 대기 수·처리 시간·실패 수 메트릭(:9101) | `app/workers/main.py` |
| 시험 데이터 | 고정 ID upsert (반복 실행 안전), Keycloak realm 사용자와 sub 일치 | `app/seed.py`, `deploy/compose/keycloak/` |
| 합성 영상 | ffmpeg testsrc2, 촬영시각 있음/없음, LARGE_GB 대용량 | `tools/sample_video.sh` |
| CI | 계약·추적, ruff·mypy·bandit, 마이그레이션 up/down/up, pytest, pip-audit, 라이선스, 비밀값(gitleaks), 웹 eslint·tsc·npm audit, (06 이후) Go | `.github/workflows/ci.yml` |

## 데이터 모델 (0001)
organizations, users, projects, project_assignments, devices, sessions(실증회차), session_devices, jobs, enrollment_codes, device_credentials, api_keys, audit_log, break_glass_grants, idempotency_keys

## 테스트 전략
- 단위: 설정·로그 마스킹·ID·권한 표·domain import 규칙 (`backend/tests/unit`)
- 통합: 헬스·준비 상태·오류 형식·메트릭·기능 상태 (`backend/tests/integration/test_system.py`) — 시험마다 임시 DB 생성 후 삭제
- 계약: 응답을 `contracts/openapi.yaml` 스키마로 검증 (`backend/tests/contract`)
