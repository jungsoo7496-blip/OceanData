"""Prometheus 메트릭 (01번 요구사항 8). 경로는 라우트 템플릿으로 묶어 카디널리티를 막는다."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter("http_requests_total", "HTTP 요청 수", ["method", "route", "status"], registry=REGISTRY)
HTTP_ERRORS = Counter("http_errors_total", "HTTP 오류 응답 수 (4xx·5xx)", ["method", "route", "code_class"],
                      registry=REGISTRY)
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP 처리 시간", ["method", "route"],
                         registry=REGISTRY)
DB_CONFLICTS = Counter("db_conflict_responses_total", "DB 동시성 오류를 409로 바꿔 돌려준 수", ["sqlstate"],
                       registry=REGISTRY)

WORKER_REGISTRY = CollectorRegistry(auto_describe=True)
JOBS_QUEUED = Gauge("worker_jobs_queued", "작업 유형별 대기 수", ["job_type"], registry=WORKER_REGISTRY)
JOB_DURATION = Histogram("worker_job_duration_seconds", "작업 처리 시간", ["job_type"], registry=WORKER_REGISTRY)
JOB_FAILURES = Counter("worker_job_failures_total", "작업 실패 수", ["job_type", "error_code"],
                       registry=WORKER_REGISTRY)
JOBS_DONE = Counter("worker_jobs_completed_total", "작업 완료 수", ["job_type"], registry=WORKER_REGISTRY)
PIPELINE_FRAMES = Counter("worker_frames_processed_total", "처리한 영상 프레임 수 (초당 처리 프레임 계산용)",
                          ["job_type"], registry=WORKER_REGISTRY)
PIPELINE_SECONDS = Counter("worker_frame_processing_seconds_total", "프레임 처리에 쓴 시간", ["job_type"],
                           registry=WORKER_REGISTRY)

# 수집 모니터링 (11번 요구사항 8.1) — 워커가 규칙을 평가할 때 갱신
INGEST_LAG = Gauge("ingest_lag_seconds", "장비별 수집 지연 (현재 − 마지막 정상 등록·관측)",
                   ["organization_id", "device_id", "kind"], registry=WORKER_REGISTRY)
WATCH_ACTIVE = Gauge("watch_alerts_active", "규칙별 활성 경보 수", ["rule_code"], registry=WORKER_REGISTRY)
WATCH_EVALS = Counter("watch_evaluations_total", "감시 규칙 평가 횟수", registry=WORKER_REGISTRY)
INCIDENTS_OPENED = Counter("incidents_opened_total", "규칙별 새 장애 기록 수", ["rule_code"], registry=WORKER_REGISTRY)

# Hot/Warm 계층 이동 (08번 요구사항 9) — 처리량은 가정하지 않고 재서 조정한다 (HW-8)
TIER_MOVES = Counter("tier_moves_total", "계층 이동 결과 수 (SWITCHED·DONE·FAILED·CANCELLED)", ["to_tier", "result"],
                     registry=WORKER_REGISTRY)
TIER_MOVE_BYTES = Counter("tier_move_bytes_total", "계층 이동으로 복사하고 sha256을 확인한 바이트", ["to_tier"],
                          registry=WORKER_REGISTRY)
