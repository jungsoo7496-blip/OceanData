"""RECEIVED에 멈춘 자산의 무결성 검증 재투입 (운영자 명령 `requeue-verify`, ADR-0071, 04번 요구사항 4.1·4.7·1.5).

기관 정책이 악성코드 검사를 요구하는데 검사기가 없으면 검증 작업은 상태를 바꾸기 전에 멈추고(ScannerUnavailable)
큐가 다시 시도한다. 최대 시도(job_policies, 기본 5회·약 4분)를 넘기면 작업은 FAILED가 되고
`_asset_failed → pipeline.mark_failed`는 `RECEIVED → FAILED`가 domain.md 5장 표에 없어 아무것도 바꾸지 않는다.
자산은 RECEIVED에, 업로드 예약은 그대로 남는다. 처음 작업의 dedupe 키(`integrity_verify:{asset}:{upload}`)는
끝난 뒤에도 키를 쥐어 같은 키로는 새 작업이 생기지 않는다.

여기서는 상태 전이를 더하지 않는다. '대기·실행 중인 검증 작업이 없는 RECEIVED 자산'을 찾아 검증 작업을 새로 넣을 뿐이다.
등록·예약 정산은 워커의 검증(ADR-0058: 이어 하기·한 번만 등록)이 한다. 자산 행은 바꾸지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.models import Asset, JobRow, Organization, Upload
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

log = logging.getLogger(__name__)
VERIFY_JOB = "integrity_verify"
ACTIVE = ("QUEUED", "RUNNING")  # 재시도 대기(실행 시각이 미래인 QUEUED)와 임대 중인 RUNNING도 진행 중으로 본다
AUDIT_ACTION = "INTEGRITY_VERIFY_REQUEUED"


@dataclass
class RequeueReport:
    requeued: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)


def _job_view(j: JobRow | None) -> dict[str, Any] | None:
    if j is None:
        return None
    return {"job_id": j.job_id, "status": j.status, "attempts": j.attempts, "last_error_code": j.last_error_code,
            "updated_at": iso(j.updated_at)}


def _verify_jobs(asset_id: str) -> Any:
    return select(JobRow).where(JobRow.job_type == VERIFY_JOB, JobRow.asset_id == asset_id)


def _active_job(db: Session, asset_id: str) -> JobRow | None:
    return db.scalars(_verify_jobs(asset_id).where(JobRow.status.in_(ACTIVE))
                      .order_by(JobRow.created_at.desc()).limit(1)).first()


def _view(db: Session, a: Asset) -> dict[str, Any]:
    """운영자가 볼 것: 어느 기관의 무엇이 언제부터, 예약이 얼마나 묶였고, 마지막 검증 작업이 어떻게 끝났나."""
    up = db.scalars(select(Upload).where(Upload.asset_id == a.asset_id, Upload.status == "RECEIVED")
                    .order_by(Upload.created_at.desc()).limit(1)).first()
    org = db.get(Organization, a.organization_id)
    last = db.scalars(_verify_jobs(a.asset_id).order_by(JobRow.created_at.desc(), JobRow.job_id.desc())
                      .limit(1)).first()
    return {"asset_id": a.asset_id, "organization_id": a.organization_id, "updated_at": iso(a.updated_at),
            "upload_id": up.upload_id if up else None, "reserved_bytes": up.reserved_bytes if up else 0,
            "malware_scan_required": bool(org is not None and org.malware_scan_required),
            "last_job": _job_view(last)}


def not_stuck(db: Session, a: Asset | None) -> dict[str, Any] | None:
    """멈춘 자산이 아니면 그 까닭. 진행 중인 자산·다른 상태의 자산은 건드리지 않는다."""
    if a is None or a.deleted_at is not None:
        return {"reason": "NOT_FOUND"}
    if a.ingest_status != "RECEIVED":
        return {"reason": "NOT_RECEIVED", "ingest_status": a.ingest_status}
    active = _active_job(db, a.asset_id)
    if active is not None:
        return {"reason": "VERIFY_JOB_ACTIVE", "job": _job_view(active)}
    return None


def find_stuck(db: Session, *, organization_id: str | None = None,
               asset_id: str | None = None) -> list[dict[str, Any]]:
    """대기·실행 중인 검증 작업이 없는 RECEIVED 자산 (작업이 FAILED·DONE이거나 없다). 오래된 것부터."""
    active = exists().where(JobRow.job_type == VERIFY_JOB, JobRow.asset_id == Asset.asset_id,
                            JobRow.status.in_(ACTIVE))
    q = select(Asset).where(Asset.ingest_status == "RECEIVED", Asset.deleted_at.is_(None), ~active)
    if organization_id is not None:
        q = q.where(Asset.organization_id == organization_id)
    if asset_id is not None:
        q = q.where(Asset.asset_id == asset_id)
    return [_view(db, a) for a in db.scalars(q.order_by(Asset.updated_at, Asset.asset_id)).all()]


def _requeue_one(c: Container, asset_id: str, reason: str, report: RequeueReport) -> None:
    with c.sessionmaker.begin() as db:
        # 워커의 검증 첫 트랜잭션(RECEIVED → VALIDATING)과 같은 자산 행 잠금으로 줄 선다. 다른 실행·요청이 잡고 있으면
        # 기다리지 않고 건너뛴다 — 운영자가 다시 돌리면 그때 상태로 판단한다
        a = db.scalars(select(Asset).where(Asset.asset_id == asset_id).with_for_update(skip_locked=True)).one_or_none()
        if a is None:
            seen = db.get(Asset, asset_id)
            report.skipped.append({"asset_id": asset_id, **({"reason": "LOCKED"} if seen is not None
                                                            else {"reason": "NOT_FOUND"})})
            return
        why = not_stuck(db, a)  # 잠근 뒤 다시 본다: 그사이 등록됐거나 다른 실행이 작업을 넣었을 수 있다
        if why is not None:
            report.skipped.append({"asset_id": asset_id, **why})
            return
        view = _view(db, a)
        conn = db.connection()
        # 처음 작업의 키는 끝난 뒤에도 남아 있어 쓰지 않는다. 재투입 키는 대기·실행 중일 때만 막는다(끝나면 키를 놓는다)
        job_id = c.queue.enqueue(VERIFY_JOB, {"asset_id": asset_id, "upload_id": view["upload_id"], "requeued": True},
                                 organization_id=a.organization_id, dedupe_key=f"{VERIFY_JOB}:{asset_id}:requeue",
                                 dedupe_active_only=True, connection=conn)
        c.audit.record(AUDIT_ACTION, actor_kind="SYSTEM", actor_id="cli", organization_id=a.organization_id,
                       target_type="Asset", target_id=asset_id, reason=reason, connection=conn,
                       details={"job_id": job_id, "previous_job": view["last_job"],
                                "reserved_bytes": view["reserved_bytes"]})
    report.requeued.append({"asset_id": asset_id, "job_id": job_id, "previous_job": view["last_job"],
                            "malware_scan_required": view["malware_scan_required"]})


def requeue(c: Container, asset_ids: list[str], *, reason: str) -> RequeueReport:
    """자산마다 자기 트랜잭션에서 잠그고 다시 확인한 뒤 검증 작업과 감사를 함께 확정한다 (ADR-0059와 같은 방식).

    한 자산이 실패해도 기록하고 다음 자산으로 간다. 여러 번·동시에 돌려도 대기·실행 중인 검증 작업은 자산마다 하나다."""
    report = RequeueReport()
    for asset_id in asset_ids:
        try:
            _requeue_one(c, asset_id, reason, report)
        except Exception as exc:  # noqa: BLE001 - 한 자산의 실패가 나머지를 막지 않게 한다
            log.error("verify_requeue_failed", extra={"asset_id": asset_id, "error_type": type(exc).__name__})
            report.failed.append({"asset_id": asset_id, "error_type": type(exc).__name__})
    return report
