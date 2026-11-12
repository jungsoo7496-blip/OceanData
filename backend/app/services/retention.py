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
    Upload,
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
VERIFYING_INGEST_STATES = ("RECEIVED", "VALIDATING")


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


class DeletionStorageFailed(Exception):
    """저장소가 일부 객체를 지우지 못했다 — 삭제를 확정하지 않는다.

    이미 지운 객체는 다시 요청하면 '이미 없음'으로 넘어간다 (ADR-0055)."""

    def __init__(self, failed: list[dict[str, str]]) -> None:
        super().__init__(f"저장소 객체 {len(failed)}개를 지우지 못했습니다")
        self.failed = failed


def _purge_object(place: Any, key: str, asset_id: str) -> bool:
    """객체 하나를 지운다. 이미 없으면 지운 것이다. 지웠는지 확인되지 않으면 False.

    삭제 호출이 실패해도 객체가 실제로 없으면(이미 지워졌다) 성공으로 본다. 있거나 확인할 수 없으면 실패다."""
    try:
        place.storage.delete_object(place.bucket, key)
        return True
    except Exception as exc:  # noqa: BLE001 - 아래에서 실제로 남았는지 확인한다
        log.warning("delete_object_failed", extra={"asset_id": asset_id, "tier": place.tier,
                                                   "error_type": type(exc).__name__})
    try:
        return bool(place.storage.head_object(place.bucket, key) is None)
    except Exception:  # noqa: BLE001 - 확인할 수 없으면 지웠다고 말하지 않는다
        return False


def with_descendants(db: Session, roots: list[Asset], *, lock: bool = False) -> list[Asset]:
    """원 자산을 지우면 그 파생본도 함께 지운다 (계보로 찾음).

    lock이면(삭제) 단계마다 자산 행을 잠그고 최신 값으로 다시 읽은 뒤 그 파생본을 찾는다.
    파생본을 만드는 작업(구간 클립)은 원 자산 행을 잠그고 살아 있는지 확인한 뒤 기록하므로, 그사이 확정된
    파생본이 삭제·삭제 증명에서 빠지지 않는다 (ADR-0033). 기다리는 동안 다른 요청이 지운 자산은 뺀다.
    잠금 순서는 자산 행 → 필요한 업로드 행(대기 없이) → 기관 쿼터 행 (delete_assets)."""
    if lock:
        roots = lock_live(db, [a.asset_id for a in roots])
    seen = {a.asset_id: a for a in roots}
    frontier = list(seen)
    while frontier:
        kids = db.execute(select(AssetLineage.child_asset_id).where(AssetLineage.parent_asset_id.in_(frontier))) \
            .scalars().all()
        new = sorted(set(kids) - set(seen))
        if lock:
            found = lock_live(db, new)
        else:
            found = [a for a in (db.get(Asset, k) for k in new) if a is not None and a.deleted_at is None]
        seen.update((a.asset_id, a) for a in found)
        frontier = [a.asset_id for a in found]
    return list(seen.values())


def lock_live(db: Session, asset_ids: list[str]) -> list[Asset]:
    """자산 행을 asset_id 순으로 잠그고(FOR UPDATE) 최신 값으로 다시 읽는다. 이미 삭제된 자산은 뺀다."""
    if not asset_ids:
        return []
    rows = db.execute(select(Asset).where(Asset.asset_id.in_(asset_ids)).order_by(Asset.asset_id).with_for_update()
                      .execution_options(populate_existing=True)).scalars().all()
    return [a for a in rows if a.deleted_at is None]


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


def _abort_pending_uploads(c: Container, db: Session, organization_id: str, assets: list[Asset]) -> set[str]:
    from app.services.ingest import IngestService

    if any(a.ingest_status in VERIFYING_INGEST_STATES for a in assets):
        raise DeletionRefused("CONFLICT", "검증 중인 자산이 있습니다. 잠시 뒤 다시 시도하세요")
    pending = {a.asset_id for a in assets if a.ingest_status == "UPLOADING"}
    if not pending:
        return pending

    # 완료·만료는 업로드를 먼저 잠근다. 이미 자산을 잠근 삭제는 그 업로드를 기다리지 않는다.
    uploads = db.scalars(select(Upload).where(
        Upload.asset_id.in_(pending), Upload.organization_id == organization_id, Upload.status == "UPLOADING",
    ).order_by(Upload.upload_id).with_for_update(skip_locked=True)).all()
    if len(uploads) != len(pending) or {up.asset_id for up in uploads} != pending:
        raise DeletionRefused("CONFLICT", "진행 중인 업로드를 확인할 수 없습니다. 잠시 뒤 다시 시도하세요")

    service = IngestService(c, db)
    for up in uploads:
        service.abort(up, preserve_asset=True)
    return pending


def delete_assets(c: Container, db: Session, *, organization_id: str, assets: list[Asset], scope: str,
                  target_id: str, reason: str | None, requested_by: str, confirm_affected: bool) -> DeletionCertificate:
    held = [a.asset_id for a in assets if a.legal_hold]
    if held:  # 법적 보류 (요구사항 4.1)
        raise DeletionRefused("CONFLICT", f"법적 보류 중인 자산이 있어 삭제할 수 없습니다 ({len(held)}건)",
                              [{"asset_id": x, "reason": "LEGAL_HOLD"} for x in held])
    affected = affected_by(db, assets)
    granted = [x for x in affected if x["cited_by"] == "DATASET"]
    if granted:  # 유효한 이용 권한이 있는 데이터셋 버전은 재확인으로도 지울 수 없다 — 철회가 먼저 (요구사항 5.3, T15-c)
        raise DeletionRefused("CONFLICT", "유효한 이용 권한이 있는 데이터셋에 포함된 자산은 삭제할 수 없습니다. "
                              "이용 권한을 먼저 철회하세요", granted)
    if affected and not confirm_affected:  # 영향 목록 확인 후 재확인 (요구사항 5.4)
        raise DeletionRefused("CONFLICT", "인용 중인 증빙·보고서가 있습니다. confirm_affected=true로 다시 요청하세요",
                              affected)
    aborted = _abort_pending_uploads(c, db, organization_id, assets)
    now = c.clock.now()
    objects: list[dict[str, Any]] = []
    purge: list[tuple[Any, str, str]] = []  # (계층, 객체 키, 자산 ID) — DB가 받아들인 뒤에 지운다
    backed_up = False
    q = db.execute(select(OrgQuota).where(OrgQuota.organization_id == organization_id).with_for_update()) \
        .scalar_one_or_none()
    cert_id = new_id("deletion_certificate")
    from app.services import tiers

    used = tiers.in_use(c, db)
    for a in assets:
        # 바이너리가 없는 자산(C0, 취소된 업로드의 흔적)에는 지울 객체가 없다 — 증명에도 넣지 않는다
        if a.asset_id not in aborted and a.has_binary and a.storage_key and a.storage_zone:
            # 쓰는 모든 계층에서 지운다: 계층 이동 중 사본·유예 중인 원래 객체도 남기지 않는다 (08번 요구사항 9)
            purge.extend((place, a.storage_key, a.asset_id) for place in tiers.zone_places(c, a.storage_zone, used))
            objects.append({"asset_id": a.asset_id, "object_key": a.storage_key, "checksum_sha256": a.checksum_sha256,
                            "byte_size": a.byte_size})
            if q is not None:
                if a.storage_zone == "QUARANTINE":
                    q.quarantine_bytes = max(0, q.quarantine_bytes - a.byte_size)
                else:
                    q.used_bytes = max(0, q.used_bytes - a.byte_size)
                if a.backup_requested_at is not None:  # 신청 백업 용량도 뺀다 (백업 사본은 보존 주기 뒤 사라짐)
                    q.backup_bytes = max(0, q.backup_bytes - a.byte_size)
        if a.asset_id not in aborted and is_backup_target(a):
            backed_up = True
        a.deleted_at, a.deletion_certificate_id, a.storage_key, a.updated_at = now, cert_id, None, now
    cert = DeletionCertificate(
        certificate_id=cert_id, organization_id=organization_id, scope=scope, target_id=target_id, reason=reason,
        requested_by=requested_by, deleted_at=now, objects=objects,
        # 독립 백업 사본은 백업 보존 주기가 끝나면 사라진다 (요구사항 5.2)
        backup_copies_expire_at=now + timedelta(days=c.settings.backup_retention_days) if backed_up else None)
    db.add(cert)
    db.flush()  # DB 제약(사유의 NUL 등)이 걸리면 여기서 멈춘다 — 저장소 객체는 아직 그대로다
    failed = [{"asset_id": aid, "tier": place.tier} for place, key, aid in purge if not _purge_object(place, key, aid)]
    if failed:  # 못 지운 객체를 '지웠다'고 증명하지 않는다. 호출자가 트랜잭션을 되돌린다 (재시도하면 이어서 지운다)
        raise DeletionStorageFailed(failed)
    return cert


def certificate_view(cert: DeletionCertificate) -> dict[str, Any]:
    return {"certificate_id": cert.certificate_id, "organization_id": cert.organization_id, "scope": cert.scope,
            "target_id": cert.target_id, "reason": cert.reason, "requested_by": cert.requested_by,
            "deleted_at": iso(cert.deleted_at), "objects": cert.objects,
            "backup_copies_expire_at": iso(cert.backup_copies_expire_at)}


# ---------------------------------------------------------------- 보존 만료

def _expired(db: Session, now: datetime) -> Any:
    return select(Asset).join(RetentionPolicy, RetentionPolicy.policy_id == Asset.retention_policy_id) \
        .where(Asset.deleted_at.is_(None), Asset.retention_until <= now, RetentionPolicy.expiry_action == "DELETE",
               Asset.ingest_status.not_in(("UPLOADING", *VERIFYING_INGEST_STATES)))


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
    warnings = [
        {
            **citation,
            "reason": "IN_DATASET_VERSION" if citation["cited_by"] == "DATASET" else "CITED_BY_APPROVED_EVIDENCE",
        }
        for citation in affected_by(db, victims)
    ]
    return {"generated_at": iso(now), "targets": [{"target": k, "count": v[0], "bytes": v[1]}
                                                   for k, v in sorted(targets.items())],
            "total_count": sum(v[0] for v in targets.values()), "total_bytes": sum(v[1] for v in targets.values()),
            "excluded_legal_hold": held, "warnings": warnings}


def execute_expired(c: Container, db: Session, now: datetime) -> list[DeletionCertificate]:
    """만료 동작 실행 (요구사항 3.4). 기관별 삭제 증명 하나씩. 인용 중인 자산은 건너뛴다.

    대상 자산 행을 asset_id 순으로 먼저 잠근다 — API 삭제·파생 작업(구간 클립)과 같은 '자산 행 → 기관 쿼터 행'
    순서이고, 파생 작업이 원 자산을 잠그고 기록하는 중이면 끝날 때까지 기다린다.
    기다리는 동안 바뀐 행(이미 삭제됨 등)은 다시 판단한다."""
    stmt = _expired(db, now).order_by(Asset.asset_id).with_for_update(of=Asset) \
        .execution_options(populate_existing=True)
    rows = [a for a in db.execute(stmt).scalars().all() if not a.legal_hold]
    cited = {w["asset_id"] for w in affected_by(db, rows)}
    by_org: dict[str, list[Asset]] = {}
    for a in rows:
        if a.asset_id not in cited:
            by_org.setdefault(a.organization_id, []).append(a)
    certs = []
    for org, assets in sorted(by_org.items()):
        try:
            # 기관마다 따로 확정한다: 저장소 삭제가 실패한 기관의 DB 변경만 되돌리고 앞 기관의 삭제는 남긴다 (ADR-0055)
            with db.begin_nested():
                cert = delete_assets(c, db, organization_id=org, assets=assets, scope="RETENTION",
                                     target_id=f"retention-{now.date().isoformat()}", reason="보존 기간 만료",
                                     requested_by="SERVICE", confirm_affected=False)
        except DeletionStorageFailed as exc:
            log.error("retention_delete_failed", extra={"organization_id": org, "objects": len(exc.failed)})
            c.notifier.send(Notification(
                kind="retention.delete_failed", organization_id=None, subject="보존 만료 삭제 실패",
                body=f"기관 {org}: 저장소 객체 {len(exc.failed)}개를 지우지 못해 이번 삭제를 확정하지 않았습니다. "
                     "저장소가 돌아오면 다음 실행이 이어서 지웁니다",
                data={"organization_id": org, "objects": len(exc.failed)}))
            continue
        certs.append(cert)
        c.audit.record("RETENTION_DELETED", actor_kind="SERVICE", actor_id="retention", organization_id=org,
                       target_type="DeletionCertificate", target_id=cert.certificate_id,
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
    """DB 사용량과 스토리지 실측 대조 (요구사항 2.3). 차이가 있으면 운영자에게 알린다.

    HOT·WARM 계층 버킷을 모두 잰다. 계층 이동 중 사본과 유예 중인 원래 객체(잠시 두 벌)는 빼고 비교한다."""
    from app.services import tier_mover, tiers

    zones = ("RAW", "PROCESSED", "EVIDENCE", "QUARANTINE")
    places = tiers.all_places(c, zones, tiers.in_use(c, db))
    diffs = []
    for q in db.execute(select(OrgQuota)).scalars():
        measured = 0
        for place in places:
            try:
                measured += sum(o.size for o in place.storage.list_objects(place.bucket, q.organization_id + "/"))
            except Exception as exc:  # noqa: BLE001 - 버킷이 없으면 0으로 보고 기록만
                log.warning("reconcile_list_failed", extra={"zone": place.zone, "tier": place.tier,
                                                            "error_type": type(exc).__name__})
        try:
            measured -= tier_mover.transient_bytes(c, db, q.organization_id, zones)
        except Exception as exc:  # noqa: BLE001 - 확인하지 못하면 빼지 않는다 (차이로 드러난다)
            log.warning("reconcile_transient_failed", extra={"error_type": type(exc).__name__})
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
