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
