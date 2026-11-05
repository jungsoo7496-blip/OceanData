"""Worker — 작업 큐를 처리한다.

- 작업 유형은 `register()`로 등록한다. 다른 스펙(07 센서 파싱, 14 비식별 등)도 같은 방식으로 더한다 (05번 요구사항 2.4)
- 실패는 지수 백오프로 최대 시도(job_policies.max_attempts)까지 재시도하고, 다 쓰면 FAILED (요구사항 7.1)
- 주기 정리: 임대 만료 작업 회수(1.3), 만료 업로드 세션 정리(04번 3.3), 무결성 로그 일 단위 앵커(5.2)
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prometheus_client import start_http_server

from app.config import get_settings
from app.container import Container, build_container
from app.logging_setup import configure_logging
from app.metrics import JOB_DURATION, JOB_FAILURES, JOBS_DONE, JOBS_QUEUED, WORKER_REGISTRY
from app.ports.queue import Job

log = logging.getLogger("worker")


class RetryableError(Exception):
    def __init__(self, code: str, retry_in_s: float | None = None) -> None:
        super().__init__(code)
        self.code, self.retry_in_s = code, retry_in_s


@dataclass(frozen=True)
class Handler:
    fn: Callable[[Container, Job], Any]
    # 재시도를 다 썼을 때 호출 (자산 FAILED 처리 등)
    on_exhausted: Callable[[Container, Job, str], None] | None = None


HANDLERS: dict[str, Handler] = {}


def register(job_type: str, fn: Callable[[Container, Job], Any],
             on_exhausted: Callable[[Container, Job, str], None] | None = None) -> None:
    HANDLERS[job_type] = Handler(fn, on_exhausted)


def backoff_s(attempt: int, base: float = 15.0, cap: float = 3600.0) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))


# ---- 기본 작업 유형
def _integrity_verify(c: Container, job: Job) -> None:
    from app.services.ingest import IntegrityVerifier

    IntegrityVerifier(c).verify(job.payload["asset_id"])


def _pipeline_step(name: str) -> Callable[[Container, Job], Any]:
    def run(c: Container, job: Job) -> Any:
        from app.services import pipeline

        fn = {"media_probe": lambda: pipeline.media_probe(c, job.payload["asset_id"]),
              "sensor_parse": lambda: pipeline.sensor_parse(c, job.payload["asset_id"]),
              "make_proxy": lambda: pipeline.make_proxy(c, job.payload["asset_id"], job.job_id),
              "make_thumbnail": lambda: pipeline.make_thumbnail(c, job.payload["asset_id"], job.job_id)}[name]
        result = fn()
        if result == "FAILED":
            raise RetryableError(f"{name.upper()}_FAILED")
        if result == "OK":
            pipeline.next_step(c, job.payload, job.organization_id)
        return result

    return run


def _asset_failed(c: Container, job: Job, code: str) -> None:
    from app.services.pipeline import mark_failed

    if job.payload.get("asset_id"):
        mark_failed(c, job.payload["asset_id"], code)


register("noop", lambda c, job: None)
register("integrity_verify", _integrity_verify, _asset_failed)  # 04번
for _step in ("media_probe", "make_proxy", "make_thumbnail"):  # 05번
    register(_step, _pipeline_step(_step), _asset_failed)
register("sensor_parse", _pipeline_step("sensor_parse"))  # 07번 (실패해도 원본 자산 상태는 그대로)


def _deidentify(c: Container, job: Job) -> Any:
    from app.services import deid

    result = deid.deidentify(c, job.payload["asset_id"], job.job_id, job.payload)
    if result == "FAILED":
        raise RetryableError("DEID_FAILED")
    return result


register("deidentify", _deidentify)  # 14번 (공유 심사 PROCESSING 원본만, 13번이 등록)


def _tag_scenes(c: Container, job: Job) -> Any:
    from app.services import pipeline, tagging

    result = tagging.tag_scenes(c, job.payload["asset_id"])
    if result == "OK":
        pipeline.next_step(c, job.payload, job.organization_id)
    return result


register("tag_scenes", _tag_scenes)


def _collect_source(c: Container, job: Job) -> Any:
    from app.domain.normalize import parse_rfc3339
    from app.services import external

    out = external.collect(c, job.payload["source_id"], parse_rfc3339(job.payload["since"]),
                           parse_rfc3339(job.payload["until"]))
    return out["status"]


def _env_join(c: Container, job: Job) -> Any:
    from app.db.models import Asset
    from app.services import external

    with c.sessionmaker.begin() as db:
        a = db.get(Asset, job.payload["asset_id"], with_for_update=True)
        if a is None:
            return "SKIPPED"
        env = external.join_env(c, db, a)
        if env is not None:
            a.env_context = env
            from app.services.tagging import apply_weather

            apply_weather(db, c.settings, a, c.clock.now())  # 날씨 태그 관측값 보정 (14번 요구사항 6.3)
    return "OK" if env is not None and env["observations"] else "NO_MATCH"


def _backup(c: Container, job: Job) -> Any:
    from app.services import backup

    try:
        rid = backup.run(c)
    except backup.BackupBusy as exc:
        return {"status": "SKIPPED", "running": str(exc)}
    return {"backup_run_id": rid}


register("backup", _backup)  # 08번 계층형 백업 (ADR-0021)
register("collect_source", _collect_source)  # 12번 (실패는 큐의 지수 백오프)
register("env_join", _env_join)  # 12번  # 14번 (PROCESSING_STEPS에 TAG가 있을 때 영상 처리 뒤)


def run_once(c: Container, worker_id: str) -> bool:
    classes = tuple(c.settings.worker_resource_classes)
    job = c.queue.claim(list(HANDLERS), worker_id, classes)
    if job is None:
        return False
    handler = HANDLERS[job.job_type]
    start = time.perf_counter()
    try:
        handler.fn(c, job)
    except Exception as exc:  # noqa: BLE001 - 모든 실패는 재시도 대상, 최대 시도 후 FAILED
        code = exc.code if isinstance(exc, RetryableError) else "INTERNAL"
        delay = exc.retry_in_s if isinstance(exc, RetryableError) and exc.retry_in_s else backoff_s(job.attempts)
        JOB_FAILURES.labels(job.job_type, code).inc()
        retried = c.queue.fail(job.job_id, code, delay)
        if isinstance(exc, RetryableError):
            log.warning("job_retry" if retried else "job_failed", extra={"job_id": job.job_id,
                                                                         "job_type": job.job_type, "error_code": code})
        else:
            log.error("job_retry" if retried else "job_failed", exc_info=exc,
                      extra={"job_id": job.job_id, "job_type": job.job_type})
        if not retried and handler.on_exhausted is not None:
            handler.on_exhausted(c, job, code)
    else:
        c.queue.complete(job.job_id)
        JOBS_DONE.labels(job.job_type).inc()
    finally:
        JOB_DURATION.labels(job.job_type).observe(time.perf_counter() - start)
    return True


def sweep(c: Container) -> int:
    """주기 정리: 만료 업로드 세션. 여러 워커가 돌아도 SKIP LOCKED로 겹치지 않는다."""
    from app.services.ingest import IngestService

    with c.sessionmaker.begin() as db:
        n = IngestService(c, db).sweep_expired()
    if n:
        log.info("expired_uploads_swept", extra={"count": n})
    return n


def _dispatch_webhooks(c: Container) -> Any:
    from app.services import webhooks

    return webhooks.deliver_due(c)


def maintenance(c: Container) -> None:
    from app.services import retention
    from app.services.integrity import anchor_previous_days

    reaped = c.queue.reap_expired_leases()
    if reaped:
        log.warning("job_leases_reaped", extra={"count": reaped})
    anchor_previous_days(c)
    # 하루 한 번: 보존 만료 예고·dry-run(또는 자동 실행)·사용량 대조 (08번)
    retention.run_daily(c, "retention_notify", lambda db: {"orgs": retention.notify_expiring(c, db, c.clock.now())})
    retention.run_daily(c, "retention_run", lambda db: (
        {"executed": [x.certificate_id for x in retention.execute_expired(c, db, c.clock.now())]}
        if c.settings.retention_auto_execute else retention.dry_run(db, c.clock.now())))
    retention.run_daily(c, "quota_reconcile", lambda db: {"diffs": retention.reconcile(c, db)})
    # 공공 Open API 주기 수집·보충 (12번 요구사항 2)
    from app.services import external

    with c.sessionmaker.begin() as db:
        external.schedule(c, db)
    # 이용 신청 응답 기한·권한 만료 (13번 요구사항 8.3, 10.1)
    from app.services import datasets

    with c.sessionmaker.begin() as db:
        changed = datasets.expire(c, db)
        if any(changed.values()):
            log.info("grants_expired", extra=changed)
    # 긴급 열람 만료 회수 (02번 요구사항 8.4). 접근 판단은 만료 시각으로 바로 막고, 여기서는 상태·감사를 맞춘다
    from app.services import break_glass

    with c.sessionmaker.begin() as db:
        ended = break_glass.expire(db, c.clock.now())
    for g in ended:
        c.audit.record("BREAK_GLASS_EXPIRED", actor_kind="SYSTEM", actor_id="worker", organization_id=g.organization_id,
                       target_type="BreakGlassGrant", target_id=g.grant_id)
    # 하루 한 번: 기관별 사용량 스냅숏 → 용량 예측·리드타임 알림 (08번 요구사항 8·10)
    from app.services import capacity

    today = c.clock.now().date()
    retention.run_daily(c, "usage_snapshot", lambda db: {"orgs": capacity.snapshot(c, db, today)})
    retention.run_daily(c, "capacity_forecast", lambda db: capacity.notify_if_near(c, db, today))
    # 정기 백업 (08번 요구사항 6.1): BACKUP_ENABLED일 때 BACKUP_INTERVAL_H마다
    from app.services import backup

    with c.sessionmaker.begin() as db:
        if backup.due(c, db):
            c.queue.enqueue("backup", {"requested_by": "schedule"}, dedupe_key="backup")
    # 하루 한 번: 기한 지난 정기 점검 알림 (11번 요구사항 6.3)
    from app.services import inspections

    retention.run_daily(c, "inspection_overdue",
                        lambda db: {"overdue": inspections.notify_overdue(c, db, c.clock.now())})


def watch(c: Container) -> Any:
    """감시 규칙 평가 (11번)와 근거 손상 감지 (10번). 워커가 여럿이어도 한 번에 하나만 한다."""
    from sqlalchemy import text

    from app.metrics import INCIDENTS_OPENED, INGEST_LAG, WATCH_ACTIVE, WATCH_EVALS
    from app.services import evidence, monitoring

    with c.sessionmaker.begin() as db:
        if not db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('watch_rules'))")).scalar():
            return None
        res = monitoring.evaluate(c, db)
    with c.sessionmaker.begin() as db:  # 10번 근거 손상 감지 (Evidence_Integrity_Checker)
        if db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('evidence_check'))")).scalar():
            flagged = evidence.check_damage(c, db)
            if flagged:
                log.warning("evidence_damaged", extra={"documents": len(flagged)})
    WATCH_EVALS.inc()
    for code in monitoring.RULES:
        WATCH_ACTIVE.labels(code).set(res.active_by_rule.get(code, 0))
    for code in res.opened_rules:
        INCIDENTS_OPENED.labels(code).inc()
    INGEST_LAG.clear()
    for h in res.health:
        if h["lag_s"] is not None:
            INGEST_LAG.labels(h["organization_id"], h["device_id"], h["kind"]).set(h["lag_s"])
    if res.opened or res.resolved:
        log.info("watch_evaluated", extra={"opened": len(res.opened), "resolved": len(res.resolved),
                                           "reminded": len(res.reminded)})
    return res


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
    log.info("worker_started", extra={"worker_id": worker_id, "job_types": sorted(HANDLERS),
                                      "resource_classes": settings.worker_resource_classes})
    last_depth = 0.0
    periodic: list[tuple[float, Callable[[], Any], str]] = [
        (300, lambda: sweep(c), "sweep"),
        (settings.job_lease_reap_interval_s, lambda: maintenance(c), "maintenance"),
        (settings.watch_eval_interval_s, lambda: watch(c), "watch"),  # 11번 감시 규칙
        (settings.webhook_dispatch_interval_s, lambda: _dispatch_webhooks(c), "webhooks"),  # 04번 웹훅
    ]
    last_run = {name: 0.0 for _, _, name in periodic}
    while not stopping["flag"]:
        now = time.monotonic()
        for interval, fn, name in periodic:
            if now - last_run[name] > interval:
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - 정리 실패가 작업 처리를 막지 않게
                    log.error(f"{name}_failed", exc_info=exc)
                last_run[name] = now
        if now - last_depth > 10:
            for jt, n in c.queue.depth_by_type().items():
                JOBS_QUEUED.labels(jt).set(n)
            last_depth = now
        if not run_once(c, worker_id):
            time.sleep(settings.worker_poll_interval_s)
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
