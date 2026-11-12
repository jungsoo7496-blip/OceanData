"""정기 점검 기록 (11번 요구사항 6): 자동 확인 항목 채우기, 기한 지난 점검 알림."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    BackupRun,
    DailyTask,
    HardwareSignalState,
    Incident,
    Inspection,
    IntegrityLogEntry,
    JobRow,
    OrgQuota,
    Upload,
    User,
)
from app.domain import inspections as tpl
from app.ports.notifier import Notification
from app.services import monitoring
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

INGEST_RULES = ("HEARTBEAT_MISSING", "INGEST_LAG", "SESSION_NO_DATA", "OBSERVATION_LAG", "UPLOAD_FAILURE_RATE")


def _open_incidents(db: Session, rules: tuple[str, ...]) -> int:
    return int(db.execute(select(func.count()).select_from(Incident).where(
        Incident.rule_code.in_(rules), Incident.status != "RESOLVED")).scalar_one())


def _hardware(db: Session, kinds: tuple[str, ...] | None = None) -> tuple[str, dict[str, Any]]:
    q = select(HardwareSignalState)
    if kinds:
        q = q.where(HardwareSignalState.kind.in_(kinds))
    rows = list(db.execute(q).scalars())
    if not rows:
        return "AUTO_UNAVAILABLE", {"reason": "하드웨어 신호가 아직 연결되지 않았습니다"}
    bad = [f"{r.host}/{r.kind}/{r.component}".rstrip("/") for r in rows if r.status != "OK"]
    return ("FAIL" if bad else "PASS"), {"signals": len(rows), "not_ok": bad[:20]}


def _backup_check(c: Container, db: Session, now: datetime) -> tuple[str, dict[str, Any]]:
    """D_BACKUP: 최근 백업 회차 (08번 요구사항 6.3, ADR-0021)."""
    last = db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(1)).scalar_one_or_none()
    if last is None:
        if not c.settings.backup_enabled:
            return "AUTO_UNAVAILABLE", {"reason": "정기 백업이 꺼져 있습니다 (BACKUP_ENABLED=false)"}
        return "FAIL", {"reason": "백업 기록이 없습니다"}
    late = now - last.started_at > timedelta(hours=2 * c.settings.backup_interval_h)
    rt = last.restore_test or {}
    detail = {"backup_run_id": last.backup_run_id, "status": last.status, "started_at": iso(last.started_at),
              "object_count": last.object_count, "failed_objects": len(last.failed_objects or []),
              "restore_test_ok": rt.get("ok"), "restore_tested_at": rt.get("tested_at")}
    return ("PASS" if last.status == "SUCCEEDED" and not late else "FAIL"), detail


def auto_fill(c: Container, db: Session, cycle: str, now: datetime) -> dict[str, tuple[str, dict[str, Any]]]:
    """자동 확인 가능한 항목의 결과 (요구사항 6.4)."""
    since = now - timedelta(hours=24)
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    codes = {t.item_code for t in tpl.items_for(cycle) if t.auto}
    if "D_INGEST" in codes:
        registered = db.execute(select(func.count()).select_from(IntegrityLogEntry)
                                .where(IntegrityLogEntry.registered_at >= since)).scalar_one()
        failed = db.execute(select(func.count()).select_from(Upload).where(
            Upload.updated_at >= since, Upload.error_code.is_not(None))).scalar_one()
        n_open = _open_incidents(db, INGEST_RULES)
        out["D_INGEST"] = ("FAIL" if n_open else "PASS",
                           {"registered_24h": int(registered), "failed_uploads_24h": int(failed),
                            "open_ingest_incidents": n_open})
    if "D_QUARANTINE" in codes:
        by_reason = {r: int(n) for r, n in db.execute(select(Asset.quarantine_reason, func.count()).where(
            Asset.ingest_status == "QUARANTINED", Asset.ingested_at >= since).group_by(Asset.quarantine_reason))}
        surge = _open_incidents(db, ("QUARANTINE_SURGE",))
        out["D_QUARANTINE"] = ("FAIL" if surge else "PASS", {"quarantined_24h": by_reason, "surge_incidents": surge})
    if "D_STORAGE" in codes:
        usage = []
        for q in db.execute(select(OrgQuota)).scalars():
            if q.quota_bytes:
                pct = 100 * (q.used_bytes + q.quarantine_bytes + q.reserved_bytes + q.backup_bytes) / q.quota_bytes
                usage.append((q.organization_id, round(pct, 1)))
        top = max((p for _, p in usage), default=0.0)
        out["D_STORAGE"] = ("FAIL" if top >= 90 else "PASS",
                            {"max_usage_pct": top, "orgs_over_80": [o for o, p in usage if p >= 80]})
    if "D_BACKUP" in codes:
        out["D_BACKUP"] = _backup_check(c, db, now)
    if "D_HARDWARE" in codes:
        out["D_HARDWARE"] = _hardware(db)
    if "W_SMART" in codes:
        out["W_SMART"] = _hardware(db, ("SMART", "DISK"))
    if "W_GPU_BACKLOG" in codes:
        wait = int(monitoring.rule_params(db, "PROCESSING_BACKLOG").get("max_wait_s", 3600))
        n, oldest = db.execute(select(func.count(), func.min(JobRow.run_after)).where(
            JobRow.status == "QUEUED", JobRow.resource_class == "GPU")).one()
        late = oldest is not None and (now - oldest).total_seconds() > wait
        out["W_GPU_BACKLOG"] = ("FAIL" if late else "PASS", {"gpu_queued": int(n), "oldest_run_after": iso(oldest)})
    if "M_HASH_SAMPLE" in codes:
        from app.services.integrity import verify_integrity

        rep = verify_integrity(c)
        out["M_HASH_SAMPLE"] = ("PASS" if rep.ok else "FAIL",
                                {"checked_entries": rep.checked_entries, "sampled": rep.sampled,
                                 "broken_at_seq": rep.broken_at_seq, "mismatched": rep.mismatched_assets[:20],
                                 "missing": rep.missing_objects[:20]})
    if "M_RETENTION" in codes:
        task = db.get(DailyTask, "retention_run")
        if task is None:
            out["M_RETENTION"] = ("FAIL", {"reason": "보존 정책 실행 기록이 없습니다"})
        else:
            fresh = (now.replace(tzinfo=None) - task.last_run_on).days <= 2
            out["M_RETENTION"] = ("PASS" if fresh else "FAIL",
                                  {"last_run_on": task.last_run_on.date().isoformat(), "result": task.result})
    if "M_CAPACITY" in codes:
        ctx = monitoring.EvalCtx(db, now, c.settings)
        found = list(monitoring.EVALUATORS["STORAGE_FORECAST"](ctx, monitoring.rule_params(db, "STORAGE_FORECAST")))
        out["M_CAPACITY"] = ("FAIL" if found else "PASS",
                             {"warnings": [{"organization_id": v.organization_id, **v.detail} for v in found]})
    return out


def create(c: Container, db: Session, *, cycle: str, period_key: str, due_at: datetime, assignee: str | None,
           created_by: str, inspection_id: str) -> Inspection:
    now = c.clock.now()
    auto = auto_fill(c, db, cycle, now)
    items = []
    for t in tpl.items_for(cycle):
        result, detail = auto.get(t.item_code, (None, None))
        items.append({"item_code": t.item_code, "title": t.title, "auto": t.auto, "result": result, "note": None,
                      "auto_detail": detail})
    row = Inspection(inspection_id=inspection_id, cycle=cycle, period_key=period_key, status="IN_PROGRESS",
                     assignee=assignee, due_at=due_at, note=None,
                     items=items, evidence=[], created_by=created_by, created_at=now)
    db.add(row)
    return row


def view(i: Inspection) -> dict[str, Any]:
    return {"inspection_id": i.inspection_id, "cycle": i.cycle, "period_key": i.period_key, "status": i.status,
            "assignee": i.assignee, "due_at": iso(i.due_at), "note": i.note, "items": i.items,
            "evidence": [{k: e[k] for k in ("evidence_id", "item_code", "filename", "byte_size", "checksum_sha256")}
                         for e in i.evidence],
            "created_at": iso(i.created_at), "completed_at": iso(i.completed_at), "completed_by": i.completed_by}


def notify_overdue(c: Container, db: Session, now: datetime) -> list[str]:
    """기한이 지났는데 완료 기록이 없는 바로 앞 기간을 운영자에게 알린다 (요구사항 6.3). 하루 한 번."""
    today = now.astimezone(ZoneInfo(c.settings.ops_timezone)).date()
    overdue = []
    for cycle in tpl.CYCLES:
        key = tpl.previous_period(cycle, today)
        done = db.execute(select(Inspection.status).where(Inspection.cycle == cycle,
                                                          Inspection.period_key == key)).scalar()
        if done != "COMPLETED":
            overdue.append(f"{cycle}:{key}")
    if overdue:
        to = tuple(sorted(r for r in db.execute(select(User.contact_email).where(
            User.status == "ACTIVE", User.contact_email.is_not(None),
            User.roles.any("PLATFORM_OPERATOR"))).scalars() if r))  # type: ignore[arg-type]
        c.notifier.send(Notification(kind="inspection.overdue", organization_id=None,
                                     subject=f"정기 점검 기한 경과 {len(overdue)}건",
                                     body="기한이 지났는데 완료 기록이 없는 점검:\n" + "\n".join(overdue)
                                     + "\n\n운영 콘솔의 정기 점검 화면에서 기록하세요.",
                                     recipients=to, data={"overdue": overdue}))
    return overdue
