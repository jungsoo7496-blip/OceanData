# ADR-0006: 계약을 먼저 고치고 구현한다

- 상태: accepted
- 날짜: 2026-11-04
- 관련: contracts/, structure.md 5장

## 맥락
12월 G1에 메타데이터·API v1.0을 동결해야 한다. 구현에서 계약을 뽑으면 구현이 계약이 된다.

## 결정
contracts/(OpenAPI 3.1, JSON Schema 2020-12, 코드표, 예시)를 기준으로 둔다. API·스키마 변경은 contracts 수정 → make contracts-check → 구현 → 계약 시험 순서다. 타입은 계약에서 생성한다.

## 결과
기업 연계 문서가 구현보다 먼저 나오고 동결 회의 자료가 된다. domain.md와 스키마 코드값이 어긋나면 검사가 실패한다.
