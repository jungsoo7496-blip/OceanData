"""데이터셋·이용 신청·권한·배포 (13번 요구사항 5~12).

- 데이터셋 버전: 공유 조건(L2 이상)을 지금 충족하는 승인 자산만 DATASET 존에 복사하고 매니페스트(sha256)를 만든다.
- 버전의 등급은 항목의 '현재' 등급 중 가장 낮은 것 (domain.md 4장). 하향된 자산이 있으면 그 버전 등급도 내려간다.
- 이용 신청: 모든 제공기관 ORG_ADMIN 승인 → 운영자 최종 승인 → Grant. 한 곳이라도 거절하면 끝 (운영자가 못 뒤집음).
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AccessDecision,
    AccessRequest,
    Asset,
    AssetLineage,
    Dataset,
    DatasetVersion,
    DatasetVersionItem,
    DeidReview,
    Distribution,
    Grant,
    User,
)
from app.domain import sharing_rules as rules
from app.domain import states
from app.domain.errors import RuleViolation, UnmetCondition
from app.domain.ids import new_id
from app.ports.notifier import Notification
from app.services import contracts
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

MANIFEST_VARIANTS = {"DEID", "PROXY", "CLIP", "THUMBNAIL", "SENSOR_EXPORT"}
CATALOG_MIN = "APPROVED_PARTNERS"  # 이용기관(CONSUMER)은 L3 이상만
NO_RECALL = "이미 다운로드한 파일은 회수되지 않습니다. 약정에 따라 파기하고 파기 확인을 제출해 주세요."


def contains_asset(db: Session, asset_id: str) -> bool:
    """Sharing_Rules의 L3 조건 '데이터셋 버전에 포함'."""
    return db.execute(select(func.count()).select_from(DatasetVersionItem)
                      .where(DatasetVersionItem.asset_id == asset_id)).scalar_one() > 0


def version_scope(db: Session, v: DatasetVersion) -> str:
    """항목의 현재 등급 중 가장 낮은 것. 삭제된 항목이 있으면 PRIVATE."""
    rows = db.execute(select(Asset.sharing_scope, Asset.deleted_at).join(
        DatasetVersionItem, DatasetVersionItem.asset_id == Asset.asset_id)
        .where(DatasetVersionItem.dataset_version_id == v.dataset_version_id)).all()
    return rules.dataset_scope(["PRIVATE" if d else s for s, d in rows])


def _root(db: Session, a: Asset) -> str:
    from app.services.sharing import ancestors

    roots = [x.asset_id for x in ancestors(db, a.asset_id) if x.variant == "ORIGINAL"]
    return roots[0] if roots else a.asset_id


def _chain(db: Session, asset_id: str) -> list[dict[str, Any]]:
    """데이터셋 항목에서 원본까지 가까운 상위부터 (13번 요구사항 5.3)."""
    out: list[dict[str, Any]] = []
    cur, seen = asset_id, {asset_id}
    while True:
        e = db.execute(select(AssetLineage).where(AssetLineage.child_asset_id == cur)
                       .order_by(AssetLineage.parent_asset_id).limit(1)).scalar_one_or_none()
        if e is None or e.parent_asset_id in seen:
            return out
        parent = db.get(Asset, e.parent_asset_id)
        out.append({"asset_id": e.parent_asset_id, "relation": e.relation,
                    "variant": parent.variant if parent else "UNKNOWN"})
        cur = e.parent_asset_id
        seen.add(cur)


def version_lineage(db: Session, v: DatasetVersion, organization_id: str | None) -> dict[str, Any]:
    """데이터셋 버전 → 항목별 원본·처리 모델 버전·검수자 (T06-b). organization_id가 있으면 그 기관 항목만."""
    manifest_items = {i["asset_id"]: i for i in v.manifest.get("items", [])}
    rows = list(db.execute(select(DatasetVersionItem).where(
        DatasetVersionItem.dataset_version_id == v.dataset_version_id).order_by(DatasetVersionItem.path)).scalars())
    visible = [r for r in rows if organization_id is None or r.organization_id == organization_id]
    items = []
    for r in visible:
        a = db.get(Asset, r.asset_id)
        deid = ((a.provenance or {}) if a else {}).get("deid")
        reviews = db.execute(select(DeidReview).where(DeidReview.deid_asset_id == r.asset_id)
                             .order_by(DeidReview.created_at, DeidReview.review_id)).scalars()
        items.append({
            "asset_id": r.asset_id, "path": r.path, "organization_id": r.organization_id,
            "source_asset_id": manifest_items.get(r.asset_id, {}).get("source_asset_id", r.asset_id),
            "chain": _chain(db, r.asset_id),
            "processing": {"model": deid.get("model") or {}, "params": deid.get("params") or {},
                           "processed_at": deid.get("processed_at")} if deid else None,
            "reviews": [{"review_id": x.review_id, "reviewer_id": x.reviewer_id, "status": x.status,
                         "recheck": x.recheck, "decided_at": iso(x.decided_at) if x.decided_at else None}
                        for x in reviews],
            "review_result": a.review_result if a else None})
    return {"dataset_id": v.dataset_id, "dataset_version_id": v.dataset_version_id, "version": v.version,
            "manifest_sha256": v.manifest_sha256, "hidden_item_count": len(rows) - len(visible), "items": items}


def _copy(c: Container, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
    fd, path = tempfile.mkstemp(prefix="dsv-")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in c.storage.iter_object(src_bucket, src_key):
                f.write(chunk)
        c.storage.upload_file(dst_bucket, dst_key, path)
    finally:
        os.unlink(path)


def build_version(c: Container, db: Session, ds: Dataset, version: str, asset_ids: list[str],
                  data_card: dict[str, Any], by: str) -> DatasetVersion:
    """승인 자산으로 버전을 만든다 (요구사항 5). 조건을 못 맞춘 자산이 하나라도 있으면 RuleViolation."""
    from app.services.sharing import facts_for

    now = c.clock.now()
    assets = list(db.execute(select(Asset).where(Asset.asset_id.in_(asset_ids))).scalars())
    found = {a.asset_id for a in assets}
    unmet: list[UnmetCondition] = [UnmetCondition("자산 존재", f"{x} 없음") for x in asset_ids if x not in found]
    for a in assets:
        if a.deleted_at or not a.has_binary or not a.storage_key or a.variant not in MANIFEST_VARIANTS:
            unmet.append(UnmetCondition("바이너리가 있는 공유용 파생본", f"{a.asset_id} {a.variant}"))
            continue
        if rules.LEVEL[a.sharing_scope] < 2:
            unmet.append(UnmetCondition("sharing_scope ≥ L2", f"{a.asset_id} {a.sharing_scope}"))
            continue
        for u in rules.evaluate("SANDBOX_PARTNERS", facts_for(db, a, now), {"ORG_ADMIN"}):
            unmet.append(UnmetCondition(u.condition, f"{a.asset_id} {u.actual}"))
    if unmet:
        raise RuleViolation("데이터셋에 넣을 수 없는 자산이 있습니다", unmet)
    vid = new_id("dataset_version")
    items: list[dict[str, Any]] = []
    rows: list[DatasetVersionItem] = []
    for a in sorted(assets, key=lambda x: x.asset_id):
        ext = mimetypes.guess_extension(a.mime_type or "") or ".bin"
        path = f"{a.variant.lower()}/{a.asset_id}{ext}"
        key = f"{ds.dataset_id}/{vid}/{path}"
        _copy(c, c.settings.buckets[a.storage_zone or "PROCESSED"], a.storage_key or "", c.settings.bucket_datasets,
              key)
        rows.append(DatasetVersionItem(dataset_version_id=vid, asset_id=a.asset_id, organization_id=a.organization_id,
                                       path=path, object_key=key, byte_size=a.byte_size,
                                       checksum_sha256=a.checksum_sha256))
        items.append({"asset_id": a.asset_id, "source_asset_id": _root(db, a), "variant": a.variant, "path": path,
                      "byte_size": a.byte_size, "checksum_sha256": a.checksum_sha256,
                      "privacy_status": a.privacy_status, "sharing_scope": a.sharing_scope,
                      "legal_basis_ref": a.legal_basis_ref})
    providers = sorted({a.organization_id for a in assets})
    manifest: dict[str, Any] = {
                "schema_version": "1.0.0", "dataset_id": ds.dataset_id, "dataset_version_id": vid, "version": version,
                "created_at": iso(now), "provider_organization_ids": providers, "item_count": len(items),
                "total_bytes": sum(int(i["byte_size"]) for i in items), "items": items, "data_card": data_card}
    problems = contracts.schema_problems("dataset-version-manifest", manifest)
    if problems:
        raise RuleViolation("매니페스트·데이터 카드가 계약과 맞지 않습니다",
                            [UnmetCondition(p.pointer or "/", p.message) for p in problems[:20]])
    body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1).encode()
    mkey = f"{ds.dataset_id}/{vid}/MANIFEST.json"
    c.storage.put_object(c.settings.bucket_datasets, mkey, body, "application/json")
    v = DatasetVersion(dataset_version_id=vid, dataset_id=ds.dataset_id, version=version,
                       provider_organization_ids=providers, item_count=len(items),
                       total_bytes=manifest["total_bytes"], data_card=data_card, manifest=manifest, manifest_key=mkey,
                       manifest_sha256=hashlib.sha256(body).hexdigest(), created_by=by, created_at=now)
    db.add(v)
    db.flush()
    db.add_all(rows)
    return v


def dataset_view(db: Session, ds: Dataset, versions: list[DatasetVersion]) -> dict[str, Any]:
    latest = versions[-1] if versions else None
    return {"dataset_id": ds.dataset_id, "title": ds.title, "published": ds.published,
            "published_at": iso(ds.published_at),
            "versions": [{"dataset_version_id": v.dataset_version_id, "version": v.version,
                          "created_at": iso(v.created_at), "scope": version_scope(db, v), "item_count": v.item_count,
                          "total_bytes": v.total_bytes, "manifest_sha256": v.manifest_sha256} for v in versions],
            **({"data_card": latest.data_card} if latest else {})}


def visible_versions(db: Session, ds: Dataset, consumer: bool) -> list[DatasetVersion]:
    vs = list(db.execute(select(DatasetVersion).where(DatasetVersion.dataset_id == ds.dataset_id)
                         .order_by(DatasetVersion.created_at, DatasetVersion.dataset_version_id)).scalars())
    if consumer:  # 이용기관에는 게시된 데이터셋의 L3 이상 버전만 (요구사항 7)
        vs = [v for v in vs if ds.published and rules.LEVEL[version_scope(db, v)] >= rules.LEVEL[CATALOG_MIN]]
    return vs


# ------------------------------------------------------------------ 이용 신청·결정 (요구사항 8)


def _org_admin_emails(db: Session, orgs: list[str]) -> tuple[str, ...]:
    return tuple(sorted(e for e in db.execute(select(User.contact_email).where(
        User.organization_id.in_(orgs), User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("ORG_ADMIN"))).scalars() if e))  # type: ignore[arg-type]


def create_request(c: Container, db: Session, v: DatasetVersion, consumer_org: str, by: str,
                   body: dict[str, Any]) -> AccessRequest:
    from app.services.sharing import tier2_valid

    now = c.clock.now()
    # 약정 허용 목적 (요구사항 8.5, T08-f): 기관 이름은 알려주지 않고 수만
    refused = 0
    for org in v.provider_organization_ids:
        ag = tier2_valid(db, org, now)
        if ag is None or body["purpose"] not in (ag.allowed_purposes or []):
            refused += 1
    if body["purpose"] not in v.data_card.get("usage", {}).get("allowed_purposes", []):
        raise RuleViolation("데이터 카드의 허용 목적이 아닙니다",
                            [UnmetCondition("purpose ∈ data_card.usage.allowed_purposes", body["purpose"])])
    if refused:
        raise RuleViolation("일부 제공기관의 2단 약정이 이 목적을 허용하지 않습니다",
                            [UnmetCondition("모든 제공기관 2단 약정의 허용 목적", f"허용하지 않는 기관 {refused}곳")])
    start = date.fromisoformat(body["period"]["start_date"])
    end = date.fromisoformat(body["period"]["end_date"])
    if end < start:
        raise RuleViolation("이용 기간이 올바르지 않습니다", [UnmetCondition("end_date ≥ start_date", str(end))])
    req = AccessRequest(access_request_id=new_id("access_request"), dataset_version_id=v.dataset_version_id,
                        organization_id=consumer_org, requested_by=by, purpose=body["purpose"],
                        purpose_detail=body["purpose_detail"],
                        period_start=datetime.combine(start, time.min, tzinfo=UTC),
                        period_end=datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
                        security_measures=body["security_measures"], agreement_version=body.get("agreement_version"),
                        status="REQUESTED", deadline_at=now + timedelta(days=c.settings.access_decision_days),
                        created_at=now, updated_at=now)
    db.add(req)
    c.notifier.send(Notification(kind="access.requested", organization_id=None,
                                 subject="[이용 신청] 데이터셋 이용 승인 요청",
                                 body=f"데이터셋 버전 {v.version}에 이용 신청이 들어왔습니다. 목적: {body['purpose']}\n"
                                      f"응답 기한: {iso(req.deadline_at)} (기한 안에 응답이 없으면 거부됩니다)",
                                 recipients=_org_admin_emails(db, v.provider_organization_ids),
                                 data={"access_request_id": req.access_request_id}))
    return req


def decide(c: Container, db: Session, req: AccessRequest, v: DatasetVersion, org: str, roles: set[str],
           approve: bool, reason: str, by: str) -> Grant | None:
    """제공기관 결정 → 모두 승인이면 PROVIDER_APPROVED. 운영자 결정은 그다음 (요구사항 8.2, 8.6)."""
    now = c.clock.now()
    operator = "PLATFORM_OPERATOR" in roles
    provider = "ORG_ADMIN" in roles and org in v.provider_organization_ids
    if not operator and not provider:
        raise PermissionError("이 신청을 결정할 수 없습니다")
    if db.get(AccessDecision, (req.access_request_id, org)) is not None:
        raise states.InvalidStateTransition("grant_status", req.status, "결정 중복")
    if provider and not operator and req.status != "REQUESTED":
        raise states.InvalidStateTransition("grant_status", req.status, "PROVIDER_APPROVED")
    if operator and approve and req.status != "PROVIDER_APPROVED":  # 제공기관 거절은 운영자가 뒤집을 수 없다
        raise states.InvalidStateTransition("grant_status", req.status, "GRANTED")
    db.add(AccessDecision(access_request_id=req.access_request_id, organization_id=org,
                          role="OPERATOR" if operator else "PROVIDER", decision="APPROVE" if approve else "DENY",
                          reason=reason, decided_by=by, decided_at=now))
    db.flush()
    req.updated_at = now
    if not approve:
        req.status = states.transition("grant_status", req.status, "DENIED", roles)
        _notify_consumer(c, db, req, "거부", reason)
        return None
    if not operator:
        approved = set(db.execute(select(AccessDecision.organization_id).where(
            AccessDecision.access_request_id == req.access_request_id, AccessDecision.role == "PROVIDER",
            AccessDecision.decision == "APPROVE")).scalars())
        if approved >= set(v.provider_organization_ids):
            req.status = states.transition("grant_status", req.status, "PROVIDER_APPROVED", roles)
        return None
    if rules.LEVEL[version_scope(db, v)] < rules.LEVEL[CATALOG_MIN]:
        raise RuleViolation("데이터셋 버전 등급이 이용기관 제공 조건(L3)에 못 미칩니다",
                            [UnmetCondition("데이터셋 버전 등급 ≥ L3", version_scope(db, v))])
    req.status = states.transition("grant_status", req.status, "GRANTED", roles)
    g = Grant(grant_id=new_id("grant"), access_request_id=req.access_request_id,
              dataset_version_id=v.dataset_version_id, organization_id=req.organization_id, status="GRANTED",
              starts_at=max(now, req.period_start), expires_at=req.period_end, created_at=now)
    db.add(g)
    _notify_consumer(c, db, req, "승인", reason)
    return g


def _notify_consumer(c: Container, db: Session, req: AccessRequest, what: str, reason: str) -> None:
    to = tuple(sorted(e for e in db.execute(select(User.contact_email).where(
        User.organization_id == req.organization_id, User.status == "ACTIVE",
        User.contact_email.is_not(None))).scalars() if e))
    c.notifier.send(Notification(kind="access.decided", organization_id=req.organization_id,
                                 subject=f"[이용 신청 {what}] {req.access_request_id}",
                                 body=f"이용 신청이 {what}되었습니다.\n사유: {reason}", recipients=to,
                                 data={"access_request_id": req.access_request_id, "status": req.status}))


def expire(c: Container, db: Session, now: datetime | None = None) -> dict[str, int]:
    """응답 기한 지난 신청 → DENIED, 기간 끝난 권한 → EXPIRED (요구사항 8.3, 10.1). 워커 정비 주기."""
    now = now or c.clock.now()
    denied = 0
    for r in db.execute(select(AccessRequest).where(AccessRequest.status.in_(["REQUESTED", "PROVIDER_APPROVED"]),
                                                    AccessRequest.deadline_at < now).with_for_update()).scalars():
        r.status, r.updated_at = states.transition("grant_status", r.status, "DENIED", {"SERVICE"}), now
        _notify_consumer(c, db, r, "거부", "응답 기한 초과 (기본값 거부)")
        denied += 1
    expired = 0
    for g in db.execute(select(Grant).where(Grant.status == "GRANTED", Grant.expires_at <= now)
                        .with_for_update()).scalars():
        g.status = states.transition("grant_status", g.status, "EXPIRED", {"SERVICE"})
        req = db.get(AccessRequest, g.access_request_id)
        if req is not None:
            req.status, req.updated_at = g.status, now
        expired += 1
    return {"denied": denied, "expired": expired}


def revoke(c: Container, db: Session, g: Grant, roles: set[str], by: str, reason: str) -> None:
    """철회: 즉시 새 URL 발급 중지, 이용자에게 알림(회수 불가 명시), 파기 확인 대기 (요구사항 10.2, 10.4)."""
    now = c.clock.now()
    g.status = states.transition("grant_status", g.status, "REVOKED", roles)
    g.revoked_at, g.revoked_by, g.revoke_reason = now, by, reason
    req = db.get(AccessRequest, g.access_request_id)
    if req is not None:
        req.status, req.updated_at = "REVOKED", now
        _notify_consumer(c, db, req, "철회", f"{reason}\n{NO_RECALL}")


def on_downgrade(c: Container, db: Session, a: Asset, handling: str, by: str, reason: str) -> list[str]:
    """공유 등급 하향 (요구사항 10.3): 새 버전에서는 규칙이 빼고, REVOKE_NOW면 그 자산이 든 버전의 권한을 끊는다."""
    if handling != "REVOKE_NOW":
        return []
    vids = list(db.execute(select(DatasetVersionItem.dataset_version_id)
                           .where(DatasetVersionItem.asset_id == a.asset_id)).scalars())
    revoked = []
    for g in db.execute(select(Grant).where(Grant.dataset_version_id.in_(vids), Grant.status == "GRANTED")
                        .with_for_update()).scalars() if vids else []:
        revoke(c, db, g, {"ORG_ADMIN", "PLATFORM_OPERATOR"}, by, f"제공기관 공유 등급 하향: {reason}")
        revoked.append(g.grant_id)
    return revoked


# ------------------------------------------------------------------ 배포 (요구사항 9)


def distribute(c: Container, db: Session, g: Grant, by: str, paths: list[str] | None) -> dict[str, Any]:
    now = c.clock.now()
    if g.status != "GRANTED" or g.expires_at <= now or g.starts_at > now:
        raise GrantInactive(g.status if g.status != "GRANTED" else "기간 밖")
    v = db.get(DatasetVersion, g.dataset_version_id)
    assert v is not None  # noqa: S101 - FK
    items = list(db.execute(select(DatasetVersionItem).where(
        DatasetVersionItem.dataset_version_id == v.dataset_version_id).order_by(DatasetVersionItem.path)).scalars())
    if paths:
        wanted = set(paths)
        items = [i for i in items if i.path in wanted]
    ttl = c.settings.presign_download_ttl_s
    did = new_id("distribution")
    # 받는 쪽 식별 정보를 담은 매니페스트 (요구사항 9.3)
    recipient = {"distribution_id": did, "grant_id": g.grant_id, "organization_id": g.organization_id,
                 "requested_by": by, "issued_at": iso(now), "grant_expires_at": iso(g.expires_at),
                 "manifest_sha256": v.manifest_sha256}
    rkey = f"{v.dataset_id}/{v.dataset_version_id}/distributions/{did}.json"
    rbody = json.dumps({"recipient": recipient, "manifest": v.manifest}, ensure_ascii=False, indent=1).encode()
    c.storage.put_object(c.settings.bucket_datasets, rkey, rbody, "application/json")
    files = [{"path": "RECIPIENT_MANIFEST.json", "url": c.storage.presign_get(c.settings.bucket_datasets, rkey, ttl),
              "byte_size": len(rbody), "checksum_sha256": hashlib.sha256(rbody).hexdigest()}]
    files += [{"path": i.path, "url": c.storage.presign_get(c.settings.bucket_datasets, i.object_key, ttl),
               "byte_size": i.byte_size, "checksum_sha256": i.checksum_sha256} for i in items]
    db.add(Distribution(distribution_id=did, grant_id=g.grant_id, organization_id=g.organization_id,
                        requested_by=by, file_count=len(files), expires_at=now + timedelta(seconds=ttl),
                        created_at=now))
    return {"distribution_id": did, "grant_id": g.grant_id, "dataset_version_id": v.dataset_version_id,
            "expires_at": iso(now + timedelta(seconds=ttl)), "files": files, "manifest": v.manifest,
            "recipient": recipient}


class GrantInactive(Exception):
    pass


def request_view(db: Session, r: AccessRequest) -> dict[str, Any]:
    g = db.execute(select(Grant).where(Grant.access_request_id == r.access_request_id)).scalar_one_or_none()
    v = db.get(DatasetVersion, r.dataset_version_id)
    approved = db.execute(select(func.count()).select_from(AccessDecision).where(
        AccessDecision.access_request_id == r.access_request_id, AccessDecision.role == "PROVIDER",
        AccessDecision.decision == "APPROVE")).scalar_one()
    out: dict[str, Any] = {
        "access_request_id": r.access_request_id, "dataset_version_id": r.dataset_version_id, "purpose": r.purpose,
        "status": r.status, "created_at": iso(r.created_at), "deadline_at": iso(r.deadline_at),
        "pending_provider_approvals": max(0, len(v.provider_organization_ids) - int(approved)) if v else 0}
    if g is not None:
        out.update({"grant_id": g.grant_id, "grant_expires_at": iso(g.expires_at), "grant_status": g.status})
    return out
