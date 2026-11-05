# 계약 파일 (v1.0 초안)

12월 G1(표준·API 동결) 회의에 올릴 API·데이터 계약 초안이다. domain.md의 값과 규칙을 기계가 읽는 형태로 옮겼다.

| 파일 | 내용 |
|---|---|
| `openapi.yaml` | OpenAPI 3.1. 외부 연계 API 51개 작업 + 웹훅 5종. `x-freeze: G1`은 이번 동결 대상, `G4`·`G5`는 공동활용 단계에서 확정 |
| `schemas/common.schema.json` | ID 형식, 코드값(enum), 시간·위치·출처·확장 필드 공통 정의 |
| `schemas/asset-metadata.schema.json` | 공통 메타데이터 v1.0 자산 레코드 (공유 조건 불변식 포함) |
| `schemas/upload-manifest.schema.json` | 업로드 신고서 (organization_id를 받지 않는다) |
| `schemas/observation-batch.schema.json` | 센서 관측값 배치 (결측 = null + MISSING) |
| `schemas/event.schema.json` | 실증 이벤트 (모델 결과는 human_verified=false로만 생성) |
| `schemas/agent-heartbeat.schema.json`, `device-policy.schema.json` | 수집기(에이전트) 상태 보고·수집기와 원천 장비 정책 |
| `schemas/dataset-version-manifest.schema.json` | 데이터셋 버전 매니페스트 + 데이터 카드 (기준 문서 18.2장) |
| `schemas/access-request.schema.json` | 이용 신청 |
| `schemas/problem.schema.json` | RFC 9457 오류 형식 |
| `schemas/webhook-event.schema.json` | 웹훅 본문 |
| `codes/` | 단위 사전, 관측 항목, 이벤트 상위분류, 이용 목적, 격리 사유 (단위·관측 항목은 초안) |
| `examples/` | 통과해야 하는 예시 17개 |
| `examples/invalid/` | 실패해야 하는 예시 24개 (위경도 뒤바뀜, CORE·원본의 L2 상향, 운영자 승인 전 L2, 결측을 0으로, 기관 ID 직접 입력 등) |

## 검사
```
pip install -r tools/requirements.txt
make contracts-check      # = python tools/validate_contracts.py
```
검사 내용: 스키마 문법, 예시 통과·실패, domain.md·api-conventions.md와 코드값 일치, 코드표 상호 일치, OpenAPI 구조와 `$ref` 대상, openapi-spec-validator 검증.

08번 백업 실행 환경 보완(ADR-0022)은 API·스키마 변경이 없다. `/v1/backup-runs`와 기존 실패 결과 형식을 유지한다.
PostgreSQL 도구 주버전 불일치는 기존 `FAILED`·`error` 또는 복원 시험의 `ok: false`로 보고한다.
구현 전 기존 계약 검사를 통과했고, T10의 백업 조회·요청 응답 계약 검사로 호환성을 확인한다.

17번 후속 보완(ADR-0023)도 API·스키마 변경이 없다. 실제 웹 재개 시험은 기존 업로드 응답의 `part_size`를
그대로 사용하고 완료 파트 목록·완료 요청·영수증 해시를 검증한다. 새 마이그레이션·도메인 코드 변경은 없다.

06번 로컬 보완(ADR-0024·0025)은 기존 `getAssetReceipt` 계약을 실제 호출해 영수증 검증 뒤 완료 처리한다.
C0는 바이너리·무결성 로그가 없는 메타데이터 등록 계약이므로 별도 의미를 유지한다.
DPAPI·업데이트 감독·SQLite 상태 경합 방어·생성 SDK는 API·서버 DB·도메인 코드값을 바꾸지 않는다.
SQLite도 기존 파일/kv 구조를 사용하므로 새 DB 마이그레이션/down은 해당 없음이다.
`make agent-generate`·`make agent-generate-check`로 발신 Go SDK를 생성·검사한다.
생성용 참조 투영은 원본 JSON Schema 조건을 유지하고 네트워크·계약 폴더 밖·동적 참조는 거부한다.

03번 작업 12(ADR-0026)의 `POST /v1/codes/event-categories/subcodes`는 사람 `ORG_ADMIN`이 자기 기관에 하위코드를 등록하는 계약이다.
본문 예시는 `{"code":"VESSEL.small_boat","label":"소형선박"}`이다. 성공 응답은 201이며 해당 기관에서 조회·사용할 수 있다.
기관 범위는 토큰에서 결정한다. 미등록 상위분류는 422, 같은 기관의 중복 코드는 409다.
`Idempotency-Key`와 같은 본문으로 재시도하면 201 응답을 다시 주고 `Idempotent-Replayed: true`를 붙인다.
공통 코드표 값 및 기존 DB 구조는 유지한다.

03번 작업 7의 업로드는 `location`이 없을 때만 `source_location`을 EPSG:4326으로 변환한다.
변환 결과에 기존 위치 형식·범위 규칙을 적용하며 위반하면 400 `VALIDATION_ERROR`를 반환한다.
원본 좌표와 변환 이력을 보존한다. 계약 설명을 갱신했고 기존 데이터 스키마를 사용한다.

## 고치는 순서
1. 값·규칙을 바꾸려면 `.kiro/steering/domain.md`부터 고친다.
2. `schemas/common.schema.json`과 관련 스키마·예시를 맞춘다.
3. `make contracts-check`를 통과시킨다.
4. 동결 후 변경은 schema_version 부버전(필드 추가)이나 주버전(삭제·의미 변경, `/v2`)으로만 한다.

## 동결 회의에서 정할 것
- 회차 표시값(session_label) 규칙과 폴더 이름 규칙
- 관측 항목·단위 사전 확정 (연계 카드 기반)
- 기기 토큰 발급 방식 (`/v1/auth/token`, 보안 설계)
- 해역 코드(area_code) 체계
