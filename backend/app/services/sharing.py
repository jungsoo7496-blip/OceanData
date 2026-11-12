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


def downgrade(c: Container, db: Session, a: Asset, target: str, handling: str, by: str, reason: str) -> list[str]:
    """하향은 언제든 (요구사항 10.3). 기존 이용 권한 처리 선택을 기록하고, 즉시 철회면 13번 B가 권한을 끊는다.

    끊은 이용 권한 ID를 돌려준다 — 철회 감사를 남기는 호출자가 쓴다
    (분류 지정 ADR-0074, data_role CORE 변경 ADR-0076)."""
    now = c.clock.now()
    a.sharing_scope, a.updated_at = target, now
    try:
        from app.services.datasets import on_downgrade
    except ImportError:
        return []
    return on_downgrade(c, db, a, handling, by, reason)


def lock_family(db: Session, a: Asset) -> list[Asset]:
    """자신과 모든 파생본 행을 잠그고 최신 값으로 다시 읽는다 (분류 지정, ADR-0074).

    잠금 순서는 원 자산 → 파생본이고, 계보 단계마다 asset_id 순이다. 삭제(retention.with_descendants)·클립 기록·
    검수 결정(deid.decide)과 같은 순서라 서로 교착하지 않는다. 등급과 상관없이 모두 잠근다 — 같은 때 L0·L1에서
    L2 이상으로 올리는 신청이 바뀌기 전 분류를 보고 지나가지 않게 한다 (그 신청은 이 잠금을 기다린 뒤 바뀐 분류를 본다).
    """
    db.refresh(a, with_for_update=True)
    family, seen, frontier = [a], {a.asset_id}, [a.asset_id]
    while frontier:
        kids = sorted(set(db.execute(select(AssetLineage.child_asset_id)
                                     .where(AssetLineage.parent_asset_id.in_(frontier))).scalars()) - seen)
        if not kids:
            break
        seen |= set(kids)
        family += list(db.execute(select(Asset).where(Asset.asset_id.in_(kids)).order_by(Asset.asset_id)
                                  .with_for_update().execution_options(populate_existing=True)).scalars())
        frontier = kids
    return family


def shared_facts(db: Session, family: list[Asset], now: datetime) -> dict[str, rules.Facts]:
    """L2 이상인 자산의 사실. L0·L1은 조건이 승인뿐이라 분류로 내릴 일이 없어 모으지 않는다 (scope_after_change)."""
    return {x.asset_id: facts_for(db, x, now) for x in family if rules.LEVEL[x.sharing_scope] >= 2}


def lower_after_classification(c: Container, db: Session, before: dict[str, rules.Facts], family: list[Asset],
                               by: str, reason: str, now: datetime) -> list[dict[str, Any]]:
    """분류 지정 뒤 자신·파생본의 등급을 다시 본다 (요구사항 3.6·4.6, domain.md 3장 상속, ADR-0074).

    before는 바꾸기 전에 모은 L2 이상 자산의 사실이다(shared_facts). 분류 조건(sensitivity_class·privacy_status)을
    못 맞추거나 이번 변경으로 다른 조건을 새로 못 맞추게 된 자산은 조건을 충족하는 가장 높은 등급으로 내리고,
    그 자산이 든 데이터셋 버전의 이용 권한은 즉시 철회한다 (data_role CORE 변경과 같은 처리).
    내린 자산마다 끊은 권한 ID를 함께 돌려준다.
    """
    lowered: list[dict[str, Any]] = []
    for x in family:
        if x.asset_id not in before:
            continue
        target = rules.scope_after_change(x.sharing_scope, before[x.asset_id], facts_for(db, x, now))
        if rules.is_downgrade(x.sharing_scope, target):
            item: dict[str, Any] = {"asset_id": x.asset_id, "from": x.sharing_scope, "to": target}
            item["revoked_grant_ids"] = downgrade(c, db, x, target, "REVOKE_NOW", by, f"분류 변경: {reason}")
            lowered.append(item)
    return lowered


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


def action_applies(action: str, current: str) -> bool:
    """RESUBMIT는 반려된 것의 재신청(REJECTED → CANDIDATE)만이다. NONE → CANDIDATE는 후보 지정(mark_candidates)으로."""
    return action != "RESUBMIT" or current == "REJECTED"


def review(c: Container, db: Session, a: Asset, action: str, roles: set[str], actor: str, reason: str | None,
           legal_basis_ref: str | None, handling: str | None) -> list[str]:
    """원본 단위 심사 전이 (domain.md 5장 share_status 표). 바뀐 파생본 ID 목록을 돌려준다."""
    now = c.clock.now()
    if a.variant != "ORIGINAL":
        raise RuleViolation("공유 심사는 원본 단위로 합니다", [UnmetCondition("variant=ORIGINAL", a.variant)])
    target = ACTIONS[action]
    if not action_applies(action, a.share_status):
        raise states.InvalidStateTransition("share_status", a.share_status, target, ",".join(sorted(roles)))
    a_status = states.transition("share_status", a.share_status, target, roles)
    if target == "CANDIDATE" and a.data_role != "BYPRODUCT":  # 후보는 BYPRODUCT 원본만 (요구사항 1.1, domain.md 5장)
        raise RuleViolation("공유 후보는 data_role=BYPRODUCT인 원본만입니다",
                            [UnmetCondition("data_role=BYPRODUCT", a.data_role)])
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
