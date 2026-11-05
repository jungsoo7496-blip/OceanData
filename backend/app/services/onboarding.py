"""기관 약정·데이터 연계 카드 기록 (03번 요구사항 1.4·1.5, ONB-01).

- 약정: 운영기관이 체결한 약정의 참조·유효기간·허용 목적만 기록한다(원본 문서는 저장하지 않음). 13번 공유 규칙은
  `sharing.tier2_valid()`로 이 기록을 읽는다. 종료는 새 상태 코드 없이 valid_until을 당긴다.
- 연계 카드: 기관·장비 단위, 버전은 바꾸지 않고 새 버전으로만. 장비를 지정하면 카드의 local_copy_policy를 장비 기록에
  반영한다(domain.md 9장). 희망 collection_mode는 기록만 한다 — 장비 정책은 운영기관이 정한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Agreement, Device, DevicePolicy, IntegrationCard
from app.domain.errors import RuleViolation, UnmetCondition
from app.domain.ids import new_id
from app.services import contracts
from app.timefmt import iso


class CardInvalid(Exception):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("연계 카드 내용이 계약과 맞지 않습니다")
        self.errors = errors


def in_effect(a: Agreement, now: datetime) -> bool:
    return a.status == "ACTIVE" and a.valid_from <= now and (a.valid_until is None or a.valid_until > now)


def agreement_view(a: Agreement, now: datetime) -> dict[str, Any]:
    return {"agreement_id": a.agreement_id, "organization_id": a.organization_id, "tier": a.tier,
            "reference": a.reference, "valid_from": iso(a.valid_from),
            "valid_until": iso(a.valid_until) if a.valid_until else None,
            "allowed_purposes": list(a.allowed_purposes or []), "status": a.status, "in_effect": in_effect(a, now),
            "created_by": a.created_by, "created_at": iso(a.created_at)}


def check_agreement(tier: str, purposes: list[str], valid_from: datetime, valid_until: datetime | None) -> None:
    unmet: list[UnmetCondition] = []
    if tier == "TIER1" and purposes:
        unmet.append(UnmetCondition("1단(수집·관리) 약정은 허용 목적을 두지 않는다", ",".join(purposes)))
    if tier == "TIER2" and not purposes:
        unmet.append(UnmetCondition("2단(공동활용 제공) 약정은 허용 목적이 하나 이상", "없음"))
    if valid_until is not None and valid_until <= valid_from:
        unmet.append(UnmetCondition("valid_until > valid_from", iso(valid_until)))
    if unmet:
        raise RuleViolation("약정 기록 조건을 충족하지 않습니다", unmet)


def create_agreement(db: Session, *, organization_id: str, tier: str, reference: str, valid_from: datetime,
                     valid_until: datetime | None, purposes: list[str], by: str, now: datetime) -> Agreement:
    check_agreement(tier, purposes, valid_from, valid_until)
    row = Agreement(agreement_id=new_id("agreement"), organization_id=organization_id, tier=tier, reference=reference,
                    valid_from=valid_from, valid_until=valid_until, allowed_purposes=sorted(set(purposes)),
                    status="ACTIVE", created_by=by, created_at=now)
    db.add(row)
    db.flush()
    return row


def end_agreement(a: Agreement, effective_at: datetime) -> datetime | None:
    """유효기간 끝을 effective_at으로 당긴다. 이미 그보다 일찍 끝났으면 바꾸지 않고 None."""
    if a.valid_until is not None and a.valid_until <= effective_at:
        return None
    if effective_at < a.valid_from:
        raise RuleViolation("종료 시각이 약정 시작보다 이릅니다",
                            [UnmetCondition("effective_at >= valid_from", iso(effective_at))])
    before = a.valid_until
    a.valid_until = effective_at
    return before or effective_at


# ------------------------------------------------------------------ 연계 카드


def check_card(content: dict[str, Any]) -> None:
    problems = contracts.schema_problems("integration-card", content)
    if problems:
        raise CardInvalid([{"pointer": "/content" + p.pointer, "code": p.code, "message": p.message}
                           for p in problems[:50]])


def linked_policies(db: Session, card_id: str) -> list[dict[str, Any]]:
    rows = db.execute(select(DevicePolicy.device_id, DevicePolicy.version).where(
        DevicePolicy.integration_card_id == card_id).order_by(DevicePolicy.device_id, DevicePolicy.version)).all()
    return [{"device_id": d, "version": v} for d, v in rows]


def card_view(db: Session, card: IntegrationCard) -> dict[str, Any]:
    return {"integration_card_id": card.integration_card_id, "version": card.version,
            "organization_id": card.organization_id, "device_id": card.device_id, "content": card.content,
            "linked_policies": linked_policies(db, card.integration_card_id), "created_by": card.created_by,
            "created_at": iso(card.created_at)}


def latest_versions(db: Session, cond: Any) -> list[IntegrationCard]:
    latest = select(IntegrationCard.integration_card_id, func.max(IntegrationCard.version).label("v")) \
        .where(cond).group_by(IntegrationCard.integration_card_id).subquery()
    return list(db.execute(select(IntegrationCard).join(
        latest, (IntegrationCard.integration_card_id == latest.c.integration_card_id)
        & (IntegrationCard.version == latest.c.v)).order_by(IntegrationCard.integration_card_id)).scalars())


def write_card(db: Session, *, organization_id: str, card_id: str | None, device: Device | None,
               content: dict[str, Any], by: str, now: datetime) -> tuple[IntegrationCard, dict[str, Any] | None]:
    """새 카드(버전 1) 또는 새 버전. 장비 local_copy_policy가 바뀌면 (이전, 이후)를 돌려준다."""
    check_card(content)
    if card_id is None:
        card_id, version = new_id("integration_card"), 1
    else:
        # 집계와 FOR UPDATE는 함께 못 쓴다: 기존 버전 행을 잠근 뒤 최댓값 (동시 새 버전 직렬화)
        versions = db.execute(select(IntegrationCard.version).where(
            IntegrationCard.integration_card_id == card_id).with_for_update()).scalars().all()
        version = max(versions, default=0) + 1
    card = IntegrationCard(integration_card_id=card_id, version=version, organization_id=organization_id,
                           device_id=device.device_id if device else None, content=content, created_by=by,
                           created_at=now)
    db.add(card)
    changed = None
    policy = (content.get("current_storage") or {}).get("local_copy_policy")
    if device is not None and policy and policy != device.local_copy_policy:
        changed = {"device_id": device.device_id, "from": device.local_copy_policy, "to": policy}
        device.local_copy_policy = policy
    db.flush()
    return card, changed
