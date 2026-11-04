# ADR-0002: 작업 큐는 PostgreSQL로 한다

- 상태: accepted
- 날짜: 2026-11-04
- 관련: 05번 요구사항 1, OSS-CANDIDATES

## 맥락
비동기 처리(메타 추출·프록시·비식별 등)가 필요하다. GPT v0.05와 사용자 정리표는 Redis 기반 큐(Celery·RQ·Dramatiq) + outbox를 제안했다.

## 결정
PostgreSQL 기반 큐를 쓴다. 상태 변경과 작업 등록이 같은 DB 트랜잭션이므로 outbox·inbox 테이블과 전달기가 필요 없다. 구현은 procrastinate(MIT) PoC 후 결정하고, 우선순위·임대·재시도 요구를 못 맞추면 FOR UPDATE SKIP LOCKED로 직접 만든다.

## 결과
서버 구성요소가 하나 줄고 Redis 라이선스 변경(7.4 RSALv2/SSPL, 8.0 AGPL 선택지) 검토가 필요 없다. 처리량이 PostgreSQL로 부족하다는 실측이 나오면 QueuePort 어댑터만 바꾼다. procrastinate는 유지보수 인력 모집 공지가 있어 PoC ADR에 대안(자체 구현) 비용을 함께 적는다.
