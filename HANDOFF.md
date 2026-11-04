# 인수인계 (클라우드 → 로컬)

이 파일은 클라우드 세션에서 만든 작업을 로컬에서 이어가기 위한 안내다. 작업이 진행될 때마다 갱신한다.
최종 갱신: 2026-11-04 (12번 Open API까지)

## 1. 지금 상태 한눈에
| 스펙 | 상태 | 남은 것 (자세히는 각 tasks.md) |
|---|---|---|
| 기준선 | 완료 | kiro-pack v0.2 + GPT v0.07 오픈소스 검토서(참고) |
| 01 개발 기반 | 거의 완료 | `make up` 실패 경로 자동 시험, golangci-lint(06번과 함께) |
| 02 인증·테넌트 | 핵심 완료 | Keycloak MFA 단계 인증, 긴급 열람(요구사항 8), 구성원 관리(요구사항 9) |
| 03 메타데이터 표준 | 핵심 완료 | 타입 생성, 기관 하위코드 추가 API, 품질 재평가 명령, 약정·연계 카드 등록 API |
| 04 수집 API | 핵심 완료 | 웹훅, 악성코드 검사 어댑터, 예제 클라이언트·Schemathesis, 10GB 시험, 쿼터 동시성 시험 |
| 06 동기화 에이전트 | 핵심 완료 | Windows DPAPI, 자동 업데이트, 순환 삭제(15번과), oapi-codegen 타입 생성. 서비스 설치는 실제 PC에서 확인 필요 |
| 05 처리 파이프라인 | 핵심 완료 | 대용량 임시 디스크 정책, Model_Registry 등록 API(14번과), GPU 인코더 실측 |
| 07 센서·이벤트 | 핵심 완료 | 겹침 자산 조회 API, MQTT 계정 보안 저장, 하위코드 등록 API |
| 08 저장·보존 | 일부 완료 | 보존·법적 보류·삭제 증명·쿼터 알림·사용량·일일 대조 완료. 백업·복원 시험(PoC 후)·Hot/Warm·용량 예측·SSE·월간 리포트 남음 |
| 09 포털 | 핵심 화면 완료 | 온보딩 마법사, 회차 타임라인, 클립 내보내기, 운영 콘솔 편집, 접근성 CI |
| 11 수집 모니터링 | 핵심 완료 | 감시 규칙 13종·장애 기록·메일/웹훅 알림·정기 점검·하드웨어 모의 신호·T07 완료. Open API 실패 신호원(12번), 운영 메신저, 실제 하드웨어 어댑터 남음 |
| 10 증빙·보고서 | 핵심 완료 | 회차 기록 버전·증빙 승인·지표 4종·양식·보고서 초안/승인/제출·PDF·XLSX·HWPX·근거 손상·역추적·T11 완료. 실제 관리기관 양식(T11-f), 정답 데이터로 검증 필요 해제(14번) 남음 |
| 13 공유·데이터셋 | 핵심 완료 | 공유 규칙(속성 기반 T08)·우회 가드·후보·심사·등급 변경·데이터셋 버전·카탈로그·이용 신청·Grant·배포·철회·이용 이력·DCAT·T15 완료. 비식별 작업 처리(14번), 약정 API(03번), 썸네일 샘플 남음 |
| 14 비식별·검수 | 핵심 완료 (기준선) | 대상 제한·가림 렌더링·처리 이력·검수 배정/표본/결정·미탐 회차 재검수·품질 집계·태그·이벤트 규칙·장면 검색·T09 완료. **탐지·임베딩 모델은 모의/기준선** — 모델 선정 후 교체 (ADR-0020) |
| 12 Open API·환경 결합 | 핵심 완료 (모의 어댑터) | 소스 등록·주기 수집·보충·호출 한도·형식 변경 격리·경보·관측소 장비·환경 결합(거리·시각 차이)·재배포 제한·출처 표기·보고서 변수·환경 필터·T17 완료. **실제 기관 API 어댑터는 명세 확인 후** |
| 15~17 | 시작 전 | 다음 추천: 17(수용·릴리스) → 15·16(조건부, 하드웨어·요구 확정 후) |

**시험:** 백엔드 174건(단위·속성 기반·통합·계약·수용 T01·T07·T08·T09·T11·T15·T17) + 에이전트(Go) 8개 패키지 통과 (MQTT 시험은 MQTT_TEST_BROKER 필요) (`cd backend && pytest`).
수집 시험은 moto S3 서버와 실제 SeaweedFS(`TEST_S3_ENDPOINT`) 양쪽에서 통과.
**개발 환경:** `make up` 7개 서비스 healthy. 실제 Keycloak 로그인 → 업로드 생성 → localhost:8333 사전 서명 URL로 파트 전송 → 워커 무결성 검증 → READY_PRIVATE → 영수증까지 확인.

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

- API 문서: http://localhost:8000/docs · 웹 포털: http://localhost:5173 (member-a/dev-password로 로그인) · 포털 E2E: `cd web && node e2e/portal.mjs` · Keycloak: http://localhost:8080 (admin / .env의 KEYCLOAK_ADMIN_PASSWORD)
- 시험 계정: `admin-a`, `member-a`, `admin-b`, `member-b`, `viewer`, `operator`, `operator2`, `reviewer`, `consumer` / 비밀번호 `dev-password` (admin·operator는 첫 로그인 때 OTP 등록)
- 개발 토큰: `deploy/compose/keycloak/README.md`
- 회사·기관망에 SSL 검사 프록시가 있어 이미지 빌드 중 인증서 오류가 나면: 프록시 CA를 `deploy/certs/proxy-ca.crt`로 넣는다 (커밋 안 됨).

## 3. Claude Code로 이어서 하기
로컬 저장소에서 `claude`를 실행하고 이렇게 시작하면 된다.

```
HANDOFF.md, AGENTS.md, .kiro/steering/*.md를 읽고 17-acceptance-release를 이어서 진행해줘.
requirements.md를 기준으로 design.md와 tasks.md를 먼저 쓰고, 계약(contracts/)부터 고친 다음 구현해.
```

Kiro를 쓸 경우: Specs 패널에서 03번을 열고 README의 "Kiro에 줄 지시 예시"를 따른다. 01·02번 design.md·tasks.md는 이미 있다.

## 4. 구조 요약
```
backend/app/
  api/        system.py(헬스·기능), auth.py(토큰·등록·API 키·감사·me), context.py(사업·회차·장비),
              catalog.py(스키마·코드·사전 검증·정책·계보), uploads.py, assets.py, deps.py(인증·권한·범위)
  domain/     ids, principal, permissions(Permission_Table), states(상태 전이 표), metadata(기본값·variant 규칙),
              normalize(촬영시각·위치·단위), quality, errors   ← 프레임워크 import 금지(시험으로 검사)
  services/   auth, tokens, tenant(Tenant_Guard), audit(해시 체인), idempotency, contracts(검증기·번들),
              ingest(업로드·무결성 검증·격리·쿼터·영수증)
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
| C0 세션 취소 시 역전이 | 바이너리 없는 C0 자산에 연 업로드를 취소·만료하면 UPLOADING → READY_PRIVATE로 되돌린다. domain.md 5장 표에 없는 전이 | 04번 tasks "확인 필요" |
| LineageEdge.relation | SAMPLE_FRAME 값이 없어 THUMBNAIL로 기록 중 | 04번 tasks "확인 필요" |
| PSF-2.0 라이선스 예외 | typing_extensions·greenlet(FastAPI·SQLAlchemy 필수 의존성). 승인 전까지 검사는 경고만 | `docs/adr/0011-psf-license-exception.md` |
| Keycloak MFA 방식 | OTP 로그인 시 `acr`/`amr` 클레임이 나오게 단계 인증을 설정할지 | `deploy/compose/keycloak/README.md` |
| 구성원 초대 방식 | 플랫폼이 Keycloak 관리 API로 사용자를 만들지, IdP에서 만들고 연결만 할지 | 02번 tasks 12 |
| 공공 Open API 소스 | 어느 기관의 어떤 API(기상·해양 관측)를 쓸지, 인증·호출 한도·재배포 조건 확인. 확인 전에는 모의 어댑터만 | 12번 design.md |
| 비식별·태깅·임베딩 모델 | 라이선스·학습 데이터 조건을 보고 모델을 골라 Model_Registry에 등록. 지금은 합성 시험용 모의 탐지기라 실제 영상에서 아무것도 가리지 않는다 | `docs/adr/0020-deid-baseline-detector-embedder.md` |
| 보고서 PDF 라이선스 예외 | WeasyPrint가 끌고 오는 Pillow(MIT-CMU)·Pyphen(MPL-1.1 선택) 승인. 거절하면 `REPORT_FORMATS`에서 PDF를 빼고 의존성 제거 | `docs/adr/0018-report-rendering.md` |
| 실증지표·보고서 양식 | 시드의 지표·양식은 합성 예시. 실제 지표 정의(분자·분모)와 관리기관 양식(HWPX)을 받아 운영자가 등록 | 10번 design.md |
| 감시 규칙 기본 임계값 | 하트비트 3배·최소 5분, 지연 3배, 회차 2시간 유예·48시간 창, 현장 디스크 10GiB 등은 초안. 시범 운영 후 조정 (`PATCH /v1/watch-rules`) | 11번 design.md |
| 운영 메신저·알림 주소 | 알림은 메일(SMTP)+일반 웹훅만. 메신저 종류, 사용자 메일 주소를 IdP에서 가져올지 | ADR-0017, 11번 tasks 8 |
| 기존 결정 대기 | 전자정부 프레임워크 요구 여부, 운영 스토리지, 배포 형태 등 | `.kiro/steering/tech.md` 5장 |

## 6. 이번에 내린 설계 결정 (ADR)
- ADR-0011 PSF-2.0 예외 (승인 대기)
- ADR-0012 PostgreSQL 드라이버 pg8000 (psycopg는 LGPL이라 허용 목록 밖)
- ADR-0013 기기·API 키 토큰은 자체 발급 JWT, 폐기는 매 요청 DB 확인으로 즉시 반영
- ADR-0014 작업 큐는 PostgreSQL SKIP LOCKED 자체 구현 (procrastinate는 psycopg LGPL)
- ADR-0015 관측값 매핑 프로필 형식
- ADR-0016 웹 포털 의존성 (Uppy ARIA 알려진 문제)
- ADR-0017 수집 경보 상태·장애 자동 해결·ACK 시 재알림 중지·알림 수신 주소(`users.contact_email`)
- ADR-0020 비식별·태깅·검색 기준선(모의 탐지기·밝기 주야·태그 어휘 임베딩), 모델은 포트 뒤 — 선정 후 교체
- ADR-0019 공유 조건은 순수 함수 하나 + ORM 상향 가드, 데이터셋 항목 표·버전 등급은 현재 항목 등급으로
- ADR-0018 보고서 내보내기: PDF(WeasyPrint)·XLSX(openpyxl)·HWPX(양식 XML 치환), Pillow·Pyphen 예외 승인 대기

## 7. 알아 둘 점
- 감사 로그(audit_log)는 트리거로 수정·삭제가 막혀 있다. 개발 DB를 비우려면 `make down` 후 볼륨 삭제(`docker compose ... down -v`).
- 통합 시험은 `TEST_DATABASE_URL`의 DB 서버에 임시 DB를 만들고 끝나면 지운다. `make up`의 postgres(5432)를 쓰면 된다.
- 공유 시험 흐름(개발): 약정 `python -m app.cli agreement <기관ID> --reference 합성 --purposes AI_TRAINING`, 비식별 작업 대신 `python -m app.cli share-processed <원본ID>`(14번 전까지).
- 알림 메일 확인: `.env`에 `SMTP_HOST=mailpit` 후 `docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile ops up -d mailpit`, 받은 메일은 http://localhost:8025. 워커가 `WATCH_EVAL_INTERVAL_S`마다 감시 규칙을 평가한다. 수동 평가: `python -m app.cli watch`, 모의 하드웨어 신호: `python -m app.cli hardware-signal --status CRITICAL`.
- `.github/workflows/ci.yml`은 GitHub에 push하면 돈다. 클라우드 세션에서는 아직 GitHub Actions 결과를 확인하지 못했다.
