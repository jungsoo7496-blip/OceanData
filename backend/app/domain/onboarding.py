"""온보딩 규칙 — 연계 카드 '확인 필요' 항목, 연계 난이도, 장비 정책 초안 (09번 요구사항 3.2·3.4, ADR-0037).

프레임워크·DB를 쓰지 않는다 (structure.md 2장).
- 연계 카드의 열린 항목: 기준 문서 부록 A의 15개 분야가 비어 있거나, 값에 '확인 필요'가 적혀 있거나, 현장 사본 정책이
  UNKNOWN(모름, domain.md 9장)이면 열린 항목이다.
- 연계 난이도 등급: 판정 기준이 어디에도 정해져 있지 않다. 등급을 지어내지 않고 '기준 미정 — 확인 필요'로 둔다.
- 정책 초안 미리 채우기: 카드의 구조화된 값 세 개만 쓴다. 자유 기록(note 등)에서는 값을 뽑지 않는다.
- 초안 → 정책: 지금 수집기 정책에서 그 장비 항목만 바꾸고(transfer_windows는 수집기 전체),
  '확인 필요' 항목은 그대로 둔다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

TBD_MARK = "확인 필요"

# 기준 문서 부록 A의 분야 (integration-card.schema.json 최상위 항목, 표 순서)
CARD_SECTIONS: tuple[tuple[str, str], ...] = (
    ("basic", "기본"), ("equipment", "장비"), ("data", "데이터"), ("volume", "발생량"), ("time_sync", "시각"),
    ("network", "통신"), ("current_storage", "현행 저장"), ("collection_mode_requested", "수집 수준"),
    ("core_byproduct", "핵심/부산물"), ("rights_privacy", "권리·개인정보"), ("integration_methods", "연계 방식"),
    ("desired_services", "원하는 서비스"), ("report_forms", "보고 양식"), ("samples", "샘플"),
    ("termination", "종료 시"),
)

# 연계 난이도 등급: 기준 문서는 '연계 카드 → 난이도 등급 → PoC → 확대'(19장)만 말하고 판정 기준은 없다 (ADR-0037 미결)
DIFFICULTY_GRADE: dict[str, Any] = {"grade": None, "criteria_defined": False, "note": "기준 미정 — 확인 필요"}

# 정책 초안 항목 (요구사항 3.2). transfer_windows만 수집기 정책 최상위, 나머지는 sources[]의 그 장비 항목
DRAFT_FIELDS: tuple[str, ...] = ("collection_mode", "allowed_data_roles", "local_copy_policy", "local_retention_days",
                                 "expected_interval_s", "transfer_windows", "source_timezone")
COLLECTOR_FIELDS = frozenset({"transfer_windows"})
# 장비 기록에도 있는 항목 — 수집 서버의 variant 허용(ingest)·시각 변환(observations)·정상성 판정(monitoring)이 쓴다
DEVICE_COLUMNS: tuple[str, ...] = ("collection_mode", "local_copy_policy", "local_retention_days",
                                   "expected_interval_s", "source_timezone")
# 카드에서 미리 채우는 항목 → 카드 내용의 위치. 이 세 개 밖의 값은 카드에서 가져오지 않는다
CARD_PREFILL: dict[str, tuple[str, ...]] = {
    "collection_mode": ("collection_mode_requested",),
    "local_copy_policy": ("current_storage", "local_copy_policy"),
    "local_retention_days": ("current_storage", "local_retention_days"),
}
FROM_CARD, ENTERED, NEEDS_CONFIRMATION = "FROM_CARD", "ENTERED", "NEEDS_CONFIRMATION"
UNSET: dict[str, Any] = {"status": NEEDS_CONFIRMATION}


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _pointer(*parts: str | int) -> str:
    return "".join("/" + _escape(str(p)) for p in parts)


def is_empty(value: Any) -> bool:
    """값이 없는 분야인가. False·0은 답이 있는 것이다 (예: 7일 실측 안 함)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return all(is_empty(v) for v in value)
    if isinstance(value, dict):
        return all(is_empty(v) for v in value.values())
    return False


def tbd_pointers(value: Any, *path: str | int) -> list[str]:
    """'확인 필요'가 적힌 문자열 값의 위치 (JSON Pointer). 객체 키는 이름순 — JSONB는 키 순서를 보존하지 않는다."""
    if isinstance(value, str):
        return [_pointer(*path)] if TBD_MARK in value else []
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in tbd_pointers(v, *path, i)]
    if isinstance(value, dict):
        return [p for k, v in sorted(value.items()) for p in tbd_pointers(v, *path, k)]
    return []


def open_items(content: dict[str, Any]) -> list[dict[str, Any]]:
    """카드에서 비어 있거나 '확인 필요'로 남은 분야 (요구사항 3.4). 부록 A 순서."""
    out: list[dict[str, Any]] = []
    for key, label in CARD_SECTIONS:
        value = content.get(key)
        if is_empty(value):
            out.append({"section": key, "label": label, "state": "EMPTY", "pointers": []})
            continue
        pointers = tbd_pointers(value, key)
        if key == "current_storage" and isinstance(value, dict) and value.get("local_copy_policy") == "UNKNOWN":
            pointers.append(_pointer(key, "local_copy_policy"))  # 현장 사본 정책 모름 (domain.md 9장)
        if pointers:
            out.append({"section": key, "label": label, "state": NEEDS_CONFIRMATION, "pointers": pointers})
    return out


def prefill(content: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """카드의 구조화된 값으로 채울 수 있는 초안 항목. 값이 없으면 그 항목은 빠진다."""
    out: dict[str, dict[str, Any]] = {}
    for field, path in CARD_PREFILL.items():
        node: Any = content or {}
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if node is not None:
            out[field] = {"status": FROM_CARD, "value": copy.deepcopy(node), "card_pointer": _pointer(*path)}
    return out


def initial_fields(card_content: dict[str, Any] | None, values: dict[str, Any],
                   unset: set[str]) -> dict[str, dict[str, Any]]:
    """새 초안: 넣은 값(ENTERED) > '확인 필요'로 둔 항목 > 카드 값(FROM_CARD) > 확인 필요."""
    from_card = prefill(card_content)
    out: dict[str, dict[str, Any]] = {}
    for field in DRAFT_FIELDS:
        if field in values:
            out[field] = {"status": ENTERED, "value": copy.deepcopy(values[field])}
        elif field in unset or field not in from_card:
            out[field] = dict(UNSET)
        else:
            out[field] = from_card[field]
    return out


def edit_fields(fields: dict[str, dict[str, Any]], values: dict[str, Any],
                unset: set[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """초안 고치기: 넣은 값은 ENTERED, unset은 확인 필요로. 보내지 않은 항목은 그대로. (새 항목, 바뀐 항목 전후)."""
    new = copy.deepcopy(fields)
    changes: list[dict[str, Any]] = []
    for field in DRAFT_FIELDS:
        if field in values:
            after: dict[str, Any] = {"status": ENTERED, "value": copy.deepcopy(values[field])}
        elif field in unset:
            after = dict(UNSET)
        else:
            continue
        if after != fields.get(field):
            changes.append({"field": field, "before": fields.get(field), "after": after})
            new[field] = after
    return new, changes


def needs_confirmation(fields: dict[str, dict[str, Any]]) -> list[str]:
    return [f for f in DRAFT_FIELDS if fields.get(f, UNSET)["status"] == NEEDS_CONFIRMATION]


def settings_of(policy: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """정책 문서·장비 항목에서 초안 항목 일곱 개의 값. transfer_windows가 없으면 빈 목록(제한 없음)."""
    out = {f: copy.deepcopy(entry.get(f)) for f in DRAFT_FIELDS if f not in COLLECTOR_FIELDS}
    out["transfer_windows"] = copy.deepcopy(policy.get("transfer_windows") or [])
    return {f: out[f] for f in DRAFT_FIELDS}


def same_value(field: str, a: Any, b: Any) -> bool:
    if field == "allowed_data_roles" and isinstance(a, list) and isinstance(b, list):
        return sorted(a) == sorted(b)  # 허용 역할은 집합이다 (순서 무관)
    return bool(a == b)


@dataclass
class Plan:
    policy: dict[str, Any]          # 적용 후 수집기 정책 문서 (policy_version은 저장할 때 정한다)
    source_in_current: bool         # 지금 정책에 이 장비 항목이 있었나
    current: dict[str, Any]         # 지금 값 (일곱 항목)
    proposed: dict[str, Any]        # 적용 후 값
    changes: list[dict[str, Any]]   # 바뀌는 항목 [{field, scope, before, after}]


def plan(current_policy: dict[str, Any], device_id: str, base_entry: dict[str, Any],
         fields: dict[str, dict[str, Any]]) -> Plan:
    """초안을 지금 수집기 정책에 얹은 결과. '확인 필요' 항목은 지금 값 그대로다.

    base_entry: 지금 정책에 그 장비 항목이 없을 때 쓸 항목 (장비 기록으로 만든 기본 정책 항목과 같다)."""
    doc = copy.deepcopy(current_policy)
    sources: list[dict[str, Any]] = doc.setdefault("sources", [])
    index = next((i for i, s in enumerate(sources) if s.get("device_id") == device_id), None)
    entry = copy.deepcopy(sources[index] if index is not None else base_entry)
    current = settings_of(current_policy, entry)
    for field in DRAFT_FIELDS:
        state = fields.get(field, UNSET)
        if state["status"] == NEEDS_CONFIRMATION:
            continue
        if field in COLLECTOR_FIELDS:
            doc[field] = copy.deepcopy(state["value"])
        else:
            entry[field] = copy.deepcopy(state["value"])
    if index is None:
        sources.append(entry)
    else:
        sources[index] = entry
    proposed = settings_of(doc, entry)
    changes = [{"field": f, "scope": "COLLECTOR" if f in COLLECTOR_FIELDS else "SOURCE",
                "before": current[f], "after": proposed[f]}
               for f in DRAFT_FIELDS if not same_value(f, current[f], proposed[f])]
    return Plan(doc, index is not None, current, proposed, changes)
