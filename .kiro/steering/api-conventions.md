---
inclusion: fileMatch
fileMatchPattern: ["backend/app/api/**", "contracts/**", "web/src/api/**", "agent/internal/client/**"]
---

# API 규약

## 계약
- 기본 경로 `/v1`. 기준 문서는 `contracts/openapi.yaml`(OpenAPI 3.1)과 `contracts/schemas/`(JSON Schema 2020-12)다.
- 계약을 먼저 고치고 구현한다. 구현이 계약과 다르면 구현을 고친다.
- JSON 필드는 snake_case, 시각은 RFC 3339 UTC(`Z`). 단, 관측값의 원천 시각처럼 domain.md가 오프셋을 요구하는 입력은 예외다.
- 하위호환: 필드 추가는 허용. 필드 삭제·의미 변경·enum 값 삭제는 `/v2`로.
- 모든 응답에 `X-Request-Id` 헤더를 넣는다 (요청에 있으면 그대로, 없으면 새 ULID).

## 오류 형식 — RFC 9457 (`application/problem+json`)
```json
{
  "type": "/problems/rule-violation",
  "title": "공유 조건을 충족하지 않습니다",
  "status": 422,
  "detail": "SANDBOX_PARTNERS로 올리려면 아래 조건이 필요합니다.",
  "instance": "/v1/assets/ast_.../share-requests",
  "code": "RULE_VIOLATION",
  "request_id": "01J...",
  "errors": [],
  "unmet_conditions": [{"condition": "review_result=PASSED", "actual": "null"}]
}
```
- 프로그램 분기는 `code`로 한다. `type`은 사람이 읽는 설명 문서 경로다.
- `title`·`detail`은 한국어. 내부 경로·SQL·스택·다른 기관 식별자를 넣지 않는다.

| code | HTTP | 용도 |
|---|---|---|
| VALIDATION_ERROR | 400 | 입력 형식·필수값 오류 (`errors`에 JSON Pointer별 사유) |
| UNAUTHENTICATED | 401 | 인증 실패·만료·폐기된 자격증명 |
| FORBIDDEN | 403 | 같은 기관 안에서 권한 부족 |
| MFA_REQUIRED | 403 | 다중 인증 없이 고위험 동작 요청 |
| NOT_FOUND | 404 | 없음 또는 다른 기관 리소스 (구분하지 않는다) |
| CONFLICT | 409 | 그 밖의 상태 충돌 |
| IDEMPOTENCY_CONFLICT | 409 | 같은 Idempotency-Key, 다른 본문 |
| INVALID_STATE_TRANSITION | 409 | domain.md 5장에서 허용하지 않는 전이 |
| QUOTA_EXCEEDED | 409 | 저장 쿼터 초과 |
| GONE | 410 | 만료된 업로드 세션·등록 코드·이용 권한 |
| PAYLOAD_TOO_LARGE | 413 | 요청 본문·배치 크기 한도 초과 |
| RULE_VIOLATION | 422 | 수집·공유·이용 정책 조건 미충족 (`unmet_conditions`) |
| UNSUPPORTED_SCHEMA_VERSION | 422 | 다른 주버전, 또는 서버가 모르는 더 높은 부버전 (스키마 검증 전에 먼저 확인) |
| RATE_LIMITED | 429 | 호출 제한 (`Retry-After` 헤더) |
| INTERNAL | 500 | 서버 오류 |
| STORAGE_UNAVAILABLE | 503 | 스토리지 장애로 업로드 세션을 열 수 없음 (`Retry-After`) |
| SERVICE_UNAVAILABLE | 503 | 점검·의존 시스템 장애 |

## 요청·응답
- 목록: 커서 기반 페이지네이션 `?limit=&cursor=`, 응답 `{"items": [...], "next_cursor": "..."}`. limit 기본 50, 최대 500.
- 목록·검색 응답에 전체 건수를 기본으로 넣지 않는다. 넣는다면 권한 범위 필터를 적용한 뒤 센다 (자동완성·집계도 같다).
- 쓰기 요청은 `Idempotency-Key` 헤더를 받는다. 같은 키·같은 본문은 처음 응답을 다시 주고 `Idempotent-Replayed: true`를 붙인다. 같은 키·다른 본문은 409 IDEMPOTENCY_CONFLICT.
- 배치 요청(관측값 등): 본문 전체가 형식 오류면 400. 형식이 맞으면 200과 항목별 결과(`ACCEPTED` / `DUPLICATE` / `REJECTED` + 사유 코드)를 돌려준다.
- 오래 걸리는 작업은 `202 Accepted` + 상태 조회 경로를 돌려준다.
- 대용량 바이너리는 API 서버를 통과시키지 않는다. 사전 서명 URL로 스토리지에 직접 보낸다.

## 인증
- 사용자: Keycloak OIDC 액세스 토큰 (Bearer).
- 기기·기관 서버: 기기 자격증명 또는 API 키를 `POST /v1/auth/token`에서 단기 액세스 토큰(기본 5분, 설정값)으로 바꿔 쓴다. 장기 자격증명을 매 요청에 보내지 않는다.

## 웹훅
- 본문 HMAC-SHA256 서명(`X-Signature`), 타임스탬프 헤더(`X-Signature-Timestamp`), 이벤트 ID(중복 제거용), 지수 백오프 재시도.
- 이벤트 형식은 `contracts/openapi.yaml`의 `webhooks` 절이 기준이다.
