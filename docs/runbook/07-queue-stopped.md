# 07 작업 큐 정지·적체

**판단 기준:** 수집 모니터링 PROCESSING_BACKLOG 경보, 워커 메트릭 `worker_jobs_queued` 증가, `worker_job_failures_total` 급증.

**자동 대응:** 업로드·관측값 수신은 워커와 무관하게 계속된다(T12-c). 임대가 끝난 작업은 회수되고, 실패는 지수 백오프 후 최대 시도에서 FAILED.

**사람이 할 것**
1. 워커 상태: `$COMPOSE ps worker`, `$COMPOSE logs --tail 200 worker`.
2. 멈춤이면 `$COMPOSE restart worker`. 반복되면 실패 작업 유형(`worker_job_failures_total{job_type,error_code}`)을 보고 해당 스펙 문서로.
3. GPU 적체: `GPU_ENABLED`와 `job_policies.gpu_fallback` 확인(05번). 우선순위 조정은 `python -m app.cli set-policy`.
4. 큰 적체가 풀린 뒤 FAILED 자산 재처리(운영자). 검증 작업이 FAILED인데 자산이 `RECEIVED`에 남은 것은 재처리 대상이 아니다 —
   [11-verify-stuck-received.md](11-verify-stuck-received.md)의 `requeue-verify`로 다시 넣는다.
5. 구간 클립(`clip_export`)이 워커를 오래 쥐어 수집 작업이 밀리면 클립 전용 워커를 따로 띄운다 (ADR-0069):
   `.env`에 `WORKER_EXCLUDE_JOB_TYPES=clip_export`(기본 워커가 클립을 뺀다)와 `COMPOSE_PROFILES=clip-worker`(클립만
   처리하는 `worker-clips`를 켠다)를 함께 두고 `$COMPOSE up -d worker worker-clips`(또는 `make up`). 프로필도 `.env`에
   둬야 `make up`·`make down`(재부팅 뒤 기동 포함)에서 두 워커가 함께 오르내린다 — 명령줄 `--profile`로만 켜면 제외 설정만 남아 다음
   `make up`에서 `worker-clips`가 빠지고, 클립이 오류 없이 대기(QUEUED)에 남다가 기관마다 `CLIP_MAX_PENDING_PER_ORG`(기본 3)건이
   차면 새 클립 요청이 429가 된다(`make up`이 경고한다). 되돌릴 때는 두 줄을 함께 지우고 `$COMPOSE up -d worker`로 기본 워커를 다시 띄운 뒤
   `$COMPOSE --profile clip-worker rm -sf worker-clips`로 전용 워커를 멈춘다 — 작업 유형은 가져갈 때 나누므로 대기 중인
   클립은 그대로 처리된다. 클립이 시간 한도로 FAILED(`CLIP_TIMEOUT`)가 잦으면
   `CLIP_TIMEOUT_FACTOR`(기본 4, 구간 1초당 초)를 운영 하드웨어 실측에 맞춘다.

**완료 확인:** `worker_jobs_queued`가 줄고 PROCESSING_BACKLOG 경보 해제, 새 업로드가 READY_PRIVATE까지 진행.
