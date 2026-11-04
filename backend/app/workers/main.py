"""Worker — 작업 큐를 처리한다. 작업 유형별 핸들러는 05번 스펙부터 HANDLERS에 등록한다."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from collections.abc import Callable
from typing import Any

from prometheus_client import start_http_server

from app.config import get_settings
from app.container import Container, build_container
from app.logging_setup import configure_logging
from app.metrics import JOB_DURATION, JOB_FAILURES, JOBS_DONE, JOBS_QUEUED, WORKER_REGISTRY
from app.ports.queue import Job

log = logging.getLogger("worker")


class RetryableError(Exception):
    def __init__(self, code: str, retry_in_s: float = 30.0) -> None:
        super().__init__(code)
        self.code, self.retry_in_s = code, retry_in_s


Handler = Callable[[Container, Job], None]
def _integrity_verify(c: Container, job: Job) -> None:
    from app.services.ingest import IntegrityVerifier

    IntegrityVerifier(c).verify(job.payload["asset_id"])


HANDLERS: dict[str, Handler] = {
    "noop": lambda c, job: None,  # 동작 확인용
    "integrity_verify": _integrity_verify,  # 04번 무결성 검증·격리
    # "make_proxy": 05번 처리 파이프라인에서 등록한다 (그때까지 영상 자산은 REGISTERED에 머문다)
}


def sweep(c: Container) -> int:
    """주기 정리 작업 (만료 업로드 세션). 여러 워커가 돌아도 SKIP LOCKED로 겹치지 않는다."""
    from app.services.ingest import IngestService

    with c.sessionmaker.begin() as db:
        n = IngestService(c, db).sweep_expired()
    if n:
        log.info("expired_uploads_swept", extra={"count": n})
    return n


def run_once(c: Container, worker_id: str) -> bool:
    job = c.queue.claim(list(HANDLERS), worker_id)
    if job is None:
        return False
    start = time.perf_counter()
    try:
        HANDLERS[job.job_type](c, job)
    except RetryableError as exc:
        JOB_FAILURES.labels(job.job_type, exc.code).inc()
        c.queue.fail(job.job_id, exc.code, exc.retry_in_s)
        log.warning("job_retry", extra={"job_id": job.job_id, "job_type": job.job_type, "error_code": exc.code})
    except Exception as exc:  # noqa: BLE001 - 처리되지 않은 예외는 재시도하지 않는 실패로 기록
        JOB_FAILURES.labels(job.job_type, "INTERNAL").inc()
        c.queue.fail(job.job_id, "INTERNAL", None)
        log.error("job_failed", exc_info=exc, extra={"job_id": job.job_id, "job_type": job.job_type})
    else:
        c.queue.complete(job.job_id)
        JOBS_DONE.labels(job.job_type).inc()
    finally:
        JOB_DURATION.labels(job.job_type).observe(time.perf_counter() - start)
    return True


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    c = build_container(settings)
    start_http_server(settings.worker_metrics_port, registry=WORKER_REGISTRY)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = {"flag": False}

    def _stop(*_: Any) -> None:
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker_started", extra={"worker_id": worker_id, "job_types": sorted(HANDLERS)})
    last_depth = last_sweep = 0.0
    while not stopping["flag"]:
        if time.monotonic() - last_sweep > 300:
            try:
                sweep(c)
            except Exception as exc:  # noqa: BLE001 - 정리 실패가 작업 처리를 막지 않게
                log.error("sweep_failed", exc_info=exc)
            last_sweep = time.monotonic()
        if time.monotonic() - last_depth > 10:
            for jt, n in c.queue.depth_by_type().items():
                JOBS_QUEUED.labels(jt).set(n)
            last_depth = time.monotonic()
        if not run_once(c, worker_id):
            time.sleep(settings.worker_poll_interval_s)
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
