# 07 작업 큐 정지·적체

**판단 기준:** 수집 모니터링 PROCESSING_BACKLOG 경보, 워커 메트릭 `worker_jobs_queued` 증가, `worker_job_failures_total` 급증.

**자동 대응:** 업로드·관측값 수신은 워커와 무관하게 계속된다(T12-c). 임대가 끝난 작업은 회수되고, 실패는 지수 백오프 후 최대 시도에서 FAILED.

**사람이 할 것**
1. 워커 상태: `$COMPOSE ps worker`, `$COMPOSE logs --tail 200 worker`.
2. 멈춤이면 `$COMPOSE restart worker`. 반복되면 실패 작업 유형(`worker_job_failures_total{job_type,error_code}`)을 보고 해당 스펙 문서로.
3. GPU 적체: `GPU_ENABLED`와 `job_policies.gpu_fallback` 확인(05번). 우선순위 조정은 `python -m app.cli set-policy`.
4. 큰 적체가 풀린 뒤 FAILED 자산 재처리(운영자).

**완료 확인:** `worker_jobs_queued`가 줄고 PROCESSING_BACKLOG 경보 해제, 새 업로드가 READY_PRIVATE까지 진행.
