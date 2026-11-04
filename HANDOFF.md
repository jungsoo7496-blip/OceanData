# 인수인계 (클라우드 → 로컬)

이 파일은 클라우드 세션에서 만든 작업을 로컬에서 이어가기 위한 안내다. 작업이 진행될 때마다 갱신한다.
최종 갱신: 2026-11-04

## 1. 지금 상태 한눈에
| 스펙 | 상태 | 비고 |
|---|---|---|
| 기준선 | 완료 | kiro-pack v0.2 그대로 + GPT v0.07 오픈소스 검토서(참고) |
| 01 개발 기반 | 거의 완료 | 남은 것: `make up` 실패 경로 자동 시험, golangci-lint(06번과 함께) |
| 02 인증·테넌트 | 핵심 완료 | 남은 것: Keycloak MFA 단계 인증 설정, 긴급 열람(요구사항 8), 구성원 관리(요구사항 9) |
| 03~17 | 시작 전 | 다음은 03 → 04 순서 |

각 스펙의 완료/남은 작업은 `.kiro/specs/NN-*/tasks.md`에 체크박스로 있다.

**시험:** 단위 9 · 통합 14 · 계약 2 · T01 수용시험 15 = 40건 통과 (`cd backend && pytest`)
**개발 환경:** `make up`으로 7개 서비스 전부 healthy 확인, 실제 Keycloak 토큰으로 `/v1/me`·`/v1/sessions` 확인

## 2. 로컬에서 시작하기
필요한 것: Docker(Compose v2), Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22, (06번부터) Go

```bash
git clone https://github.com/jungsoo7496-blip/OceanData.git
cd OceanData
cp .env.example .env          # 값은 개발용 자리표시자. 필요하면 바꾼다
make venv                     # backend/.venv에 개발 의존성 설치
make up                       # 전체 개발 환경 기동 (처음엔 이미지 빌드로 몇 분 걸림)
make seed                     # 시험 기관 5·사용자 9·사업 2·장비 5·회차 2
make test-integration         # 통합 시험 (make up의 PostgreSQL 사용)
make test-acceptance          # T01 수용시험
```

- API 문서: http://localhost:8000/docs · 웹: http://localhost:5173 · Keycloak: http://localhost:8080 (admin / .env의 KEYCLOAK_ADMIN_PASSWORD)
- 시험 계정: `admin-a`, `member-a`, `admin-b`, `member-b`, `viewer`, `operator`, `operator2`, `reviewer`, `consumer` / 비밀번호 `dev-password` (admin·operator는 첫 로그인 때 OTP 등록)
- 개발 토큰: `deploy/compose/keycloak/README.md`
- 회사·기관망에 SSL 검사 프록시가 있어 이미지 빌드 중 인증서 오류가 나면: 프록시 CA를 `deploy/certs/proxy-ca.crt`로 넣는다 (커밋 안 됨).

## 3. Claude Code로 이어서 하기
로컬 저장소에서 `claude`를 실행하고 이렇게 시작하면 된다.

```
HANDOFF.md, AGENTS.md, .kiro/steering/*.md를 읽고 03-metadata-standard를 이어서 진행해줘.
requirements.md를 기준으로 design.md와 tasks.md를 먼저 쓰고, 계약(contracts/)부터 고친 다음 구현해.
```

Kiro를 쓸 경우: Specs 패널에서 03번을 열고 README의 "Kiro에 줄 지시 예시"를 따른다. 01·02번 design.md·tasks.md는 이미 있다.

## 4. 구조 요약
```
backend/app/
  api/        system.py(헬스·기능), auth.py(토큰·등록·API 키·감사·me), context.py(사업·회차·장비), deps.py(인증·권한·범위)
  domain/     ids.py, principal.py, permissions.py(Permission_Table), errors.py   ← 프레임워크 import 금지(시험으로 검사)
  services/   auth.py, tokens.py, tenant.py(Tenant_Guard), audit.py(해시 체인), idempotency.py, contracts.py
  ports/      8개 포트 / adapters/ S3·PgQueue·Keycloak·Notifier·Clock
  db/         models.py, migrations/versions/0001_*.py
  workers/    main.py (HANDLERS에 작업 유형 등록)
backend/tests/ unit · integration · contract(openapi 응답 검증) · acceptance(T01)
deploy/compose/ docker-compose.yml, postgres/(pgvector+PostGIS 이미지), keycloak/realm-marine.json
tools/         validate_contracts.py, check_traceability.py, check_licenses.py, check_secrets.py, compose_up.sh, sample_video.sh
```

새 엔드포인트를 만들 때 지킬 것:
1. `contracts/openapi.yaml`부터 고치고 `make contracts-check`
2. `PERMISSIONS`에 동작 추가 → 라우터에서 `Depends(require("동작"))`
3. 기관 소유 테이블 조회는 `scoped_select` / `load_scoped`만 사용 (직접 `select(Model)` 금지)
4. `backend/tests/acceptance/test_t01_tenant_isolation.py`의 `CROSS_ORG_CASES`에 교차 기관 케이스 추가
5. `backend/tests/contract/`에 응답 계약 검사 추가

## 5. 사람이 결정해야 할 것
| 항목 | 내용 | 위치 |
|---|---|---|
| PSF-2.0 라이선스 예외 | typing_extensions·greenlet(FastAPI·SQLAlchemy 필수 의존성). 승인 전까지 검사는 경고만 | `docs/adr/0011-psf-license-exception.md` |
| Keycloak MFA 방식 | OTP 로그인 시 `acr`/`amr` 클레임이 나오게 단계 인증을 설정할지 | `deploy/compose/keycloak/README.md` |
| 구성원 초대 방식 | 플랫폼이 Keycloak 관리 API로 사용자를 만들지, IdP에서 만들고 연결만 할지 | 02번 tasks 12 |
| 기존 결정 대기 | 전자정부 프레임워크 요구 여부, 운영 스토리지, 배포 형태 등 | `.kiro/steering/tech.md` 5장 |

## 6. 이번에 내린 설계 결정 (ADR)
- ADR-0011 PSF-2.0 예외 (승인 대기)
- ADR-0012 PostgreSQL 드라이버 pg8000 (psycopg는 LGPL이라 허용 목록 밖)
- ADR-0013 기기·API 키 토큰은 자체 발급 JWT, 폐기는 매 요청 DB 확인으로 즉시 반영

## 7. 알아 둘 점
- 감사 로그(audit_log)는 트리거로 수정·삭제가 막혀 있다. 개발 DB를 비우려면 `make down` 후 볼륨 삭제(`docker compose ... down -v`).
- 통합 시험은 `TEST_DATABASE_URL`의 DB 서버에 임시 DB를 만들고 끝나면 지운다. `make up`의 postgres(5432)를 쓰면 된다.
- `.github/workflows/ci.yml`은 GitHub에 push하면 돈다. 클라우드 세션에서는 아직 GitHub Actions 결과를 확인하지 못했다.
