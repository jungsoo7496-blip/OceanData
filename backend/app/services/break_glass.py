"""긴급 열람 (02번 요구사항 8, security.md: 운영자라도 L0 원본은 긴급 열람 승인이 있을 때만).

요청(사유·대상·기간 ≤ 24시간) → 다른 운영자 승인(설정으로 본인 승인 허용) → 대상 기관 ORG_ADMIN 통지 → 기간 동안
원본 접근마다 감사 → 기간이 끝나면 자동 회수. 회수는 접근 판단에서 바로(만료 시각 비교) 일어나고, 워커는 상태만 맞춘다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Asset, BreakGlassGrant, TrialSession, User
from app.domain.ids import new_id
from app.ports.notifier import Notification
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

MAX_DURATION_S = 24 * 3600


class Refused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


def effective_status(g: BreakGlassGrant, now: datetime) -> str:
    if g.status == "ACTIVE" and g.expires_at is not None and g.expires_at <= now:
        return "EXPIRED"
    return g.status


def view(g: BreakGlassGrant, now: datetime) -> dict[str, Any]:
    return {"grant_id": g.grant_id, "organization_id": g.organization_id, "target_type": g.target_type,
            "target_id": g.target_id, "reason": g.reason, "duration_s": g.duration_s,
            "status": effective_status(g, now), "requested_by": g.requested_by, "approved_by": g.approved_by,
            "starts_at": iso(g.starts_at) if g.starts_at else None,
            "expires_at": iso(g.expires_at) if g.expires_at else None, "created_at": iso(g.created_at)}


def target_org(db: Session, target_type: str, target_id: str) -> str | None:
    if target_type == "ASSET":
        a = db.get(Asset, target_id)
        return a.organization_id if a is not None and a.deleted_at is None else None
    s = db.get(TrialSession, target_id)
    return s.organization_id if s is not None else None


def request(db: Session, *, by: str, target_type: str, target_id: str, reason: str, duration_s: int,
            now: datetime) -> BreakGlassGrant:
    if not 60 <= duration_s <= MAX_DURATION_S:
        raise Refused("VALIDATION_ERROR", "기간은 60초~24시간입니다")
    org = target_org(db, target_type, target_id)
    if org is None:
        raise Refused("NOT_FOUND", "대상을 찾을 수 없습니다")
    g = BreakGlassGrant(grant_id=new_id("break_glass"), requested_by=by, approved_by=None, organization_id=org,
                        target_type=target_type, target_id=target_id, reason=reason, duration_s=duration_s,
                        status="REQUESTED", starts_at=None, expires_at=None, created_at=now)
    db.add(g)
    db.flush()
    return g


def decide(c: Container, db: Session, g: BreakGlassGrant, *, by: str, approve: bool, now: datetime) -> None:
    if g.status != "REQUESTED":
        raise Refused("CONFLICT", f"이미 결정된 요청입니다 ({effective_status(g, now)})")
    if g.requested_by == by and not c.settings.break_glass_single_approval:
        raise Refused("FORBIDDEN", "요청한 운영자는 승인할 수 없습니다 (다른 운영자 승인 필요)")
    if not approve:
        g.status = "DENIED"
        return
    g.status, g.approved_by = "ACTIVE", by
    g.starts_at, g.expires_at = now, now + timedelta(seconds=g.duration_s)
    _notify(c, db, g)


def revoke(g: BreakGlassGrant, now: datetime) -> None:
    if effective_status(g, now) not in ("REQUESTED", "ACTIVE"):
        raise Refused("CONFLICT", f"이미 끝난 긴급 열람입니다 ({effective_status(g, now)})")
    if g.status == "REQUESTED":
        g.status = "DENIED"  # 승인 전 철회는 거절과 같다
        return
    g.status, g.expires_at = "EXPIRED", now


def _notify(c: Container, db: Session, g: BreakGlassGrant) -> None:
    """대상 기관 ORG_ADMIN에게 통지 (요구사항 8.2). 본문은 대상·사유·기간만 (URL·토큰 없음)."""
    to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
        User.organization_id == g.organization_id, User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("ORG_ADMIN"))).scalars() if e}))  # type: ignore[arg-type]
    c.notifier.send(Notification(
        kind="break_glass.granted", organization_id=g.organization_id,
        subject="[긴급 열람] 운영기관이 귀 기관 원본에 임시로 접근합니다",
        body="\n".join([f"대상: {g.target_type} {g.target_id}", f"사유: {g.reason}",
                        f"기간: {iso(g.starts_at)} ~ {iso(g.expires_at)} (UTC)", "",
                        "접근 기록은 감사 로그와 플랫폼의 긴급 열람 기록에서 확인할 수 있습니다."]),
        recipients=to, data={"grant_id": g.grant_id, "target_type": g.target_type, "target_id": g.target_id}))


def active_grant_for(db: Session, a: Asset, operator_id: str, now: datetime) -> BreakGlassGrant | None:
    """이 운영자가 이 자산(또는 그 회차)에 지금 유효한 긴급 열람 권한을 갖고 있나."""
    targets = [and_(BreakGlassGrant.target_type == "ASSET", BreakGlassGrant.target_id == a.asset_id)]
    if a.session_id:
        targets.append(and_(BreakGlassGrant.target_type == "SESSION", BreakGlassGrant.target_id == a.session_id))
    return db.execute(select(BreakGlassGrant).where(
        BreakGlassGrant.requested_by == operator_id, BreakGlassGrant.status == "ACTIVE",
        BreakGlassGrant.organization_id == a.organization_id, BreakGlassGrant.expires_at > now, or_(*targets))
        .order_by(BreakGlassGrant.expires_at.desc()).limit(1)).scalar_one_or_none()


def expire(db: Session, now: datetime) -> list[BreakGlassGrant]:
    """기간이 끝난 권한을 EXPIRED로 (요구사항 8.4). 워커 정비 주기에서 부른다."""
    rows = list(db.execute(select(BreakGlassGrant).where(
        BreakGlassGrant.status == "ACTIVE", BreakGlassGrant.expires_at <= now).with_for_update()).scalars())
    if rows:
        db.execute(update(BreakGlassGrant).where(BreakGlassGrant.grant_id.in_([g.grant_id for g in rows]))
                   .values(status="EXPIRED"))
    return rows
