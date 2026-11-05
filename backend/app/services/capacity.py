"""용량 예측과 기관별 월간 자원 사용 리포트 (08번 요구사항 8, 10).

- 스냅숏: 워커가 하루 한 번 기관별 사용량(쿼터 장부)·존별 활성 자산 크기·독립 백업 대상 크기를
  `usage_snapshots`에 남긴다.
- 예측: 최근 window_days 스냅숏 합계의 최소제곱 직선으로 설정 사용률(기본 70%) 도달 예상일.
  전체 용량은 하드웨어가 정해져야 알 수 있으므로 설정값(STORAGE_CAPACITY_BYTES)이 없으면 '확인 필요'로 둔다.
  리드타임(기본 180일) 안이면 운영자에게 알린다.
- 월간 리포트: 금액 없이 사용량만. 처리 시간은 작업의 마지막 시도 시작(locked_at)~끝(updated_at) 근사,
  다운로드는 URL 발급 기준.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Asset, AuditLog, Distribution, JobRow, OrgQuota, UsageSnapshot, User
from app.ports.notifier import Notification

if TYPE_CHECKING:
    from app.container import Container

ZONES = ("RAW", "PROCESSED", "EVIDENCE", "DATASET", "QUARANTINE")


def _backup_target():  # type: ignore[no-untyped-def]
    return or_(Asset.backup_class == "PROTECTED", Asset.storage_zone.in_(("EVIDENCE", "DATASET")),
               Asset.variant == "DEID")


def snapshot(c: Container, db: Session, day: date) -> int:
    """오늘 스냅숏 (이미 있으면 덮어쓴다). 기관 수를 돌려준다."""
    live = (Asset.deleted_at.is_(None), Asset.has_binary.is_(True))
    zones: dict[str, dict[str, int]] = {}
    for org, zone, total in db.execute(select(Asset.organization_id, Asset.storage_zone, func.sum(Asset.byte_size))
                                       .where(*live).group_by(Asset.organization_id, Asset.storage_zone)):
        if zone:
            zones.setdefault(org, {})[zone] = int(total or 0)
    targets = dict(db.execute(select(Asset.organization_id, func.sum(Asset.byte_size))
                              .where(*live, _backup_target(), Asset.storage_zone != "QUARANTINE")
                              .group_by(Asset.organization_id)).tuples().all())
    now = c.clock.now()
    n = 0
    for q in db.execute(select(OrgQuota)).scalars():
        row = db.get(UsageSnapshot, (day, q.organization_id)) or UsageSnapshot(
            day=day, organization_id=q.organization_id)
        row.stored_bytes, row.quarantine_bytes = q.used_bytes, q.quarantine_bytes
        row.reserved_bytes, row.backup_bytes = q.reserved_bytes, q.backup_bytes
        row.by_zone = zones.get(q.organization_id, {})
        row.backup_target_bytes = int(targets.get(q.organization_id) or 0)
        row.created_at = now
        db.merge(row)
        n += 1
    return n


def _total(s: UsageSnapshot) -> int:
    return s.stored_bytes + s.quarantine_bytes + s.reserved_bytes + s.backup_bytes


def forecast(c: Container, db: Session, today: date) -> dict[str, Any]:
    st = c.settings
    since = today - timedelta(days=st.capacity_window_days - 1)
    per_day: dict[date, int] = {}
    for s in db.execute(select(UsageSnapshot).where(UsageSnapshot.day >= since, UsageSnapshot.day <= today)).scalars():
        per_day[s.day] = per_day.get(s.day, 0) + _total(s)
    days = sorted(per_day)
    totals = [per_day[d] for d in days]
    history = [{"day": d.isoformat(), "total_bytes": t} for d, t in zip(days, totals, strict=True)]
    current: int = totals[-1] if totals else int(db.execute(select(func.coalesce(func.sum(
        OrgQuota.used_bytes + OrgQuota.quarantine_bytes + OrgQuota.reserved_bytes + OrgQuota.backup_bytes), 0)))
        .scalar_one())
    slope: float | None = None
    if len(days) >= 2:  # 최소제곱 직선의 기울기 (바이트/일)
        xs = [float((d - since).days) for d in days]
        mx, my = sum(xs) / len(xs), sum(totals) / len(totals)
        den = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, totals, strict=True)) / den if den else None
    cap = st.storage_capacity_bytes
    out: dict[str, Any] = {"capacity_bytes": cap, "threshold_ratio": st.capacity_threshold_ratio,
                           "lead_time_days": st.capacity_lead_time_days, "window_days": st.capacity_window_days,
                           "current_bytes": int(current), "current_ratio": round(current / cap, 4) if cap else None,
                           "slope_bytes_per_day": round(slope, 1) if slope is not None else None,
                           "reach_date": None, "within_lead_time": False, "note": None, "history": history}
    if not cap:
        out["note"] = "전체 스토리지 용량(STORAGE_CAPACITY_BYTES)이 설정되지 않았습니다 — 확인 필요"
        return out
    target = cap * st.capacity_threshold_ratio
    if current >= target:
        reach: date | None = today
    elif slope and slope > 0:
        reach = today + timedelta(days=int((target - current) / slope) + 1)
    else:
        reach = None
        out["note"] = ("사용량이 늘지 않아 도달 예상일이 없습니다" if slope is not None
                       else "스냅숏이 2일 이상 필요합니다")
    if reach is not None:
        out["reach_date"] = reach.isoformat()
        out["within_lead_time"] = (reach - today).days <= st.capacity_lead_time_days
    return out


def notify_if_near(c: Container, db: Session, today: date) -> dict[str, Any]:
    """리드타임 안이면 운영자에게 알린다 (요구사항 8.2). 워커가 하루 한 번."""
    f = forecast(c, db, today)
    if f["within_lead_time"]:
        to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
            User.status == "ACTIVE", User.contact_email.is_not(None),
            User.roles.any("PLATFORM_OPERATOR"))).scalars() if e}))  # type: ignore[arg-type]
        pct = int(f["threshold_ratio"] * 100)
        c.notifier.send(Notification(
            kind="capacity.forecast", organization_id=None,
            subject=f"[용량 예측] 사용률 {pct}% 도달 예상 {f['reach_date']}",
            body="\n".join([f"현재 {f['current_bytes']} 바이트 (사용률 {f['current_ratio']})",
                            f"추세 {f['slope_bytes_per_day']} 바이트/일, 도달 예상일 {f['reach_date']}",
                            f"리드타임 {f['lead_time_days']}일 안입니다. 증설 일정을 검토하세요."]),
            recipients=to, data={k: f[k] for k in ("reach_date", "current_bytes", "slope_bytes_per_day")}))
    return {k: f[k] for k in ("reach_date", "within_lead_time", "current_bytes")}


def month_range(month: str) -> tuple[datetime, datetime]:
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return start, end


def monthly_report(db: Session, month: str, organization_id: str | None) -> list[dict[str, Any]]:
    start, end = month_range(month)
    orgs = [organization_id] if organization_id else sorted(
        set(db.execute(select(OrgQuota.organization_id)).scalars())
        | set(db.execute(select(UsageSnapshot.organization_id).where(
            UsageSnapshot.day >= start.date(), UsageSnapshot.day < end.date())).scalars()))
    out = []
    for org in orgs:
        snap = db.execute(select(UsageSnapshot).where(
            UsageSnapshot.organization_id == org, UsageSnapshot.day >= start.date(), UsageSnapshot.day < end.date())
            .order_by(UsageSnapshot.day.desc()).limit(1)).scalar_one_or_none()
        by_zone = {z: int((snap.by_zone or {}).get(z, 0)) for z in ZONES} if snap else {z: 0 for z in ZONES}
        secs = {"CPU": 0.0, "GPU": 0.0}
        for rc, total in db.execute(select(JobRow.resource_class, func.sum(
                func.extract("epoch", JobRow.updated_at - JobRow.locked_at))).where(
                JobRow.organization_id == org, JobRow.status.in_(("DONE", "FAILED")), JobRow.locked_at.is_not(None),
                JobRow.updated_at >= start, JobRow.updated_at < end).group_by(JobRow.resource_class)):
            secs[rc if rc in secs else "CPU"] += float(total or 0)
        urls = db.execute(select(func.count(), func.coalesce(func.sum(Asset.byte_size), 0)).select_from(AuditLog)
                          .join(Asset, Asset.asset_id == AuditLog.target_id).where(
                              AuditLog.action == "DOWNLOAD_URL_ISSUED", AuditLog.organization_id == org,
                              AuditLog.occurred_at >= start, AuditLog.occurred_at < end)).one()
        dists = db.execute(select(func.count()).select_from(Distribution).where(
            Distribution.organization_id == org, Distribution.created_at >= start,
            Distribution.created_at < end)).scalar_one()
        out.append({"organization_id": org, "snapshot_day": snap.day.isoformat() if snap else None,
                    "storage_by_zone": by_zone, "storage_by_tier": {"HOT": sum(by_zone.values()), "WARM": 0},
                    "backup_target_bytes": snap.backup_target_bytes if snap else 0,
                    "requested_backup_bytes": snap.backup_bytes if snap else 0,
                    "processing_seconds": {k: round(v, 1) for k, v in secs.items()},
                    "downloads": {"url_count": int(urls[0]), "url_bytes": int(urls[1]),
                                  "distribution_count": int(dists)}})
    return out
