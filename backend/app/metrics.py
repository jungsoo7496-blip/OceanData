"""Prometheus 메트릭 (01번 요구사항 8). 경로는 라우트 템플릿으로 묶어 카디널리티를 막는다."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter("http_requests_total", "HTTP 요청 수", ["method", "route", "status"], registry=REGISTRY)
HTTP_ERRORS = Counter("http_errors_total", "HTTP 오류 응답 수 (4xx·5xx)", ["method", "route", "code_class"],
                      registry=REGISTRY)
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP 처리 시간", ["method", "route"],
                         registry=REGISTRY)

WORKER_REGISTRY = CollectorRegistry(auto_describe=True)
JOBS_QUEUED = Gauge("worker_jobs_queued", "작업 유형별 대기 수", ["job_type"], registry=WORKER_REGISTRY)
JOB_DURATION = Histogram("worker_job_duration_seconds", "작업 처리 시간", ["job_type"], registry=WORKER_REGISTRY)
JOB_FAILURES = Counter("worker_job_failures_total", "작업 실패 수", ["job_type", "error_code"],
                       registry=WORKER_REGISTRY)
JOBS_DONE = Counter("worker_jobs_completed_total", "작업 완료 수", ["job_type"], registry=WORKER_REGISTRY)
