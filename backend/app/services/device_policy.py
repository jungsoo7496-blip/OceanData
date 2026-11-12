"""수집기 정책 (03번 요구사항 1.3, 09번 요구사항 3.3).

정책은 수집기 단위로 배포한다(device-policy.schema.json). 저장본이 없으면 장비 표로 만든 기본 정책을 준다.
새 버전 저장은 한 길로만 한다: 관리 명령 `set-policy`와 정책 초안 적용(09번)이 모두 `store_policy`를 쓴다 —
계약 스키마·매핑(그 수집기에 매핑된 같은 기관 원천 장비)·같은 기관 연계 카드 검사를 통과해야 저장한다.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Device, DevicePolicy, IntegrationCard
from app.domain.errors import RuleViolation, UnmetCondition
from app.services.contracts import Problem, schema_problems


def mapping_version(db: Session, device_id: str) -> str | None:
    from app.services.observations import current_mapping

    m = current_mapping(db, device_id)
    return f"m{m.version}" if m else None


def source_entry(db: Session, d: Device) -> dict[str, Any]:
    """장비 기록으로 만든 정책 항목 (기본 정책과 같은 값). 허용 data_role 기본값은 CORE (domain.md 3장)."""
    entry: dict[str, Any] = {"device_id": d.device_id}
    if d.project_id:
        entry["project_id"] = d.project_id
    entry.update({"collection_mode": d.collection_mode, "local_copy_policy": d.local_copy_policy,
                  "local_retention_days": d.local_retention_days, "allowed_data_roles": ["CORE"],
                  "expected_interval_s": d.expected_interval_s, "source_timezone": d.source_timezone,
                  "observation_mapping_version": mapping_version(db, d.device_id)})
    return entry


def default_policy(db: Session, collector: Device) -> dict[str, Any]:
    sources = db.execute(select(Device).where(Device.collector_device_id == collector.device_id,
                                              Device.organization_id == collector.organization_id,
                                              Device.status != "REVOKED").order_by(Device.device_id)).scalars()
    return {
        "policy_version": "pv-0",
        "collector_device_id": collector.device_id,
        "sources": [source_entry(db, d) for d in sources if d.project_id],
        "watch_rules": [],
        "timezone": "UTC",
        "heartbeat_interval_s": 60,
        "size_stable_s": 30,
    }


def latest_stored(db: Session, collector_id: str) -> DevicePolicy | None:
    return db.execute(select(DevicePolicy).where(DevicePolicy.device_id == collector_id)
                      .order_by(DevicePolicy.version.desc()).limit(1)).scalar_one_or_none()


def current_policy(db: Session, collector: Device) -> tuple[dict[str, Any], int | None]:
    """지금 수집기 정책 문서와 저장 버전 (저장본이 없으면 기본 정책과 None)."""
    stored = latest_stored(db, collector.device_id)
    if stored is None:
        return default_policy(db, collector), None
    return copy.deepcopy(stored.policy), stored.version


def next_version(db: Session, collector_id: str) -> int:
    current = db.execute(select(func.max(DevicePolicy.version)).where(DevicePolicy.device_id == collector_id)).scalar()
    return int(current or 0) + 1


def policy_schema_problems(doc: Any, *, stored: bool) -> list[Problem]:
    fragment = "#/$defs/storedPolicy" if stored else ""
    return schema_problems("device-policy", doc, fragment=fragment)


def policy_problems(db: Session, collector_id: str, doc: Any, integration_card_id: str | None) -> list[UnmetCondition]:
    """저장하기 전 검사. 비어 있으면 저장할 수 있다."""
    found = policy_schema_problems(doc, stored=True)
    if found:  # 형식이 틀리면 아래 검사를 할 수 없다
        return [UnmetCondition(f"장비 정책 계약 {p.pointer or '/'}", p.message) for p in found[:20]]
    collector = db.get(Device, collector_id)
    if collector is None or collector.kind != "COLLECTOR" or doc["collector_device_id"] != collector_id:
        return [UnmetCondition("수집기 장비이고 collector_device_id가 그 수집기", doc["collector_device_id"])]
    out: list[UnmetCondition] = []
    for src in doc["sources"]:  # 정책의 원천 장비는 그 수집기에 매핑된 같은 기관 장비여야 한다 (수집기 자신도 된다)
        s_dev = db.get(Device, src["device_id"])
        if s_dev is None or s_dev.organization_id != collector.organization_id or \
                (s_dev.collector_device_id != collector_id and s_dev.device_id != collector_id):
            out.append(UnmetCondition("원천 장비는 이 수집기에 매핑된 같은 기관 장비", src["device_id"]))
    if integration_card_id is not None:  # 연계 카드에서 만든 정책이면 같은 기관의 카드여야 한다 (03번 요구사항 1.5)
        owner = db.execute(select(IntegrationCard.organization_id).where(
            IntegrationCard.integration_card_id == integration_card_id).limit(1)).scalar_one_or_none()
        if owner != collector.organization_id:
            out.append(UnmetCondition("같은 기관의 연계 카드", integration_card_id))
    return out


def store_policy(db: Session, collector_id: str, doc: dict[str, Any], *, integration_card_id: str | None, by: str,
                 now: datetime) -> DevicePolicy:
    """검사를 통과한 정책을 새 버전으로 저장한다. 수집기 행을 잠가 버전 번호를 직렬화한다.

    같은 트랜잭션에서 호출자가 이미 잠갔으면 다시 잠가도 그대로다."""
    db.execute(select(Device.device_id).where(Device.device_id == collector_id).with_for_update())
    problems = policy_problems(db, collector_id, doc, integration_card_id)
    collector = db.get(Device, collector_id)
    if problems or collector is None:
        raise RuleViolation("장비 정책 조건을 충족하지 않습니다", problems)
    row = DevicePolicy(device_id=collector_id, version=next_version(db, collector_id),
                       organization_id=collector.organization_id, policy=doc, integration_card_id=integration_card_id,
                       created_by=by, created_at=now)
    db.add(row)
    db.flush()
    return row
