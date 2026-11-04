# ADR-0005: 오류 응답은 RFC 9457 형식으로 한다

- 상태: accepted
- 날짜: 2026-11-04
- 관련: api-conventions.md, contracts/schemas/problem.schema.json

## 맥락
v0.1은 자체 오류 형식이었다. GPT v0.05는 RFC 9457(problem details)을 썼다.

## 결정
application/problem+json에 code·request_id·errors·unmet_conditions 확장 필드를 더해 쓴다. 프로그램 분기는 code로 한다. 코드 목록은 v0.1 목록에 IDEMPOTENCY_CONFLICT, INVALID_STATE_TRANSITION, MFA_REQUIRED, PAYLOAD_TOO_LARGE, STORAGE_UNAVAILABLE, SERVICE_UNAVAILABLE를 더한 것이다.

## 결과
표준 형식이라 외부 개발자와 도구(계약 시험 등)가 바로 이해한다. 코드 목록은 api-conventions.md와 common.schema.json에 함께 있고 검증 스크립트가 일치를 확인한다.
