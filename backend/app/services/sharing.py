"""공유 등급·심사 흐름 (13번 요구사항 1~4, 10). 판단은 domain/sharing_rules 한 곳, 여기서는 사실 수집과 기록.

우회 방지 (요구사항 3.3): ORM으로 sharing_scope를 올리는 변경은 이 모듈이 표시한 것만 저장된다
(`before_flush` 가드). 내리는 변경은 언제든 허용한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, inspect, or_, select
from sqlalchemy.orm import Session

from app.db.models import Agreement, Asset, AssetLineage, ShareRequest, ShareReviewEvent, User
from app.domain import sharing_rules as rules
from app.domain import states
from app.domain.errors import RuleViolation, UnmetCondition
from app.domain.ids import new_id
from app.ports.notifier import Notification
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

DERIVED_FOR_SHARING = ("DEID", "SENSOR_EXPORT", "LABEL", "CLIP", "SAMPLE_FRAME")
_OK = "_sharing_rules_ok"


class SharingBypass(RuntimeError):
    """Sharing_Rules를 거치지 않고 공유 등급을 올리려 했다."""


@event.listens_for(Session, "before_flush")
def _guard(session: Session, _ctx: Any, _instances: Any) -> None:
    for obj in [*session.new, *session.dirty]:
        if not isinstance(obj, Asset):
            continue
        new = obj.sharing_scope
        if obj in session.new:
            old = "PROGRAM_REVIEW"  # 새 자산은 L1까지 바로 만들 수 있다 (시험·이관). 그 위는 규칙을 거친다
        else:
            hist = inspect(obj).attrs.sharing_scope.history
            if not hist.has_changes():
                continue
            old = hist.deleted[0] if hist.deleted else new
        if rules.LEVEL.get(new, 99) > rules.LEVEL.get(old, 0) and obj.__dict__.get(_OK) != new:
            raise SharingBypass(f"{obj.asset_id}: sharing_scope {old} → {new}는 Sharing_Rules를 거쳐야 합니다")


def _set_scope(a: Asset, target: str, now: datetime) -> None:
    a.__dict__[_OK] = target
    a.sharing_scope, a.updated_at = target, now


# ------------------------------------------------------------------ 사실 수집


def ancestors(db: Session, asset_id: str) -> list[Asset]:
    seen: set[str] = {asset_id}
    frontier, out = [asset_id], []
    while frontier:
        parents = list(db.execute(select(AssetLineage.parent_asset_id)
                                  .where(AssetLineage.child_asset_id.in_(frontier))).scalars())
        frontier = [p for p in parents if p not in seen]
        seen |= set(frontier)
        out += list(db.execute(select(Asset).where(Asset.asset_id.in_(frontier))).scalars()) if frontier else []
    return out


def descendants(db: Session, asset_id: str) -> list[Asset]:
    seen: set[str] = {asset_id}
    frontier, out = [asset_id], []
    while frontier:
        kids = list(db.execute(select(AssetLineage.child_asset_id)
                               .where(AssetLineage.parent_asset_id.in_(frontier))).scalars())
        frontier = [k for k in kids if k not in seen]
        seen |= set(frontier)
        out += list(db.execute(select(Asset).where(Asset.asset_id.in_(frontier))).scalars()) if frontier else []
    return out


def tier2_valid(db: Session, organization_id: str, now: datetime) -> Agreement | None:
    return db.execute(select(Agreement).where(
        Agreement.organization_id == organization_id, Agreement.tier == "TIER2", Agreement.status == "ACTIVE",
        Agreement.valid_from <= now, or_(Agreement.valid_until.is_(None), Agreement.valid_until > now))
        .order_by(Agreement.valid_from.desc()).limit(1)).scalar_one_or_none()


def in_dataset_version(db: Session, asset_id: str) -> bool:
    """13번 B에서 데이터셋 버전 항목 표가 생기면 채운다."""
    try:
        from app.services.datasets import contains_asset
    except ImportError:
        return False
    return contains_asset(db, asset_id)


def facts_for(db: Session, a: Asset, now: datetime) -> rules.Facts:
    return rules.Facts(
        variant=a.variant, data_role=a.data_role, sensitivity_class=a.sensitivity_class,
        privacy_status=a.privacy_status, review_result=a.review_result, share_status=a.share_status,
        legal_basis_ref=a.legal_basis_ref, tier2_agreement_valid=tier2_valid(db, a.organization_id, now) is not None,
        in_dataset_version=in_dataset_version(db, a.asset_id),
        ancestors=tuple(rules.Lineal(x.variant, x.data_role, x.sensitivity_class) for x in ancestors(db, a.asset_id)))


# ------------------------------------------------------------------ 등급 변경 (요구사항 3, 10)


def request_scope(c: Container, db: Session, a: Asset, target: str, reason: str, handling: str | None,
                  by: str) -> ShareRequest:
    """ORG_ADMIN의 등급 변경 신청. 상향: 조건 미충족 → RuleViolation, 운영자 승인 필요 → IN_REVIEW."""
    now = c.clock.now()
    cur = a.sharing_scope
    if target == cur:
        raise states.InvalidStateTransition("sharing_scope", cur, target)
    req = ShareRequest(share_request_id=new_id("share_request"), organization_id=a.organization_id,
                       asset_id=a.asset_id, from_scope=cur, target_scope=target, state="APPLIED", reason=reason,
                       existing_grant_handling=handling, requested_by=by, requested_at=now)
    if rules.is_downgrade(cur, target):
        if handling is None:
            raise RuleViolation("하향할 때는 이미 부여된 이용 권한 처리(existing_grant_handling)를 정해야 합니다",
                                [UnmetCondition(condition="existing_grant_handling 지정", actual=None)])
        downgrade(c, db, a, target, handling, by, reason)
        db.add(req)
        return req
    unmet = rules.evaluate(target, facts_for(db, a, now), {"ORG_ADMIN"})
    hard = [u for u in unmet if u.condition != "PLATFORM_OPERATOR 승인"]
    if hard:
        raise RuleViolation(f"{rules.LABEL[target]} 조건을 충족하지 못했습니다", hard)
    if unmet:  # 운영자 승인만 남음
        req.state = "IN_REVIEW"
    else:
        _set_scope(a, target, now)
    db.add(req)
    return req


def decide_request(c: Container, db: Session, req: ShareRequest, approve: bool, reason: str, by: str) -> Asset:
    """L3·L4 신청에 대한 운영자 결정. 결정 시점에 조건을 다시 평가한다."""
    if req.state != "IN_REVIEW":
        raise states.InvalidStateTransition("share_request", req.state, "APPLIED" if approve else "DENIED")
    a = db.get(Asset, req.asset_id, with_for_update=True)
    assert a is not None  # noqa: S101 - FK
    now = c.clock.now()
    req.decided_by, req.decided_at, req.decision_reason = by, now, reason
    if not approve:
        req.state = "DENIED"
        return a
    if a.sharing_scope != req.from_scope:
        raise states.InvalidStateTransition("sharing_scope", a.sharing_scope, req.target_scope)
    unmet = rules.evaluate(req.target_scope, facts_for(db, a, now), {"ORG_ADMIN", "PLATFORM_OPERATOR"})
    if unmet:
        raise RuleViolation(f"{rules.LABEL[req.target_scope]} 조건을 충족하지 못했습니다", unmet)
    _set_scope(a, req.target_scope, now)
    req.state = "APPLIED"
    return a


def downgrade(c: Container, db: Session, a: Asset, target: str, handling: str, by: str, reason: str) -> None:
    """하향은 언제든 (요구사항 10.3). 기존 이용 권한 처리 선택을 기록하고, 즉시 철회면 13번 B가 권한을 끊는다."""
    now = c.clock.now()
    a.sharing_scope, a.updated_at = target, now
    try:
        from app.services.datasets import on_downgrade
    except ImportError:
        return
    on_downgrade(c, db, a, handling, by, reason)


# ------------------------------------------------------------------ 공유 후보·심사 흐름 (요구사항 1, 2, 4)


def mark_candidates(db: Session, assets: list[Asset], by_roles: set[str],
                    now: datetime) -> tuple[list[str], list[dict[str, str]]]:
    """BYPRODUCT 원본만 CANDIDATE로. 나머지는 사유와 함께 제외 목록으로 (요구사항 1)."""
    done, excluded = [], []
    for a in assets:
        if a.data_role != "BYPRODUCT":
            excluded.append({"asset_id": a.asset_id, "reason": "CORE"})
        elif a.variant != "ORIGINAL":
            excluded.append({"asset_id": a.asset_id, "reason": "NOT_ORIGINAL"})
        elif not states.can_transition("share_status", a.share_status, "CANDIDATE", by_roles):
            excluded.append({"asset_id": a.asset_id, "reason": f"STATUS_{a.share_status}"})
        else:
            _review_event(db, a, "CANDIDATE", "/".join(sorted(by_roles)), None, None, now)
            a.share_status, a.updated_at = "CANDIDATE", now
            done.append(a.asset_id)
    return done, excluded


def _review_event(db: Session, a: Asset, to: str, actor: str, reason: str | None, legal: str | None,
                  now: datetime) -> None:
    db.add(ShareReviewEvent(asset_id=a.asset_id, organization_id=a.organization_id, from_status=a.share_status,
                            to_status=to, actor=actor, reason=reason, legal_basis_ref=legal, at=now))


ACTIONS = {
    "START_LEGAL_REVIEW": "LEGAL_REVIEW", "PASS_LEGAL_REVIEW": "PROCESSING", "APPROVE": "APPROVED",
    "REQUEST_REPROCESS": "REPROCESS", "REJECT": "REJECTED", "RESUBMIT": "CANDIDATE", "WITHDRAW": "NONE",
}


def review(c: Container, db: Session, a: Asset, action: str, roles: set[str], actor: str, reason: str | None,
           legal_basis_ref: str | None, handling: str | None) -> list[str]:
    """원본 단위 심사 전이 (domain.md 5장 share_status 표). 바뀐 파생본 ID 목록을 돌려준다."""
    now = c.clock.now()
    if a.variant != "ORIGINAL":
        raise RuleViolation("공유 심사는 원본 단위로 합니다", [UnmetCondition("variant=ORIGINAL", a.variant)])
    target = ACTIONS[action]
    a_status = states.transition("share_status", a.share_status, target, roles)
    if target == "PROCESSING" and not legal_basis_ref:
        raise RuleViolation("법률 검토 통과에는 legal_basis_ref가 필요합니다",
                            [UnmetCondition("legal_basis_ref 기록", None)])
    if target in ("REJECTED", "REPROCESS") and not reason:
        raise RuleViolation("반려에는 사유가 필요합니다", [UnmetCondition("사유", None)])
    changed: list[str] = []
    kids = [d for d in descendants(db, a.asset_id) if d.variant in DERIVED_FOR_SHARING]
    if target == "APPROVED":
        passed = [d for d in kids if d.review_result == "PASSED"]
        if not passed:
            raise RuleViolation("검수 합격한 파생본이 없습니다",
                                [UnmetCondition("파생본 review_result=PASSED", "없음")])
        for d in passed:  # 원본이 APPROVED가 되면 이 심사에서 만든 파생본에도 기록 (요구사항 4.5)
            d.share_status, d.legal_basis_ref, d.updated_at = "APPROVED", a.legal_basis_ref, now
            changed.append(d.asset_id)
    if target == "NONE":  # 공유 철회: 파생본은 L1 이하로 (domain.md 5장)
        for d in kids:
            if rules.LEVEL[d.sharing_scope] > 1:
                downgrade(c, db, d, "PROGRAM_REVIEW", handling or "REVOKE_NOW", actor, reason or "공유 철회")
            d.share_status, d.updated_at = "NONE", now
            changed.append(d.asset_id)
    _review_event(db, a, a_status, actor, reason, legal_basis_ref, now)
    if target == "PROCESSING":
        a.legal_basis_ref = legal_basis_ref
        c.queue.enqueue("deidentify", {"asset_id": a.asset_id}, organization_id=a.organization_id,
                        dedupe_key=f"deidentify:{a.asset_id}:{int(now.timestamp())}")  # 14번이 처리한다
    a.share_status, a.updated_at = a_status, now
    if target in ("REJECTED", "REPROCESS"):
        _notify_org(c, db, a, target, reason or "")
    return changed


def mark_processed(c: Container, db: Session, original: Asset) -> None:
    """비식별 등 파생본 생성 완료 (SERVICE, 14번 작업이 부른다): PROCESSING → QUALITY_REVIEW."""
    now = c.clock.now()
    target = states.transition("share_status", original.share_status, "QUALITY_REVIEW", {"SERVICE"})
    _review_event(db, original, target, "SERVICE", None, None, now)
    original.share_status, original.updated_at = target, now


def _notify_org(c: Container, db: Session, a: Asset, status: str, reason: str) -> None:
    to = tuple(sorted(e for e in db.execute(select(User.contact_email).where(
        User.organization_id == a.organization_id, User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("ORG_ADMIN"))).scalars() if e))  # type: ignore[arg-type]
    label = {"REJECTED": "반려", "REPROCESS": "재처리 요청"}[status]
    c.notifier.send(Notification(kind="share.review", organization_id=a.organization_id,
                                 subject=f"[공유 심사 {label}] {a.asset_id}",
                                 body=f"공유 후보 {a.asset_id}가 {label}되었습니다.\n사유: {reason}", recipients=to,
                                 data={"asset_id": a.asset_id, "share_status": status}))


def request_view(r: ShareRequest, share_status: str) -> dict[str, Any]:
    return {"share_request_id": r.share_request_id, "asset_id": r.asset_id, "from_scope": r.from_scope,
            "target_scope": r.target_scope, "state": r.state, "share_status": share_status, "reason": r.reason,
            "existing_grant_handling": r.existing_grant_handling, "requested_by": r.requested_by,
            "requested_at": iso(r.requested_at), "decided_by": r.decided_by, "decided_at": iso(r.decided_at),
            "decision_reason": r.decision_reason}
