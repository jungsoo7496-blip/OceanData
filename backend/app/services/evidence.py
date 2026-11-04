"""회차 기록·증빙 (10번 요구사항 1, 2, 6): 버전, 근거 확인, 인용 역색인, 승인 시 PROTECTED, 근거 손상 감지."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    Device,
    Event,
    EventAsset,
    EvidenceCitation,
    ReportVersion,
    SessionRecord,
    SessionRecordVersion,
    User,
)
from app.domain.normalize import parse_rfc3339
from app.ports.notifier import Notification
from app.services.observations import OBS
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

RECORD_FIELDS = ("kind", "title", "test_item", "conditions", "result", "note", "evidence_refs")


class BadEvidence(Exception):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("bad evidence")
        self.errors = errors


def check_refs(db: Session, organization_id: str, refs: list[dict[str, Any]]) -> list[str]:
    """근거가 같은 기관 소속이고 지금 있는지 확인하고 (요구사항 1.2) 인용 자산 목록을 돌려준다.

    다른 기관의 자산·이벤트·장비는 '없음'과 같은 오류로 답한다 (존재를 드러내지 않는다)."""
    errors: list[dict[str, str]] = []
    assets: set[str] = set()
    for i, r in enumerate(refs):
        ptr = f"/evidence_refs/{i}"
        if r["kind"] == "ASSET":
            a = db.get(Asset, r["asset_id"])
            if a is None or a.organization_id != organization_id or a.deleted_at is not None:
                errors.append({"pointer": ptr, "code": "not_found", "message": "자산을 찾을 수 없습니다"})
                continue
            if r.get("from_offset_s") is not None and r.get("to_offset_s") is not None \
                    and r["from_offset_s"] > r["to_offset_s"]:
                errors.append({"pointer": ptr, "code": "range", "message": "구간 시작이 끝보다 늦습니다"})
            assets.add(a.asset_id)
        elif r["kind"] == "EVENT":
            e = db.get(Event, r["event_id"])
            if e is None or e.organization_id != organization_id:
                errors.append({"pointer": ptr, "code": "not_found", "message": "이벤트를 찾을 수 없습니다"})
                continue
            assets |= set(db.execute(select(EventAsset.asset_id).where(EventAsset.event_id == e.event_id)).scalars())
        else:  # OBSERVATION_RANGE
            d = db.get(Device, r["source_system_id"])
            start, end = parse_rfc3339(r["from"]), parse_rfc3339(r["to"])
            if d is None or d.organization_id != organization_id:
                errors.append({"pointer": ptr, "code": "not_found", "message": "장비를 찾을 수 없습니다"})
                continue
            if start > end:
                errors.append({"pointer": ptr, "code": "range", "message": "구간 시작이 끝보다 늦습니다"})
                continue
            assets |= {x for x in db.execute(select(OBS.c.source_asset_id).distinct().where(
                OBS.c.organization_id == organization_id, OBS.c.source_system_id == d.device_id,
                OBS.c.metric == r["metric"], OBS.c.observed_at >= start, OBS.c.observed_at <= end,
                OBS.c.source_asset_id.is_not(None))).scalars() if x}
    if errors:
        raise BadEvidence(errors)
    return sorted(assets)


def cite(db: Session, organization_id: str, citing_type: str, citing_id: str, version: int, asset_ids: list[str],
         now: datetime) -> None:
    if asset_ids:
        db.execute(insert(EvidenceCitation).values([
            {"organization_id": organization_id, "citing_type": citing_type, "citing_id": citing_id,
             "citing_version": version, "asset_id": a, "cited_at": now} for a in asset_ids]).on_conflict_do_nothing())


def cited_assets(db: Session, citing_type: str, citing_id: str, version: int) -> list[str]:
    return list(db.execute(select(EvidenceCitation.asset_id).where(
        EvidenceCitation.citing_type == citing_type, EvidenceCitation.citing_id == citing_id,
        EvidenceCitation.citing_version == version).order_by(EvidenceCitation.asset_id)).scalars())


def protect(db: Session, asset_ids: list[str], now: datetime) -> int:
    """승인된 증빙·보고서가 인용한 자산 → backup_class=PROTECTED (domain.md 9장). 클립을 인용했으면 그 클립만."""
    if not asset_ids:
        return 0
    res = db.execute(update(Asset).where(Asset.asset_id.in_(asset_ids), Asset.backup_class != "PROTECTED")
                     .values(backup_class="PROTECTED", updated_at=now))
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


def add_version(db: Session, rec: SessionRecord, values: dict[str, Any], by: str, now: datetime,
                asset_ids: list[str]) -> SessionRecordVersion:
    rec.latest_version = (rec.latest_version or 0) + 1
    v = SessionRecordVersion(record_id=rec.record_id, version=rec.latest_version, status="DRAFT", created_by=by,
                             created_at=now, evidence_damaged=False, damage=[],
                             **{k: values.get(k) for k in RECORD_FIELDS})
    v.evidence_refs = values.get("evidence_refs") or []
    db.add(v)
    rec.updated_at = now
    db.flush()
    cite(db, rec.organization_id, "RECORD", rec.record_id, v.version, asset_ids, now)
    return v


def record_view(rec: SessionRecord, v: SessionRecordVersion) -> dict[str, Any]:
    return {"record_id": rec.record_id, "session_id": rec.session_id, "kind": v.kind, "title": v.title,
            "test_item": v.test_item, "conditions": v.conditions, "result": v.result, "note": v.note,
            "evidence_refs": v.evidence_refs, "created_at": iso(v.created_at), "created_by": v.created_by,
            "version": v.version, "latest_version": rec.latest_version, "status": v.status,
            "approved_by": v.approved_by, "approved_at": iso(v.approved_at), "evidence_damaged": v.evidence_damaged,
            "damage": v.damage}


# ------------------------------------------------------------------ 08번 삭제 영향 (affected_by)


def citing_approved(db: Session, asset_ids: list[str]) -> list[dict[str, Any]]:
    """승인된 증빙·보고서(승인·제출본)가 인용한 자산 목록 (08번 retention.affected_by)."""
    if not asset_ids:
        return []
    rec = db.execute(select(EvidenceCitation.asset_id, EvidenceCitation.citing_id, EvidenceCitation.citing_version)
                     .join(SessionRecordVersion, and_(
                         SessionRecordVersion.record_id == EvidenceCitation.citing_id,
                         SessionRecordVersion.version == EvidenceCitation.citing_version))
                     .where(EvidenceCitation.citing_type == "RECORD", EvidenceCitation.asset_id.in_(asset_ids),
                            SessionRecordVersion.status == "APPROVED")).all()
    rep = db.execute(select(EvidenceCitation.asset_id, EvidenceCitation.citing_id, EvidenceCitation.citing_version)
                     .join(ReportVersion, and_(ReportVersion.report_id == EvidenceCitation.citing_id,
                                               ReportVersion.version == EvidenceCitation.citing_version))
                     .where(EvidenceCitation.citing_type == "REPORT", EvidenceCitation.asset_id.in_(asset_ids),
                            ReportVersion.status.in_(["APPROVED", "SUBMITTED"]))).all()
    return [{"asset_id": a, "cited_by": kind, "id": i, "version": v}
            for kind, rows in (("RECORD", rec), ("REPORT", rep)) for a, i, v in rows]


# ------------------------------------------------------------------ 근거 손상 감지 (요구사항 6)


def check_damage(c: Container, db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """인용 자산이 삭제·격리·재처리되면 해당 증빙·보고서 버전에 '근거 손상'을 남기고 작성자·ORG_ADMIN에게 알린다.
    Evidence_Integrity_Checker — 워커가 감시 주기마다 부른다. 한 인용은 한 번만 표시한다."""
    now = now or c.clock.now()
    rows = db.execute(select(EvidenceCitation, Asset).join(Asset, Asset.asset_id == EvidenceCitation.asset_id).where(
        EvidenceCitation.damage_reason.is_(None),
        or_(Asset.deleted_at.is_not(None), Asset.ingest_status == "QUARANTINED",
            and_(Asset.last_reprocessed_at.is_not(None), Asset.last_reprocessed_at > EvidenceCitation.cited_at)))
        .with_for_update(of=EvidenceCitation)).all()
    by_doc: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for ci, a in rows:
        reason = "DELETED" if a.deleted_at else "QUARANTINED" if a.ingest_status == "QUARANTINED" else "REPROCESSED"
        ci.damage_reason = reason
        by_doc[(ci.citing_type, ci.citing_id, ci.citing_version)].append(
            {"asset_id": a.asset_id, "reason": reason, "detected_at": iso(now)})
    flagged = []
    for (kind, ident, version), damage in sorted(by_doc.items()):
        doc: SessionRecordVersion | ReportVersion | None
        if kind == "RECORD":
            doc = db.get(SessionRecordVersion, (ident, version))
            parent = db.get(SessionRecord, ident)
            title = doc.title if doc else ident
            author, org = (doc.created_by if doc else None), (parent.organization_id if parent else None)
        else:
            from app.db.models import Report

            doc = db.get(ReportVersion, (ident, version))
            rep = db.get(Report, ident)
            title = rep.title if rep else ident
            author, org = (rep.created_by if rep else None), (rep.organization_id if rep else None)
        if doc is None:
            continue
        doc.evidence_damaged = True
        doc.damage = [*(doc.damage or []), *damage]
        flagged.append({"kind": kind, "id": ident, "version": version, "damage": damage})
        if kind == "REPORT" and doc.status == "SUPERSEDED":  # type: ignore[union-attr]
            continue  # 지난 버전은 표시만
        _notify_damage(c, db, kind, ident, version, title, author, org, damage)
    return flagged


def _notify_damage(c: Container, db: Session, kind: str, ident: str, version: int, title: str, author: str | None,
                   org: str | None, damage: list[dict[str, Any]]) -> None:
    conds = []
    if author:
        conds.append(User.user_id == author)
    if org:
        conds.append(and_(User.organization_id == org, User.roles.any("ORG_ADMIN")))  # type: ignore[arg-type]
    to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
        User.status == "ACTIVE", User.contact_email.is_not(None), or_(*conds))).scalars() if e})) if conds else ()
    label = {"RECORD": "회차 기록", "REPORT": "보고서"}[kind]
    reasons = {"DELETED": "삭제", "QUARANTINED": "격리", "REPROCESSED": "재처리"}
    lines = [f"{label} '{title}' (버전 {version})이 인용한 근거 자산에 변화가 있습니다.", ""]
    lines += [f"- {d['asset_id']}: {reasons[d['reason']]}" for d in damage]
    lines += ["", "근거 손상 표시가 있는 보고서는 제출할 수 없습니다. 근거를 바꾼 새 버전을 만드세요."]
    c.notifier.send(Notification(kind="evidence.damaged", organization_id=org, subject=f"[근거 손상] {label} {title}",
                                 body="\n".join(lines), recipients=to,
                                 data={"citing_type": kind, "id": ident, "version": version,
                                       "asset_ids": [d["asset_id"] for d in damage]}))
