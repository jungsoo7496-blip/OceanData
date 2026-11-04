"""보존·삭제·법적 보류·사용량 (08번 요구사항 2~5).

- 보존 기간은 `retention_policies` 표에서 대상(RAW·PROXY·…·QUARANTINE:<사유>)별로 계산한다 (HW-3)
- 삭제는 활성 객체를 지우고 자산 기록은 deleted_at으로 남긴다. 삭제 증명에 객체 키·sha256을 적는다
- 법적 보류 중인 자산은 어떤 경로로도 지우지 않는다
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import (
    Asset,
    AssetLineage,
    DailyTask,
    DeletionCertificate,
    OrgQuota,
    RetentionPolicy,
    User,
)
from app.domain.ids import new_id
from app.ports.notifier import Notification
from app.services.backup import is_backup_target
from app.timefmt import iso

log = logging.getLogger(__name__)
FOREVER = datetime(9999, 12, 31, tzinfo=UTC)
VARIANT_TARGET = {"ORIGINAL": "RAW", "PROXY": "PROXY", "THUMBNAIL": "THUMBNAIL", "SAMPLE_FRAME": "THUMBNAIL",
                  "CLIP": "CLIP", "DEID": "DEID", "LABEL": "DEID", "SENSOR_EXPORT": "SENSOR_EXPORT"}
ALERT_STEPS = (80, 90, 100)


def target_for(asset: Asset) -> str:
    if asset.ingest_status == "QUARANTINED" and asset.quarantine_reason:
        return f"QUARANTINE:{asset.quarantine_reason}"
    if asset.storage_zone == "EVIDENCE":
        return "EVIDENCE"
    if asset.storage_zone == "DATASET":
        return "DATASET"
    return VARIANT_TARGET.get(asset.variant, "RAW")


def apply_retention(db: Session, asset: Asset, base: datetime) -> None:
    """retention_policy_id·retention_until을 계산해 넣는다 (요구사항 3.2). 정책이 없으면 무기한 보관."""
    pol = db.get(RetentionPolicy, target_for(asset))
    if pol is None:
        asset.retention_policy_id, asset.retention_until = "unclassified_keep", FOREVER
        return
    asset.retention_policy_id = pol.policy_id
    keep = pol.retention_days is None or pol.expiry_action == "KEEP"
    asset.retention_until = FOREVER if keep else base + timedelta(days=int(pol.retention_days or 0))


# ---------------------------------------------------------------- 사용량·쿼터 알림

def usage_view(q: OrgQuota) -> dict[str, Any]:
    used = q.used_bytes + q.quarantine_bytes + q.reserved_bytes + q.backup_bytes
    return {"organization_id": q.organization_id, "quota_bytes": q.quota_bytes, "usage_bytes": used,
            "usage_ratio": round(used / q.quota_bytes, 4) if q.quota_bytes else 0.0,
            "breakdown": {"stored_bytes": q.used_bytes, "quarantine_bytes": q.quarantine_bytes,
                          "reserved_bytes": q.reserved_bytes, "backup_bytes": q.backup_bytes}}


def org_admins(db: Session, org: str) -> tuple[str, ...]:
    """ORG_ADMIN 알림 수신 주소 (주소가 등록된 사람만)."""
    rows = db.execute(select(User.contact_email).where(
        User.organization_id == org, User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("ORG_ADMIN"))).scalars()  # type: ignore[arg-type]
    return tuple(sorted(r for r in rows if r))


def check_quota_alerts(c: Container, db: Session, q: OrgQuota) -> None:
    """사용률이 80·90·100%를 처음 넘을 때 ORG_ADMIN에게 알린다 (요구사항 2.4). 내려가면 다시 알릴 수 있게."""
    if not q.quota_bytes:
        return
    pct = 100 * (q.used_bytes + q.quarantine_bytes + q.reserved_bytes + q.backup_bytes) / q.quota_bytes
    reached = max((s for s in ALERT_STEPS if pct >= s), default=0)
    if reached > q.last_alert_pct:
        c.notifier.send(Notification(kind="quota.threshold", organization_id=q.organization_id,
                                     subject=f"저장 사용률 {reached}% 도달", body=f"현재 {pct:.1f}%",
                                     recipients=org_admins(db, q.organization_id),
                                     data={"threshold": reached, "usage_pct": round(pct, 2)}))
    q.last_alert_pct = reached


# ---------------------------------------------------------------- 삭제

class DeletionRefused(Exception):
    def __init__(self, code: str, detail: str, affected: list[dict[str, Any]] | None = None) -> None:
        super().__init__(detail)
        self.code, self.detail, self.affected = code, detail, affected or []


def with_descendants(db: Session, roots: list[Asset]) -> list[Asset]:
    """원 자산을 지우면 그 파생본도 함께 지운다 (계보로 찾음)."""
    seen = {a.asset_id: a for a in roots}
    frontier = list(seen)
    while frontier:
        kids = db.execute(select(AssetLineage.child_asset_id).where(AssetLineage.parent_asset_id.in_(frontier))) \
            .scalars().all()
        frontier = []
        for k in kids:
            if k not in seen:
                a = db.get(Asset, k)
                if a is not None and a.deleted_at is None:
                    seen[k] = a
                    frontier.append(k)
    return list(seen.values())


def affected_by(db: Session, assets: list[Asset]) -> list[dict[str, Any]]:
    """승인된 증빙·보고서(10번)·이용 권한이 있는 데이터셋 버전(13번)의 인용 목록.

    10번: 승인된 회차 기록 버전과 승인·제출된 보고서 버전. 13번: 유효한 이용 권한(GRANTED)이 있는 데이터셋 버전.
    """
    from app.db.models import DatasetVersionItem, Grant
    from app.services.evidence import citing_approved

    ids = [a.asset_id for a in assets]
    out = citing_approved(db, ids)
    if ids:  # 13번: 유효한 이용 권한이 있는 데이터셋 버전에 든 자산 (T15-c, 철회 후 삭제)
        rows = db.execute(select(DatasetVersionItem.asset_id, DatasetVersionItem.dataset_version_id,
                                 Grant.grant_id).join(Grant, Grant.dataset_version_id ==
                                                      DatasetVersionItem.dataset_version_id)
                          .where(DatasetVersionItem.asset_id.in_(ids), Grant.status == "GRANTED")).all()
        out += [{"asset_id": a, "cited_by": "DATASET", "id": v, "grant_id": g,
                 "action": "이용 권한을 먼저 철회하세요 (POST /v1/grants/{id}/revoke)"} for a, v, g in rows]
    return out


def delete_assets(c: Container, db: Session, *, organization_id: str, assets: list[Asset], scope: str,
                  target_id: str, reason: str | None, requested_by: str, confirm_affected: bool) -> DeletionCertificate:
    held = [a.asset_id for a in assets if a.legal_hold]
    if held:  # 법적 보류 (요구사항 4.1)
        raise DeletionRefused("CONFLICT", f"법적 보류 중인 자산이 있어 삭제할 수 없습니다 ({len(held)}건)",
                              [{"asset_id": x, "reason": "LEGAL_HOLD"} for x in held])
    affected = affected_by(db, assets)
    if affected and not confirm_affected:  # 영향 목록 확인 후 재확인 (요구사항 5.4)
        raise DeletionRefused("CONFLICT", "인용 중인 증빙·데이터셋이 있습니다. confirm_affected=true로 다시 요청하세요",
                              affected)
    now = c.clock.now()
    objects: list[dict[str, Any]] = []
    backed_up = False
    q = db.execute(select(OrgQuota).where(OrgQuota.organization_id == organization_id).with_for_update()) \
        .scalar_one_or_none()
    cert_id = new_id("deletion_certificate")
    for a in assets:
        if a.storage_key and a.storage_zone:
            try:
                c.storage.delete_object(c.settings.buckets[a.storage_zone], a.storage_key)
            except Exception as exc:  # noqa: BLE001 - 이미 없는 객체도 삭제 완료로 본다
                log.warning("delete_object_failed", extra={"asset_id": a.asset_id, "error_type": type(exc).__name__})
            objects.append({"asset_id": a.asset_id, "object_key": a.storage_key, "checksum_sha256": a.checksum_sha256,
                            "byte_size": a.byte_size})
            if q is not None and a.has_binary:
                if a.storage_zone == "QUARANTINE":
                    q.quarantine_bytes = max(0, q.quarantine_bytes - a.byte_size)
                else:
                    q.used_bytes = max(0, q.used_bytes - a.byte_size)
        if is_backup_target(a):
            backed_up = True
        a.deleted_at, a.deletion_certificate_id, a.storage_key, a.updated_at = now, cert_id, None, now
    cert = DeletionCertificate(
        certificate_id=cert_id, organization_id=organization_id, scope=scope, target_id=target_id, reason=reason,
        requested_by=requested_by, deleted_at=now, objects=objects,
        # 독립 백업 사본은 백업 보존 주기가 끝나면 사라진다 (요구사항 5.2)
        backup_copies_expire_at=now + timedelta(days=c.settings.backup_retention_days) if backed_up else None)
    db.add(cert)
    db.flush()
    return cert


def certificate_view(cert: DeletionCertificate) -> dict[str, Any]:
    return {"certificate_id": cert.certificate_id, "organization_id": cert.organization_id, "scope": cert.scope,
            "target_id": cert.target_id, "reason": cert.reason, "requested_by": cert.requested_by,
            "deleted_at": iso(cert.deleted_at), "objects": cert.objects,
            "backup_copies_expire_at": iso(cert.backup_copies_expire_at)}


# ---------------------------------------------------------------- 보존 만료

def _expired(db: Session, now: datetime) -> Any:
    return select(Asset).join(RetentionPolicy, RetentionPolicy.policy_id == Asset.retention_policy_id) \
        .where(Asset.deleted_at.is_(None), Asset.retention_until <= now, RetentionPolicy.expiry_action == "DELETE")


def dry_run(db: Session, now: datetime) -> dict[str, Any]:
    """삭제 없이 만료 대상을 보여준다 (요구사항 3.6, 3.7)."""
    rows = db.execute(_expired(db, now)).scalars().all()
    targets: dict[str, list[int]] = {}
    held = 0
    victims = []
    for a in rows:
        if a.legal_hold:
            held += 1
            continue
        t = targets.setdefault(target_for(a), [0, 0])
        t[0] += 1
        t[1] += a.byte_size if a.has_binary else 0
        victims.append(a)
    warnings = affected_by(db, victims)
    return {"generated_at": iso(now), "targets": [{"target": k, "count": v[0], "bytes": v[1]}
                                                   for k, v in sorted(targets.items())],
            "total_count": sum(v[0] for v in targets.values()), "total_bytes": sum(v[1] for v in targets.values()),
            "excluded_legal_hold": held, "warnings": warnings}


def execute_expired(c: Container, db: Session, now: datetime) -> list[DeletionCertificate]:
    """만료 동작 실행 (요구사항 3.4). 기관별 삭제 증명 하나씩. 인용 중인 자산은 건너뛴다."""
    rows = [a for a in db.execute(_expired(db, now)).scalars().all() if not a.legal_hold]
    cited = {w["asset_id"] for w in affected_by(db, rows)}
    by_org: dict[str, list[Asset]] = {}
    for a in rows:
        if a.asset_id not in cited:
            by_org.setdefault(a.organization_id, []).append(a)
    certs = []
    for org, assets in sorted(by_org.items()):
        certs.append(delete_assets(c, db, organization_id=org, assets=assets, scope="RETENTION",
                                   target_id=f"retention-{now.date().isoformat()}", reason="보존 기간 만료",
                                   requested_by="SERVICE", confirm_affected=False))
        c.audit.record("RETENTION_DELETED", actor_kind="SERVICE", actor_id="retention", organization_id=org,
                       target_type="DeletionCertificate", target_id=certs[-1].certificate_id,
                       details={"count": len(assets)})
    return certs


def notify_expiring(c: Container, db: Session, now: datetime) -> int:
    """만료 N일 전(정책 notify_days_before) ORG_ADMIN에게 알린다 (요구사항 3.3)."""
    rows = db.execute(select(Asset.organization_id, func.count(), func.min(Asset.retention_until))
                      .join(RetentionPolicy, RetentionPolicy.policy_id == Asset.retention_policy_id)
                      .where(Asset.deleted_at.is_(None), Asset.legal_hold.is_(False),
                             RetentionPolicy.expiry_action == "DELETE", Asset.retention_until > now,
                             Asset.retention_until <= now + func.make_interval(0, 0, 0,
                                                                               RetentionPolicy.notify_days_before))
                      .group_by(Asset.organization_id)).all()
    for org, n, first in rows:
        c.notifier.send(Notification(kind="retention.expiring", organization_id=org,
                                     subject=f"보존 만료 예정 자산 {n}건",
                                     body=f"가장 이른 만료: {iso(first)}", recipients=org_admins(db, org),
                                     data={"count": int(n)}))
    return len(rows)


def reconcile(c: Container, db: Session) -> list[dict[str, Any]]:
    """DB 사용량과 스토리지 실측 대조 (요구사항 2.3). 차이가 있으면 운영자에게 알린다."""
    diffs = []
    for q in db.execute(select(OrgQuota)).scalars():
        measured = 0
        for zone in ("RAW", "PROCESSED", "EVIDENCE", "QUARANTINE"):
            try:
                measured += sum(o.size for o in c.storage.list_objects(c.settings.buckets[zone],
                                                                       q.organization_id + "/"))
            except Exception as exc:  # noqa: BLE001 - 버킷이 없으면 0으로 보고 기록만
                log.warning("reconcile_list_failed", extra={"zone": zone, "error_type": type(exc).__name__})
        recorded = q.used_bytes + q.quarantine_bytes
        if measured != recorded:
            diffs.append({"organization_id": q.organization_id, "recorded": recorded, "measured": measured})
    if diffs:
        c.notifier.send(Notification(kind="quota.reconcile_mismatch", organization_id=None,
                                     subject="사용량 대조 차이", body=f"{len(diffs)}개 기관", data={"diffs": diffs}))
    return diffs


def run_daily(c: Container, name: str, fn: Any) -> Any | None:
    """하루 한 번만 실행 (여러 워커가 있어도 advisory lock으로 한 번)."""
    today = c.clock.now().astimezone(UTC).date()
    with c.sessionmaker.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:n))"), {"n": "daily:" + name})
        task = db.get(DailyTask, name)
        last: date | None = task.last_run_on.date() if task else None
        if last is not None and last >= today:
            return None
        result = fn(db)
        row = {"last_run_on": datetime.combine(today, datetime.min.time()), "result": result}
        if task is None:
            db.add(DailyTask(name=name, **row))
        else:
            task.last_run_on, task.result = row["last_run_on"], row["result"]
        return result
