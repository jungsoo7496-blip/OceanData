# ADR-0014: 작업 큐는 PostgreSQL 자체 구현 (procrastinate PoC 불채택)

- 상태: 채택
- 날짜: 2026-11-04
- 관련: ADR-0002, docs/OSS-CANDIDATES.md 2장·4장(PoC 1순위), 05번 요구사항 1·6·7

## PoC 결과
- procrastinate 3.10.0을 설치해 보니 `psycopg 3.3.7`·`psycopg-pool`을 필수로 끌어온다. psycopg는 **LGPL-3.0**으로 tech.md 3장 허용 목록 밖이다 (ADR-0012에서 같은 이유로 pg8000을 골랐다).
- 01번에서 만든 `FOR UPDATE SKIP LOCKED` 큐(`app/adapters/pg_queue.py`)가 이미 중복 배정 방지·지연 재시도를 한다.

## 결정
procrastinate를 쓰지 않고 자체 큐를 05번 요구사항에 맞게 보강한다.
- 작업 행에 대상 자산·자원 클래스(CPU/GPU)·임대 만료 시각을 둔다. 임대가 지난 RUNNING 작업은 워커 주기 정리가 QUEUED로 돌린다 (요구사항 1.3).
- 우선순위·자원 클래스·최대 시도·임대 시간·GPU 없을 때 처리(CPU 실행/대기)는 `job_policies` 표에서 읽는다 (HW-3, 요구사항 6.1·6.2).
- 실패는 지수 백오프로 최대 시도까지 재시도하고, 그래도 실패하면 FAILED와 오류 코드를 남긴다 (요구사항 7.1).

## 되돌리는 방법
QueuePort 뒤에 있으므로 다른 큐로 바꿔도 워커 핸들러는 그대로다. psycopg 라이선스 예외가 승인되면 procrastinate를 다시 검토할 수 있다.
