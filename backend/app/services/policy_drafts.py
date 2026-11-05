"""장비 정책 초안 (09번 요구사항 3.2·3.3·3.4, ADR-0037).

- 초안은 장비 기록·장비 정책을 바꾸지 않는다. 장비마다 검토 중(DRAFT) 초안은 하나다.
- 연계 카드에서는 구조화된 값 세 개만 미리 채운다 (domain/onboarding.CARD_PREFILL). 나머지는 사람이 넣거나 '확인 필요'.
- 적용은 운영자만 한다(API에서 device_policy:write, 다중 인증). 관리 명령 set-policy와 같은 저장 길
  (services/device_policy.store_policy)로 수집기 정책 새 버전을 쓰고, 초안이 정한 항목을 장비 기록에 맞춘다.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Device, IntegrationCard, PolicyDraft
from app.domain import onboarding as rules
from app.domain.errors import RuleViolation, UnmetCondition
from app.domain.ids import new_id
from app.services import device_policy
from app.services.contracts import schema_problems
from app.timefmt import iso

# 값의 형식은 장비 정책 계약의 같은 항목으로 검사한다 (contracts/schemas/device-policy.schema.json)
_FRAGMENT = {f: f"#/properties/sources/items/properties/{f}" for f in rules.DRAFT_FIELDS}
_FRAGMENT["transfer_windows"] = "#/properties/transfer_windows"


class DraftConflict(Exception):
    """상태·revision 충돌 (409)."""


def input_errors(values: dict[str, Any], unset: list[str]) -> list[dict[str, str]]:
    """values·needs_confirmation 검사. 비어 있으면 통과 (400 VALIDATION_ERROR의 errors)."""
    errors: list[dict[str, str]] = []
    for key, value in values.items():
        if key not in rules.DRAFT_FIELDS:
            errors.append({"pointer": f"/values/{key}", "code": "additionalProperties",
                           "message": "초안 항목이 아닙니다"})
            continue
        found = schema_problems("device-policy", value, fragment=_FRAGMENT[key])
        errors += [{"pointer": f"/values/{key}{p.pointer}", "code": p.code, "message": p.message} for p in found]
        if key == "source_timezone" and isinstance(value, str) and not found:
            try:  # 형식(정규식)만 맞는 이름은 받지 않는다 — 관측값 시각 변환이 이 이름으로 시간대를 찾는다
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                errors.append({"pointer": "/values/source_timezone", "code": "unknown_timezone",
                               "message": "IANA 시간대 이름이 아닙니다 (예: Asia/Seoul)"})
    if len(set(unset)) != len(unset):
        errors.append({"pointer": "/needs_confirmation", "code": "uniqueItems",
                       "message": "같은 항목이 두 번 있습니다"})
    both = sorted(set(unset) & set(values))
    if both:
        errors.append({"pointer": "/needs_confirmation", "code": "conflict",
                       "message": f"values와 needs_confirmation에 함께 있습니다: {', '.join(both)}"})
    return errors


def collector_of(device: Device) -> str | None:
    """정책이 배포되는 수집기. 수집기 자신도 원천이 될 수 있다 (domain.md 1장)."""
    return device.device_id if device.kind == "COLLECTOR" else device.collector_device_id


def _check_target(device: Device, card: IntegrationCard | None) -> str:
    unmet: list[UnmetCondition] = []
    collector_id = collector_of(device)
    if collector_id is None:
        unmet.append(UnmetCondition("수집기에 매핑된 원천 장비이거나 수집기", "매핑 없음"))
    if card is not None:
        if card.organization_id != device.organization_id:
            unmet.append(UnmetCondition("장비와 같은 기관의 연계 카드", "다른 기관"))
        elif card.device_id is not None and card.device_id not in (device.device_id, device.collector_device_id):
            unmet.append(UnmetCondition("연계 카드의 장비가 이 장비 또는 그 수집기", card.device_id))
    if unmet or collector_id is None:
        raise RuleViolation("정책 초안을 만들 수 없습니다", unmet)
    return collector_id


def create(db: Session, *, device: Device, card: IntegrationCard | None, values: dict[str, Any], unset: list[str],
           note: str | None, by: str, now: datetime) -> PolicyDraft:
    collector_id = _check_target(device, card)
    # 같은 장비의 초안 만들기를 직렬화한다 (부분 고유 색인이 마지막 방어선)
    db.execute(select(Device.device_id).where(Device.device_id == device.device_id).with_for_update())
    open_id = db.execute(select(PolicyDraft.policy_draft_id).where(
        PolicyDraft.device_id == device.device_id, PolicyDraft.status == "DRAFT")).scalar_one_or_none()
    if open_id is not None:
        raise DraftConflict(f"이 장비에는 검토 중인 초안이 있습니다 ({open_id}). 그 초안을 고치세요")
    fields = rules.initial_fields(card.content if card else None, values, set(unset))
    for name, field in fields.items():
        if field["status"] == rules.FROM_CARD and input_errors({name: field["value"]}, []):
            fields[name] = dict(rules.UNSET)
    d = PolicyDraft(policy_draft_id=new_id("policy_draft"), organization_id=device.organization_id,
                    device_id=device.device_id, collector_device_id=collector_id,
                    integration_card_id=card.integration_card_id if card else None,
                    integration_card_version=card.version if card else None, fields=fields, note=note or None,
                    status="DRAFT", revision=1, created_by=by, created_at=now, updated_by=by, updated_at=now)
    db.add(d)
    db.flush()
    return d


def _require_open(d: PolicyDraft, revision: int | None) -> None:
    if d.status != "DRAFT":
        raise DraftConflict(f"이미 처리된 초안입니다 ({d.status})")
    if revision is not None and revision != d.revision:
        raise DraftConflict(f"초안이 그 사이 고쳐졌습니다 (지금 revision {d.revision}). 다시 확인하세요")


def update(d: PolicyDraft, *, revision: int, values: dict[str, Any], unset: list[str], note: str | None,
           note_given: bool, by: str, now: datetime) -> list[dict[str, Any]]:
    """바뀐 항목 전후 (감사용, 메모는 길이만). 바뀐 것이 없으면 revision을 올리지 않는다."""
    _require_open(d, revision)
    fields, changes = rules.edit_fields(d.fields, values, set(unset))
    if note_given and (note or None) != d.note:
        changes.append({"field": "note", "before": {"length": len(d.note or "")}, "after": {"length": len(note or "")}})
        d.note = note or None
    if changes:
        d.fields = fields
        d.revision += 1
        d.updated_by, d.updated_at = by, now
    return changes


def _plan(db: Session, d: PolicyDraft) -> tuple[rules.Plan, int | None, Device]:
    device = db.get(Device, d.device_id)
    collector = db.get(Device, d.collector_device_id)
    if device is None or collector is None:  # 장비는 지우지 않으므로 생기지 않는다
        raise RuleViolation("장비를 찾을 수 없습니다", [UnmetCondition("장비 기록", d.device_id)])
    current, stored_version = device_policy.current_policy(db, collector)
    return rules.plan(current, d.device_id, device_policy.source_entry(db, device), d.fields), stored_version, device


def preview(db: Session, d: PolicyDraft) -> dict[str, Any]:
    """지금 상태에서 적용하면 어떻게 되는지. blockers가 있으면 적용은 422."""
    p, stored_version, _device = _plan(db, d)
    doc = {**p.policy, "policy_version": f"pv-{device_policy.next_version(db, d.collector_device_id)}"}
    blockers = device_policy.policy_problems(db, d.collector_device_id, doc, d.integration_card_id)
    return {"collector_device_id": d.collector_device_id, "current_policy_version": stored_version,
            "source_in_current_policy": p.source_in_current, "current": p.current, "proposed": p.proposed,
            "changes": p.changes, "blockers": [{"condition": u.condition, "actual": u.actual} for u in blockers]}


def apply(db: Session, d: PolicyDraft, *, revision: int, expected_policy_version: int | None, reason: str | None,
          by: str, now: datetime) -> dict[str, Any]:
    """수집기 정책 새 버전 저장 + 장비 기록 반영. 호출자가 초안 행을 잠근다. 감사용 전후 값을 돌려준다."""
    _require_open(d, revision)
    # 잠금 순서: 초안(호출자) → 장비 → 수집기 (store_policy가 수집기를 다시 잠가도 같은 트랜잭션이다)
    db.execute(select(Device.device_id).where(Device.device_id == d.device_id).with_for_update())
    db.execute(select(Device.device_id).where(Device.device_id == d.collector_device_id).with_for_update())
    p, stored_version, device = _plan(db, d)
    if stored_version != expected_policy_version:  # 수집기 전체 항목(transfer_windows)을 보지 못한 값으로 덮지 않는다
        now_label = f"v{stored_version}" if stored_version is not None else "저장본 없음"
        raise DraftConflict(f"수집기 정책이 미리보기 뒤 바뀌었습니다 (지금 {now_label}). 다시 확인하세요")
    doc = {**p.policy, "policy_version": f"pv-{device_policy.next_version(db, d.collector_device_id)}"}
    row = device_policy.store_policy(db, d.collector_device_id, doc, integration_card_id=d.integration_card_id,
                                     by=by, now=now)
    device_changes: list[dict[str, Any]] = []
    for field in rules.DEVICE_COLUMNS:  # 초안이 정한 항목만 (확인 필요 항목은 장비 기록도 그대로)
        state = d.fields.get(field, rules.UNSET)
        if state["status"] == rules.NEEDS_CONFIRMATION:
            continue
        before, after = getattr(device, field), copy.deepcopy(state["value"])
        if before != after:
            setattr(device, field, after)
            device_changes.append({"field": field, "scope": "SOURCE", "before": before, "after": after})
    d.status, d.decided_by, d.decided_at, d.decision_reason = "APPLIED", by, now, reason
    d.updated_by, d.updated_at = by, now
    d.applied = {"collector_device_id": d.collector_device_id, "device_policy_version": row.version,
                 "policy_version": doc["policy_version"], "changes": p.changes, "device_changes": device_changes}
    db.flush()
    return {"device_policy_version": row.version, "previous_policy_version": stored_version,
            "policy_version": doc["policy_version"], "before": p.current, "after": p.proposed,
            "changes": p.changes, "device_changes": device_changes,
            "source_in_current_policy": p.source_in_current}


def reject(d: PolicyDraft, *, reason: str, by: str, now: datetime) -> None:
    _require_open(d, None)
    d.status, d.decided_by, d.decided_at, d.decision_reason = "REJECTED", by, now, reason
    d.updated_by, d.updated_at = by, now


def view(db: Session, d: PolicyDraft, *, with_preview: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "policy_draft_id": d.policy_draft_id, "organization_id": d.organization_id, "device_id": d.device_id,
        "collector_device_id": d.collector_device_id, "integration_card_id": d.integration_card_id,
        "integration_card_version": d.integration_card_version, "status": d.status, "revision": d.revision,
        "fields": {f: d.fields.get(f, rules.UNSET) for f in rules.DRAFT_FIELDS},
        "needs_confirmation": rules.needs_confirmation(d.fields), "note": d.note,
        "created_by": d.created_by, "created_at": iso(d.created_at), "updated_by": d.updated_by,
        "updated_at": iso(d.updated_at), "decided_by": d.decided_by,
        "decided_at": iso(d.decided_at) if d.decided_at else None, "decision_reason": d.decision_reason,
        "applied": d.applied,
    }
    if with_preview and d.status == "DRAFT":
        out["preview"] = preview(db, d)
    return out
