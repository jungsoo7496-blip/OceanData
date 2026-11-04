# 01 기동·중지

**판단 기준:** 정기 점검·배포·장애 복구로 전체 또는 일부 서비스를 내리고 올려야 할 때.

**자동 대응:** `make up`은 헬스체크(`/healthz`, `/readyz`)를 통과하지 못한 서비스 이름을 출력하고 실패한다 (01번 요구사항 1.3).
워커는 SIGTERM을 받으면 지금 작업을 끝내고 멈추며, 임대가 끝난 작업은 다른 워커가 다시 가져간다 (05번 임대 회수).

**사람이 할 것**
1. 중지 전 열린 장애 확인: 운영 콘솔 → 수집 모니터링 → 열린 장애.
2. 중지: `make down` (볼륨 유지). 일부만: `$COMPOSE stop worker`.
3. 기동: `make up` → 실패 서비스가 있으면 `make logs`로 원인을 보고 해당 Runbook으로.
4. 마이그레이션 확인: `$COMPOSE run --rm migrate alembic current` = 최신 리비전.

**완료 확인:** `curl -fsS localhost:8000/readyz` 200, 웹 로그인, 워커 메트릭 `watch_evaluations_total` 증가(1분 안), 감사 로그 체인 `make verify-audit` 정상.
